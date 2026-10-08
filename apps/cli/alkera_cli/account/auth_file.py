"""Persisted CLI sign-ins: `~/.alkera/auth.yml`.

The file holds one **profile** per (API, identity, org), each with the
org-bound token the device grant minted for it, plus which profile is
**current**. Identity beyond the key (display name, roles) is not persisted; it
is fetched from ``GET /auth/me`` when a command needs it. ``email`` and
``org_name`` ride along for display only.

Which profile an operation uses is decided by :func:`resolve_profile`, in this
order: the ``ALKERA_TOKEN`` env token, then ``--org`` / ``ALKERA_ORG``, then the
project's pin (``.alkera/cloud.json``), then ``current``. A project pinned to
one org refuses every cloud operation under a credential for another
(:class:`ProjectOrgMismatchError`), before any request.

Backwards compatibility runs both ways. A file an older CLI wrote (no
``schema_version``: ``{api_url, token, expires_at}``) is read as one profile.
Every write mirrors the current profile's ``api_url``, ``token`` and
``expires_at`` at the top level, so an older CLI or extension reading this file
keeps signing in as the current profile.

Every token read goes through this module, so moving tokens into an OS keyring
later is a change here and nowhere else.

The JWT is long-lived (90 days), so the file is replaced atomically and, on
POSIX, created 0600: a crash mid-write leaves the previous file in place, and no
moment exists where a token is readable by group or other. Windows has no POSIX
modes; the file there inherits the ACL of `~/.alkera/`. Read-modify-write
cycles hold a lock beside the file, so two daemons (two editor windows) writing
at once cannot lose each other's profile.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import UUID

import yaml
from alkera_core.atomic_io import write_text_atomic
from alkera_core.project.locking import FileLock, retrying_lock
from alkera_core.versioning import VersionedModel
from pydantic import BaseModel, ConfigDict, Field, field_validator

from alkera_cli.account.jwt_decode import jwt_claims, jwt_expires_at
from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id
from alkera_cli.host.config import get_settings
from alkera_cli.host.paths import AUTH_FILE_PATH, ensure_home

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory
    from alkera_core.project.cloud_binding import CloudBinding

#: The env var that hands a process a token directly, ahead of every stored
#: profile (an agent's own environment, a CI job).
TOKEN_ENV = "ALKERA_TOKEN"  # noqa: S105 — an env var name, not a secret
#: The env var that picks a stored profile by org for one process.
ORG_ENV = "ALKERA_ORG"
#: The header a client names the org it believes it acts in with. The server
#: answers 409 ``org_changed`` when it is not the credential's own org. Spelled
#: here rather than imported because the server's module pulls in the database
#: layer; a test pins the two spellings together.
ORG_HEADER = "X-Alkera-Org"


def org_headers(token: str, org_team_id: str | None) -> dict[str, str]:
    """The bearer plus the org assertion, when the org is known."""
    out = {"Authorization": f"Bearer {token}"}
    if org_team_id:
        out[ORG_HEADER] = org_team_id
    return out


class StoredAuth(BaseModel):
    """One sign-in as the pre-profile file shape held it: the token and the URL
    it was issued for. :func:`load_auth` still returns it for the one caller
    that wants the plain current sign-in; everything else takes a
    :class:`Profile`."""

    api_url: str
    token: str
    expires_at: datetime


def _canonical_user_id(value: str) -> str:
    """A user id in its one spelling: a UUID as the hyphenated lowercase string
    (tokens carry the bare hex form, the API the hyphenated one), anything
    else as given."""
    try:
        return str(UUID(value.strip()))
    except (ValueError, AttributeError):
        return value


def _stored_org_id(value: str) -> str:
    """The org a stored sign-in names, in its one spelling, or blank when it
    names none it can be trusted with. A blank org is the shape a token whose
    claims could not be read already has (filled from ``/auth/me``); a value
    that is not an org id reads the same way rather than keying the profile,
    its header and its pin match as if it were one."""
    if not value or not value.strip():
        return ""
    try:
        return canonical_org_id(value)
    except MalformedOrgIdError:
        return ""


def profile_key(api_url: str, user_id: str, org_team_id: str) -> str:
    """The stable key of a profile: one per (API, identity, org)."""
    return f"{api_url.rstrip('/')}|{_canonical_user_id(user_id)}|{_stored_org_id(org_team_id)}"


def _canonical_key(key: str) -> str:
    parts = key.split("|")
    if len(parts) != 3:
        return key
    return profile_key(parts[0], parts[1], parts[2])


class Profile(BaseModel):
    """One stored sign-in: a token bound to one org at one API.

    The profile shape is versioned by the file that holds it
    (:class:`AuthFileV2`); unknown keys from a newer writer ride along."""

    model_config = ConfigDict(extra="allow")

    key: str = ""
    api_url: str
    user_id: str = ""
    email: str = ""
    org_team_id: str = ""
    org_name: str = ""
    token: str
    expires_at: datetime

    @field_validator("user_id")
    @classmethod
    def _canonical_user(cls, value: str) -> str:
        return _canonical_user_id(value)

    @field_validator("org_team_id")
    @classmethod
    def _canonical_org(cls, value: str) -> str:
        return _stored_org_id(value)

    @field_validator("key")
    @classmethod
    def _canonical_stored_key(cls, value: str) -> str:
        return _canonical_key(value)

    def with_key(self) -> Profile:
        """This profile keyed by its own identity fields, in their one spelling."""
        user_id, org_team_id = _canonical_user_id(self.user_id), _stored_org_id(self.org_team_id)
        return self.model_copy(
            update={
                "user_id": user_id,
                "org_team_id": org_team_id,
                "key": profile_key(self.api_url, user_id, org_team_id),
            }
        )

    @property
    def org_label(self) -> str:
        """The org as a person reads it: its name, else its id."""
        return self.org_name or self.org_team_id

    def headers(self) -> dict[str, str]:
        """The headers a request made as this profile carries: the bearer, and
        the org the client believes it is acting in (``X-Alkera-Org``), which
        the server refuses with a 409 when it is not the token's own org."""
        return org_headers(self.token, self.org_team_id)

    def as_stored_auth(self) -> StoredAuth:
        return StoredAuth(api_url=self.api_url, token=self.token, expires_at=self.expires_at)


