from conftest import entry
from test_bridge import approval, bridge
from test_history import Pages

from bax_codex_light import files


def test_truncation_preserves_valid_utf8(tmp_path):
    path = "long.txt"
    text = "а" * (files.MAX_FILE // 2 - 1) + "€" + "конец"
    (tmp_path / path).write_text(text)
    result = files.read(tmp_path, path, [path])
    assert "error" not in result
    assert result["truncated"] is True
    assert text.startswith(result["text"])


async def test_queued_user_echo_uses_same_id_as_codex_history(tmp_path):
    b = bridge(tmp_path)
    await b.on_frame({"type": "run", "text": "задача"})
    client_id, text = b.queue[0]
    echo = next(frame for frame in b.relay.sent if frame["type"] == "message")
    user = entry("server-item", "userMessage", text)
    user["item"]["clientId"] = client_id
    b.history.app = Pages({None: {"data": [user], "nextCursor": None}})
    assert (await b.history.page())[0]["id"] == echo["id"]


async def test_queue_overflow_is_rejected_without_losing_tasks(tmp_path):
    b = bridge(tmp_path)
    for i in range(11):
        await b.on_frame({"type": "run", "text": f"задача {i}"})
    assert len(b.queue) == 10
    assert b.relay.sent[-1]["type"] == "error"
    assert [text for _id, text in b.queue] == [f"задача {i}" for i in range(10)]


async def test_other_permission_request_is_never_answered(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(approval(method="item/permissions/requestApproval"))
    assert not b.questions
    assert not b.app.responses
    assert not b.relay.sent
