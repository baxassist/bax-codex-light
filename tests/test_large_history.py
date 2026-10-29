import asyncio
import json
from contextlib import asynccontextmanager, suppress
from tempfile import TemporaryDirectory

import pytest
from conftest import FakeApp, entry, until
from websockets.asyncio.server import serve, unix_serve
from websockets.exceptions import ConnectionClosed

from bax_codex_light import appserver
from bax_codex_light.appserver import AppServer, RPCError
from bax_codex_light.history import History


@asynccontextmanager
async def endpoint_for(fake, transport):
    if transport == "unix":
        with TemporaryDirectory(prefix="bax-history-", dir="/tmp") as directory:
            socket = directory + "/app.sock"
            async with unix_serve(fake.handle, socket):
                yield socket
    else:
        async with fake.running() as endpoint:
            yield endpoint


@pytest.mark.parametrize("transport", ["unix", "websocket"])
async def test_large_legacy_tool_output_keeps_history_and_live_connection(tmp_path, transport):
    fake = FakeApp(tmp_path)
    fake.items_error = "thread/items/list is not supported yet"
    command = {
        "id": "large-tool",
        "type": "commandExecution",
        "command": "pytest tests -q",
        "cwd": str(tmp_path),
        "commandActions": [],
        "status": "completed",
        "aggregatedOutput": "PRIVATE_LARGE_TOOL_OUTPUT\n" * 710000,
        "exitCode": 0,
    }
    page = {
        "data": [
            {
                "id": "long-turn",
                "status": "completed",
                "items": [
                    entry("user", "userMessage", "Продолжай проверку")["item"],
                    command,
                    entry("answer", text="Проверка выполнена")["item"],
                ],
            }
        ],
        "nextCursor": None,
    }
    assert len(json.dumps(page).encode()) > 16 * 1024 * 1024
    fake.turn_pages = {None: page}
    events = []

    async def on_event(event):
        events.append(event)

    async with endpoint_for(fake, transport) as endpoint:
        app = AppServer(endpoint)
        await app.open(on_event)
        try:
            await app.inspect("current", tmp_path)
            rows = await History(app, "current").page()
            assert [(row["kind"], row["text"]) for row in rows] == [
                ("user", "Продолжай проверку"),
                ("steps", "pytest tests -q"),
                ("assistant", "Проверка выполнена"),
            ]
            assert len(json.dumps(rows).encode()) < 2000
            assert "PRIVATE_LARGE_TOOL_OUTPUT" not in json.dumps(rows)
            assert not app.closed.is_set()
            await app.inspect("current", tmp_path)
            await fake.emit(
                "item/agentMessage/delta",
                {
                    "threadId": "current",
                    "turnId": "next",
                    "itemId": "next-answer",
                    "delta": "Продолжение",
                },
            )
            await until(lambda: bool(events))
            assert events[-1]["params"]["delta"] == "Продолжение"
            assert not app.closed.is_set()
            assert not any(call["method"] in {"turn/start", "thread/start"} for call in fake.calls)
        finally:
            await app.close()


async def test_oversized_reply_keeps_a_finite_bound_and_reports_the_cause(monkeypatch):
    monkeypatch.setattr(appserver, "MAX_MESSAGE_BYTES", 1024)

    async def handle(ws):
        with suppress(ConnectionClosed):
            async for raw in ws:
                message = json.loads(raw)
                if "id" not in message:
                    continue
                result = (
                    {"userAgent": "test"}
                    if message["method"] == "initialize"
                    else {
                        "payload": "PRIVATE_OVERSIZED_BODY" * 200,
                    }
                )
                await ws.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(handle, "127.0.0.1", 0) as server:
        app = AppServer(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        await app.open()
        try:
            with pytest.raises(RPCError, match="Ответ Codex превышает предел чтения \\(1024 байт\\)"):
                await app.request("test/large", {})
            await asyncio.wait_for(app.closed.wait(), 1)
            assert "PRIVATE_OVERSIZED_BODY" not in app.disconnect_error
            assert not app.pending
        finally:
            await app.close()
