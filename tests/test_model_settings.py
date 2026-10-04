from test_project import project_controller

from bax_codex_light.project import ProjectController


async def test_busy_choice_is_durable_and_only_changes_next_start(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        a = owner.sessions["a"]
        a.state, a.turn_id = "busy", "turn-a"
        policies = dict(owner.template)
        fake.calls.clear()
        await owner.on_frame(
            {"type": "model.set", "session": "a", "model": "other-model", "effort": "low", "rid": "pick"}
        )
        receipt = owner.relay.frames[-1]
        assert receipt["type"] == "model.settings" and receipt["rid"] == "pick"
        assert receipt["pending"] is True and receipt["current_model"] == "chosen-model"
        assert owner.template == policies
        assert all(call["method"] in {"thread/read", "model/list"} for call in fake.calls)
        restored = ProjectController(tmp_path, owner.registry, owner.state_path)
        assert restored.saved["a"]["model_selection"] == {"model": "other-model", "effort": "low"}
        await owner.on_frame({"type": "run", "session": "a", "text": "Поправка сейчас", "cid": "steer"})
        steer = next(c for c in fake.calls if c["method"] == "turn/steer")
        assert steer["params"]["expectedTurnId"] == "turn-a"
        assert "model" not in steer["params"] and "effort" not in steer["params"]
        assert a.model_selection is not None
        fake.threads["a"]["status"] = {"type": "idle"}
        a.state, a.turn_id = "ready", ""
        await owner.on_frame({"type": "run", "session": "a", "text": "Следующая задача", "cid": "start"})
        start = next(c for c in fake.calls if c["method"] == "turn/start")
        assert start["params"]["model"] == "other-model" and start["params"]["effort"] == "low"
        assert "approvalPolicy" not in start["params"] and "sandbox" not in start["params"]
        assert fake.threads["b"]["model"] == "chosen-model"
        assert a.model_selection is None
        assert not any(c["method"] == "turn/interrupt" for c in fake.calls)


async def test_stale_session_and_unsupported_effort_do_not_change_any_choice(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        for session, model, effort in [
            ("b", "other-model", "low"),
            ("foreign", "other-model", "low"),
            ("a", "other-model", "max"),
            ("a", "unknown", "low"),
        ]:
            await owner.on_frame(
                {"type": "model.set", "session": session, "model": model, "effort": effort, "rid": session}
            )
            assert owner.relay.frames[-1]["type"] == "model.settings"
            assert owner.relay.frames[-1]["error"]
            assert owner.sessions["a"].model_selection is None
        assert not any(c["method"] in {"turn/start", "turn/interrupt"} for c in fake.calls)
