import asyncio
import base64
import json

import pytest
from conftest import FakeApp, until
from test_integration import registration
from test_project import project_controller
from websockets.asyncio.server import serve

from bax_codex_light import images
from bax_codex_light.appserver import RPCRejected
from bax_codex_light.bridge import Bridge
from bax_codex_light.history import render
from bax_codex_light.project import ProjectController
from bax_codex_light.registry import Registry

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5W2XcAAAAASUVORK5CYII="
PHOTO = {"name": "photo.png", "mime": "image/png", "data": PNG}
INPUT = {"type": "image", "url": "data:image/png;base64," + PNG}


async def test_image_with_text_and_image_only_use_exact_thread_and_active_turn(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.on_frame({"type": "run", "session": "a", "text": "Что на фото?", "attachments": [PHOTO]})
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": [PHOTO, PHOTO]})
        calls = [c for c in fake.calls if c["method"] in {"turn/start", "turn/steer"}]
        assert [c["method"] for c in calls] == ["turn/start", "turn/steer"]
        assert calls[0]["params"]["input"] == [{"type": "text", "text": "Что на фото?"}, INPUT]
        assert calls[1]["params"]["input"] == [INPUT, INPUT]
        assert all(c["params"]["threadId"] == "a" for c in calls)
        assert calls[1]["params"]["expectedTurnId"] == "turn-a"
        assert all(not item.images for item in owner.sessions["a"].outbox.values())
        assert any(f.get("text") == "Картинок: 2" for f in owner.relay.frames)
        assert not any(k in calls[0]["params"] for k in ("model", "approvalPolicy", "sandboxPolicy"))


async def test_stale_image_is_never_sent_to_new_selection(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.select("b")
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": [PHOTO]})
        assert not any(c["method"].startswith("turn/") for c in fake.calls)
        assert owner.relay.frames[-1]["type"] == "error"


async def test_preparing_image_does_not_reorder_concurrent_comments(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        texts = ["Сначала фото", "Затем уточнение", "Последний вариант"]
        await asyncio.gather(
            *(
                owner.sessions["a"].on_frame(
                    {
                        "type": "run",
                        "text": text,
                        "attachments": [PHOTO] if index == 0 else [],
                    }
                )
                for index, text in enumerate(texts)
            )
        )
        calls = [c for c in fake.calls if c["method"] in {"turn/start", "turn/steer"}]
        assert [c["params"]["input"][0]["text"] for c in calls] == texts
        assert calls[0]["params"]["input"][1] == INPUT


async def test_image_larger_than_old_relay_limit_reaches_native_app_server(tmp_path):
    fake = FakeApp(tmp_path)
    encoded = base64.b64encode(base64.b64decode(PNG) + b"\x00" * (3_200_000)).decode()
    sockets = []
    reg = None

    async def relay_handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        sockets.append(ws)
        async for _ in ws:
            pass

    async with (
        fake.running() as endpoint,
        serve(relay_handler, "127.0.0.1", 0, max_size=16 * 1024 * 1024) as server,
    ):
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, reg)
        bridge = Bridge(tmp_path, registry, "current", endpoint)
        try:
            await bridge.start()
            await until(lambda: bool(sockets))
            await sockets[0].send(
                json.dumps({"v": 1, "type": "run", "text": "", "attachments": [dict(PHOTO, data=encoded)]})
            )
            await asyncio.wait_for(fake.started.wait(), 5)
            call = next(c for c in fake.calls if c["method"] == "turn/start")
            assert call["params"]["input"] == [{"type": "image", "url": "data:image/png;base64," + encoded}]
        finally:
            await bridge.close()


async def test_uncertain_image_delivery_is_not_replayed_or_duplicated_on_disk(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        original = owner.app.start_turn

        async def timed_out(*args, **kwargs):
            await original(*args, **kwargs)
            raise TimeoutError

        owner.app.start_turn = timed_out
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": [PHOTO]})
        await owner.sessions["a"]._drain()
        owner.save()
        saved = json.loads(owner.state_path.read_text())
        pending = next(iter(saved["sessions"]["a"]["pending"].values()))
        assert pending["image_count"] == 1 and pending["error"]
        assert PNG not in owner.state_path.read_text()
        assert len([c for c in fake.calls if c["method"] == "turn/start"]) == 1
        restored = ProjectController(tmp_path, owner.registry, owner.state_path)
        restored.app = owner.app
        await restored.ensure_session("a")
        assert not restored.sessions["a"].queue
        assert next(iter(restored.sessions["a"].outbox.values())).preview == "Картинок: 1"


async def test_rejected_steer_retains_image_until_retry_is_accepted(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        await owner.select("a")
        original = owner.app.steer_turn
        attempts = []

        async def reject_first(*args, **kwargs):
            attempts.append(kwargs["images"])
            if len(attempts) == 1:
                raise RPCRejected("Ход изменился")
            return await original(*args, **kwargs)

        owner.app.steer_turn = reject_first
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": [PHOTO]})
        assert attempts == [[INPUT], [INPUT]]
        assert not owner.sessions["a"].queue


@pytest.mark.parametrize(
    "attachments",
    [
        [dict(PHOTO, data="not-base64!")],
        [dict(PHOTO, data=base64.b64encode(b"plain text").decode())],
        [dict(PHOTO, mime="text/plain")],
        [dict(PHOTO, mime=[])],
        [dict(PHOTO, data="A" * (images.MAX_IMAGE_BYTES * 2))],
        [PHOTO] * 9,
        "not a list",
    ],
)
async def test_invalid_images_are_rejected_before_echo_or_codex_submission(tmp_path, attachments):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        owner.relay.frames.clear()
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": attachments})
        assert not any(c["method"].startswith("turn/") for c in fake.calls)
        assert not owner.sessions["a"].outbox
        assert [f["type"] for f in owner.relay.frames] == ["error"]


def test_image_only_history_remains_visible_without_sending_base64_to_phone():
    assert render({"type": "userMessage", "content": [INPUT]}) == ("user", "Картинок: 1")
    assert render({"type": "userMessage", "content": [{"type": "text", "text": "Фото"}, INPUT]}) == (
        "user",
        "Фото",
    )
