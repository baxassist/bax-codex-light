from bax_codex_light.session_view import SessionView


def snapshot(view, **kwargs):
    return view.snapshot(
        state="ready", question=None, supports=["run", "answer", "cancel", "session.compact"], **kwargs
    )


def test_source_resolves_only_exact_delivery_and_places_failure_at_its_turn():
    view = SessionView()
    for rid in ("a", "b"):
        view.observe("error", {"error_id": rid, "rid": rid, "delivery": "unknown", "turn_id": rid})
    view.observe("message", {"kind": "assistant", "id": 30, "message_id": "native", "turn_id": "b"})
    view.observe("message", {"kind": "user", "rid": "a", "message_id": "native-user"})
    value = snapshot(view)
    assert len(value["failures"]) == 1
    assert value["failures"][0]["rid"] == "b"
    assert value["failures"][0]["after_message_id"] == "native"
    assert value["actions"] == ["run", "session.compact"]


def test_retry_error_disappears_but_final_failed_turn_is_not_a_delivery_retry():
    view = SessionView()
    view.observe("error", {"error_id": "retry", "turn_id": "t", "will_retry": True, "delivery": "accepted"})
    view.observe("error.resolved", {"turn_id": "t"})
    assert snapshot(view)["failures"] == []
    view.observe("error", {"error_id": "final", "turn_id": "t", "delivery": "accepted"})
    view.observe("error.resolved", {"turn_id": "t"})
    assert len(snapshot(view)["failures"]) == 1


def test_snapshot_is_immutable_versioned_and_questions_actions_are_source_owned():
    view = SessionView()
    question = {"question_id": "q", "text": "Разрешить?"}
    old = view.snapshot(
        state="waiting", question=question, supports=["run", "cancel", "answer", "session.compact"]
    )
    question["text"] = "Изменилось"
    new = snapshot(view)
    assert old["question"]["text"] == "Разрешить?"
    assert old["actions"] == ["answer", "cancel", "run"]
    assert new["question"] is None and new["revision"] > old["revision"]
    assert snapshot(view) is None
    assert snapshot(view, force=True)["revision"] > new["revision"]
    assert snapshot(SessionView())["epoch"] != new["epoch"]


def test_reconnect_native_completion_removes_old_error_but_not_newer_live_failure():
    view = SessionView()
    view.observe("error", {"error_id": "old", "turn_id": "t", "delivery": "accepted"})
    baseline = view.revision
    view.observe("error", {"error_id": "new", "turn_id": "t", "delivery": "accepted"})
    assert view.reconcile_turns([{"id": "t", "status": "completed"}], baseline) == {"old"}
    assert [f["error_id"] for f in snapshot(view)["failures"]] == ["new"]
