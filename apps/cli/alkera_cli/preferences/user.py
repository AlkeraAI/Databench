"""Persisted user preferences — `~/.alkera/preferences.yml`.

Mirrors `account/auth_file.py`, but the file is edited by BOTH the CLI and the
daemon (on behalf of the VS Code extension), so writes are concurrency
-safe by construction:

- `load_preferences()` is a lock-free best-effort read — `write_text_atomic`
  swaps the file via rename, so a reader sees the old or new file whole,
  never a torn one. Missing / corrupt files degrade to defaults.
- `save_preferences()` / `update_preferences()` hold a `FileLock`
  (`~/.alkera/.preferences.lock`) for the whole read-modify-write so two
  writers can't lose each other's changes. `Preferences` is a
  `VersionedModel` (`extra="allow"`), so a writer also preserves fields a
  newer peer added but this version doesn't know about.

The schema lives in `alkera_core.schemas.preferences` so the daemon's
`preferences.*` JSON-RPC methods can share it.

The catalog-scoped keys (`ORG_SCOPED_KEYS`: the default chat model, its
effort, the per-model effort memory) are kept per org, because a model id one
org's catalog offers may not exist in another's. Every read and write takes
the org the caller acts in (`org_id`, the sign-in's org) and sees ONE
`Preferences` document: the shared keys plus that org's copy of the scoped
ones. With no org (nothing signed in) the file's top-level copy is read and
written, as before the file was kept per org. A file that has never been kept
per org has its top-level values moved into the first org that reads it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import yaml
from alkera_core.project import FileLock, LockHeldError, write_text_atomic
from alkera_core.schemas.preferences import ORG_SCOPED_KEYS, DesktopPreferences, Preferences
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.account.binding import ProfileBinding, profile_for_project
from alkera_cli.account.org_id import canonical_org_id
from alkera_cli.host import paths
from alkera_cli.host.backoff import retry_until

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

# Housekeeping fields on every VersionedModel — not user-settable and not
# shown by `/preferences`.
_HOUSEKEEPING = frozenset({"schema_version", "metadata"})

# Structured fields edited through the settings modal (a registry / picker),
# not the flat `/prefs set <key> <value>` command — `_coerce` can only turn a
# CLI string into a scalar, so listing these as settable keys would mislead.
_STRUCTURED = frozenset({"tool_card_disclosure", "model_efforts"})

# Lifecycle markers written by the onboarding flows themselves (finish/skip/
# replay), not user-pokeable preferences — hidden from `/preferences` and
# `set` so the flat command can't half-reset the flow.
_LIFECYCLE = frozenset({"onboarded_cli", "onboarded_extension"})

_TRUE = frozenset({"true", "t", "yes", "y", "on", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "off", "0"})

_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_FIRST_POLL_SECONDS = 0.02
_LOCK_MAX_POLL_SECONDS = 0.2


def settable_fields() -> dict[str, FieldInfo]:
    """The scalar preference fields the flat `set` command can edit (drops
    VersionedModel housekeeping and structured fields).

    Drives `/preferences` display, `/help`, and `set` key-validation. The
    structured fields (the tool-card registry, model efforts) are edited via
    the settings modal, not a `<key> <value>` string.
    """
    skip = _HOUSEKEEPING | _STRUCTURED | _LIFECYCLE
    return {name: field for name, field in Preferences.model_fields.items() if name not in skip}


def preferences_org(project: ProjectDirectory | None) -> str | None:
    """The org whose copy of the scoped preferences a caller working in
    ``project`` reads: the org of the sign-in that project acts as (its pinned
    profile, else the current one). ``None`` when nothing is signed in, or the
    project's pin rules every stored sign-in out."""
    try:
        profile = profile_for_project(project)
    except ProfileResolutionError:
        return None
    if profile is None or not profile.org_team_id:
        return None
    return profile.org_team_id


def credential_org(credential: object) -> str | None:
    """The org a chat's credential acts in, for the scoped preferences: its
    bound profile's org. A credential that names no org (a token minted for one
    cloud chat, none at all) reads the file's top-level copy."""
    if isinstance(credential, ProfileBinding) and credential.org_team_id:
        return credential.org_team_id
    return None


def load_preferences_as(credential: object) -> Preferences:
    """:func:`load_preferences` as the org ``credential`` acts in."""
    return load_preferences(credential_org(credential))


def set_preference_as(credential: object, key: str, raw_value: str) -> Preferences:
    """:func:`set_preference` as the org ``credential`` acts in."""
    return set_preference(key, raw_value, org_id=credential_org(credential))


def load_preferences(org_id: str | None = None) -> Preferences:
    """Return the persisted preferences as ``org_id`` reads them, or defaults
    if the file is missing / unreadable / corrupt. Never raises, never returns
    None.

    The one write a read makes is the move of a never-per-org file's top-level
    scoped values into ``org_id``: it has to land before another org reads, or
    that org would be handed them too. It is best effort under the file lock;
    a writer that takes the lock first does the same move on its own write."""
    stored = _read_file()
    if _needs_move(stored, org_id):
        try:
            with _PreferencesLock():
                stored = _moved(_read_file(), org_id)
                _write_unlocked(stored)
        except (LockHeldError, OSError):
            stored = _moved(stored, org_id)
    return _view(stored, org_id)


def save_preferences(prefs: Preferences, *, org_id: str | None = None) -> None:
    """Write the whole preferences object atomically, under the file lock."""
    update_preferences(lambda _current: prefs, org_id=org_id)


