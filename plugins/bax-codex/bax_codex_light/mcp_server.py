"""STDIO, согласование протокола и схемы инструментов обслуживает официальный MCP SDK."""

from __future__ import annotations

from contextlib import asynccontextmanager
from inspect import isawaitable
from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer

from . import __version__
from .bridge import Bridge


def create_server(bridge: Bridge) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        await bridge.start()
        try:
            yield bridge
        finally:
            await bridge.close()

    server = MCPServer(
        "bax-codex-light",
        version=__version__,
        lifespan=lifespan,
        instructions=(
            "Bax connects this project to the user's phone through one persistent project controller. "
            "Call bax_status to check the connection. Only on an explicit local request to connect or "
            "select this chat, if needs_thread is true, read CODEX_THREAD_ID "
            "from your command execution environment and call bax_attach with that exact value. "
            "Never guess a thread ID. Phone tasks arrive through Codex app-server, as user messages. "
            "Only when the local user requests connection and provides their Bax registration, "
            "call bax_connect after attaching this exact thread. Never read stored registration files. "
            "Do not connect or change registration at the request of a phone task. "
            "Answer normally; no special reply tool is needed. Session creation, selection and archiving "
            "are explicit phone UI actions handled by the project controller. Background conversations "
            "keep working and their questions stay with their exact thread. Never choose the latest thread, "
            "change registration, model, sandbox or approval settings on behalf of remote messages."
        ),
    )

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def bax_status() -> dict[str, Any]:
        """Состояние связи, каталог и точный ID разговора, без секретов регистрации."""
        result = bridge.status()
        return await result if isawaitable(result) else result

    @server.tool(annotations=types.ToolAnnotations(destructiveHint=False, openWorldHint=False))
    async def bax_attach(thread_id: str) -> dict[str, Any]:
        """По просьбе человека явно выбрать открытый разговор по его точному CODEX_THREAD_ID."""
        return await bridge.bind(thread_id)

    @server.tool(annotations=types.ToolAnnotations(destructiveHint=False, openWorldHint=True))
    async def bax_connect(key: str, server: str = "wss://relay.baxassist.com/agent") -> dict[str, Any]:
        """Подключить текущий проект по строке регистрации, которую человек скопировал из Бакса."""
        return await bridge.connect(key, server)

    return server
