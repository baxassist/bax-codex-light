import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from conftest import until
from test_integration import registration
from websockets.asyncio.server import serve

from bax_codex_light.connection import Relay
from bax_codex_light.protocol import backoff


def test_reconnect_outage_wait_is_bounded_to_thirty_seconds():
    assert [backoff(i) for i in range(7)] == [1, 2, 5, 10, 30, 30, 30]
    assert backoff(10000) == 30


async def test_real_authenticated_connection_resets_accumulated_backoff(tmp_path, monkeypatch):
    attempts = []
    ready_count = 0
    finish = asyncio.Event()
    reg = None

    async def server(ws):
        nonlocal ready_count
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        ready_count += 1
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        if ready_count == 1:
            await ws.close(code=1012, reason=f"restart {reg.secret}")
        else:
            await finish.wait()

    def retry(attempt):
        attempts.append(attempt)
        return 0

    monkeypatch.setattr("bax_codex_light.connection.backoff", retry)
    async with serve(server, "127.0.0.1", 0, ping_interval=None) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        relay = Relay(reg, tmp_path, "current")
        relay.sleep_guard.supported = False
        relay.reconnect_attempt = 20
        task = asyncio.create_task(relay.run(AsyncMock(), AsyncMock()))
        try:
            await until(lambda: ready_count >= 2 and relay.connected)
            assert attempts == [0], "успешное ready не сбросило накопленную задержку"
            assert relay.reconnect_attempt == 0 and relay.retry_delay == 0
            assert relay.last_connected_at >= relay.last_disconnected_at > 0
            assert relay.last_close_code == 1012
            assert relay.last_close_reason == "restart [секрет скрыт]"
            assert not relay.error
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            finish.set()
