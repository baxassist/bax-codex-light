"""Пустые сессии проверяются на настоящем изолированном Codex, без задания модели."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_project import RelayStub

from bax_codex_light.appserver import AppServer
from bax_codex_light.project import ProjectController
from bax_codex_light.registry import Registry


@pytest.mark.parametrize("cli", ["codex", os.environ.get("BAX_TEST_CURRENT_CODEX", "")])
async def test_native_empty_sessions_survive_selection_reconnect_and_archive(tmp_path, request, cli):
    if sys.platform not in {"darwin", "linux"} or not cli or not shutil.which(cli):
        pytest.skip("Нужен Codex CLI 0.160.0")
    version = await asyncio.create_subprocess_exec(
        cli,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(version.communicate(), 10)
    expected = b"0.160.0" if cli == "codex" else b"0.166.0"
    if expected not in stdout:
        pytest.skip(f"Нативная проверка закреплена на Codex CLI {expected.decode()}")
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
            cli,
            "app-server",
            "--listen",
            f"unix://{endpoint}",
            env={**os.environ, "CODEX_HOME": str(home)},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        app = AppServer(endpoint, timeout=10)
        cleanup = {
            "schema": 1,
            "operation": "owned-native-session-cleanup",
            "run_id": str(uuid4()),
            "owned_pid": process.pid,
            "expected_pgid": process.pid,
            "expected_sid": process.pid,
            "started_utc": datetime.now(UTC).isoformat(),
            "started_monotonic_ns": time.monotonic_ns(),
            "binding_observed": False,
            "term_attempted": False,
            "group_absent": False,
            "status": "UNKNOWN",
            "scope": "exact-created-process-group-only; escaped-descendants-unproved",
        }
        try:
            # start_new_session создаёт SID/PGID=PID. Сразу связываем собственного
            # живого ребёнка; PID/группы другого теста или HOME не принимаются.
            assert os.getpgid(process.pid) == process.pid
            assert os.getsid(process.pid) == process.pid
            cleanup["binding_observed"] = True
            # Долговечная квитанция создания записывается внутри try/finally:
            # сбой записи/fsync/ENOSPC не мешает очистить только что созданный процесс.
            # Root может прочитать только известный pytest workdir этого прогона, если
            # внешний timeout не позволит завершить JUnit.
            assert not tmp_path.is_symlink()
            owned_directory = tmp_path.resolve(strict=True)
            birth_name = f"owned-native-birth-{cleanup['run_id']}.json"
            birth_path = owned_directory / birth_name
            directory_fd = os.open(owned_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                directory_identity = os.fstat(directory_fd)
                assert stat.S_ISDIR(directory_identity.st_mode)
                birth = {
                    "schema": 1,
                    "operation": "owned-native-session-birth",
                    "run_id": cleanup["run_id"],
                    "pytest_nodeid": request.node.nodeid,
                    "owned_tmp_path": str(owned_directory),
                    "directory_device": directory_identity.st_dev,
                    "directory_inode": directory_identity.st_ino,
                    "owned_uid": os.getuid(),
                    "owned_pid": process.pid,
                    "expected_pgid": cleanup["expected_pgid"],
                    "expected_sid": cleanup["expected_sid"],
                    "binding_observed": True,
                    "started_utc": cleanup["started_utc"],
                    "started_monotonic_ns": cleanup["started_monotonic_ns"],
                    "scope": cleanup["scope"],
                }
                birth_bytes = (json.dumps(birth, sort_keys=True) + "\n").encode()
                descriptor = os.open(
                    birth_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(descriptor, "wb") as birth_file:
                    identity = os.fstat(birth_file.fileno())
                    assert stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1
                    birth_file.write(birth_bytes)
                    birth_file.flush()
                    os.fsync(birth_file.fileno())
                cleanup["birth_receipt"] = {
                    "path": str(birth_path),
                    "sha256": hashlib.sha256(birth_bytes).hexdigest(),
                    "file_device": identity.st_dev,
                    "file_inode": identity.st_ino,
                }
                request.node.user_properties.append(
                    (
                        "owned_native_birth",
                        json.dumps(
                            {"run_id": cleanup["run_id"], **cleanup["birth_receipt"]},
                            sort_keys=True,
                        ),
                    )
                )
            finally:
                os.close(directory_fd)
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
            observer = ProjectController(project, registry, root / "hook-state.json", endpoint)
            observer.app = app
            observer.relay = RelayStub(None)
            await observer.session_opened(first)
            await observer.session_opened(second)
            await observer.session_opened(second)
            assert observer.selected == first
            assert set(observer.sessions) == {first, second}
            await owner.select(first, rid="return")
            assert owner.selected == first and owner.sessions[first].state == "ready"
            assert {row["session"] for row in owner.relay.frames[-1]["items"]} == {
                first,
                second,
            }, {tid: item.get("source") for tid, item in owner.catalog.items()}
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
            primary_error = sys.exception()
            cleanup_errors = []
            try:
                await asyncio.wait_for(app.close(), 5)
                cleanup["app_closed"] = True
            except BaseException as error:
                cleanup["app_closed"] = False
                cleanup_errors.append(error)
            try:
                # Отправляем исходный TERM, только пока собственный лидер жив, а его
                # наблюдаемые SID/PGID соответствуют созданию. После его выхода
                # возможной повторно использованной группе сигнал не отправляется: только проверка.
                if cleanup["binding_observed"] and process.returncode is None:
                    try:
                        assert os.getpgid(process.pid) == cleanup["expected_pgid"]
                        assert os.getsid(process.pid) == cleanup["expected_sid"]
                        cleanup["term_attempted"] = True
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(process.wait(), 10)
            except BaseException as error:
                cleanup_errors.append(error)
            # Старый цикл молча завершался после двадцати проверок живой группы.
            # Отсутствие должно быть реально подтверждено; без KILL и общего поиска процессов.
            for _ in range(20):
                try:
                    os.killpg(cleanup["expected_pgid"], 0)
                except ProcessLookupError:
                    cleanup["group_absent"] = True
                    break
                except OSError as error:
                    cleanup_errors.append(error)
                    break
                await asyncio.sleep(0.05)
            cleanup["leader_exit_code"] = process.returncode
            cleanup["ended_utc"] = datetime.now(UTC).isoformat()
            cleanup["ended_monotonic_ns"] = time.monotonic_ns()
            cleanup["error_types"] = [type(error).__name__ for error in cleanup_errors]
            confirmed = (
                cleanup["binding_observed"]
                and cleanup.get("birth_receipt") is not None
                and cleanup["app_closed"]
                and process.returncode is not None
                and cleanup["group_absent"]
                and not cleanup_errors
            )
            cleanup["cleanup_confirmed"] = confirmed
            cleanup["status"] = "PASS" if confirmed else "UNKNOWN"
            cleanup["pytest_session_stop_requested"] = not confirmed
            # Публичные user_properties pytest копируются в свойства JUnit именно
            # этого testcase, в том числе при падении исходного assertion.
            request.node.user_properties.append(("owned_native_cleanup", json.dumps(cleanup, sort_keys=True)))
            if not confirmed:
                message = (
                    "Owned native cleanup UNKNOWN; inspect JUnit owned_native_cleanup and stop next producer"
                )
                # Завершаем этот тест и отчёт; затем pytest останавливается до следующего теста.
                if not request.session.shouldstop:
                    request.session.shouldstop = message
                if primary_error is not None:
                    primary_error.add_note(message)
                else:
                    raise AssertionError(message)
