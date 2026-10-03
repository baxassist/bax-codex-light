"""Запрошенный доступ проверяем по SDK и показываем до решения человека."""

from __future__ import annotations

import json
from pathlib import Path

from openai_codex.generated import v2_all as schemas


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
