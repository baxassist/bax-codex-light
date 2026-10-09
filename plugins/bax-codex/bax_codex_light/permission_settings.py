"""Общие режимы разрешений разговора для web и будущего мобильного клиента."""

from __future__ import annotations

PRESETS = {
    "read-only": {
        "name": "Только чтение",
        "description": "Чтение файлов. Изменения и команды вне ограничений требуют разрешения.",
        "approvalPolicy": "on-request",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    },
    "workspace-write": {
        "name": "Работа в проекте",
        "description": "Изменения в проекте. Доступ к сети и действия вне проекта — по разрешению.",
        "approvalPolicy": "on-request",
        "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
    },
    "full-access": {
        "name": "Полный доступ",
        "description": "Доступ к файлам и сети без запросов подтверждения.",
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    },
}


def mode_of(config: dict) -> str:
    policy, sandbox = config.get("approvalPolicy"), config.get("sandbox", {})
    kind = sandbox.get("type")
    if kind == "dangerFullAccess" and policy == "never":
        return "full-access"
    if (
        kind == "readOnly"
        and policy == "on-request"
        and "access" not in sandbox
        and not sandbox.get("networkAccess")
    ):
        return "read-only"
    if (
        kind == "workspaceWrite"
        and policy == "on-request"
        and not sandbox.get("networkAccess")
        and all(root == config.get("cwd") for root in sandbox.get("writableRoots", []))
        and not sandbox.get("excludeTmpdirEnvVar")
        and not sandbox.get("excludeSlashTmp")
    ):
        return "workspace-write"
    return "custom"


def choices(requirements: dict) -> list[dict]:
    modes, approvals = requirements.get("allowedSandboxModes"), requirements.get("allowedApprovalPolicies")
    # Именованные управляемые профили не заменяем набором legacy-политик.
    profiles = requirements.get("allowedPermissionProfiles")
    result = []
    for mode, preset in PRESETS.items():
        sandbox = "danger-full-access" if mode == "full-access" else mode
        allowed = (
            profiles is None
            and (modes is None or sandbox in modes)
            and (approvals is None or preset["approvalPolicy"] in approvals)
        )
        result.append(
            {"mode": mode, "name": preset["name"], "description": preset["description"], "allowed": allowed}
        )
    return result


def options(mode: str) -> dict:
    preset = PRESETS[mode]
    return {"approval_policy": preset["approvalPolicy"], "sandbox_policy": dict(preset["sandboxPolicy"])}
