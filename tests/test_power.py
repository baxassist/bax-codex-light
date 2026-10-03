from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock

import pytest
from conftest import until
from test_integration import registration

from bax_codex_light.connection import FatalRelayError, Relay
from bax_codex_light.power import IdleSleepGuard


class Process:
    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.terminated = 0

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15
        self.exited.set()


async def test_guard_uses_exact_parent_pid_and_allows_display_sleep(monkeypatch):
    monkeypatch.setattr("bax_codex_light.power.sys.platform", "darwin")
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    guard = IdleSleepGuard()
    await guard.start()
    await guard.start()
    assert spawn.await_count == 1
    assert spawn.call_args.args == ("/usr/bin/caffeinate", "-i", "-w", str(os.getpid()))
    assert guard.status() == {"supported": True, "active": True, "error": ""}
    await guard.close()
    await guard.close()
    assert process.terminated == 1
    assert not guard.status()["active"]


async def test_linux_does_not_start_macos_sleep_guard(monkeypatch):
    monkeypatch.setattr("bax_codex_light.power.sys.platform", "linux")
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    guard = IdleSleepGuard()
    await guard.start()
    await guard.close()
    spawn.assert_not_called()
    assert guard.status() == {"supported": False, "active": False, "error": ""}


async def test_guard_permission_error_is_actionable_and_does_not_raise(monkeypatch):
    monkeypatch.setattr("bax_codex_light.power.sys.platform", "darwin")
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=PermissionError("private-error"))
    )
    guard = IdleSleepGuard()
    await guard.start()
    assert not guard.status()["active"]
    assert "/usr/bin/caffeinate" in guard.error
    assert "при сне связь" in guard.error
    assert "private-error" not in guard.error
    await guard.close()


async def test_unexpected_guard_exit_is_visible_and_can_recover(monkeypatch):
    monkeypatch.setattr("bax_codex_light.power.sys.platform", "darwin")
    first, recovered = Process(), Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[first, recovered]))
    guard = IdleSleepGuard()
    await guard.start()
    first.returncode = 1
    first.exited.set()
    await until(lambda: bool(guard.error))
    assert not guard.status()["active"]
    assert "код 1" in guard.error
    await guard.start()
    assert guard.status()["active"] and not guard.error
    await guard.close()


@pytest.mark.parametrize("fatal", [False, True])
async def test_relay_preserves_guard_during_retry_and_releases_on_exit(tmp_path, fatal):
    guard = IdleSleepGuard()
    guard.supported = False
    guard.start = AsyncMock()
    guard.close = AsyncMock()
    relay = Relay(registration("ws://localhost:1"), tmp_path, "current")
    relay.sleep_guard = guard

    async def session(_ready, _frame):
        await guard.start()
        if fatal:
            raise FatalRelayError("Регистрация отклонена", "unauthorized")
        raise ConnectionRefusedError()

    relay.session = session
    task = asyncio.create_task(relay.run(AsyncMock(), AsyncMock()))
    await until(lambda: bool(relay.error_code))
    guard.start.assert_awaited_once()
    if fatal:
        await task
    else:
        guard.close.assert_not_awaited()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    guard.close.assert_awaited_once()


@pytest.mark.skipif(sys.platform != "darwin", reason="Нужны настоящие assertions macOS")
async def test_real_macos_assertion_is_scoped_to_guard_lifetime():
    guard = IdleSleepGuard()

    async def assertions():
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/pmset", "-g", "assertions", stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        assert process.returncode == 0
        return stdout.decode()

    try:
        await guard.start()
        assert guard.process
        owner = f"pid {guard.process.pid}(caffeinate)"
        for _ in range(30):
            output = await assertions()
            if owner in output:
                break
            await asyncio.sleep(0.01)
        line = next(line for line in output.splitlines() if owner in line)
        assert "PreventUserIdleSystemSleep" in line
        assert "PreventUserIdleDisplaySleep" not in line
    finally:
        await guard.close()
    assert owner not in await assertions()
