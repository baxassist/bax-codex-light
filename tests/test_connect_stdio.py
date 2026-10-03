import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from bax_codex_light.protocol import sign
from bax_codex_light.registry import Registry
from conftest import FakeApp, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.server import serve


@pytest.mark.parametrize("packaged", [False, True])
async def test_connect_stdio_to_relay_to_exact_codex_thread(tmp_path, packaged, installed_plugin):
    runtime = os.environ.get("BAX_TEST_PLUGIN_RUNTIME")
    if packaged and not runtime:
        pytest.skip("Сначала bootstrap.py --prepare в BAX_TEST_PLUGIN_RUNTIME (без скачивания в тестах)")
    project = tmp_path / "Мой сайт"
    project.mkdir()
    fake = FakeApp(project)
    agent, key_id, secret = str(uuid4()), str(uuid4()), "private-test-secret-123456789"
    connected = asyncio.Event()
    sockets = []
    messages = []

    async def relay(ws):
        hello = json.loads(await ws.recv())
        assert hello["engine"] == "codex_light"
        assert hello["path"] == str(project)
        assert hello["session_id"] == "current"
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "test", "ts": 1}))
        auth = json.loads(await ws.recv())
        assert auth["sign"] == sign(secret, "test", 1, key_id)
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": agent}))
        sockets.append(ws)
        connected.set()
        await ws.send(json.dumps({"v": 1, "type": "subscribe"}))
        async for payload in ws:
            messages.append(json.loads(payload))

    async with fake.running() as endpoint, serve(relay, "127.0.0.1", 0) as relay_server:
        relay_url = f"ws://127.0.0.1:{relay_server.sockets[0].getsockname()[1]}/agent"
        args = ["--app-server", endpoint, "--registry", str(tmp_path / "registry.json")]
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        if packaged:
            env["BAX_CODEX_PLUGIN_DATA"] = runtime
        params = StdioServerParameters(
            command=installed_plugin["command"] if packaged else sys.executable,
            args=[*installed_plugin["args"], *args]
            if packaged
            else ["-m", "bax_codex_light", "serve", "--auto-project", *args],
            cwd=installed_plugin["cwd"] if packaged else None,
            env=env,
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert not (await session.call_tool("bax_attach", {"thread_id": "current"})).is_error
            result = await session.call_tool(
                "bax_connect", {"key": f"{agent}:{key_id}:{secret}", "server": relay_url}
            )
            assert not result.is_error
            assert secret not in result.model_dump_json()
            await asyncio.wait_for(connected.wait(), 5)
            await until(lambda: any(m.get("text") == "Последний ответ" for m in messages), timeout=5)
            stored = Registry(tmp_path / "registry.json").get(project)
            assert stored.agent == agent
            assert stored.secret == secret
            assert not (stored and (tmp_path / "registry.json").stat().st_mode & 0o077)
            await sockets[0].send(json.dumps({"v": 1, "type": "run", "text": "Добавь страницу контактов"}))
            await asyncio.wait_for(fake.started.wait(), 5)
            start = next(c for c in fake.calls if c["method"] == "turn/start")
            assert start["params"]["threadId"] == "current"
            assert "model" not in start["params"]
            assert "approvalPolicy" not in start["params"]
        await until(lambda: not fake.connections)
