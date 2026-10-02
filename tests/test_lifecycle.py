from __future__ import annotations

import asyncio
import json
import os
import sys

from conftest import FakeApp, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_integration import registration
from websockets.asyncio.server import serve

from bax_codex_light.connection import Relay
from bax_codex_light.registry import Registry


async def test_mcp_eof_disconnects_bound_bridge(tmp_path):
    fake = FakeApp(tmp_path)
    connected = asyncio.Event()
    disconnected = asyncio.Event()
    reg = None

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        connected.set()
        try:
            async for _ in ws:
                pass
        finally:
            disconnected.set()

    async with fake.running() as endpoint, serve(handler, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/agent")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, reg)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "bax_codex_light",
                "serve",
                "--project",
                str(tmp_path),
                "--thread",
                "current",
                "--registry",
                str(registry.path),
                "--app-server",
                endpoint,
            ],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            await asyncio.wait_for(connected.wait(), 3)
            result = await session.call_tool("bax_status", {})
            assert result.structured_content["connected"] is True
            assert result.structured_content["thread_id"] == "current"
        await asyncio.wait_for(disconnected.wait(), 3)
        await until(lambda: not fake.connections)
        assert not any(
            call["method"] in {"thread/start", "thread/archive", "turn/interrupt"} for call in fake.calls
        )


async def test_relay_reconnects_after_network_drop(tmp_path):
    connections = 0
    ready_count = 0
    second_ready = asyncio.Event()
    reg = None

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        if connections == 1:
            await ws.close()
        else:
            await ws.wait_closed()

    async def on_ready():
        nonlocal ready_count
        ready_count += 1
        if ready_count == 2:
            second_ready.set()

    async def on_frame(_frame):
        pass

    async with serve(handler, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        relay = Relay(reg, tmp_path, "current")
        task = asyncio.create_task(relay.run(on_ready, on_frame))
        try:
            await asyncio.wait_for(second_ready.wait(), 3)
            assert relay.connected
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await relay.close()
