import pytest
from conftest import entry

from bax_codex_light.history import History, message_id


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def items(self, thread_id, cursor, limit):
        self.calls.append((thread_id, cursor))
        return self.pages[cursor]


@pytest.mark.parametrize("separate_pages", [False, True])
async def test_reused_native_item_ids_in_different_turns_keep_all_messages(separate_pages):
    def turn(name):
        return [
            {**entry("item-2", text=f"Ответ {name}"), "turnId": name},
            {**entry("item-1", "userMessage", f"Задача {name}"), "turnId": name},
        ]

    pages = Pages(
        {None: {"data": turn("new"), "nextCursor": "old"}, "old": {"data": turn("old"), "nextCursor": None}}
    )
    history = History(pages, "current")
    if separate_pages:
        newest = await history.page(limit=2)
        older = await history.page(newest[0]["id"], 2)
        assert max(row["id"] for row in older) < min(row["id"] for row in newest)
        rows = older + newest
    else:
        rows = await history.page()
    assert [row["text"] for row in rows] == ["Задача old", "Ответ old", "Задача new", "Ответ new"]
    assert [row["id"] for row in rows] == sorted({row["id"] for row in rows})
    assert await history.page(rows[0]["id"]) == []


async def test_pagination_is_stable_and_steps_folded():
    pages = Pages(
        {
            None: {
                "data": [
                    entry("a"),
                    entry("tool2", "commandExecution"),
                    entry("tool1", "commandExecution"),
                    entry("u", "userMessage"),
                ],
                "nextCursor": "older",
            },
            "older": {"data": [entry("old")], "nextCursor": None},
        }
    )
    history = History(pages, "current")
    first = await history.page(limit=2)
    assert [row["kind"] for row in first] == ["user", "steps", "assistant"]
    assert first[1]["count"] == 2
    assert [r["id"] for r in first] == sorted(r["id"] for r in first)
    assert await history.page(limit=2) == first
    old = await history.page(first[0]["id"], 2)
    assert old[0]["id"] < first[0]["id"]
    assert await history.page(old[0]["id"]) == []
    with pytest.raises(ValueError):
        await history.page(123)


async def test_missing_new_items_after_reconnect_keep_chronological_ids():
    pages = Pages({None: {"data": [entry("a"), entry("u", "userMessage")], "nextCursor": None}})
    history = History(pages, "current")
    initial = await history.page()
    pages.pages[None]["data"] = [entry("new"), entry("a"), entry("insert"), entry("u", "userMessage")]
    updated = await history.page()
    assert [r["id"] for r in updated] == sorted(r["id"] for r in updated)
    assert history.ids[("turn", "a")] == initial[-1]["id"]
    assert history.ids[("turn", "u")] == initial[0]["id"]
    assert history.live_id("new", "turn") == updated[-1]["id"]


async def test_async_questions_without_text_survive_history_reload():
    item = {
        "id": "question",
        "type": "agentMessage",
        "text": "",
        "questions": [
            {"title": "Что сделать?", "options": ["Проверить", "Исправить"]},
            {"title": "Как назвать проект?"},
        ],
    }
    pages = Pages({None: {"data": [{"turnId": "turn", "item": item}], "nextCursor": None}})
    history = History(pages, "current")
    rows = await history.page()
    assert rows[0]["text"] == "Что сделать?\n• Проверить\n• Исправить\n\nКак назвать проект?"
    assert rows[0]["kind"] == "assistant"
    assert await history.page() == rows


def test_async_question_title_already_in_text_is_not_duplicated():
    from bax_codex_light.history import render

    assert render(
        {
            "type": "agentMessage",
            "text": "Как назвать проект?",
            "questions": [
                {"title": "Как назвать проект?"},
            ],
        }
    ) == ("assistant", "Как назвать проект?")


async def test_full_reload_recovers_exhausted_ids_and_keeps_pagination_and_live_updates():
    pages = Pages(
        {
            None: {
                "data": [
                    entry("right", text="right"),
                    entry("insert", text="insert"),
                    entry("left", "userMessage", "left"),
                ],
                "nextCursor": "older",
            },
            "older": {"data": [entry("old", "userMessage", "old")], "nextCursor": None},
        }
    )
    history = History(pages, "current")
    history.ids = {("turn", "left"): 100, ("turn", "right"): 101}
    history.low, history.high = 100, 101
    history.initialized = True
    history.cursors = {100: "obsolete"}
    queued = history.preview_id("pending")
    rows = await history.page(limit=2)
    assert [r["text"] for r in rows] == ["left", "insert", "right"]
    assert [r["id"] for r in rows] == sorted({r["id"] for r in rows})
    assert rows[0]["id"] > queued
    assert history.preview_id("pending") > rows[-1]["id"]
    assert await history.page(limit=2) == rows
    older = await history.page(rows[0]["id"], 2)
    assert older[0]["id"] < rows[0]["id"]
    assert history.live_id("next", "turn") > rows[-1]["id"]
    assert "left" in history.recorded
    assert 100 not in history.cursors


