"""Один MCP-процесс, один существующий разговор Codex, одна регистрация Бакса."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import __version__, files
from .approvals import permission_details, permission_profile, remember_approval
from .appserver import AppServer, RPCError, RPCRejected
from .connection import Relay, network_error
from .history import History, identity, render
from .preferences import Preferences
from .registry import Registration, Registry

log = logging.getLogger(__name__)
SUPPORTS = ["subscribe", "run", "answer", "history", "files.list", "files.read", "power.get", "power.set"]


@dataclass
class Submission:
    text: str
    error: str = ""


class Bridge:
    def __init__(
        self, project: Path | None, registry: Registry, thread_id: str = "", endpoint: str | None = None
    ):
        self.project = project.resolve() if project else None
        self.registry = registry
        self.preferences = Preferences(registry.path.with_name(f"{registry.path.stem}-settings.json"))
        self.power_error = ""
        self.thread_id = thread_id
        self.endpoint = endpoint
        self.app: AppServer | None = None
        self.relay: Relay | None = None
        self.history: History | None = None
        self.state = "offline"
        self.error = ""
        self.error_code = ""
        self.project_ready = asyncio.Event()
        self.questions: dict[str, dict] = {}
        self.requests: dict[int | str, dict] = {}
        self.queue: deque[tuple[str, str]] = deque(maxlen=10)
        # До появления настоящей userMessage в Codex эхо не является историей.
        self.outbox: dict[str, Submission] = {}
        self.turn_id = ""
        self.streamed: set[str] = set()
        self.seen_async_questions: set[str] = set()
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def status(self) -> dict:
        error = self.error or (self.relay.error if self.relay else "")
        error_code = self.error_code or (self.relay.error_code if self.relay else "")
        needs_registration = False
        try:
            needs_registration = bool(self.project and not self.registry.get(self.project))
        except (OSError, ValueError, TypeError):
            error = (
                "Не удалось прочитать сохранённую регистрацию Бакса. Проверьте доступ к файлу регистрации."
            )
            error_code = "registration_unavailable"
        return {
            "plugin_version": __version__,
            "project": str(self.project) if self.project else None,
            "thread_id": self.thread_id or None,
            "state": self.state,
            "connected": bool(self.relay and self.relay.connected),
            "queued": len(self.queue),
            "unconfirmed": len(self.outbox),
            "delivery_errors": sum(bool(item.error) for item in self.outbox.values()),
            "pending_questions": len(self.questions),
            "error": error,
            "error_code": error_code,
            "keep_awake": self.power_settings() if self.relay else None,
            "relay_connection": {
                "last_connected_at": self.relay.last_connected_at,
                "last_disconnected_at": self.relay.last_disconnected_at,
                "last_close_code": self.relay.last_close_code,
                "last_close_reason": self.relay.last_close_reason,
                "next_retry_seconds": self.relay.retry_delay,
            }
            if self.relay
            else None,
            "approvals": self.app.approvals if self.app else None,
            "needs_thread": not bool(self.thread_id),
            "needs_registration": needs_registration,
        }

    async def start(self) -> None:
        # MCP и его инструменты запускаются сразу; сокет Codex проверяется в фоне.
        if self.task is not None and self.task.done():
            self.task = None
        if self.thread_id and self.task is None:
            self.task = asyncio.create_task(self._run(), name="bax-bridge")

    async def bind(self, thread_id: str) -> dict:
        async with self.lock:
            if not thread_id.strip():
                raise ValueError("Нужен точный CODEX_THREAD_ID")
            if self.thread_id:
                if thread_id != self.thread_id:
                    raise ValueError("Мост уже привязан к другому разговору; перезапустите MCP")
                return self.status()
            self.thread_id = thread_id
            await self.start()
            # У attach можно кратко дождаться метаданных, не задерживая запуск MCP.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.project_ready.wait(), 2)
            return self.status()

    async def connect(self, key: str, server: str = "wss://relay.baxassist.com/agent") -> dict:
        async with self.lock:
            if not self.thread_id or self.project is None:
                detail = self.error or "Сначала вызовите bax_attach с точным CODEX_THREAD_ID этого разговора"
                raise ValueError(f"Папка разговора ещё не подтверждена. {detail}")
            registration = Registration.from_key(key, server)
            previous = self.registry.get(self.project)
            if previous and previous.agent != registration.agent:
                raise ValueError("Проект подключён к другому агенту. Существующая регистрация сохранена")
            changed = previous and (
                previous.key_id != registration.key_id
                or previous.secret != registration.secret
                or previous.server != registration.server
            )
            self.registry.put(self.project, registration)
            if changed and self.task is not None:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            if self.task is not None and self.task.done():
                self.task = None
            await self.start()
            return self.status()

    async def _run(self) -> None:
        attempt = 0
        while True:
            relay_task = None
            try:
                self.app = AppServer(self.endpoint)
                await self.app.open(self.on_event)
                thread = await self.app.inspect(self.thread_id, self.project)
                self.project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
                registration = self.registry.get(self.project)
                if not registration:
                    self.error = "Нет регистрации. Вставьте команду из Бакса и вызовите bax_connect"
                    self.error_code = "registration_missing"
                    return
                thread = await self.app.attach(self.thread_id, self.project)
                self._set_state(thread["status"])
                self.history = self.history or History(self.app, self.thread_id)
                self.history.app = self.app
                keep_awake = True
                try:
                    keep_awake = await asyncio.to_thread(
                        self.preferences.get, self.project, registration.agent
                    )
                    self.power_error = ""
                except (OSError, ValueError, TypeError):
                    self.power_error = (
                        "Не удалось прочитать настройки агента. Защита от сна включена по умолчанию. "
                        f"Проверьте файл {self.preferences.path}."
                    )
                self.relay = Relay(registration, self.project, self.thread_id, keep_awake=keep_awake)
                self.error = ""
                self.error_code = ""
                relay_task = asyncio.create_task(self.relay.run(self.on_ready, self.on_frame))
                self.project_ready.set()
                await self.app.closed.wait()
                raise RPCError("Сессия Codex отключилась")
            except Exception as error:
                self.state = "offline"
                if isinstance(error, PermissionError):
                    self.error_code = "codex_socket_permission_denied"
                    self.error = (
                        f"ОС запретила доступ к локальному сокету Codex: {self.app.endpoint}. "
                        "Запускайте MCP штатно из Codex. "
                        "Ручному запуску в песочнице нужно разрешение доступа."
                    )
                elif isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
                    self.error_code = "codex_unavailable"
                    self.error = (
                        f"Локальный сервер Codex недоступен: {self.app.endpoint}. "
                        "Откройте Codex или перезапустите его; плагин повторит подключение."
                    )
                elif isinstance(error, RPCError):
                    self.error_code = "codex_rpc"
                    self.error = str(error)
                else:
                    self.error_code, self.error = network_error(error, "локальному серверу Codex")
                self.project_ready.set()
                log.warning("%s (код: %s)", self.error, self.error_code)
            finally:
                self.questions.clear()
                self.requests.clear()
                if relay_task:
                    relay_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await relay_task
                if self.relay:
                    await self.relay.close()
                if self.app:
                    await self.app.close()
                self.project_ready.set()
            await asyncio.sleep(min(2**attempt, 30))
            attempt = min(attempt + 1, 5)

    def _set_state(self, status: dict) -> None:
        kind = status.get("type")
        if kind == "active":
            flags = status.get("activeFlags", [])
            self.state = (
                "waiting" if any(f in flags for f in ["waitingOnApproval", "waitingOnUserInput"]) else "busy"
            )
        elif kind == "idle":
            self.state = "ready"
        else:
            self.state = "offline"

    async def send(self, frame_type: str, **fields) -> bool:
        if self.relay:
            return await self.relay.send(frame_type, session=self.thread_id, **fields)
        return False

    async def on_ready(self) -> None:
        await self.send("caps", mode="lite", supports=SUPPORTS, remember=True)
        await self.send("power.settings", **self.power_settings())
        await self.send("status", state=self.state)
        await self._drain()

    def power_settings(self) -> dict:
        if not self.relay:
            return {"enabled": True, "supported": False, "active": False, "error": self.power_error}
        return {
            **self.relay.sleep_guard.status(),
            "enabled": self.relay.keep_awake_enabled,
            "error": self.power_error or self.relay.sleep_guard.error,
        }

    async def set_power(self, frame: dict) -> None:
        error = ""
        try:
            enabled = frame.get("keep_awake")
            if type(enabled) is not bool:
                raise ValueError("Для защиты от сна нужен переключатель true или false")
            if not self.relay or not self.project:
                raise ValueError("Агент ещё не подключён; повторите после подключения")
            agent = self.relay.registration.agent
            if frame.get("agent", agent) != agent:
                raise ValueError("Настройки относятся к другому агенту")
            await asyncio.to_thread(self.preferences.put, self.project, agent, enabled)
            self.power_error = ""
            await self.relay.set_keep_awake(enabled)
        except (OSError, ValueError, TypeError) as failure:
            error = (
                str(failure)
                if isinstance(failure, ValueError)
                else (
                    "Не удалось сохранить настройку защиты от сна. "
                    f"Проверьте доступ к {self.preferences.path}. "
                    "Предыдущий выбор сохранён."
                )
            )
        settings = self.power_settings()
        await self.send(
            "power.settings", **{**settings, "error": error or settings["error"]}, rid=frame.get("rid")
        )

    async def send_history(self, before: int | None = None, limit: int = 50) -> None:
        if not self.history:
            raise RPCError("Codex ещё не подключён")
        rows = await self.history.page(before, limit)
        for client_id in list(self.outbox):
            if client_id in self.history.recorded:
                self.outbox.pop(client_id)
        if before is None:
            for client_id, submission in self.outbox.items():
                rows.append(
                    {
                        "id": self.history.preview_id(client_id),
                        "kind": "user",
                        "text": submission.text,
                    }
                )
                if submission.error:
                    rows.append(
                        {
                            "id": self.history.preview_id(client_id) + 1,
                            "kind": "error",
                            "text": submission.error,
                        }
                    )
        for row in sorted(rows, key=lambda row: row["id"]):
            await self.send("message", **row)

    async def on_frame(self, frame: dict) -> None:
        try:
            kind = frame.get("type")
            if kind == "subscribe":
                await self.on_ready()
                await self.send_history()
                await self.show_question()
                await self.send("background", tasks=[])
                await self.send("status", state=self.state)
            elif kind == "history":
                await self.send_history(
                    int(frame.get("before", 0)), max(1, min(int(frame.get("limit", 50)), 100))
                )
            elif kind == "run":
                if frame.get("attachments"):
                    raise ValueError("В этой версии принимаются только текстовые задачи")
                text = frame.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
                    raise ValueError("Нужна непустая текстовая задача до 100000 символов")
                if len(self.queue) == self.queue.maxlen:
                    raise ValueError("Очередь заполнена; дождитесь выполнения задач")
                if not self.history or not self.app or self.app.closed.is_set():
                    raise RPCError("Сессия Codex недоступна")
                client_id = str(uuid4())
                self.queue.append((client_id, text))
                self.outbox[client_id] = Submission(text)
                # Эхо подтверждает приём в очередь для таймера доставки на телефоне.
                await self.send("message", id=self.history.preview_id(client_id), kind="user", text=text)
                await self._drain()
            elif kind == "answer":
                await self.answer(frame)
            elif kind == "power.get":
                await self.send("power.settings", **self.power_settings(), rid=frame.get("rid"))
            elif kind == "power.set":
                await self.set_power(frame)
            elif kind == "files.list":
                paths = await files.tracked(self.project)
                await self.send(
                    "files", paths=paths[: files.MAX_PATHS], truncated=len(paths) > files.MAX_PATHS
                )
            elif kind == "files.read":
                path = str(frame.get("path", ""))
                paths = await files.tracked(self.project)
                result = await asyncio.to_thread(files.read, self.project, path, paths)
                await self.send("file.text", path=path, **result)
            else:
                await self.send("error", code="unsupported", message=f"Команда {kind!r} не поддерживается")
        except (ValueError, RPCError, TimeoutError) as error:
            await self.send("error", code="invalid_request", message=str(error))

    async def _drain(self) -> None:
        async with self.lock:
            while self.queue and self.app:
                thread = await self.app.inspect(self.thread_id, self.project)
                self._set_state(thread["status"])
                if self.state != "ready":
                    await self.send("status", state=self.state)
                    return
                # Убираем до отправки: при обрыве повторять уже доставленную задачу нельзя.
                client_id, text = self.queue.popleft()
                self.state = "busy"
                await self.send("status", state=self.state)
                try:
                    result = await self.app.start_turn(self.thread_id, text, client_id)
                    self.turn_id = result["turn"]["id"]
                    log.info("Codex: сообщение %s принято, ход %s", client_id, self.turn_id)
                    return
                except Exception as error:
                    # Повтор требует решения человека; предварительное эхо остаётся
                    # видимым до подтверждения настоящей историей Codex.
                    message = (
                        f"Codex отклонил сообщение: {error}"
                        if isinstance(error, RPCRejected)
                        else "Доставка задачи не подтверждена. Проверьте разговор перед повторной отправкой"
                    )
                    if submission := self.outbox.get(client_id):
                        submission.error = message
                    log.warning("Codex: доставка сообщения %s — %s", client_id, type(error).__name__)
                    await self.send(
                        "error",
                        code="delivery_rejected" if isinstance(error, RPCRejected) else "delivery_uncertain",
                        message=message,
                    )
                    if isinstance(error, RPCRejected):
                        with contextlib.suppress(Exception):
                            thread = await self.app.inspect(self.thread_id, self.project)
                            self._set_state(thread["status"])
                            await self.send("status", state=self.state)

    async def on_event(self, event: dict) -> None:
        params = event.get("params", {})
        if params.get("threadId") != self.thread_id:
            return
        method = event["method"]
        if "id" in event:
            await self.question(event)
            return
        if method in {"thread/archived", "thread/closed"}:
            self.state = "offline"
            self.error = "Этот разговор Codex закрыт. Подключите новый разговор его точным CODEX_THREAD_ID."
            self.error_code = "codex_thread_closed"
            if self.task:
                self.task.cancel()
            return
        if method == "thread/status/changed":
            self._set_state(params["status"])
            await self.send("status", state=self.state)
            if self.state == "offline" and self.app:
                await self.app.close()
            elif self.state == "ready":
                await self._drain()
        elif method == "turn/started":
            self.turn_id = params["turn"]["id"]
            self.state = "busy"
            await self.send("status", state=self.state)
        elif method == "turn/completed":
            self.turn_id = ""
            # Асинхронный вопрос остаётся доступным и после завершения хода.
            for request_id, request in list(self.requests.items()):
                if request["method"] != "agentMessage/asyncQuestion":
                    self.requests.pop(request_id)
                    for qid in request["question_ids"]:
                        self.questions.pop(qid, None)
                        await self.send("question.resolved", question_id=qid)
            await self.send("done", id=self.history.high if self.history else 0)
        elif method in {"item/started", "item/completed"} and self.history:
            item = params["item"]
            if item.get("type") == "agentMessage" and item.get("delivery") == "async":
                await self.async_questions(item, params["turnId"])
            view = render(item)
            if view and view[1]:
                item_id = identity(item)
                if item.get("type") == "userMessage":
                    self.history.recorded.add(item_id)
                    self.outbox.pop(item_id, None)
                entry_id = self.history.live_id(item_id)
                # Готовый ответ заменяет потоковую строку с тем же ID.
                await self.send("message", id=entry_id, kind=view[0], text=view[1])
                self.streamed.add(item["id"])
        elif method == "item/agentMessage/delta" and self.history:
            item_id = params["itemId"]
            entry_id = self.history.live_id(item_id)
            self.streamed.add(item_id)
            await self.send("delta", id=entry_id, chunk=params["delta"])
        elif method == "serverRequest/resolved":
            request_id = params.get("requestId")
            resolved = [qid for qid, value in self.questions.items() if value["request_id"] == request_id]
            self.requests.pop(request_id, None)
            self.questions = {
                qid: value for qid, value in self.questions.items() if value["request_id"] != request_id
            }
            for qid in resolved:
                await self.send("question.resolved", question_id=qid)
            await self.show_question()

    async def question(self, event: dict) -> None:
        method, params, request_id = event["method"], event["params"], event["id"]
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
        }:
            return  # Другие виды разрешений остаются в локальном клиенте Codex.
        if not all(
            isinstance(params.get(key), str) and params[key] for key in ("threadId", "turnId", "itemId")
        ):
            return
        if self.turn_id and params["turnId"] != self.turn_id:
            return
        if request_id in self.requests:
            return
        permissions = None
        if method == "item/permissions/requestApproval":
            try:
                if not isinstance(params.get("cwd"), str) or not Path(params["cwd"]).is_absolute():
                    raise ValueError("Рабочий каталог должен быть абсолютным")
                permissions = permission_profile(params.get("permissions"))
            except ValueError:
                log.warning("Codex запросил доступ в неподдерживаемом формате; запрос остаётся в Codex")
                await self.send(
                    "error",
                    code="unsupported_permissions",
                    message="Не удалось прочитать запрошенный доступ. Подтвердите этот запрос в самом Codex.",
                )
                return
        self.requests[request_id] = {
            "method": method,
            "turn_id": params["turnId"],
            "answers": {},
            "question_ids": [],
        }
        remembered = remember_approval(method, params, permissions)
        self.requests[request_id]["remembered"] = remembered
        cards = []
        if method == "item/tool/requestUserInput":
            questions = params.get("questions", [])
            if not questions or any(q.get("isSecret") for q in questions):
                self.requests.pop(request_id, None)
                return
            for question in questions:
                options = [option["label"] for option in question.get("options") or []]
                cards.append(
                    (
                        {
                            "kind": "choice",
                            "tool": "request_user_input",
                            "text": question["question"],
                            "options": options,
                            "allow_custom_answer": not options or question.get("isOther") is True,
                            "input": {},
                            "rule": "",
                        },
                        question["id"],
                    )
                )
        elif method == "item/permissions/requestApproval":
            self.requests[request_id]["permissions"] = permissions
            details = permission_details(permissions)
            details["Рабочая папка"] = params["cwd"]
            text = params.get("reason") or "Codex запрашивает дополнительный доступ к сети или файлам"
            cards.append(
                (
                    {
                        "kind": "permission",
                        "tool": "Доступ к сети и файлам",
                        "text": f"{text}\n«Разрешить» — до окончания текущего хода.",
                        "input": details,
                        "options": [],
                        "rule": "",
                    },
                    None,
                )
            )
        else:
            tool = "command" if "commandExecution" in method else "file_change"
            text = params.get("reason") or "Codex запрашивает разрешение на действие"
            details = {key: str(params[key]) for key in ("command", "cwd", "grantRoot") if params.get(key)}
            if isinstance(params.get("networkApprovalContext"), dict):
                network = params["networkApprovalContext"]
                tool = "Доступ к сети"
                details = {
                    label: str(network[key])
                    for key, label in (("host", "Адрес"), ("protocol", "Протокол"))
                    if network.get(key)
                }
            cards.append(
                (
                    {
                        "kind": "permission",
                        "tool": tool,
                        "text": text,
                        "input": details,
                        "options": [],
                        "rule": "",
                    },
                    None,
                )
            )
        for card, field_id in cards:
            if card["kind"] == "permission":
                card["remember"] = remembered is not None
                card["remember_label"] = remembered.label if remembered else ""
                card["rule"] = remembered.rule if remembered else ""
                if remembered:
                    card["text"] += f"\n«{remembered.label}»: {remembered.rule}."
            qid = str(uuid4())
            card["question_id"] = qid
            self.requests[request_id]["question_ids"].append(qid)
            self.questions[qid] = {"request_id": request_id, "field_id": field_id, "card": card}
        await self.show_question()

    async def async_questions(self, item: dict, turn_id: str) -> None:
        if item["id"] in self.seen_async_questions:
            return
        questions = item.get("questions") or []
        if not questions:
            return
        self.seen_async_questions.add(item["id"])
        request_id = f"async:{item['id']}"
        self.requests[request_id] = {
            "method": "agentMessage/asyncQuestion",
            "turn_id": turn_id,
            "answers": {},
            "question_ids": [],
            "titles": {},
        }
        for index, question in enumerate(questions):
            qid = str(uuid4())
            field_id = str(index)
            self.requests[request_id]["question_ids"].append(qid)
            self.requests[request_id]["titles"][field_id] = question["title"]
            self.questions[qid] = {
                "request_id": request_id,
                "field_id": field_id,
                "card": {
                    "question_id": qid,
                    "kind": "choice",
                    "tool": "request_user_input_async",
                    "text": question["title"],
                    "options": question.get("options") or [],
                    "allow_custom_answer": True,
                    "input": {},
                    "rule": "",
                },
            }
        await self.show_question()

    async def show_question(self) -> None:
        # Нынешний UI Бакса показывает одну карточку; вопросы передаём по очереди.
        if self.questions:
            question = next(iter(self.questions.values()))
            await self.send("question", **question["card"])

    async def answer(self, frame: dict) -> None:
        qid = frame.get("question_id")
        question = self.questions.get(qid)
        if question is None:
            raise ValueError("Вопрос уже решён или относится к другой сессии")
        request_id = question["request_id"]
        request = self.requests[request_id]
        remember = frame.get("remember", False)
        if not isinstance(remember, bool):
            raise ValueError("Нужен логический признак сохранения разрешения")
        if remember and (frame.get("verdict") != "allow" or not request.get("remembered")):
            raise ValueError("Codex не предложил сохранение разрешения для этого запроса")
        if (
            request["method"] != "agentMessage/asyncQuestion"
            and self.turn_id
            and request["turn_id"] != self.turn_id
        ):
            raise ValueError("Этот запрос относится к уже завершённому ходу")
        if request["method"] == "agentMessage/asyncQuestion":
            if len(self.queue) == self.queue.maxlen:
                raise ValueError("Очередь заполнена; дождитесь выполнения задач")
            if not self.app or self.app.closed.is_set():
                raise RPCError("Сессия Codex недоступна; вопрос сохранён для повторной подписки")
        if request["method"] in {"item/tool/requestUserInput", "agentMessage/asyncQuestion"}:
            option = frame.get("option")
            if frame.get("verdict") != "choice" or not isinstance(option, str) or not option.strip():
                raise ValueError("Нужен текст ответа на вопрос")
            if not question["card"]["allow_custom_answer"] and option not in question["card"]["options"]:
                raise ValueError("Выберите один из предложенных вариантов")
            request["answers"][question["field_id"]] = {"answers": [option]}
            self.questions.pop(qid)
            if any(other in self.questions for other in request["question_ids"]):
                await self.show_question()
                return
            result = {"answers": request["answers"]}
        else:
            verdict = frame.get("verdict")
            if verdict not in {"allow", "deny"}:
                raise ValueError("Нужен verdict allow или deny")
            if remember:
                result = request["remembered"].response
            elif request["method"] == "item/permissions/requestApproval":
                result = {
                    "permissions": request["permissions"] if verdict == "allow" else {},
                    "scope": "turn",
                }
            else:
                result = {"decision": "accept" if verdict == "allow" else "decline"}
        if not self.app:
            raise RPCError("Codex не подключён")
        if request["method"] == "agentMessage/asyncQuestion":
            await self.submit_async_answer(request)
        else:
            await self.app.respond(request_id, result)
        self.requests.pop(request_id, None)
        for other in request["question_ids"]:
            self.questions.pop(other, None)
        await self.show_question()

    async def submit_async_answer(self, request: dict) -> None:
        text = "\n\n".join(
            f"{request['titles'][field_id]}\nОтвет: {answer['answers'][0]}"
            for field_id, answer in request["answers"].items()
        )
        if len(self.queue) == self.queue.maxlen:
            raise ValueError("Очередь заполнена; дождитесь выполнения задач")
        client_id = str(uuid4())
        self.outbox[client_id] = Submission(text)
        await self.send("message", id=self.history.preview_id(client_id), kind="user", text=text)
        if self.state in {"busy", "waiting"}:
            try:
                # Ответ идёт в тот ход, который задал вопрос. Другой ход не затрагиваем.
                await self.app.steer_turn(self.thread_id, request["turn_id"], text, client_id)
                return
            except RPCRejected:
                pass  # Ход уже завершился: обычная очередь того же разговора.
            except Exception:
                message = "Доставка ответа не подтверждена. Проверьте разговор перед повторной отправкой."
                self.outbox[client_id].error = message
                await self.send("error", code="delivery_uncertain", message=message)
                return
        self.queue.append((client_id, text))
        await self._drain()

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.relay:
            await self.relay.close()
        if self.app:
            await self.app.close()
