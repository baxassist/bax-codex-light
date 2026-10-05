import asyncio

import pytest
from conftest import FakeApp, entry, until

from bax_codex_light.appserver import AppServer, RPCError, RPCRejected
from bax_codex_light.history import History


async def test_legacy_history_falls_back_to_native_turn_pages_in_chronological_order(tmp_path):
    fake = FakeApp(tmp_path)
    fake.items_error = "thread/items/list is not supported yet"
    fake.turn_pages = {
        None: {
            "data": [
                {
                    "id": "newer",
                    "status": "completed",
                    "items": [
                        entry("user-new", "userMessage", "Новая задача")["item"],
                        entry("answer-new", text="Новый ответ")["item"],
                    ],
                }
            ],
            "nextCursor": "older",
        },
        "older": {
            "data": [
                {
                    "id": "older",
                    "status": "completed",
                    "items": [
                        entry("user-old", "userMessage", "Прежняя задача")["item"],
                        entry("answer-old", text="Прежний ответ")["item"],
                    ],
                }
            ],
            "nextCursor": None,
        },
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        try:
            history = History(app, "current")
            rows = await history.page()
            assert [row["text"] for row in rows] == [
                "Прежняя задача",
                "Прежний ответ",
                "Новая задача",
                "Новый ответ",
            ]
            assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)
            assert history.recorded == {"user-old", "user-new"}
            assert not await history.page(before=rows[0]["id"])
            assert len([c for c in fake.calls if c["method"] == "thread/items/list"]) == 1
            turns = [c["params"] for c in fake.calls if c["method"] == "thread/turns/list"]
            assert [p.get("cursor") for p in turns] == [None, "older"]
            assert all(p["itemsView"] == "full" and p["limit"] == 1 for p in turns)
        finally:
            await app.close()


async def test_history_rejection_does_not_hide_failure_or_change_backend(tmp_path):
    fake = FakeApp(tmp_path)
    fake.items_error = "история недоступна"
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        try:
            with pytest.raises(RPCRejected, match="история недоступна"):
                await app.items("current", None, 50)
            assert not app.turn_history
            assert not any(c["method"] == "thread/turns/list" for c in fake.calls)
        finally:
            await app.close()


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
            with pytest.raises(RPCRejected, match="test"):
                await app.request("test/error", {})
            assert not app.closed.is_set()
        finally:
            await app.close()


async def test_never_resumes_closed_thread(tmp_path):
    fake = FakeApp(tmp_path)
    fake.state = "notLoaded"
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            with pytest.raises(RPCError, match="не открыта"):
                await app.attach("current", tmp_path)
            assert not any(c["method"] == "thread/resume" for c in fake.calls)
        finally:
            await app.close()


async def test_phone_selection_rejects_thread_that_stays_not_loaded(tmp_path):
    fake = FakeApp(tmp_path)
    fake.state = "notLoaded"
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            with pytest.raises(RPCError, match="не загрузил"):
                await app.resume_thread("current", tmp_path)
            assert "current" not in app.configurations
            assert not any(call["method"] == "turn/start" for call in fake.calls)
        finally:
            await app.close()


async def test_failed_turn_does_not_close_thread_and_its_reason_is_available(tmp_path):
    fake = FakeApp(tmp_path)
    fake.state = "systemError"
    fake.turn_pages = {
        None: {
            "data": [
                {
                    "id": "failed-turn",
                    "status": "failed",
                    "error": {"message": "model at capacity"},
                    "items": [],
                }
            ],
            "nextCursor": None,
        }
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            assert (await app.attach("current", tmp_path))["status"]["type"] == "systemError"
            assert await app.last_turn_error("current") == "model at capacity"
            await app.start_turn("current", "Повтори", "retry")
            assert any(call["method"] == "turn/start" for call in fake.calls)
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


async def test_active_turn_lookup_reads_only_latest_turn_metadata(tmp_path):
    fake = FakeApp(tmp_path)
    fake.state = "active"
    fake.active_turn_id = "active-turn"
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        try:
            await app.open()
            assert await app.active_turn("current") == "active-turn"
            call = next(c for c in fake.calls if c["method"] == "thread/turns/list")
            assert call["params"] == {
                "threadId": "current",
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            }
            fake.state = "idle"
            assert await app.active_turn("current") == ""
            assert not any(
                c["method"] in {"thread/resume", "thread/start", "turn/start", "turn/steer"}
                for c in fake.calls
            )
        finally:
            await app.close()
