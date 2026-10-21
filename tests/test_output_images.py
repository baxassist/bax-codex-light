import base64

import pytest

from bax_codex_light import output_images
from bax_codex_light.bridge import Bridge
from bax_codex_light.project import SessionBridge
from bax_codex_light.registry import Registry

PNG = b"\x89PNG\r\n\x1a\n" + b"picture"


def test_collect_only_explicit_results_with_dedup_and_code_exclusion(tmp_path):
    image = tmp_path / "example.png"
    image.write_bytes(PNG)
    url = f"sandbox:{image}"
    text = f"![Макет]({url})\n[Скачать]({url})\n`![Код]({tmp_path}/code.png)`"
    result = output_images.collect(tmp_path, "current", text)
    assert len(result) == 1
    assert result[0]["url"] == url
    assert base64.b64decode(result[0]["data"]) == PNG
    assert result[0]["mime"] == "image/png"
    assert result[0]["filename"] == "example.png"
    assert output_images.collect(tmp_path, "current", f"```md\n{text}\n```") == []


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize(
    "name", ["missing.png", ".env.png", "invalid.png", "linked.png", "folder.png", "../other.png"]
)
def test_unavailable_secret_symlink_non_file_and_traversal_are_not_read(tmp_path, name, quoted):
    (tmp_path / "invalid.png").write_text("private data")
    (tmp_path / ".env.png").write_bytes(PNG)
    (tmp_path / "folder.png").mkdir()
    (tmp_path / "linked.png").symlink_to(tmp_path / ".env.png")
    url = f"{tmp_path}/{name}"
    result = output_images.collect(
        tmp_path, "current", f"![Изображение]({'<' + url + '>' if quoted else url})"
    )
    assert len(result) == 1
    assert "error" in result[0] and "data" not in result[0]


@pytest.mark.parametrize("sandbox", [False, True])
def test_commonmark_angle_paths_preserve_spaces_parentheses_and_canonical_url(tmp_path, sandbox):
    path = tmp_path / "Скрин веба (новый).png"
    path.write_bytes(PNG)
    url = ("sandbox:" if sandbox else "") + str(path)
    text = f"![Скрин](<{url}>)\n[Скачать](<{url}>)\n`![Код](<{url}>)`"
    result = output_images.collect(tmp_path, "current", text)
    assert len(result) == 1
    assert result[0]["url"] == url
    assert result[0]["filename"] == path.name
    assert base64.b64decode(result[0]["data"]) == PNG
    assert output_images.collect(tmp_path, "current", f"```md\n{text}\n```") == []


def test_other_thread_generated_images_are_not_accessible(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    current = home / "generated_images" / "current" / "output.png"
    current.parent.mkdir(parents=True)
    current.write_bytes(PNG)
    project = tmp_path / "project"
    project.mkdir()
    assert "data" in output_images.collect(project, "current", f"![Макет]({current})")[0]
    assert "error" in output_images.collect(project, "other", f"![Макет]({current})")[0]


def test_size_limits_and_encoded_path(tmp_path, monkeypatch):
    path = tmp_path / "two words.png"
    path.write_bytes(PNG)
    assert (
        "data" in output_images.collect(tmp_path, "current", f"![Макет]({str(path).replace(' ', '%20')})")[0]
    )
    monkeypatch.setattr(output_images, "MAX_IMAGE_BYTES", 8)
    assert (
        "error" in output_images.collect(tmp_path, "current", f"![Макет]({str(path).replace(' ', '%20')})")[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("project_mode", [False, True])
async def test_live_and_history_share_delivery_without_user_file_reads(tmp_path, project_mode, quoted):
    class Relay:
        sent = []

        async def send(self, frame_type, **fields):
            self.sent.append({"type": frame_type, **fields})
            return True

    class Owner:
        project = tmp_path
        registry = Registry(tmp_path / "registration.json")
        endpoint = None
        app = None
        relay = Relay()

        async def send(self, frame_type, **fields):
            return await self.relay.send(frame_type, **fields)

    bridge = (
        SessionBridge(Owner(), "current") if project_mode else Bridge(tmp_path, Owner.registry, "current")
    )
    bridge.relay = Owner.relay
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    url = f"<{path}>" if quoted else str(path)
    await bridge.send("message", id=1, kind="assistant", text=f"![Макет]({url})")
    assert bridge.relay.sent[-1]["session"] == "current"
    assert "data" in bridge.relay.sent[-1]["images"][0]
    bridge.history = type("History", (), {"recorded": {}, "page": None})()

    async def page(before, limit):
        return [{"id": 1, "kind": "assistant", "text": f"![Макет]({url})"}]

    bridge.history.page = page
    if project_mode:
        bridge.owner.save = lambda: None
    await bridge.send_history()
    assert "data" in bridge.relay.sent[-1]["images"][0]
    await bridge.send("message", id=2, kind="tool", text=f"![Макет]({path})")
    assert "images" not in bridge.relay.sent[-1]
