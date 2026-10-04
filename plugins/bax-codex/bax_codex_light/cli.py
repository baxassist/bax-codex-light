"""Ручные команды для разработки; подключение плагина доступно через MCP."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .appserver import AppServer
from .bridge import Bridge
from .controller import ProjectClient, serve_controller
from .mcp_server import create_server
from .registry import Registration, Registry


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Связь Бакса с уже открытой сессией Codex")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("connect", "status", "doctor", "serve", "controller"):
        child = commands.add_parser(command)
        child.add_argument(
            "--project", type=Path, default=Path(os.environ.get("BAX_CODEX_PROJECT", os.getcwd()))
        )
        child.add_argument("--registry", type=Path, default=None)
        if command in {"serve", "doctor", "controller"}:
            child.add_argument(
                "--thread",
                default=os.environ.get("BAX_CODEX_THREAD_ID", os.environ.get("CODEX_THREAD_ID", "")),
            )
            child.add_argument("--app-server", default=os.environ.get("BAX_CODEX_APP_SERVER"))
        if command == "serve":
            child.add_argument(
                "--conversation-only", action="store_true", help="Прежний мост одного разговора"
            )
            child.add_argument(
                "--auto-project",
                action="store_true",
                help="Взять каталог из точного открытого разговора при attach",
            )
        if command == "doctor":
            child.add_argument(
                "--check-history", action="store_true", help="Проверить чтение истории без вывода текста"
            )
        if command == "connect":
            child.add_argument("--server", required=True, help="wss://host/agent")
            child.add_argument(
                "--key-stdin", action="store_true", help="Прочитать ключ из stdin вместо prompt"
            )
            child.add_argument(
                "--compat-claude-lite",
                action="store_true",
                help="Подключиться к выделенному агенту нынешнего типа Claude Code Lite",
            )
    return root


async def doctor(args) -> dict:
    if not args.thread:
        raise ValueError("Не задан CODEX_THREAD_ID; передайте точный ID через --thread")
    app = AppServer(args.app_server)
    try:
        await app.open()
        thread = await app.inspect(args.thread, args.project)
        history_ok = None
        if args.check_history:
            await app.items(args.thread, None, 1)
            history_ok = True
        return {
            "ok": True,
            "project": str(args.project.resolve()),
            "thread_id": thread["id"],
            "state": thread["status"]["type"],
            "cli_version": thread["cliVersion"],
            "sdk_version": "0.160.0",
            "app_server": app.endpoint,
            "registered": Registry(args.registry).get(args.project) is not None,
            "history_ok": history_ok,
        }
    finally:
        await app.close()


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    try:
        if not args.project.is_dir():
            raise ValueError("Каталог проекта не существует")
        registry = Registry(args.registry)
        if args.command == "connect":
            key = sys.stdin.readline().strip() if args.key_stdin else getpass.getpass("Ключ агента Бакса: ")
            registration = Registration.from_key(key, args.server, compatibility=args.compat_claude_lite)
            registry.put(args.project, registration)
            print(
                json.dumps(
                    {
                        "registered": True,
                        "project": str(args.project.resolve()),
                        "engine": registration.engine,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "status":
            registration = registry.get(args.project)
            print(
                json.dumps(
                    {
                        "registered": bool(registration),
                        "project": str(args.project.resolve()),
                        "engine": registration.engine if registration else None,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "doctor":
            print(json.dumps(asyncio.run(doctor(args)), ensure_ascii=False, indent=2))
        elif args.command == "controller":
            asyncio.run(serve_controller(args.project, registry, args.app_server))
        else:
            client = Bridge if args.conversation_only else ProjectClient
            bridge = client(
                None if args.auto_project else args.project, registry, args.thread, args.app_server
            )
            asyncio.run(create_server(bridge).run_stdio_async())
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"bax-codex-light: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
