import json

from test_response_metadata import append, fixture


def test_restored_context_uses_last_request_and_verified_model(tmp_path):
    path, metadata = fixture(tmp_path)
    append(path, "turn", "gpt-test")
    with path.open("a") as file:
        file.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": 12000},
                            "total_token_usage": {"total_tokens": 700000},
                            "model_context_window": 100000,
                        },
                    },
                }
            )
            + "\n"
        )
    metadata.refresh()
    assert metadata.token_usage == {"used": 12000, "max": 100000, "model": "gpt-test"}
