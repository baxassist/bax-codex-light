"""Первый запуск на установленном Python: отдельный venv, затем официальный MCP SDK.

Этот файл запускается и старым системным Python Mac, чтобы найти Python 3.11+.
В stdout до запуска MCP ничего не пишем — он принадлежит протоколу MCP.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import venv
from pathlib import Path

MIN_PYTHON = (3, 11)
ROOT = Path(__file__).resolve().parent


def ensure_python() -> None:
    if sys.version_info >= MIN_PYTHON:
        return
    candidates = [
        shutil.which("python3"),
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run(
            [candidate, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if probe.returncode == 0:
            os.execv(candidate, [candidate, str(ROOT / "bootstrap.py"), *sys.argv[1:]])
    raise RuntimeError("Для плагина Бакс нужен установленный Python 3.11 или новее")


def runtime_python() -> Path:
    started = time.monotonic()
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()[:16]
    data = Path(os.environ.get("BAX_CODEX_PLUGIN_DATA", str(Path.home() / ".bax/codex-runtime")))
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data.is_symlink():
        raise RuntimeError("Каталог окружения Бакса не должен быть symlink")
    name = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}-{digest}"
    runtime = data / name
    python = runtime / "bin/python"
    ready = runtime / ".ready"
    lock_fd = os.open(data / (name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        deadline = time.monotonic() + 210
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Первый запуск Бакса ещё идёт. Повторите подключение MCP") from None
                time.sleep(0.1)
        if runtime.is_symlink():
            raise RuntimeError("Окружение Бакса не должно быть symlink")
        if ready.is_file() and python.is_file():
            print(
                f"Бакс: готовое окружение ({time.monotonic() - started:.2f} с).", file=sys.stderr, flush=True
            )
            return python
        print("Бакс: первый запуск, устанавливаю библиотеки плагина…", file=sys.stderr, flush=True)
        runtime.mkdir(mode=0o700, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(runtime)
        # Закреплены все зависимости и хеши. Пользовательские библиотеки не меняются.
        result = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "--only-binary=:all:",
                "--index-url",
                "https://pypi.org/simple",
                "-r",
                str(requirements),
            ],
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError("Не удалось установить библиотеки. Проверьте интернет и переподключите MCP")
        ready.write_text(digest + "\n")
        print(
            f"Бакс: библиотеки установлены ({time.monotonic() - started:.2f} с).", file=sys.stderr, flush=True
        )
    return python


def main() -> None:
    try:
        ensure_python()
        python = runtime_python()
        if sys.argv[1:] == ["--prepare"]:
            print(json.dumps({"ready": True, "python": str(python)}))
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONNOUSERSITE"] = "1"
        print("Бакс: запускаю MCP; подключение к Codex проверяется в фоне.", file=sys.stderr, flush=True)
        os.execve(str(python), [str(python), "-m", "bax_codex_light", *sys.argv[1:]], env)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Бакс: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
