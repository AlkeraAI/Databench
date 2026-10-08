"""Decisions, and the record a decision leaves behind.

A :class:`Decision` is what a policy returns and what the HTTP layer turns into
a status code. A :class:`DecisionEvent` is what gets written to the event
outbox so a denial is on record even though the request that produced it
rolled back. The two redaction rules live here, stated once:

* the payload carries only the attributes a policy *declared* auditable,
  coerced to short scalars by :func:`audited_attrs`;
* :data:`NEVER_AUDITED_KEYS` can never be declared, and are dropped again at
  payload time as belt and braces.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from alkera_core.authz.enums import Action, Effect
from alkera_core.authz.resource import Resource
from alkera_core.validation.storable_text import storable

#: The outbox event type every decision row carries.
AUTHZ_EVENT_TYPE = "authz.decision"

#: Event-type prefixes only the server may emit. A client-reported audit batch
#: that names one of these is forged, whatever else it says.
SERVER_ONLY_EVENT_PREFIXES: tuple[str, ...] = ("authz.",)


def is_server_only_event_type(event_type: str) -> bool:
    """``True`` when ``event_type`` may only ever originate server-side."""
    return event_type.startswith(SERVER_ONLY_EVENT_PREFIXES)


#: Attribute names that never reach a decision row, however a policy declares
#: its allowlist. Secrets, request bodies and free-form content.
NEVER_AUDITED_KEYS = frozenset(
    {
        "secret",
        "password",
        "token",
        "api_key",
        "access_token",
        "refresh_token",
        "shared_secret",
        "client_secret",
        "body",
        "payload",
        "content",
        "sql",
        "query",
    }
)

#: Longest string an audited attribute may carry.
MAX_AUDITED_STRING = 128

#: Longest resource id a decision row records: the outbox's ``entity_id`` width.
MAX_RECORDED_ID = 255

#: Longest request path a decision row records.
MAX_RECORDED_PATH = 2048

#: Most refused ids one batch summary row lists; the count beside them is exact.
MAX_RECORDED_REFUSALS = 100

#: The ``entity_id`` a batch summary row is filed under: it is about many
#: resources, so it names none of them there and lists them in its payload.
BATCH_ENTITY_ID = "batch"


@dataclass(frozen=True, slots=True)
class Decision:
    """A policy's verdict.

    ``message`` is the HTTP detail text on a denial. ``as_not_found`` asks the
    HTTP layer for a 404 instead of a 403 (an opaque denial that must not
    confirm the resource exists). ``error_code`` makes the 403 detail a
    structured ``{"code", "message"}`` object for clients that key off a code.
    ``unauthenticated`` asks for a 401 instead: the credential the request
    rests on is refused as a credential (a machine credential revoked or
    never minted), not the caller's reach — a client must stop presenting it
    rather than retry. None of those makes sense on an allow, and a decision
    that claims one is refused at construction, as is one that asks for both
    a 404 and a 401.
    """

    effect: Effect
    reason: str
    policy: str
    as_not_found: bool = False
    message: str = ""
    error_code: str | None = None
    unauthenticated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.effect, Effect):
            raise ValueError(f"decision effect must be an Effect, got {self.effect!r}")
        if not self.reason:
            raise ValueError("a decision needs a reason")
        if not self.policy:
            raise ValueError("a decision names the policy that made it")
        if self.effect is Effect.ALLOW and (
            self.as_not_found or self.error_code is not None or self.unauthenticated
        ):
            raise ValueError("an allow carries no not-found, error-code or 401 hint")
        if self.as_not_found and self.unauthenticated:
            raise ValueError("a denial is a 404 or a 401, not both")

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def allow(policy: str, reason: str) -> Decision:
    return Decision(effect=Effect.ALLOW, reason=reason, policy=policy)


def deny(
    policy: str,
    reason: str,
    *,
    message: str,
    as_not_found: bool = False,
    error_code: str | None = None,
    unauthenticated: bool = False,
) -> Decision:
    return Decision(
        effect=Effect.DENY,
        reason=reason,
        policy=policy,
        as_not_found=as_not_found,
        message=message,
        error_code=error_code,
        unauthenticated=unauthenticated,
    )


_DROP = object()


def _scalar(value: object) -> object:
    """Coerce one value to an audit-safe JSON scalar, or ``_DROP``."""
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP
    if isinstance(value, str):
        return storable(value, limit=MAX_AUDITED_STRING)
    if isinstance(value, UUID):
        return str(value)
    return _DROP


def _coerce(value: object) -> object:
    if isinstance(value, set | frozenset | list | tuple):
        items = [_scalar(item) for item in value]
        if not all(isinstance(item, str) for item in items):
            return _DROP
        return sorted(item for item in items if isinstance(item, str))
    return _scalar(value)


def audited_attrs(attrs: Mapping[str, object], allowlist: frozenset[str]) -> dict[str, Any]:
    """The subset of ``attrs`` a decision row may carry.

    Keeps only allowlisted keys that are not in :data:`NEVER_AUDITED_KEYS`.
    Booleans, ints and finite floats pass through; enums become their value;
    strings are cut at :data:`MAX_AUDITED_STRING`; UUIDs become strings; a
    set, frozenset, list or tuple of string-like values (roles, ids) becomes a
    sorted list. Anything else — ``None``, nested mappings, bytes, arbitrary
    objects, mixed collections — is dropped. Never raises.
    """
    out: dict[str, Any] = {}
    for key in sorted(allowlist):
        if key in NEVER_AUDITED_KEYS or key not in attrs:
            continue
        coerced = _coerce(attrs[key])
        if coerced is not _DROP:
            out[key] = coerced
    return out


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """Everything a decision row records.

    ``actor`` is the acting context's audit document; ``attrs`` is already the
    allowlisted, coerced subset (see :func:`audited_attrs`). ``method`` and
    ``path`` are the request line without its query string.

    The resource id and the path are whatever the caller sent — a NUL, a lone
    surrogate, ten kilobytes — and a decision is recorded whatever they are, so
    the row carries them through :func:`storable` rather than verbatim.
    """

    org_id: UUID
    actor: dict[str, Any]
    action: Action
    resource: Resource
    decision: Decision
    attrs: dict[str, Any]
    method: str
    path: str

    @property
    def recorded_id(self) -> str:
        """The resource id as the row's ``entity_id`` can always hold it."""
        return storable(self.resource.id, limit=MAX_RECORDED_ID)

    def outbox_payload(self) -> dict[str, Any]:
        """The JSON payload of the ``authz.decision`` outbox row."""
        team_id = self.resource.team_id
        return {
            "effect": self.decision.effect.value,
            "action": self.action.value,
            "reason": self.decision.reason,
            "policy": self.decision.policy,
            "as_not_found": self.decision.as_not_found,
            "error_code": self.decision.error_code,
            "resource": {
                "type": self.resource.type.value,
                "id": self.recorded_id,
                "team_id": str(team_id) if team_id is not None else None,
            },
            "attrs": {k: v for k, v in self.attrs.items() if k not in NEVER_AUDITED_KEYS},
            "method": self.method,
            "path": storable(self.path, limit=MAX_RECORDED_PATH),
        }


