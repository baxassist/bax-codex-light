from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from conftest import FakeApp, entry, until
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.server import serve

from bax_codex_light.bridge import Bridge
from bax_codex_light.connection import FatalRelayError, Relay
from bax_codex_light.protocol import sign
from bax_codex_light.registry import Registration, Registry


def registration(server):
    return Registration(
        str(uuid4()), str(uuid4()), "test-secret-long-enough", server, str(uuid4()), "claude_code_lite"
    )


async def test_relay_hmac_thread_history_run_and_live_answer(tmp_path):
    fake = FakeApp(tmp_path)
    mobile = []
    engine_socket = []
    registered = None

    async def relay_handler(ws):
        hello = json.loads(await ws.recv())
        assert hello["engine"] == "claude_code_lite"
        assert hello["session_id"] == "current"
        assert hello["path"] == str(tmp_path)
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "nonce", "ts": 123}))
        auth = json.loads(await ws.recv())
        assert auth["sign"] == sign(registered.secret, "nonce", 123, registered.key_id)
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": registered.agent, "user": "user"}))
        engine_socket.append(ws)
        await ws.send(json.dumps({"v": 1, "type": "subscribe"}))
        async for raw in ws:
            mobile.append(json.loads(raw))

    async with fake.running() as endpoint, serve(relay_handler, "127.0.0.1", 0) as server:
        registered = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/agent")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, registered)
        b = Bridge(tmp_path, registry, "current", endpoint)
        try:
            await b.start()
            await until(lambda: any(f.get("text") == "Последний ответ" for f in mobile))
            await engine_socket[0].send(json.dumps({"v": 1, "type": "run", "text": "Задача с телефона"}))
            await asyncio.wait_for(fake.started.wait(), 2)
            start = next(c for c in fake.calls if c["method"] == "turn/start")
            assert start["params"] == {
                "threadId": "current",
                "clientUserMessageId": start["params"]["clientUserMessageId"],
                "input": [{"type": "text", "text": "Задача с телефона"}],
            }
            await fake.emit(
                "item/agentMessage/delta",
                {
                    "threadId": "current",
                    "turnId": "turn",
                    "itemId": "live",
                    "delta": "Ответ",
                },
            )
            await until(lambda: any(f.get("chunk") == "Ответ" for f in mobile))
            await fake.emit(
                "item/commandExecution/requestApproval",
                {
                    "threadId": "current",
                    "turnId": "turn",
                    "itemId": "cmd",
                    "startedAtMs": 1,
                    "command": "npm test",
                    "reason": "Доступ",
                },
                request_id=88,
            )
            await until(lambda: any(f["type"] == "question" for f in mobile))
            qid = next(f["question_id"] for f in mobile if f["type"] == "question")
            await engine_socket[0].send(
                json.dumps({"v": 1, "type": "answer", "question_id": qid, "verdict": "deny"})
            )
            await until(lambda: bool(fake.responses))
            assert fake.responses[-1] == {"id": 88, "result": {"decision": "decline"}}
        finally:
            await b.close()
        await until(lambda: not fake.connections)


