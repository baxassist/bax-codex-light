import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import FakeApp, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.server import serve

from bax_codex_light.protocol import sign
from bax_codex_light.registry import Registry

BUNDLE_BINARY = (
    Path(__file__).resolve().parents[1] / "dist/Bax Codex.app/Contents/Resources/agent/bax-codex-light"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_pairing_stdio_to_relay_to_exact_codex_thread(tmp_path, packaged):
    binary = BUNDLE_BINARY
    if packaged and not binary.is_file():
        pytest.skip("Сначала macos/build.sh")
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
        redeemed = []

        class PairingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                assert self.path == "/api/v1/agents/pairing/redeem"
                redeemed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                payload = json.dumps({"key": f"{agent}:{key_id}:{secret}", "relay": relay_url}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), PairingHandler)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        args = [
            "serve",
            "--auto-project",
            "--app-server",
            endpoint,
            "--api",
            f"http://127.0.0.1:{http.server_port}/api/v1",
            "--registry",
            str(tmp_path / "registry.json"),
        ]
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        params = StdioServerParameters(
            command=str(binary) if packaged else sys.executable,
            args=args if packaged else ["-m", "bax_codex_light", *args],
            env=env,
        )
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                attached = await session.call_tool("bax_attach", {"thread_id": "current"})
                assert not attached.is_error
                assert attached.structured_content["project"] == str(project)
                paired = await session.call_tool("bax_pair", {"code": "ABCD-EFGH-JKLM"})
                assert not paired.is_error
                assert secret not in paired.model_dump_json()
                await asyncio.wait_for(connected.wait(), 5)
                await until(lambda: any(m.get("text") == "Последний ответ" for m in messages), timeout=5)
                stored = Registry(tmp_path / "registry.json").get(project)
                assert stored.agent == agent
                assert stored.secret == secret
                assert redeemed == [{"code": "ABCDEFGHJKLM"}]
                await sockets[0].send(
                    json.dumps({"v": 1, "type": "run", "text": "Добавь страницу контактов"})
                )
                await asyncio.wait_for(fake.started.wait(), 5)
                start = next(c for c in fake.calls if c["method"] == "turn/start")
                assert start["params"]["threadId"] == "current"
                assert "model" not in start["params"]
                assert "approvalPolicy" not in start["params"]
        finally:
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            await asyncio.to_thread(worker.join, 2)
        await until(lambda: not fake.connections)
