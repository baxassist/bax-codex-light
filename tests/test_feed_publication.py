import asyncio

import pytest
from test_project import project_controller

from bax_codex_light.appserver import RPCError


async def test_explicit_publication_waits_for_exact_receipt_and_keeps_selection(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        publication = asyncio.create_task(
            owner.publish_to_feed("b", "Плагин 0.5.9 опубликован", "plugin-0.5.9")
        )
        for _ in range(100):
            if owner.relay.frames[-1]["type"] == "feed.publish":
                break
            await asyncio.sleep(0.01)
        frame = owner.relay.frames[-1]
        assert frame["type"] == "feed.publish" and frame["publication_key"] == "plugin-0.5.9"
        await owner.on_frame({"type": "feed.published", "rid": "foreign", "published": True})
        assert not publication.done()
        await owner.on_frame(
            {"type": "feed.published", "rid": frame["rid"], "published": True, "event_id": "event"}
        )
        assert await publication == {"published": True, "event_id": "event"}
        assert owner.selected == "a" and owner.publications == {}
        assert not any(call["method"] in {"turn/start", "thread/settings/update"} for call in fake.calls)


async def test_disabled_publication_reports_no_event(tmp_path):
    async with project_controller(tmp_path) as (_, owner):

        async def send(kind, **fields):
            assert kind == "feed.publish"
            await owner.on_frame(
                {
                    "type": "feed.published",
                    "rid": fields["rid"],
                    "published": False,
                    "reason": "stream_disabled",
                }
            )
            return True

        owner.send = send
        assert await owner.publish_to_feed("a", "Сборка готова", "ios-205") == {
            "published": False,
            "reason": "stream_disabled",
        }


async def test_foreign_thread_and_invalid_text_cannot_publish(tmp_path):
    async with project_controller(tmp_path) as (_, owner):
        with pytest.raises(RPCError):
            await owner.publish_to_feed("foreign", "Сборка готова", "ios-205")
        with pytest.raises(ValueError):
            await owner.publish_to_feed("a", " ", "ios-205")
        assert not any(frame["type"] == "feed.publish" for frame in owner.relay.frames)


async def test_offline_is_not_reported_as_publication(tmp_path):
    async with project_controller(tmp_path) as (_, owner):
        owner.relay.connected = False
        with pytest.raises(ValueError, match="не подключён"):
            await owner.publish_to_feed("a", "Сборка готова", "ios-205")


async def test_no_receipt_is_unknown_and_same_key_can_be_retried(tmp_path, monkeypatch):
    async with project_controller(tmp_path) as (_, owner):
        original = asyncio.wait_for

        async def quick_wait(future, timeout):  # noqa: ASYNC109 — подмена стандартного wait_for
            return await original(future, 0.01 if timeout == 20 else timeout)

        monkeypatch.setattr(asyncio, "wait_for", quick_wait)
        result = await owner.publish_to_feed("a", "Сборка готова", "ios-205")
        assert result == {"published": None, "reason": "confirmation_timeout", "publication_key": "ios-205"}
        assert owner.publications == {}


async def test_user_prompt_is_only_in_new_thread_start_and_can_be_changed_or_disabled(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.calls.clear()
        prompt = "Публикуй только Apple-сборки с версией и номером"
        await owner.on_frame(
            {"type": "session.select", "session": "new", "expected_session": "a", "feed_prompt": prompt}
        )
        starts = [c for c in fake.calls if c["method"] == "thread/start"]
        assert len(starts) == 1
        instructions = starts[0]["params"]["developerInstructions"]
        assert instructions.count(prompt) == 1 and "bax_publish" in instructions
        assert not any(c["method"] == "turn/start" for c in fake.calls)
        assert owner.template.get("developerInstructions") is None
        first = owner.selected
        fake.calls.clear()
        await owner.on_frame(
            {
                "type": "session.select",
                "session": "a",
                "expected_session": first,
                "feed_prompt": "не вставлять",
            }
        )
        assert not any(c["method"] == "thread/start" for c in fake.calls)
        await owner.on_frame(
            {"type": "session.select", "session": "new", "expected_session": "a", "feed_prompt": ""}
        )
        start = next(c for c in fake.calls if c["method"] == "thread/start")
        assert "developerInstructions" not in start["params"]
