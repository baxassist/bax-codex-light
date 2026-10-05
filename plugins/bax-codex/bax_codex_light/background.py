"""Документированные backgroundTerminals API; SDK 0.160 ещё не содержит эти типы.

Не подменяем неподдерживаемый метод пустым списком. Остановка адресуется только
processId, который сам app-server вернул для проверенного разговора проекта.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ListParams(BaseModel):
    thread_id: str = Field(alias="threadId")
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=100)


class Terminal(BaseModel):
    process_id: str = Field(alias="processId")
    item_id: str = Field(alias="itemId")
    command: str
    cwd: str


class ListResponse(BaseModel):
    data: list[Terminal]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class TerminateParams(BaseModel):
    thread_id: str = Field(alias="threadId")
    process_id: str = Field(alias="processId")


class TerminateResponse(BaseModel):
    terminated: bool
