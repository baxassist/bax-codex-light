import asyncio

import pytest
from conftest import FakeApp, until
from test_bridge import FakeRelay

from bax_codex_light.appserver import AppServer
from bax_codex_light.bridge import Bridge
from bax_codex_light.history import History
from bax_codex_light.registry import Registry


@pytest.mark.parametrize("completed", [False, True])
async def test_async_question_buttons_and_reply_use_exact_conversation(tmp_path, completed):
    fake = FakeApp(tmp_path)
    fake.state = "active"
    bridge = Bridge(tmp_path, Registry(tmp_path / "registry.json"), "current")
    bridge.relay = FakeRelay()
    item = {
        "id": "async-question",
        "type": "agentMessage",
        "text": "Вы видите карточку?",
        "delivery": "async",
        "questions": [{"title": "Вы видите карточку?", "options": ["Да", "Нет"]}],
    }
    async with fake.running() as endpoint:
        app = AppServer(endpoint)
        bridge.app = app
        try:
            await app.open(bridge.on_event)
            bridge._set_state((await app.attach("current", tmp_path))["status"])
            bridge.history = History(app, "current")
            params = {"threadId": "current", "turnId": "turn", "item": item, "startedAtMs": 1}
            await fake.emit("item/started", params)
            await until(lambda: bool(bridge.questions))
            qid = next(iter(bridge.questions))
            card = next(frame for frame in bridge.relay.sent if frame["type"] == "question")
            assert card["options"] == ["Да", "Нет"] and card["session"] == "current"
            finish = {k: v for k, v in params.items() if k != "startedAtMs"}
            finish["completedAtMs"] = 2
            await fake.emit("item/completed", finish)
            await asyncio.sleep(0.02)
            assert len(bridge.questions) == 1
            if completed:
                await bridge.on_event(
                    {"method": "turn/completed", "params": {"threadId": "current", "turn": {"id": "turn"}}}
                )
                assert qid in bridge.questions
                fake.state = "idle"
                bridge.state = "ready"
            await bridge.on_frame({"type": "answer", "question_id": qid, "verdict": "choice", "option": "Да"})
            method = "turn/start" if completed else "turn/steer"
            sent = [call for call in fake.calls if call["method"] in {"turn/start", "turn/steer"}]
            assert len(sent) == 1 and sent[0]["method"] == method
            assert sent[0]["params"]["threadId"] == "current"
            assert sent[0]["params"]["input"][0]["text"] == "Вы видите карточку?\nОтвет: Да"
            if not completed:
                assert sent[0]["params"]["expectedTurnId"] == "turn"
            assert "model" not in sent[0]["params"] and "approvalPolicy" not in sent[0]["params"]
            await fake.emit("item/completed", finish)
            await asyncio.sleep(0.02)
            assert not bridge.questions
            assert not any(call["method"] == "thread/start" for call in fake.calls)
        finally:
            await app.close()


async def test_uncertain_async_answer_is_not_sent_twice(tmp_path):
    from test_bridge import bridge as make_bridge

    bridge = make_bridge(tmp_path)
    bridge.state = "busy"
    calls = []

    async def timeout(*args):
        calls.append(args)
        raise TimeoutError("private exception")

    bridge.app.steer_turn = timeout
    await bridge.async_questions(
        {"id": "question", "questions": [{"title": "Проверка?", "options": ["Да", "Нет"]}]}, "turn"
    )
    qid = next(iter(bridge.questions))
    frame = {"type": "answer", "question_id": qid, "verdict": "choice", "option": "Да"}
    await bridge.on_frame(frame)
    await bridge.on_frame(frame)
    assert len(calls) == 1 and not bridge.queue
    assert bridge.status()["delivery_errors"] == 1
    assert not bridge.questions
    assert any(frame.get("code") == "delivery_uncertain" for frame in bridge.relay.sent)
    assert "private exception" not in str(bridge.relay.sent)
