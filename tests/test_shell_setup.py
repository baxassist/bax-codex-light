import json
import os
import subprocess
from pathlib import Path

import pytest

from bax_codex_light import integration, launcher


@pytest.fixture
def shell_home(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    binary = tmp_path / "codex"
    binary.write_text('#!/bin/sh\nprintf "codex-cli 0.166.0\\n"\n')
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return home


@pytest.mark.parametrize("original", [b"", b"# mine\n", b"# no newline"])
def test_preview_install_repeat_remove_preserves_user_changes(shell_home, original):
    rc = shell_home / ".zshrc"
    rc.write_bytes(original)
    assert not integration.setup()["applied"]
    assert rc.read_bytes() == original
    assert not (shell_home / ".bax").exists()
    integration.setup(apply=True)
    installed = rc.read_bytes()
    integration.setup(apply=True)
    assert rc.read_bytes() == installed
    rc.write_bytes(installed + b"# later user change\n")
    integration.remove(apply=True)
    assert rc.read_bytes() == original + b"# later user change\n"
    assert not integration.remove(apply=True)["removed"]
    # Already-open shells retain a functioning launcher and its manifest.
    assert (shell_home / ".bax/codex-integration/integration.json").is_file()
    integration.setup(apply=True)
    assert rc.read_bytes().count(integration.BEGIN.encode()) == 1


def test_real_shell_function_quotes_paths_and_arguments(shell_home):
    integration.setup(apply=True)
    state = shell_home / ".bax/codex-integration/integration.json"
    config = json.loads(state.read_text())
    binary = Path(config["codex"])
    binary.write_text("#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n")
    env = dict(os.environ, ZDOTDIR=str(shell_home))
    result = subprocess.run(
        ["zsh", "-ic", "codex exec 'a b' '$literal'"], env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == ["exec", "a b", "$literal"]


def test_modified_block_and_existing_alias_are_not_overwritten(shell_home):
    rc = shell_home / ".zshrc"
    rc.write_text("alias codex='my-codex'\n")
    with pytest.raises(ValueError, match="существующая"):
        integration.setup(apply=True)
    rc.write_text("# safe\n")
    integration.setup(apply=True)
    rc.write_text(rc.read_text().replace('"$@"', '"different"'))
    before = rc.read_bytes()
    for action in (integration.setup, integration.remove):
        with pytest.raises(ValueError, match="вручную"):
            action(apply=True)
        assert rc.read_bytes() == before


def test_symlink_and_unsupported_shell_leave_files_untouched(shell_home):
    target = shell_home / "target"
    target.write_text("keep")
    (shell_home / ".zshrc").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        integration.setup(apply=True)
    with pytest.raises(ValueError, match="поддерживает"):
        integration.setup(apply=True, shell="fish")
    assert target.read_text() == "keep"


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], True),
        (["resume", "exact-id"], True),
        (["fork"], True),
        (["agents"], True),
        (["exec", "hello"], False),
        (["--help"], False),
        (["plugin", "install", "x"], False),
        (["-c", "x=exec", "hello"], True),
        (["--model", "exec", "resume", "id"], True),
        (["login"], False),
        (["logout"], False),
        (["--", "exec"], True),
    ],
)
def test_command_classification(args, expected):
    assert launcher.interactive(args) is expected


def test_launch_never_falls_back_and_preserves_arguments(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(launcher.os, "execv", lambda exe, args: calls.append(args))
    launcher.launch("/bin/codex", ["resume", "exact", "--model", "chosen"])
    assert calls == [
        ["/bin/codex", "app-server", "daemon", "start"],
        [
            "/bin/codex",
            "--remote",
            "unix://" + str(tmp_path / "app-server-control/app-server-control.sock"),
            "resume",
            "exact",
            "--model",
            "chosen",
        ],
    ]
    assert os.environ["BAX_CODEX_APP_SERVER"].endswith("app-server-control.sock")
    calls.clear()
    monkeypatch.setattr(launcher.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1))
    with pytest.raises(RuntimeError, match="Не удалось"):
        launcher.launch("/bin/codex", [])
    assert not calls
    for args in (["--no-daemon"], ["--remote", "ws://example.invalid"]):
        with pytest.raises(ValueError):
            launcher.launch("/bin/codex", args)
    assert not calls


def test_shell_write_failure_restores_integration_files(shell_home, monkeypatch):
    rc = shell_home / ".zshrc"
    rc.write_text("# original\n")
    original = integration.atomic_write

    def fail_rc(path, data, mode=0o600):
        if path == rc:
            raise OSError("injected disk error")
        original(path, data, mode)

    monkeypatch.setattr(integration, "atomic_write", fail_rc)
    with pytest.raises(OSError):
        integration.setup(apply=True)
    assert rc.read_text() == "# original\n"
    assert not (shell_home / ".bax/codex-integration/integration.json").exists()
    monkeypatch.setattr(integration, "atomic_write", original)
    integration.setup(apply=True)
    manifest = shell_home / ".bax/codex-integration/integration.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(integration, "atomic_write", fail_rc)
    with pytest.raises(OSError):
        integration.remove(apply=True)
    assert manifest.read_bytes() == before


def test_manual_launcher_changes_are_preserved(shell_home):
    integration.setup(apply=True)
    script = shell_home / ".bax/codex-integration/launcher.py"
    script.write_text("# edited by user\n")
    with pytest.raises(ValueError, match="вручную"):
        integration.setup(apply=True)
    assert script.read_text() == "# edited by user\n"


def test_hook_upgrade_preserves_rc_and_never_downgrades(shell_home, monkeypatch):
    integration.setup(apply=True)
    rc = shell_home / ".zshrc"
    before = rc.read_bytes()
    directory = shell_home / ".bax/codex-integration"
    manifest = directory / "integration.json"
    monkeypatch.setattr(integration, "__version__", "0.5.41")
    integration.refresh_owned_launcher()
    assert json.loads(manifest.read_text())["version"] == "0.5.41"
    monkeypatch.setattr(integration, "__version__", "0.5.39")
    integration.refresh_owned_launcher()
    assert json.loads(manifest.read_text())["version"] == "0.5.41"
    assert rc.read_bytes() == before
    integration.remove(apply=True)
    assert not integration.auto_connect_enabled()


def test_alias_in_sourced_file_and_orphan_launcher_are_not_overwritten(shell_home):
    extra = shell_home / "extra.zsh"
    extra.write_text("alias codex='custom-codex'\n")
    rc = shell_home / ".zshrc"
    rc.write_text(f"source '{extra}'\n")
    before = rc.read_bytes()
    with pytest.raises(ValueError, match="alias"):
        integration.setup(apply=True)
    assert rc.read_bytes() == before
    rc.write_text("# safe\n")
    directory = shell_home / ".bax/codex-integration"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "launcher.py").write_text("# not ours")
    with pytest.raises(ValueError, match="манифеста"):
        integration.setup(apply=True)
    assert (directory / "launcher.py").read_text() == "# not ours"