def profile_from_token(
    api_url: str,
    token: str,
    *,
    email: str = "",
    org_name: str = "",
    expires_at: datetime | None = None,
) -> Profile:
    """A profile keyed by the token's own claims (``sub``, ``org_team_id``),
    never by what the caller asked for. Claims that cannot be read leave the
    identity blank, to be filled from ``/auth/me``."""
    claims = jwt_claims(token) or {}
    user_id = claims.get("sub")
    org_team_id = claims.get("org_team_id")
    claim_email = claims.get("email")
    return Profile(
        api_url=api_url.rstrip("/"),
        user_id=user_id if isinstance(user_id, str) else "",
        org_team_id=org_team_id if isinstance(org_team_id, str) else "",
        email=email or (claim_email if isinstance(claim_email, str) else ""),
        org_name=org_name,
        token=token,
        expires_at=expires_at if expires_at is not None else jwt_expires_at(token),
    ).with_key()


def _v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap the pre-profile file (``{api_url, token, expires_at}``) into one
    current profile, keyed by the token's unverified claims. Pure."""
    api_url = data.get("api_url")
    token = data.get("token")
    out: dict[str, Any] = {"schema_version": "2.0.0", "current": None, "profiles": []}
    if not isinstance(api_url, str) or not isinstance(token, str) or not token:
        return out
    raw_expiry = data.get("expires_at")
    expires_at: Any = raw_expiry if raw_expiry is not None else jwt_expires_at(token)
    claims = jwt_claims(token) or {}
    user_id = claims.get("sub") if isinstance(claims.get("sub"), str) else ""
    org_team_id = claims.get("org_team_id") if isinstance(claims.get("org_team_id"), str) else ""
    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
    key = profile_key(api_url, str(user_id), str(org_team_id))
    out["profiles"] = [
        {
            "key": key,
            "api_url": api_url.rstrip("/"),
            "user_id": user_id,
            "email": email,
            "org_team_id": org_team_id,
            "org_name": "",
            "token": token,
            "expires_at": expires_at,
        }
    ]
    out["current"] = key
    return out


