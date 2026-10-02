import asyncio

import pytest
from conftest import FakeApp, until

from bax_codex_light.appserver import AppServer, RPCError


async def test_attach_uses_sdk_and_preserves_settings(tmp_path):
    fake = FakeApp(tmp_path)
    events = []

    async def receive(event):
        # RPC из обработчика события не должен блокировать чтение своего ответа.
        await app.inspect("current", tmp_path)
        events.append(event)

    async with fake.running() as endpoint:
        app = AppServer(endpoint, timeout=1)
        try:
            await app.open(receive)
            assert (await app.attach("current", tmp_path))["id"] == "current"
            resume = next(c for c in fake.calls if c["method"] == "thread/resume")
            assert resume["params"] == {"threadId": "current", "excludeTurns": True}
            assert len((await app.items("current", None, 50))["data"]) == 2
            await fake.emit("thread/status/changed", {"threadId": "current", "status": {"type": "idle"}})
            await until(lambda: bool(events))
            with pytest.raises(RPCError, match="test"):
                await app.request("test/error", {})
        finally:
            await app.close()


@pytest.mark.parametrize("state", ["notLoaded", "systemError"])
async def test_never_resumes_closed_thread(tmp_path, state):
    fake = FakeApp(tmp_path)
    fake.state = state
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            with pytest.raises(RPCError, match="не открыта"):
                await app.attach("current", tmp_path)
            assert not any(c["method"] == "thread/resume" for c in fake.calls)
        finally:
            await app.close()


async def test_rejects_other_thread_and_project(tmp_path):
    fake = FakeApp(tmp_path)
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            for thread_id, project in [("other", tmp_path), ("current", tmp_path / "other")]:
                with pytest.raises(RPCError, match="не совпадает"):
                    await app.attach(thread_id, project)
            assert not any(c["method"] == "thread/resume" for c in fake.calls)
        finally:
            await app.close()


async def test_notification_overflow_closes_instead_of_blocking(tmp_path):
    fake = FakeApp(tmp_path)
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        app.events = asyncio.Queue(maxsize=1)
        app.consumer.cancel()
        await fake.emit("unknown", {})
        await fake.emit("unknown", {})
        await asyncio.wait_for(app.closed.wait(), 2)
        await app.close()
