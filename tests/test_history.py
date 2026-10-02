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
