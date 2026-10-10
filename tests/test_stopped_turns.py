import pytest
from conftest import FakeApp, entry
from test_bridge import bridge
from test_project import project_controller

from bax_codex_light.appserver import AppServer, RPCError
from bax_codex_light.history import History


@pytest.mark.parametrize("native", [False, True])
async def test_stopped_history_restores_partial_answer_and_one_stable_status(tmp_path, native):
    fake = FakeApp(tmp_path)
    user = entry("u", "userMessage", "Задача")["item"]
    answer = entry("a", text="Частичный ответ")["item"]
    fake.items = [{"turnId": "turn", "item": answer}, {"turnId": "turn", "item": user}]
    fake.items_error = None if native else "thread/items/list is not supported yet"
    fake.turn_pages = {
        None: {
            "data": [{"id": "turn", "status": "interrupted", "completedAt": 123, "items": [user, answer]}],
            "nextCursor": "older",
        },
        "older": {
            "data": [
                {"id": "older", "status": "completed", "items": [entry("old", text="Прежний ответ")["item"]]}
            ],
            "nextCursor": None,
        },
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        try:
            history = History(app, "current")
            rows = await history.page(limit=2)
            assert [row["text"] for row in rows] == ["Задача", "Частичный ответ", "Работа остановлена"]
            status = rows[-1]
            assert status["kind"] == "event" and status["event"] == "stopped"
            assert status["turn_id"] == "turn" and status["created_at"] == 123
            assert await history.page(limit=2) == rows
            assert history.live_id("bax:stopped:turn") == status["id"]
            restored = await History(app, "current").page(limit=2)
            assert restored[-1] == status
            if not native:
                older = await history.page(before=rows[0]["id"], limit=2)
                assert [row["text"] for row in older] == ["Прежний ответ"]
        finally:
            await app.close()


async def test_native_page_boundary_does_not_put_stop_in_middle_of_answer(tmp_path):
    fake = FakeApp(tmp_path)
    fake.items = [entry("u", "userMessage", "Задача")]
    fake.turn_pages = {
        None: {
            "data": [
                {"id": "turn", "status": "interrupted", "items": [fake.items[0]["item"], entry("a")["item"]]}
            ],
            "nextCursor": None,
        }
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        try:
            result = await app.items("current", None, 1)
            assert len(result["data"]) == 1
        finally:
            await app.close()


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_live_outcome_is_confirmed_by_codex_and_repeated_event_uses_same_id(tmp_path, status):
    b = bridge(tmp_path)
    b.history.live_id("partial")
    event = {
        "method": "turn/completed",
        "params": {"threadId": "current", "turn": {"id": "turn", "status": status, "completedAt": 123}},
    }
    await b.on_event(event)
    await b.on_event(event)
    markers = [f for f in b.relay.sent if f["type"] == "message" and f.get("event") == "stopped"]
    if status == "interrupted":
        assert len(markers) == 2 and markers[0] == markers[1]
        assert markers[0]["created_at"] == 123
        assert markers[0]["id"] > b.history.ids["partial"]
    else:
        assert not markers
    assert b.relay.sent[-1]["type"] == "done" and b.relay.sent[-1]["status"] == status


async def test_cancel_ack_never_claims_a_completed_stop_and_targets_exact_session(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        await owner.on_frame({"type": "cancel", "session": "a", "rid": "stop-a"})
        result = next(f for f in owner.relay.frames if f.get("rid") == "stop-a")
        assert result["type"] == "cancel.result" and result["outcome"] == "requested"
        assert result["session"] == "a" and result["turn_id"] == "turn-a"
        assert not any(f.get("event") == "stopped" for f in owner.relay.frames)
        await owner.on_frame({"type": "cancel", "session": "a", "rid": "idle"})
        assert owner.relay.frames[-1]["outcome"] == "idle"
        await owner.on_frame({"type": "cancel", "session": "foreign", "rid": "bad"})
        assert owner.relay.frames[-1]["type"] == "error"


async def test_failed_interrupt_keeps_error_and_never_creates_stopped_marker(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}

        async def fail(thread_id, turn_id):
            raise RPCError("Остановка не выполнена")

        owner.app.interrupt_turn = fail
        await owner.on_frame({"type": "cancel", "session": "a", "rid": "failed-stop"})
        result = owner.relay.frames[-1]
        assert result["type"] == "error" and result["rid"] == "failed-stop"
        assert not any(f.get("event") == "stopped" for f in owner.relay.frames)


async def test_interrupted_empty_turn_remains_visible_after_reconnect(tmp_path):
    fake = FakeApp(tmp_path)
    fake.items_error = "thread/items/list is not supported yet"
    fake.turn_pages = {
        None: {"data": [{"id": "empty", "status": "interrupted", "items": []}], "nextCursor": None}
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        await app.open()
        try:
            rows = await History(app, "current").page()
            assert len(rows) == 1 and rows[0]["event"] == "stopped"
        finally:
            await app.close()
