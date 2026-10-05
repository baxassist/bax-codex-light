import asyncio

import pytest
from conftest import entry

from bax_codex_light.appserver import RPCRejected
from bax_codex_light.bridge import Bridge
from bax_codex_light.history import History
from bax_codex_light.power import IdleSleepGuard
from bax_codex_light.registry import Registry


class FakeApp:
    def __init__(self):
        self.closed = asyncio.Event()
        self.state = "active"
        self.active_turn_id = ""
        self.steers = []
        self.calls = []
        self.responses = []
        self.fail = False
        self.history_items = []
        self.approvals = {}

    async def inspect(self, thread_id, project):
        return {"status": {"type": self.state, "activeFlags": []}}

    async def start_turn(self, thread_id, text, client_id):
        self.calls.append((thread_id, text))
        self.state = "active"
        if self.fail:
            raise TimeoutError
        return {"turn": {"id": "turn"}}

    async def active_turn(self, thread_id):
        return self.active_turn_id if self.state == "active" else ""

    async def steer_turn(self, thread_id, turn_id, text, client_id):
        self.steers.append((thread_id, turn_id, text, client_id))
        return {"turnId": turn_id}

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))

    async def recent_turns(self, thread_id):
        return []

    async def last_turn_failure(self, thread_id):
        return {}

    async def items(self, thread_id, cursor, limit):
        return {"data": self.history_items, "nextCursor": None}


class FakeRelay:
    connected = True
    keep_awake_enabled = True
    error = ""
    error_code = ""
    last_connected_at = None
    last_disconnected_at = None
    last_close_code = None
    last_close_reason = ""
    retry_delay = 0

    def __init__(self):
        self.sent = []
        self.sleep_guard = IdleSleepGuard()
        self.sleep_guard.supported = False

    async def send(self, frame_type, **fields):
        self.sent.append({"type": frame_type, **fields})
        return True


def bridge(tmp_path):
    result = Bridge(tmp_path, Registry(tmp_path / "registry.json"), "current")
    result.app = FakeApp()
    result.relay = FakeRelay()
    result.history = History(result.app, "current")
    return result


async def test_missing_active_turn_id_queues_then_delivers_backlog_in_one_turn(tmp_path):
    b = bridge(tmp_path)
    await b.on_frame({"type": "run", "text": "первое"})
    await b.on_frame({"type": "run", "text": "второе"})
    assert not b.app.calls
    assert len(b.queue) == 2
    b.app.state = "idle"
    await asyncio.gather(b._drain(), b._drain())
    assert b.app.calls == [("current", "первое")]
    assert b.app.steers[0][:3] == ("current", "turn", "второе")
    assert not b.queue


async def test_queued_question_survives_reopening_phone_history(tmp_path):
    b = bridge(tmp_path)
    question = "Какую модель ты используешь для тестов?"
    await b.on_frame({"type": "run", "text": question})
    echo = next(frame for frame in b.relay.sent if frame["type"] == "message")
    b.relay.sent.clear()

    await b.on_frame({"type": "subscribe"})

    rows = [frame for frame in b.relay.sent if frame["type"] == "message"]
    assert rows == [echo]
    assert b.status()["queued"] == 1
    assert not b.app.calls


async def test_uncertain_delivery_never_auto_retries(tmp_path):
    b = bridge(tmp_path)
    b.app.state = "idle"
    b.app.fail = True
    await b.on_frame({"type": "run", "text": "задача"})
    await b._drain()
    assert len(b.app.calls) == 1
    assert not b.queue
    assert any(f.get("code") == "delivery_uncertain" for f in b.relay.sent)

    b.relay.sent.clear()
    await b.on_frame({"type": "subscribe"})
    rows = [frame for frame in b.relay.sent if frame["type"] == "message"]
    assert any(row["kind"] == "user" and row["text"] == "задача" for row in rows)
    assert any(row["kind"] == "error" and "не подтверждена" in row["text"] for row in rows)
    assert len(b.app.calls) == 1


