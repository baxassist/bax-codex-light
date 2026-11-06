"""Hooks не зависят от модели и не запускают незагруженные разговоры."""

from __future__ import annotations

import asyncio
from pathlib import Path

from websockets.exceptions import WebSocketException

from .appserver import RPCError
from .controller import ProjectClient, request
from .integration import auto_connect_enabled, refresh_owned_launcher
from .registry import Registry


async def register_event(
    event: dict, registry: Registry, endpoint: str | None = None, *, readiness_seconds=30
) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Hook должен передать JSON-объект")
    if not await asyncio.to_thread(auto_connect_enabled):
        return {"registered": False, "reason": "disabled"}
    name = event.get("hook_event_name")
    if name not in {"SessionStart", "UserPromptSubmit"}:
        return {"registered": False, "reason": "ignored_event"}
    if name == "SessionStart" and event.get("source") not in {"startup", "resume", "fork", "clear"}:
        return {"registered": False, "reason": "ignored_source"}
    thread_id, cwd = event.get("session_id"), event.get("cwd")
    if not isinstance(thread_id, str) or not thread_id.strip() or not isinstance(cwd, str):
        raise ValueError("Hook не передал точный ID разговора и каталог")
    project = Path(cwd)
    if not project.is_absolute() or not await asyncio.to_thread(project.is_dir):
        raise ValueError("Каталог hook должен быть абсолютным существующим каталогом")
    if await asyncio.to_thread(registry.get, project) is None:
        return {"registered": False, "reason": "needs_registration"}
    await asyncio.to_thread(refresh_owned_launcher)
    client = ProjectClient(project, registry, thread_id, endpoint)
    try:
        async with asyncio.timeout(readiness_seconds):
            while True:
                try:
                    directory = await client.ensure_controller()
                    return await request(directory, "session_opened", thread_id=thread_id)
                except (RPCError, OSError, TimeoutError, WebSocketException):
                    # SessionStart может опередить публикацию thread; UserPromptSubmit
                    # повторяет только регистрацию, без повторной доставки задач.
                    await asyncio.sleep(0.5)
    except TimeoutError:
        return {"registered": False, "reason": "backend_unavailable"}
    finally:
        await client.close()
