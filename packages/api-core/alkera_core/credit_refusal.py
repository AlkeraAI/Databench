"""A money refusal, named — the single vocabulary for "which allowance ran out".

Lives beside ``overflow.py`` for the reason that module does: shared by the
model gateway (which stamps a ``code`` and the facts a reader needs on its
``402`` body) and the CLI harness translator (which lifts the same code off the
error the agent subprocess surfaces, so the durable transcript row carries
it). Keeping both halves here means the codes, the envelope shape and the
parser can never drift apart across the two apps.

The envelope is the error shape the API already speaks —
``{"type": "error", "error": {"type": "gateway_error", "code": ..., "message":
..., ...}}`` — so an existing client that reads only ``error.message`` keeps
working, and one that reads ``error.code`` learns which allowance refused.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from alkera_core.money import credits_to_nanos, nanos_to_credits
from alkera_core.versioning import VersionedModel


class CreditRefusalCode(StrEnum):
    """Why the gateway would not fund a request. Stable wire values."""

    #: The seat's own cycle allowance (and any prepaid on it) is spent.
    CYCLE_EXHAUSTED = "credit.cycle_exhausted"
    #: The org-wide per-member budget on the org pool is spent for this member.
    MEMBER_BUDGET_EXHAUSTED = "credit.member_budget_exhausted"
    #: A team (or the org) pool itself is spent.
    POOL_EXHAUSTED = "credit.pool_exhausted"
    #: This member's own limit on a team pool is spent; the pool still has room.
    MEMBER_POOL_LIMIT_EXHAUSTED = "credit.member_pool_limit_exhausted"
    #: Nothing funds this caller at all — no seat balance, no pool they may draw.
    NO_FUNDING = "credit.no_funding"
    #: The request exceeds a spend cap that an approver must lift first.
    CAP_APPROVAL_REQUIRED = "credit.cap_approval_required"
    #: A refunded or disputed payment left the account owing money for credit it
    #: had already spent; usage is paused until a top-up pays it.
    BALANCE_OWED = "credit.balance_owed"


_CODE_VALUES = frozenset(code.value for code in CreditRefusalCode)

#: The refusal's plain sentence per code. ``{team}`` is the pool's team name.
MESSAGES: dict[CreditRefusalCode, str] = {
    CreditRefusalCode.CYCLE_EXHAUSTED: "Your cycle allowance is used up.",
    CreditRefusalCode.MEMBER_BUDGET_EXHAUSTED: "Your budget on {team} is used up.",
    CreditRefusalCode.POOL_EXHAUSTED: "The {team} pool is used up.",
    CreditRefusalCode.MEMBER_POOL_LIMIT_EXHAUSTED: "Your limit on the {team} pool is used up.",
    CreditRefusalCode.NO_FUNDING: "Nothing funds this workspace's usage yet.",
    CreditRefusalCode.CAP_APPROVAL_REQUIRED: "This request needs a spend-cap approval.",
    CreditRefusalCode.BALANCE_OWED: "Usage is paused until the balance owed is paid.",
}


class CreditRefusal(VersionedModel):
    """The named refusal, as stamped on the wire and as persisted on the
    transcript row of the turn it ended.

    Carries only facts a reader may see: the code, a plain sentence, the pool's
    team when one refused, the cycle's reset when one applies, a management link
    only when THIS caller can act on it, and — when a source really did turn a
    hold down — what the request was estimated to cost against what was left.
    Those two are the difference between "used up" and a figure to act on, so
    they travel with the sentence rather than being left for a client to guess.
    They are absent together or present together; a lone figure is not readable.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    code: CreditRefusalCode
    message: str
    team_id: str | None = None
    team_name: str | None = None
    resets_at: datetime | None = None
    manage_url: str | None = None
    #: What admission estimated this request at, in nano-USD.
    estimated_cost_nanos: int | None = None
    #: What the source that refused it had left, in nano-USD.
    available_nanos: int | None = None

    def envelope(self) -> dict[str, Any]:
        """The ``{"type": "error", "error": {...}}`` body of the 402."""
        error: dict[str, Any] = {
            "type": "gateway_error",
            "code": self.code.value,
            "message": self.message,
        }
        if self.team_id is not None:
            error["team_id"] = self.team_id
            error["team_name"] = self.team_name
        if self.resets_at is not None:
            error["resets_at"] = self.resets_at.isoformat()
        if self.manage_url is not None:
            error["manage_url"] = self.manage_url
        if self.estimated_cost_nanos is not None and self.available_nanos is not None:
            # Display credits, not nano-USD: credits are the product's user-facing
            # money unit everywhere else, and the raw ledger unit on the wire would
            # invite a client to do its own arithmetic in the wrong one.
            error["estimated_cost_credits"] = nanos_to_credits(self.estimated_cost_nanos)
            error["available_credits"] = nanos_to_credits(self.available_nanos)
        return {"type": "error", "error": error}


