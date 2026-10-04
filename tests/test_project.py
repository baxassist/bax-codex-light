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
                if method == "initialize":
                    result = {"userAgent": "fake/0.160.0"}
                elif method == "thread/read":
                    result = {"thread": self.threads[tid]}
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
                        ],
                        "nextCursor": None,
                    }
                elif method == "thread/start":
                    self.created += 1
                    tid = f"new-{self.created}"
                    self.threads[tid] = thread(self.project, thread_id=tid)
                    result = {"thread": self.threads[tid], **self.settings}
                    if self.mismatch_model:
                        result["model"] = "wrong-model"
                elif method == "thread/archive":
                    self.archived.add(tid)
                    self.threads[tid]["status"] = {"type": "notLoaded"}
                    result = {}
                elif method == "thread/unarchive":
                    self.archived.discard(tid)
                    self.threads[tid]["status"] = {"type": "idle"}
                    result = {"thread": self.threads[tid]}
                elif method == "thread/items/list":
                    result = {"data": [entry(f"answer-{tid}", text=f"История {tid}")], "nextCursor": None}
                elif method == "thread/turns/list":
                    result = {
                        "data": [{"id": f"turn-{tid}", "status": "inProgress", "items": []}]
                        if self.threads[tid]["status"]["type"] == "active"
                        else []
                    }
                elif method in {"turn/start", "turn/steer"}:
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


async def test_new_thread_refuses_changed_settings_without_submitting_task(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.mismatch_model = True
        await owner.on_frame({"type": "session.select", "session": "new", "expected_session": "a"})
        assert owner.selected == "a"
        assert owner.relay.frames[-1]["type"] == "error"
        assert not any(c["method"] == "turn/start" for c in fake.calls)


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
