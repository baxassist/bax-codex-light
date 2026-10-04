"""Один канал проекта; разговоры продолжают работать независимо от выбранного экрана."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from . import __version__, files
from .appserver import AppServer, RPCError
from .bridge import SUPPORTS, Bridge, Submission
from .connection import Relay, network_error
from .history import History
from .preferences import Preferences
from .registry import Registration, Registry

log = logging.getLogger(__name__)
PROJECT_SUPPORTS = [*SUPPORTS, "sessions.list", "session.select", "session.close", "cancel"]
CONFIG_FIELDS = (
    "model",
    "modelProvider",
    "approvalPolicy",
    "approvalsReviewer",
    "reasoningEffort",
    "sandbox",
    "activePermissionProfile",
    "serviceTier",
)


class SessionBridge(Bridge):
    def __init__(self, owner: ProjectController, thread_id: str):
        super().__init__(owner.project, owner.registry, thread_id, owner.endpoint)
        self.owner = owner
        self.app = owner.app
        self.history = History(self.app, thread_id)

    async def send_history(self, before: int | None = None, limit: int = 50) -> None:
        await super().send_history(before, limit)
        self.owner.save()  # Прочитанная история могла подтвердить ранее неопределённую доставку.

    async def send(self, frame_type: str, **fields) -> bool:
        # Записываем ожидающее сообщение до эха приёма на телефон.
        if frame_type == "message" and fields.get("kind") == "user":
            self.owner.save()
        if frame_type == "status":
            fields["background_sessions"] = self.owner.background_count
            if self.owner.selected == self.thread_id:
                return await self.owner.send("status", session=self.thread_id, **fields)
            return await self.owner.send("session.status", session=self.thread_id, **fields)
        return await self.owner.send(frame_type, session=self.thread_id, **fields)


class ProjectController:
    def __init__(self, project: Path, registry: Registry, state_path: Path, endpoint: str | None = None):
        self.project = project.resolve()
        self.registry = registry
        self.state_path = state_path
        self.endpoint = endpoint
        self.identity = "project:" + hashlib.sha256(str(self.project).encode()).hexdigest()[:32]
        self.selected = ""
        self.sessions: dict[str, SessionBridge] = {}
        self.saved: dict = {}
        self.template: dict = {}
        self.app: AppServer | None = None
        self.relay: Relay | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        self.error = self.error_code = ""
        self.catalog: dict[str, dict] = {}
        self.preferences = Preferences(registry.path.with_name(f"{registry.path.stem}-settings.json"))
        self.load()

    def load(self) -> None:
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(fd) as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise ValueError("Состояние проекта доступно другим пользователям; нужны права 600")
            data = json.load(file)
        if data.get("project") != str(self.project):
            raise ValueError("Состояние контроллера принадлежит другому проекту")
        self.selected = data.get("selected", "")
        self.saved = data.get("sessions", {})
        self.template = data.get("template", {})

    def save(self) -> None:
        sessions = dict(self.saved)
        for thread_id, bridge in self.sessions.items():
            sessions[thread_id] = {
                "pending": {
                    cid: {"text": item.text, "error": item.error, "steer_allowed": item.steer_allowed}
                    for cid, item in bridge.outbox.items()
                }
            }
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".project-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(
                    {
                        "project": str(self.project),
                        "selected": self.selected,
                        "sessions": sessions,
                        "template": self.template,
                    },
                    file,
                    ensure_ascii=False,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.state_path)
        finally:
            Path(temp).unlink(missing_ok=True)
        self.saved = sessions

    def status(self) -> dict:
        selected = self.sessions.get(self.selected)
        relay = self.relay
        unavailable = [s for tid, s in self.saved.items() if tid not in self.sessions]
        return {
            "plugin_version": __version__,
            "mode": "project",
            "project": str(self.project),
            "thread_id": self.selected or None,
            "controller_id": self.identity,
            "controller_pid": os.getpid(),
            "state": selected.state if selected else "ready",
            "connected": bool(relay and relay.connected),
            "queued": sum(len(s.queue) for s in self.sessions.values()),
            "unconfirmed": sum(len(s.outbox) for s in self.sessions.values())
            + sum(len(s.get("pending", {})) for s in unavailable),
            "delivery_errors": sum(bool(i.error) for s in self.sessions.values() for i in s.outbox.values())
            + sum(len(s.get("pending", {})) for s in unavailable),
            "pending_questions": sum(len(s.questions) for s in self.sessions.values()),
            "background_sessions": self.background_count,
            "error": self.error or (relay.error if relay else ""),
            "error_code": self.error_code or (relay.error_code if relay else ""),
            "keep_awake": self.power_settings(),
            "relay_connection": {
                "last_connected_at": relay.last_connected_at,
                "last_disconnected_at": relay.last_disconnected_at,
                "last_close_code": relay.last_close_code,
                "last_close_reason": relay.last_close_reason,
                "next_retry_seconds": relay.retry_delay,
            }
            if relay
            else None,
            "approvals": {
                "policy": self.template.get("approvalPolicy"),
                "reviewer": self.template.get("approvalsReviewer"),
                "manual": self.template.get("approvalsReviewer") == "user"
                and self.template.get("approvalPolicy") != "never",
            },
            "needs_thread": False,
            "needs_registration": self.registry.get(self.project) is None,
        }

    @property
    def background_count(self) -> int:
        return sum(s.state in {"busy", "waiting"} for tid, s in self.sessions.items() if tid != self.selected)

    async def start(self) -> None:
        if self.task is None or self.task.done():
            self.ready.clear()
            self.task = asyncio.create_task(self.run(), name="bax-project-controller")

    async def run(self) -> None:
        attempt = 0
        while True:
            relay_task = None
            try:
                self.app = AppServer(self.endpoint)
                await self.app.open(self.on_event)
                # Восстанавливаем только явно выбранные ранее разговоры этого проекта.
                for thread_id in set(self.saved) | ({self.selected} if self.selected else set()):
                    try:
                        await self.ensure_session(thread_id)
                    except RPCError as error:
                        if self.selected == thread_id:
                            self.selected = ""
                        # Недоступная/архивная сессия не отменяет неопределённую доставку.
                        self.saved.setdefault(thread_id, {})["error"] = str(error)
                registration = self.registry.get(self.project)
                if not registration:
                    self.error_code = "registration_missing"
                    self.error = "Нет регистрации проекта. Вставьте команду подключения из Бакса"
                    self.ready.set()
                    await self.app.closed.wait()
                    raise RPCError("Связь с Codex потеряна")
                awake = self.preferences.get(self.project, registration.agent)
                self.relay = Relay(registration, self.project, self.identity, keep_awake=awake)
                self.error = self.error_code = ""
                self.ready.set()
                relay_task = asyncio.create_task(self.relay.run(self.on_ready, self.on_frame))
                attempt = 0
                await self.app.closed.wait()
                raise RPCError("Связь с Codex потеряна; контроллер проекта повторит подключение")
            except Exception as error:
                self.error_code, self.error = network_error(error, "локальному серверу Codex")
                if isinstance(error, RPCError):
                    self.error_code, self.error = "codex_unavailable", str(error)
                self.ready.set()
                log.warning("%s", self.error)
            finally:
                self.save()
                if relay_task:
                    relay_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await relay_task
                if self.relay:
                    await self.relay.close()
                if self.app:
                    await self.app.close()
                for bridge in self.sessions.values():
                    for qid in list(bridge.questions):
                        await self.send("question.resolved", session=bridge.thread_id, question_id=qid)
                    bridge.questions.clear()
                    bridge.requests.clear()
            await asyncio.sleep(min(2**attempt, 30))
            attempt = min(attempt + 1, 5)

    async def ensure_session(self, thread_id: str, *, archived: bool = False) -> SessionBridge:
        if not self.app or self.app.closed.is_set():
            raise RPCError("Codex пока недоступен")
        thread = await self.app.read_thread(thread_id, self.project)
        if archived:
            thread = await self.app.unarchive_thread(thread_id, self.project)
        thread = await self.app.resume_thread(thread_id, self.project)
        if thread_id not in self.sessions:
            bridge = SessionBridge(self, thread_id)
            for cid, pending in self.saved.get(thread_id, {}).get("pending", {}).items():
                bridge.outbox[cid] = Submission(
                    pending["text"],
                    error=pending.get("error")
                    or "Контроллер перезапущен. Проверьте историю перед повторной отправкой.",
                    steer_allowed=pending.get("steer_allowed", True),
                )
            self.sessions[thread_id] = bridge
        bridge = self.sessions[thread_id]
        bridge.app = self.app
        bridge.history.app = self.app
        bridge._set_state(thread["status"])
        bridge.turn_id = await self.app.active_turn(thread_id) if bridge.state in {"busy", "waiting"} else ""
        self.catalog[thread_id] = thread
        return bridge

    async def attach(self, thread_id: str) -> dict:
        # Локальная команда явно выбирает именно переданный CODEX_THREAD_ID.
        async with self.lock:
            if not self.task or self.task.done():
                await self.start()
            await asyncio.wait_for(self.ready.wait(), 3)
            if not self.app or self.app.closed.is_set():
                raise RPCError(self.error or "Codex пока недоступен")
            await self.app.inspect(thread_id, self.project)
            await self.select(thread_id)
            return self.status()

    async def connect(self, key: str, server: str) -> dict:
        async with self.lock:
            registration = Registration.from_key(key, server)
            previous = self.registry.get(self.project)
            if previous and previous.agent != registration.agent:
                raise ValueError("Проект подключён к другому агенту; регистрация сохранена")
            changed = not previous or any(
                getattr(previous, k) != getattr(registration, k) for k in ("key_id", "secret", "server")
            )
            self.registry.put(self.project, registration)
            if changed and self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            await self.start()
            return self.status()

    async def send(self, frame_type: str, **fields) -> bool:
        return bool(self.relay and await self.relay.send(frame_type, **fields))

    def power_settings(self) -> dict:
        return {
            **(
                self.relay.sleep_guard.status()
                if self.relay
                else {"supported": False, "active": False, "error": ""}
            ),
            "enabled": self.relay.keep_awake_enabled if self.relay else True,
        }

    async def on_ready(self) -> None:
        await self.send("caps", mode="project", supports=PROJECT_SUPPORTS, remember=True)
        await self.send("power.settings", **self.power_settings())
        await self.publish_selection()

    async def publish_selection(self, rid: str | None = None) -> None:
        bridge = self.sessions.get(self.selected)
        thread = self.catalog.get(self.selected, {})
        await self.send(
            "session.changed",
            session=self.selected,
            title=thread.get("name") or thread.get("preview", "")[:100] or "Новая сессия",
            rid=rid,
        )
        await self.send(
            "status",
            session=self.selected,
            state=bridge.state if bridge else "ready",
            background_sessions=self.background_count,
        )

    async def select(self, thread_id: str, *, archived: bool = False, rid: str | None = None) -> None:
        if thread_id == "new":
            if self.selected:
                await self.ensure_session(self.selected)
                self.template = {
                    k: v for k, v in self.app.configurations[self.selected].items() if k in CONFIG_FIELDS
                }
            thread = await self.app.new_thread(self.project, self.template)
            thread_id = thread["id"]
        bridge = await self.ensure_session(thread_id, archived=archived)
        self.selected = thread_id
        self.template = {k: v for k, v in self.app.configurations[thread_id].items() if k in CONFIG_FIELDS}
        self.save()
        await self.publish_selection(rid)
        if self.relay and self.relay.connected:
            await bridge.send_history()
            await bridge.show_question()
            await self.send("history.done", session=thread_id)
            await self.send_sessions()

    async def send_sessions(self, *, archived: bool = False, cursor: str | None = None) -> None:
        if not self.app or self.app.closed.is_set():
            raise RPCError("Codex пока недоступен")
        page = await self.app.list_threads(self.project, archived=archived, cursor=cursor)
        rows = []
        for thread in page["data"]:
            path = await asyncio.to_thread(Path(thread["cwd"]).resolve)
            if path != self.project or thread.get("parentThreadId"):
                continue
            self.catalog[thread["id"]] = thread
            bridge = self.sessions.get(thread["id"])
            status = bridge.state if bridge else thread["status"]["type"]
            rows.append(
                {
                    "session": thread["id"],
                    "title": thread.get("name") or thread["preview"][:100] or "Новая сессия",
                    "updated_at": thread["updatedAt"],
                    "entries": 0,
                    "current": thread["id"] == self.selected,
                    "state": status,
                    "pending_questions": len(bridge.questions) if bridge else 0,
                    "unconfirmed": len(bridge.outbox)
                    if bridge
                    else len(self.saved.get(thread["id"], {}).get("pending", {})),
                    "archived": archived,
                }
            )
        await self.send(
            "sessions",
            items=rows,
            archived=archived,
            next_cursor=page.get("nextCursor"),
            append=cursor is not None,
            selected=self.selected,
        )

    async def on_frame(self, frame: dict) -> None:
        try:
            kind = frame.get("type")
            if kind == "subscribe":
                await self.on_ready()
                if bridge := self.sessions.get(self.selected):
                    await bridge.send_history()
                    await bridge.show_question()
                await self.send("history.done", session=self.selected)
                await self.send_sessions()
            elif kind == "sessions.list":
                await self.send_sessions(archived=frame.get("archived") is True, cursor=frame.get("cursor"))
            elif kind in {"session.select", "session.close"}:
                async with self.lock:
                    if frame.get("expected_session") != self.selected:
                        raise ValueError("Выбранная сессия изменилась; обновите список и повторите действие")
                    target = frame.get("session")
                    if not isinstance(target, str) or not target:
                        raise ValueError("Нужен точный ID сессии")
                    if kind == "session.select":
                        await self.select(
                            target, archived=frame.get("archived") is True, rid=frame.get("rid")
                        )
                    else:
                        await self.close_session(target, frame.get("rid"))
            elif kind in {"power.get", "power.set"}:
                if kind == "power.set":
                    enabled = frame.get("keep_awake")
                    if type(enabled) is not bool:
                        raise ValueError("Защита от сна должна быть true или false")
                    reg = self.relay.registration
                    self.preferences.put(self.project, reg.agent, enabled)
                    await self.relay.set_keep_awake(enabled)
                await self.send("power.settings", **self.power_settings(), rid=frame.get("rid"))
            elif kind == "files.list":
                paths = await files.tracked(self.project)
                await self.send(
                    "files", paths=paths[: files.MAX_PATHS], truncated=len(paths) > files.MAX_PATHS
                )
            elif kind == "files.read":
                paths = await files.tracked(self.project)
                path = str(frame.get("path", ""))
                result = await asyncio.to_thread(files.read, self.project, path, paths)
                await self.send("file.text", path=path, **result)
            else:
                target = frame.get("session")
                bridge = self.sessions.get(target)
                if not bridge or (kind not in {"answer", "cancel"} and target != self.selected):
                    raise ValueError("Выберите сессию; команда не отправлена в другой разговор")
                if kind == "cancel":
                    turn_id = await self.app.active_turn(target)
                    if turn_id:
                        await self.app.interrupt_turn(target, turn_id)
                else:
                    await bridge.on_frame(frame)
                self.save()
        except (ValueError, RPCError, TimeoutError, OSError) as error:
            await self.send(
                "error",
                code="session_command_failed",
                message=str(error),
                rid=frame.get("rid"),
                session=frame.get("session"),
            )

    async def close_session(self, thread_id: str, rid: str | None) -> None:
        # Закрытие не останавливает чужую работу и не удаляет историю.
        thread = await self.app.read_thread(thread_id, self.project)
        bridge = self.sessions.get(thread_id)
        if (
            (not bridge and self.saved.get(thread_id, {}).get("pending"))
            or thread["status"]["type"] == "active"
            or (bridge and (bridge.queue or bridge.outbox or bridge.questions))
        ):
            raise ValueError("Сессия ещё работает или ждёт ответа. Сначала остановите задачу или ответьте")
        await self.app.archive_thread(thread_id)
        self.sessions.pop(thread_id, None)
        self.saved.pop(thread_id, None)
        if self.selected == thread_id:
            self.selected = ""
        self.save()
        await self.publish_selection(rid)
        await self.send_sessions()

    async def on_event(self, event: dict) -> None:
        params = event.get("params", {})
        thread_id = params.get("threadId")
        bridge = self.sessions.get(thread_id)
        if not bridge:
            return
        method = event["method"]
        if method in {"thread/closed", "thread/archived"}:
            # Закрытие одного разговора не закрывает канал и другие разговоры проекта.
            bridge.state = "offline"
            for qid in list(bridge.questions):
                await self.send("question.resolved", session=thread_id, question_id=qid)
            bridge.questions.clear()
            bridge.requests.clear()
            if method == "thread/archived":
                if not bridge.outbox and not bridge.queue:
                    self.sessions.pop(thread_id, None)
                    self.saved.pop(thread_id, None)
            if self.selected == thread_id:
                self.selected = ""
                await self.publish_selection()
        elif method == "thread/status/changed" and params["status"]["type"] == "notLoaded":
            bridge.state = "offline"
            # Не закрываем общий app-server из-за одного выгруженного разговора.
            if self.selected == thread_id:
                self.selected = ""
                await self.publish_selection()
            else:
                await bridge.send("status", state="offline")
        else:
            await bridge.on_event(event)
        if method != "item/agentMessage/delta":
            self.save()
        if (
            method
            in {
                "thread/status/changed",
                "turn/started",
                "turn/completed",
                "serverRequest/resolved",
                "thread/closed",
                "thread/archived",
            }
            or "id" in event
            or bridge.questions
        ):
            with contextlib.suppress(RPCError, TimeoutError):
                await self.send_sessions()

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.save()