class AuthFileV2(VersionedModel):
    """`auth.yml`: the stored profiles and which one is current.

    ``api_url`` / ``token`` / ``expires_at`` at the top level are a mirror of
    the current profile, rewritten on every save, for older readers only. This
    model never reads them back: the profiles are the source of truth."""

    SCHEMA_VERSION: ClassVar[str] = "2.0.0"
    MIGRATIONS: ClassVar[dict[str, Any]] = {"1.0.0": _v1_to_v2}

    current: str | None = None
    profiles: list[Profile] = Field(default_factory=list)

    @field_validator("current")
    @classmethod
    def _canonical_current(cls, value: str | None) -> str | None:
        return _canonical_key(value) if value else value

    @classmethod
    def parse(cls, data: dict[str, Any]) -> AuthFileV2:
        """Validate a loaded document. A document without ``schema_version`` is
        the pre-profile shape, read as version 1."""
        if "schema_version" not in data:
            data = {**data, "schema_version": "1.0.0"}
        return cls.model_validate(data)

    def find(self, key: str) -> Profile | None:
        return next((p for p in self.profiles if p.key == key), None)

    @property
    def current_profile(self) -> Profile | None:
        return self.find(self.current) if self.current else None

    def to_document(self) -> dict[str, Any]:
        """The YAML document: profiles first, then the mirror of ``current``."""
        doc = self.model_dump(mode="json", exclude={"api_url", "token", "expires_at"})
        if not doc.get("metadata"):
            doc.pop("metadata", None)
        current = self.current_profile
        if current is not None:
            doc["api_url"] = current.api_url
            doc["token"] = current.token
            doc["expires_at"] = current.expires_at.isoformat()
        return doc


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------


def _lock_path() -> Path:
    """Beside the file, in a directory of its own: the lock's bookkeeping files
    hold a pid, never a token, and keeping them out of the home keeps every
    file there that a token could be read from at 0600."""
    return AUTH_FILE_PATH.parent / ".locks" / "auth.lock"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    ensure_home()
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with retrying_lock(FileLock(lock)):
        yield


def _read_document() -> dict[str, Any] | None:
    if not AUTH_FILE_PATH.exists():
        return None
    try:
        data: Any = yaml.safe_load(AUTH_FILE_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def load_profiles() -> AuthFileV2 | None:
    """The stored profiles, or None when the file is missing or unreadable."""
    data = _read_document()
    if data is None:
        return None
    try:
        return AuthFileV2.parse(data)
    except Exception:  # pydantic ValidationError + anything a hand edit leaves
        return None


def api_credentials() -> tuple[str, str] | None:
    """The API URL and bearer token the stored login sends, or ``None`` when
    there is no login with a token to send."""
    auth = load_auth()
    if auth is None or not auth.token:
        return None
    return auth.api_url, auth.token


def _write(auth: AuthFileV2) -> None:
    """Write ``auth`` atomically (0600), or remove the file when it holds no
    profile, so an older reader sees "signed out" rather than a half file."""
    if not auth.profiles:
        AUTH_FILE_PATH.unlink(missing_ok=True)
        return
    if auth.current_profile is None:
        auth.current = auth.profiles[0].key
    write_text_atomic(
        AUTH_FILE_PATH, yaml.safe_dump(auth.to_document(), sort_keys=False), mode=0o600
    )


def save_profile(profile: Profile, *, make_current: bool) -> Profile:
    """Store ``profile`` (replacing any with the same key) and return it as
    stored. ``make_current`` makes it the current profile; otherwise the first
    profile ever stored still becomes current."""
    stored = profile if profile.key else profile.with_key()
    with _locked():
        auth = load_profiles() or AuthFileV2()
        profiles = [p for p in auth.profiles if p.key != stored.key]
        profiles.append(stored)
        auth.profiles = profiles
        if make_current or auth.current_profile is None:
            auth.current = stored.key
        _write(auth)
    return stored


def update_profile_identity(
    key: str, *, user_id: str, org_team_id: str, email: str, org_name: str
) -> Profile | None:
    """Fill a profile's identity from ``/auth/me``, re-keying it when the
    identity was unknown. Returns the stored profile, or None when ``key`` is
    no longer stored."""
    with _locked():
        auth = load_profiles()
        if auth is None:
            return None
        existing = auth.find(key)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "user_id": user_id or existing.user_id,
                "org_team_id": org_team_id or existing.org_team_id,
                "email": email or existing.email,
                "org_name": org_name or existing.org_name,
            }
        ).with_key()
        if updated == existing:
            return existing
        was_current = auth.current == key
        auth.profiles = [p for p in auth.profiles if p.key not in {key, updated.key}]
        auth.profiles.append(updated)
        if was_current:
            auth.current = updated.key
        _write(auth)
    return updated


