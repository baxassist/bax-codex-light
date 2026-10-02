import asyncio

import pytest

from bax_codex_light.bridge import Bridge
from bax_codex_light.history import History
from bax_codex_light.registry import Registry


class FakeApp:
    def __init__(self):
        self.closed = asyncio.Event()
        self.state = "active"
        self.calls = []
        self.responses = []
        self.fail = False

    async def inspect(self, thread_id, project):
        return {"status": {"type": self.state, "activeFlags": []}}

    async def start_turn(self, thread_id, text, client_id):
        self.calls.append((thread_id, text))
        self.state = "active"
        if self.fail:
            raise TimeoutError
        return {"turn": {"id": "turn"}}

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))


class FakeRelay:
    connected = True
    error = ""

    def __init__(self):
        self.sent = []

    async def send(self, frame_type, **fields):
        self.sent.append({"type": frame_type, **fields})
        return True


def bridge(tmp_path):
    result = Bridge(tmp_path, Registry(tmp_path / "registry.json"), "current")
    result.app = FakeApp()
    result.relay = FakeRelay()
    result.history = History(result.app, "current")
    return result


async def test_busy_tasks_queue_without_steering_and_only_one_starts(tmp_path):
    b = bridge(tmp_path)
    await b.on_frame({"type": "run", "text": "первое"})
    await b.on_frame({"type": "run", "text": "второе"})
    assert not b.app.calls
    assert len(b.queue) == 2
    b.app.state = "idle"
    await asyncio.gather(b._drain(), b._drain())
    assert b.app.calls == [("current", "первое")]
    assert [text for _client_id, text in b.queue] == ["второе"]
    b.app.state = "idle"
    await b.on_event(
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "current",
                "status": {"type": "idle"},
            },
        }
    )
    assert b.app.calls[-1] == ("current", "второе")


async def test_uncertain_delivery_never_auto_retries(tmp_path):
    b = bridge(tmp_path)
    b.app.state = "idle"
    b.app.fail = True
    await b.on_frame({"type": "run", "text": "задача"})
    await b._drain()
    assert len(b.app.calls) == 1
    assert not b.queue
    assert any(f.get("code") == "delivery_uncertain" for f in b.relay.sent)


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
async def test_no_auto_approval_or_persistent_grants(tmp_path, verdict, decision):
    b = bridge(tmp_path)
    await b.on_event(approval())
    assert not b.app.responses
    question_id = next(iter(b.questions))
    with pytest.raises(ValueError):
        await b.answer({"question_id": question_id, "verdict": verdict, "remember": True})
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
