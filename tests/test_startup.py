from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from uuid import uuid4

import pytest
from conftest import FakeApp, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_integration import registration
from websockets.asyncio.server import serve

from bax_codex_light.appserver import AppServer
from bax_codex_light.bridge import Bridge
from bax_codex_light.connection import Relay, network_error
from bax_codex_light.registry import Registry


async def test_mcp_tools_available_when_exact_thread_socket_is_missing(tmp_path):
    missing = f"/tmp/bax-missing-{uuid4().hex}.sock"
    registry = Registry(tmp_path / "registry.json")
    registry.put(tmp_path, registration("ws://localhost:1"))
    original = registry.path.read_bytes()
    env = dict(os.environ, CODEX_THREAD_ID="current")
    env.pop("BAX_CODEX_THREAD_ID", None)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "bax_codex_light",
            "serve",
            "--auto-project",
            "--registry",
            str(registry.path),
            "--app-server",
            missing,
        ],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), 5)
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "bax_status",
            "bax_attach",
            "bax_connect",
            "bax_resolve_question",
            "bax_publish",
        }
        for _ in range(30):
            result = await session.call_tool("bax_status", {})
            assert not result.is_error
            status = result.structured_content
            if status["error_code"]:
                break
            await asyncio.sleep(0.01)
        assert status["error_code"] == "codex_unavailable"
        assert missing in status["error"]
        assert "Откройте Codex" in status["error"]
        assert status["thread_id"] == "current"
        assert status["project"] is None
    assert registry.path.read_bytes() == original


async def test_start_does_not_wait_for_codex_or_die_on_permission_error(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def blocked_open(self, handler=None):
        await gate.wait()
        raise PermissionError("private-error-do-not-print")

    monkeypatch.setattr(AppServer, "open", blocked_open)
    bridge = Bridge(None, Registry(tmp_path / "registry.json"), "current")
    try:
        await asyncio.wait_for(bridge.start(), 0.1)
        assert bridge.status()["thread_id"] == "current"
        gate.set()
        await until(lambda: bool(bridge.error_code))
        status = bridge.status()
        assert status["error_code"] == "codex_socket_permission_denied"
        assert "песочнице" in status["error"]
        assert "private-error" not in status["error"]
        assert not status["connected"]
    finally:
        await bridge.close()


async def test_codex_socket_recovery_keeps_exact_thread_and_project(tmp_path):
    missing = f"/tmp/bax-missing-{uuid4().hex}.sock"
    bridge = Bridge(None, Registry(tmp_path / "registry.json"), "current", missing)
    fake = FakeApp(tmp_path)
    try:
        await bridge.start()
        await until(lambda: bridge.error_code == "codex_unavailable")
        async with fake.running() as endpoint:
            bridge.endpoint = endpoint
            await until(lambda: bridge.project == tmp_path)
            await until(lambda: bridge.error_code == "registration_missing")
            assert bridge.thread_id == "current"
            assert not any(call["method"] in {"thread/start", "turn/start"} for call in fake.calls)
    finally:
        await bridge.close()


async def test_busy_relay_explains_reason_and_reconnects_without_switching_thread(tmp_path, caplog):
    release = asyncio.Event()
    ready = asyncio.Event()
    seen_threads = []
    reg = None

    async def handler(ws):
        hello = json.loads(await ws.recv())
        seen_threads.append(hello["session_id"])
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        if len(seen_threads) == 1:
            await ws.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "error",
                        "code": "agent_busy",
                        "message": "Бакс уже подключён к другому разговору. Закройте его мост.",
                    }
                )
            )
        else:
            await release.wait()
            await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
            await ws.wait_closed()

    async def on_ready():
        ready.set()

    async def on_frame(_frame):
        pass

    async with serve(handler, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        relay = Relay(reg, tmp_path, "current")
        task = asyncio.create_task(relay.run(on_ready, on_frame))
        try:
            await until(lambda: relay.error_code == "agent_busy")
            assert "другому разговору" in relay.error
            assert "Закройте" in relay.error
            assert "agent_busy" in caplog.text
            release.set()
            await asyncio.wait_for(ready.wait(), 3)
            assert relay.connected and not relay.error and not relay.error_code
            assert seen_threads == ["current", "current"]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await relay.close()


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("secret"), "timeout"),
        (ConnectionRefusedError("secret"), "network_unavailable"),
        (ssl.SSLCertVerificationError("secret"), "tls_certificate"),
    ],
)
def test_network_diagnostics_do_not_expose_exception_secrets(error, code):
    actual_code, message = network_error(error, "релею Бакса")
    assert actual_code == code
    assert "secret" not in message


@pytest.mark.parametrize("method", ["thread/archived", "thread/closed"])
async def test_closed_exact_thread_releases_bridge(tmp_path, method):
    bridge = Bridge(tmp_path, Registry(tmp_path / "registry.json"), "current")
    bridge.task = asyncio.create_task(asyncio.sleep(60))
    await bridge.on_event({"method": method, "params": {"threadId": "other"}})
    assert not bridge.task.cancelling()
    await bridge.on_event({"method": method, "params": {"threadId": "current"}})
    with pytest.raises(asyncio.CancelledError):
        await bridge.task
    assert bridge.error_code == "codex_thread_closed"
    assert bridge.thread_id == "current"
