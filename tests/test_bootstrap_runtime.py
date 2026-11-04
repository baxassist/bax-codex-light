"""Реальный запуск автономного venv; подменяется только установка закреплённых зависимостей."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Bootstrap uses POSIX fcntl on macOS/Linux")
BOOTSTRAP = Path(__file__).resolve().parents[1] / "plugins/bax-codex/bootstrap.py"


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("bax_bootstrap_real_venv_regression", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_fixture(tmp_path, monkeypatch, *, legacy_kind, install_exit=0):
    bootstrap = load_bootstrap()
    requirements = (BOOTSTRAP.parent / "requirements.txt").read_bytes()
    digest = hashlib.sha256(requirements).hexdigest()[:16]
    root = (tmp_path / "owned-runtime-root").resolve()
    # Историческая формула позволяет исходному коду дойти до реального сбоя
    # ensurepip до обращения к новому вспомогательному API.
    legacy = root / f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}-{digest}"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "previous-attempt.marker").write_bytes(b"preserve this earlier owned attempt\n")
    if legacy_kind == "ready":
        (legacy / ".ready").write_text(digest + "\n")
        # Исторический маркер готовности не разрешает принять старое окружение.
        # Этот неисполняемый маркер проверяется до любой попытки запуска.
        (legacy / "bin/python").write_bytes(b"preserve legacy interpreter sentinel\n")
    before_paths = {str(p.relative_to(legacy)) for p in legacy.rglob("*")}
    before_files = {str(p.relative_to(legacy)): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    monkeypatch.setenv("BAX_CODEX_PLUGIN_DATA", str(root))
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(sys, "argv", [str(BOOTSTRAP), "--prepare"])
    real_run = subprocess.run
    observed = {"installs": [], "ensurepip": []}

    def install_only(argv, *args, **kwargs):
        command = [str(value) for value in argv]
        if command[1:3] == ["-m", "pip"]:
            # Останавливаемся здесь, даже если штатная команда установки потеряет
            # защитный параметр: регрессия не должна обращаться к индексу пакетов.
            assert command[3:5] == ["--isolated", "install"]
            assert Path(command[0]).is_relative_to(root)
            assert "--require-hashes" in command and "--only-binary=:all:" in command
            assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
            assert command[-2:] == ["-r", str(BOOTSTRAP.parent / "requirements.txt")]
            observed["installs"].append(command)
            return subprocess.CompletedProcess(command, install_exit)
        if command[1:3] == ["-m", "ensurepip"]:
            observed["ensurepip"].append(command)
        # EnvBuilder, дочерний ensurepip и запуск Python остаются реальными.
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", install_only)
    return (
        bootstrap,
        root,
        requirements,
        legacy,
        before_paths,
        before_files,
        observed,
        real_run,
    )


def assert_legacy_preserved(legacy, before_paths, before_files):
    assert {str(p.relative_to(legacy)) for p in legacy.rglob("*")} == before_paths
    for name, body in before_files.items():
        path = legacy / name
        assert not path.is_symlink() and path.read_bytes() == body


def assert_real_venv_starts(python, real_run):
    result = real_run(
        [
            str(python),
            "-I",
            "-c",
            "import json,pip,sys; print(json.dumps({'prefix':sys.prefix, "
            "'base':sys.base_prefix, 'pip':pip.__file__}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    report = json.loads(result.stdout)
    assert Path(report["prefix"]).resolve() == python.parent.parent.resolve()
    assert report["prefix"] != report["base"]
    assert Path(report["pip"]).resolve().is_relative_to(python.parent.parent.resolve())


@pytest.mark.parametrize("legacy_kind", ["partial", "ready"])
def test_prepare_starts_real_venv_and_preserves_legacy_layout(tmp_path, monkeypatch, capsys, legacy_kind):
    bootstrap, root, requirements, legacy, paths, files, observed, real_run = prepare_fixture(
        tmp_path, monkeypatch, legacy_kind=legacy_kind
    )
    bootstrap.main()  # Исходный код действительно входит в EnvBuilder/ensurepip до нового helper.
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is True
    python = Path(output["python"])
    assert python.parent.parent != legacy  # Исторический маркер не исполняется.
    identity = bootstrap.runtime_identity(
        requirements, sys.implementation.cache_tag, sysconfig.get_platform()
    )
    assert python == root / identity["name"] / "bin/python"
    assert (python.parent.parent / ".ready").read_text() == identity["ready"]
    assert len(observed["ensurepip"]) == len(observed["installs"]) == 1
    assert_real_venv_starts(python, real_run)
    assert_legacy_preserved(legacy, paths, files)
    # Исходный публичный --prepare повторно использует только новое готовое окружение.
    bootstrap.main()
    assert json.loads(capsys.readouterr().out) == output
    assert len(observed["ensurepip"]) == len(observed["installs"]) == 1
    assert_legacy_preserved(legacy, paths, files)


def test_failed_dependency_install_never_marks_new_runtime_ready(tmp_path, monkeypatch, capsys):
    bootstrap, root, requirements, legacy, paths, files, observed, real_run = prepare_fixture(
        tmp_path, monkeypatch, legacy_kind="partial", install_exit=1
    )
    with pytest.raises(SystemExit) as stopped:
        bootstrap.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out.strip()
    assert "Не удалось установить библиотеки" in captured.err
    assert len(observed["ensurepip"]) == len(observed["installs"]) == 1
    python = Path(observed["installs"][0][0])
    assert python.parent.parent != legacy
    identity = bootstrap.runtime_identity(
        requirements, sys.implementation.cache_tag, sysconfig.get_platform()
    )
    assert python == root / identity["name"] / "bin/python"
    assert not (python.parent.parent / ".ready").exists()
    # Падает подменённая установка, а не скопированный или неработающий интерпретатор.
    assert_real_venv_starts(python, real_run)
    assert_legacy_preserved(legacy, paths, files)


@pytest.mark.parametrize("marker_kind", ["wrong_digest", "symlink"])
def test_ready_marker_must_match_runtime_identity(tmp_path, monkeypatch, marker_kind):
    bootstrap = load_bootstrap()
    requirements = (BOOTSTRAP.parent / "requirements.txt").read_bytes()
    identity = bootstrap.runtime_identity(
        requirements, sys.implementation.cache_tag, sysconfig.get_platform()
    )
    root = (tmp_path / "owned-runtime-root").resolve()
    runtime = root / identity["name"]
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").write_text("must not execute\n")
    ready = runtime / ".ready"
    if marker_kind == "symlink":
        target = tmp_path / "marker-target"
        target.write_text(identity["ready"])
        ready.symlink_to(target)
    else:
        ready.write_text("unrelated-dependencies\n")
    monkeypatch.setenv("BAX_CODEX_PLUGIN_DATA", str(root))
    with pytest.raises(RuntimeError, match="Маркер окружения"):
        bootstrap.runtime_python()
    assert (runtime / "bin/python").read_text() == "must not execute\n"
    if marker_kind == "symlink":
        assert ready.is_symlink() and target.read_text() == identity["ready"]
    else:
        assert ready.read_text() == "unrelated-dependencies\n"
