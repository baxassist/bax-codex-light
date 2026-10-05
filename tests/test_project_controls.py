from test_project import project_controller


async def test_rename_targets_exact_session_without_selecting_or_changing_permissions(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.on_frame(
            {
                "type": "session.rename",
                "session": "b",
                "expected_session": "a",
                "title": "  План выпуска  ",
                "rid": "rename",
            }
        )
        assert owner.selected == "a" and fake.threads["b"]["name"] == "План выпуска"
        assert any(
            frame["type"] == "session.renamed" and frame["session"] == "b" and frame["rid"] == "rename"
            for frame in owner.relay.frames
        )
        assert not any(
            call["method"] in {"turn/start", "turn/interrupt", "thread/start"} for call in fake.calls
        )
        count = sum(call["method"] == "thread/name/set" for call in fake.calls)
        for target, expected, title in [("foreign", "a", "Чужой"), ("b", "old", "Поздно"), ("b", "a", "\n")]:
            await owner.on_frame(
                {"type": "session.rename", "session": target, "expected_session": expected, "title": title}
            )
            assert owner.relay.frames[-1]["type"] == "error"
        assert sum(call["method"] == "thread/name/set" for call in fake.calls) == count


async def test_context_uses_last_request_not_accumulated_tokens(tmp_path):
    async with project_controller(tmp_path) as (_, owner):
        await owner.select("a")
        await owner.on_event(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "a",
                    "tokenUsage": {
                        "last": {"totalTokens": 20000},
                        "total": {"totalTokens": 800000},
                        "modelContextWindow": 100000,
                    },
                },
            }
        )
        assert owner.relay.frames[-1]["type"] == "stats"
        assert owner.relay.frames[-1]["context"] == {"used": 20000, "max": 100000}


async def test_background_stop_keeps_original_session_after_selection_changes(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.terminals["a"] = [
            {"processId": "7", "itemId": "exec-a", "command": "pytest tests", "cwd": str(tmp_path)}
        ]
        await owner.select("b")
        fake.terminals["b"] = [
            {"processId": "7", "itemId": "exec-b", "command": "npm run dev", "cwd": str(tmp_path)}
        ]
        await owner.on_frame({"type": "background.get"})
        tasks = owner.relay.frames[-1]["tasks"]
        assert {task["id"] for task in tasks} == {"a:7", "b:7"}
        await owner.on_frame({"type": "background.stop", "session": "a", "task_id": "7"})
        stopped = [call for call in fake.calls if call["method"] == "thread/backgroundTerminals/terminate"]
        assert stopped[-1]["params"] == {"threadId": "a", "processId": "7"}
        assert owner.selected == "b" and fake.terminals["b"] and not fake.terminals["a"]
        assert owner.backgrounds["a:7"]["status"] == "stopped"
        await owner.on_frame({"type": "background.stop", "session": "foreign", "task_id": "7"})
        assert owner.relay.frames[-1]["type"] == "error"
        assert (
            len([call for call in fake.calls if call["method"] == "thread/backgroundTerminals/terminate"])
            == 1
        )


async def test_failed_termination_is_never_reported_as_stopped(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.terminals["a"] = [{"processId": "7", "itemId": "exec", "command": "build", "cwd": str(tmp_path)}]
        await owner.send_background()
        fake.terminated = False
        await owner.on_frame({"type": "background.stop", "session": "a", "task_id": "7"})
        assert owner.relay.frames[-1]["type"] == "error"
    assert owner.backgrounds["a:7"]["status"] == "running"


async def test_manual_compact_is_exact_native_operation_and_cannot_repeat_while_pending(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.calls.clear()
        frame = {"type": "session.compact", "session": "a", "expected_session": "a", "rid": "c"}
        await owner.on_frame(frame)
        assert owner.relay.frames[-1]["type"] == "context.history"
        assert owner.relay.frames[-1]["compacting"] is True
        assert owner.relay.frames[-1]["available"] is False
        assert owner.status()["state"] == "busy"
        assert [c["params"] for c in fake.calls if c["method"] == "thread/compact/start"] == [
            {"threadId": "a"}
        ]
        for target, expected in [("a", "a"), ("b", "a"), ("a", "old"), ("foreign", "a")]:
            await owner.on_frame({**frame, "session": target, "expected_session": expected})
            assert owner.relay.frames[-1]["type"] == "error"
        assert sum(c["method"] == "thread/compact/start" for c in fake.calls) == 1
        assert not any(
            c["method"] in {"thread/resume", "turn/start", "turn/interrupt", "thread/start"}
            for c in fake.calls
        )
        await owner.on_event({"method": "thread/compacted", "params": {"threadId": "a", "turnId": "t"}})
        assert not owner.compacting and owner.relay.frames[-1]["compacting"] is False


async def test_compact_rejects_native_busy_thread_without_interrupting_it(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.threads["a"]["status"] = {"type": "active", "activeFlags": []}
        fake.calls.clear()
        await owner.on_frame(
            {"type": "session.compact", "session": "a", "expected_session": "a", "rid": "busy"}
        )
        assert owner.relay.frames[-1]["type"] == "error"
        assert not owner.compacting
        assert not any(c["method"] in {"thread/compact/start", "turn/interrupt"} for c in fake.calls)


async def test_compaction_ack_precedes_native_rpc_and_journal_recovers_missing_completion(tmp_path):
    async with project_controller(tmp_path) as (_, owner):
        await owner.select("a")
        history = {"available": True, "items": [{"id": "old"}]}

        async def get_history(*args):
            return history

        async def compact(*args):
            assert owner.relay.frames[-1]["rid"] == "c"
            assert owner.relay.frames[-1]["compacting"] is True

        owner.app.compaction_history = get_history
        owner.app.compact_thread = compact
        await owner.on_frame({"type": "session.compact", "session": "a", "expected_session": "a", "rid": "c"})
        await owner.send_context("a")
        assert owner.compacting == {"a"}  # Старая запись не означает новый успех.
        history["items"] = [{"id": "old"}, {"id": "new"}]
        await owner.on_frame({"type": "context.get", "session": "a", "expected_session": "a", "rid": "poll"})
        assert not owner.compacting and not owner.compaction_baselines
        assert owner.relay.frames[-1]["compacting"] is False
        assert owner.relay.frames[-1]["rid"] == "poll"
        assert owner.relay.frames[-1]["items"][-1]["id"] == "new"


async def test_compaction_rpc_timeout_does_not_report_cancelled_operation(tmp_path):
    async with project_controller(tmp_path) as (_, owner):
        await owner.select("a")

        async def compact(*args):
            raise TimeoutError()

        owner.app.compact_thread = compact
        await owner.on_frame({"type": "session.compact", "session": "a", "expected_session": "a", "rid": "c"})
        assert owner.compacting == {"a"}
        assert owner.relay.frames[-1]["compacting"] is True
        assert "не подтвердил" in owner.relay.frames[-1]["error"]
        await owner.on_event({"method": "thread/compacted", "params": {"threadId": "a", "turnId": "t"}})
        assert not owner.compacting
        assert not owner.relay.frames[-1]["error"]
