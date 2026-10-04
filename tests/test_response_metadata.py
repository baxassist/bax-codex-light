import json

import pytest

from bax_codex_light.response_metadata import ResponseMetadata


def fixture(tmp_path):
    path = tmp_path / "sessions" / "exact.jsonl"
    path.parent.mkdir()
    thread = {"id": "exact", "cwd": str(tmp_path), "path": str(path), "model": "current-never-use"}
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "exact", "cwd": str(tmp_path)}}) + "\n"
    )
    return path, ResponseMetadata(thread, tmp_path)


def append(path, turn, model, text="Ответ", item_id="raw", timestamp="2026-10-04T12:00:00Z"):
    rows = [
        {
            "type": "turn_context",
            "payload": {"turn_id": turn, "cwd": str(path.parent.parent), "model": model, "effort": "high"},
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": item_id,
                "content": [{"type": "output_text", "text": text}],
            },
        },
    ]
    with path.open("a") as file:
        file.write("".join(json.dumps(row) + "\n" for row in rows))


def entry(turn, item_id="legacy", text="Ответ"):
    return {"turnId": turn, "item": {"id": item_id, "type": "agentMessage", "text": text}}


def test_history_uses_own_turn_parameters_and_never_current_model(tmp_path):
    path, metadata = fixture(tmp_path)
    append(path, "old", "old-model", item_id="raw-old")
    append(path, "new", "new-model", item_id="raw-new", timestamp="2026-10-04T13:00:00Z")
    values = metadata.decorate([entry("old"), entry("new"), entry("unknown")])
    assert values[("old", "legacy")]["model"] == "old-model"
    assert values[("new", "legacy")]["model"] == "new-model"
    assert values[("new", "legacy")]["created_at"] - values[("old", "legacy")]["created_at"] == 3600
    assert ("unknown", "legacy") not in values
    assert metadata.decorate([entry("old")])[("old", "legacy")] == values[("old", "legacy")]


def test_ambiguous_repeated_reply_omits_guessed_time(tmp_path):
    path, metadata = fixture(tmp_path)
    append(path, "same", "model", item_id="raw-1")
    append(path, "same", "model", item_id="raw-2", timestamp="2026-10-04T13:00:00Z")
    partial = metadata.decorate([entry("same")])[("same", "legacy")]
    assert partial["model"] == "model" and "created_at" not in partial
    full = metadata.decorate([entry("same", "first"), entry("same", "second")])
    assert full[("same", "second")]["created_at"] > full[("same", "first")]["created_at"]


def test_foreign_journal_is_rejected_and_unfinished_line_is_retried(tmp_path):
    path, metadata = fixture(tmp_path)
    append(path, "turn", "model")
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    assert metadata.decorate([entry("turn")]) == {}
    with path.open("ab") as file:
        file.write(b"\n")
    assert metadata.decorate([entry("turn")])[("turn", "legacy")]["model"] == "model"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "foreign", "cwd": str(tmp_path)}}) + "\n"
    )
    with pytest.raises(ValueError, match="другому"):
        metadata.decorate([entry("turn")])


def test_disallowed_path_does_not_read_content_and_sdk_timestamp_is_kept(tmp_path):
    metadata = ResponseMetadata(
        {"id": "exact", "cwd": str(tmp_path), "path": str(tmp_path / "unrelated.jsonl")}, tmp_path
    )
    value = entry("turn")
    value["completedAtMs"] = 1791115200000
    assert metadata.path is None
    assert metadata.decorate([value])[("turn", "legacy")] == {"created_at": 1791115200}
