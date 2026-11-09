import asyncio
from uuid import uuid4

import pytest
from test_bridge import bridge

from bax_codex_light.appserver import RPCRejected


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("voice", [False, True])
async def test_busy_comment_uses_current_turn_immediately_without_granting_permission(
    tmp_path, waiting, voice
):
    b = bridge(tmp_path)
    b.app.active_turn_id = "active-turn"
    if waiting:

        async def inspect(thread_id, project):
            return {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}

        b.app.inspect = inspect
    b.questions = {"pending": {"card": {"kind": "permission"}, "request_id": 42}}
    cid, job = str(uuid4()), str(uuid4())
    frame = {"client_id": cid, "type": "run", "text": "Уточнение: использовать Wi-Fi"}
    if voice:
        frame.update(voice_job_id=job, rid=job)
    await b.on_frame(frame)
    assert b.app.steers[0][:3] == ("current", "active-turn", "Уточнение: использовать Wi-Fi")
    assert not b.app.calls and not b.queue
    assert "pending" in b.questions and not b.app.responses
    assert b.state == ("waiting" if waiting else "busy")
    assert b.status()["delivery_errors"] == 0
    if voice:
        ack = next(f for f in b.relay.sent if f["type"] == "voice.accepted")
        assert ack["rid"] == job and ack["turn_id"] == "active-turn"
        await b.on_frame(frame)
        assert len(b.app.steers) == 1 and not b.queue


async def test_previously_queued_voice_can_join_current_turn_after_update(tmp_path):
    b = bridge(tmp_path)
    cid, job = str(uuid4()), str(uuid4())
    await b.on_frame({"type": "run", "text": "Сохранённое голосовое", "client_id": cid, "voice_job_id": job})
    assert len(b.queue) == 1  # Текущий ход ещё не известен, ничего не отправлено.
    b.outbox[cid].steer_allowed = False  # Сохранённое правило старого пакета.
    b.app.active_turn_id = "active-turn"
    await b._drain()
    assert b.app.steers == [("current", "active-turn", "Сохранённое голосовое", cid)]
    assert not b.queue and not b.app.calls


async def test_live_comments_keep_order_and_are_not_independent_turns(tmp_path):
    b = bridge(tmp_path)
    b.turn_id = "active-turn"
    comments = ["Сначала проверить лог", "Это уточнение к задаче", "Последний вариант актуальный"]
    await asyncio.gather(
        *(b.on_frame({"client_id": str(uuid4()), "type": "run", "text": text}) for text in comments)
    )
    assert [call[2] for call in b.app.steers] == comments
    assert len({call[3] for call in b.app.steers}) == len(comments)
    assert not b.queue and not b.app.calls


@pytest.mark.parametrize("next_state", ["active", "idle"])
@pytest.mark.parametrize("voice", [False, True])
async def test_steer_race_rechecks_exact_turn_after_explicit_rejection(tmp_path, next_state, voice):
    b = bridge(tmp_path)
    b.turn_id = "old-turn"
    attempts = []

    async def steer(thread_id, turn_id, text, client_id):
        attempts.append((thread_id, turn_id, text, client_id))
        if len(attempts) == 1:
            b.app.state = next_state
            b.app.active_turn_id = "new-turn"
            raise RPCRejected("expected turn changed")
        return {"turnId": turn_id}

    b.app.steer_turn = steer
    frame = {"client_id": str(uuid4()), "type": "run", "text": "Важное уточнение"}
    if voice:
        frame["voice_job_id"] = str(uuid4())
    await b.on_frame(frame)
    if next_state == "active":
        assert [call[1] for call in attempts] == ["old-turn", "new-turn"]
        assert attempts[0][3] == attempts[1][3]
        assert not b.app.calls
    else:
        assert len(attempts) == 1
        assert b.app.calls == [("current", "Важное уточнение")]
    assert not b.queue and b.status()["delivery_errors"] == 0


async def test_repeated_explicit_rejection_retains_queue_with_clear_notice(tmp_path):
    b = bridge(tmp_path)
    b.app.active_turn_id = "turn"
    calls = []

    async def reject(*args):
        calls.append(args)
        raise RPCRejected("temporarily unavailable")

    b.app.steer_turn = reject
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "Не потерять комментарий"})
    assert len(calls) == 2 and len(b.queue) == 1
    assert any(frame.get("code") == "message_queued" for frame in b.relay.sent)
    b.app.state = "idle"
    await b._drain()
    assert b.app.calls == [("current", "Не потерять комментарий")]
    assert not b.queue


@pytest.mark.parametrize("voice", [False, True])
async def test_uncertain_live_delivery_is_never_replayed(tmp_path, voice):
    b = bridge(tmp_path)
    b.turn_id = "turn"
    calls = []

    async def timeout(*args):
        calls.append(args)
        raise TimeoutError()

    b.app.steer_turn = timeout
    frame = {"client_id": str(uuid4()), "type": "run", "text": "Сообщение при обрыве"}
    if voice:
        frame["voice_job_id"] = str(uuid4())
    await b.on_frame(frame)
    await b.on_frame({"type": "subscribe"})
    await b._drain()
    assert len(calls) == 1 and not b.queue and not b.app.calls
    assert b.status()["delivery_errors"] == 1
    assert any(frame.get("code") == "delivery_uncertain" for frame in b.relay.sent)


async def test_late_async_reply_waits_but_does_not_block_new_live_comment(tmp_path):
    b = bridge(tmp_path)
    b.state = "busy"
    b.turn_id = "current-turn"
    steers = []

    async def steer(thread_id, turn_id, text, client_id):
        steers.append((thread_id, turn_id, text, client_id))
        if turn_id == "previous-turn":
            raise RPCRejected("previous turn completed")
        return {"turnId": turn_id}

    b.app.steer_turn = steer
    await b.async_questions(
        {"id": "old-question", "questions": [{"title": "Старый вопрос?"}]}, "previous-turn"
    )
    qid = next(iter(b.questions))
    await b.answer({"question_id": qid, "verdict": "choice", "option": "Ответ позже"})
    assert len(b.queue) == 1
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "Комментарий к текущей задаче"})
    assert [call[1] for call in steers] == ["previous-turn", "current-turn"]
    assert steers[-1][2] == "Комментарий к текущей задаче"
    assert len(b.queue) == 1 and not b.app.calls
    b.app.state = "idle"
    await b._drain()
    assert b.app.calls == [("current", "Старый вопрос?\nОтвет: Ответ позже")]


async def test_rejected_live_comment_is_preserved_if_queue_fills_in_flight(tmp_path):
    from bax_codex_light.bridge import Submission

    b = bridge(tmp_path)
    b.turn_id = "turn"

    async def reject(*args):
        for i in range(b.queue.maxlen):
            cid = f"waiting-{i}"
            b.outbox[cid] = Submission("Сообщение в очереди", steer_allowed=False)
            b.queue.append((cid, "Сообщение в очереди"))
        raise RPCRejected("not accepted")

    b.app.steer_turn = reject
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "Сохранить мой текст"})
    assert len(b.queue) == b.queue.maxlen
    assert b.status()["delivery_errors"] == 1
    preserved = next(s for s in b.outbox.values() if s.text == "Сохранить мой текст")
    assert "очередь уже заполнена" in preserved.error
