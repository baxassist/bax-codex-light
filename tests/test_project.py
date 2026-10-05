from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from conftest import FakeApp, entry, thread, until
from test_integration import registration

from bax_codex_light.appserver import AppServer
from bax_codex_light.bridge import Submission
from bax_codex_light.power import IdleSleepGuard
from bax_codex_light.project import ProjectController
from bax_codex_light.registry import Registry


class ProjectApp(FakeApp):
    def __init__(self, project):
        super().__init__(project)
        self.threads = {tid: thread(project, thread_id=tid) for tid in ("a", "b")}
        self.threads["foreign"] = thread(project.parent, thread_id="foreign")
        self.archived = set()
        self.created = 0
        self.terminals = {"a": [], "b": []}
        self.terminated = True
        self.unmaterialized = set()
        self.empty = set()
        self.name_error = False
        self.hide_empty = False
        self.settings = {
            "cwd": str(project),
            "model": "chosen-model",
            "modelProvider": "openai",
            "reasoningEffort": "high",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandbox": {
                "type": "workspaceWrite",
                "writableRoots": [str(project)],
                "networkAccess": False,
                "excludeSlashTmp": True,
                "excludeTmpdirEnvVar": True,
            },
        }
        self.mismatch_model = False
        for value in self.threads.values():
            value.update(model="chosen-model", reasoningEffort="high")
        self.models = [
            {
                "id": model,
                "model": model,
                "displayName": model,
                "description": "Тестовая модель",
                "hidden": False,
                "isDefault": model == "chosen-model",
                "defaultReasoningEffort": efforts[0],
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort, "description": effort} for effort in efforts
                ],
            }
            for model, efforts in [("chosen-model", ["high", "max"]), ("other-model", ["low", "medium"])]
        ]

    async def handle(self, ws):
        self.connections.add(ws)
        try:
            async for raw in ws:
                message = json.loads(raw)
                if "method" not in message:
                    self.responses.append(message)
                    continue
                self.calls.append(message)
                if "id" not in message:
                    continue
                method, params = message["method"], message.get("params", {})
                tid = params.get("threadId")
                if (
                    method in {"thread/resume", "thread/items/list", "thread/turns/list"}
                    and tid in self.unmaterialized
                ):
                    await ws.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {"code": -32600, "message": f"no rollout found for thread id {tid}"},
                            }
                        )
                    )
                    continue
                if method == "initialize":
                    result = {"userAgent": "fake/0.160.0"}
                elif method == "thread/read":
                    result = {"thread": self.threads[tid]}
                elif method == "model/list":
                    result = {"data": self.models, "nextCursor": None}
                elif method == "thread/resume":
                    self.threads[tid]["status"] = (
                        self.threads[tid]["status"] if tid not in self.archived else {"type": "notLoaded"}
                    )
                    result = {"thread": self.threads[tid], **self.settings}
                elif method == "thread/list":
                    result = {
                        "data": [
                            t
                            for key, t in self.threads.items()
                            if (key in self.archived) == params.get("archived", False)
                            and not (self.hide_empty and key in self.empty and key not in self.archived)
                        ],
                        "nextCursor": None,
                    }
                elif method == "thread/start":
                    self.created += 1
                    tid = f"new-{self.created}"
                    self.threads[tid] = thread(self.project, thread_id=tid)
                    self.threads[tid]["historyMode"] = params.get("historyMode", "paginated")
                    self.unmaterialized.add(tid)
                    self.empty.add(tid)
                    result = {"thread": self.threads[tid], **self.settings}
                    if self.mismatch_model:
                        result["model"] = "wrong-model"
                elif method == "thread/name/set":
                    if self.name_error:
                        await ws.send(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "error": {"code": -32600, "message": "не удалось сохранить новую сессию"},
                                }
                            )
                        )
                        continue
                    self.unmaterialized.discard(tid)
                    self.threads[tid]["name"] = params["name"]
                    result = {}
                elif method == "thread/backgroundTerminals/list":
                    result = {"data": self.terminals.get(tid, []), "nextCursor": None}
                elif method == "thread/backgroundTerminals/terminate":
                    if self.terminated:
                        self.terminals[tid] = [
                            item for item in self.terminals[tid] if item["processId"] != params["processId"]
                        ]
                    result = {"terminated": self.terminated}
                elif method == "thread/archive":
                    self.archived.add(tid)
                    self.threads[tid]["status"] = {"type": "notLoaded"}
                    result = {}
                elif method == "thread/unarchive":
                    self.archived.discard(tid)
                    self.threads[tid]["status"] = {"type": "idle"}
                    result = {"thread": self.threads[tid]}
                elif method == "thread/items/list":
                    result = {
                        "data": [] if tid in self.empty else [entry(f"answer-{tid}", text=f"История {tid}")],
                        "nextCursor": None,
                    }
                elif method == "thread/turns/list":
                    result = {
                        "data": [{"id": f"turn-{tid}", "status": "inProgress", "items": []}]
                        if self.threads[tid]["status"]["type"] == "active"
                        else (
                            [
                                {
                                    "id": f"turn-{tid}",
                                    "status": "failed",
                                    "error": {
                                        "message": (
                                            "Selected model is at capacity. Please try a different model."
                                        )
                                    },
                                    "items": [],
                                }
                            ]
                            if self.threads[tid]["status"]["type"] == "systemError"
                            else []
                        )
                    }
                elif method in {"turn/start", "turn/steer"}:
                    if method == "turn/start" and "model" in params:
                        self.threads[tid].update(model=params["model"], reasoningEffort=params["effort"])
                    self.empty.discard(tid)
                    self.threads[tid]["preview"] = next(
                        (part["text"] for part in params.get("input", []) if part["type"] == "text"), ""
                    )
                    self.threads[tid]["status"] = {"type": "active", "activeFlags": []}
                    result = (
                        {"turn": {"id": f"turn-{tid}", "status": "inProgress", "items": []}}
                        if (method == "turn/start")
                        else {"turnId": params["expectedTurnId"]}
                    )
                elif method == "turn/interrupt":
                    self.threads[tid]["status"] = {"type": "idle"}
                    result = {}
                else:
                    result = {}
                await ws.send(json.dumps({"id": message["id"], "result": result}))
        finally:
            self.connections.discard(ws)


