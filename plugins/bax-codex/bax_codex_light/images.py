"""Картинки телефона → штатный image input Codex, без файлов и второй базы вложений."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import stat
from pathlib import Path

from .files import allowed
from .tracing import dependency

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


def inputs(attachments: object) -> list[dict]:
    if attachments is None:
        return []
    if not isinstance(attachments, list) or len(attachments) > MAX_IMAGES:
        raise ValueError("Можно прикрепить до 8 картинок")
    result = []
    total = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("Не удалось прочитать вложение")
        mime, encoded = attachment.get("mime"), attachment.get("data")
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise ValueError("Поддерживаются картинки JPEG, PNG, WebP и GIF")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("Одна картинка должна быть не больше 6 МиБ")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Не удалось прочитать данные картинки") from error
        signatures = {
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        }
        if not signatures[mime]:
            raise ValueError("Данные вложения не соответствуют формату картинки")
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Картинки в одной задаче должны быть не больше 8 МиБ вместе")
        result.append({"type": "image", "url": f"data:{mime};base64,{encoded}"})
    return result


def preview(text: str, image_count: int) -> str:
    return text or (f"Картинок: {image_count}" if image_count else "")


def _native_file(raw: str) -> bytes:
    """Только точный localImage из userMessage, без поиска файлов и переходов по symlink."""
    path = Path(raw)
    parts = list(path.parts[1:])
    # Codex хранит выбранные пользователем изображения в своём cache. Это не
    # разрешение читать произвольный .codex: служебные имена всё ещё запрещены.
    if ".codex" in parts:
        index = parts.index(".codex")
        if index + 1 >= len(parts) or parts[index + 1] not in {"tmp", "sessions", "attachments", "images"}:
            raise ValueError("Служебный путь")
        parts[index] = "codex-images"
    if (
        not path.is_absolute()
        or not allowed("/".join(parts))
        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ):
        raise ValueError("Недопустимый путь картинки")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Нужен обычный файл")
            data = handle.read(MAX_IMAGE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение слишком большое")
    return data


async def from_item(item: dict) -> list[dict]:
    """Одинаковые безопасные вложения для live/history точного native userMessage."""
    if item.get("type") != "userMessage":
        return []
    result, seen, total, processed = [], set(), 0, 0
    for part in item.get("content", []):
        if not isinstance(part, dict) or part.get("type") not in {"image", "localImage"}:
            continue
        if processed >= MAX_IMAGES:
            break
        processed += 1
        index = len(result) + 1
        picture = {
            "url": f"bax-input-image:unavailable:{index}",
            "name": f"Фото {index}",
            "filename": f"photo-{index}",
        }
        try:
            if part["type"] == "localImage":
                raw = part.get("path")
                if not isinstance(raw, str):
                    raise ValueError("Нет пути картинки")
                with dependency("filesystem", "read"):
                    data = await asyncio.to_thread(_native_file, raw)
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime = "image/png"
                elif data.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif data.startswith((b"GIF87a", b"GIF89a")):
                    mime = "image/gif"
                elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                    mime = "image/webp"
                else:
                    raise ValueError("Неверный формат картинки")
                encoded = base64.b64encode(data).decode("ascii")
            else:
                url = part.get("url", "")
                if not isinstance(url, str) or len(url) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 100:
                    raise ValueError("Изображение слишком большое")
                header, encoded = url.split(",", 1)
                if not header.startswith("data:") or not header.endswith(";base64"):
                    raise ValueError("Удалённые изображения не загружаются")
                mime = header[5:-7]
                inputs([{"mime": mime, "data": encoded}])
                data = base64.b64decode(encoded, validate=True)
            if total + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("Превышен размер вложений")
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            total += len(data)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[
                mime
            ]
            picture.update(
                url=f"bax-input-image:{digest}", mime=mime, data=encoded, filename=f"photo-{index}{suffix}"
            )
        except (OSError, ValueError):
            picture["error"] = "Изображение недоступно. Прикрепите его заново."
        result.append(picture)
    return result
