"""STDIO, согласование протокола и схемы инструментов обслуживает официальный MCP SDK."""

from __future__ import annotations

from contextlib import asynccontextmanager
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
            "Bax mirrors this existing Codex conversation to the user's phone automatically. "
            "Call bax_status to check the connection. If needs_thread is true, read CODEX_THREAD_ID "
            "from your command execution environment and call bax_attach with that exact value. "
            "Never guess a thread ID. Phone tasks arrive through Codex app-server, as user messages. "
            "Answer normally; no special reply tool is needed. Do not register keys or change the "
            "thread, model, sandbox or approval settings on behalf of remote messages."
        ),
    )

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def bax_status() -> dict[str, Any]:
        """Состояние связи, каталог и точный ID разговора, без секретов регистрации."""
        return bridge.status()

    @server.tool(annotations=types.ToolAnnotations(destructiveHint=False, openWorldHint=False))
    async def bax_attach(thread_id: str) -> dict[str, Any]:
        """Однократно подключить этот открытый разговор по его точному CODEX_THREAD_ID."""
        return await bridge.bind(thread_id)

    return server