def remove_profile(key: str) -> Profile | None:
    """Remove one profile. When it was current, the next stored profile (if
    any) becomes current. Returns the removed profile."""
    with _locked():
        auth = load_profiles()
        if auth is None:
            return None
        removed = auth.find(key)
        if removed is None:
            return None
        auth.profiles = [p for p in auth.profiles if p.key != key]
        if auth.current == key:
            auth.current = auth.profiles[0].key if auth.profiles else None
        _write(auth)
    return removed


def set_current(key: str) -> Profile:
    """Make the stored profile ``key`` current. Raises ``KeyError`` when it is
    not stored."""
    with _locked():
        auth = load_profiles()
        target = auth.find(key) if auth is not None else None
        if auth is None or target is None:
            raise KeyError(key)
        auth.current = key
        _write(auth)
    return target


def delete_auth() -> bool:
    """Remove every stored profile. True when a file was deleted."""
    with _locked():
        if AUTH_FILE_PATH.exists():
            AUTH_FILE_PATH.unlink()
            return True
    return False


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

ProfileSource = Literal["token_env", "flag", "env", "pin", "current"]
"""Where the resolved profile came from: ``ALKERA_TOKEN``, ``--org``,
``ALKERA_ORG``, the project's pin, or the current profile."""


class ProfileResolutionError(Exception):
    """No profile may be used for this operation. The message is the one line
    every surface shows."""


class NoProfileForOrgError(ProfileResolutionError):
    """An org was asked for (``--org`` / ``ALKERA_ORG``) and no stored profile
    is for it."""

    def __init__(self, org: str) -> None:
        self.org = org
        super().__init__(f"No sign-in for {org}. Run `alkera login --org {org}`.")


class ProjectOrgMismatchError(ProfileResolutionError):
    """The project is pinned to another org than the resolved profile's."""

    def __init__(self, org_name: str) -> None:
        self.org_name = org_name
        super().__init__(f"This project belongs to {org_name}. Run `alkera org switch {org_name}`.")


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    profile: Profile
    source: ProfileSource


_invocation_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "alkera_invocation_org", default=None
)


def set_invocation_org(org: str | None) -> None:
    """Record the global ``--org`` for this invocation."""
    _invocation_org.set(org.strip() if org and org.strip() else None)


def _settings_api_url() -> str:
    return get_settings().alkera_api_url.rstrip("/")


def _env_token_profile(token: str) -> Profile:
    claims = jwt_claims(token) or {}
    expiry = claims.get("exp")
    expires_at = (
        datetime.fromtimestamp(int(expiry), tz=UTC)
        if isinstance(expiry, int | float)
        else datetime.now(UTC) + timedelta(days=1)
    )
    return profile_from_token(_settings_api_url(), token, expires_at=expires_at)


def matching_profiles(auth: AuthFileV2, org: str) -> list[Profile]:
    """Stored profiles whose org is ``org`` (an id, or a name compared without
    case), the current profile's API first."""
    wanted = org.strip()
    folded = wanted.casefold()
    try:
        wanted_id: str | None = canonical_org_id(wanted)
    except MalformedOrgIdError:
        wanted_id = None  # a name, compared below
    matches = [
        p
        for p in auth.profiles
        if (wanted_id is not None and p.org_team_id == wanted_id)
        or (p.org_name and p.org_name.casefold() == folded)
    ]
    current = auth.current_profile
    preferred_api = current.api_url if current is not None else None
    return sorted(matches, key=lambda p: (p.api_url != preferred_api, p is not current))


def resolve_profile_with_source(
    *, org: str | None = None, project: ProjectDirectory | None = None
) -> ResolvedProfile | None:
    """The profile an operation uses and where it came from, or None when
    nothing is signed in.

    Raises :class:`NoProfileForOrgError` when an org was asked for and none is
    stored, and :class:`ProjectOrgMismatchError` when ``project`` is pinned to
    another org than the profile that would be used."""
    pin = project.cloud_binding().read() if project is not None else None
    resolved = _select(org=org, pin=pin)
    if resolved is None:
        return None
    if pin is not None and not pin.matches(
        api_url=resolved.profile.api_url, org_team_id=resolved.profile.org_team_id
    ):
        raise ProjectOrgMismatchError(pin.org_name or pin.org_team_id or "another organization")
    return resolved


