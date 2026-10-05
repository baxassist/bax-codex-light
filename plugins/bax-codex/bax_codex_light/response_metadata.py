"""Параметры ответа из точного журнала Codex, без подстановки текущей модели.

SDK отдаёт ID хода и текст, но legacy history не содержит времени/модели элемента.
Путь берём только из проверенного thread/read. Содержимое ответов не сохраняем:
для сопоставления legacy ID используются хеш, ход, роль, phase и порядок совпадений.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def fingerprint(item: dict, turn_id: str) -> tuple | None:
    if item.get("type") != "agentMessage" or not item.get("text"):
        return None
    return turn_id, item.get("phase"), hashlib.sha256(item["text"].encode()).hexdigest()


class ResponseMetadata:
    def __init__(self, thread: dict, codex_home: Path):
        self.thread_id = thread["id"]
        self.project = Path(thread["cwd"]).resolve()
        self.path: Path | None = None
        if thread.get("path"):
            candidate = Path(thread["path"])
            resolved = candidate.resolve()
            roots = [codex_home / "sessions", codex_home / "archived_sessions"]
            if candidate.is_absolute() and any(resolved.is_relative_to(root.resolve()) for root in roots):
                self.path = resolved
        self.offset = 0
        self.inode = None
        self.verified = False
        self.context: dict = {}
        self.by_id: dict[str, dict] = {}
        self.by_fingerprint: dict[tuple, list[dict]] = defaultdict(list)
        self.turns: dict[str, dict] = {}
        self.token_usage: dict = {}

    def refresh(self) -> None:
        if not self.path:
            return
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            stat = os.fstat(file.fileno())
            if stat.st_ino != self.inode or stat.st_size < self.offset:
                self.offset = 0
                self.inode = stat.st_ino
                self.verified = False
                self.context = {}
                self.by_id.clear()
                self.by_fingerprint.clear()
                self.turns.clear()
                self.token_usage = {}
            file.seek(self.offset)
            while line := file.readline(16 * 1024 * 1024):
                if not line.endswith(b"\n"):
                    # Незавершённую запись дочитаем при следующем событии.
                    break
                self.offset = file.tell()
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                self._record(row)

    def _record(self, row: dict) -> None:
        kind, payload = row.get("type"), row.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "session_meta":
            self.verified = payload.get("id") == self.thread_id and payload.get("cwd") == str(self.project)
            if not self.verified:
                raise ValueError("Журнал принадлежит другому разговору или проекту")
            return
        if not self.verified:
            return
        if kind == "turn_context":
            turn = payload.get("turn_id")
            if isinstance(turn, str) and turn and payload.get("cwd") == str(self.project):
                self.context = {"turn_id": turn}
                for field in ("model", "effort"):
                    value = payload.get(field)
                    if isinstance(value, str) and value:
                        self.context[field] = value
                self.turns[turn] = dict(self.context)
            else:
                self.context = {}
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            used, maximum = last.get("total_tokens"), info.get("model_context_window")
            if type(used) is int and used >= 0 and type(maximum) is int and maximum > 0:
                self.token_usage = {"used": used, "max": maximum, "model": self.context.get("model", "")}
        elif (
            kind == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
        ):
            text = "".join(
                part.get("text", "") for part in payload.get("content", []) if isinstance(part, dict)
            )
            if not text or not self.context:
                return
            metadata = dict(self.context)
            try:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    metadata["created_at"] = stamp.timestamp()
            except (ValueError, KeyError, TypeError):
                pass
            item_id = payload.get("id")
            if isinstance(item_id, str) and item_id:
                if item_id in self.by_id:
                    return
                self.by_id[item_id] = metadata
            key = self.context["turn_id"], payload.get("phase"), hashlib.sha256(text.encode()).hexdigest()
            self.by_fingerprint[key].append(metadata)

    def decorate(self, entries: list[dict]) -> dict[tuple[str, str], dict]:
        self.refresh()
        result = {}
        groups = defaultdict(list)
        for entry in entries:
            item, turn = entry["item"], entry["turnId"]
            key = turn, item["id"]
            if metadata := self.by_id.get(item["id"]):
                if metadata["turn_id"] == turn:
                    result[key] = dict(metadata)
            elif value := fingerprint(item, turn):
                groups[value].append(key)
            # В paginated API время элемента известно независимо от журнала.
            stamp = entry.get("completedAtMs") or entry.get("startedAtMs")
            if isinstance(stamp, (int, float)) and stamp > 0:
                result.setdefault(key, {})["created_at"] = stamp / 1000
        for value, keys in groups.items():
            candidates = self.by_fingerprint.get(value, [])
            if len(candidates) == len(keys):
                # entries идут от раннего к позднему. Полная legacy-пачка хода
                # позволяет различить даже одинаковые ответы с разным временем.
                for key, metadata in zip(keys, candidates, strict=True):
                    result[key] = {**metadata, **result.get(key, {})}
            elif candidates:
                # Неполное/неоднозначное совпадение: только одинаковые параметры,
                # время и модель соседнего ответа угадывать нельзя.
                common = {k: v for k, v in candidates[0].items() if all(c.get(k) == v for c in candidates)}
                for key in keys:
                    result[key] = {**common, **result.get(key, {})}
        return result

    def live(self, item: dict, turn_id: str) -> dict:
        return self.decorate([{"item": item, "turnId": turn_id}]).get((turn_id, item["id"]), {})
