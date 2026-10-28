"""Смена аккаунта через настоящий RPC-транспорт, без входа в аккаунты пользователя."""

import json
from uuid import uuid4

import pytest
from conftest import until
from test_project import project_controller

from bax_codex_light.project import ProjectController


def run_frame():
    cid = str(uuid4())
    return {"type": "run", "rid": cid, "client_id": cid, "text": "Сохранённая задача"}


def model_calls(fake):
    return [call for call in fake.calls if call["method"] in {"turn/start", "turn/steer"}]


async def test_account_switch_does_not_send_queued_task_or_redelivery(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        frame = run_frame()
        bridge = owner.sessions["a"]
        await bridge.accept_run(frame)
        fake.account["workspaceRouting"]["chatgptAccountId"] = "account-b"
        await bridge._drain()
        await bridge.on_frame(frame)  # Повтор доставки после потерянного эха.
        assert not model_calls(fake)
        assert frame["client_id"] in bridge.outbox
        assert bridge.outbox[frame["client_id"]].error
        assert len(bridge.queue) == 1
        assert any(f.get("category") == "authentication" for f in owner.relay.frames)
        await owner.on_frame(
            {"type": "voice.status", "session": "a", "rid": "status", "client_id": frame["client_id"]}
        )
        assert owner.relay.frames[-1]["status"] == "queued"
        # Новая явная задача относится к B; старая не мешает ей и не переносится.
        fresh = run_frame()
        await bridge.on_frame(fresh)
        assert len(model_calls(fake)) == 1
        assert model_calls(fake)[0]["params"]["clientUserMessageId"] == fresh["client_id"]
        assert frame["client_id"] in bridge.outbox


async def test_account_binding_survives_controller_restart(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        frame = run_frame()
        await owner.sessions["a"].accept_run(frame)
        fake.account["workspaceRouting"]["chatgptAccountId"] = "account-b"
        restarted = ProjectController(tmp_path, owner.registry, owner.state_path, owner.endpoint)
        restarted.app, restarted.relay = owner.app, owner.relay
        restored = await restarted.ensure_session("a")
        await restored._drain()
        assert not model_calls(fake)
        assert len(restored.queue) == 1
        # Вернувшись в A, можно доставить точно ту же ещё не отправленную задачу.
        fake.account["workspaceRouting"]["chatgptAccountId"] = "account-a"
        await restored._drain()
        assert len(model_calls(fake)) == 1
        assert model_calls(fake)[0]["params"]["clientUserMessageId"] == frame["client_id"]
        state = json.loads(owner.state_path.read_text())
        serialized = json.dumps(state)
        assert "account-a" not in serialized and "test@example.invalid" not in serialized


async def test_signed_out_does_not_accept_new_task(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.account = {"account": None, "requiresOpenaiAuth": True}
        bridge = owner.sessions["a"]
        await bridge.on_frame(run_frame())
        assert not model_calls(fake)
        assert not bridge.queue and not bridge.outbox
        assert any(f.get("category") == "authentication" for f in owner.relay.frames)


async def test_account_notifications_reach_all_sessions_and_recover(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        for tid in ("a", "b"):
            await owner.select(tid)
            await owner.sessions[tid].accept_run(run_frame())
        previous = fake.account
        fake.account = {"account": None, "requiresOpenaiAuth": True}
        await fake.emit("account/updated", {"authMode": None})
        await until(lambda: all(next(iter(b.outbox.values())).error for b in owner.sessions.values()))
        assert owner.status()["authentication"] == "signed_out"
        assert not model_calls(fake) and owner.selected == "b"
        fake.account = previous
        await fake.emit("account/updated", {"authMode": "chatgpt", "planType": "pro"})
        await until(lambda: len(model_calls(fake)) == 2)
        assert {c["params"]["threadId"] for c in model_calls(fake)} == {"a", "b"}
        assert owner.status()["authentication"] == "ready"


async def test_auth_change_while_status_send_is_suspended_does_not_submit(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        bridge = owner.sessions["a"]
        await bridge.accept_run(run_frame())
        send = owner.relay.send
        switched = False

        async def delayed(frame_type, **fields):
            nonlocal switched
            if frame_type == "status" and not switched:
                switched = True
                previous = owner.app.account_revision
                fake.account["workspaceRouting"]["chatgptAccountId"] = "account-b"
                await fake.emit("account/updated", {"authMode": "chatgpt", "planType": "pro"})
                await until(lambda: owner.app.account_revision > previous)
            return await send(frame_type, **fields)

        owner.relay.send = delayed
        await bridge._drain()
        assert switched and not model_calls(fake)
        assert len(bridge.queue) == 1


async def test_unknown_delivery_is_never_replayed_under_new_account(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        original = owner.app.start_turn

        async def lost_response(*args, **kwargs):
            await original(*args, **kwargs)
            raise TimeoutError

        owner.app.start_turn = lost_response
        frame = run_frame()
        bridge = owner.sessions["a"]
        await bridge.on_frame(frame)
        assert bridge.delivery_receipts[frame["client_id"]]["state"] == "unknown"
        fake.account["workspaceRouting"]["chatgptAccountId"] = "account-b"
        await bridge.on_frame(frame)
        assert len(model_calls(fake)) == 1 and not bridge.queue
        assert bridge.delivery_receipts[frame["client_id"]]["state"] == "unknown"


@pytest.mark.parametrize(
    "account", [{}, {"account": {"type": "chatgpt", "planType": "pro"}, "requiresOpenaiAuth": True}]
)
async def test_unknown_account_identity_fails_closed(tmp_path, account):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.account = account
        bridge = owner.sessions["a"]
        await bridge.on_frame(run_frame())
        assert not model_calls(fake) and not bridge.outbox


async def test_legacy_queue_is_not_assigned_to_current_account(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        frame = run_frame()
        await owner.sessions["a"].accept_run(frame)
        data = json.loads(owner.state_path.read_text())
        del data["sessions"]["a"]["pending"][frame["client_id"]]["account_scope"]
        owner.state_path.write_text(json.dumps(data))
        restarted = ProjectController(tmp_path, owner.registry, owner.state_path, owner.endpoint)
        restarted.app, restarted.relay = owner.app, owner.relay
        restored = await restarted.ensure_session("a")
        await restored._drain()
        assert not model_calls(fake) and len(restored.queue) == 1
        await restored.send_history()
        assert any(f.get("category") == "authentication" for f in owner.relay.frames)