def update_preferences(
    mutate: Callable[[Preferences], Preferences], *, org_id: str | None = None
) -> Preferences:
    """Lock-guarded read-modify-write. `mutate` receives the current
    preferences as ``org_id`` reads them and returns the new object; the lock
    is held across the read AND the write so a concurrent writer can't clobber
    the result. The scoped keys of the result are stored as ``org_id``'s; every
    other org's copy is left as it was."""
    paths.ensure_home()
    with _PreferencesLock():
        stored = _moved(_read_file(), org_id)
        updated = mutate(_view(stored, org_id))
        _write_unlocked(_folded(stored, updated, org_id))
        return updated


def set_preference(key: str, raw_value: str, *, org_id: str | None = None) -> Preferences:
    """Coerce `raw_value` to `key`'s field type and persist it.

    Raises `ValueError` (with a user-friendly message) on an unknown key
    or a value that won't coerce. The key/value parse is purposely
    distinct from a *usage* error — the caller already knows the args
    arrived in the right shape.
    """
    fields = settable_fields()
    if key not in fields:
        valid = ", ".join(sorted(fields)) or "(none)"
        raise ValueError(f"unknown preference '{key}' (valid: {valid})")
    coerced = _coerce(fields[key], key, raw_value)

    def mutate(current: Preferences) -> Preferences:
        data = current.model_dump()
        data[key] = coerced
        try:
            return Preferences.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"invalid value for '{key}': {raw_value!r}") from exc

    return update_preferences(mutate, org_id=org_id)


# --- internals -------------------------------------------------------------


def _coerce(field: FieldInfo, key: str, raw: str) -> Any:
    """Coerce a CLI string into the field's Python type. Bools get a
    friendly true/false vocabulary; everything else rides through
    Pydantic validation in `set_preference`."""
    if field.annotation is bool:
        token = raw.strip().lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
        raise ValueError(f"'{key}' expects true/false, got {raw!r}")
    return raw


def _org_key(org_id: str) -> str:
    """The key an org's scoped copy is stored under. Refuses a value that is
    not an org id (``MalformedOrgIdError``) rather than filing preferences
    under it."""
    return canonical_org_id(org_id)


def _read_file() -> DesktopPreferences:
    """The file as stored, or the default document when it is missing,
    unreadable or corrupt."""
    path = paths.PREFERENCES_FILE_PATH
    if not path.exists():
        return DesktopPreferences()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return DesktopPreferences()
    if not isinstance(data, dict):
        return DesktopPreferences()
    try:
        return DesktopPreferences.model_validate(data)
    except Exception:  # ValidationError + anything weird
        return DesktopPreferences()


def _needs_move(stored: DesktopPreferences, org_id: str | None) -> bool:
    """Whether a file that has never been kept per org is being read by an org
    for the first time. A missing file has nothing to move."""
    return bool(org_id) and not stored.orgs and paths.PREFERENCES_FILE_PATH.exists()


def _moved(stored: DesktopPreferences, org_id: str | None) -> DesktopPreferences:
    """``stored`` with its top-level scoped values copied into ``org_id``'s
    entry, when no org has an entry yet. The top-level copy stays for an older
    CLI; nothing that knows its org reads it again."""
    if not org_id or stored.orgs:
        return stored
    flat = stored.model_dump(mode="json", include=set(ORG_SCOPED_KEYS))
    return stored.model_copy(update={"orgs": {_org_key(org_id): flat}})


def _view(stored: DesktopPreferences, org_id: str | None) -> Preferences:
    """The one document ``org_id`` reads: the shared keys, and that org's copy
    of the scoped keys (their defaults when it has none). Only a reader with no
    org reads the top-level copy."""
    data = stored.model_dump(mode="json")
    orgs = data.pop("orgs", {})
    if org_id:
        for key in ORG_SCOPED_KEYS:
            data.pop(key, None)
        scoped = orgs.get(_org_key(org_id)) or {}
        data.update({k: v for k, v in scoped.items() if k in ORG_SCOPED_KEYS})
    return Preferences.model_validate(data)


def _folded(
    stored: DesktopPreferences, updated: Preferences, org_id: str | None
) -> DesktopPreferences:
    """The file after ``updated`` is written as ``org_id``: its scoped keys go
    to that org's entry, the top-level copy and every other org's entry are
    kept, and the shared keys are written at the top level."""
    data = updated.model_dump(mode="json")
    data.pop("orgs", None)
    orgs = dict(stored.orgs)
    if org_id:
        orgs[_org_key(org_id)] = {key: data.pop(key) for key in sorted(ORG_SCOPED_KEYS)}
        data.update(stored.model_dump(mode="json", include=set(ORG_SCOPED_KEYS)))
    data["orgs"] = orgs
    return DesktopPreferences.model_validate(data)


def _write_unlocked(prefs: Preferences) -> None:
    payload = prefs.model_dump(mode="json")
    text = yaml.safe_dump(payload, sort_keys=False)
    write_text_atomic(paths.PREFERENCES_FILE_PATH, text, mode=0o600)


class _PreferencesLock:
    """Acquire the preferences `FileLock` with a short bounded retry.

    The lock is held only for a microscopic read-modify-write, so live
    contention (CLI vs. daemon writing at the same instant) is rare and
    resolves within a couple of retries. Dead holders are reclaimed by
    `FileLock` itself via PID liveness."""

    def __init__(self) -> None:
        self._lock = FileLock(paths.PREFERENCES_LOCK_PATH)

    def __enter__(self) -> FileLock:
        retry_until(
            self._lock.acquire,
            retry_on=LockHeldError,
            timeout=_LOCK_TIMEOUT_SECONDS,
            first=_LOCK_FIRST_POLL_SECONDS,
            cap=_LOCK_MAX_POLL_SECONDS,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        return self._lock

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


__all__ = [
    "credential_org",
    "load_preferences",
    "load_preferences_as",
    "preferences_org",
    "save_preferences",
    "set_preference",
    "set_preference_as",
    "settable_fields",
    "update_preferences",
]
