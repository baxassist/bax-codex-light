"""Собственный точный supervisor существующего native-теста каталога без модели."""

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SUPERVISOR = r"""
import json,os,signal,subprocess,sys,time
# A caught handler resets to the default in the exec child, unlike SIG_IGN.
# Keep THIS known session leader alive after original Codex exits, so its group
# can still be closed through current live-leader authority.
signal.signal(signal.SIGTERM, lambda *_args: None)
result_path=sys.argv[1]
completed=subprocess.run(sys.argv[2:],capture_output=True,text=True)
body=json.dumps({'returncode':completed.returncode,'stdout':completed.stdout,'stderr':completed.stderr})
fd=os.open(result_path+'.writing',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'w') as stream:
 stream.write(body);stream.flush();os.fsync(stream.fileno())
os.replace(result_path+'.writing',result_path)
while True:time.sleep(60)
"""


def _write(path, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        raw = json.dumps(value, sort_keys=True).encode() + b"\n"
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        identity = os.fstat(stream.fileno())
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "file_device": identity.st_dev,
        "file_inode": identity.st_ino,
    }


def _alive(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def _signal(process, bound, value):
    if not bound or process.poll() is not None:
        return False
    try:
        if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
            return False
        os.killpg(process.pid, value)
        return True
    except ProcessLookupError:
        return False


def run_native_catalog(command, *, env, timeout, directory, request):
    """Возвращаем исходный результат CLI; выход supervisor отдельно доказывает очистку."""
    assert request.node.originalname == "test_native_first_turn_waits_for_pending_plugin"
    directory = Path(directory)
    assert directory.is_absolute() and ".." not in directory.parts
    blocked = {
        "sec",
        "secrets",
        ".secrets",
        "нетрогать",
        ".bax",
        ".codex",
        ".claude",
        "registration",
        "registrations",
        "privatecache",
        "private-cache",
        "private_cache",
    }
    assert not any(
        part.casefold() in blocked or part.casefold().startswith(".env") for part in directory.parts
    )
    for parent in reversed((directory.parent, *directory.parent.parents)):
        assert not parent.is_symlink()
    assert directory.parent.resolve(strict=True) == directory.parent
    directory.mkdir(mode=0o700)
    run = str(uuid4())
    completion = directory / "original-codex-completion.json"
    record = {
        "schema": 1,
        "operation": "owned-native-catalog-supervisor",
        "run_id": run,
        "pytest_nodeid": request.node.nodeid,
        "owned_directory": str(directory),
        "started_utc": datetime.now(UTC).isoformat(),
        "started_monotonic_ns": time.monotonic_ns(),
        "binding_observed": False,
        "birth_written": False,
        "supervisor_program_sha256": hashlib.sha256(SUPERVISOR.encode()).hexdigest(),
        "scope": (
            "exact created supervisor session; original CLI result preserved; escaped sessions unproved"
        ),
    }
    result = primary = None
    errors = []
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR, str(completion), *command],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        record.update(owned_pid=process.pid, expected_pgid=process.pid, expected_sid=process.pid)
        assert os.getpgid(process.pid) == process.pid and os.getsid(process.pid) == process.pid
        record["binding_observed"] = True
        birth = _write(directory / ("owned-native-catalog-birth-" + run + ".json"), record)
        record["birth_written"] = True
        record["birth_receipt"] = birth
        request.node.user_properties.append(("owned_native_catalog_birth", json.dumps(birth, sort_keys=True)))
        while not completion.exists():
            assert process.poll() is None, "Owned supervisor exited before original CLI completion"
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.01)
        with os.fdopen(os.open(completion, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            identity = os.fstat(stream.fileno())
            assert (
                stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1 and identity.st_size <= 8 * 1024**2
            )
            raw = stream.read(8 * 1024**2 + 1)
        value = json.loads(raw)
        assert set(value) == {"returncode", "stdout", "stderr"}
        assert (
            type(value["returncode"]) is int
            and isinstance(value["stdout"], str)
            and isinstance(value["stderr"], str)
        )
        record["original_exit_code"] = value["returncode"]
        record["original_completion_sha256"] = hashlib.sha256(raw).hexdigest()
        result = subprocess.CompletedProcess(command, value["returncode"], value["stdout"], value["stderr"])
    except BaseException as error:
        primary = error
    finally:
        try:
            # TERM достигает исходных CLI/Git, пока лидер с обработчиком остаётся
            # живым. Последующий KILL всё ещё привязан к этому живому Popen.
            record["term_sent"] = _signal(process, record["binding_observed"], signal.SIGTERM)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                record["kill_sent"] = _signal(process, record["binding_observed"], signal.SIGKILL)
                process.wait(timeout=5)
        except BaseException as error:
            errors.append(type(error).__name__)
        absent = False
        try:
            for _ in range(20):
                if not _alive(process.pid):
                    absent = True
                    break
                time.sleep(0.05)
        except BaseException as error:
            errors.append(type(error).__name__)
        confirmed = (
            record["binding_observed"]
            and record["birth_written"]
            and process.poll() is not None
            and absent
            and not errors
        )
        record.update(
            ended_utc=datetime.now(UTC).isoformat(),
            ended_monotonic_ns=time.monotonic_ns(),
            supervisor_exit_code=process.poll(),
            group_absent=absent,
            cleanup_confirmed=confirmed,
            status="PASS" if confirmed else "UNKNOWN",
            error_types=errors,
            pytest_session_stop_requested=not confirmed,
        )
        try:
            final = _write(directory / ("owned-native-catalog-cleanup-" + run + ".json"), record)
            record["cleanup_receipt"] = final
        except BaseException as error:
            confirmed = False
            record.update(
                status="UNKNOWN",
                cleanup_confirmed=False,
                pytest_session_stop_requested=True,
            )
            errors.append(type(error).__name__)
        request.node.user_properties.append(
            ("owned_native_catalog_cleanup", json.dumps(record, sort_keys=True))
        )
        if not confirmed:
            message = "Owned native catalog supervisor cleanup UNKNOWN; stop before next test"
            if not request.session.shouldstop:
                request.session.shouldstop = message
            if primary is None:
                primary = AssertionError(message)
            else:
                primary.add_note(message)
    if primary is not None:
        raise primary
    return result
