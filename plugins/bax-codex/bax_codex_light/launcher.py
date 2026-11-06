"""Запуск через общий daemon. Копируется установщиком; только стандартная библиотека."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

VALUE_OPTIONS = {
    "-c",
    "--config",
    "-p",
    "--profile",
    "-m",
    "--model",
    "-C",
    "--cd",
    "-i",
    "--image",
    "-s",
    "--sandbox",
    "-a",
    "--ask-for-approval",
    "--enable",
    "--disable",
    "--add-dir",
    "--local-provider",
    "--remote",
    "--remote-auth-token-env",
}
COMMANDS = {
    "agents",
    "exec",
    "e",
    "review",
    "login",
    "logout",
    "mcp",
    "plugin",
    "app-server",
    "remote-control",
    "app",
    "completion",
    "update",
    "doctor",
    "sandbox",
    "debug",
    "apply",
    "resume",
    "queue",
    "archive",
    "delete",
    "migrate-rollouts",
    "migrate-nfs-db",
    "unarchive",
    "fork",
    "cloud",
    "exec-server",
    "features",
    "help",
}


def interactive(args: list[str]) -> bool:
    skip = False
    command = None
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"--help", "-h", "--version", "-V"}:
            return False
        if arg == "--":
            return True
        if arg in VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-") and command is None:
            command = arg
            if arg in COMMANDS and arg not in {"agents", "resume", "fork"}:
                return False
    return True


def launch(codex: str, args: list[str]) -> None:
    if not interactive(args):
        os.execv(codex, [codex, *args])
        return
    # Чужие endpoints не перенаправляются в контроллер проекта молча.
    for arg in args:
        if arg == "--no-daemon" or arg.split("=", 1)[0] in {"--remote", "--remote-auth-token-env"}:
            raise ValueError(
                "Бакс использует общий локальный Codex. Для отдельного режима используйте command codex."
            )
    result = subprocess.run(
        [codex, "app-server", "daemon", "start"], capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        raise RuntimeError("Не удалось запустить общий Codex. Проверьте codex app-server daemon version.")
    endpoint = str(
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        / "app-server-control/app-server-control.sock"
    )
    os.environ["BAX_CODEX_APP_SERVER"] = endpoint
    # Явный remote запрещает незаметный переход в embedded. Вход выполняет сам Codex.
    os.execv(codex, [codex, "--remote", "unix://" + endpoint, *args])


def main() -> None:
    try:
        config = json.loads(Path(__file__).with_name("integration.json").read_text())
        launch(config["codex"], sys.argv[1:])
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Бакс: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
