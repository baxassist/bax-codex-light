"""Регрессии зависших карточек и пагинации новой живой сессии."""

import asyncio

import pytest
from conftest import entry
from test_bridge import bridge
from test_history import Pages

from bax_codex_light.history import History, message_id


async def test_queue_notice_resolves_by_exact_receipt_in_source_snapshot(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    for rid in ("queued", "other"):
        await b.send_failure("message_queued", "Ожидает", operation="run", rid=rid)
    await b.send("error.resolved", rid="queued", delivery="accepted")
    assert [f["rid"] for f in b.make_view(force=True)["failures"]] == ["other"]
    await b.send("message", kind="user", id=1, rid="other", text="Предварительное эхо")
    assert [f["rid"] for f in b.make_view(force=True)["failures"]] == ["other"]
    await b.send("message", kind="user", id=1, rid="other", text="Принято", message_id="native-user")
    assert b.make_view(force=True)["failures"] == []


async def test_next_native_turn_removes_stale_quota_but_keeps_unconfirmed_delivery(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    await b.send_turn_failure(
        {"id": "failed", "error": {"message": "Лимит", "codexErrorInfo": "usageLimitExceeded"}}
    )
    await b.send_failure(
        "delivery_uncertain", "Нет квитанции", operation="run", rid="pending", delivery="unknown"
    )
    await b.on_event({"method": "turn/started", "params": {"threadId": "current", "turn": {"id": "next"}}})
    assert [f["rid"] for f in b.make_view(force=True)["failures"]] == ["pending"]
    assert not b.failures and b.last_failure is None
    assert not b.app.calls and not b.app.steers


@pytest.mark.parametrize("status", ["inProgress", "completed", "interrupted"])
async def test_reconnect_and_restart_do_not_restore_failures_before_newer_turn(tmp_path, status):
    for restarted in (False, True):
        b = bridge(tmp_path)
        b.view.enabled = True
        failed = {"id": "failed", "status": "failed", "error": {"message": "Старый лимит"}}
        if not restarted:
            await b.send_turn_failure(failed)
        b.app.history_items = [{**entry("old"), "turnId": "failed"}, {**entry("new"), "turnId": "next"}]

        async def turns(thread_id, failed=failed):
            return [failed, {"id": "next", "status": status}]

        b.app.recent_turns = turns
        await b.on_frame({"type": "subscribe", "view_protocol": 1})
        done = next(f for f in reversed(b.relay.sent) if f["type"] == "history.done")
        assert done["view"]["failures"] == []
        assert not b.failures and b.last_failure is None


async def test_newest_failure_stays_visible_and_stale_read_cannot_restore_previous_one(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    old = {"id": "old", "status": "failed", "error": {"message": "Старый сбой"}}
    await b.send_turn_failure(old)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale(thread_id):
        entered.set()
        await release.wait()
        return [old]

    b.app.recent_turns = stale
    read = asyncio.create_task(b.send_failures())
    await entered.wait()
    await b.on_event({"method": "turn/started", "params": {"threadId": "current", "turn": {"id": "new"}}})
    await b.send_turn_failure({"id": "new", "error": {"message": "Новый сбой"}})
    release.set()
    await read
    assert [f["message"] for f in b.make_view(force=True)["failures"]] == ["Новый сбой"]


async def test_live_first_message_after_empty_subscribe_has_valid_older_page(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    await b.on_frame({"type": "subscribe", "view_protocol": 1})
    user = entry("first", "userMessage", "Первый вопрос")
    await b.on_event(
        {"method": "item/started", "params": {"threadId": "current", "turnId": "turn", "item": user["item"]}}
    )
    first = b.history.ids[("turn", "first")]
    b.app.history_items = [entry("answer"), user]
    b.relay.sent.clear()
    await b.on_frame({"type": "history", "before": first})
    assert not any(f["type"] == "error" for f in b.relay.sent)
    assert any(f["type"] == "history.done" for f in b.relay.sent)
    assert not any(f["type"] == "message" for f in b.relay.sent)


async def test_live_anchor_and_folded_steps_can_page_without_registered_boundary():
    pages = Pages(
        {
            None: {
                "data": [
                    entry("new"),
                    entry("tool2", "commandExecution"),
                    entry("tool1", "commandExecution"),
                ],
                "nextCursor": "old",
            },
            "old": {"data": [entry("old", "userMessage", "Старый вопрос")], "nextCursor": None},
        }
    )
    history = History(pages, "current")
    history.live_id("tool1", "turn")
    boundary = history.live_id("tool2", "turn")
    history.live_id("new", "turn")
    rows = await history.page(boundary, limit=1)
    assert [row["text"] for row in rows] == ["Старый вопрос", "Команда"]
    assert all(row["id"] < boundary for row in rows)
    assert await history.page(rows[0]["id"], limit=1) == []
    with pytest.raises(ValueError, match="Неизвестный курсор"):
        await history.page(123)


@pytest.mark.parametrize("status", ["completed", "interrupted"])
async def test_confirmed_terminal_outcome_clears_exact_final_failure_live(tmp_path, status):
    b = bridge(tmp_path)
    b.view.enabled = True
    for turn in ("failed", "unrelated"):
        await b.send_turn_failure({"id": turn, "error": {"message": "Ошибка " + turn}})
    await b.on_event(
        {
            "method": "turn/completed",
            "params": {"threadId": "current", "turn": {"id": "failed", "status": status}},
        }
    )
    assert [f["turn_id"] for f in b.make_view(force=True)["failures"]] == ["unrelated"]
    assert all(f["turn_id"] == "unrelated" for f in b.failures.values())


async def test_newest_error_survives_history_and_foreign_native_events(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    latest = {"id": "failed", "status": "failed", "error": {"message": "Текущая ошибка"}}

    async def turns(thread_id):
        return [latest]

    b.app.recent_turns = turns
    await b.on_frame({"type": "subscribe", "view_protocol": 1})
    await b.on_event({"method": "turn/started", "params": {"threadId": "foreign", "turn": {"id": "other"}}})
    assert [f["message"] for f in b.make_view(force=True)["failures"]] == ["Текущая ошибка"]
    await b.on_frame({"type": "subscribe", "view_protocol": 1})
    assert len(b.make_view(force=True)["failures"]) == 1


async def test_unknown_live_anchor_is_not_silently_reported_as_end_of_history():
    pages = Pages({None: {"data": [], "nextCursor": None}})
    history = History(pages, "current")
    boundary = history.live_id("not-persisted", "turn")
    with pytest.raises(ValueError, match="ещё не доступно"):
        await history.page(boundary)


@pytest.mark.parametrize("persisted", [False, True])
async def test_pending_first_message_preview_is_a_valid_history_boundary(persisted):
    user = entry("new", "userMessage", "Новый вопрос")
    user["item"]["clientId"] = "client"
    pages = Pages({None: {"data": [], "nextCursor": None}})
    history = History(pages, "current")
    await history.page()
    boundary = history.preview_id("client")
    pages.pages[None]["data"] = [user] if persisted else []
    assert await history.page(boundary) == []
    assert await history.page(boundary) == []


async def test_live_boundary_search_crosses_native_pages_and_preserves_history_identity():
    pages = Pages(
        {
            None: {"data": [entry("newer")], "nextCursor": "middle"},
            "middle": {"data": [entry("anchor"), entry("old")], "nextCursor": "tail"},
            "tail": {"data": [entry("oldest", "userMessage")], "nextCursor": None},
        }
    )
    history = History(pages, "current")
    boundary = history.live_id("anchor", "turn")
    history.live_id("newer", "turn")
    rows = await history.page(boundary, limit=2)
    assert len(rows) == 2 and all(row["id"] < boundary for row in rows)
    assert [row["message_id"] for row in rows] == [
        message_id("current", "turn", item) for item in ("oldest", "old")
    ]
    assert pages.calls == [("current", None), ("current", "middle"), ("current", "tail")]


async def test_new_turn_during_history_error_send_does_not_crash_or_restore_card(tmp_path):
    b = bridge(tmp_path)
    b.view.enabled = True
    failed = {"id": "failed", "status": "failed", "error": {"message": "Старый лимит"}}

    async def turns(thread_id):
        return [failed]

    original = b.relay.send
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(kind, **fields):
        if kind == "error":
            entered.set()
            await release.wait()
        return await original(kind, **fields)

    b.app.recent_turns, b.relay.send = turns, slow_send
    read = asyncio.create_task(b.send_failures())
    await entered.wait()
    await b.on_event({"method": "turn/started", "params": {"threadId": "current", "turn": {"id": "next"}}})
    release.set()
    await read
    assert b.make_view(force=True)["failures"] == []
    assert b.last_failure is None and not b.failures


async def test_original_project_transport_resolves_only_background_sessions_old_error(tmp_path):
    from test_project import project_controller

    async with project_controller(tmp_path) as (_, owner):
        await owner.select("a")
        await owner.on_frame({"type": "subscribe", "view_protocol": 1})
        await owner.select("b")
        for session in ("a", "b"):
            await owner.on_event(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": session,
                        "turn": {
                            "id": "failed-" + session,
                            "status": "failed",
                            "error": {"message": "Ошибка " + session},
                        },
                    },
                }
            )
        await owner.on_event(
            {"method": "turn/started", "params": {"threadId": "a", "turn": {"id": "next-a"}}}
        )
        assert owner.selected == "b"
        assert owner.sessions["a"].make_view(force=True)["failures"] == []
        assert [f["message"] for f in owner.sessions["b"].make_view(force=True)["failures"]] == ["Ошибка b"]
        snapshots = [f for f in owner.relay.frames if f["type"] == "session.view" and f["session"] == "a"]
        assert snapshots[-1]["view"]["failures"] == []
