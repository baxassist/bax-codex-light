import asyncio
import json
from uuid import uuid4

import pytest

from bax_codex_light.tracing import command_context, response_fields

PARENT = "00-11111111111111111111111111111111-2222222222222222-01"
OTHER = "00-33333333333333333333333333333333-4444444444444444-01"


async def test_context_isolated_per_command_and_not_inherited_by_background():
    async def worker(parent):
        with command_context({"traceparent": parent}):
            await asyncio.sleep(0)
            assert response_fields({}) == {"traceparent": parent}

            async def background():
                return response_fields({})

            assert await asyncio.create_task(background()) == {}
            with command_context():
                assert response_fields({}) == {}
            assert response_fields({}) == {"traceparent": parent}
            assert response_fields({"traceparent": OTHER}) == {"traceparent": OTHER}
        assert response_fields({}) == {}

    await asyncio.gather(worker(PARENT), worker(OTHER))
    with pytest.raises(RuntimeError), command_context({"traceparent": PARENT}):
        raise RuntimeError("synthetic")
    assert response_fields({}) == {}


@pytest.mark.parametrize("parent", ["bad", 1, "00-" + "0" * 32 + "-2222222222222222-01", PARENT[:-2] + "FF"])
async def test_invalid_context_rejected_before_handler(parent):
    with pytest.raises(ValueError), command_context({"traceparent": parent}):
        pytest.fail("invalid context entered")


async def test_real_transport_correlates_reply_without_leaking_to_next_command(tmp_path):
    from test_integration import registration
    from websockets.asyncio.server import serve

    from bax_codex_light.connection import Relay

    received = []
    completed = asyncio.Event()
    connection = None

    async def server(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(
            json.dumps({"v": 1, "type": "ready", "agent": connection.registration.agent, "user": "u"})
        )
        for fields in ({"traceparent": PARENT}, {}):
            await ws.send(json.dumps({"v": 1, "type": "history", **fields}))
            received.append(json.loads(await ws.recv()))
        completed.set()
        await ws.wait_closed()

    async def ready(*args):
        pass

    async def handle(message):
        await connection.send("history.done")

    async with serve(server, "127.0.0.1", 0, ping_interval=None) as listener:
        url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/agent"
        connection = Relay(registration(url), tmp_path, "current", keep_awake=False)
        task = asyncio.create_task(connection.session(ready, handle))
        try:
            await asyncio.wait_for(completed.wait(), 5)
            assert received[0]["traceparent"] == PARENT
            assert "traceparent" not in received[1]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_queued_run_uses_saved_parent_and_native_events_require_exact_turn(tmp_path):
    from test_bridge import bridge

    b = bridge(tmp_path)
    frames = []

    async def send(frame_type, **fields):
        frames.append({"type": frame_type, **response_fields(fields)})
        return True

    b.relay.send = send
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "queued", "traceparent": PARENT})
    assert next(iter(b.outbox.values())).traceparent == PARENT
    b.app.state = "idle"
    frames.clear()
    with command_context({"traceparent": OTHER}):
        await b._drain()
    assert b.turn_traces == {"turn": PARENT}
    accepted = next(frame for frame in frames if frame["type"] == "error.resolved")
    assert accepted["traceparent"] == PARENT
    for turn, expected in [("turn", PARENT), ("foreign", None)]:
        frames.clear()
        await b.on_event(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "current",
                    "turnId": turn,
                    "itemId": "answer-" + turn,
                    "delta": "text",
                },
            }
        )
        assert frames[-1].get("traceparent") == expected
    # A later steering command must not relabel the original native turn.
    b.app.active_turn_id = "turn"
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "steer", "traceparent": OTHER})
    assert b.turn_traces == {"turn": PARENT}


async def test_project_restart_preserves_submission_and_confirmed_turn_context(tmp_path):
    from test_project import project_controller

    from bax_codex_light.bridge import Submission
    from bax_codex_light.project import ProjectController

    async with project_controller(tmp_path) as (_fake, owner):
        session = await owner.ensure_session("a")
        session.outbox["pending"] = Submission("source", traceparent=PARENT)
        session.turn_traces["confirmed-turn"] = OTHER
        owner.save()
        restored = ProjectController(tmp_path, owner.registry, owner.state_path, owner.endpoint)
        restored.app = owner.app
        recovered = await restored.ensure_session("a")
        assert recovered.outbox["pending"].traceparent == PARENT
        assert recovered.turn_traces == {"confirmed-turn": OTHER}


async def test_unknown_native_outcome_retains_original_context_and_has_no_invented_turn(tmp_path):
    from test_bridge import bridge

    b = bridge(tmp_path)
    frames = []

    async def send(frame_type, **fields):
        frames.append({"type": frame_type, **response_fields(fields)})
        return True

    b.relay.send = send
    await b.on_frame({"client_id": str(uuid4()), "type": "run", "text": "queued", "traceparent": PARENT})
    b.app.state, b.app.fail = "idle", True
    with command_context({"traceparent": OTHER}):
        await b._drain()
    failure = next(frame for frame in frames if frame["type"] == "error")
    assert failure["traceparent"] == PARENT and failure["delivery"] == "unknown"
    assert not b.turn_traces
    assert next(iter(b.outbox.values())).traceparent == PARENT
    assert not b.queue  # No automatic native retry after an unknown outcome.
