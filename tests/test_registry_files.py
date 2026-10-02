import os
from uuid import uuid4

import pytest

from bax_codex_light import files
from bax_codex_light.registry import Registration, Registry, validate_url


def key():
    return f"{uuid4()}:{uuid4()}:test-secret-at-least-16-chars"


def test_registry_private_separate_and_idempotent(tmp_path):
    registry = Registry(tmp_path / ".bax/codex-light.json")
    reg = Registration.from_key(key(), "wss://bax.example/agent", compatibility=True)
    registry.put(tmp_path, reg)
    assert registry.get(tmp_path) == reg
    assert registry.path.stat().st_mode & 0o777 == 0o600
    assert not (registry.path.parent / "lite.json").exists()
    registry.put(tmp_path, Registration.from_key(f"{reg.agent}:{reg.key_id}:{reg.secret}", reg.server))
    assert registry.get(tmp_path).install_id == reg.install_id
    with pytest.raises(ValueError, match="другому проекту"):
        registry.put(tmp_path / "other", reg)
    registry.path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        registry.get(tmp_path)


@pytest.mark.parametrize("url", ["ws://bax.example/agent", "https://bax.example/agent", "wss://u:p@host/a"])
def test_insecure_url_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("url", ["ws://localhost:8010/agent", "ws://[::1]/agent", "wss://bax.example/agent"])
def test_allowed_url(url):
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        ".env",
        ".env.local",
        "secret.pem",
        ".codex/auth.json",
        "credentials.json",
        ".git/config",
    ],
)
def test_sensitive_files_rejected(path):
    assert not files.allowed(path)


def test_file_reads_no_links_binary_or_untracked(tmp_path):
    (tmp_path / "ok.txt").write_text("Привет")
    (tmp_path / "binary").write_bytes(b"abc\x00def")
    (tmp_path / "link").symlink_to(tmp_path / "ok.txt")
    (tmp_path / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "fifo").touch()
    os.unlink(tmp_path / "fifo")
    os.mkfifo(tmp_path / "fifo")
    paths = ["ok.txt", "binary", "link", "dirlink/ok.txt", "fifo"]
    assert files.read(tmp_path, "ok.txt", paths)["text"] == "Привет"
    for path in paths[1:] + ["untracked", "../ok.txt"]:
        assert "error" in files.read(tmp_path, path, paths)
