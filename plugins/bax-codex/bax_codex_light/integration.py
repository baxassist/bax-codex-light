"""Явная, обратимая настройка оболочки. Никогда не вызывается при обычном MCP startup."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, launcher

BEGIN = "# >>> Bax Codex integration >>>\n"
END = "# <<< Bax Codex integration <<<\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, name = tempfile.mkstemp(prefix=".bax-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def paths(shell: str | None = None) -> tuple[str, Path, Path]:
    shell = shell or Path(os.environ.get("SHELL", "")).name
    if os.name != "posix" or shell not in {"zsh", "bash"}:
        raise ValueError("Автонастройка поддерживает macOS/Linux с zsh или bash; файлы не изменены")
    home = Path.home()
    rc = Path(os.environ.get("ZDOTDIR", str(home))) / ".zshrc" if shell == "zsh" else home / ".bashrc"
    return shell, rc, home / ".bax/codex-integration"


def validate_directory(directory: Path) -> None:
    for parent in (directory.parent, directory):
        if parent.is_symlink():
            raise ValueError("Каталог интеграции не должен быть symlink")
    if directory.exists():
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise ValueError("Каталог интеграции должен быть приватным")
        for file in directory.iterdir():
            if file.is_symlink():
                raise ValueError("Файлы интеграции не должны быть symlink")


def read_rc(rc: Path) -> bytes:
    if rc.is_symlink() or (rc.exists() and not rc.is_file()):
        raise ValueError("Файл оболочки — symlink или не обычный файл; автоматическая правка отменена")
    if rc.exists() and (rc.stat().st_uid != os.getuid() or rc.stat().st_mode & 0o022):
        raise ValueError("Небезопасные права файла оболочки; автоматическая правка отменена")
    return rc.read_bytes() if rc.exists() else b""


def codex_binary() -> str:
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("Codex не найден в PATH. Сначала установите Codex CLI")
    result = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10, check=True)
    version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if not version or tuple(map(int, version.groups())) < (0, 166, 0):
        raise ValueError("Для автоматического подключения нужен Codex CLI 0.166.0 или новее")
    return os.path.abspath(codex)


def setup(*, apply: bool = False, shell: str | None = None) -> dict:
    shell, rc, directory = paths(shell)
    validate_directory(directory)
    codex = codex_binary()
    before = read_rc(rc)
    script = directory / "launcher.py"
    block = (
        BEGIN
        + "codex() { "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(script))
        + ' "$@"; }\n'
        + END
    ).encode()
    manifest_path = directory / "integration.json"
    previous = None
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if script.exists() and digest(script.read_bytes()) != previous["launcher_sha256"]:
            raise ValueError("Launcher Бакса изменён вручную; обновление отменено")
        if previous["rc"] != str(rc):
            raise ValueError("Уже настроена другая оболочка. Сначала отключите прежнюю интеграцию")
        if previous.get("active", True):
            old = previous["block"].encode()
            if before.count(old) != 1:
                raise ValueError("Блок Бакса изменён вручную; файлы сохранены, требуется проверка")
            if old.startswith(b"\n"):
                block = b"\n" + block
            after = before.replace(old, block, 1)
        else:
            previous = None
    if previous is None:
        if script.exists() and not manifest_path.exists():
            raise ValueError("Найден launcher без манифеста Бакса; файл сохранён")
        probe = subprocess.run(
            [shell, "-ic", "alias codex >/dev/null 2>&1 || typeset -f codex >/dev/null 2>&1"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            raise ValueError("Найдена существующая функция или alias codex в оболочке; замена запрещена")
        if (
            BEGIN.encode() in before
            or END.encode() in before
            or re.search(rb"(?m)^\s*(?:alias\s+codex[= ]|(?:function\s+)?codex\s*\(\s*\))", before)
        ):
            raise ValueError("Найдена существующая настройка codex. Автоматическая замена запрещена")
        block = (b"\n" if before and not before.endswith(b"\n") else b"") + block
        after = before + block
    result = {
        "applied": False,
        "shell": shell,
        "rc": str(rc),
        "codex": codex,
        "next_step": "Откройте новый терминал и выполните codex; разрешите hooks Бакса в /hooks",
    }
    if not apply:
        return {**result, "proposed_block": block.decode()}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("Каталог интеграции должен быть приватным и без symlink")
    with os.fdopen(
        os.open(directory / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read_rc(rc) != before:
            raise ValueError("Настройки оболочки изменились во время подготовки. Повторите настройку")
        backup = directory / ("shell-before-" + digest(before)[:16])
        if not previous:
            if backup.exists() and backup.read_bytes() != before:
                raise ValueError("Резервная копия изменена; файлы сохранены")
            if not backup.exists():
                atomic_write(backup, before)
        state = {
            "active": True,
            "version": __version__,
            "rc": str(rc),
            "codex": codex,
            "block": block.decode(),
            "launcher_sha256": digest(Path(launcher.__file__).read_bytes()),
            "backup_sha256": previous["backup_sha256"] if previous else digest(before),
            "rc_existed": previous["rc_existed"] if previous else rc.exists(),
        }
        old_files = {p: p.read_bytes() if p.exists() else None for p in (script, manifest_path)}
        try:
            atomic_write(script, Path(launcher.__file__).read_bytes())
            atomic_write(manifest_path, json.dumps(state).encode())
            atomic_write(rc, after, rc.stat().st_mode & 0o777 if rc.exists() else 0o600)
        except BaseException:
            for path, data in old_files.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data)
            raise
    return {**result, "applied": True}


def remove(*, apply: bool = False, shell: str | None = None) -> dict:
    _, rc, directory = paths(shell)
    validate_directory(directory)
    manifest_path = directory / "integration.json"
    if not manifest_path.exists():
        return {"removed": False, "reason": "not_installed"}
    state = json.loads(manifest_path.read_text())
    if not state.get("active", True):
        return {"removed": False, "reason": "not_installed"}
    if state["rc"] != str(rc):
        raise ValueError("Интеграция относится к другой оболочке")
    before = read_rc(rc)
    block = state["block"].encode()
    if before.count(block) != 1:
        raise ValueError("Блок Бакса изменён вручную; автоматическое удаление отменено")
    after = before.replace(block, b"", 1)
    if not apply:
        return {"removed": False, "rc": str(rc), "proposed": "Удалить только блок Бакса"}
    with os.fdopen(
        os.open(directory / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read_rc(rc) != before:
            raise ValueError("Файл оболочки изменился; удаление отменено")
        # Сохраняем launcher и backup: уже открытые терминалы продолжают работать.
        state["active"] = False
        atomic_write(manifest_path, json.dumps(state).encode())
        try:
            atomic_write(rc, after, rc.stat().st_mode & 0o777)
        except BaseException:
            state["active"] = True
            atomic_write(manifest_path, json.dumps(state).encode())
            raise
    return {
        "removed": True,
        "next_step": "В текущем терминале: unset -f codex; новые терминалы уже без интеграции",
    }


def auto_connect_enabled() -> bool:
    directory = Path.home() / ".bax/codex-integration"
    validate_directory(directory)
    manifest = directory / "integration.json"
    return not manifest.exists() or json.loads(manifest.read_text()).get("active", True)


def refresh_owned_launcher() -> None:
    """Обновление только ранее установленного собственного файла, без правки rc."""
    directory = Path.home() / ".bax/codex-integration"
    validate_directory(directory)
    manifest = directory / "integration.json"
    if not manifest.exists():
        return
    with os.fdopen(
        os.open(directory / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(manifest.read_text())
        if not state.get("active", True):
            return
        if tuple(map(int, state["version"].split("."))) >= tuple(map(int, __version__.split("."))):
            return
        script = directory / "launcher.py"
        before = script.read_bytes()
        if digest(before) != state["launcher_sha256"]:
            raise ValueError("Launcher Бакса изменён вручную; обновление отменено")
        content = Path(launcher.__file__).read_bytes()
        state.update(version=__version__, launcher_sha256=digest(content))
        atomic_write(script, content)
        try:
            atomic_write(manifest, json.dumps(state).encode())
        except BaseException:
            atomic_write(script, before)
            raise
