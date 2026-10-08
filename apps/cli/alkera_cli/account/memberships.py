"""The orgs a signed-in person belongs to (``GET /api/v1/auth/memberships``).

The route lists the caller's own active memberships and which org the calling
token is bound to. It is read with one stored profile's token; a token is never
exchanged for another org's (each org gets its own device approval), so this
list says where the person *could* sign in, and the profiles say where they
*are* signed in.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from alkera_sdk import AlkeraClient

from alkera_cli.account.auth_file import AuthFileV2, Profile
from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id

MEMBERSHIPS_PATH = "/api/v1/auth/memberships"


class MembershipsError(Exception):
    """The memberships route could not be read (signed out, refused, offline,
    or an answer this client cannot read)."""


class SignInRejectedError(MembershipsError):
    """The server answered 401: the stored sign-in itself is no longer
    accepted, and only signing in again fixes it. Every other failure (offline,
    a server too old to have the route, a 5xx) says nothing about the sign-in."""


@dataclass(frozen=True, slots=True)
class Membership:
    org_team_id: str
    org_name: str
    role: str
    sso_required: bool = False
    last_active_at: str | None = None


@dataclass(frozen=True, slots=True)
class Memberships:
    active_org_team_id: str
    memberships: tuple[Membership, ...]


def _parse(body: object) -> Memberships:
    if not isinstance(body, dict):
        raise MembershipsError("The organizations answer wasn't an object.")
    rows = body.get("memberships")
    if not isinstance(rows, list):
        raise MembershipsError("The organizations answer had no list.")
    parsed: list[Membership] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("org_team_id"), str):
            continue
        last = row.get("last_active_at")
        parsed.append(
            Membership(
                org_team_id=row["org_team_id"],
                org_name=str(row.get("org_name") or ""),
                role=str(row.get("role") or ""),
                sso_required=row.get("sso_required") is True,
                last_active_at=last if isinstance(last, str) else None,
            )
        )
    active = body.get("active_org_team_id")
    return Memberships(
        active_org_team_id=active if isinstance(active, str) else "", memberships=tuple(parsed)
    )


def fetch_memberships(
    profile: Profile,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> Memberships:
    """The memberships as ``profile`` reads them. ``transport`` is the test seam.

    Read through the SDK client's transport (the route is newer than the
    generated client), with the profile's org asserted like every other call."""
    try:
        with AlkeraClient(
            base_url=profile.api_url,
            token=profile.token,
            org_id=profile.org_team_id or None,
            timeout=timeout,
            httpx_args={"transport": transport} if transport is not None else None,
        ) as api:
            resp = api.raw_client.get_httpx_client().get(MEMBERSHIPS_PATH)
    except httpx.HTTPError as exc:
        raise MembershipsError(f"Couldn't reach {profile.api_url}: {exc}") from exc
    if resp.status_code == 401:
        raise SignInRejectedError("The stored sign-in was refused. Run `alkera login`.")
    if resp.status_code in (403, 409):
        raise MembershipsError("The stored sign-in was refused for this organization.")
    if resp.status_code == 404:
        raise MembershipsError("This Alkera server can't list your organizations yet.")
    if resp.status_code >= 400:
        raise MembershipsError(f"The server answered {resp.status_code}.")
    try:
        return _parse(resp.json())
    except ValueError as exc:
        raise MembershipsError("The organizations answer wasn't JSON.") from exc


def find_membership(memberships: Memberships, org: str) -> Membership | None:
    """The membership ``org`` names: an org id, or a name compared without case."""
    wanted = org.strip()
    folded = wanted.casefold()
    try:
        wanted_id = canonical_org_id(wanted)
    except MalformedOrgIdError:
        wanted_id = None  # a name, compared below
    for row in memberships.memberships:
        if wanted_id is not None and row.org_team_id == wanted_id:
            return row
    named = [row for row in memberships.memberships if row.org_name.casefold() == folded]
    return named[0] if len(named) == 1 else None


def stored_profile_for(auth: AuthFileV2 | None, api_url: str, org_team_id: str) -> Profile | None:
    """A stored profile for ``org_team_id`` at ``api_url``, current first."""
    if auth is None:
        return None
    try:
        org_team_id = canonical_org_id(org_team_id)
    except MalformedOrgIdError:
        return None  # no stored sign-in is for a value that is not an org
    matches = [
        p
        for p in auth.profiles
        if p.org_team_id == org_team_id and p.api_url.rstrip("/") == api_url.rstrip("/")
    ]
    current = auth.current_profile
    matches.sort(key=lambda p: p is not current)
    return matches[0] if matches else None


__all__ = [
    "MEMBERSHIPS_PATH",
    "Membership",
    "Memberships",
    "MembershipsError",
    "SignInRejectedError",
    "fetch_memberships",
    "find_membership",
    "stored_profile_for",
]
