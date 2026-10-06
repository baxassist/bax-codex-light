"""Безопасный снимок /status: только разрешённые поля, без токенов и почты."""

from openai_codex.generated import v2_all as schemas

from .appserver import RPCError


async def snapshot(owner, target: str) -> dict:
    app = owner.app
    thread = await app.inspect(target, owner.project)
    config = app.configurations.get(target, {})
    mode = config.get("collaborationMode") or {}
    sandbox = config.get("sandbox") or {}
    result = {
        "server": "Local background server" if "://" not in app.endpoint else "Local WebSocket server",
        "directory": thread["cwd"],
        "model": thread.get("model") or config.get("model"),
        "effort": thread.get("reasoningEffort") or config.get("reasoningEffort"),
        "provider": thread.get("modelProvider") or config.get("modelProvider"),
        "permissions": (config.get("activePermissionProfile") or {}).get("id") or sandbox.get("type"),
        "approval_policy": config.get("approvalPolicy"),
        "instructions": config.get("instructionSources"),
        "collaboration_mode": mode.get("mode"),
        "thread_name": thread.get("name"),
        "limits": [],
        "unavailable": [],
    }
    permission_names = {
        "dangerFullAccess": "Full Access",
        "danger-full-access": "Full Access",
        "workspaceWrite": "Workspace Write",
        "workspace-write": "Workspace Write",
        "readOnly": "Read Only",
        "read-only": "Read Only",
    }
    result["permissions"] = permission_names.get(result["permissions"], result["permissions"])
    if result["collaboration_mode"] == "default":
        result["collaboration_mode"] = "Default"
    elif result["collaboration_mode"] == "plan":
        result["collaboration_mode"] = "Plan"
    # Account/read без refreshToken не меняет вход и не запускает обновление токенов.
    try:
        account = await app.typed("account/read", schemas.GetAccountParams, schemas.GetAccountResponse, {})
        account = account.get("account") or {}
        result["account"] = account.get("planType") or account.get("type")
    except (RPCError, TimeoutError, OSError, ValueError):
        result["unavailable"].append("account")
    try:
        limits = await app.typed(
            "account/rateLimits/read",
            schemas.GetAccountRateLimitsParams,
            schemas.GetAccountRateLimitsResponse,
            {"excludeResetCreditDetails": True},
        )
        buckets = limits.get("rateLimitsByLimitId") or {"codex": limits["rateLimits"]}
        for bucket_id, raw in buckets.items():
            bucket = schemas.RateLimitSnapshot.model_validate(raw).model_dump(mode="json", by_alias=True)
            for kind in ("primary", "secondary"):
                window = bucket.get(kind)
                if window:
                    result["limits"].append(
                        {
                            "id": f"{bucket_id}:{kind}",
                            "name": bucket.get("limitName") or bucket_id,
                            "used_pct": window["usedPercent"],
                            "minutes": window.get("windowDurationMins"),
                            "resets_at": window.get("resetsAt"),
                        }
                    )
    except (RPCError, TimeoutError, OSError, ValueError):
        result["unavailable"].append("limits")
    return result
