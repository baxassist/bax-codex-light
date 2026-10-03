"""Один MCP-процесс, один существующий разговор Codex, одна регистрация Бакса."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import files
from .appserver import AppServer, RPCError, RPCRejected
from .connection import Relay
from .history import History, identity, render
from .registry import Registration, Registry

log = logging.getLogger(__name__)
SUPPORTS = ["subscribe", "run", "answer", "history", "files.list", "files.read"]


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
        self.thread_id = thread_id
        self.endpoint = endpoint
        self.app: AppServer | None = None
        self.relay: Relay | None = None
        self.history: History | None = None
        self.state = "offline"
        self.error = ""
        self.questions: dict[str, dict] = {}
        self.requests: dict[int | str, dict] = {}
        self.queue: deque[tuple[str, str]] = deque(maxlen=10)
        # До появления настоящей userMessage в Codex эхо не является историей.
        self.outbox: dict[str, Submission] = {}
        self.turn_id = ""
        self.streamed: set[str] = set()
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def status(self) -> dict:
        return {
            "project": str(self.project) if self.project else None,
            "thread_id": self.thread_id or None,
            "state": self.state,
            "connected": bool(self.relay and self.relay.connected),
            "queued": len(self.queue),
            "unconfirmed": len(self.outbox),
            "delivery_errors": sum(bool(item.error) for item in self.outbox.values()),
            "pending_questions": len(self.questions),
            "error": self.error or (self.relay.error if self.relay else ""),
            "needs_thread": not bool(self.thread_id),
            "needs_registration": bool(self.project and not self.registry.get(self.project)),
        }

    async def start(self) -> None:
        if self.thread_id and self.project is None:
            probe = AppServer(self.endpoint)
            try:
                await probe.open()
                thread = await probe.inspect(self.thread_id)
                self.project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
            finally:
                await probe.close()
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
            probe = AppServer(self.endpoint)
            try:
                await probe.open()
                thread = await probe.inspect(thread_id, self.project)
            finally:
                await probe.close()
            self.thread_id = thread_id
            self.project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
            await self.start()
            return self.status()

    async def connect(self, key: str, server: str = "wss://relay.baxassist.com/agent") -> dict:
        async with self.lock:
            if not self.thread_id or self.project is None:
                raise ValueError("Сначала вызовите bax_attach с точным CODEX_THREAD_ID этого разговора")
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
                registration = self.registry.get(self.project)
                if not registration:
                    self.error = "Нет регистрации. Вставьте команду из Бакса и вызовите bax_connect"
                    return
                self.app = AppServer(self.endpoint)
                await self.app.open(self.on_event)
                thread = await self.app.attach(self.thread_id, self.project)
                self._set_state(thread["status"])
                self.history = self.history or History(self.app, self.thread_id)
                self.history.app = self.app
                self.relay = Relay(registration, self.project, self.thread_id)
                self.error = ""
                relay_task = asyncio.create_task(self.relay.run(self.on_ready, self.on_frame))
                await self.app.closed.wait()
                raise RPCError("Сессия Codex отключилась")
            except Exception as error:
                self.state = "offline"
                self.error = str(error)
                log.warning("Codex: %s", type(error).__name__)
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
        await self.send("caps", mode="lite", supports=SUPPORTS, remember=False)
        await self.send("status", state=self.state)
        await self._drain()

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
            self.questions.clear()
            self.requests.clear()
            await self.send("done", id=self.history.high if self.history else 0)
        elif method in {"item/started", "item/completed"} and self.history:
            item = params["item"]
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
            "item/tool/requestUserInput",
        }:
            return  # Другие виды разрешений остаются в локальном клиенте Codex.
        if not all(isinstance(params.get(key), str) for key in ("threadId", "turnId", "itemId")):
            return
        if request_id in self.requests:
            return
        self.requests[request_id] = {"method": method, "answers": {}, "question_ids": []}
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
                            "input": {},
                            "rule": "",
                        },
                        question["id"],
                    )
                )
        else:
            tool = "command" if "commandExecution" in method else "file_change"
            text = params.get("reason") or "Codex запрашивает разрешение на действие"
            details = {key: str(params[key]) for key in ("command", "cwd", "grantRoot") if params.get(key)}
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
            qid = str(uuid4())
            card["question_id"] = qid
            self.requests[request_id]["question_ids"].append(qid)
            self.questions[qid] = {"request_id": request_id, "field_id": field_id, "card": card}
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
        if frame.get("remember"):
            raise ValueError("Разрешения выдаются только на одно действие")
        if request["method"] == "item/tool/requestUserInput":
            option = frame.get("option")
            if frame.get("verdict") != "choice" or not isinstance(option, str) or not option.strip():
                raise ValueError("Нужен текст ответа на вопрос")
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
            result = {"decision": "accept" if verdict == "allow" else "decline"}
        if not self.app:
            raise RPCError("Codex не подключён")
        await self.app.respond(request_id, result)
        self.requests.pop(request_id, None)
        for other in request["question_ids"]:
            self.questions.pop(other, None)
        await self.show_question()

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.relay:
            await self.relay.close()
        if self.app:
            await self.app.close()
