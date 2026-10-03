from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
from conftest import FakeApp, until
from test_bridge import approval, bridge
from test_integration import registration
from websockets.asyncio.server import serve

from bax_codex_light.approvals import permission_details, permission_profile
from bax_codex_light.appserver import AppServer
from bax_codex_light.bridge import Bridge
from bax_codex_light.registry import Registry


def access_request(project: Path, *, thread_id="current", turn_id="turn", request_id=88):
    event = approval(thread_id, request_id, "item/permissions/requestApproval")
    event["params"].update(
        cwd=str(project),
        turnId=turn_id,
        permissions={
            "network": {"enabled": True},
            "fileSystem": {"read": [str(project / "read")], "write": [str(project / "write")]},
        },
    )
    return event


@pytest.mark.parametrize("verdict", ["allow", "deny"])
async def test_phone_grants_only_requested_access_for_current_turn(tmp_path, verdict):
    b = bridge(tmp_path)
    event = access_request(tmp_path)
    await b.on_event(event)
    assert not b.app.responses
    card = b.relay.sent[-1]
    assert card["kind"] == "permission"
    assert "текущего хода" in card["text"]
    assert str(tmp_path / "write") in card["input"].values()
    assert "Сеть" in card["input"]
    qid = card["question_id"]
    with pytest.raises(ValueError):
        await b.answer({"question_id": qid, "verdict": verdict, "remember": True})
    await b.answer(
        {
            "question_id": qid,
            "verdict": verdict,
            "scope": "session",
            "permissions": {"fileSystem": {"write": ["/"]}},
        }
    )
    assert b.app.responses == [
        (88, {"permissions": event["params"]["permissions"] if verdict == "allow" else {}, "scope": "turn"})
    ]
    with pytest.raises(ValueError):
        await b.answer({"question_id": qid, "verdict": "allow"})


@pytest.mark.parametrize("foreign", ["thread", "turn"])
async def test_foreign_permission_prompt_is_ignored(tmp_path, foreign):
    b = bridge(tmp_path)
    b.turn_id = "turn"
    event = access_request(
        tmp_path,
        thread_id="other" if foreign == "thread" else "current",
        turn_id="old" if foreign == "turn" else "turn",
    )
    await b.on_event(event)
    assert not b.questions and not b.app.responses and not b.relay.sent


async def test_terminal_resolution_and_turn_completion_close_phone_card(tmp_path):
    b = bridge(tmp_path)
    b.turn_id = "turn"
    for method in ("serverRequest/resolved", "turn/completed"):
        await b.on_event(access_request(tmp_path))
        qid = next(iter(b.questions))
        params = {"threadId": "current", "requestId": 88}
        if method == "turn/completed":
            params = {"threadId": "current", "turn": {"id": "turn", "status": "completed"}}
        await b.on_event({"method": method, "params": params})
        assert not b.questions
        assert any(f.get("question_id") == qid and f["type"] == "question.resolved" for f in b.relay.sent)
        with pytest.raises(ValueError):
            await b.answer({"question_id": qid, "verdict": "allow"})
    assert not b.app.responses


