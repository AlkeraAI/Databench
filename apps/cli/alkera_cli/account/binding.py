"""The sign-in a chat, a project or a command acts as.

A chat binds its profile once, when it opens (:class:`ProfileBinding`), and
keeps the profile **key** for its life: a switch of the current profile in
another terminal or editor window changes what new chats open as, never a
running one. Each use re-reads the bound profile by key, so a re-login of the
same profile picks up its new token, and a logout of it fails the chat closed
(:class:`BoundProfileGoneError`) instead of falling through to whatever profile
is current now.

A project that syncs to the cloud is pinned to the org of its first sync
(:func:`profile_for_sync`). Every later cloud operation for the project goes
through :func:`profile_for_project`, which refuses a credential for another org
before any request is made.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from alkera_core.project.cloud_binding import CloudBinding

from alkera_cli.account.auth_file import (
    Profile,
    ProfileResolutionError,
    ProfileSource,
    ProjectOrgMismatchError,
    load_profile,
    resolve_profile,
    resolve_profile_with_source,
)

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory


class BoundProfileGoneError(ProfileResolutionError):
    """The profile a chat opened with is no longer stored (it was logged out).
    The chat stops rather than act as another profile."""

    def __init__(self, label: str) -> None:
        super().__init__(
            f"The sign-in this chat opened with ({label}) is gone. Run `alkera login`."
        )


@runtime_checkable
class ChatCredential(Protocol):
    """What a chat presents to the gateway: a token read at each use."""

    def token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class FixedCredential:
    """A credential minted for one chat (a cloud chat's own gateway token).

    ``share_scope`` is the chat's own door to where a "shared" note lands, for
    the person the chat acts for. A box holds no profile of that person, so
    without it the knowledge tools read the chat as signed out."""

    value: str
    share_scope: Callable[[], Awaitable[str | None]] | None = field(default=None, compare=False)

    def token(self) -> str:
        return self.value


class ProfileBinding:
    """A chat's sign-in: the profile it resolved when it opened."""

    def __init__(self, profile: Profile, source: ProfileSource) -> None:
        self._profile = profile
        self._source = source

    @classmethod
    def bind(
        cls, *, project: ProjectDirectory | None = None, org: str | None = None
    ) -> ProfileBinding | None:
        """Resolve the profile a chat opening now uses, or None when nothing is
        signed in. Raises :class:`ProfileResolutionError` when the project's pin
        or an asked-for org rules every stored profile out."""
        resolved = resolve_profile_with_source(org=org, project=project)
        return cls(resolved.profile, resolved.source) if resolved is not None else None

    @property
    def key(self) -> str:
        return self._profile.key

    @property
    def source(self) -> ProfileSource:
        return self._source

    @property
    def org_team_id(self) -> str:
        return self._profile.org_team_id

    @property
    def api_url(self) -> str:
        return self._profile.api_url

    def profile(self) -> Profile:
        """The bound profile as stored now. A token handed in through the
        environment is not stored, so it is its own binding."""
        if self._source == "token_env":
            return self._profile
        current = load_profile(self._profile.key)
        if current is None:
            raise BoundProfileGoneError(self._profile.org_label or self._profile.email)
        return current

    def token(self) -> str:
        return self.profile().token


def profile_for_project(
    project: ProjectDirectory | None, *, org: str | None = None
) -> Profile | None:
    """The profile a cloud operation for ``project`` uses, or None when nothing
    is signed in. Raises :class:`ProfileResolutionError` when the project is
    pinned to another org (or an asked-for org is not stored)."""
    return resolve_profile(org=org, project=project)


def pin_project(project: ProjectDirectory, profile: Profile) -> None:
    """Pin ``project`` to ``profile``'s org unless it is already pinned. A
    profile whose org is unknown pins nothing: the pin is only ever written
    from a credential the server named."""
    store = project.cloud_binding()
    if store.read() is not None or not profile.org_team_id:
        return
    store.write(
        CloudBinding(
            api_url=profile.api_url, org_team_id=profile.org_team_id, org_name=profile.org_name
        )
    )


def profile_for_sync(project: ProjectDirectory | None, *, org: str | None = None) -> Profile | None:
    """:func:`profile_for_project`, pinning the project to the profile's org
    when this is its first sync. Every site that sends a project's data to the
    cloud for the first time resolves its credential through here."""
    profile = profile_for_project(project, org=org)
    if profile is not None and project is not None:
        pin_project(project, profile)
    return profile


def acting_profile(
    project: ProjectDirectory | None,
    *,
    credential: object | None = None,
    sync: bool = False,
) -> Profile | None:
    """The profile a cloud call for ``project`` acts as.

    Called for a chat (``credential`` is its :class:`ProfileBinding`), that is
    the chat's bound profile, held to the project's pin like any other; a chat
    never borrows the current profile. Otherwise the project's own resolution.
    ``sync`` pins an unpinned project to the profile's org (a first sync).
    Raises :class:`ProfileResolutionError` on a pin mismatch or a logged-out
    bound profile."""
    if not isinstance(credential, ProfileBinding):
        return profile_for_sync(project) if sync else profile_for_project(project)
    profile = credential.profile()
    if project is not None:
        pin = project.cloud_binding().read()
        if pin is None:
            if sync:
                pin_project(project, profile)
        elif not pin.matches(api_url=profile.api_url, org_team_id=profile.org_team_id):
            raise ProjectOrgMismatchError(pin.org_name or pin.org_team_id)
    return profile


#: Which profile each live chat session acts as (session id -> profile key), for
#: the process-wide consumers that see only a session id (the org audit
#: reporter): an event a chat caused is delivered as that chat's profile.
_session_profiles: dict[str, str] = {}


def bind_session(session_id: str, credential: object | None) -> None:
    """Record the profile a chat session acts as. A credential that is not a
    stored profile (a cloud chat's own token) records nothing."""
    if session_id and isinstance(credential, ProfileBinding) and credential.key:
        _session_profiles[session_id] = credential.key


def session_profile_key(session_id: str) -> str | None:
    """The profile key a chat session acts as, or None when no chat claims it."""
    return _session_profiles.get(session_id) if session_id else None


def resolve_profile_or_none(*, project: ProjectDirectory | None = None) -> Profile | None:
    """:func:`profile_for_project`, reading a refusal as no profile. For the
    process-wide consumers that must never raise."""
    try:
        return profile_for_project(project)
    except ProfileResolutionError:
        return None


def unpin_project(project: ProjectDirectory) -> bool:
    """Remove the project's pin (a deliberate move to another org)."""
    return project.cloud_binding().clear()


__all__ = [
    "BoundProfileGoneError",
    "ChatCredential",
    "FixedCredential",
    "ProfileBinding",
    "acting_profile",
    "bind_session",
    "pin_project",
    "profile_for_project",
    "profile_for_sync",
    "resolve_profile_or_none",
    "session_profile_key",
    "unpin_project",
]
