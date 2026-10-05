"""Документы с телефона → закрытая папка проекта, картинки → штатный input Codex."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path

from . import images


def prepare(project: Path, attachments: object) -> tuple[list[dict], str]:
    if attachments is None or attachments == []:
        return [], ""
    if not isinstance(attachments, list) or len(attachments) > images.MAX_IMAGES:
        raise ValueError("Можно прикрепить до 8 файлов")
    pictures, documents, total = [], [], 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("Не удалось прочитать вложение")
        mime, encoded = item.get("mime"), item.get("data")
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime):
            raise ValueError("Неизвестный формат файла")
        if not isinstance(encoded, str) or len(encoded) > ((images.MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("Один файл должен быть не больше 6 МиБ")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Не удалось прочитать данные файла") from error
        total += len(data)
        if len(data) > images.MAX_IMAGE_BYTES or total > images.MAX_TOTAL_BYTES:
            raise ValueError("Файлы в одной задаче должны быть не больше 8 МиБ вместе")
        if mime.startswith("image/"):
            pictures.append(item)
        else:
            name = item.get("name")
            if (
                not isinstance(name, str)
                or not name.strip()
                or name in {".", ".."}
                or any(ch in name for ch in ("/", "\\"))
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
                or len(name.encode()) > 240
            ):
                raise ValueError("Недопустимое имя файла")
            documents.append((name, data))
    inputs = images.inputs(pictures)
    if not documents:
        return inputs, ""
    root = project.resolve() / ".bax-attachments"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.resolve().parent != project.resolve():
        raise ValueError("Папка вложений должна находиться внутри проекта и не быть ссылкой")
    # Вложения не попадают в обычный git add/commit и сохраняются для resume.
    ignore = root / ".gitignore"
    try:
        fd = os.open(ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if ignore.is_symlink():
            raise ValueError("Файл исключений вложений не должен быть ссылкой") from None
        # Последнее правило сохраняет исключение и при уже существующем .gitignore.
        fd = os.open(ignore, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r+") as stream:
            existing = stream.read()
            if existing.splitlines()[-1:] != ["*"]:
                stream.write(("" if not existing or existing.endswith("\n") else "\n") + "*\n")
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write("*\n")
    folder = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
    paths = []
    for name, data in documents:
        # Отдельная папка для каждого файла сохраняет даже одинаковые имена.
        target = Path(tempfile.mkdtemp(prefix="file-", dir=folder)) / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        paths.append(f"{json.dumps(name, ensure_ascii=False)}: {json.dumps(str(target), ensure_ascii=False)}")
    return inputs, "\n\nПрикреплённые файлы (прочитайте их при выполнении задачи):\n" + "\n".join(paths)
