"""Запрошенный доступ проверяем по SDK и показываем до решения человека."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from openai_codex.generated import v2_all as schemas


@dataclass(frozen=True)
class RememberApproval:
    response: dict
    label: str
    rule: str


def remember_approval(method: str, params: dict, permissions: dict | None) -> RememberApproval | None:
    """Срок и правило предлагает Codex; телефон выбирает только готовый вариант."""
    if method == "item/permissions/requestApproval":
        if not isinstance(permissions, dict):
            return None
        return RememberApproval(
            {"permissions": permissions, "scope": "session"},
            "Разрешить на диалог",
            "Показанные сеть и пути — до конца этого диалога",
        )
    if method == "item/fileChange/requestApproval":
        root = params.get("grantRoot")
        if isinstance(root, str) and Path(root).is_absolute():
            return RememberApproval(
                {"decision": "acceptForSession"},
                "Разрешить на диалог",
                f"Изменения в {root} — до конца этого диалога",
            )
        return None
    if method != "item/commandExecution/requestApproval":
        return None
    choices = params.get("availableDecisions")
    if choices is not None and not isinstance(choices, list):
        return None

    def offered(decision):
        return choices is None or decision in choices

    network = params.get("networkApprovalContext")
    if isinstance(network, dict):
        rules = params.get("proposedNetworkPolicyAmendments")
        if not isinstance(rules, list):
            return None
        for rule in rules:
            if (
                isinstance(rule, dict)
                and set(rule) == {"host", "action"}
                and isinstance(rule["host"], str)
                and rule["host"]
                and rule["host"] == network.get("host")
                and rule["action"] == "allow"
            ):
                decision = {"applyNetworkPolicyAmendment": {"network_policy_amendment": dict(rule)}}
                if offered(decision):
                    return RememberApproval(
                        {"decision": decision},
                        "Разрешить всегда",
                        f"Постоянное правило Codex для адреса {rule['host']} (все протоколы)",
                    )
        # acceptForSession для managed network может иметь иной срок; не угадываем.
        return None
    prefix = params.get("proposedExecpolicyAmendment")
    if isinstance(prefix, list) and prefix and all(isinstance(part, str) and part for part in prefix):
        decision = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": list(prefix)}}
        if offered(decision):
            return RememberApproval(
                {"decision": decision},
                "Разрешить всегда",
                f"Постоянное правило Codex для команд с началом: {shlex.join(prefix)}",
            )
    if (
        params.get("kind", "command") == "command"
        and isinstance(params.get("command"), str)
        and params["command"]
        and offered("acceptForSession")
    ):
        return RememberApproval(
            {"decision": "acceptForSession"},
            "Разрешить на диалог",
            "Codex запомнит это подтверждение до конца текущего диалога",
        )
    return None


def permission_profile(value: dict) -> dict:
    profile = schemas.RequestPermissionProfile.model_validate_json(
        json.dumps(value, allow_nan=False), strict=True, extra="forbid"
    )
    result = profile.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
    for mode in ("read", "write"):
        for path in (result.get("fileSystem") or {}).get(mode) or []:
            if not Path(path).is_absolute():
                raise ValueError("Путь запрошенного доступа должен быть абсолютным")
    for entry in (result.get("fileSystem") or {}).get("entries") or []:
        path = entry["path"]
        if path["type"] == "path" and not Path(path["path"]).is_absolute():
            raise ValueError("Путь запрошенного доступа должен быть абсолютным")
    return result


def permission_details(profile: dict) -> dict[str, str]:
    details = {}
    if (profile.get("network") or {}).get("enabled"):
        details["Сеть"] = "Сетевой доступ"
    filesystem = profile.get("fileSystem") or {}
    labels = {"read": "Чтение", "write": "Запись", "deny": "Запрет"}
    paths = [(mode, path) for mode in ("read", "write") for path in filesystem.get(mode) or []]
    for entry in filesystem.get("entries") or []:
        path = entry["path"]
        if path["type"] == "path":
            text = path["path"]
        elif path["type"] == "glob_pattern":
            text = f"По шаблону: {path['pattern']}"
        else:
            value = path["value"]
            text = {
                "root": "Все файлы (/)",
                "minimal": "Минимальные системные пути",
                "project_roots": "Папки проекта",
                "tmpdir": "Системная временная папка",
                "slash_tmp": "/tmp",
            }.get(value["kind"], value.get("path", value["kind"]))
            if value.get("subpath"):
                text += f" / {value['subpath']}"
        paths.append((entry["access"], text))
    for index, (mode, path) in enumerate(paths, 1):
        details[f"{labels[mode]} {index}"] = path
    if filesystem.get("globScanMaxDepth"):
        details["Глубина поиска по шаблону"] = str(filesystem["globScanMaxDepth"])
    return details
