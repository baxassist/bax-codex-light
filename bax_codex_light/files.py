"""Чтение tracked-файлов проекта с проверками пути, типа и служебных имён."""

from __future__ import annotations

import asyncio
import codecs
import os
import stat
from pathlib import Path, PurePosixPath

MAX_FILE = 256 * 1024
MAX_PATHS = 5000


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts or "\x00" in path:
        return False
    blocked = {
        ".git",
        ".aws",
        ".ssh",
        ".bax",
        ".codex",
        ".agents",
        "credentials",
        "credentials.json",
        "auth.json",
        "id_rsa",
        "id_ed25519",
        ".netrc",
        ".npmrc",
        ".pypirc",
    }
    return not any(
        part.lower() in blocked
        or part.lower().startswith(".env")
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    )


async def tracked(project: Path) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(project),
        "ls-files",
        "-z",
        "--",
        ".",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    if process.returncode:
        return []
    return sorted(
        path for raw in output.split(b"\0") if raw and allowed(path := raw.decode("utf-8", "replace"))
    )


def read(project: Path, path: str, paths: list[str]) -> dict:
    if not allowed(path) or path not in paths:
        return {"error": "Файл не входит в разрешённые tracked-файлы проекта"}
    fd = os.open(project.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                return {"error": "Можно читать только обычные файлы"}
            data = file.read(MAX_FILE + 1)
        if b"\0" in data:
            return {"error": "Двоичный файл"}
        try:
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(data[:MAX_FILE], final=len(data) <= MAX_FILE)
        except UnicodeDecodeError:
            return {"error": "Файл не является текстом UTF-8"}
        return {"text": text, "size": metadata.st_size, "truncated": len(data) > MAX_FILE}
    except OSError:
        return {"error": "Файл недоступен или является symlink"}
    finally:
        os.close(fd)
