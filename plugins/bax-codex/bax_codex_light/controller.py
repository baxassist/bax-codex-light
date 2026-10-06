"""Локальный контроллер проекта, независимый от времени жизни STDIO MCP."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from . import __version__
from .appserver import AppServer, RPCError
from .project import ProjectController
from .registry import Registry


class ControllerUpdatePending(RPCError):
    """Старый контроллер сохраняет связь до безопасного промежутка между ходами."""


def runtime_path(project: Path, registry: Registry, endpoint: str | None) -> Path:
    key = f"{project.resolve()}\n{registry.path.resolve()}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    path = registry.path.parent / "codex-projects" / digest
    # На macOS Unix socket ограничен 104 байтами; длинный путь теста/профиля не помещается.
    if len(os.fsencode(path / "control.sock")) >= 100:
        path = Path("/tmp") / f"bax-codex-{os.getuid()}" / digest
    return path


async def request(directory: Path, method: str, **params) -> dict:
    async with unix_connect(
        str(directory / "control.sock"),
        uri="ws://localhost/",
        open_timeout=2,
        close_timeout=1,
        max_size=2 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({"method": method, "params": params}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if "error" in result:
            raise RPCError(result["error"])
        return result["result"]


def private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = directory.lstat()
    if directory.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("Каталог контроллера должен быть приватным, без symlink")


async def serve_controller(project: Path, registry: Registry, endpoint: str | None = None) -> None:
    directory = runtime_path(project, registry, endpoint)
    private_directory(directory)
    lock_fd = os.open(directory / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Другой MCP уже запустил единственного владельца проекта.
        socket_path = directory / "control.sock"
        socket_path.unlink(missing_ok=True)
        controller = ProjectController(project, registry, directory / "state.json", endpoint)
        shutdown = asyncio.Event()

        async def handle(ws):
            async for payload in ws:
                try:
                    message = json.loads(payload)
                    method, params = message["method"], message.get("params", {})
                    if method == "status":
                        result = controller.status(params.get("thread_id", ""))
                    elif method == "resolve_question":
                        result = await controller.resolve_question(params["thread_id"], params["question_id"])
                    elif method == "attach":
                        result = await controller.attach(params["thread_id"])
                    elif method == "connect":
                        result = await controller.connect(params["key"], params["server"])
                    elif method == "shutdown":
                        status = controller.status()
                        if (
                            status["background_sessions"]
                            or status["state"] in {"busy", "waiting"}
                            or any(
                                status[k]
                                for k in ("queued", "unconfirmed", "pending_questions", "delivery_errors")
                            )
                        ):
                            raise ValueError("Контроллер занят; обновление отложено до завершения работы")
                        result = {"stopping": True}
                        shutdown.set()
                    else:
                        raise ValueError("Неизвестная команда контроллера")
                    await ws.send(json.dumps({"result": result}, ensure_ascii=False))
                except (ValueError, KeyError, RPCError, OSError, TimeoutError) as error:
                    await ws.send(json.dumps({"error": str(error)}, ensure_ascii=False))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown.set)
        try:
            async with unix_serve(handle, str(socket_path), max_size=2 * 1024 * 1024):
                os.chmod(socket_path, 0o600)
                await controller.start()
                await shutdown.wait()
        finally:
            await controller.close()
            socket_path.unlink(missing_ok=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


class ProjectClient:
    """Каждый MCP знает свой точный разговор, но не владеет каналом Бакса."""

    def __init__(
        self, project: Path | None, registry: Registry, thread_id: str = "", endpoint: str | None = None
    ):
        self.project = project.resolve() if project else None
        self.registry = registry
        self.thread_id = thread_id
        self.endpoint = endpoint
        self.error = ""
        self.error_code = ""
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        # Инициализация MCP не ждёт доступности Codex; required-инструменты готовы сразу.
        if self.thread_id and self.task is None:
            self.task = asyncio.create_task(self.auto_discover(), name="bax-project-client")

    async def auto_discover(self) -> None:
        attempt = 0
        while True:
            await self.discover()
            if not self.error:
                return
            if self.error_code == "controller_update_pending":
                # Это не сетевой сбой: 30-секундная задержка пропускает короткий
                # промежуток между задачами. Сам владелец проверяет безопасность остановки.
                await asyncio.sleep(0.25)
                continue
            await asyncio.sleep(min(2**attempt, 30))
            attempt = min(attempt + 1, 5)

    async def discover(self) -> None:
        app = AppServer(self.endpoint)
        try:
            await app.open()
            thread = await app.inspect(self.thread_id, self.project)
            self.project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
            await self.ensure_controller()
            self.error = ""
            self.error_code = ""
        except (RPCError, OSError, TimeoutError, ValueError) as error:
            self.error = str(error)
            self.error_code = "controller_unavailable"
            if isinstance(error, ControllerUpdatePending):
                self.error_code = "controller_update_pending"
            elif isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
                self.error_code = "codex_unavailable"
                self.error = (
                    f"Локальный сервер Codex недоступен: {app.endpoint}. "
                    "Откройте Codex; повторите подключение."
                )
            elif isinstance(error, PermissionError):
                self.error_code = "codex_socket_permission_denied"
                self.error = (
                    f"ОС запретила доступ к локальному сокету Codex: {app.endpoint}. "
                    "Запускайте MCP штатно из Codex."
                )
        finally:
            await app.close()

    async def ensure_controller(self) -> Path:
        if self.project is None:
            raise ValueError("Нужен точный CODEX_THREAD_ID текущего разговора")
        directory = runtime_path(self.project, self.registry, self.endpoint)
        try:
            status = await request(directory, "status")
        except (OSError, TimeoutError):
            status = None
        if status is not None:
            version = status.get("plugin_version", "")
            try:
                previous = tuple(int(part) for part in version.split("."))
                installed = tuple(int(part) for part in __version__.split("."))
            except ValueError as error:
                raise RPCError("Неизвестная версия контроллера; работающий проект сохранён") from error
            if previous >= installed:
                return directory  # Старое окно MCP не понижает уже обновлённый контроллер.
            # Владелец сам проверяет фоновые ходы, доставки и вопросы перед остановкой.
            # Если он занят, auto_discover повторит попытку; связь остаётся у прежнего владельца.
            try:
                await request(directory, "shutdown")
            except RPCError as error:
                if "Контроллер занят" in str(error):
                    raise ControllerUpdatePending(str(error)) from error
                raise
            for _ in range(100):
                if not (directory / "control.sock").exists():
                    break
                await asyncio.sleep(0.05)
            else:
                raise RPCError("Старый контроллер ещё завершает работу; обновление будет повторено")
        private_directory(directory)
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        source_path = await asyncio.to_thread(Path(__file__).resolve)
        env["PYTHONPATH"] = str(source_path.parent.parent)
        args = [
            sys.executable,
            "-m",
            "bax_codex_light",
            "controller",
            "--project",
            str(self.project),
            "--registry",
            str(self.registry.path),
        ]
        if self.endpoint:
            args.extend(["--app-server", self.endpoint])
        fd = os.open(
            directory / "controller.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "a") as log:
            await asyncio.to_thread(
                subprocess.Popen,
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        async with asyncio.timeout(5):
            while True:
                try:
                    await request(directory, "status")
                    return directory
                except (OSError, TimeoutError):
                    await asyncio.sleep(0.05)

    async def status(self, thread_id: str = "") -> dict:
        if thread_id:
            if self.thread_id and self.thread_id != thread_id:
                raise ValueError("Этот MCP принадлежит другому разговору")
            if not self.thread_id:
                # Некоторые поверхности не передают ID в MCP. Проверяем явный ID
                # через app-server, не выбирая и не возобновляя другую сессию.
                app = AppServer(self.endpoint)
                try:
                    await app.open()
                    thread = await app.inspect(thread_id, self.project)
                    self.project = await asyncio.to_thread(Path(thread["cwd"]).resolve)
                    self.thread_id = thread_id
                finally:
                    await app.close()
        if self.project:
            try:
                result = await request(
                    runtime_path(self.project, self.registry, self.endpoint),
                    "status",
                    thread_id=self.thread_id,
                )
                result["caller_thread_id"] = self.thread_id or None
                result["needs_thread"] = not self.thread_id or result["thread_id"] != self.thread_id
                return result
            except (OSError, TimeoutError, RPCError) as error:
                self.error = str(error)
        return {
            "plugin_version": __version__,
            "project": str(self.project) if self.project else None,
            "thread_id": self.thread_id or None,
            "caller_thread_id": self.thread_id or None,
            "mode": "project",
            "connected": False,
            "state": "offline",
            "needs_thread": True,
            "needs_registration": False,
            "error": self.error,
            "error_code": self.error_code,
        }

    async def resolve_question(self, question_id: str, thread_id: str = "") -> dict:
        await self.status(thread_id)
        if not self.project or not self.thread_id:
            raise ValueError("Нужен точный CODEX_THREAD_ID текущего разговора")
        return await request(
            runtime_path(self.project, self.registry, self.endpoint),
            "resolve_question",
            thread_id=self.thread_id,
            question_id=question_id,
        )

    async def bind(self, thread_id: str) -> dict:
        if not thread_id.strip():
            raise ValueError("Нужен точный CODEX_THREAD_ID")
        if self.thread_id and self.thread_id != thread_id:
            raise ValueError("Этот MCP принадлежит другому разговору")
        self.thread_id = thread_id
        await self.discover()
        if self.error:
            raise RPCError(self.error)
        directory = await self.ensure_controller()
        await request(directory, "attach", thread_id=thread_id)
        return await self.status()

    async def connect(self, key: str, server: str = "wss://relay.baxassist.com/agent") -> dict:
        if self.project is None or not self.thread_id:
            raise ValueError("Сначала подключите этот разговор через bax_attach")
        directory = await self.ensure_controller()
        await request(directory, "connect", key=key, server=server)
        await request(directory, "attach", thread_id=self.thread_id)
        return await self.status()

    async def close(self) -> None:
        # EOF одного окна не останавливает контроллер, фоновые задачи и их вопросы.
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
