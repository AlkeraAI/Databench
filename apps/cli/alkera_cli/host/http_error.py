"""Shared extraction of the backend's error sentence from HTTP responses."""

from __future__ import annotations

from typing import Any

import httpx


def response_error_detail(response: httpx.Response) -> str:
    """Return a canonical-envelope or bare FastAPI error sentence, else empty."""
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    detail = error.get("message") if isinstance(error, dict) else None
    if not isinstance(detail, str) or not detail:
        detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return str(detail) if detail is not None else ""


#: Statuses a server answers when the request was sound and only the moment
#: was wrong: throttled, or briefly unable to serve.
RETRYABLE_STATUSES = frozenset({429, 503})


def response_is_retryable(response: httpx.Response) -> bool:
    """Whether the server asked to be tried again: a throttle or an
    unavailable answer, or an error envelope whose details say
    ``retryable``."""
    if response.status_code in RETRYABLE_STATUSES:
        return True
    try:
        payload: Any = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    return isinstance(details, dict) and details.get("retryable") is True


__all__ = ["RETRYABLE_STATUSES", "response_error_detail", "response_is_retryable"]
