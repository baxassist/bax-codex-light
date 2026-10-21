"""Медленное чтение истории не занимает канал команд и не переживает свой запрос."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from bax_codex_light.connection import Relay
from bax_codex_light.registry import Registration


@pytest.mark.parametrize("replacement", ["subscribe", "session.select", "session.close", "disconnect"])
async def test_commands_pass_suspended_history_and_cancel_obsolete_snapshot(tmp_path, replacement):
    entered, cancelled, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    connection = None
    received = []

    async def server(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": "agent"}))
        await ws.send(json.dumps({"v": 1, "type": "subscribe", "rid": "old"}))
        await asyncio.wait_for(entered.wait(), 2)
        # Эта квитанция должна прийти ДО окончания чтения/передачи истории.
        await ws.send(json.dumps({"v": 1, "type": "run", "rid": "run"}))
        received.append(json.loads(await asyncio.wait_for(ws.recv(), 2)))
        if replacement == "disconnect":
            await ws.close()
        else:
            await ws.send(json.dumps({"v": 1, "type": replacement, "rid": "new"}))
            received.append(json.loads(await asyncio.wait_for(ws.recv(), 2)))
        await asyncio.wait_for(cancelled.wait(), 2)
        completed.set()
        await ws.wait_closed()

    async def ready():
        pass

    async def handle(message):
        if message.get("rid") == "old":
            entered.set()
            try:
                await release.wait()
                await connection.send("message", text="obsolete snapshot")
            finally:
                cancelled.set()
        elif message["type"] == "run":
            await connection.send("message", rid="run", kind="user", text="accepted")
        else:
            await connection.send("history.done" if replacement == "subscribe" else "session.changed")

    async with serve(server, "127.0.0.1", 0, ping_interval=None) as listener:
        url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/agent"
        reg = Registration(agent="agent", key_id="key", secret="secret", server=url, install_id="install")
        connection = Relay(reg, tmp_path, "current", keep_awake=False)
        task = asyncio.create_task(connection.session(ready, handle))
        try:
            await asyncio.wait_for(completed.wait(), 5)
            assert received[0]["rid"] == "run"
            assert "history_rid" not in received[0]
            if replacement == "subscribe":
                assert received[1]["history_rid"] == "new"
            assert all(item.get("text") != "obsolete snapshot" for item in received)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
