"""The person's organizations on this machine: list them, switch between the
ones signed in to, and sign out of them.

The library half of ``alkera org list|switch`` and ``alkera logout``, and of the
daemon's ``auth.listOrgs``, ``auth.switchOrg`` and ``auth.logout``: the CLI and
the daemon are façades over these and decide nothing themselves, so the two
surfaces cannot drift apart on which stored sign-in an org names.

Which stored sign-in an org names, for a switch and for a sign-out by org:

* only sign-ins at the API the machine is acting against count (the current
  sign-in's, else the first stored). The same org can be stored at two
  deployments with two tokens, and switching orgs must never quietly move the
  person to another deployment;
* an org id (any spelling) names the sign-in for that org;
* anything else is a name, compared without case, and names a sign-in only
  when exactly one at that API carries it. Names are display text two orgs
  may share, so an ambiguous one is refused and the person is asked for the id
  rather than switched into whichever came first.
"""

from __future__ import annotations

from dataclasses import dataclass

from alkera_cli.account import auth_file, memberships, session
from alkera_cli.account.auth_file import AuthFileV2, Profile, ProfileResolutionError
from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id


class AmbiguousOrgNameError(ProfileResolutionError):
    """More than one stored sign-in at the acting API carries the org name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"More than one organization you are signed in to is named {name}. "
            "Use its id (`alkera org list`)."
        )


class NoStoredSignInError(ProfileResolutionError):
    """No stored sign-in at the acting API is for the org asked for."""

    def __init__(self, org: str) -> None:
        self.org = org
        super().__init__(f"No sign-in for {org}.")


def acting_api_url(auth: AuthFileV2) -> str | None:
    """The API this machine acts against: the current sign-in's, else the
    first stored one's; ``None`` with nothing stored."""
    current = auth.current_profile
    if current is not None:
        return current.api_url.rstrip("/")
    return auth.profiles[0].api_url.rstrip("/") if auth.profiles else None


def stored_sign_in_for(auth: AuthFileV2, org: str) -> Profile | None:
    """The stored sign-in ``org`` names at the acting API (see the module
    docstring), or ``None`` when none does. Raises
    :class:`AmbiguousOrgNameError` for a name more than one carries."""
    api_url = acting_api_url(auth)
    if api_url is None:
        return None
    at_api = [p for p in auth.profiles if p.api_url.rstrip("/") == api_url]
    wanted = org.strip()
    try:
        org_id = canonical_org_id(wanted)
    except MalformedOrgIdError:
        folded = wanted.casefold()
        named = [p for p in at_api if p.org_name and p.org_name.casefold() == folded]
        if len(named) > 1:
            raise AmbiguousOrgNameError(wanted) from None
        return named[0] if named else None
    return next((p for p in at_api if p.org_team_id == org_id), None)


def switch_org(org: str) -> Profile | None:
    """Make the stored sign-in ``org`` names current and return it; ``None``
    when none is stored (the caller then signs in to it). Raises
    :class:`AmbiguousOrgNameError`."""
    auth = auth_file.load_profiles()
    if auth is None:
        return None
    target = stored_sign_in_for(auth, org)
    if target is None:
        return None
    return auth_file.set_current(target.key)


@dataclass(frozen=True, slots=True)
class ListedOrg:
    """One org the person belongs to, as a picker lists it."""

    org_team_id: str
    org_name: str
    role: str
    sso_required: bool
    stored: bool
    """This machine holds a sign-in for it at the reader's API."""
    current: bool
    """It is the org the reading sign-in acts in."""


def list_orgs(reader: Profile) -> tuple[ListedOrg, ...]:
    """The orgs ``reader``'s person belongs to, read as ``reader``. Raises
    :class:`~alkera_cli.account.memberships.MembershipsError` when the API
    cannot answer."""
    auth = auth_file.load_profiles()
    listed = memberships.fetch_memberships(reader)
    return tuple(
        ListedOrg(
            org_team_id=row.org_team_id,
            org_name=row.org_name,
            role=row.role,
            sso_required=row.sso_required,
            stored=memberships.stored_profile_for(auth, reader.api_url, row.org_team_id)
            is not None,
            current=row.org_team_id == reader.org_team_id,
        )
        for row in listed.memberships
    )


@dataclass(frozen=True, slots=True)
class SignOut:
    """What a sign-out did."""

    removed: tuple[Profile, ...]
    revoke_unreachable: bool
    """At least one token could not be revoked server-side (it was forgotten
    here regardless)."""
    now_current: Profile | None
    """The sign-in current afterwards."""
    current_changed: bool
    """Another stored sign-in became current."""


def sign_out(*, org: str | None = None, every: bool = False) -> SignOut:
    """Sign out of the current org (the default), of the org ``org`` names, or
    of ``every`` stored one. Each token is revoked best effort before it is
    forgotten, so an unreachable server never keeps a sign-in on disk. Raises
    :class:`NoStoredSignInError` for an ``org`` with no stored sign-in and
    :class:`AmbiguousOrgNameError` for a name more than one carries."""
    auth = auth_file.load_profiles()
    if auth is None or not auth.profiles:
        if org is not None and not every:
            raise NoStoredSignInError(org)
        return SignOut(
            removed=(), revoke_unreachable=False, now_current=None, current_changed=False
        )
    if every:
        targets = list(auth.profiles)
    elif org is not None:
        found = stored_sign_in_for(auth, org)
        if found is None:
            raise NoStoredSignInError(org)
        targets = [found]
    else:
        current = auth.current_profile
        targets = [current] if current is not None else []
    unreachable = False
    for profile in targets:
        if not session.revoke_session(
            profile.api_url, profile.token, org_id=profile.org_team_id or None
        ):
            unreachable = True
        auth_file.remove_profile(profile.key)
    remaining = auth_file.load_profiles()
    now_current = remaining.current_profile if remaining is not None else None
    return SignOut(
        removed=tuple(targets),
        revoke_unreachable=unreachable,
        now_current=now_current,
        current_changed=now_current is not None and now_current.key != auth.current,
    )


__all__ = [
    "AmbiguousOrgNameError",
    "ListedOrg",
    "NoStoredSignInError",
    "SignOut",
    "acting_api_url",
    "list_orgs",
    "sign_out",
    "stored_sign_in_for",
    "switch_org",
]
