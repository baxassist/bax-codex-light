import base64
import json
from pathlib import Path

import pytest
from test_images import PHOTO
from test_project import project_controller

from bax_codex_light import attachments


def document(name="Отчёт.pdf", data=b"%PDF-1.7\n", mime="application/pdf"):
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode()}


def test_mixed_files_preserve_binary_names_and_native_images(tmp_path):
    images, text = attachments.prepare(tmp_path, [document(), PHOTO, document(data=b"second")])
    assert images[0]["type"] == "image"
    lines = text.splitlines()[-2:]
    paths = [Path(json.loads(line.split(": ", 1)[1])) for line in lines]
    assert paths[0] != paths[1] and all(path.name == "Отчёт.pdf" for path in paths)
    assert [path.read_bytes() for path in paths] == [b"%PDF-1.7\n", b"second"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert (tmp_path / ".bax-attachments" / ".gitignore").read_text() == "*\n"


@pytest.mark.parametrize(
    "bad",
    [
        document("../secret"),
        document("a/b"),
        document("a\\b"),
        document("\n"),
        {"name": "x", "mime": "text/plain", "data": "%%%"},
        document(data=b"x"),
    ],
)
def test_invalid_batch_never_writes_partial_documents(tmp_path, monkeypatch, bad):
    if bad == document(data=b"x"):
        monkeypatch.setattr(attachments.images, "MAX_IMAGE_BYTES", 0)
    with pytest.raises(ValueError):
        attachments.prepare(tmp_path, [document(data=b""), bad])
    assert not (tmp_path / ".bax-attachments").exists()


def test_attachment_folder_cannot_escape_project_through_symlink(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / ".bax-attachments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        attachments.prepare(tmp_path, [document()])
    assert not list(outside.iterdir())


def test_existing_ignore_cannot_expose_new_documents(tmp_path):
    root = tmp_path / ".bax-attachments"
    root.mkdir()
    (root / ".gitignore").write_text("*.png\n!*.pdf")
    attachments.prepare(tmp_path, [document()])
    assert (root / ".gitignore").read_text() == "*.png\n!*.pdf\n*\n"


async def test_document_only_task_reaches_exact_thread_as_existing_file(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.on_frame({"type": "run", "session": "a", "text": "", "attachments": [document()]})
        sent = next(call for call in fake.calls if call["method"] == "turn/start")["params"]
        assert sent["threadId"] == "a" and sent["input"][0]["type"] == "text"
        assert "Отчёт.pdf" in sent["input"][0]["text"]
        assert list((tmp_path / ".bax-attachments").rglob("Отчёт.pdf"))[0].read_bytes() == b"%PDF-1.7\n"
