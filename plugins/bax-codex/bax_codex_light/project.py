"""Один канал проекта; разговоры продолжают работать независимо от выбранного экрана."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from . import __version__, files, session_status
from .appserver import EMPTY_THREAD_NAME, AppServer, RPCError, RPCRejected
from .bridge import SUPPORTS, Bridge, Submission
from .connection import Relay, network_error
from .errors import failure_fields, safe_message
from .history import History
from .permission_settings import PRESETS, choices, mode_of, options
from .preferences import Preferences
from .registry import Registration, Registry

log = logging.getLogger(__name__)
PROJECT_SUPPORTS = [
    *SUPPORTS,
    "sessions.list",
    "session.select",
    "session.close",
    "session.rename",
    "background.get",
    "background.stop",
    "cancel",
    "skills.list",
    "model.get",
    "model.set",
    "permissions.get",
    "permissions.set",
    "session.compact",
    "context.get",
    "session.status.get",
]
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


def session_title(thread: dict) -> str:
    name = thread.get("name")
    # После первого сообщения пустое служебное имя уступает содержательному превью.
    return (
        name if name and name != EMPTY_THREAD_NAME else thread.get("preview", "")[:100] or EMPTY_THREAD_NAME
    )


class SessionBridge(Bridge):
    def __init__(self, owner: ProjectController, thread_id: str):
        super().__init__(owner.project, owner.registry, thread_id, owner.endpoint)
        self.owner = owner
        self.app = owner.app
        self.history = History(self.app, thread_id)
        self.permission_selection: str | None = None
        self.model_selection: dict | None = None
        self.token_usage: dict = {}

    def start_options(self) -> dict:
        result = dict(self.model_selection or {})
        if self.permission_selection:
            result.update(options(self.permission_selection))
        return result

    async def model_started(self) -> None:
        if self.permission_selection:
            await self.app.resume_thread(self.thread_id, self.project)
            config = self.app.configurations[self.thread_id]
            if mode_of(config) == self.permission_selection:
                self.permission_selection = None
                if self.owner.selected == self.thread_id:
                    self.owner.template = {k: v for k, v in config.items() if k in CONFIG_FIELDS}
                self.owner.save()
            await self.owner.send_permission_settings(self.thread_id, config=config)
        if not self.model_selection:
            return
        thread = await self.app.read_thread(self.thread_id, self.project)
        if self.owner.model_values(thread) == self.model_selection:
            self.model_selection = None
            self.owner.catalog[self.thread_id] = thread
            self.owner.save()
            await self.owner.send_model_settings(self.thread_id)

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
        self.compacting: set[str] = set()
        self.compaction_baselines: dict[str, str | None] = {}
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
        self.backgrounds: dict[str, dict] = {}
        self.publications: dict[str, asyncio.Future] = {}
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
                "model_selection": bridge.model_selection,
                "permission_selection": bridge.permission_selection,
                "pending": {
                    cid: {
                        "text": item.text,
                        "error": item.error,
                        "steer_allowed": item.steer_allowed,
                        "image_count": item.image_count,
                        "request_id": item.request_id,
                        "delivery": item.delivery,
                    }
                    for cid, item in bridge.outbox.items()
                },
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

    def status(self, thread_id: str = "") -> dict:
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
            "state": "busy" if self.selected in self.compacting else selected.state if selected else "ready",
            "connected": bool(relay and relay.connected),
            "queued": sum(len(s.queue) for s in self.sessions.values()),
            "unconfirmed": sum(len(s.outbox) for s in self.sessions.values())
            + sum(len(s.get("pending", {})) for s in unavailable),
            "delivery_errors": sum(bool(i.error) for s in self.sessions.values() for i in s.outbox.values())
            + sum(len(s.get("pending", {})) for s in unavailable),
            "pending_questions": sum(len(s.questions) for s in self.sessions.values()),
            "async_questions": (
                self.sessions[thread_id or self.selected].async_question_details()
                if (thread_id or self.selected) in self.sessions
                else []
            ),
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
        return sum(
            s.state in {"busy", "waiting"} or tid in self.compacting
            for tid, s in self.sessions.items()
            if tid != self.selected
        )

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
                        self.saved.setdefault(thread_id, {})["error"] = safe_message(error)
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
                    self.error_code, self.error = "codex_unavailable", safe_message(error)
                self.ready.set()
                log.warning("%s", safe_message(self.error))
                if self.relay and self.relay.connected:
                    with contextlib.suppress(Exception):
                        await self.send(
                            "error",
                            **failure_fields(
                                self.error_code, self.error, operation="connect", scope="connection"
                            ),
                        )
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
            permission = self.saved.get(thread_id, {}).get("permission_selection")
            if isinstance(permission, str) and permission in PRESETS:
                bridge.permission_selection = permission
            selection = self.saved.get(thread_id, {}).get("model_selection")
            if isinstance(selection, dict) and all(
                isinstance(selection.get(k), str) and selection[k] for k in ("model", "effort")
            ):
                bridge.model_selection = {k: selection[k] for k in ("model", "effort")}
            for cid, pending in self.saved.get(thread_id, {}).get("pending", {}).items():
                bridge.outbox[cid] = Submission(
                    pending["text"],
                    error=pending.get("error")
                    or "Контроллер перезапущен. Проверьте историю перед повторной отправкой.",
                    steer_allowed=pending.get("steer_allowed", True),
                    image_count=pending.get("image_count", 0),
                    request_id=pending.get("request_id"),
                    delivery=pending.get("delivery", "unknown"),
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

    async def send_stats(self, thread_id: str) -> None:
        bridge = self.sessions.get(thread_id)
        if not bridge:
            return
        thread = self.catalog.get(thread_id, {})
        config = self.app.configurations.get(thread_id, {})
        model = thread.get("model") or config.get("model") or ""
        effort = thread.get("reasoningEffort") or config.get("reasoningEffort") or ""
        usage = bridge.token_usage or await self.app.context_usage(thread_id)
        context = {"used": 0, "max": 0}
        if not usage.get("model") or usage["model"] == model:
            if "used" in usage and "max" in usage:
                context = {key: usage[key] for key in ("used", "max")}
        await self.send("stats", session=thread_id, model=model, effort=effort, context=context)

    async def send_background(self) -> None:
        running = set()
        errors = []
        for thread_id, bridge in list(self.sessions.items()):
            if bridge.state == "offline":
                continue
            try:
                items = await self.app.background_terminals(thread_id, self.project)
            except (RPCRejected, RPCError, ValueError, TimeoutError) as error:
                errors.append(safe_message(error))
                continue
            for item in items:
                identity = thread_id + ":" + item["processId"]
                running.add(identity)
                previous = self.backgrounds.get(identity, {})
                self.backgrounds[identity] = {
                    "id": identity,
                    "task_id": item["processId"],
                    "session": thread_id,
                    "description": item["command"],
                    "status": "running",
                    "started_at": previous.get("started_at"),
                }
        if not errors:
            for identity, task in self.backgrounds.items():
                if task["status"] == "running" and identity not in running:
                    task.update(status="finished", finished_at=time.time())
        # Снимок только текущего контроллера; процесс другой сессии не переадресуется выбранной.
        active = [task for task in self.backgrounds.values() if task["status"] == "running"]
        finished = [task for task in self.backgrounds.values() if task["status"] != "running"][-100:]
        tasks = [*active, *finished]
        self.backgrounds = {task["id"]: task for task in tasks}
        await self.send(
            "background",
            tasks=tasks,
            error=("Codex не передал список фоновых процессов: " + "; ".join(dict.fromkeys(errors)))
            if errors
            else "",
        )

    async def publish_selection(self, rid: str | None = None) -> None:
        bridge = self.sessions.get(self.selected)
        thread = self.catalog.get(self.selected, {})
        await self.send(
            "session.changed",
            session=self.selected,
            title=session_title(thread),
            rid=rid,
        )
        await self.send(
            "status",
            session=self.selected,
            state=bridge.state if bridge else "ready",
            background_sessions=self.background_count,
        )
        await self.send_stats(self.selected)

    async def select(
        self, thread_id: str, *, archived: bool = False, rid: str | None = None, feed_prompt: str = ""
    ) -> None:
        if thread_id == "new":
            if self.selected:
                await self.ensure_session(self.selected)
                self.template = {
                    k: v for k, v in self.app.configurations[self.selected].items() if k in CONFIG_FIELDS
                }
            thread = await self.app.new_thread(self.project, self.template, feed_prompt)
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
            await bridge.send_failures()
            await self.send_sessions()

    async def send_sessions(self, *, archived: bool = False, cursor: str | None = None) -> None:
        if not self.app or self.app.closed.is_set():
            raise RPCError("Codex пока недоступен")
        page = await self.app.list_threads(self.project, archived=archived, cursor=cursor)
        threads = list(page["data"])
        if not archived and cursor is None:
            # Codex не всегда включает чат без первого сообщения в thread/list.
            # Добавляем только уже явно открытые и проверенные пустые разговоры.
            listed = {thread["id"] for thread in threads}
            threads.extend(
                self.catalog[tid]
                for tid, bridge in self.sessions.items()
                if tid not in listed
                and bridge.state != "offline"
                and tid in self.catalog
                and not self.catalog[tid].get("preview")
            )
        rows = []
        for thread in threads:
            path = await asyncio.to_thread(Path(thread["cwd"]).resolve)
            if path != self.project or thread.get("parentThreadId"):
                continue
            self.catalog[thread["id"]] = thread
            bridge = self.sessions.get(thread["id"])
            status = (
                bridge.state
                if bridge
                else ("ready" if thread["status"]["type"] == "systemError" else thread["status"]["type"])
            )
            rows.append(
                {
                    "session": thread["id"],
                    "title": session_title(thread),
                    "updated_at": thread["updatedAt"],
                    "entries": 0,
                    "current": thread["id"] == self.selected,
                    "state": status,
                    "pending_questions": len(bridge.questions) if bridge else 0,
                    "unconfirmed": len(bridge.outbox)
                    if bridge
                    else len(self.saved.get(thread["id"], {}).get("pending", {})),
                    "archived": archived,
                    "last_error": bridge.last_failure if bridge else None,
                }
            )
        rows.sort(key=lambda row: (row["updated_at"], row["session"]), reverse=True)
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
            if kind == "feed.published":
                pending = self.publications.get(frame.get("rid"))
                if pending and not pending.done():
                    pending.set_result({k: v for k, v in frame.items() if k not in {"v", "type", "rid"}})
            elif kind == "subscribe":
                await self.on_ready()
                bridge = self.sessions.get(self.selected)
                if bridge:
                    await bridge.send_history()
                    await bridge.show_question()
                await self.send("history.done", session=self.selected)
                if bridge:
                    await bridge.send_failures()
                await self.send_sessions()
            elif kind == "sessions.list":
                await self.send_sessions(archived=frame.get("archived") is True, cursor=frame.get("cursor"))
            elif kind in {"session.select", "session.close", "session.rename"}:
                async with self.lock:
                    if frame.get("expected_session") != self.selected:
                        raise ValueError("Выбранная сессия изменилась; обновите список и повторите действие")
                    target = frame.get("session")
                    if not isinstance(target, str) or not target:
                        raise ValueError("Нужен точный ID сессии")
                    if kind == "session.select":
                        await self.select(
                            target,
                            archived=frame.get("archived") is True,
                            rid=frame.get("rid"),
                            feed_prompt=frame.get("feed_prompt", ""),
                        )
                    elif kind == "session.rename":
                        name = frame.get("title")
                        if (
                            not isinstance(name, str)
                            or not 1 <= len(name.strip()) <= 100
                            or any(ord(ch) < 32 for ch in name)
                        ):
                            raise ValueError("Название сессии должно содержать от 1 до 100 символов")
                        await self.app.rename_thread(target, self.project, name.strip())
                        self.catalog.setdefault(target, {})["name"] = name.strip()
                        await self.send(
                            "session.renamed", session=target, title=name.strip(), rid=frame.get("rid")
                        )
                        await self.send_sessions(archived=frame.get("archived") is True)
                    else:
                        await self.close_session(target, frame.get("rid"))
            elif kind in {"context.get", "session.compact"}:
                async with self.lock:
                    target = frame.get("session")
                    if (
                        not target
                        or target != self.selected
                        or frame.get("expected_session") != self.selected
                    ):
                        raise ValueError("Выбранная сессия изменилась; откройте её контекст заново")
                    if target not in self.sessions:
                        raise ValueError("Этот разговор не подключён к проекту")
                    if kind == "session.compact":
                        if target in self.compacting:
                            raise ValueError("Сжатие этого разговора уже запущено")
                        baseline = await self.app.compaction_history(target, self.project)
                        self.compaction_baselines[target] = (
                            baseline["items"][-1]["id"] if baseline["items"] else None
                        )
                        self.compacting.add(target)
                        # Подтверждаем запрос сразу; завершение приходит отдельно.
                        await self.send(
                            "context.history",
                            session=target,
                            rid=frame.get("rid"),
                            compacting=True,
                            **baseline,
                        )
                        try:
                            await self.app.compact_thread(target, self.project)
                        except TimeoutError:
                            # Тайм-аут RPC не отменяет уже доставленную команду Codex.
                            await self.send_context(
                                target,
                                error="Codex пока не подтвердил запуск. Проверяю журнал сжатия.",
                            )
                            return
                        except BaseException:
                            self.compacting.discard(target)
                            self.compaction_baselines.pop(target, None)
                            raise
                    await self.send_context(target, frame.get("rid"))
            elif kind == "background.get":
                await self.send_background()
            elif kind == "background.stop":
                target = frame.get("session")
                if target not in self.sessions:
                    raise ValueError("Этот разговор не подключён к проекту")
                terminated = await self.app.terminate_background(
                    target, self.project, str(frame.get("task_id") or "")
                )
                if not terminated:
                    raise ValueError("Codex не подтвердил остановку фонового процесса")
                task = self.backgrounds.get(target + ":" + str(frame.get("task_id") or ""))
                if task:
                    task.update(status="stopped", finished_at=time.time())
                await self.send_background()
            elif kind == "session.status.get":
                target = frame.get("session")
                if not target or target != self.selected or target not in self.sessions:
                    raise ValueError("Выбранная сессия изменилась; откройте статус заново")
                details = await session_status.snapshot(self, target)
                await self.send_stats(target)
                await self.send("session.details", session=target, rid=frame.get("rid"), **details)
            elif kind == "skills.list":
                try:
                    skills = await self.app.installed_skills(self.project)
                    await self.send("skills", rid=frame.get("rid"), skills=skills)
                except (ValueError, RPCError, TimeoutError, OSError) as error:
                    await self.send("skills", rid=frame.get("rid"), skills=[], error=safe_message(error))
            elif kind in {"permissions.get", "permissions.set"}:
                async with self.lock:
                    await self.permission_command(frame)
            elif kind in {"model.get", "model.set"}:
                async with self.lock:
                    await self.model_command(frame)
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
            if frame.get("type") in {"model.get", "model.set", "permissions.get", "permissions.set"}:
                failure = failure_fields(
                    "session_command_failed", error, operation=str(frame.get("type")), rid=frame.get("rid")
                )
                await self.send(
                    "permissions.settings"
                    if str(frame.get("type")).startswith("permissions.")
                    else "model.settings",
                    session=frame.get("session"),
                    rid=frame.get("rid"),
                    error=failure["message"],
                    error_details={**failure, "session": frame.get("session")},
                )
                return
            await self.send(
                "error",
                **failure_fields(
                    "session_command_failed",
                    error,
                    operation=str(frame.get("type") or "command"),
                    rid=frame.get("rid"),
                ),
                session=frame.get("session"),
            )

    async def send_context(self, thread_id: str, rid=None, error=None) -> None:
        result = await self.app.compaction_history(thread_id, self.project)
        if thread_id in self.compacting and result["available"] and result["items"]:
            latest = result["items"][-1]["id"]
            if latest != self.compaction_baselines.get(thread_id):
                # Shared app-server может не прислать завершающее событие этому клиенту.
                # Новая запись точного журнала подтверждает завершение без повторной команды.
                self.compacting.discard(thread_id)
                self.compaction_baselines.pop(thread_id, None)
                self.sessions[thread_id].token_usage = await self.app.context_usage(thread_id)
                await self.send_stats(thread_id)
                error = None
        await self.send(
            "context.history",
            session=thread_id,
            rid=rid,
            compacting=thread_id in self.compacting,
            error=safe_message(error) if error else None,
            **result,
        )

    async def permission_command(self, frame: dict) -> None:
        target = frame.get("session")
        if (
            not isinstance(target, str)
            or not target
            or target != self.selected
            or target not in self.sessions
        ):
            raise ValueError("Выбранный разговор изменился. Откройте разрешения снова")
        await self.app.resume_thread(target, self.project)  # Чтение без переопределений.
        config = self.app.configurations[target]
        requirements = await self.app.permission_requirements()
        available = choices(requirements)
        if frame["type"] == "permissions.set":
            mode = frame.get("mode")
            if not isinstance(mode, str) or not any(
                item["mode"] == mode and item["allowed"] for item in available
            ):
                raise ValueError("Этот режим недоступен по правилам Codex на компьютере")
            if mode == "full-access" and frame.get("confirm_full_access") is not True:
                raise ValueError("Подтвердите полный доступ к файлам и сети без запросов разрешения")
            bridge = self.sessions[target]
            bridge.permission_selection = None if mode == mode_of(config) else mode
            self.save()
        await self.send_permission_settings(target, rid=frame.get("rid"), config=config, available=available)

    async def send_permission_settings(self, target: str, *, rid=None, config=None, available=None) -> None:
        bridge = self.sessions.get(target)
        if not bridge:
            return
        if config is None:
            await self.app.resume_thread(target, self.project)
            config = self.app.configurations[target]
        if available is None:
            available = choices(await self.app.permission_requirements())
        current = mode_of(config)
        await self.send(
            "permissions.settings",
            session=target,
            rid=rid,
            mode=bridge.permission_selection or current,
            current_mode=current,
            pending=bool(bridge.permission_selection),
            approval_policy=config["approvalPolicy"],
            reviewer=config["approvalsReviewer"],
            sandbox=config["sandbox"],
            profile=(config.get("activePermissionProfile") or {}).get("id"),
            modes=available,
        )

    @staticmethod
    def model_values(thread: dict) -> dict:
        return {"model": thread.get("model") or "", "effort": thread.get("reasoningEffort") or ""}

    async def model_command(self, frame: dict) -> None:
        target = frame.get("session")
        if (
            not isinstance(target, str)
            or not target
            or target != self.selected
            or target not in self.sessions
        ):
            raise ValueError("Выбранный диалог изменился. Откройте модель и effort снова")
        # Проверка cwd/ID выполняется тем же thread/read, что и для обычных команд.
        thread = await self.app.read_thread(target, self.project)
        catalog = await self.app.model_catalog()
        if frame["type"] == "model.set":
            model, effort = frame.get("model"), frame.get("effort")
            choice = next((item for item in catalog if item["model"] == model), None)
            if not choice or effort not in [
                item["reasoningEffort"] for item in choice["supportedReasoningEfforts"]
            ]:
                raise ValueError("Эта модель или effort недоступны. Обновите список и повторите")
            selection = {"model": model, "effort": effort}
            bridge = self.sessions[target]
            bridge.model_selection = None if selection == self.model_values(thread) else selection
            self.save()
        await self.send_model_settings(target, rid=frame.get("rid"), thread=thread, catalog=catalog)

    async def send_model_settings(self, target: str, *, rid=None, thread=None, catalog=None) -> None:
        bridge = self.sessions.get(target)
        if not bridge:
            return
        thread = thread or await self.app.read_thread(target, self.project)
        catalog = catalog if catalog is not None else await self.app.model_catalog()
        current = self.model_values(thread)
        selected = bridge.model_selection or current
        await self.send(
            "model.settings",
            session=target,
            rid=rid,
            **selected,
            current_model=current["model"],
            current_effort=current["effort"],
            pending=bool(bridge.model_selection),
            models=[
                {
                    "model": item["model"],
                    "name": item["displayName"],
                    "default_effort": item["defaultReasoningEffort"],
                    "efforts": [value["reasoningEffort"] for value in item["supportedReasoningEfforts"]],
                }
                for item in catalog
            ],
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

    async def publish_to_feed(self, thread_id: str, text: str, publication_key: str) -> dict:
        if not self.app or not self.relay or not self.relay.connected:
            raise ValueError("Бакс не подключён")
        await self.app.inspect(thread_id, self.project)
        if not text.strip() or len(text) > 10000 or not publication_key.strip() or len(publication_key) > 128:
            raise ValueError("Нужен текст до 10000 символов и постоянный ключ результата до 128 символов")
        rid = str(uuid4())
        pending = asyncio.get_running_loop().create_future()
        self.publications[rid] = pending
        try:
            if not await self.send("feed.publish", rid=rid, text=text, publication_key=publication_key):
                raise ValueError("Нет связи с Потоком; публикация не подтверждена")
            try:
                return await asyncio.wait_for(pending, 20)
            except TimeoutError:
                return {
                    "published": None,
                    "reason": "confirmation_timeout",
                    "publication_key": publication_key,
                }
        finally:
            self.publications.pop(rid, None)

    async def resolve_question(self, thread_id: str, question_id: str) -> dict:
        bridge = self.sessions.get(thread_id)
        if bridge is None:
            raise ValueError("Этот разговор не подключён к проекту")
        result = await bridge.resolve_question(question_id)
        self.save()
        return result

    async def on_event(self, event: dict) -> None:
        params = event.get("params", {})
        thread_id = params.get("threadId")
        bridge = self.sessions.get(thread_id)
        if not bridge:
            return
        method = event["method"]
        if method == "thread/settings/updated":
            settings = params.get("threadSettings", {})
            thread = self.catalog.get(thread_id, {}).copy()
            thread.update(model=settings.get("model"), reasoningEffort=settings.get("effort"))
            self.catalog[thread_id] = thread
            if bridge.model_selection == self.model_values(thread):
                bridge.model_selection = None
            if thread_id == self.selected:
                # Новые разговоры наследуют подтверждённые параметры выбранного,
                # остальные политики не меняются.
                self.template.update(model=thread.get("model"), reasoningEffort=thread.get("reasoningEffort"))
            await self.send_model_settings(thread_id, thread=thread)
            bridge.token_usage = {}
            await self.send_stats(thread_id)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            last = usage.get("last", {})
            used, maximum = last.get("totalTokens"), usage.get("modelContextWindow")
            if type(used) is int and used >= 0 and type(maximum) is int and maximum > 0:
                thread = self.catalog.get(thread_id, {})
                bridge.token_usage = {"used": used, "max": maximum, "model": thread.get("model", "")}
                await self.send_stats(thread_id)
        elif method == "thread/name/updated":
            name = params.get("threadName")
            self.catalog.setdefault(thread_id, {})["name"] = name
            await self.send(
                "session.renamed", session=thread_id, title=session_title(self.catalog[thread_id])
            )
        elif method in {"thread/closed", "thread/archived"}:
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
        if thread_id in self.compacting and (
            method in {"thread/compacted", "turn/completed", "thread/closed", "thread/archived"}
            or (method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction")
        ):
            self.compacting.discard(thread_id)
            self.compaction_baselines.pop(thread_id, None)
            failure = params.get("turn", {}).get("error") or {}
            await self.send_context(thread_id, error=failure.get("message"))
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
