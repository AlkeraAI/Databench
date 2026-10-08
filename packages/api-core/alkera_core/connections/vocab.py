"""The one vocabulary for a connection's lifecycle.

Every column that stores one of these, every wire field that carries one and
every badge a surface renders spells the value from here. A backend row, the
worker's verdict, the daemon's state store and the portal's pill therefore
cannot disagree about what a word means — the audit found nine vocabularies
for one question, and this module is the answer to that.

Three facts are stored (:class:`Outcome` of the last settled verification,
:class:`VerificationState` of the check in flight, :class:`CredentialState` of
the secret) and one :class:`Badge` is derived from them by
:func:`alkera_core.connections.badge.derive_badge`.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """What a settled verification found. ``unsupported`` means the connector
    cannot be checked from this vantage; ``infrastructure`` means Alkera could
    not run the check at all (no guard library, an unreadable ciphertext, a
    worker that never picked it up) — neither is the customer's fault."""

    ok = "ok"
    invalid_credential = "invalid_credential"
    permission = "permission"
    unreachable = "unreachable"
    timeout = "timeout"
    unsupported = "unsupported"
    infrastructure = "infrastructure"
    error = "error"


class VerificationState(StrEnum):
    """Where a verification record is."""

    queued = "queued"
    running = "running"
    settled = "settled"
    abandoned = "abandoned"


class CredentialState(StrEnum):
    """The state of one stored secret. The product writes ``present``,
    ``needs_reauth``, ``unreadable`` and ``revoked`` today; ``absent``,
    ``expiring``, ``expired`` and ``refreshing`` are reserved for expiry and
    refresh tracking and already derive correctly."""

    present = "present"
    needs_reauth = "needs_reauth"
    unreadable = "unreadable"
    revoked = "revoked"
    absent = "absent"
    expiring = "expiring"
    expired = "expired"
    refreshing = "refreshing"


class Reauth(StrEnum):
    """What fixes a credential that needs re-authentication."""

    reenter = "reenter"
    browser = "browser"
    external_cli = "external_cli"
    admin = "admin"


class Badge(StrEnum):
    """The one derived status a person sees."""

    connected = "connected"
    stale = "stale"
    verifying = "verifying"
    not_checked = "not_checked"
    needs_reauth = "needs_reauth"
    no_access = "no_access"
    unreachable = "unreachable"
    error = "error"
    incomplete = "incomplete"
    unsupported = "unsupported"
    disabled = "disabled"
    muted = "muted"


#: Credential states that mean a person must act before the secret works again.
REAUTH_CREDENTIAL_STATES: frozenset[CredentialState] = frozenset(
    {
        CredentialState.needs_reauth,
        CredentialState.unreadable,
        CredentialState.revoked,
        CredentialState.expired,
    }
)

#: Verification states that mean a check is out right now.
IN_FLIGHT_VERIFICATION_STATES: frozenset[VerificationState] = frozenset(
    {VerificationState.queued, VerificationState.running}
)

__all__ = [
    "IN_FLIGHT_VERIFICATION_STATES",
    "REAUTH_CREDENTIAL_STATES",
    "Badge",
    "CredentialState",
    "Outcome",
    "Reauth",
    "VerificationState",
]