async def test_late_answer_for_another_turn_cannot_grant_access(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(access_request(tmp_path))
    qid = next(iter(b.questions))
    b.turn_id = "new"
    with pytest.raises(ValueError, match="завершённому ходу"):
        await b.answer({"question_id": qid, "verdict": "allow"})
    assert not b.app.responses


@pytest.mark.parametrize(
    "permissions",
    [
        None,
        {"network": {"enabled": "yes"}},
        {"network": {"enabled": True, "unknown": "private"}},
        {"fileSystem": {"read": ["relative/path"]}},
        {"fileSystem": {"entries": [{"access": "write", "path": {"type": "path", "path": "relative"}}]}},
        {"unknown": {"write": ["/"]}},
    ],
)
async def test_invalid_permissions_stay_in_native_codex(tmp_path, permissions):
    b = bridge(tmp_path)
    event = access_request(tmp_path)
    event["params"]["permissions"] = permissions
    await b.on_event(event)
    assert not b.questions and not b.requests and not b.app.responses
    assert b.relay.sent[-1]["code"] == "unsupported_permissions"
    assert "private" not in b.relay.sent[-1]["message"]


def test_sdk_filesystem_entries_and_scope_are_visible():
    profile = permission_profile(
        {
            "fileSystem": {
                "entries": [
                    {"access": "read", "path": {"type": "path", "path": "/tmp/read"}},
                    {"access": "write", "path": {"type": "special", "value": {"kind": "root"}}},
                    {"access": "read", "path": {"type": "glob_pattern", "pattern": "/tmp/*.txt"}},
                ]
            }
        }
    )
    details = permission_details(profile)
    assert details["Чтение 1"] == "/tmp/read"
    assert details["Запись 2"] == "Все файлы (/)"
    assert details["Чтение 3"] == "По шаблону: /tmp/*.txt"


async def test_managed_network_approval_shows_host_and_protocol(tmp_path):
    b = bridge(tmp_path)
    event = approval()
    event["params"]["networkApprovalContext"] = {"host": "example.test", "protocol": "https"}
    await b.on_event(event)
    card = b.relay.sent[-1]
    assert card["kind"] == "permission" and card["tool"] == "Доступ к сети"
    assert card["input"] == {"Адрес": "example.test", "Протокол": "https"}


@pytest.mark.parametrize("verdict", ["allow", "deny"])
async def test_permission_round_trip_over_native_codex_and_relay_websockets(tmp_path, verdict):
    fake = FakeApp(tmp_path)
    mobile, sockets = [], []
    reg = None

    async def relay(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": reg.agent}))
        sockets.append(ws)
        async for raw in ws:
            mobile.append(json.loads(raw))

    async with fake.running() as endpoint, serve(relay, "127.0.0.1", 0) as server:
        reg = registration(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        registry = Registry(tmp_path / "registry.json")
        registry.put(tmp_path, reg)
        b = Bridge(tmp_path, registry, "current", endpoint)
        try:
            await b.start()
            await until(lambda: bool(sockets))
            params = access_request(tmp_path)["params"]
            await fake.emit("item/permissions/requestApproval", params, request_id=88)
            await until(lambda: any(frame.get("type") == "question" for frame in mobile))
            card = next(frame for frame in mobile if frame["type"] == "question")
            await sockets[0].send(
                json.dumps({"v": 1, "type": "answer", "question_id": card["question_id"], "verdict": verdict})
            )
            await until(lambda: bool(fake.responses))
            assert fake.responses[-1] == {
                "id": 88,
                "result": {
                    "permissions": params["permissions"] if verdict == "allow" else {},
                    "scope": "turn",
                },
            }
            assert b.status()["approvals"] == {"policy": "on-request", "reviewer": "user", "manual": True}
            assert not any(
                call["method"] in {"config/value/write", "config/batchWrite"} for call in fake.calls
            )
        finally:
            await b.close()


@pytest.mark.parametrize("reviewer", ["user", "auto_review"])
async def test_attach_reports_review_mode_without_overriding_it(tmp_path, reviewer):
    fake = FakeApp(tmp_path)
    fake.approvals_reviewer = reviewer
    app = AppServer()
    async with fake.running() as endpoint:
        app.endpoint = endpoint
        try:
            await app.open()
            await app.attach("current", tmp_path)
            assert app.approvals["manual"] is (reviewer == "user")
            assert app.approvals["reviewer"] == reviewer
            resume = next(call for call in fake.calls if call["method"] == "thread/resume")
            assert resume["params"] == {"threadId": "current", "excludeTurns": True}
        finally:
            await app.close()


@pytest.fixture
def native_approval_schemas(tmp_path):
    if not shutil.which("codex"):
        pytest.skip("Нужен закреплённый CLI Codex 0.160.0")
    home = tmp_path / "isolated-codex"
    home.mkdir()
    env = dict(os.environ, CODEX_HOME=str(home))
    version = subprocess.run(["codex", "--version"], env=env, capture_output=True, text=True, check=True)
    if "0.160.0" not in version.stdout:
        pytest.skip("Проверка wire-схем закреплена на CLI 0.160.0")
    output = tmp_path / "schema"
    subprocess.run(
        ["codex", "app-server", "generate-json-schema", "--out", str(output)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return {
        name: json.loads((output / f"PermissionsRequestApproval{name}.json").read_text())
        for name in ("Params", "Response")
    }


@pytest.mark.parametrize("verdict", ["allow", "deny"])
async def test_permission_reply_matches_native_0160_schema(tmp_path, native_approval_schemas, verdict):
    b = bridge(tmp_path)
    event = access_request(tmp_path)
    jsonschema.validate(event["params"], native_approval_schemas["Params"])
    await b.on_event(event)
    qid = next(iter(b.questions))
    await b.answer({"question_id": qid, "verdict": verdict})
    result = b.app.responses[-1][1]
    jsonschema.validate(result, native_approval_schemas["Response"])
    assert result["scope"] == "turn"
