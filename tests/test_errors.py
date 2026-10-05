import pytest
from test_project import project_controller

from bax_codex_light.errors import failure_fields, safe_message


@pytest.mark.parametrize(
    "info,category,action",
    [
        ("serverOverloaded", "capacity", "choose_model"),
        ("usageLimitExceeded", "quota", "check_limits"),
        ("unauthorized", "authentication", "open_codex"),
        ("sandboxError", "permission", "open_codex"),
        ("contextWindowExceeded", "context", "compact"),
        ({"httpConnectionFailed": {"httpStatusCode": 429}}, "rate_limit", "wait"),
        ({"responseStreamDisconnected": {"httpStatusCode": None}}, "connection", "wait"),
        ("future_code", "unknown", "none"),
    ],
)
def test_native_error_categories(info, category, action):
    result = failure_fields(
        "codex_turn_failed", "Точная причина", operation="turn", turn_id="t", info=info, delivery="accepted"
    )
    assert result["category"] == category and result["action"] == action
    assert "Точная причина" in result["message"]
    assert result["delivery"] == "accepted"


def test_safe_details_never_include_credentials_or_paths():
    text = safe_message(
        "Bearer abc123 sk-test-secret token=private /Users/max/private.txt https://api.test/?token=secret"
    )
    for secret in ("abc123", "sk-test-secret", "private.txt", "token=secret", "api.test"):
        assert secret not in text


def test_same_turn_updates_same_error_and_different_turn_is_separate():
    def error(turn, **kw):
        return failure_fields("codex_turn_failed", "error", operation="turn", turn_id=turn, **kw)

    assert error("a")["error_id"] == error("a", will_retry=True)["error_id"]
    assert error("a")["error_id"] != error("b")["error_id"]
    assert error("a", will_retry=True)["action"] == "wait"
    assert error("a", delivery="unknown")["action"] == "check_history"


async def test_live_failure_and_completion_use_same_identity_and_keep_other_session(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        await owner.select("b")
        failure = {"message": "provider failure", "codexErrorInfo": "usageLimitExceeded"}
        await owner.on_event(
            {
                "method": "error",
                "params": {"threadId": "a", "turnId": "failed-a", "error": failure, "willRetry": True},
            }
        )
        assert owner.sessions["a"].last_failure is None
        await owner.on_event(
            {
                "method": "turn/completed",
                "params": {"threadId": "a", "turn": {"id": "failed-a", "status": "failed", "error": failure}},
            }
        )
        errors = [f for f in owner.relay.frames if f["type"] == "error"]
        assert errors[-2]["error_id"] == errors[-1]["error_id"]
        assert errors[-1]["category"] == "quota" and errors[-1]["delivery"] == "accepted"
        assert errors[-1]["session"] == "a" and owner.selected == "b"
        await owner.on_event({"method": "turn/started", "params": {"threadId": "a", "turn": {"id": "next"}}})
        assert owner.sessions["a"].last_failure is None
        assert not any(c["method"] in {"turn/start", "turn/steer"} for c in fake.calls)


def test_rpc_native_cause_and_timeout_are_distinct():
    from bax_codex_light.appserver import RPCRejected

    result = failure_fields(
        "session_command_failed",
        RPCRejected("error", {"codexErrorInfo": "unauthorized"}),
        operation="model.set",
    )
    assert result["category"] == "authentication" and result["native_code"] == "unauthorized"
    result = failure_fields("session_command_failed", TimeoutError(), operation="history")
    assert result["category"] == "history" and result["action"] == "reload_history"
    assert result["message"]
    denied = failure_fields(
        "codex_turn_failed",
        "Forbidden",
        operation="turn",
        info={"httpConnectionFailed": {"httpStatusCode": 403}},
    )
    assert denied["category"] == "permission"


def test_quoted_secret_and_jwt_redaction():
    text = safe_message(
        '"token": "sensitive value" secret=private eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.signature'
    )
    assert "sensitive" not in text and "private" not in text and "eyJ" not in text
    assert "Project" not in safe_message("cannot read '/Users/max/My Project/private.txt'")