async def test_relay_rejects_identity_mismatch(tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": "other"}))

    async def unused(*args):
        pytest.fail("Must not reach ready")

    async with serve(handler, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        relay = Relay(reg, tmp_path, "current")
        with pytest.raises(FatalRelayError, match="другого агента"):
            await relay.session(unused, unused)
        assert not relay.connected


async def test_busy_comment_is_steered_immediately_and_history_reconnect_does_not_resend(tmp_path):
    fake = FakeApp(tmp_path)
    fake.state = "active"
    mobile = []
    sockets = []
    reg = None
    question = "Какую модель ты используешь для тестов?"

    async def relay_handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        sockets.append(ws)
        await ws.send(json.dumps({"v": 1, "type": "subscribe"}))
        async for raw in ws:
            mobile.append(json.loads(raw))

    def questions():
        return [frame for frame in mobile if frame.get("kind") == "user" and frame.get("text") == question]

    async with fake.running() as endpoint, serve(relay_handler, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/agent")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, reg)
        b = Bridge(tmp_path, registry, "current", endpoint)
        try:
            await b.start()
            await until(lambda: any(frame.get("type") == "background" for frame in mobile))
            await sockets[-1].send(json.dumps({"v": 1, "type": "run", "text": question}))
            await until(lambda: len(questions()) == 1)
            await until(lambda: any(call["method"] == "turn/steer" for call in fake.calls))
            preview_id = questions()[0]["id"]
            await sockets[-1].send(json.dumps({"v": 1, "type": "subscribe"}))
            await until(lambda: len(questions()) == 2)
            assert not fake.started.is_set()

            await sockets[-1].close()
            await until(lambda: len(sockets) == 2 and len(questions()) == 3)
            assert {frame["id"] for frame in questions()} == {preview_id}
            assert b.status()["queued"] == 0
            steers = [call for call in fake.calls if call["method"] == "turn/steer"]
            assert len(steers) == 1
            assert steers[0]["params"]["threadId"] == "current"
            assert steers[0]["params"]["expectedTurnId"] == "turn"
            assert not any(call["method"] in {"turn/start", "thread/start"} for call in fake.calls)
            client_id = steers[0]["params"]["clientUserMessageId"]
            native = entry("native-question", "userMessage", question)
            native["item"]["clientId"] = client_id
            fake.items.insert(0, native)
            await fake.emit(
                "item/completed",
                {"threadId": "current", "turnId": "turn", "completedAtMs": 2, "item": native["item"]},
            )
            await until(lambda: not b.outbox)
            assert questions()[-1]["id"] == preview_id

            marker = len(mobile)
            await sockets[-1].send(json.dumps({"v": 1, "type": "subscribe"}))
            await until(lambda: any(frame.get("type") == "background" for frame in mobile[marker:]))
            restored = [
                frame
                for frame in mobile[marker:]
                if frame.get("kind") == "user" and frame.get("text") == question
            ]
            assert len(restored) == 1
            assert not any(call["method"] == "turn/start" for call in fake.calls)
            assert len([call for call in fake.calls if call["method"] == "turn/steer"]) == 1

            answer = entry("comment-answer", text="Актуальное уточнение учтено")
            fake.items.insert(0, answer)
            await fake.emit(
                "item/completed",
                {
                    "threadId": "current",
                    "turnId": "turn",
                    "completedAtMs": 3,
                    "item": answer["item"],
                },
            )
            await until(lambda: any(frame.get("text") == "Актуальное уточнение учтено" for frame in mobile))
            marker = len(mobile)
            await sockets[-1].send(json.dumps({"v": 1, "type": "subscribe"}))
            await until(lambda: any(frame.get("type") == "background" for frame in mobile[marker:]))
            rows = [frame for frame in mobile[marker:] if frame.get("type") == "message"]
            assert rows[-1]["text"] == "Актуальное уточнение учтено"
            assert rows[-2]["text"] == question
        finally:
            await b.close()


async def test_official_mcp_stdio_client_and_clean_eof(tmp_path):
    # Проверка MCP без endpoint Codex, ключей, вызовов модели и глобального конфига.
    env = {k: v for k, v in os.environ.items() if k not in {"CODEX_THREAD_ID", "BAX_CODEX_THREAD_ID"}}
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "bax_codex_light",
            "serve",
            "--project",
            str(tmp_path),
            "--registry",
            str(tmp_path / "registry.json"),
        ],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialize = await session.initialize()
        assert initialize.server_info.name == "bax-codex-light"
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "bax_status",
            "bax_attach",
            "bax_connect",
            "bax_resolve_question",
            "bax_publish",
        }
        response = await session.call_tool("bax_status", {})
        assert not response.is_error
        assert response.structured_content["needs_thread"] is True
        assert response.structured_content["connected"] is False
        assert "secret" not in json.dumps(response.structured_content)
