"""Преобразование истории Codex в строки Бакса со стабильными ID и пагинацией."""

from __future__ import annotations

from typing import Any


def identity(item: dict) -> str:
    return item.get("clientId") or item["id"]


def render(item: dict) -> tuple[str, str] | None:
    kind = item.get("type")
    if kind == "userMessage":
        return "user", "\n".join(
            part.get("text", "") for part in item.get("content", []) if part.get("type") == "text"
        )
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


class History:
    def __init__(self, app: Any, thread_id: str):
        self.app = app
        self.thread_id = thread_id
        self.ids: dict[str, int] = {}
        self.previews: dict[str, int] = {}
        self.recorded: set[str] = set()
        self.low = self.high = 1 << 40
        self.initialized = False
        self.cursors: dict[int, str | None] = {}

    def live_id(self, item_id: str) -> int:
        if item_id not in self.ids:
            if item_id in self.previews:
                self.ids[item_id] = self.previews.pop(item_id)
                self.high = max(self.high, self.ids[item_id])
            else:
                self.high += 1 << 20
                self.ids[item_id] = self.high
        return self.ids[item_id]

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
        chronological = [identity(entry["item"]) for entry in reversed(entries)]
        self.recorded.update(
            identity(entry["item"]) for entry in entries if entry["item"].get("type") == "userMessage"
        )
        for item_id in chronological:
            if item_id in self.previews:
                self.live_id(item_id)
        self._assign(chronological, newest=before is None and self.initialized)
        self.initialized = True
        rows: list[dict] = []
        for entry in reversed(entries):
            item = entry["item"]
            view = render(item)
            if view is None or not view[1]:
                continue
            row = {"id": self.ids[identity(item)], "kind": view[0], "text": view[1]}
            if row["kind"] in {"tool", "thinking"}:
                if rows and rows[-1]["kind"] == "steps":
                    rows[-1].update(id=row["id"], text=row["text"], count=rows[-1]["count"] + 1)
                else:
                    rows.append({**row, "kind": "steps", "count": 1})
            else:
                rows.append(row)
        if rows:
            self.cursors[rows[0]["id"]] = next_cursor
        return rows

    def _assign(self, chronological: list[str], *, newest: bool) -> None:
        """Вставка пропущенных элементов между известными, без изменения старых ID."""
        step = 1 << 20
        index = 0
        while index < len(chronological):
            if chronological[index] in self.ids:
                index += 1
                continue
            end = index
            while end < len(chronological) and chronological[end] not in self.ids:
                end += 1
            count = end - index
            left = self.ids.get(chronological[index - 1]) if index else None
            right = self.ids.get(chronological[end]) if end < len(chronological) else None
            if left is None and right is None:
                left = self.high if newest else self.low - step * (count + 1)
                right = left + step * (count + 1)
            elif left is None:
                left = min(self.low, right) - step * (count + 1)
            elif right is None:
                right = max(self.high, left) + step * (count + 1)
            increment = (right - left) // (count + 1)
            if increment < 1:
                raise ValueError("Слишком много вставок истории: перезапустите мост")
            for offset, item_id in enumerate(chronological[index:end], 1):
                self.ids[item_id] = left + increment * offset
            self.low = min(self.low, self.ids[chronological[index]])
            self.high = max(self.high, self.ids[chronological[end - 1]])
            index = end
