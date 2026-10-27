"""Преобразование истории Codex в строки Бакса со стабильными ID и пагинацией."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .images import from_item, preview

log = logging.getLogger(__name__)


def submission_key(item: dict) -> str:
    """Ключ подтверждения отправки; не идентичность строки истории."""
    return item.get("clientId") or item["id"]


def message_key(turn_id: str, item_id: str) -> tuple[str, str]:
    """Идентичность внутри точного разговора, общая для истории и live-событий."""
    return turn_id, item_id


def message_id(thread_id: str, turn_id: str, item_id: str) -> str:
    """Wire ID: SHA-256 канонического ключа, без текста, даты и номера в ленте."""
    source = json.dumps(
        ["bax-message-v1", "codex", thread_id, turn_id, item_id], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def stopped_entry(turn: dict) -> dict | None:
    """Служебная строка из подтверждённого исхода хода, без записи в Codex."""
    if turn.get("status") != "interrupted" or not turn.get("id"):
        return None
    return {
        "turnId": turn["id"],
        "item": {"id": f"bax:stopped:{turn['id']}", "type": "baxTurnStopped"},
        **({"created_at": turn["completedAt"]} if turn.get("completedAt") else {}),
    }


def render(item: dict) -> tuple[str, str] | None:
    kind = item.get("type")
    if kind == "baxTurnStopped":
        return "event", "Работа остановлена"
    if kind == "userMessage":
        content = item.get("content", [])
        text = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
        return "user", preview(text, sum(p.get("type") in {"image", "localImage"} for p in content))
    if kind == "agentMessage":
        text = item.get("text", "")
        # В async-сообщении сам вопрос может быть только в questions, без text.
        missing = [q for q in item.get("questions") or [] if q["title"] not in text]
        questions = [
            "\n".join([q["title"], *(f"• {option}" for option in q.get("options") or [])]) for q in missing
        ]
        return "assistant", "\n\n".join(filter(None, [text, *questions]))
    if kind == "commandExecution":
        return "tool", item.get("command", "Команда")
    if kind == "fileChange":
        return "tool", "Изменение: " + ", ".join(change.get("path", "") for change in item.get("changes", []))
    if kind in {"mcpToolCall", "dynamicToolCall"}:
        return "tool", "/".join(filter(None, [item.get("server", item.get("namespace")), item.get("tool")]))
    if kind == "webSearch":
        return "tool", "Поиск: " + item.get("query", "")
    if kind == "plan":
        return "thinking", item.get("text", "План")
    if kind == "reasoning":
        return "thinking", "Обдумывает задачу"
    return None


class HistoryOrderError(ValueError):
    """Локальные номера больше не соответствуют истории Codex."""


class History:
    def __init__(self, app: Any, thread_id: str):
        self.app = app
        self.thread_id = thread_id
        self.ids: dict[tuple[str, str], int] = {}
        self.previews: dict[str, int] = {}
        self.recorded: set[str] = set()
        self.low = self.high = 1 << 40
        self.initialized = False
        self.cursors: dict[int, str | None] = {}
        self.rebase_on_reload = False

    def live_id(self, item_id: str, turn_id: str, *, client_id: str | None = None) -> int:
        # Native item ID уникален только внутри хода. Объект History — внутри разговора.
        key = message_key(turn_id, item_id)
        if key not in self.ids:
            if client_id in self.previews:
                self.ids[key] = self.previews.pop(client_id)
                self.high = max(self.high, self.ids[key])
            else:
                self.high += 1 << 20
                self.ids[key] = self.high
        return self.ids[key]

    def preview_id(self, client_id: str) -> int:
        """Эхо очереди ещё не задаёт позицию сообщения в настоящей истории Codex."""
        if client_id not in self.previews:
            # Оставляем место для событий ещё активного хода. high продвинется
            # сюда только после настоящего появления сообщения в Codex.
            self.previews[client_id] = max(self.high, max(self.previews.values(), default=0)) + (1 << 40)
        return self.previews[client_id]

    async def page(self, before: int | None = None, limit: int = 50) -> list[dict]:
        if before is not None and before not in self.cursors:
            raise ValueError("Неизвестный курсор истории; заново откройте переписку")
        cursor = self.cursors.get(before) if before is not None else None
        if before is not None and cursor is None:
            return []
        entries: list[dict] = []
        conversation_count = 0
        next_cursor = cursor
        for _ in range(20):
            result = await self.app.items(self.thread_id, next_cursor, 100)
            batch = result.get("data", [])
            entries.extend(batch)
            conversation_count += sum(e["item"].get("type") in {"userMessage", "agentMessage"} for e in batch)
            next_cursor = result.get("nextCursor")
            if not next_cursor or conversation_count >= limit:
                break
        # Страницы могут перекрываться, если между запросами добавились события.
        unique, seen = [], set()
        for entry in entries:
            item_id = message_key(entry["turnId"], entry["item"]["id"])
            if item_id not in seen:
                unique.append(entry)
                seen.add(item_id)
        entries = list(reversed(unique))
        chronological = [message_key(entry["turnId"], entry["item"]["id"]) for entry in entries]
        self.recorded.update(
            submission_key(entry["item"]) for entry in entries if entry["item"].get("type") == "userMessage"
        )
        for entry in entries:
            item = entry["item"]
            if item.get("clientId") in self.previews:
                self.live_id(item["id"], entry["turnId"], client_id=item["clientId"])
        if before is None and self.rebase_on_reload:
            self._rebase()
        try:
            self._assign(chronological, newest=before is None and self.initialized)
        except HistoryOrderError:
            if before is not None:
                # Перезаход перечитывает только свежую пачку: испорченные старые
                # якоря могут туда не попасть и иначе сохранятся до перезапуска контроллера.
                self.rebase_on_reload = True
                log.warning("Изменился порядок старой страницы; следующая загрузка сбросит её шкалу")
                raise ValueError("История обновилась; заново откройте переписку") from None
            # Полная загрузка заменяет ленту на телефоне (history.done). Новая шкала
            # номеров восстанавливает порядок без изменения истории/разговора Codex.
            self._rebase()
            self._assign(chronological, newest=True)
        self.initialized = True
        rows: list[dict] = []
        ordered = entries
        metadata = (
            await self.app.response_metadata(self.thread_id, ordered)
            if hasattr(self.app, "response_metadata")
            else {}
        )
        for entry in ordered:
            item = entry["item"]
            view = render(item)
            if view is None or not view[1]:
                continue
            row = {
                "id": self.ids[message_key(entry["turnId"], item["id"])],
                "message_id": message_id(self.thread_id, entry["turnId"], item["id"]),
                "kind": view[0],
                "text": view[1],
                "turn_id": entry.get("turnId", ""),
            }
            if row["kind"] == "user":
                pictures = await from_item(item)
                if pictures:
                    row["images"] = pictures
                if item.get("clientId"):
                    row["rid"] = item["clientId"]
            detail = metadata.get((entry.get("turnId", ""), item["id"]), {})
            if row["kind"] == "assistant":
                row.update(detail)
            elif row["kind"] == "user" and "created_at" in detail:
                row["created_at"] = detail["created_at"]
            if item.get("type") == "baxTurnStopped":
                row["event"] = "stopped"
                if entry.get("created_at"):
                    row["created_at"] = entry["created_at"]
            if row["kind"] in {"tool", "thinking"}:
                if rows and rows[-1]["kind"] == "steps":
                    rows[-1].update(
                        id=row["id"],
                        message_id=row["message_id"],
                        text=row["text"],
                        count=rows[-1]["count"] + 1,
                    )
                else:
                    rows.append({**row, "kind": "steps", "count": 1})
            else:
                rows.append(row)
        if rows:
            self.cursors[rows[0]["id"]] = next_cursor
        return rows

    def _rebase(self) -> None:
        self.low = self.high = max(self.high, max(self.previews.values(), default=0)) + (1 << 20)
        self.ids = {}
        self.previews = {}
        self.cursors = {}
        self.rebase_on_reload = False

    def _assign(self, chronological: list[tuple[str, str]], *, newest: bool) -> None:
        """Вставка пропущенных элементов между известными, без изменения старых ID."""
        ids = dict(self.ids)
        known = [ids[item_id] for item_id in chronological if item_id in ids]
        if any(left >= right for left, right in zip(known, known[1:], strict=False)):
            raise HistoryOrderError("Порядок сообщений истории изменился")
        low, high = self.low, self.high
        step = 1 << 20
        index = 0
        while index < len(chronological):
            if chronological[index] in ids:
                index += 1
                continue
            end = index
            while end < len(chronological) and chronological[end] not in ids:
                end += 1
            count = end - index
            left = ids.get(chronological[index - 1]) if index else None
            right = ids.get(chronological[end]) if end < len(chronological) else None
            if left is None and right is None:
                left = high if newest else low - step * (count + 1)
                right = left + step * (count + 1)
            elif left is None:
                left = min(low, right) - step * (count + 1)
            elif right is None:
                right = max(high, left) + step * (count + 1)
            increment = (right - left) // (count + 1)
            if increment < 1:
                raise HistoryOrderError("Между номерами сообщений не осталось места")
            for offset, item_id in enumerate(chronological[index:end], 1):
                ids[item_id] = left + increment * offset
            low = min(low, ids[chronological[index]])
            high = max(high, ids[chronological[end - 1]])
            index = end

        self.ids = ids
        self.low, self.high = low, high
