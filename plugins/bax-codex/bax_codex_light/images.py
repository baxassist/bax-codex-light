"""Картинки телефона → штатный image input Codex, без файлов и второй базы вложений."""

from __future__ import annotations

import base64
import binascii

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
