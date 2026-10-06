import json
from types import SimpleNamespace

import pytest
from test_project import project_controller

from bax_codex_light.appserver import RPCError
from bax_codex_light.session_status import snapshot


@pytest.mark.asyncio
async def test_status_whitelists_account_and_all_quota_buckets(tmp_path):
    calls = []

    class App:
        endpoint = "/local.sock"
        configurations = {
            "a": {
                "model": "m",
                "sandbox": {"type": "dangerFullAccess"},
                "instructionSources": ["AGENTS.md"],
                "approvalPolicy": "never",
            }
        }

        async def inspect(self, target, project):
            assert target == "a" and project == tmp_path
            return {"cwd": str(project), "name": "Name", "modelProvider": "openai"}

        async def typed(self, method, params_type, response_type, params):
            calls.append((method, params))
            if method == "account/read":
                return {"account": {"type": "chatgpt", "planType": "pro", "email": "private@example.org"}}
            return {
                "rateLimits": {},
                "rateLimitsByLimitId": {
                    "codex": {"secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 123}},
                    "luna": {"limitName": "Luna Reserve", "secondary": {"usedPercent": 0}},
                },
                "accountId": "private-id",
            }

    details = await snapshot(SimpleNamespace(app=App(), project=tmp_path), "a")
    assert details["account"] == "pro"
    assert len(details["limits"]) == 2
    assert details["limits"][0]["used_pct"] == 35
    assert details["limits"][1]["name"] == "Luna Reserve"
    assert "private@example.org" not in json.dumps(details)
    assert "private-id" not in json.dumps(details)
    assert [c[0] for c in calls] == ["account/read", "account/rateLimits/read"]
    assert all("refreshToken" not in c[1] and "supportsLunaReserve" not in c[1] for c in calls)


@pytest.mark.asyncio
async def test_optional_status_failure_does_not_hide_session(tmp_path):
    class App:
        endpoint = "/local.sock"
        configurations = {}

        async def inspect(self, target, project):
            return {"cwd": str(project)}

        async def typed(self, *args):
            raise RPCError("Unavailable")

    result = await snapshot(SimpleNamespace(app=App(), project=tmp_path), "a")
    assert result["directory"] == str(tmp_path)
    assert result["limits"] == []
    assert result["unavailable"] == ["account", "limits"]


@pytest.mark.asyncio
async def test_status_rejects_other_selected_session(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        await owner.select("a")
        fake.calls.clear()
        await owner.on_frame({"type": "session.status.get", "session": "b", "rid": "stale"})
        assert owner.relay.frames[-1]["type"] == "error"
        assert not fake.calls