@dataclass(frozen=True, slots=True)
class BatchDecisionEvent:
    """The one row a batch of decisions over a single resource type leaves.

    A listing decides every row it might show, and a row per decision would
    file thousands of rows for one page view, most of them about rows the
    caller simply does not see. So a batch is recorded once: what was asked,
    of which type, how many were allowed and refused, and which were refused
    (the first :data:`MAX_RECORDED_REFUSALS`, each through :func:`storable`;
    the count is exact either way).
    """

    org_id: UUID
    actor: dict[str, Any]
    action: Action
    resource_type: str
    allowed: int
    refused_ids: tuple[str, ...]
    method: str
    path: str

    @property
    def effect(self) -> str:
        """``allow`` when nothing was refused, ``deny`` when nothing was
        allowed, ``partial`` between: the field a single decision's row carries,
        so a reader filtering refusals finds a batch that refused everything."""
        if not self.refused_ids:
            return Effect.ALLOW.value
        if not self.allowed:
            return Effect.DENY.value
        return "partial"

    def outbox_payload(self) -> dict[str, Any]:
        """The JSON payload of the summary's ``authz.decision`` outbox row."""
        listed = self.refused_ids[:MAX_RECORDED_REFUSALS]
        return {
            "batch": True,
            "effect": self.effect,
            "action": self.action.value,
            "resource": {"type": self.resource_type},
            "allowed": self.allowed,
            "refused": len(self.refused_ids),
            "refused_ids": [storable(rid, limit=MAX_RECORDED_ID) for rid in listed],
            "method": self.method,
            "path": storable(self.path, limit=MAX_RECORDED_PATH),
        }


__all__ = [
    "AUTHZ_EVENT_TYPE",
    "BATCH_ENTITY_ID",
    "MAX_AUDITED_STRING",
    "MAX_RECORDED_ID",
    "MAX_RECORDED_PATH",
    "MAX_RECORDED_REFUSALS",
    "NEVER_AUDITED_KEYS",
    "SERVER_ONLY_EVENT_PREFIXES",
    "BatchDecisionEvent",
    "Decision",
    "DecisionEvent",
    "allow",
    "audited_attrs",
    "deny",
    "is_server_only_event_type",
]
