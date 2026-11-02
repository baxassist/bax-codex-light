"""Один настоящий STDIO MCP обслуживает вызовы родителя и fork; Codex RPC имитируется."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from test_project import ProjectApp

from bax_codex_light.controller import request, runtime_path
from bax_codex_light.registry import Registry


@asynccontextmanager
async def shared_mcp(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    async with fake.running() as endpoint:
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "bax_codex_light",
                "serve",
                "--auto-project",
                "--thread",
                "a",
                "--app-server",
                endpoint,
                "--registry",
                str(registry.path),
            ],
            env=env,
        )
        directory = runtime_path(tmp_path, registry, endpoint)
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("bax_attach", {"thread_id": "a"})
                assert not result.is_error
                yield fake, session
        finally:
            await request(directory, "shutdown")


def context(thread):
    # sessionId намеренно остаётся родительским, как у настоящего fork.
    return types.RequestParamsMeta(threadId=thread, sessionId="a")


async def test_fork_context_selects_without_argument_and_keeps_parent(tmp_path):
    async with shared_mcp(tmp_path) as (fake, session):
        before = len(fake.calls)
        status = await session.call_tool("bax_status", meta=context("b"))
        assert not status.is_error
        assert status.structured_content["caller_thread_id"] == "b"
        assert status.structured_content["thread_id"] == "a"
        assert status.structured_content["needs_thread"]
        assert not any(c["method"] == "thread/resume" for c in fake.calls[before:])
        attached = await session.call_tool("bax_attach", meta=context("b"))
        assert not attached.is_error
        assert attached.structured_content["thread_id"] == "b"
        results = await asyncio.gather(
            *(session.call_tool("bax_status", meta=context(tid)) for tid in ["a", "b"] * 4)
        )
        for result, tid in zip(results, ["a", "b"] * 4, strict=True):
            assert not result.is_error
            assert result.structured_content["caller_thread_id"] == tid
            assert result.structured_content["thread_id"] == "b"
        legacy = await session.call_tool("bax_status")
        assert legacy.structured_content["caller_thread_id"] == "a"
        assert legacy.structured_content["thread_id"] == "b"
        assert not any(
            c["method"] in {"turn/start", "turn/interrupt", "thread/archive", "thread/start"}
            for c in fake.calls
        )
        tools = (await session.list_tools()).tools
        assert all("ctx" not in t.input_schema.get("properties", {}) for t in tools)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("bax_status", {}),
        ("bax_attach", {}),
        ("bax_resolve_question", {"question_id": "q"}),
        ("bax_publish", {"text": "test", "publication_key": "test"}),
    ],
)
async def test_metadata_mismatch_rejected_before_rpc(tmp_path, tool, args):
    async with shared_mcp(tmp_path) as (fake, session):
        before = len(fake.calls)
        result = await session.call_tool(tool, {**args, "thread_id": "a"}, meta=context("b"))
        assert result.is_error
        assert "не совпадает" in result.model_dump_json()
        assert len(fake.calls) == before


@pytest.mark.parametrize("native", [None, "", 42])
async def test_invalid_metadata_never_falls_back_to_parent(tmp_path, native):
    async with shared_mcp(tmp_path) as (_fake, session):
        result = await session.call_tool("bax_attach", meta=context(native))
        assert result.is_error
        status = await session.call_tool("bax_status")
        assert status.structured_content["thread_id"] == "a"


async def test_foreign_project_and_writer_conflict_preserve_parent(tmp_path):
    async with shared_mcp(tmp_path) as (fake, session):
        foreign = await session.call_tool("bax_attach", meta=context("foreign"))
        assert foreign.is_error
        fake.writer_conflicts.add("b")
        refused = await session.call_tool("bax_attach", meta=context("b"))
        assert refused.is_error and "active writer" in refused.model_dump_json()
        status = await session.call_tool("bax_status", meta=context("a"))
        assert status.structured_content["thread_id"] == "a"
        assert not any(
            c["method"] in {"thread/start", "thread/fork", "turn/start", "turn/interrupt"} for c in fake.calls
        )