class RelayStub:
    connected = True
    error = error_code = ""
    last_connected_at = last_disconnected_at = last_close_code = None
    last_close_reason = ""
    retry_delay = 0
    keep_awake_enabled = True

    def __init__(self, reg):
        self.registration = reg
        self.frames = []
        self.sleep_guard = IdleSleepGuard()

    async def send(self, frame_type, **fields):
        self.frames.append({"type": frame_type, **fields})
        return True


@asynccontextmanager
async def project_controller(tmp_path):
    fake = ProjectApp(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    reg = registration("ws://127.0.0.1:1/agent")
    registry.put(tmp_path, reg)
    async with fake.running() as endpoint:
        owner = ProjectController(tmp_path, registry, tmp_path / "state.json", endpoint)
        app = owner.app = AppServer(endpoint)
        await app.open(owner.on_event)
        owner.relay = RelayStub(reg)
        try:
            yield fake, owner
        finally:
            await app.close()


async def test_switch_keeps_active_thread_queue_and_questions(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        a = owner.sessions["a"]
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        a.state, a.turn_id = "busy", "turn-a"
        a.queue.append(("pending", "Сообщение для a"))
        a.outbox["pending"] = Submission("Сообщение для a")
        a.questions["qa"] = {"request_id": 1, "card": {"question_id": "qa", "text": "Разрешение a"}}
        await owner.on_frame({"type": "session.select", "session": "b", "expected_session": "a"})
        assert owner.selected == "b"
        assert a.state == "busy" and list(a.queue) == [("pending", "Сообщение для a")]
        assert "qa" in a.questions and not owner.sessions["b"].questions
        assert not any(c["method"] in {"thread/archive", "turn/interrupt"} for c in fake.calls)


async def test_system_error_session_opens_with_history_and_failure_reason(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.threads["a"]["status"] = {"type": "systemError"}
        await owner.send_sessions()
        rows = next(frame["items"] for frame in owner.relay.frames if frame["type"] == "sessions")
        assert next(row for row in rows if row["session"] == "a")["state"] == "ready"
        await owner.select("a")
        assert owner.selected == "a"
        assert owner.sessions["a"].state == "ready"
        assert any(
            frame["type"] == "message" and frame.get("text") == "История a" for frame in owner.relay.frames
        )
        assert any(
            frame["type"] == "error" and "Модель сейчас перегружена" in frame["message"]
            for frame in owner.relay.frames
        )
        frames = owner.relay.frames
        assert next(i for i, f in enumerate(frames) if f["type"] == "history.done") < next(
            i for i, f in enumerate(frames) if f["type"] == "error"
        )
        owner.relay.frames.clear()
        await owner.on_frame({"type": "subscribe"})
        failures = [f for f in owner.relay.frames if f["type"] == "error"]
        assert len(failures) == 1 and failures[0]["session"] == "a"
        await owner.on_event(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "a", "status": {"type": "systemError"}},
            }
        )
        assert owner.selected == "a"
        assert not owner.app.closed.is_set()
        await owner.on_frame({"type": "run", "session": "a", "text": "Повтори запрос"})
        starts = [c["params"] for c in fake.calls if c["method"] == "turn/start"]
        assert len(starts) == 1 and starts[0]["threadId"] == "a"
        assert "model" not in starts[0] and "effort" not in starts[0]


async def test_failure_details_unavailable_do_not_block_history(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.threads["a"]["status"] = {"type": "systemError"}

        async def unavailable(thread_id):
            raise TimeoutError

        owner.app.last_turn_failure = unavailable
        owner.app.recent_turns = unavailable
        await owner.select("a")
        assert owner.selected == "a" and owner.sessions["a"].state == "ready"
        assert any(frame["type"] == "history.done" for frame in owner.relay.frames)
        assert any(
            frame.get("code") == "codex_turn_failed" and frame["session"] == "a"
            for frame in owner.relay.frames
        )


async def test_resolve_answered_background_question_does_not_select_or_interrupt_it(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        a = owner.sessions["a"]
        await a.async_questions({"id": "issuer", "questions": [{"title": "Issuer?"}]}, "turn-a")
        qid = next(iter(a.questions))
        await owner.select("b")
        assert owner.status("a")["async_questions"] == [{"question_id": qid, "text": "Issuer?"}]
        assert owner.status("b")["async_questions"] == []
        assert not (await owner.resolve_question("b", qid))["resolved"]
        assert qid in a.questions
        assert (await owner.resolve_question("a", qid))["resolved"]
        assert owner.selected == "b"
        assert not any(c["method"] in {"turn/start", "turn/steer", "turn/interrupt"} for c in fake.calls)
        assert {"type": "question.resolved", "session": "a", "question_id": qid} in owner.relay.frames


async def test_background_deltas_and_answers_keep_exact_session(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.select("b")
        await fake.emit(
            "item/agentMessage/delta",
            {"threadId": "a", "turnId": "turn-a", "itemId": "answer-a", "delta": "Фоновый ответ"},
        )
        await until(lambda: any(f.get("chunk") == "Фоновый ответ" for f in owner.relay.frames))
        frame = next(f for f in owner.relay.frames if f.get("chunk") == "Фоновый ответ")
        assert frame["session"] == "a" and owner.selected == "b"
        a = owner.sessions["a"]
        a.turn_id = "turn-a"
        await fake.emit(
            "item/commandExecution/requestApproval",
            {
                "threadId": "a",
                "turnId": "turn-a",
                "itemId": "cmd-a",
                "command": "pytest",
                "cwd": str(tmp_path),
                "reason": "Проверить код",
            },
            request_id=42,
        )
        await until(lambda: bool(a.questions))
        qid = next(iter(a.questions))
        await owner.on_frame({"type": "answer", "session": "b", "question_id": qid, "verdict": "allow"})
        assert not fake.responses and qid in a.questions
        await owner.on_frame({"type": "answer", "session": "a", "question_id": qid, "verdict": "allow"})
        await until(lambda: bool(fake.responses))
        assert fake.responses[-1] == {"id": 42, "result": {"decision": "accept"}}
        assert owner.selected == "b" and not a.questions


async def test_new_thread_inherits_actual_settings_and_leaves_background_running(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        await owner.on_frame({"type": "session.select", "session": "new", "expected_session": "a"})
        assert owner.selected == "new-1" and owner.sessions["a"].state == "busy"
        params = next(c["params"] for c in fake.calls if c["method"] == "thread/start")
        assert params["model"] == "chosen-model" and params["approvalsReviewer"] == "auto_review"
        assert params["approvalPolicy"] == "on-request" and params["sandbox"] == "workspace-write"
        assert params["config"]["model_reasoning_effort"] == "high"
        assert params["config"]["sandbox_workspace_write"]["network_access"] is False
        assert params["cwd"] == str(tmp_path)
        assert not any(c["method"] == "turn/interrupt" for c in fake.calls)
        saved = next(c for c in fake.calls if c["method"] == "thread/name/set")
        assert saved["params"] == {"threadId": "new-1", "name": "Новая сессия"}
        assert "new-1" not in fake.unmaterialized
        assert not await owner.sessions["new-1"].history.page()
        assert not any(c["method"] == "turn/start" for c in fake.calls)


async def test_new_thread_refuses_changed_settings_without_submitting_task(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.mismatch_model = True
        await owner.on_frame({"type": "session.select", "session": "new", "expected_session": "a"})
        assert owner.selected == "a"
        assert owner.relay.frames[-1]["type"] == "error"
        assert not any(c["method"] == "turn/start" for c in fake.calls)
        assert not any(c["method"] == "thread/name/set" for c in fake.calls)


async def test_new_thread_save_failure_keeps_previous_selection_and_work(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        fake.name_error = True
        await owner.on_frame(
            {"type": "session.select", "session": "new", "expected_session": "a", "rid": "new"}
        )
        assert owner.selected == "a" and owner.sessions["a"].state == "busy"
        assert owner.relay.frames[-1]["type"] == "error"
        assert owner.relay.frames[-1]["rid"] == "new"
        assert fake.unmaterialized == {"new-1"}
        assert not any(c["method"] in {"turn/start", "turn/interrupt"} for c in fake.calls)
        assert not any(
            c["method"] == "thread/resume" and c["params"]["threadId"] == "new-1" for c in fake.calls
        )


async def test_new_empty_session_can_be_reselected_and_first_message_is_sent_once(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.select("new")
        await owner.select("b")
        await owner.select("new-1")
        await owner.on_frame({"type": "run", "session": "new-1", "text": "Правим вход", "client_id": "first"})
        calls = [c for c in fake.calls if c["method"] == "turn/start"]
        assert len(calls) == 1 and calls[0]["params"]["threadId"] == "new-1"
        assert calls[0]["params"]["input"] == [{"type": "text", "text": "Правим вход"}]
        await owner.send_sessions()
        current = next(row for row in owner.relay.frames[-1]["items"] if row["session"] == "new-1")
        assert current["title"] == "Правим вход"
        fake.threads["new-1"]["name"] = "Название из Codex"
        await owner.send_sessions()
        current = next(row for row in owner.relay.frames[-1]["items"] if row["session"] == "new-1")
        assert current["title"] == "Название из Codex"


async def test_loaded_empty_sessions_stay_visible_before_codex_lists_them(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.hide_empty = True
        await owner.select("a")
        await owner.select("new")
        await owner.select("new")
        await owner.send_sessions()
        rows = owner.relay.frames[-1]["items"]
        assert {row["session"] for row in rows} == {"a", "b", "new-1", "new-2"}
        assert len(rows) == 4 and next(r for r in rows if r["current"])["session"] == "new-2"
        await owner.close_session("new-1", "close")
        assert "new-1" not in {row["session"] for row in owner.relay.frames[-1]["items"]}
        await owner.send_sessions(archived=True)
        assert [row["session"] for row in owner.relay.frames[-1]["items"]] == ["new-1"]


async def test_stale_run_and_switch_do_not_retarget_messages(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.select("b")
        await owner.on_frame({"type": "run", "session": "a", "text": "старый экран"})
        await owner.on_frame({"type": "session.select", "expected_session": "a", "session": "new"})
        assert owner.selected == "b"
        assert not any(c["method"] in {"turn/start", "thread/start"} for c in fake.calls)


async def test_foreign_project_cannot_be_selected_or_closed(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        for kind in ("session.select", "session.close"):
            await owner.on_frame({"type": kind, "session": "foreign", "expected_session": "a"})
        assert owner.selected == "a"
        assert not any(c["method"] == "thread/archive" for c in fake.calls)
        await owner.send_sessions()
        assert "foreign" not in {r["session"] for r in owner.relay.frames[-1]["items"]}


async def test_close_archives_idle_thread_and_restore_reads_history(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.on_frame({"type": "session.close", "session": "a", "expected_session": "a"})
        assert owner.selected == "" and "a" in fake.archived and owner.relay.connected
        await owner.send_sessions(archived=True)
        assert owner.relay.frames[-1]["items"][0]["archived"] is True
        await owner.on_frame(
            {"type": "session.select", "session": "a", "archived": True, "expected_session": ""}
        )
        assert owner.selected == "a" and "a" not in fake.archived
        assert any(f.get("text") == "История a" and f.get("session") == "a" for f in owner.relay.frames)


@pytest.mark.parametrize("pending", ["active", "queue", "outbox", "questions"])
async def test_close_does_not_discard_background_work(tmp_path, pending):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        a = owner.sessions["a"]
        if pending == "active":
            fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        elif pending == "queue":
            a.queue.append(("cid", "pending"))
        elif pending == "outbox":
            a.outbox["cid"] = Submission("pending")
        else:
            a.questions["qid"] = {}
        await owner.on_frame({"type": "session.close", "session": "a", "expected_session": "a"})
        assert owner.selected == "a" and "a" not in fake.archived


async def test_external_close_keeps_project_and_other_sessions(tmp_path):
    async with project_controller(tmp_path) as (_fake, owner):
        await owner.select("a")
        await owner.select("b")
        await owner.on_event({"method": "thread/closed", "params": {"threadId": "a"}})
        assert owner.selected == "b" and owner.relay.connected and not owner.app.closed.is_set()
        await owner.on_event({"method": "thread/closed", "params": {"threadId": "b"}})
        assert owner.selected == "" and owner.relay.connected


async def test_external_archive_keeps_uncertain_delivery(tmp_path):
    async with project_controller(tmp_path) as (_fake, owner):
        await owner.select("a")
        owner.sessions["a"].outbox["cid"] = Submission("Неподтверждённая задача")
        await owner.on_event({"method": "thread/archived", "params": {"threadId": "a"}})
        assert owner.selected == "" and owner.sessions["a"].outbox["cid"].text
        assert owner.status()["unconfirmed"] == 1
        saved = ProjectController(tmp_path, owner.registry, owner.state_path, owner.endpoint)
        assert saved.status()["unconfirmed"] == 1 and saved.status()["delivery_errors"] == 1


async def test_restart_restores_exact_selection_and_does_not_repeat_uncertain_delivery(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        owner.sessions["a"].outbox["pending"] = Submission("Сохранённое сообщение")
        owner.sessions["a"].queue.append(("pending", "Сохранённое сообщение"))
        await owner.select("b")
        owner.save()
        saved = ProjectController(tmp_path, owner.registry, owner.state_path, owner.endpoint)
        saved.app = owner.app
        assert saved.selected == "b"
        a = await saved.ensure_session("a")
        assert a.outbox["pending"].text == "Сохранённое сообщение" and a.outbox["pending"].error
        assert not a.queue
        assert not any(c["method"] in {"turn/start", "turn/steer"} for c in fake.calls)
        assert owner.state_path.stat().st_mode & 0o077 == 0


async def test_history_uses_stored_thread_api_without_new_turn(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.calls.clear()
        await owner.sessions["a"].send_history()
        assert [c["method"] for c in fake.calls] == ["thread/items/list"]


async def test_two_projects_have_different_controller_identities(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    registry = Registry(tmp_path / "registry.json")
    first = ProjectController(tmp_path, registry, tmp_path / "first.json")
    second = ProjectController(other, registry, tmp_path / "second.json")
    assert first.identity != second.identity


async def test_creation_with_named_permission_profile_preserves_profile(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.settings["activePermissionProfile"] = {"id": "chosen-profile"}
        await owner.select("a")
        await owner.select("new")
        params = next(c["params"] for c in fake.calls if c["method"] == "thread/start")
        assert params["config"]["default_permissions"] == "chosen-profile" and "sandbox" not in params


async def test_subscribe_recovers_dense_history_without_changing_thread_or_permissions(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        bridge = owner.sessions["a"]

        async def items(thread_id, cursor, limit):
            assert thread_id == "a"
            return {"data": [entry("right"), entry("missing"), entry("left")], "nextCursor": None}

        bridge.app.items = items
        bridge.history.ids = {"left": 100, "right": 101}
        bridge.history.low, bridge.history.high = 100, 101
        owner.relay.frames.clear()
        fake.calls.clear()
        await owner.on_frame({"type": "subscribe"})
        messages = [f for f in owner.relay.frames if f["type"] == "message"]
        assert len(messages) == 3
        assert [f["id"] for f in messages] == sorted({f["id"] for f in messages})
        assert any(f["type"] == "history.done" for f in owner.relay.frames)
        assert not any(f["type"] == "error" for f in owner.relay.frames)
        assert owner.selected == "a"
        assert not any(c["method"] in {"thread/start", "turn/start", "turn/steer"} for c in fake.calls)