async def test_explicit_rejection_keeps_text_and_reason_without_retry(tmp_path):
    b = bridge(tmp_path)
    b.app.state = "idle"

    async def rejected(thread_id, text, client_id):
        b.app.calls.append((thread_id, text))
        raise RPCRejected("Сессия не принимает прямые задачи")

    b.app.start_turn = rejected
    await b.on_frame({"type": "run", "text": "мой вопрос"})
    await b.on_frame({"type": "subscribe"})
    await b._drain()

    assert b.app.calls == [("current", "мой вопрос")]
    assert b.status()["delivery_errors"] == 1
    assert any(frame.get("code") == "delivery_rejected" for frame in b.relay.sent)
    assert any(frame.get("kind") == "error" and "не принимает" in frame["text"] for frame in b.relay.sent)
    assert b.state == "ready"


async def test_rejected_message_does_not_block_next_queued_message(tmp_path):
    b = bridge(tmp_path)
    await b.on_frame({"type": "run", "text": "отклонённое"})
    await b.on_frame({"type": "run", "text": "следующее"})
    original = b.app.start_turn

    async def reject_first(thread_id, text, client_id):
        if text == "отклонённое":
            b.app.calls.append((thread_id, text))
            raise RPCRejected("Запрос отклонён")
        return await original(thread_id, text, client_id)

    b.app.start_turn = reject_first
    b.app.state = "idle"
    await b._drain()
    assert b.app.calls == [("current", "отклонённое"), ("current", "следующее")]
    assert not b.queue


async def test_late_native_confirmation_removes_uncertainty_without_retry(tmp_path):
    b = bridge(tmp_path)
    b.app.state = "idle"
    b.app.fail = True
    await b.on_frame({"type": "run", "text": "мой вопрос"})
    client_id = next(iter(b.outbox))
    echo = next(frame for frame in b.relay.sent if frame["type"] == "message")
    native = entry("native", "userMessage", "мой вопрос")
    native["item"]["clientId"] = client_id

    await b.on_event({"method": "item/completed", "params": {"threadId": "current", "item": native["item"]}})
    confirmation = [frame for frame in b.relay.sent if frame["type"] == "message"][-1]
    assert confirmation["id"] == echo["id"]
    assert any(f["type"] == "error.resolved" and f.get("delivery") == "accepted" for f in b.relay.sent)
    b.app.history_items = [native]
    b.relay.sent.clear()
    await b.on_frame({"type": "subscribe"})

    rows = [frame for frame in b.relay.sent if frame["type"] == "message"]
    assert len(rows) == 1
    assert rows[0]["text"] == "мой вопрос"
    assert not b.outbox
    assert b.status()["delivery_errors"] == 0
    assert len(b.app.calls) == 1


async def test_queued_echo_does_not_break_history_after_active_answer(tmp_path):
    b = bridge(tmp_path)
    b.app.history_items = [entry("old")]
    await b.send_history()
    await b.on_frame({"type": "run", "text": "вопрос в очереди"})
    client_id = b.queue[0][0]
    answer = entry("active-answer")
    await b.on_event({"method": "item/completed", "params": {"threadId": "current", "item": answer["item"]}})
    b.app.state = "idle"
    await b._drain()
    native = entry("native-question", "userMessage", "вопрос в очереди")
    native["item"]["clientId"] = client_id
    # Шаг пришёл при обрыве связи; в файле Codex вопрос идёт после активного ответа.
    b.app.history_items = [native, entry("missed-step", "commandExecution"), answer, entry("old")]
    b.relay.sent.clear()

    await b.on_frame({"type": "subscribe"})

    assert not any(frame["type"] == "error" for frame in b.relay.sent)
    rows = [frame for frame in b.relay.sent if frame["type"] == "message"]
    assert rows[-1]["text"] == "вопрос в очереди"
    assert len([row for row in rows if row["kind"] == "user"]) == 1
    assert not b.outbox


