"""Подключение к существующему daemon Codex со схемами официального SDK."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openai_codex.generated import v2_all as schemas
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from websockets.asyncio.client import connect, unix_connect

from . import __version__

EventHandler = Callable[[dict], Awaitable[None]]


class RPCError(RuntimeError):
    pass


class RPCRejected(RPCError):
    """Сервер явно отклонил запрос: в отличие от тайм-аута, ответ получен."""


class AppServer:
    def __init__(self, endpoint: str | None = None, *, timeout: float = 20):
        self.endpoint = endpoint or str(Path.home() / ".codex/app-server-control/app-server-control.sock")
        self.timeout = timeout
        self.ws: Any = None
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=4096)
        self.reader: asyncio.Task | None = None
        self.consumer: asyncio.Task | None = None
        self.handler: EventHandler | None = None
        self.closed = asyncio.Event()
        self.info: dict = {}

    async def open(self, handler: EventHandler | None = None) -> None:
        self.handler = handler
        self.closed.clear()
        if "://" in self.endpoint:
            url = urlsplit(self.endpoint)
            if url.scheme != "ws" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("App-server должен быть локальным: Unix socket или loopback ws://")
            self.ws = await connect(
                self.endpoint, max_size=16 * 1024 * 1024, open_timeout=self.timeout, close_timeout=2
            )
        else:
            self.ws = await unix_connect(
                self.endpoint,
                uri="ws://localhost/",
                max_size=16 * 1024 * 1024,
                open_timeout=self.timeout,
                close_timeout=2,
            )
        self.reader = asyncio.create_task(self._read(), name="codex-rpc-reader")
        self.consumer = asyncio.create_task(self._consume(), name="codex-events")
        try:
            self.info = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "bax_codex_light",
                        "title": "Bax Codex Light",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        except BaseException:
            await self.close()
            raise

    async def _read(self) -> None:
        try:
            async for payload in self.ws:
                message = json.loads(payload)
                if not isinstance(message, dict):
                    continue
                if "method" not in message and "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RPCRejected(str(message["error"].get("message", "RPC error")))
                            )
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message:
                    self.events.put_nowait(message)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RPCError("Связь с app-server потеряна"))
            self.pending.clear()
            self.closed.set()
            if self.ws is not None:
                await self.ws.close()

    async def _consume(self) -> None:
        while True:
            message = await self.events.get()
            model = NOTIFICATION_MODELS.get(message.get("method", ""))
            if "id" not in message and model is not None:
                try:
                    validated = model.model_validate(message.get("params", {}))
                    message["params"] = validated.model_dump(
                        mode="json", by_alias=True, exclude_none=True, exclude_unset=True
                    )
                except ValueError:
                    continue
            if self.handler:
                try:
                    await self.handler(message)
                except Exception:
                    await self.ws.close(code=1011, reason="event handler failed")
                    return

    async def request(self, method: str, params: dict) -> dict:
        if self.ws is None or self.closed.is_set():
            raise RPCError("App-server не подключён")
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(request_id, None)

    async def typed(self, method: str, params_type: type, response_type: type, params: dict) -> dict:
        request = params_type.model_validate(params).model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_unset=True
        )
        response = await self.request(method, request)
        return response_type.model_validate(response).model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_unset=True
        )

    async def inspect(self, thread_id: str, project: Path | None = None) -> dict:
        params = schemas.ThreadReadParams.model_validate({"threadId": thread_id, "includeTurns": False})
        raw = await self.request("thread/read", params.model_dump(by_alias=True, exclude_unset=True))
        result = schemas.ThreadReadResponse.model_validate(raw).model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_unset=True
        )
        thread = result["thread"]
        thread_project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
        expected_project = await asyncio.to_thread(project.resolve) if project else thread_project
        if thread["id"] != thread_id or thread_project != expected_project or not thread_project.is_dir():
            raise RPCError("ID сессии или её рабочий каталог не совпадает с выбранным проектом")
        if thread["status"]["type"] in {"notLoaded", "systemError"}:
            raise RPCError("Сессия не открыта в Codex. Мост не запускает закрытые сессии")
        if raw["thread"].get("canAcceptDirectInput") is False:
            raise RPCError("Эта сессия Codex не принимает прямые задачи")
        return thread

    async def attach(self, thread_id: str, project: Path) -> dict:
        await self.inspect(thread_id, project)
        result = await self.typed(
            "thread/resume",
            schemas.ThreadResumeParams,
            schemas.ThreadResumeResponse,
            {"threadId": thread_id, "excludeTurns": True},
        )
        expected_project = await asyncio.to_thread(project.resolve)
        if result["thread"]["id"] != thread_id or result["thread"]["cwd"] != str(expected_project):
            raise RPCError("App-server подписал мост на другую сессию или проект")
        return result["thread"]

    async def items(self, thread_id: str, cursor: str | None, limit: int) -> dict:
        return await self.typed(
            "thread/items/list",
            schemas.ThreadItemsListParams,
            schemas.ThreadItemsListResponse,
            {
                "threadId": thread_id,
                "cursor": cursor,
                "limit": limit,
                "sortDirection": "desc",
            },
        )

    async def start_turn(self, thread_id: str, text: str, client_id: str) -> dict:
        return await self.typed(
            "turn/start",
            schemas.TurnStartParams,
            schemas.TurnStartResponse,
            {
                "threadId": thread_id,
                "clientUserMessageId": client_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def respond(self, request_id: int | str, result: dict) -> None:
        if self.closed.is_set() or self.ws is None:
            raise RPCError("App-server не подключён")
        await self.ws.send(json.dumps({"id": request_id, "result": result}))

    async def close(self) -> None:
        self.closed.set()
        if self.ws is not None:
            await self.ws.close()
        for task in (self.reader, self.consumer):
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.ws = None
