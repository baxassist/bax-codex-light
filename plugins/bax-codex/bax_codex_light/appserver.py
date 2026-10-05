"""Подключение к существующему daemon Codex со схемами официального SDK."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openai_codex.generated import v2_all as schemas
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from pydantic import Field
from websockets.asyncio.client import connect, unix_connect

from . import __version__, background
from .response_metadata import ResponseMetadata

EventHandler = Callable[[dict], Awaitable[None]]
EMPTY_THREAD_NAME = "Новая сессия"


class ThreadStartWithHistoryParams(schemas.ThreadStartParams):
    # SDK 0.160.0 знает тип формата, но ещё не включает экспериментальный параметр
    # создания. Paginated creation не поддержан; выбираем штатный legacy contract.
    history_mode: schemas.ThreadHistoryMode = Field(alias="historyMode")


class RPCError(RuntimeError):
    pass


class RPCRejected(RPCError):
    """Сервер явно отклонил запрос: в отличие от тайм-аута, ответ получен."""


class AppServer:
    def __init__(self, endpoint: str | None = None, *, timeout: float = 20):
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.endpoint = endpoint or str(codex_home / "app-server-control/app-server-control.sock")
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
        self.approvals: dict = {}
        self.configurations: dict[str, dict] = {}
        self.turn_history: set[str] = set()
        self.threads: dict[str, dict] = {}
        self.metadata: dict[str, ResponseMetadata] = {}
        self.metadata_lock = asyncio.Lock()

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
        result = response_type.model_validate(response).model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_unset=True
        )
        # SDK 0.160.0 ещё не включает происхождение именованного профиля в ответах.
        # Эффективный sandbox выше проверен штатным типом; сохраняем только ID профиля.
        if method in {"thread/start", "thread/resume"} and response.get("activePermissionProfile"):
            profile = response["activePermissionProfile"]
            if not isinstance(profile, dict) or not isinstance(profile.get("id"), str) or not profile["id"]:
                raise RPCError("Codex вернул неизвестный профиль доступа")
            result["activePermissionProfile"] = {"id": profile["id"]}
        return result

    async def read_thread(self, thread_id: str, project: Path | None = None) -> dict:
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
        if thread.get("parentThreadId"):
            raise RPCError("Дочерние разговоры обслуживает их основная сессия")
        if raw["thread"].get("canAcceptDirectInput") is False:
            raise RPCError("Эта сессия Codex не принимает прямые задачи")
        self.threads[thread_id] = thread
        return thread

    async def inspect(self, thread_id: str, project: Path | None = None) -> dict:
        thread = await self.read_thread(thread_id, project)
        # systemError описывает сбой запроса в загруженном разговоре, а не его закрытие.
        if thread["status"]["type"] == "notLoaded":
            raise RPCError("Сессия не открыта в Codex. Мост не запускает закрытые сессии")
        return thread

    async def attach(self, thread_id: str, project: Path) -> dict:
        await self.inspect(thread_id, project)
        return await self.resume_thread(thread_id, project)

    async def resume_thread(self, thread_id: str, project: Path) -> dict:
        await self.read_thread(thread_id, project)
        result = await self.typed(
            "thread/resume",
            schemas.ThreadResumeParams,
            schemas.ThreadResumeResponse,
            {"threadId": thread_id, "excludeTurns": True},
        )
        expected_project = await asyncio.to_thread(project.resolve)
        if result["thread"]["id"] != thread_id or result["thread"]["cwd"] != str(expected_project):
            raise RPCError("App-server подписал мост на другую сессию или проект")
        if result["thread"]["status"]["type"] == "notLoaded":
            raise RPCError("Codex не загрузил эту сессию; восстановите её из архива или повторите позже")
        self.approvals = {
            "policy": result["approvalPolicy"],
            "reviewer": result["approvalsReviewer"],
            "manual": result["approvalsReviewer"] == "user" and result["approvalPolicy"] != "never",
        }
        self.configurations[thread_id] = result
        return result["thread"]

    async def list_threads(self, project: Path, *, archived: bool = False, cursor: str | None = None) -> dict:
        return await self.typed(
            "thread/list",
            schemas.ThreadListParams,
            schemas.ThreadListResponse,
            {
                "cwd": str(project),
                "archived": archived,
                "cursor": cursor,
                "limit": 50,
                "sortKey": "updated_at",
                "sourceKinds": ["cli", "vscode", "appServer"],
            },
        )

    async def new_thread(self, project: Path, template: dict) -> dict:
        config = {}
        params = {"cwd": str(project), "serviceName": "bax_codex_light", "historyMode": "legacy"}
        if template:
            for field in ("model", "modelProvider", "approvalPolicy", "approvalsReviewer", "serviceTier"):
                if template.get(field) is not None:
                    params[field] = template[field]
            if template.get("reasoningEffort") is not None:
                config["model_reasoning_effort"] = template["reasoningEffort"]
            profile = template.get("activePermissionProfile")
            if profile:
                config["default_permissions"] = profile["id"]
            else:
                sandbox = template["sandbox"]
                modes = {
                    "readOnly": "read-only",
                    "workspaceWrite": "workspace-write",
                    "dangerFullAccess": "danger-full-access",
                }
                if sandbox["type"] not in modes:
                    raise RPCError("Этот профиль доступа нельзя перенести: создайте разговор в Codex")
                params["sandbox"] = modes[sandbox["type"]]
                if sandbox["type"] == "workspaceWrite":
                    config["sandbox_workspace_write"] = {
                        "writable_roots": sandbox.get("writableRoots", []),
                        "network_access": sandbox.get("networkAccess", False),
                        "exclude_tmpdir_env_var": sandbox.get("excludeTmpdirEnvVar", False),
                        "exclude_slash_tmp": sandbox.get("excludeSlashTmp", False),
                    }
        if config:
            params["config"] = config
        result = await self.typed(
            "thread/start", ThreadStartWithHistoryParams, schemas.ThreadStartResponse, params
        )
        thread = result["thread"]
        thread_project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
        expected_project = await asyncio.to_thread(project.resolve)
        if thread_project != expected_project:
            raise RPCError("Codex создал разговор в другом проекте")
        if thread.get("historyMode") != "legacy":
            raise RPCError("Codex не сохранил поддерживаемый формат истории; новый разговор не выбран")
        self.configurations[thread["id"]] = result
        # Новый разговор ещё не получает задач, если сервер изменил выбранные настройки.
        for field in (
            "model",
            "modelProvider",
            "approvalPolicy",
            "approvalsReviewer",
            "reasoningEffort",
            "sandbox",
            "activePermissionProfile",
            "serviceTier",
        ):
            if field in template and result.get(field) != template[field]:
                raise RPCError(
                    f"Codex не сохранил настройку {field}; новый разговор {thread['id']} не выбран"
                )
        # thread/start подписывает нас на живой разговор, но его история появляется
        # на диске отложенно. Штатное имя сохраняет пустую сессию без задания модели:
        # после подтверждения доступны resume и пагинация истории.
        await self.typed(
            "thread/name/set",
            schemas.ThreadSetNameParams,
            schemas.ThreadSetNameResponse,
            {"threadId": thread["id"], "name": EMPTY_THREAD_NAME},
        )
        return thread

    async def archive_thread(self, thread_id: str) -> None:
        await self.typed(
            "thread/archive",
            schemas.ThreadArchiveParams,
            schemas.ThreadArchiveResponse,
            {"threadId": thread_id},
        )

    async def rename_thread(self, thread_id: str, project: Path, name: str) -> None:
        await self.read_thread(thread_id, project)
        await self.typed(
            "thread/name/set",
            schemas.ThreadSetNameParams,
            schemas.ThreadSetNameResponse,
            {"threadId": thread_id, "name": name},
        )
        self.threads[thread_id]["name"] = name

    async def context_usage(self, thread_id: str) -> dict:
        await self.response_metadata(thread_id, [])
        value = self.metadata.get(thread_id)
        return dict(value.token_usage) if value else {}

    async def background_terminals(self, thread_id: str, project: Path) -> list[dict]:
        thread = await self.read_thread(thread_id, project)
        if thread["status"]["type"] == "notLoaded":
            return []
        items, cursor = [], None
        for _ in range(20):
            page = await self.typed(
                "thread/backgroundTerminals/list",
                background.ListParams,
                background.ListResponse,
                {"threadId": thread_id, "limit": 100, "cursor": cursor},
            )
            items.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return items
        raise RPCError("Список фоновых процессов слишком большой")

    async def terminate_background(self, thread_id: str, project: Path, process_id: str) -> bool:
        tasks = await self.background_terminals(thread_id, project)
        if not any(item["processId"] == process_id for item in tasks):
            raise ValueError("Этот фоновый процесс уже завершился")
        result = await self.typed(
            "thread/backgroundTerminals/terminate",
            background.TerminateParams,
            background.TerminateResponse,
            {"threadId": thread_id, "processId": process_id},
        )
        return result["terminated"]

    async def unarchive_thread(self, thread_id: str, project: Path) -> dict:
        await self.read_thread(thread_id, project)
        result = await self.typed(
            "thread/unarchive",
            schemas.ThreadUnarchiveParams,
            schemas.ThreadUnarchiveResponse,
            {"threadId": thread_id},
        )
        thread_project = await asyncio.to_thread(Path(result["thread"]["cwd"]).resolve)
        expected_project = await asyncio.to_thread(project.resolve)
        if result["thread"]["id"] != thread_id or thread_project != expected_project:
            raise RPCError("Codex восстановил другой разговор")
        return result["thread"]

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.typed(
            "turn/interrupt",
            schemas.TurnInterruptParams,
            schemas.TurnInterruptResponse,
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def items(self, thread_id: str, cursor: str | None, limit: int) -> dict:
        if thread_id not in self.turn_history:
            try:
                return await self.typed(
                    "thread/items/list",
                    schemas.ThreadItemsListParams,
                    schemas.ThreadItemsListResponse,
                    {"threadId": thread_id, "cursor": cursor, "limit": limit, "sortDirection": "desc"},
                )
            except RPCRejected as error:
                if str(error) != "thread/items/list is not supported yet":
                    raise
                self.turn_history.add(thread_id)
        # Legacy store не поддерживает item pagination. Один ход на запрос
        # ограничивает размер ответа; курсор остаётся штатным курсором Codex.
        page = await self.typed(
            "thread/turns/list",
            schemas.ThreadTurnsListParams,
            schemas.ThreadTurnsListResponse,
            {
                "threadId": thread_id,
                "cursor": cursor,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "full",
            },
        )
        return schemas.ThreadItemsListResponse.model_validate(
            {
                "data": [
                    {"turnId": turn["id"], "item": item}
                    for turn in page["data"]
                    for item in reversed(turn["items"])
                ],
                "nextCursor": page.get("nextCursor"),
            }
        ).model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)

    async def active_turn(self, thread_id: str) -> str:
        result = await self.typed(
            "thread/turns/list",
            schemas.ThreadTurnsListParams,
            schemas.ThreadTurnsListResponse,
            {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
        )
        turn = next(iter(result["data"]), None)
        return turn["id"] if turn and turn["status"] == "inProgress" else ""

    async def last_turn_error(self, thread_id: str) -> str:
        result = await self.typed(
            "thread/turns/list",
            schemas.ThreadTurnsListParams,
            schemas.ThreadTurnsListResponse,
            {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
        )
        turn = next(iter(result["data"]), None)
        return (turn.get("error") or {}).get("message", "") if turn else ""

    async def model_catalog(self) -> list[dict]:
        items, cursor = [], None
        for _ in range(20):
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            page = await self.typed("model/list", schemas.ModelListParams, schemas.ModelListResponse, params)
            items.extend(item for item in page["data"] if not item["hidden"])
            cursor = page.get("nextCursor")
            if not cursor:
                return items
        raise RPCError("Каталог моделей слишком большой; повторите запрос позже")

    async def response_metadata(self, thread_id: str, entries: list[dict]) -> dict:
        async with self.metadata_lock:
            thread = self.threads.get(thread_id)
            if not thread:
                return {}
            home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            value = self.metadata.get(thread_id)
            try:
                source = await asyncio.to_thread(Path(thread["path"]).resolve) if thread.get("path") else None
                if not value or value.path != source:
                    value = self.metadata[thread_id] = await asyncio.to_thread(ResponseMetadata, thread, home)
                return await asyncio.to_thread(value.decorate, entries)
            except (OSError, ValueError):
                # Отсутствующий журнал не мешает истории; текущая модель не служит заменой.
                return {}

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        client_id: str,
        *,
        images: list[dict] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        return await self.typed(
            "turn/start",
            schemas.TurnStartParams,
            schemas.TurnStartResponse,
            {
                "threadId": thread_id,
                "clientUserMessageId": client_id,
                "input": ([{"type": "text", "text": text}] if text else []) + (images or []),
                **({"model": model, "effort": effort} if model is not None else {}),
            },
        )

    async def steer_turn(
        self, thread_id: str, turn_id: str, text: str, client_id: str, *, images: list[dict] | None = None
    ) -> dict:
        return await self.typed(
            "turn/steer",
            schemas.TurnSteerParams,
            schemas.TurnSteerResponse,
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": client_id,
                "input": ([{"type": "text", "text": text}] if text else []) + (images or []),
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
