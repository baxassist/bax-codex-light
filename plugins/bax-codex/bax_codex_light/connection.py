"""Транспорт Bax v1 на websockets: HMAC, проверка версии и переподключение."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import __version__
from .power import IdleSleepGuard
from .protocol import backoff, frame, parse, sign
from .registry import Registration

log = logging.getLogger(__name__)


class RelayResponseError(RuntimeError):
    def __init__(self, message: str, code: str = "relay_protocol"):
        super().__init__(message)
        self.code = code


class FatalRelayError(RelayResponseError):
    """Отказ релея, при котором повтор с тем же ключом не поможет."""


def network_error(error: Exception, service: str) -> tuple[str, str]:
    if isinstance(error, PermissionError):
        return "permission_denied", f"ОС запретила подключение к {service}. Проверьте разрешения запуска MCP."
    if isinstance(error, ssl.SSLCertVerificationError):
        return (
            "tls_certificate",
            f"Ошибка проверки TLS при подключении к {service}. Проверьте дату и сертификаты Python.",
        )
    if isinstance(error, TimeoutError):
        return "timeout", f"Подключение к {service} превысило время ожидания. Проверьте соединение."
    if isinstance(error, InvalidStatus):
        code = error.response.status_code
        return "http_rejected", f"WebSocket-подключение к {service} отклонено (HTTP {code})."
    if isinstance(error, ConnectionClosed):
        return "connection_closed", f"Соединение с сервером закрыто: {service}. Подключение будет повторено."
    if isinstance(error, OSError):
        return (
            "network_unavailable",
            f"Нет подключения к {service} ({type(error).__name__}). Проверьте сеть и адрес сервера.",
        )
    return (
        "connection_failed",
        f"Сбой подключения к {service} ({type(error).__name__}). Подключение будет повторено.",
    )


class Relay:
    def __init__(self, registration: Registration, project: Path, thread_id: str, *, keep_awake: bool = True):
        self.registration = registration
        self.project = project
        self.thread_id = thread_id
        self.ws: Any = None
        self.connected = False
        self.error = ""
        self.error_code = ""
        self.reconnect_attempt = 0
        self.retry_delay = 0
        self.last_connected_at: float | None = None
        self.last_disconnected_at: float | None = None
        self.last_close_code: int | None = None
        self.last_close_reason = ""
        self.sleep_guard = IdleSleepGuard()
        self.keep_awake_enabled = keep_awake

    async def set_keep_awake(self, enabled: bool) -> None:
        self.keep_awake_enabled = enabled
        if enabled and self.connected:
            await self.sleep_guard.start()
        elif not enabled:
            await self.sleep_guard.close()
            self.sleep_guard.error = ""

    async def send(self, frame_type: str, **fields: Any) -> bool:
        if not self.connected or self.ws is None:
            return False
        try:
            await self.ws.send(frame(frame_type, **fields))
            return True
        except Exception:
            return False

    async def _receive(self) -> dict:
        data = parse(await asyncio.wait_for(self.ws.recv(), 65))
        if data.get("v") != 1:
            raise FatalRelayError("Несовместимая версия протокола Бакса")
        if data.get("type") == "error":
            code = data.get("code", "unknown")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
                code = "unknown"
            message = data.get("message")
            if not isinstance(message, str) or not message.strip():
                message = {
                    "agent_busy": "Бакс уже подключён к другому разговору этого проекта. Закройте его мост.",
                    "unauthorized": "Регистрация отклонена. Скопируйте актуальную команду из Бакса.",
                    "key_claimed": "Ключ закреплён за другим компьютером. Выпустите отдельный ключ в Баксе.",
                    "wrong_engine": "Этот агент создан для другого движка.",
                    "unsupported_version": "Версии протокола несовместимы. Обновите плагин.",
                    "agent_taken": "Подключение занято другим разговором.",
                }.get(code, "Релей отклонил запрос")
            message = " ".join(message.replace(self.registration.secret, "[секрет скрыт]").split())[:1000]
            detail = f"{message} (код: {code})"
            if code in {"unauthorized", "key_claimed", "wrong_engine", "unsupported_version", "agent_taken"}:
                raise FatalRelayError(detail, code)
            raise RelayResponseError(detail, code)
        return data

    async def session(
        self, on_ready: Callable[[], Awaitable[None]], on_frame: Callable[[dict], Awaitable[None]]
    ) -> None:
        reg = self.registration
        async with connect(
            reg.server, max_size=16 * 1024 * 1024, open_timeout=20, ping_interval=20, ping_timeout=20
        ) as ws:
            self.ws = ws
            try:
                await ws.send(
                    frame(
                        "hello",
                        key_id=reg.key_id,
                        agent_version=__version__,
                        engine=reg.engine,
                        install_id=reg.install_id,
                        install_name=socket.gethostname(),
                        path=str(self.project),
                        session_id=self.thread_id,
                    )
                )
                challenge = await self._receive()
                if challenge.get("type") != "challenge":
                    raise FatalRelayError("Ожидался challenge от Бакса")
                await ws.send(
                    frame("auth", sign=sign(reg.secret, challenge["nonce"], challenge["ts"], reg.key_id))
                )
                ready = await self._receive()
                if ready.get("type") != "ready" or str(ready.get("agent")) != reg.agent:
                    raise FatalRelayError("Сервер вернул другого агента")
                self.connected = True
                self.reconnect_attempt = 0
                self.retry_delay = 0
                self.last_connected_at = time.time()
                self.error = ""
                self.error_code = ""
                if self.keep_awake_enabled:
                    await self.sleep_guard.start()
                await on_ready()
                while True:
                    message = await self._receive()
                    if message.get("type") == "ping":
                        await ws.send(frame("pong"))
                    elif message.get("type") != "pong":
                        await on_frame(message)
            finally:
                if self.connected:
                    self.last_disconnected_at = time.time()
                self.connected = False
                self.ws = None

    async def run(
        self, on_ready: Callable[[], Awaitable[None]], on_frame: Callable[[dict], Awaitable[None]]
    ) -> None:
        try:
            while True:
                try:
                    await self.session(on_ready, on_frame)
                except FatalRelayError as error:
                    self.error = str(error)
                    self.error_code = error.code
                    log.error("%s", self.error)
                    return
                except RelayResponseError as error:
                    self.error = str(error)
                    self.error_code = error.code
                    log.warning("%s; переподключение", self.error)
                except Exception as error:
                    self.error_code, self.error = network_error(error, "релею Бакса")
                    if isinstance(error, ConnectionClosed):
                        close = error.rcvd or error.sent
                        self.last_close_code = close.code if close else None
                        reason = close.reason if close else ""
                        self.last_close_reason = " ".join(
                            reason.replace(self.registration.secret, "[секрет скрыт]").split()
                        )[:200]
                    log.warning("%s", self.error)
                self.retry_delay = backoff(self.reconnect_attempt)
                await asyncio.sleep(self.retry_delay)
                self.retry_delay = 0
                self.reconnect_attempt += 1
        finally:
            await self.sleep_guard.close()

    async def close(self) -> None:
        await self.sleep_guard.close()
        if self.ws is not None:
            await self.ws.close()
