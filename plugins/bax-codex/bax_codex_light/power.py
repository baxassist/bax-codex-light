"""Не даём macOS усыпить подключённый мост; экран и ручной сон не затрагиваем."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

log = logging.getLogger(__name__)


class IdleSleepGuard:
    def __init__(self):
        self.supported = sys.platform == "darwin"
        self.process: asyncio.subprocess.Process | None = None
        self.watcher: asyncio.Task | None = None
        self.error = ""

    def status(self) -> dict:
        return {
            "supported": self.supported,
            "active": bool(self.process and self.process.returncode is None),
            "error": self.error,
        }

    async def start(self) -> None:
        if not self.supported or self.status()["active"]:
            return
        await self.close()
        self.error = ""
        try:
            # -i запрещает только автоматический сон системы; -w снимает защиту
            # даже при аварийном завершении MCP. -d и -u не используем.
            self.process = await asyncio.create_subprocess_exec(
                "/usr/bin/caffeinate",
                "-i",
                "-w",
                str(os.getpid()),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.watcher = asyncio.create_task(self._watch(), name="bax-idle-sleep-guard")
        except OSError:
            self.error = (
                "Не удалось включить защиту от автоматического сна macOS. "
                "Проверьте доступность /usr/bin/caffeinate; при сне связь с Баксом прервётся."
            )
            log.warning("%s", self.error)

    async def _watch(self) -> None:
        code = await self.process.wait()
        self.error = (
            f"Защита от автоматического сна macOS завершилась (код {code}). "
            "Переподключите мост; при сне связь с Баксом прервётся."
        )
        log.warning("%s", self.error)

    async def close(self) -> None:
        if self.watcher:
            self.watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watcher
            self.watcher = None
        process, self.process = self.process, None
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
