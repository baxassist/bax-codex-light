import pytest
from conftest import entry

from bax_codex_light.history import History


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def items(self, thread_id, cursor, limit):
        self.calls.append((thread_id, cursor))
        return self.pages[cursor]


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
    assert history.ids["a"] == initial[-1]["id"]
    assert history.ids["u"] == initial[0]["id"]
    assert history.live_id("new") == updated[-1]["id"]


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
    pages = Pages({None: {"data": [{"item": item}], "nextCursor": None}})
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
    history.ids = {"left": 100, "right": 101}
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
    assert history.live_id("next") > rows[-1]["id"]
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
    history.live_id("right")
    history.live_id("left")
    rows = await history.page()
    assert [r["text"] for r in rows] == ["left", "missing", "right"]
    assert [r["id"] for r in rows] == sorted({r["id"] for r in rows})
    assert await history.page() == rows


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
