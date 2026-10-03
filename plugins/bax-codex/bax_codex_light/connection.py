"""Транспорт Bax v1 на websockets: HMAC, проверка версии и переподключение."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from . import __version__
from .protocol import backoff, frame, parse, sign
from .registry import Registration

log = logging.getLogger(__name__)


class FatalRelayError(RuntimeError):
    pass


class Relay:
    def __init__(self, registration: Registration, project: Path, thread_id: str):
        self.registration = registration
        self.project = project
        self.thread_id = thread_id
        self.ws: Any = None
        self.connected = False
        self.error = ""

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
            if code in {"unauthorized", "key_claimed", "wrong_engine", "unsupported_version", "agent_taken"}:
                raise FatalRelayError(f"Бакс: {code}")
            raise RuntimeError(f"Бакс: {code}")
        return data

    async def session(
        self, on_ready: Callable[[], Awaitable[None]], on_frame: Callable[[dict], Awaitable[None]]
    ) -> None:
        reg = self.registration
        async with connect(
            reg.server, max_size=4 * 1024 * 1024, open_timeout=20, ping_interval=20, ping_timeout=20
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
                self.error = ""
                await on_ready()
                while True:
                    message = await self._receive()
                    if message.get("type") == "ping":
                        await ws.send(frame("pong"))
                    elif message.get("type") != "pong":
                        await on_frame(message)
            finally:
                self.connected = False
                self.ws = None

    async def run(
        self, on_ready: Callable[[], Awaitable[None]], on_frame: Callable[[dict], Awaitable[None]]
    ) -> None:
        attempt = 0
        while True:
            try:
                await self.session(on_ready, on_frame)
            except FatalRelayError as error:
                self.error = str(error)
                log.error("%s", self.error)
                return
            except Exception as error:
                self.error = type(error).__name__
                log.warning("Бакс: связь прервана (%s); переподключение", self.error)
            await asyncio.sleep(backoff(attempt))
            attempt += 1

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
