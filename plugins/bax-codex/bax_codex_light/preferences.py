"""Настройки агента проекта; отдельно от ключей регистрации и настроек Codex."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path


class Preferences:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise ValueError("Файл настроек доступен другим пользователям: установите chmod 600")
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("Повреждён файл настроек агента")
        return data

    def get(self, project: Path, agent: str) -> bool:
        agents = self._read().get(str(project.resolve()), {})
        if not isinstance(agents, dict):
            raise ValueError("Повреждены настройки проекта")
        value = agents.get(agent, {})
        if not isinstance(value, dict) or type(value.get("keep_awake", True)) is not bool:
            raise ValueError("Защита от сна должна быть true или false")
        return value.get("keep_awake", True)

    def put(self, project: Path, agent: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("Защита от сна должна быть true или false")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            key = str(project.resolve())
            agents = data.setdefault(key, {})
            if not isinstance(agents, dict):
                raise ValueError("Повреждены настройки проекта")
            agents[agent] = {"keep_awake": enabled}
            fd, temp = tempfile.mkstemp(prefix=".codex-light-settings-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, self.path)
            finally:
                Path(temp).unlink(missing_ok=True)
