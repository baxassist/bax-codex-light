"""ERR-1: безопасная причина, область и идентичность сбоя, без повторной доставки."""

from __future__ import annotations

import re
from hashlib import sha256


def safe_message(value: object) -> str:
    text = str(value or "").strip()
    # additionalDetails и сырые RPC-ответы никогда не отправляем. Из message убираем
    # credentials, URL (включая query), абсолютные локальные пути и управляющие символы.
    text = re.sub(r"(?i)\b(?:bearer\s+\S+|sk-[\w-]+|bax:[^\s]+)", "[скрыто]", text)
    text = re.sub(r"(?i)\b(?:api[_-]?key|token|secret|authorization)\s*[:=]\s*[^\s,;]+", "[скрыто]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?", "[скрыто]", text)
    text = re.sub(
        r'(?i)["\']?(?:api[_-]?key|token|secret|authorization)["\']?\s*[:=]\s*["\'][^"\']*["\']',
        "[скрыто]",
        text,
    )
    text = re.sub(r"https?://\S+", "[адрес скрыт]", text)
    text = re.sub(r'["\'](?:/|[A-Za-z]:\\)[^"\']*["\']', "[путь скрыт]", text)
    text = re.sub(r"(?<!\w)(?:/[\w.~-]+){2,}[^\s,;]*|[A-Za-z]:\\[^\s]+", "[путь скрыт]", text)
    return " ".join(text.split())[:600]


REASONS = {
    "contextWindowExceeded": ("context", "Контекст запроса превышает окно модели.", "compact"),
    "sessionBudgetExceeded": ("quota", "Исчерпан бюджет этого разговора Codex.", "check_limits"),
    "usageLimitExceeded": ("quota", "Достигнут лимит использования Codex.", "check_limits"),
    "rateLimitExceeded": ("rate_limit", "Превышена частота запросов к модели.", "wait"),
    "serverOverloaded": ("capacity", "Модель сейчас перегружена.", "choose_model"),
    "flexUnavailable": ("capacity", "Модель сейчас недоступна для выбранного режима.", "choose_model"),
    "unauthorized": ("authentication", "Codex не прошёл авторизацию у провайдера.", "open_codex"),
    "sandboxError": ("permission", "Ошибка выполнения в песочнице Codex.", "open_codex"),
    "tooManyDenials": ("permission", "Выполнение остановлено после отказов в разрешениях.", "open_codex"),
    "cyberPolicy": ("policy", "Запрос остановлен политикой модели.", "edit_request"),
    "misalignmentPolicyViolation": ("policy", "Запрос остановлен политикой модели.", "edit_request"),
    "badRequest": ("request", "Модель отклонила параметры запроса.", "edit_request"),
    "internalServerError": ("provider", "Внутренняя ошибка провайдера модели.", "wait"),
}


def failure_fields(
    code: str,
    message: object,
    *,
    operation: str,
    scope: str = "session",
    turn_id: str = "",
    rid: str | None = None,
    delivery: str = "not_applicable",
    info: object = None,
    will_retry: bool = False,
) -> dict:
    if info is None:
        info = getattr(message, "native_info", None)
    native_code = info if isinstance(info, str) else next(iter(info), "") if isinstance(info, dict) else ""
    http_status = (
        next((v.get("httpStatusCode") for v in info.values() if isinstance(v, dict)), None)
        if isinstance(info, dict)
        else None
    )
    category, explanation, action = REASONS.get(native_code, ("unknown", "", "none"))
    reason = safe_message(message)
    source = "plugin" if isinstance(message, ValueError) else "codex"
    if isinstance(message, ValueError) and category == "unknown":
        category, action = "request", "edit_request"
    if category == "unknown":
        low = reason.lower()
        if "model is at capacity" in low or http_status == 503:
            category, explanation, action = REASONS["serverOverloaded"]
        elif http_status in {401, 403}:
            category, explanation, action = REASONS["unauthorized"]
        elif http_status == 429:
            category, explanation, action = REASONS["rateLimitExceeded"]
        elif native_code in {
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseStreamDisconnected",
            "responseTooManyFailedAttempts",
        }:
            category, explanation, action = "connection", "Связь Codex с провайдером прервалась.", "wait"
    if isinstance(message, TimeoutError):
        reason = reason or "Codex не подтвердил результат операции вовремя."
        category, action = (
            "timeout",
            "check_history" if operation in {"run", "session.compact"} else "open_codex",
        )
    if operation == "permission.request" and category == "unknown":
        category, action = "permission", "open_codex"
    if operation in {"history", "subscribe"} and category in {"unknown", "timeout", "request"}:
        category, action = "history", "reload_history"
    elif operation == "connect" and category == "unknown":
        category, action = "connection", "open_codex"
    if delivery == "unknown":
        category, action = "delivery", "check_history"
    elif delivery == "rejected" and category == "unknown":
        category, action = "delivery", "edit_request"
    if will_retry:
        action = "wait"
    actions = {
        "choose_model": "Повторите запрос позже или выберите другую модель.",
        "check_limits": "Проверьте лимиты в Codex; время их обновления пока неизвестно.",
        "wait": "Codex повторяет запрос самостоятельно." if will_retry else "Повторите запрос позже.",
        "compact": "Сожмите контекст или начните новый разговор.",
        "open_codex": "Проверьте настройки и сообщения в Codex на компьютере.",
        "edit_request": "Проверьте запрос перед новой отправкой.",
        "reload_history": "Обновите историю разговора; новую задачу отправлять не нужно.",
        "check_history": "Проверьте историю перед повторной отправкой; задача могла быть принята.",
    }
    text = " ".join(dict.fromkeys(x for x in (explanation, reason, actions.get(action, "")) if x))
    identity = turn_id or rid or sha256(text.encode()).hexdigest()[:20]
    return {
        "code": code,
        "message": text or "Codex сообщил об ошибке без подробностей.",
        "error_id": f"{operation}:{identity}:{code}",
        "scope": scope,
        "source": source,
        "operation": operation,
        "category": category,
        "native_code": native_code,
        "turn_id": turn_id,
        "rid": rid,
        "delivery": delivery,
        "action": action,
        "will_retry": will_retry,
    }
