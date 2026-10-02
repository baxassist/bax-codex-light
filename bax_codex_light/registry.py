"""Отдельное приватное хранилище, не затрагивающее регистрацию Claude."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError("URL сервера не должен содержать пароль, fragment или пустой host")
    if parsed.scheme != "wss" and not (
        parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Используйте wss://; ws:// разрешён только для loopback")
    return url


@dataclass(frozen=True)
class Registration:
    agent: str
    key_id: str
    secret: str
    server: str
    install_id: str
    engine: str = "codex_light"

    @classmethod
    def from_key(cls, key: str, server: str, *, compatibility: bool = False) -> Registration:
        parts = key.strip().split(":")
        if len(parts) != 3 or len(parts[2]) < 16:
            raise ValueError(
                "Ключ должен иметь вид agent_uuid:key_uuid:secret (секрет не короче 16 символов)"
            )
        return cls(
            str(UUID(parts[0])),
            str(UUID(parts[1])),
            parts[2],
            validate_url(server),
            str(uuid4()),
            "claude_code_lite" if compatibility else "codex_light",
        )


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = path or Path.home() / ".bax" / "codex-light.json"

    def _read(self) -> dict:
        if self.path.is_symlink():
            raise ValueError("Файл регистрации не должен быть symlink")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise ValueError("Файл регистрации доступен другим пользователям: установите chmod 600")
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("Повреждён файл регистрации")
        return data

    def get(self, project: Path) -> Registration | None:
        value = self._read().get(str(project.resolve()))
        if value is None:
            return None
        registration = Registration(**value)
        validate_url(registration.server)
        if registration.engine not in {"codex_light", "claude_code_lite"}:
            raise ValueError("Неизвестный движок регистрации")
        return registration

    def put(self, project: Path, registration: Registration) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            project_key = str(project.resolve())
            for path, saved in data.items():
                if saved.get("key_id") == registration.key_id:
                    if path != project_key:
                        raise ValueError("Этот ключ уже привязан к другому проекту; выпустите отдельный ключ")
                    registration = replace(registration, install_id=saved["install_id"])
            data[project_key] = asdict(registration)
            fd, temp = tempfile.mkstemp(prefix=".codex-light-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, self.path)
            finally:
                Path(temp).unlink(missing_ok=True)
