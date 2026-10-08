"""The signed-in user as the gate sees them.

A principal carries the teams the user may act for and the org's escalation
opt-in. A session resolves one from the backend under its own bearer token,
caches it briefly, and installs it process-wide. Ownership checks ask it
which owning teams are foreign.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alkera_cli.account.auth_file import ProfileResolutionError, org_headers
from alkera_cli.account.binding import ProfileBinding, profile_for_project
from alkera_cli.contracts.visibility import normalize_scope, parse_scope

logger = logging.getLogger(__name__)

#: sign-in -> (monotonic stamp, resolved principal or a failed read). The key is
#: the credentials rather than the host, because a re-login as a different user
#: keeps the host and would otherwise be served the previous user's teams. A short
#: TTL keeps repeated session opens off the backend while still picking up a
#: membership or opt-in change promptly. Failures cache too, so an unreachable
#: backend costs one timeout per window rather than one per chat.
_ACTOR_CACHE: dict[str, tuple[float, ActingPrincipal | None]] = {}
_ACTOR_TTL_SECONDS = 60.0
_ACTOR_HTTP_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True, slots=True)
class ActingPrincipal:
    """The signed-in user as the gate sees them."""

    teams: frozenset[str] = frozenset()
    """The scope tokens the user may act for, folded with ``normalize_scope``.
    Empty means every confirmed owner is foreign to them. These are the scopes
    read's ``member_scopes``, the user's org plus every team on their ancestor
    chains, resolved from membership alone so the org's sync switch never empties
    them."""
    escalation_enabled: bool = False
    """The org's opt-in to cross-team escalation, from the org settings.
    Off keeps ownership display-only."""


def clear_actor_cache() -> None:
    """Drop the cached principals (tests + an explicit refresh)."""
    _ACTOR_CACHE.clear()


#: Where the principal's credentials come from. ``None`` reads the persisted
#: sign-in, which is the only production answer.
_auth_source: Callable[[], Any] | None = None


def set_auth_source(read: Callable[[], Any] | None) -> None:
    """Repoint the credentials this module resolves a principal from, or restore
    the persisted sign-in with ``None``. A test isolates the gate from the
    machine's real login here, so nothing on this path reaches a live backend
    under a real token. The cached principals go too, since they belong to the
    source that produced them."""
    global _auth_source
    _auth_source = read
    clear_actor_cache()


def _read_auth(alkera_dir: Any = None, credential: Any = None) -> Any:
    """The sign-in a principal is resolved under: a chat's bound profile when
    ``credential`` is one, else the profile of the project at ``alkera_dir``
    (its pin, then the current profile). A project pinned to another org, or a
    bound profile that was logged out, reads as no sign-in."""
    if _auth_source is not None:
        return _auth_source()

    try:
        if isinstance(credential, ProfileBinding):
            return credential.profile()
        return profile_for_project(_project_at(alkera_dir))
    except ProfileResolutionError:
        return None


def _project_at(alkera_dir: Any) -> Any:
    if alkera_dir is None:
        return None
    from pathlib import Path

    from alkera_core.project import ProjectDirectory

    try:
        return ProjectDirectory(Path(alkera_dir), create_if_missing=False)
    except FileNotFoundError:
        return None


def _sign_in_key(auth: Any) -> str:
    """The identity of one sign-in, and the empty string for none. The token is
    hashed rather than held, since this key outlives the read."""
    if auth is None:
        return ""
    digest = hashlib.sha256(str(auth.token).encode()).hexdigest()
    return f"{auth.api_url}#{digest}"


# The live principals of this process, one per sign-in they were resolved under.
# The daemon serves one machine owner (``daemon.protocol.authorize_read``), who may
# hold sign-ins to several orgs: a chat's principal is the one of the sign-in it
# acts as, so an org's teams never decide escalation for another org's chat. The
# session build fills a slot after resolving the sign-in, the same way
# ``audit.set_decision_observer`` installs the org reporter. Keying by the sign-in
# is what keeps a signed-out or re-logged-in process from acting on the previous
# user's teams. A principal installed with no sign-in to bind to stands alone.
_actors: dict[str, ActingPrincipal] = {}
_unbound_actor: ActingPrincipal | None = None


def set_acting_principal(
    actor: ActingPrincipal | None, *, alkera_dir: Any = None, credential: Any = None
) -> None:
    """Install the acting principal for the sign-in in force for this project or
    chat now, or clear every installed principal with ``None``."""
    global _unbound_actor
    if actor is None:
        _actors.clear()
        _unbound_actor = None
        return
    key = _sign_in_key(_read_auth(alkera_dir, credential))
    if key:
        _actors[key] = actor
    else:
        _unbound_actor = actor


def current_acting_principal(
    *, alkera_dir: Any = None, credential: Any = None
) -> ActingPrincipal | None:
    """The principal installed for the sign-in in force for this project or chat,
    ``None`` when no session resolved one under it."""
    key = _sign_in_key(_read_auth(alkera_dir, credential))
    if key and key in _actors:
        return _actors[key]
    return _unbound_actor


async def _fetch_actor(api_url: str, token: str, org_id: str = "") -> ActingPrincipal:
    import httpx

    base = api_url.rstrip("/")
    headers = org_headers(token, org_id)
    async with httpx.AsyncClient(timeout=_ACTOR_HTTP_TIMEOUT_SECONDS) as client:
        scopes = await client.get(f"{base}/api/v1/kb/scopes", headers=headers)
        scopes.raise_for_status()
        settings = await client.get(f"{base}/api/v1/org/settings", headers=headers)
        settings.raise_for_status()

    tokens = [str(t) for t in scopes.json().get("member_scopes") or []]
    return ActingPrincipal(
        teams=frozenset(normalize_scope(t) for t in tokens if parse_scope(t).shared),
        escalation_enabled=bool(settings.json().get("ownership_escalation_enabled", False)),
    )


async def resolve_acting_principal(
    *,
    alkera_dir: Any = None,
    credential: Any = None,
    ttl_seconds: float = _ACTOR_TTL_SECONDS,
) -> ActingPrincipal | None:
    """The acting user's teams and their org's escalation opt-in, cached briefly.
    Both come from the backend under the session's own bearer token (the chat's
    bound profile, else the project's), never from a request, so no caller can
    widen its own membership. ``None`` when signed out or unreadable on a cold
    cache; the gate then keeps cross-team escalation silent instead of guessing
    at membership. A warm-cache fetch failure serves the last-known principal."""
    auth = _read_auth(alkera_dir, credential)
    if auth is None:
        return None

    key = _sign_in_key(auth)
    now = time.monotonic()
    cached = _ACTOR_CACHE.get(key)
    if cached is not None and now - cached[0] < ttl_seconds:
        return cached[1]

    value: ActingPrincipal | None
    try:
        value = await _fetch_actor(auth.api_url, auth.token, getattr(auth, "org_team_id", ""))
    except Exception:
        logger.warning("gate.actor.fetch_failed", exc_info=True)
        value = cached[1] if cached is not None else None
    _ACTOR_CACHE[key] = (now, value)
    return value


def _foreign_owners(teams: list[str], actor: ActingPrincipal | None) -> list[str]:
    """The owning teams the acting user is not on. Empty unless the org opted into
    cross-team escalation: an unowned estate, a signed-out session, and an
    org that keeps ownership display-only all stay silent."""
    if actor is None or not actor.escalation_enabled:
        return []
    return [t for t in teams if normalize_scope(t) not in actor.teams]


__all__ = [
    "ActingPrincipal",
    "clear_actor_cache",
    "current_acting_principal",
    "resolve_acting_principal",
    "set_acting_principal",
    "set_auth_source",
]