def refusal_message(
    code: CreditRefusalCode,
    *,
    team_name: str | None = None,
    estimated_cost_nanos: int | None = None,
    available_nanos: int | None = None,
) -> str:
    """The refusal's sentence, with the shortfall appended when both figures are
    known. A caller that reads only ``message`` still learns how far short it is."""
    sentence = MESSAGES[code].format(team=team_name or "your team")
    if estimated_cost_nanos is None or available_nanos is None:
        return sentence
    needed = nanos_to_credits(estimated_cost_nanos)
    left = nanos_to_credits(available_nanos)
    return f"{sentence} This request is estimated at {needed:,} credits and {left:,} are left."


def credit_refusal_from_error_payload(error_field: Any) -> CreditRefusal | None:
    """Translator-side: the refusal inside whatever the agent surfaced, or None.

    An agent subprocess does not hand the gateway's body over intact: opencode
    wraps it as ``{name, data: {message, responseBody: "<the body, as a
    string>"}}``, a direct client may pass the envelope itself, and a future
    harness may nest it one level deeper. So the payload is walked, every
    stringified JSON value is opened, and the first mapping carrying a known
    ``code`` wins. A message that merely mentions credit never classifies —
    the code is the signal, not the prose.
    """
    for candidate in _mappings(error_field, depth=0):
        code = candidate.get("code")
        if isinstance(code, str) and code in _CODE_VALUES:
            return _parse(candidate, CreditRefusalCode(code))
    return None


def _parse(candidate: dict[str, Any], code: CreditRefusalCode) -> CreditRefusal:
    message = candidate.get("message")
    team_id = candidate.get("team_id")
    team_name = candidate.get("team_name")
    resets_at = candidate.get("resets_at")
    manage_url = candidate.get("manage_url")
    parsed_reset: datetime | None = None
    if isinstance(resets_at, str):
        try:
            parsed_reset = datetime.fromisoformat(resets_at)
        except ValueError:
            parsed_reset = None
    named_team = team_name if isinstance(team_name, str) else None
    # The wire carries display credits; the model keeps nano-USD, so a refusal
    # parsed back off an agent's error reads the same as the one that was sent.
    estimated = _credits_field(candidate.get("estimated_cost_credits"))
    available = _credits_field(candidate.get("available_credits"))
    if estimated is None or available is None:
        estimated = available = None
    return CreditRefusal(
        code=code,
        message=(
            message
            if isinstance(message, str) and message
            else refusal_message(
                code,
                team_name=named_team,
                estimated_cost_nanos=estimated,
                available_nanos=available,
            )
        ),
        team_id=team_id if isinstance(team_id, str) else None,
        team_name=named_team,
        resets_at=parsed_reset,
        manage_url=manage_url if isinstance(manage_url, str) else None,
        estimated_cost_nanos=estimated,
        available_nanos=available,
    )


def _credits_field(value: Any) -> int | None:
    """A whole-credit figure off the wire, back in nano-USD. Anything that is not
    a non-negative integer is discarded rather than coerced — the refusal is
    parsed out of whatever an agent subprocess wrapped it in, so the shapes are
    not trustworthy."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return credits_to_nanos(value)


_MAX_DEPTH = 8


def _mappings(value: Any, *, depth: int) -> list[dict[str, Any]]:
    """Every mapping reachable from ``value`` — through lists, nested dicts and
    JSON encoded inside strings — shallowest first."""
    if depth > _MAX_DEPTH:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.startswith(("{", "[")):
            return []
        try:
            decoded = json.loads(stripped)
        except ValueError:
            return []
        return _mappings(decoded, depth=depth + 1)
    if isinstance(value, dict):
        found: list[dict[str, Any]] = [value]
        for child in value.values():
            found.extend(_mappings(child, depth=depth + 1))
        return found
    if isinstance(value, list):
        found = []
        for child in value:
            found.extend(_mappings(child, depth=depth + 1))
        return found
    return []


__all__ = [
    "MESSAGES",
    "CreditRefusal",
    "CreditRefusalCode",
    "credit_refusal_from_error_payload",
    "refusal_message",
]