def approval(thread_id="current", request_id=123, method="item/commandExecution/requestApproval"):
    return {
        "id": request_id,
        "method": method,
        "params": {
            "threadId": thread_id,
            "turnId": "turn",
            "itemId": "item",
            "startedAtMs": 1,
            "command": "npm test",
            "cwd": "/tmp/project",
            "reason": "Нужен доступ",
        },
    }


async def test_unrelated_events_and_requests_are_ignored(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(approval("other"))
    await b.on_event(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "other",
                "itemId": "foreign",
                "delta": "private",
            },
        }
    )
    assert not b.questions
    assert not b.relay.sent
    assert not b.app.responses


@pytest.mark.parametrize("verdict,decision", [("allow", "accept"), ("deny", "decline")])
async def test_one_time_answer_never_auto_grants_or_remembers(tmp_path, verdict, decision):
    b = bridge(tmp_path)
    await b.on_event(approval())
    assert not b.app.responses
    question_id = next(iter(b.questions))
    await b.answer({"question_id": question_id, "verdict": verdict})
    assert b.app.responses == [(123, {"decision": decision})]
    with pytest.raises(ValueError):
        await b.answer({"question_id": question_id, "verdict": "allow"})


async def test_terminal_resolution_invalidates_phone_question(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(approval())
    question_id = next(iter(b.questions))
    await b.on_event(
        {
            "method": "serverRequest/resolved",
            "params": {
                "threadId": "current",
                "requestId": 123,
            },
        }
    )
    with pytest.raises(ValueError):
        await b.answer({"question_id": question_id, "verdict": "allow"})
    assert not b.app.responses


async def test_multiple_questions_are_displayed_one_at_a_time(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(
        {
            "id": 42,
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "current",
                "turnId": "turn",
                "itemId": "item",
                "isBlocking": True,
                "questions": [
                    {"id": "q1", "question": "Первый?", "options": [{"label": "Да"}]},
                    {"id": "q2", "question": "Второй?", "options": [{"label": "Нет"}]},
                ],
            },
        }
    )
    sent_questions = [f for f in b.relay.sent if f["type"] == "question"]
    assert len(sent_questions) == 1
    first, second = list(b.questions)
    await b.answer({"question_id": first, "verdict": "choice", "option": "Да"})
    assert not b.app.responses
    assert b.relay.sent[-1]["text"] == "Второй?"
    await b.answer({"question_id": second, "verdict": "choice", "option": "Нет"})
    assert b.app.responses == [(42, {"answers": {"q1": {"answers": ["Да"]}, "q2": {"answers": ["Нет"]}}})]


async def test_secret_questions_stay_local(tmp_path):
    b = bridge(tmp_path)
    event = approval(method="item/tool/requestUserInput")
    event["params"]["questions"] = [{"id": "secret", "question": "Пароль?", "isSecret": True}]
    await b.on_event(event)
    assert not b.questions
    assert not b.app.responses
    assert not b.relay.sent


async def test_stream_does_not_emit_empty_duplicate_row(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "current",
                "turnId": "turn",
                "itemId": "answer",
                "delta": "При",
            },
        }
    )
    await b.on_event(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "current",
                "turnId": "turn",
                "itemId": "answer",
                "delta": "вет",
            },
        }
    )
    await b.on_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": "current",
                "item": {"type": "agentMessage", "id": "answer", "text": "Привет"},
            },
        }
    )
    assert [f["type"] for f in b.relay.sent] == ["delta", "delta", "message"]
    assert len({f["id"] for f in b.relay.sent}) == 1


async def test_binding_cannot_switch_thread(tmp_path):
    b = bridge(tmp_path)
    with pytest.raises(ValueError, match="другому разговору"):
        await b.bind("other")
