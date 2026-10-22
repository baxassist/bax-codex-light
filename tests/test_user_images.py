import base64
from uuid import uuid4

import pytest

from bax_codex_light import images
from bax_codex_light.bridge import Bridge
from bax_codex_light.history import History
from bax_codex_light.registry import Registry
from bax_codex_light.tracing import command_context, response_fields

PNG = b"\x89PNG\r\n\x1a\n" + b"picture"


def item(content):
    return {"id": "photo-input", "type": "userMessage", "clientId": str(uuid4()), "content": content}


async def test_native_data_url_is_bounded_and_does_not_disclose_original_url():
    value = item([{"type": "image", "url": "data:image/png;base64," + base64.b64encode(PNG).decode()}])
    pictures = await images.from_item(value)
    assert base64.b64decode(pictures[0]["data"]) == PNG
    assert pictures[0]["url"].startswith("bax-input-image:")
    assert pictures[0]["mime"] == "image/png"
    assert await images.from_item({**value, "type": "agentMessage"}) == []
    assert await images.from_item(item(value["content"] * 10)) == pictures


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/photo.png",
        "file:///etc/private.png",
        "data:image/png;base64,bad",
        "data:text/plain;base64,YQ==",
    ],
)
async def test_remote_missing_and_invalid_images_are_not_fetched(url):
    pictures = await images.from_item(item([{"type": "image", "url": url}]))
    assert len(pictures) == 1 and "error" in pictures[0] and "data" not in pictures[0]
    assert url not in str(pictures)


async def test_explicit_native_local_image_has_separate_read_timing_without_path(tmp_path):
    path = tmp_path / "Фото (одно).png"
    path.write_bytes(PNG)
    trace = f"00-{uuid4().hex}-{uuid4().hex[:16]}-01"
    with command_context({"traceparent": trace}):
        pictures = await images.from_item(item([{"type": "localImage", "path": str(path)}]))
        fields = response_fields({})
    assert base64.b64decode(pictures[0]["data"]) == PNG
    assert fields["dependency_timings"][0]["system"] == "filesystem"
    assert fields["dependency_timings"][0]["operation"] == "read"
    assert fields["dependency_timings"][0]["outcome"] == "ok"
    assert str(path) not in str(fields) + str(pictures)


@pytest.mark.parametrize("name", [".env.png", "missing.png", "linked.png", "folder.png", "../escape.png"])
async def test_local_native_images_reject_secrets_symlink_directory_and_escape(tmp_path, name):
    (tmp_path / ".env.png").write_bytes(PNG)
    (tmp_path / "linked.png").symlink_to(tmp_path / ".env.png")
    (tmp_path / "folder.png").mkdir()
    pictures = await images.from_item(item([{"type": "localImage", "path": str(tmp_path / name)}]))
    assert len(pictures) == 1 and "error" in pictures[0] and "data" not in pictures[0]


async def test_local_limits_and_native_cache_are_explicit(tmp_path, monkeypatch):
    folder = tmp_path / ".codex" / "tmp"
    folder.mkdir(parents=True)
    path = folder / "photo.png"
    path.write_bytes(PNG)
    assert "data" in (await images.from_item(item([{"type": "localImage", "path": str(path)}])))[0]
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 8)
    assert "error" in (await images.from_item(item([{"type": "localImage", "path": str(path)}])))[0]
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 100)
    monkeypatch.setattr(images, "MAX_TOTAL_BYTES", 8)
    assert "error" in (await images.from_item(item([{"type": "localImage", "path": str(path)}])))[0]


async def test_live_and_history_user_images_share_native_item_and_request_id(tmp_path):
    value = item(
        [
            {"type": "text", "text": "Фото"},
            {"type": "image", "url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
        ]
    )

    class App:
        async def items(self, thread, cursor, limit):
            assert thread == "current"
            return {"data": [{"item": value, "turnId": "turn"}], "nextCursor": None}

    class Relay:
        sent = []

        async def send(self, frame_type, **fields):
            self.sent.append({"type": frame_type, **fields})
            return True

    bridge = Bridge(tmp_path, Registry(tmp_path / "registration.json"), "current")
    bridge.app = App()
    bridge.history = History(bridge.app, "current")
    bridge.relay = Relay()
    await bridge.on_event(
        {"method": "item/completed", "params": {"threadId": "foreign", "turnId": "turn", "item": value}}
    )
    assert not bridge.relay.sent
    await bridge.on_event(
        {"method": "item/completed", "params": {"threadId": "current", "turnId": "turn", "item": value}}
    )
    live = next(row for row in bridge.relay.sent if row["type"] == "message")
    page = (await bridge.history.page())[0]
    assert live["images"] == page["images"]
    assert live["rid"] == page["rid"] == value["clientId"]
    assert live["text"] == page["text"] == "Фото"
