"""Derive the one status a person sees from the three facts a connection stores.

The precedence is the whole contract and it is pinned row by row by
a shared vector file, which the
web's presentation test reads too. Top wins:

    disabled → muted → client build blocker → credential needs a person →
    a check is in flight → the last settled outcome → freshness → not checked

The backend derives it for a team row, the daemon for a local row and for a
member's view of a team row; the web never derives, it presents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from alkera_core.connections.vocab import (
    IN_FLIGHT_VERIFICATION_STATES,
    REAUTH_CREDENTIAL_STATES,
    Badge,
    CredentialState,
    Outcome,
    VerificationState,
)

#: A settled ``ok`` older than this reads as ``stale``: still connected, in a
#: quieter tone. Both still show when the connection was last verified — the
#: horizon changes how loudly a surface says it, not what it says.
FRESHNESS_HORIZON = timedelta(hours=24)

Blocker = Literal["", "unsupported", "incomplete"]


@dataclass(frozen=True)
class StatusInputs:
    """The facts :func:`derive_badge` reads. Everything is optional but
    ``enabled`` so a caller with only a verdict can still derive."""

    enabled: bool
    muted: bool = False
    #: A client-side build blocker: this machine cannot assemble the connection
    #: (``incomplete`` — the member owes fields, ``unsupported`` — needs a
    #: newer Alkera). Never set by the server.
    blocker: Blocker = ""
    credential_state: CredentialState = CredentialState.present
    verification_state: VerificationState | None = None
    last_outcome: Outcome | None = None
    #: When the last verification of ANY outcome settled.
    last_verified_at: datetime | None = None


def derive_badge(
    inputs: StatusInputs,
    *,
    now: datetime,
    horizon: timedelta = FRESHNESS_HORIZON,
) -> Badge:
    """The badge for ``inputs`` at ``now``. Pure; the fixture is the spec."""
    if not inputs.enabled:
        return Badge.disabled
    if inputs.muted:
        return Badge.muted
    if inputs.blocker == "unsupported":
        return Badge.unsupported
    if inputs.blocker == "incomplete":
        return Badge.incomplete
    if inputs.credential_state in REAUTH_CREDENTIAL_STATES:
        return Badge.needs_reauth
    if inputs.verification_state in IN_FLIGHT_VERIFICATION_STATES:
        return Badge.verifying
    match inputs.last_outcome:
        case Outcome.ok:
            if inputs.last_verified_at is not None and now - inputs.last_verified_at > horizon:
                return Badge.stale
            return Badge.connected
        case Outcome.invalid_credential:
            return Badge.needs_reauth
        case Outcome.permission:
            return Badge.no_access
        case Outcome.unreachable | Outcome.timeout:
            return Badge.unreachable
        case Outcome.error | Outcome.infrastructure:
            return Badge.error
        case _:
            return Badge.not_checked


__all__ = ["FRESHNESS_HORIZON", "Blocker", "StatusInputs", "derive_badge"]
