"""Пустые сессии проверяются на настоящем изолированном Codex, без задания модели."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import pytest
from test_project import RelayStub

from bax_codex_light.appserver import AppServer
from bax_codex_light.project import ProjectController
from bax_codex_light.registry import Registry


async def test_native_empty_sessions_survive_selection_reconnect_and_archive(tmp_path):
    if sys.platform not in {"darwin", "linux"} or not shutil.which("codex"):
        pytest.skip("Нужен Codex CLI 0.160.0")
    version = await asyncio.create_subprocess_exec(
        "codex", "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    stdout, _ = await asyncio.wait_for(version.communicate(), 10)
    if b"0.160.0" not in stdout:
        pytest.skip("Нативная проверка закреплена на Codex CLI 0.160.0")
    # macOS ограничивает длину Unix socket; штатный pytest tmp_path длиннее лимита.
    with (
        tempfile.TemporaryDirectory(prefix="bax-native-", dir="/tmp") as folder,
        (tmp_path / "app.log").open("wb") as log,
    ):
        root = Path(folder)
        home = root / "isolated-codex"
        project = (root / "project").resolve()
        home.mkdir()
        project.mkdir()
        endpoint = str(root / "app.sock")
        state = root / "controller.json"
        registry = Registry(root / "registry.json")
        process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            f"unix://{endpoint}",
            env={**os.environ, "CODEX_HOME": str(home)},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        app = AppServer(endpoint, timeout=10)
        try:
            async with asyncio.timeout(15):
                while not await asyncio.to_thread(Path(endpoint).exists):
                    assert process.returncode is None, "Изолированный Codex остановился"
                    await asyncio.sleep(0.05)
            owner = ProjectController(project, registry, state, endpoint)
            owner.app = app
            owner.relay = RelayStub(None)
            await app.open(owner.on_event)
            await owner.select("new", rid="first")
            first = owner.selected
            assert first and owner.sessions[first].state == "ready"
            assert not await owner.sessions[first].history.page()
            assert await asyncio.to_thread(Path(owner.catalog[first]["path"]).is_file)
            await owner.select("new", rid="second")
            second = owner.selected
            assert second != first
            await owner.select(first, rid="return")
            assert owner.selected == first and owner.sessions[first].state == "ready"
            assert {row["session"] for row in owner.relay.frames[-1]["items"]} == {first, second}, {
                tid: item.get("source") for tid, item in owner.catalog.items()
            }
            assert not any(frame["type"] in {"message", "error"} for frame in owner.relay.frames)
            previous_config = app.configurations[first]
            await app.close()

            app = AppServer(endpoint, timeout=10)
            restored = ProjectController(project, registry, state, endpoint)
            restored.app = app
            restored.relay = RelayStub(None)
            await app.open(restored.on_event)
            assert restored.selected == first
            await restored.select(first)
            assert restored.sessions[first].state == "ready"
            assert not await restored.sessions[first].history.page()
            for field in (
                "model",
                "modelProvider",
                "approvalPolicy",
                "approvalsReviewer",
                "sandbox",
                "reasoningEffort",
            ):
                assert app.configurations[first].get(field) == previous_config.get(field)
            await restored.close_session(first, "archive")
            assert not restored.selected
            await restored.select(first, archived=True, rid="restore")
            assert restored.selected == first and restored.sessions[first].state == "ready"
            assert not await restored.sessions[first].history.page()
            assert not any(frame["type"] in {"message", "error"} for frame in restored.relay.frames)
        finally:
            await app.close()
            # Завершаем также только дочерние процессы этого тестового Codex,
            # чтобы фоновое клонирование bundled plugins не переживало временный HOME.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 10)
            for _ in range(20):
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
