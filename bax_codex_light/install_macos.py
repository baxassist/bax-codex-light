"""Установка собственного каталога через штатный CLI Codex, без правки чужих настроек."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def find_codex() -> Path:
    candidates = [
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ]
    located = shutil.which("codex")
    if located:
        candidates.insert(0, Path(located))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError("Сначала установите и откройте приложение Codex, затем повторите подключение Бакса")


def install(app: Path, *, root: Path | None = None, codex: Path | None = None) -> dict:
    app = app.resolve()
    binary = app / "Contents/Resources/agent/bax-codex-light"
    source = app / "Contents/Resources/plugin"
    if not binary.is_file() or not (source / ".codex-plugin/plugin.json").is_file():
        raise ValueError("Приложение Бакса неполное. Скачайте установщик заново")
    codex = codex or find_codex()
    root = root or Path.home() / "Library/Application Support/Bax Codex/marketplace"
    plugin = root / "plugins/bax-codex"
    plugin.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, plugin, dirs_exist_ok=True)
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bax_codex": {
                        "command": str(binary),
                        "args": ["serve", "--auto-project"],
                        "startup_timeout_sec": 30,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    catalog = root / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps(
            {
                "name": "baxassist",
                "interface": {"displayName": "Бакс"},
                "plugins": [
                    {
                        "name": "bax-codex",
                        "source": {"source": "local", "path": "./plugins/bax-codex"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                        "category": "Productivity",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )

    def run(*args):
        result = subprocess.run([str(codex), *args], capture_output=True, text=True, timeout=90)
        if result.returncode:
            # CLI не получает кодов/ключей. Ошибка полезна для диагностики несовместимой версии.
            raise ValueError("Codex не установил плагин: " + (result.stderr or result.stdout)[-1200:])
        return result.stdout

    run("plugin", "marketplace", "add", str(root), "--json")
    run("plugin", "add", "bax-codex@baxassist", "--json")
    # Убираем только прежнюю запись нашего пакета, после успешной установки нового.
    previous = subprocess.run(
        [str(codex), "mcp", "get", "bax", "--json"], capture_output=True, text=True, timeout=15
    )
    if previous.returncode == 0:
        try:
            command = json.loads(previous.stdout).get("transport", {}).get("command", "")
        except (ValueError, AttributeError):
            command = ""
        if Path(command).name == "bax-codex-light":
            run("mcp", "remove", "bax")
    return {"installed": True, "plugin": "bax-codex@baxassist", "restart_required": True}
