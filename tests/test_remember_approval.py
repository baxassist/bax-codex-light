from __future__ import annotations

import jsonschema
import pytest
from test_bridge import approval, bridge
from test_permissions import access_request


def prefix_request(*, choices=None):
    event = approval()
    prefix = ["npm", "test"]
    event["params"]["proposedExecpolicyAmendment"] = prefix
    event["params"]["availableDecisions"] = choices or [
        "accept",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": prefix}},
        "decline",
    ]
    return event


async def test_always_uses_exact_native_prefix_after_explicit_phone_choice(tmp_path):
    b = bridge(tmp_path)
    b.app.approvals = {"policy": "on-request", "reviewer": "auto_review", "manual": False}
    await b.on_event(prefix_request())
    card = b.relay.sent[-1]
    assert card["remember"] and card["remember_label"] == "Разрешить всегда"
    assert "npm test" in card["rule"] and "Постоянное" in card["rule"]
    assert not b.app.responses
    await b.answer(
        {
            "question_id": card["question_id"],
            "verdict": "allow",
            "remember": True,
            "decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["sh"]}},
            "approvalPolicy": "never",
        }
    )
    assert b.app.responses == [
        (123, {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["npm", "test"]}}})
    ]
    assert b.app.approvals["reviewer"] == "auto_review"
    assert b.app.approvals["policy"] == "on-request"
    with pytest.raises(ValueError):
        await b.answer({"question_id": card["question_id"], "verdict": "allow", "remember": True})