def _select(*, org: str | None, pin: CloudBinding | None) -> ResolvedProfile | None:
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return ResolvedProfile(_env_token_profile(env_token), "token_env")
    auth = load_profiles()
    flag = org or _invocation_org.get()
    source: ProfileSource = "flag" if flag else "env"
    asked = flag or os.environ.get(ORG_ENV, "").strip()
    if asked:
        found = matching_profiles(auth, asked) if auth is not None else []
        if not found:
            raise NoProfileForOrgError(asked)
        return ResolvedProfile(found[0], source)
    if auth is None:
        return None
    if pin is not None:
        pinned = next(
            (p for p in auth.profiles if pin.matches(api_url=p.api_url, org_team_id=p.org_team_id)),
            None,
        )
        if pinned is not None:
            return ResolvedProfile(pinned, "pin")
    current = auth.current_profile
    return ResolvedProfile(current, "current") if current is not None else None


def resolve_profile(
    *, org: str | None = None, project: ProjectDirectory | None = None
) -> Profile | None:
    """The profile an operation uses (see :func:`resolve_profile_with_source`)."""
    resolved = resolve_profile_with_source(org=org, project=project)
    return resolved.profile if resolved is not None else None


def profile_matching_token(token: str) -> Profile | None:
    """The stored profile for the same person and org as ``token`` (by its
    unverified claims), read fresh: how a long-lived process re-reads its own
    sign-in after a re-login without ever taking up another org's."""
    claims = jwt_claims(token) or {}
    user_id, org_team_id = claims.get("sub"), claims.get("org_team_id")
    auth = load_profiles()
    if auth is None:
        return None
    org_team_id = _stored_org_id(org_team_id) if isinstance(org_team_id, str) else ""
    if isinstance(user_id, str) and user_id and org_team_id:
        user_id = _canonical_user_id(user_id)
        return next(
            (p for p in auth.profiles if p.user_id == user_id and p.org_team_id == org_team_id),
            None,
        )
    return next((p for p in auth.profiles if p.token == token), None)


def has_sign_in() -> bool:
    """Whether any credential is available (an env token or a stored
    profile). Reads no token."""
    if os.environ.get(TOKEN_ENV, "").strip():
        return True
    auth = load_profiles()
    return auth is not None and bool(auth.profiles)


def load_profile(key: str) -> Profile | None:
    """The stored profile ``key``, read fresh, or None when it is gone."""
    auth = load_profiles()
    return auth.find(key) if auth is not None else None


def save_auth(auth: StoredAuth) -> Profile:
    """Store a plain sign-in as a profile keyed by its token's claims and make
    it current. Compatibility for writers that hold only a token."""
    return save_profile(
        profile_from_token(auth.api_url, auth.token, expires_at=auth.expires_at),
        make_current=True,
    )


def load_auth() -> StoredAuth | None:
    """The plain current sign-in, as the pre-profile file shape held it.

    Kept for compatibility only. A caller that acts for a chat, a project or a
    command resolves a :class:`Profile` instead."""
    auth = load_profiles()
    current = auth.current_profile if auth is not None else None
    return current.as_stored_auth() if current is not None else None


__all__ = [
    "ORG_ENV",
    "ORG_HEADER",
    "TOKEN_ENV",
    "AuthFileV2",
    "NoProfileForOrgError",
    "Profile",
    "ProfileResolutionError",
    "ProfileSource",
    "ProjectOrgMismatchError",
    "ResolvedProfile",
    "StoredAuth",
    "api_credentials",
    "delete_auth",
    "has_sign_in",
    "load_auth",
    "load_profile",
    "load_profiles",
    "matching_profiles",
    "org_headers",
    "profile_from_token",
    "profile_key",
    "profile_matching_token",
    "remove_profile",
    "resolve_profile",
    "resolve_profile_with_source",
    "save_auth",
    "save_profile",
    "set_current",
    "set_invocation_org",
    "update_profile_identity",
]