async def test_full_reload_recovers_reordered_live_anchors():
    pages = Pages(
        {
            None: {
                "data": [
                    entry("right", text="right"),
                    entry("missing", text="missing"),
                    entry("left", text="left"),
                ],
                "nextCursor": None,
            }
        }
    )
    history = History(pages, "current")
    history.live_id("right", "turn")
    history.live_id("left", "turn")
    rows = await history.page()
    assert [r["text"] for r in rows] == ["left", "missing", "right"]
    assert [r["id"] for r in rows] == sorted({r["id"] for r in rows})
    assert await history.page() == rows


async def test_reload_after_older_order_error_discards_anchors_outside_latest_page():
    pages = Pages(
        {
            None: {
                "data": [
                    entry("latest-right", text="latest-right"),
                    entry("latest-left", text="latest-left"),
                ],
                "nextCursor": "older",
            },
            "older": {
                "data": [entry("old-right", text="old-right"), entry("old-left", text="old-left")],
                "nextCursor": None,
            },
        }
    )
    history = History(pages, "current")
    # В свежей странице всё упорядочено, но старые live-якоря расходятся с журналом.
    history.ids = {
        ("turn", "old-left"): 200,
        ("turn", "old-right"): 100,
        ("turn", "latest-left"): 300,
        ("turn", "latest-right"): 400,
    }
    history.low, history.high = 100, 400
    history.initialized = True
    latest = await history.page(limit=2)
    stale_cursor = latest[0]["id"]
    with pytest.raises(ValueError, match="История обновилась"):
        await history.page(stale_cursor, 2)

    # Повторное открытие должно восстанавливать и якоря за пределами свежей пачки.
    refreshed = await history.page(limit=2)
    older = await history.page(refreshed[0]["id"], 2)
    assert [row["text"] for row in older] == ["old-left", "old-right"]
    assert [row["id"] for row in older + refreshed] == sorted({row["id"] for row in older + refreshed})
    with pytest.raises(ValueError, match="Неизвестный курсор"):
        await history.page(stale_cursor, 2)
    assert await history.page(limit=2) == refreshed


async def test_many_late_insertions_do_not_exhaust_the_bridge():
    pages = Pages(
        {None: {"data": [entry("right", text="right"), entry("left", text="left")], "nextCursor": None}}
    )
    history = History(pages, "current")
    await history.page()
    chronological = ["left", "right"]
    for n in range(100):
        chronological.insert(1, f"late-{n}")
        pages.pages[None]["data"] = [entry(item_id, text=item_id) for item_id in reversed(chronological)]
        rows = await history.page(limit=1000)
        assert [r["text"] for r in rows] == chronological
        assert [r["id"] for r in rows] == sorted({r["id"] for r in rows})


async def test_overlapping_pages_do_not_duplicate_messages():
    pages = Pages(
        {
            None: {
                "data": [entry("latest", text="latest"), entry("overlap", text="overlap")],
                "nextCursor": "older",
            },
            "older": {
                "data": [entry("overlap", text="overlap"), entry("old", "userMessage", "old")],
                "nextCursor": None,
            },
        }
    )
    rows = await History(pages, "current").page(limit=50)
    assert [r["text"] for r in rows] == ["old", "overlap", "latest"]


async def test_user_date_is_forwarded_without_assistant_parameters():
    pages = Pages({None: {"data": [entry("u", "userMessage")], "nextCursor": None}})

    async def metadata(thread, entries):
        return {
            (row["turnId"], row["item"]["id"]): {"created_at": 1791115200, "model": "wrong-for-user"}
            for row in entries
        }

    pages.response_metadata = metadata
    rows = await History(pages, "current").page()
    assert rows[0]["created_at"] == 1791115200
    assert "model" not in rows[0]


def test_message_id_uses_unambiguous_source_key_only():
    assert message_id("t", "u", "i") == "4a2edaab6f793952a2697758eaa0dc5909d30d4bd2b86190d847f0a1e1d1a83e"
    assert message_id("a:b", "c", "d") != message_id("a", "b:c", "d")
    assert message_id("t", "u", "i") != message_id("other", "u", "i")
    assert message_id("t", "u", "i") != message_id("t", "other", "i")


async def test_message_id_survives_rebase_fresh_history_and_content_change():
    pages = Pages({None: {"data": [entry("right"), entry("left")], "nextCursor": None}})
    history = History(pages, "current")
    original = await history.page()
    history.ids = {("turn", "right"): 1, ("turn", "left"): 2}
    pages.pages[None]["data"][0]["item"]["text"] = "Исправленный текст"
    changed = await history.page()
    fresh = await History(pages, "current").page()
    assert (
        [r["message_id"] for r in original]
        == [r["message_id"] for r in changed]
        == [r["message_id"] for r in fresh]
    )
    assert [r["id"] for r in original] != [r["id"] for r in changed]
    assert changed[-1]["text"] == "Исправленный текст"
