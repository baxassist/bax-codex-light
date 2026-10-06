import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from conftest import FakeApp, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.server import serve

from bax_codex_light.controller import request, runtime_path
from bax_codex_light.protocol import sign
from bax_codex_light.registry import Registry


@pytest.mark.parametrize("packaged", [False, True])
async def test_connect_stdio_to_relay_to_exact_codex_thread(tmp_path, packaged, installed_plugin):
    runtime = os.environ.get("BAX_TEST_PLUGIN_RUNTIME")
    if packaged and not runtime:
        pytest.skip("Сначала bootstrap.py --prepare в BAX_TEST_PLUGIN_RUNTIME (без скачивания в тестах)")
    project = tmp_path / "Мой сайт"
    project.mkdir()
    fake = FakeApp(project)
    agent, key_id, secret = str(uuid4()), str(uuid4()), "private-test-secret-123456789"
    connected = asyncio.Event()
    sockets = []
    messages = []

    async def relay(ws):
        hello = json.loads(await ws.recv())
        assert hello["engine"] == "codex_light"
        assert hello["path"] == str(project)
        assert hello["session_id"].startswith("project:")
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "test", "ts": 1}))
        auth = json.loads(await ws.recv())
        assert auth["sign"] == sign(secret, "test", 1, key_id)
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": agent}))
        sockets.append(ws)
        connected.set()
        await ws.send(json.dumps({"v": 1, "type": "subscribe"}))
        async for payload in ws:
            message = json.loads(payload)
            messages.append(message)
            if message.get("type") == "feed.publish":
                result = {"published": True, "event_id": "published-event"}
                if message["publication_key"] == "disabled":
                    result = {
                        "published": False,
                        "reason": "stream_disabled",
                        "error": {"code": "stream_disabled", "message": "Уведомления выключены"},
                    }
                await ws.send(json.dumps({"v": 1, "type": "feed.published", "rid": message["rid"], **result}))

    async with fake.running() as endpoint, serve(relay, "127.0.0.1", 0) as relay_server:
        relay_url = f"ws://127.0.0.1:{relay_server.sockets[0].getsockname()[1]}/agent"
        args = ["--app-server", endpoint, "--registry", str(tmp_path / "registry.json")]
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
        if packaged:
            env["BAX_CODEX_PLUGIN_DATA"] = runtime
        params = StdioServerParameters(
            command=installed_plugin["command"] if packaged else sys.executable,
            args=[*installed_plugin["args"], *args]
            if packaged
            else ["-m", "bax_codex_light", "serve", "--auto-project", *args],
            cwd=installed_plugin["cwd"] if packaged else None,
            env=env,
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert not (await session.call_tool("bax_attach", {"thread_id": "current"})).is_error
            result = await session.call_tool(
                "bax_connect", {"key": f"{agent}:{key_id}:{secret}", "server": relay_url}
            )
            assert not result.is_error
            assert secret not in result.model_dump_json()
            await asyncio.wait_for(connected.wait(), 5)
            await until(lambda: any(m.get("text") == "Последний ответ" for m in messages), timeout=5)
            publication = await session.call_tool(
                "bax_publish",
                {
                    "text": "Плагин 0.5.9 опубликован: добавлена явная запись результата в Поток",
                    "publication_key": "plugin-0.5.9",
                    "thread_id": "current",
                },
            )
            assert not publication.is_error
            assert publication.structured_content["published"] is True
            assert publication.structured_content["event_id"] == "published-event"
            assert (
                next(m for m in messages if m["type"] == "feed.publish")["publication_key"] == "plugin-0.5.9"
            )
            rejected = await session.call_tool(
                "bax_publish",
                {"text": "Запрещённая публикация", "publication_key": "disabled", "thread_id": "current"},
            )
            assert rejected.is_error
            assert "stream_disabled" in rejected.model_dump_json()
            # Отказ не закрывает канал и не мешает следующей задаче.
            still_connected = await session.call_tool("bax_status", {"thread_id": "current"})
            assert not still_connected.is_error and still_connected.structured_content["connected"]
            stored = Registry(tmp_path / "registry.json").get(project)
            assert stored.agent == agent
            assert stored.secret == secret
            assert not (stored and (tmp_path / "registry.json").stat().st_mode & 0o077)
            await sockets[0].send(
                json.dumps({"v": 1, "type": "run", "session": "current", "text": "Добавь страницу контактов"})
            )
            await asyncio.wait_for(fake.started.wait(), 5)
            start = next(c for c in fake.calls if c["method"] == "turn/start")
            assert start["params"]["threadId"] == "current"
            assert "model" not in start["params"]
            assert "approvalPolicy" not in start["params"]
            fake.items.append(
                {
                    "item": {
                        "id": "u2",
                        "clientId": start["params"]["clientUserMessageId"],
                        "type": "userMessage",
                        "content": start["params"]["input"],
                    },
                    "turnId": "turn",
                    "startedAtMs": 1,
                    "completedAtMs": 2,
                }
            )
            await fake.emit(
                "item/completed",
                {"threadId": "current", "turnId": "turn", "item": fake.items[-1]["item"], "completedAtMs": 2},
            )
            fake.state = "idle"
            await fake.emit("thread/status/changed", {"threadId": "current", "status": {"type": "idle"}})
            await fake.emit(
                "item/completed",
                {
                    "threadId": "current",
                    "turnId": "turn",
                    "completedAtMs": 3,
                    "item": {
                        "id": "issuer",
                        "type": "agentMessage",
                        "delivery": "async",
                        "text": "",
                        "questions": [{"title": "Пришли Issuer ID"}],
                    },
                },
            )
            await until(lambda: any(m.get("type") == "question" for m in messages))
            card = next(m for m in messages if m.get("type") == "question")
            current = await session.call_tool("bax_status", {})
            assert card["question_id"] in current.model_dump_json()
            calls_before = len(fake.calls)
            resolved = await session.call_tool("bax_resolve_question", {"question_id": card["question_id"]})
            assert not resolved.is_error
            await until(lambda: any(m.get("type") == "question.resolved" for m in messages))
            closed = next(m for m in messages if m.get("type") == "question.resolved")
            assert closed["question_id"] == card["question_id"] and closed["session"] == "current"
            assert not any(
                c["method"] in {"turn/start", "turn/steer", "turn/interrupt"}
                for c in fake.calls[calls_before:]
            )
        directory = runtime_path(project, Registry(tmp_path / "registry.json"), endpoint)
        status = await request(directory, "status")
        assert status["connected"] is True  # Закрытие MCP сохраняет канал проекта.

        async def idle():
            state = await request(directory, "status")
            return not state["unconfirmed"] and state["state"] == "ready"

        for _ in range(250):
            if await idle():
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("Контроллер не подтвердил доставку и готовность")
        await request(directory, "shutdown")
        await until(lambda: not fake.connections)
