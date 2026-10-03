"""Постоянный ключ остаётся внутри процесса; MCP возвращает только состояние связи."""

from urllib.parse import urlsplit

import httpx

from .registry import Registration

DEFAULT_API = "https://api.baxassist.com/api/v1"


async def redeem(code: str, api: str = DEFAULT_API) -> Registration:
    url = urlsplit(api)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or not url.hostname
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})
        )
    ):
        raise ValueError("Для подключения требуется HTTPS; HTTP разрешён только на localhost")
    code = code.strip().upper().replace("-", "").replace(" ", "")
    if len(code) != 12 or any(c not in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" for c in code):
        raise ValueError("Код должен состоять из 12 букв и цифр. Получите его в Баксе")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = await client.post(api.rstrip("/") + "/agents/pairing/redeem", json={"code": code})
    if response.status_code != 200:
        raise ValueError("Код неверен, истёк или уже использован. Получите новый в Баксе")
    payload = response.json()
    return Registration.from_key(payload["key"], payload["relay"])
