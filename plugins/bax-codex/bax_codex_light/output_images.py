"""Изображения, явно включённые в ответ: локальные файлы передаются вместе с историей."""

from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote

from .files import allowed
from .images import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_TOTAL_BYTES

LINK = re.compile(r"\!?\[([^\]\n]*)\]\((?:<((?:sandbox:/|/)[^<>\r\n]+)>|((?:sandbox:/|/)[^\s)]+))\)")


def collect(project: Path, thread_id: str, text: str) -> list[dict]:
    result, seen, total = [], set(), 0
    # Кодовые блоки описывают синтаксис, а не публикуют файлы.
    text = re.sub(r"```.*?(?:```|$)", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    for match in LINK.finditer(text):
        label, quoted_url, bare_url = match.groups()
        url = quoted_url or bare_url
        if url in seen or len(result) >= MAX_IMAGES:
            continue
        seen.add(url)
        raw = unquote(url.removeprefix("sandbox:"))
        path = Path(raw)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        item = {"url": url, "name": label or path.name, "filename": path.name}
        result.append(item)
        try:
            roots = [project.resolve(), Path("/mnt/data")]
            # Генератор сохраняет результаты в каталоге точного разговора.
            if thread_id and re.fullmatch(r"[\w-]+", thread_id):
                roots.append(
                    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                    / "generated_images"
                    / thread_id
                )
            root = next((root for root in roots if path.is_relative_to(root)), None)
            if root is None or not allowed(str(path.relative_to(root))):
                raise ValueError("Изображение находится вне доступных результатов этого разговора")
            # Не следуем symlink ни в одном компоненте разрешённого каталога.
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = path.relative_to(root).parts
                for part in parts[:-1]:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise ValueError("Изображение должно быть обычным файлом")
                    data = file.read(MAX_IMAGE_BYTES + 1)
            finally:
                os.close(fd)
            if len(data) > MAX_IMAGE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("Изображение превышает лимит передачи")
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif data.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif data.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ValueError("Файл не является поддерживаемым изображением")
            total += len(data)
            item.update(mime=mime, data=base64.b64encode(data).decode("ascii"))
        except (OSError, ValueError):
            item["error"] = "Изображение недоступно. Попросите агента приложить его заново."
    return result