async def test_single_allow_does_not_apply_offered_persistent_rule(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(prefix_request())
    await b.answer({"question_id": next(iter(b.questions)), "verdict": "allow"})
    assert b.app.responses == [(123, {"decision": "accept"})]


@pytest.mark.parametrize("choices", [[], ["accept", "decline"], "invalid"])
async def test_missing_native_choice_cannot_be_added_by_phone(tmp_path, choices):
    b = bridge(tmp_path)
    event = prefix_request()
    event["params"]["availableDecisions"] = choices
    await b.on_event(event)
    card = b.relay.sent[-1]
    assert not card["remember"] and not card["remember_label"]
    with pytest.raises(ValueError, match="не предложил"):
        await b.answer({"question_id": card["question_id"], "verdict": "allow", "remember": True})
    assert not b.app.responses


@pytest.mark.parametrize("legacy", [False, True])
async def test_command_without_permanent_rule_can_be_allowed_for_dialogue(tmp_path, legacy):
    b = bridge(tmp_path)
    event = approval()
    if not legacy:
        event["params"]["availableDecisions"] = ["accept", "acceptForSession", "decline"]
    await b.on_event(event)
    card = b.relay.sent[-1]
    assert card["remember_label"] == "Разрешить на диалог"
    await b.answer({"question_id": card["question_id"], "verdict": "allow", "remember": True})
    assert b.app.responses == [(123, {"decision": "acceptForSession"})]


def network_request(*, host="example.test", rule_host="example.test", action="allow"):
    event = approval()
    rule = {"host": rule_host, "action": action}
    event["params"].update(
        networkApprovalContext={"host": host, "protocol": "https"},
        proposedNetworkPolicyAmendments=[rule],
        availableDecisions=[
            "accept",
            {"applyNetworkPolicyAmendment": {"network_policy_amendment": rule}},
            "decline",
        ],
    )
    return event


async def test_always_network_uses_only_the_native_host_rule(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(network_request())
    card = b.relay.sent[-1]
    assert card["remember_label"] == "Разрешить всегда"
    assert "example.test" in card["rule"] and "все протоколы" in card["rule"]
    await b.answer(
        {
            "question_id": card["question_id"],
            "verdict": "allow",
            "remember": True,
            "host": "*",
            "action": "allow",
        }
    )
    assert b.app.responses == [
        (
            123,
            {
                "decision": {
                    "applyNetworkPolicyAmendment": {
                        "network_policy_amendment": {"host": "example.test", "action": "allow"}
                    }
                }
            },
        )
    ]


@pytest.mark.parametrize("change", ["foreign_host", "deny_rule", "malformed", "no_rules"])
async def test_network_without_exact_allow_rule_does_not_offer_always(tmp_path, change):
    b = bridge(tmp_path)
    event = network_request(
        rule_host="other.test" if change == "foreign_host" else "example.test",
        action="deny" if change == "deny_rule" else "allow",
    )
    if change == "malformed":
        event["params"]["proposedNetworkPolicyAmendments"] = 4
    if change == "no_rules":
        event["params"].pop("proposedNetworkPolicyAmendments")
    await b.on_event(event)
    assert not b.relay.sent[-1]["remember"]
    assert not b.app.responses


@pytest.mark.parametrize("root", [None, "relative", "/tmp/project"])
async def test_file_session_grant_requires_visible_absolute_root(tmp_path, root):
    b = bridge(tmp_path)
    event = approval(method="item/fileChange/requestApproval")
    event["params"]["grantRoot"] = root
    await b.on_event(event)
    card = b.relay.sent[-1]
    if root == "/tmp/project":
        assert root in card["rule"]
        await b.answer({"question_id": card["question_id"], "verdict": "allow", "remember": True})
        assert b.app.responses == [(123, {"decision": "acceptForSession"})]
    else:
        assert not card["remember"]
        with pytest.raises(ValueError):
            await b.answer({"question_id": card["question_id"], "verdict": "allow", "remember": True})


async def test_session_permissions_are_only_requested_paths_and_network(tmp_path):
    b = bridge(tmp_path)
    event = access_request(tmp_path)
    await b.on_event(event)
    card = b.relay.sent[-1]
    assert card["remember_label"] == "Разрешить на диалог"
    await b.answer(
        {
            "question_id": card["question_id"],
            "verdict": "allow",
            "remember": True,
            "permissions": {"fileSystem": {"write": ["/"]}},
            "scope": "forever",
        }
    )
    assert b.app.responses == [(88, {"permissions": event["params"]["permissions"], "scope": "session"})]


@pytest.mark.parametrize("verdict,remember", [("deny", True), ("allow", "yes"), ("allow", 1)])
async def test_invalid_remember_answer_never_sends_native_response(tmp_path, verdict, remember):
    b = bridge(tmp_path)
    await b.on_event(prefix_request())
    with pytest.raises(ValueError):
        await b.answer({"question_id": next(iter(b.questions)), "verdict": verdict, "remember": remember})
    assert not b.app.responses


async def test_auto_review_notifications_do_not_create_manual_phone_prompts(tmp_path):
    b = bridge(tmp_path)
    for method in ("item/autoApprovalReview/started", "item/autoApprovalReview/completed"):
        await b.on_event({"method": method, "params": {"threadId": "current", "turnId": "turn"}})
    assert not b.questions and not b.app.responses and not b.relay.sent


@pytest.mark.parametrize("kind", ["prefix", "network", "file", "permissions"])
async def test_remember_response_matches_real_codex_0160_schema(tmp_path, native_approval_schemas, kind):
    b = bridge(tmp_path)
    if kind == "prefix":
        event, family = prefix_request(), "CommandExecution"
    elif kind == "network":
        event, family = network_request(), "CommandExecution"
    elif kind == "file":
        event, family = approval(method="item/fileChange/requestApproval"), "FileChange"
        event["params"]["grantRoot"] = str(tmp_path)
    else:
        event, family = access_request(tmp_path), "Permissions"
    schemas = native_approval_schemas["families"][family]
    jsonschema.validate(event["params"], schemas["Params"])
    await b.on_event(event)
    await b.answer({"question_id": next(iter(b.questions)), "verdict": "allow", "remember": True})
    jsonschema.validate(b.app.responses[-1][1], schemas["Response"])
