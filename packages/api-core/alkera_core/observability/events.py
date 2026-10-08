"""Analytics-grade product events for the growth engine.

`emit_event` writes one structured line on the dedicated `alkera.events` logger
with a stable schema (`event`, `kind="analytics"`, `user_id`, `org_id`, plus
typed props). In `local` that renders to the console; in staging/production it is
JSON on stderr — trivially routed to a downstream analytics sink later with no
code change (same "ready to enable" posture as Sentry).

Props are scrubbed defensively, but the contract is: **ids, counts, enums,
durations — never content** (no prompts, chat text, file bodies, emails-as-props).
The authed identity (`user_id`/`org_id`) is the join key.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from alkera_core.logging import get_logger

_events_log = get_logger("alkera.events")

# Keys we set ourselves — drop them from caller props so they can't collide.
_RESERVED_PROP_KEYS = frozenset({"event", "kind", "user_id", "org_id", "level", "timestamp"})


class EventName(StrEnum):
    """Append-only taxonomy of product events. Dot-namespaced by domain."""

    # --- Acquisition / activation ---
    org_created = "org.created"
    user_signed_up = "auth.signed_up"
    user_logged_in = "auth.logged_in"
    user_logged_out = "auth.logged_out"
    cli_token_minted = "auth.cli_token_minted"  # noqa: S105 — event name, not a credential
    email_verification_sent = "auth.email_verification_sent"
    email_verified = "auth.email_verified"
    password_reset_requested = "auth.password_reset_requested"  # noqa: S105 — event name
    password_reset_completed = "auth.password_reset_completed"  # noqa: S105 — event name
    oauth_login = "oauth.login"
    oauth_linked = "oauth.linked"

    # --- Collaboration / retention ---
    invitation_sent = "invitation.sent"
    invitation_accepted = "invitation.accepted"
    membership_added = "membership.added"
    membership_removed = "membership.removed"
    team_created = "team.created"

    # --- Product usage ---
    chat_started = "chat.started"
    chat_completed = "chat.completed"
    harness_started = "harness.started"
    harness_crashed = "harness.crashed"
    daemon_started = "daemon.started"

    # --- Billing ---
    credit_hold_placed = "billing.hold_placed"
    credit_settled = "billing.settled"
    credit_abandoned = "billing.abandoned"
    credit_granted = "billing.granted"

    # --- Comms / support ---
    email_sent = "email.sent"
    crash_report_submitted = "crash_report.submitted"
    client_error_reported = "client_error.reported"


def _coerce_id(value: str | UUID | None) -> str | None:
    return str(value) if value is not None else None


def emit_event(
    name: EventName | str,
    *,
    user_id: str | UUID | None = None,
    org_id: str | UUID | None = None,
    **props: Any,
) -> None:
    """Emit one analytics event. Safe to call from anywhere — never raises."""
    # Local import avoids a module-load cycle (redaction is a leaf, but importing
    # it here keeps `events` free of an import-time dependency ordering concern).
    from alkera_core.observability.redaction import scrub_mapping

    payload = {k: v for k, v in props.items() if k not in _RESERVED_PROP_KEYS}
    try:
        _events_log.info(
            str(name),
            kind="analytics",
            user_id=_coerce_id(user_id),
            org_id=_coerce_id(org_id),
            **scrub_mapping(payload),
        )
    except Exception:  # analytics must never break the calling path
        # NB: `event` is structlog's positional message arg — pass the failed
        # event name under a different key to avoid an argument collision.
        _events_log.warning("analytics.emit_failed", failed_event=str(name))
