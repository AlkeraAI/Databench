"""`AuditedRoute` — the route class that records Alkera-staff write actions.

Set as the ``route_class`` on every admin sub-router so that any mutating
request (POST/PUT/PATCH/DELETE) that succeeds (2xx) lands in ``audit_logs``.
Reads and failed/denied requests are not logged — the latter raise before the
handler returns, so the recording code never runs.

The audit write is strictly best-effort and uses its own session (the request's
own unit of work has already committed by the time we get here): a failure to
record is logged as ``audit.write_failed`` and never propagates to the caller.

A completeness test (``apps/backend/tests/test_audit_logs.py``) asserts every
``/admin/v1`` route uses this class, so a future sub-router that forgets to set
``route_class`` fails CI rather than silently going unaudited.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from fastapi import Request, Response
from fastapi.routing import APIRoute

from backend.services.audit import audit_log as audit_log_service

log = get_logger(__name__)

_AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Exact (case-insensitive) field names whose values are scrubbed before storage.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "admin_password",
        "new_password",
        "current_password",
        "token",
        "secret",
        "client_secret",
        "api_key",
        "access_token",
        "refresh_token",
    }
)
_REDACTED = "***"
_MAX_DETAIL_BYTES = 8192


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (_REDACTED if key.lower() in _SENSITIVE_KEYS else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def _build_detail(
    path_params: dict[str, Any], query_params: dict[str, str], raw_body: bytes
) -> dict[str, Any] | None:
    detail: dict[str, Any] = {}
    if path_params:
        detail["path_params"] = {k: str(v) for k, v in path_params.items()}
    if query_params:
        detail["query_params"] = _redact(dict(query_params))
    body = _parse_body(raw_body)
    if body is not None:
        redacted = _redact(body)
        # Guard against a pathologically large payload bloating the audit row.
        if len(json.dumps(redacted, default=str)) <= _MAX_DETAIL_BYTES:
            detail["body"] = redacted
        else:
            detail["body"] = {"_truncated": True}
    return detail or None


class AuditedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        action = self.name

        async def handler(request: Request) -> Response:
            # Read the body up front so Starlette caches it for the endpoint to
            # re-read. Only relevant for mutating methods (the ones we log).
            audited = request.method in _AUDITED_METHODS
            raw_body = await request.body() if audited else b""
            # Named BEFORE the handler runs: a delete leaves nothing to name after.
            target = await _resolve_target(request, raw_body) if audited else None
            response = await original(request)
            if audited and 200 <= response.status_code < 300:
                await _write_entry(request, action, response.status_code, raw_body, target)
            return response

        return handler


async def _resolve_target(request: Request, raw_body: bytes) -> str | None:
    """Best-effort, like the write: a lookup that fails leaves the row without
    a target rather than failing the staff action."""
    try:
        async with AsyncSessionLocal() as session:
            return await audit_log_service.resolve_target(
                session, path_params=dict(request.path_params), body=_parse_body(raw_body)
            )
    except Exception:
        log.warning("audit.target_unresolved", path=request.url.path, exc_info=True)
        return None


async def _write_entry(
    request: Request, action: str, status_code: int, raw_body: bytes, target: str | None
) -> None:
    # Stashed by `require_platform_staff` (the admin_router floor dependency).
    actor = getattr(request.state, "actor", None)
    if actor is None:
        return
    role = actor.platform_role.value if actor.platform_role is not None else None
    detail = _build_detail(dict(request.path_params), dict(request.query_params), raw_body)
    try:
        async with AsyncSessionLocal() as session:
            await audit_log_service.record(
                session,
                actor_id=actor.id,
                actor_email=actor.email,
                actor_platform_role=role,
                action=action,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                detail=detail,
                target=target,
            )
            await session.commit()
    except Exception:
        log.error("audit.write_failed", action=action, path=request.url.path, exc_info=True)
