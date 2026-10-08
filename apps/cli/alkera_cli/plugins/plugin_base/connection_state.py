"""One standing state per connection this workspace can reach, local or team.

A connection rots unattended: an OAuth token expires, an env var stops being
exported, a secret file is deleted, an admin rotates a warehouse password. This
keeps the facts every surface derives its badge from (the last outcome, when it
was last checked, when it was last verified, and what the credential needs from
a person) in one document keyed by one identity.

A team row is ``team:<record_id>`` and a local one ``local:<plugin>:<handle>``,
so a team row's own Test, accept and query outcomes land under the row the
member sees, and whatever observed the I/O writes where the list reads.

Two writers feed it. The scheduled credential sweep opens no socket, so its
``ok`` only ever means an expiry was recorded and has not passed. Real network
verdicts arrive from I/O the product was doing anyway, through
:func:`record_io_outcome`. The sweep never overwrites what real I/O just learned
(:func:`observed_outranks`), and ``verified_at`` (stamped only by a real check)
feeds the freshness horizon, so an unattended "Connected" ages to `stale`
instead of claiming forever that somebody looked.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import structlog
from alkera_core.atomic_io import write_json_atomic
from alkera_core.connections import (
    FRESHNESS_HORIZON,
    Badge,
    Blocker,
    CredentialState,
    Outcome,
    Reauth,
    StatusInputs,
    derive_badge,
)
from alkera_core.connectors.redaction import redact_secrets
from alkera_core.extensions import ExtensionPoint
from alkera_core.project.locking import LockHeldError
from alkera_core.project.sidecar_lock import sidecar_lock
from alkera_core.versioning import VersionedModel
from pydantic import Field

if TYPE_CHECKING:
    from alkera_core.connectors.connection import Connection
    from alkera_core.project import ProjectDirectory

logger = structlog.get_logger(__name__)

#: The sweep's reading for a connection it cannot settle cheaply. Not an
#: ``Outcome``: unprobed is not a verdict, and the record keeps whatever real
#: check it last had rather than pretending this one said anything.
UNVERIFIABLE = ""

#: How long an OBSERVED failure outranks the cheap reading. Real I/O saw the
#: vendor; metadata did not.
OBSERVED_TTL_SECONDS = 3600.0


def local_key(plugin: str, handle: str) -> str:
    """The key for one of the member's own connections."""
    return f"local:{plugin}:{handle}"


def team_key(record_id: str) -> str:
    """The key for a team/org Preconfigured Connection row."""
    return f"team:{record_id}"


def key_for(conn: Connection) -> str:
    """The key ``conn``'s outcomes belong under.

    A team row materializes as an ordinary ``Connection`` carrying the server
    record id, and that id — not the local handle it happens to have taken — is
    its identity everywhere else, so its verdicts key off it here too.
    """
    record_id = str(getattr(conn, "_runtime_bindings", {}).get("team_record_id", "") or "")
    if not record_id:
        record_id = str(conn.attributes.get("team_record_id", "") or "")
    if record_id:
        return team_key(record_id)
    return local_key(conn.plugin, conn.handle)


def identity_of(key: str) -> tuple[str, str, str, str]:
    """``(origin, plugin, handle, record_id)`` read back off a key."""
    if key.startswith("team:"):
        return ("team", "", "", key[len("team:") :])
    rest = key[len("local:") :] if key.startswith("local:") else key
    plugin, _, handle = rest.partition(":")
    return ("local", plugin, handle, "")


def _sentence(text: str) -> str:
    """A detail from any source as one displayable sentence. ``classify_exception``
    speaks in lowercase fragments for a log line, and this field is read by a
    person in a connection row."""
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


class ConnectionStateRecord(VersionedModel):
    """One connection's standing state."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    key: str = ""
    origin: str = "local"
    """``local`` (the member's own ``connections.json``) | ``team`` (a
    Preconfigured Connection row, keyed by its server record id)."""
    plugin: str = ""
    handle: str = ""
    record_id: str = ""
    outcome: str = ""
    """An :class:`~alkera_core.connections.Outcome` member, or ``""`` for never
    settled. Held as ``str`` so an outcome written by a newer client survives an
    older reader instead of failing validation."""
    detail: str = ""
    """One user-facing sentence explaining a non-``ok`` outcome. Stays local: the
    cloud inventory lane uploads the outcome, never this."""
    source: str = "sweep"
    """``sweep`` (credential metadata only) | ``observed`` (real I/O) |
    ``verify`` (somebody pressed Test)."""
    checked_at: datetime | None = None
    verified_at: datetime | None = None
    """When a REAL check last settled, whatever it found. Feeds the freshness
    horizon, so an ``ok`` nobody has re-proven in 24 h reads ``stale``."""
    credential_state: str = CredentialState.present.value
    reauth: str = ""
    """A :class:`~alkera_core.connections.Reauth` member naming what fixes a
    credential a person has to touch, or ``""``."""


class ConnectionStateDocument(VersionedModel):
    """The whole document, keyed by :func:`key_for`."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    connections: dict[str, ConnectionStateRecord] = Field(default_factory=dict)


#: The legacy statuses of ``connection-health.json``, mapped onto outcomes. The
#: old ``unverifiable`` was "nothing cheap to check", which is no outcome at all.
_LEGACY_OUTCOMES: dict[str, str] = {
    "ok": Outcome.ok.value,
    "invalid_credential": Outcome.invalid_credential.value,
    "unreachable": Outcome.unreachable.value,
    "timeout": Outcome.timeout.value,
    "permission": Outcome.permission.value,
    "error": Outcome.error.value,
    "unverifiable": UNVERIFIABLE,
}


def _migrated_from_health(raw: str) -> ConnectionStateDocument:
    """The old health document as state records, one per entry.

    Best-effort by design: a verdict is re-derived by the next sweep anyway, so
    an unreadable legacy file costs a badge for fifteen minutes, never a raise on
    a path whose real job is a connection list.
    """
    document = ConnectionStateDocument()
    try:
        legacy = ConnectionStateDocument.model_validate_json(raw).connections
    except ValueError:
        return document
    for old_key, entry in legacy.items():
        extra = entry.model_extra or {}
        plugin = entry.plugin or str(extra.get("plugin", ""))
        handle = entry.handle or str(extra.get("handle", ""))
        if not plugin or not handle:
            plugin, _, handle = old_key.partition(":")
        status = str(extra.get("status", "")) or UNVERIFIABLE
        key = local_key(plugin, handle)
        document.connections[key] = ConnectionStateRecord(
            key=key,
            origin="local",
            plugin=plugin,
            handle=handle,
            outcome=_LEGACY_OUTCOMES.get(status, Outcome.error.value if status else UNVERIFIABLE),
            detail=entry.detail,
            source=entry.source,
            checked_at=entry.checked_at,
            # The old field only ever recorded an ``ok``; a real check is a real
            # check, so it carries over as one.
            verified_at=extra_datetime(extra.get("last_verified_at")),
        )
    return document


def extra_datetime(value: Any) -> datetime | None:
    """A datetime off an untyped legacy field, or ``None``."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        with suppress(ValueError):
            return datetime.fromisoformat(value)
    return None


class ConnectionStateStore:
    """Atomic read/write of ``.alkera/connection-state.json``.

    State lives apart from ``connections.json`` because that allow-list is
    lock-free last-writer-wins on rare human edits, and a background sweep
    writing into it would break that. This document has several writers and the
    daemon reaches them from its thread pool, so every mutation takes the paired
    in-process and cross-process lock. A missing or corrupt file reads as the
    empty state, since the next sweep re-derives all of it.
    """

    def __init__(self, path: Path, *, legacy_path: Path | None = None) -> None:
        self._path = Path(path)
        self._legacy_path = (
            Path(legacy_path)
            if legacy_path is not None
            else self._path.with_name("connection-health.json")
        )

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ConnectionStateDocument:
        try:
            raw = self._path.read_text()
        except OSError:
            return self._from_legacy()
        try:
            return ConnectionStateDocument.model_validate_json(raw)
        except ValueError:
            return ConnectionStateDocument()

    def _from_legacy(self) -> ConnectionStateDocument:
        """The pre-migration document, read out of the old health file.

        Migration is a read, not a rewrite: the first mutation persists it under
        the new name and the old file is left where it is, so an older client
        sharing the workspace keeps reading what it wrote.
        """
        try:
            raw = self._legacy_path.read_text()
        except OSError:
            return ConnectionStateDocument()
        return _migrated_from_health(raw)

    def update(
        self, mutate: Callable[[ConnectionStateDocument], ConnectionStateDocument]
    ) -> ConnectionStateDocument:
        """Read, apply ``mutate``, write back, and return what was PERSISTED.

        A contended or failed write returns the state still on disk, never the
        mutation that did not land: a caller told its verdict was stored when it
        was not would report a lie to whoever pressed Test.
        """
        try:
            with sidecar_lock(self._path):
                state = mutate(self.load())
                write_json_atomic(self._path, state.model_dump(mode="json"))
                return state
        except LockHeldError:
            logger.info("connection_state.lock_contended", path=str(self._path))
        except OSError:
            logger.warning("connection_state.write_failed", path=str(self._path), exc_info=True)
        return self.load()


def store_for(project: ProjectDirectory) -> ConnectionStateStore:
    return ConnectionStateStore(
        project.connection_state_path, legacy_path=project.connection_health_path
    )


def load_states(project: ProjectDirectory) -> dict[str, ConnectionStateRecord]:
    """Every standing state, best-effort. A badge is not worth failing a
    connection list over, and the next sweep re-derives the document anyway."""
    try:
        return dict(store_for(project).load().connections)
    except Exception:
        logger.debug("connection_state.unreadable", exc_info=True)
        return {}


def state_for(project: ProjectDirectory, key: str) -> ConnectionStateRecord | None:
    return load_states(project).get(key)


# ----------------------------------------------------------------------
# Listeners: who wants to know that a connection's state moved
# ----------------------------------------------------------------------

StateListener = Callable[[Path, str], None]
_LISTENERS: list[StateListener] = []


def on_state_written(listener: StateListener) -> Callable[[], None]:
    """Call ``listener(project_path, key)`` after a record is persisted.

    Process-global because the writers are not: a query tool, a refresh job and
    the sweep all record outcomes with no runtime in hand, and the daemon still
    has to push the new badge. Listeners filter by project path; the returned
    callable unsubscribes.
    """
    _LISTENERS.append(listener)

    def _off() -> None:
        with suppress(ValueError):
            _LISTENERS.remove(listener)

    return _off


def notify_state_written(project: ProjectDirectory, key: str) -> None:
    """Tell every listener that ``key`` moved. Never raises into a writer."""
    for listener in list(_LISTENERS):
        try:
            listener(project.path, key)
        except Exception:
            logger.debug("connection_state.listener_failed", key=key, exc_info=True)


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


def record_outcome(
    project: ProjectDirectory,
    key: str,
    outcome: str,
    detail: str = "",
    *,
    source: str = "observed",
    verified: bool,
    credential_state: str | None = None,
    reauth: str | None = None,
    plugin: str = "",
    handle: str = "",
    now: Callable[[], float] | None = None,
    notify: bool = True,
) -> ConnectionStateRecord:
    """Record one outcome against ``key`` and return what was PERSISTED.

    ``verified`` says whether something actually checked: a real probe, a query,
    a Test. The cheap sweep passes ``False``, so its reading refreshes
    ``checked_at`` and leaves ``verified_at`` where the last real check put it —
    which is what lets an unattended "Connected" age to `stale`.

    ``now`` defaults to the wall clock READ AT CALL TIME, never captured in the
    signature: a default argument binds the real ``time.time`` once at import and
    no frozen clock can reach it afterwards, which is exactly the freshness
    boundary these records have to be driven across in a test.
    """
    origin, key_plugin, key_handle, record_id = identity_of(key)
    moment = datetime.fromtimestamp((now or time.time)(), tz=UTC)

    def _apply(state: ConnectionStateDocument) -> ConnectionStateDocument:
        prior = state.connections.get(key)
        state.connections[key] = ConnectionStateRecord(
            key=key,
            origin=origin,
            plugin=plugin or key_plugin or (prior.plugin if prior else ""),
            handle=handle or key_handle or (prior.handle if prior else ""),
            record_id=record_id,
            outcome=outcome,
            detail=_sentence(detail),
            source=source,
            checked_at=moment,
            verified_at=moment if verified else (prior.verified_at if prior else None),
            credential_state=(
                credential_state
                if credential_state is not None
                else _credential_state_for(outcome, prior)
            ),
            reauth=reauth if reauth is not None else _reauth_for(outcome, prior),
        )
        return state

    persisted = store_for(project).update(_apply).connections.get(key) or ConnectionStateRecord(
        key=key
    )
    if notify:
        notify_state_written(project, key)
    return persisted


def _credential_state_for(outcome: str, prior: ConnectionStateRecord | None) -> str:
    """The credential axis implied by an outcome alone.

    A refused credential needs a person; anything the vendor answered at all
    clears a previous "needs a person", because the credential plainly works.
    Every other outcome (unreachable, timeout) says nothing about the credential,
    so it keeps what was there.
    """
    if outcome == Outcome.invalid_credential.value:
        return CredentialState.needs_reauth.value
    if outcome == Outcome.ok.value:
        return CredentialState.present.value
    return prior.credential_state if prior else CredentialState.present.value


def _reauth_for(outcome: str, prior: ConnectionStateRecord | None) -> str:
    if outcome == Outcome.ok.value:
        return ""
    if outcome == Outcome.invalid_credential.value:
        return (prior.reauth if prior else "") or Reauth.reenter.value
    return prior.reauth if prior else ""


def outcome_of(
    exc: BaseException | None, *, secrets: Iterable[str | None] = ()
) -> tuple[str, str, str]:
    """``(outcome, detail, reauth)`` for one exception, or a clean run.

    A connector that names its own verdict is believed: an exception carrying an
    ``outcome`` attribute (a typed probe failure, a refused sign-in) settles the
    classification before the prose table ever runs, so a genuine "sign in again"
    never badges as a generic error.

    Every branch here returns words a DRIVER wrote, and a service that refuses a
    credential may quote it back, so ``secrets`` (the plaintext the failed call
    used) is scrubbed out of the detail whichever branch produced it. The detail
    is persisted on the state record and rendered in a connection row, so an
    unscrubbed one puts a live credential in a durable file.
    """
    if exc is None:
        return (Outcome.ok.value, "", "")
    typed = getattr(exc, "outcome", None)
    reauth = str(getattr(exc, "reauth", "") or "")
    detail = redact_secrets(str(getattr(exc, "detail", "") or "") or str(exc), secrets)
    if typed:
        return (str(typed), detail, reauth)
    if isinstance(exc, TimeoutError):
        return (Outcome.timeout.value, detail or "the check timed out", reauth)
    scrubbed = tuple(secret for secret in secrets if secret)
    for explain in FAILURE_EXPLAINERS.items():
        explained = explain(exc, scrubbed)
        if explained is not None:
            return explained
    return (Outcome.error.value, _sentence(detail), reauth)


#: ``(exception, secrets) -> (outcome, detail, reauth)`` for a failure a
#: distribution's drivers raise, or ``None`` to leave it to the next one. The
#: detail must already have ``secrets`` scrubbed out.
FailureExplainer = Callable[[BaseException, tuple[str, ...]], "tuple[str, str, str] | None"]

#: How a driver's failure becomes a connection's outcome, asked in registration
#: order after the typed and timeout cases. With none, a failure is an error
#: carrying the driver's own scrubbed sentence.
FAILURE_EXPLAINERS: ExtensionPoint[FailureExplainer] = ExtensionPoint(
    "connections.failure_explainers"
)


def _secrets_of(conn: Connection) -> tuple[str, ...]:
    """The plaintext the failed call used, so the driver's sentence can be scrubbed.

    Resolved here rather than asked of each caller: the recorder is the seam
    every observed failure passes through, and a caller that forgets writes a
    credential into a file that outlives the process. Best-effort — an
    unresolvable reference is simply not matched, and a resolver that raises
    must not cost the health reading itself.
    """
    from alkera_cli.plugins.plugin_base.credential_manager import credential_plaintexts

    try:
        return credential_plaintexts(conn)
    except Exception:
        logger.info("connection_state.secrets_unresolved", plugin=conn.plugin, exc_info=True)
        return ()


def record_io_outcome(
    project: ProjectDirectory,
    conn: Connection,
    exc: BaseException | None,
    *,
    now: Callable[[], float] | None = None,
    source: str = "observed",
) -> None:
    """Feed the outcome of I/O the product was doing anyway into the store.

    Keyed by origin, so a team row's own failures land where its row reads them
    instead of being dropped for not appearing in the member's allow-list.
    Best-effort throughout: this rides paths whose real job is a query or a
    refresh.
    """
    try:
        outcome, detail, reauth = outcome_of(exc, secrets=_secrets_of(conn) if exc else ())
        key = key_for(conn)
        record_outcome(
            project,
            key,
            outcome,
            detail,
            source=source,
            verified=True,
            reauth=reauth or None,
            credential_state=(
                CredentialState.needs_reauth.value
                if reauth and outcome == Outcome.invalid_credential.value
                else None
            ),
            plugin=conn.plugin,
            handle=conn.handle,
            now=now,
        )
    except Exception:
        logger.info("connection_state.record_skipped", plugin=conn.plugin, exc_info=True)


@asynccontextmanager
async def records_health(
    project: ProjectDirectory,
    conn: Connection,
    *,
    record_success: bool = True,
    source: str = "observed",
) -> AsyncIterator[None]:
    """Feed the wrapped I/O's outcome into the store. An exception records a
    classified failure and propagates; a clean exit records a success. Pass
    ``record_success=False`` when a clean run proves nothing, the way an
    offline seed reads a local file and authenticates nobody. A cancellation
    records nothing, because it says nothing about the credential."""
    try:
        yield
    except BaseException as exc:
        if isinstance(exc, Exception):
            await asyncio.to_thread(record_io_outcome, project, conn, exc, source=source)
        raise
    if record_success:
        await asyncio.to_thread(record_io_outcome, project, conn, None, source=source)


def forget(project: ProjectDirectory, key: str, *, notify: bool = True) -> None:
    """Drop one connection's record, so a removed connection that is re-added
    starts unjudged instead of inheriting an hour of someone else's history."""

    def _drop(state: ConnectionStateDocument) -> ConnectionStateDocument:
        state.connections.pop(key, None)
        return state

    store_for(project).update(_drop)
    if notify:
        notify_state_written(project, key)


def rename(project: ProjectDirectory, old_key: str, new_key: str) -> None:
    """Move one connection's record to a new key. A rename keeps the same
    credential and the same warehouse, so it keeps its history — dropping it
    would blank the badge of a connection nothing happened to."""
    if old_key == new_key:
        return
    _, plugin, handle, record_id = identity_of(new_key)

    def _move(state: ConnectionStateDocument) -> ConnectionStateDocument:
        record = state.connections.pop(old_key, None)
        if record is not None:
            state.connections[new_key] = record.model_copy(
                update={
                    "key": new_key,
                    "plugin": plugin or record.plugin,
                    "handle": handle or record.handle,
                    "record_id": record_id,
                }
            )
        return state

    store_for(project).update(_move)
    notify_state_written(project, old_key)
    notify_state_written(project, new_key)


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def badge_for(
    record: ConnectionStateRecord | None,
    *,
    enabled: bool = True,
    muted: bool = False,
    blocker: Blocker = "",
    now: datetime | None = None,
    horizon: timedelta = FRESHNESS_HORIZON,
) -> Badge:
    """The one derived badge for a standing state.

    Every surface calls this rather than mapping outcomes itself, so a local row,
    a team row and the server's own row all read the same way.
    """
    moment = now or datetime.now(tz=UTC)
    return derive_badge(
        status_inputs(record, enabled=enabled, muted=muted, blocker=blocker),
        now=moment,
        horizon=horizon,
    )


def status_inputs(
    record: ConnectionStateRecord | None,
    *,
    enabled: bool = True,
    muted: bool = False,
    blocker: Blocker = "",
) -> StatusInputs:
    """The facts ``derive_badge`` reads, from one stored record.

    An outcome or credential state a newer client wrote is dropped rather than
    guessed at: an unknown value derives as "never checked", never as healthy.
    """
    outcome: Outcome | None = None
    credential = CredentialState.present
    if record is not None:
        with suppress(ValueError):
            outcome = Outcome(record.outcome) if record.outcome else None
        with suppress(ValueError):
            credential = CredentialState(record.credential_state or CredentialState.present.value)
    return StatusInputs(
        enabled=enabled,
        muted=muted,
        blocker=blocker,
        credential_state=credential,
        last_outcome=outcome,
        last_verified_at=_aware(record.verified_at) if record else None,
    )


def _aware(moment: datetime | None) -> datetime | None:
    """A stored timestamp as an aware one — a naive value read back from an older
    writer would raise against an aware ``now``."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def observed_outranks(
    prior: ConnectionStateRecord | None, *, moment: float, ttl_seconds: float
) -> bool:
    """Whether ``prior`` is a real-I/O verdict the cheap reading may not overwrite.

    An observed ``ok`` is never held, so a credential that has since expired on disk
    says so. A transient failure is held for its window, long enough not to flap while
    the vendor is still refusing. A REJECTED credential is held with no window at all:
    metadata cannot tell a revoked token from a live one, so a lapse badges the
    connection Connected again. Only real I/O or a change to the connection knows it
    was fixed, and every one of those writes a verdict here."""
    if prior is None or prior.source == "sweep" or prior.outcome == Outcome.ok.value:
        return False
    if prior.outcome == Outcome.invalid_credential.value:
        return True
    if prior.checked_at is None:
        return False
    stamped = _aware(prior.checked_at)
    assert stamped is not None
    return moment - stamped.timestamp() < ttl_seconds


__all__ = [
    "OBSERVED_TTL_SECONDS",
    "UNVERIFIABLE",
    "ConnectionStateDocument",
    "ConnectionStateRecord",
    "ConnectionStateStore",
    "badge_for",
    "forget",
    "identity_of",
    "key_for",
    "load_states",
    "local_key",
    "notify_state_written",
    "observed_outranks",
    "on_state_written",
    "outcome_of",
    "record_io_outcome",
    "record_outcome",
    "records_health",
    "rename",
    "state_for",
    "status_inputs",
    "store_for",
    "team_key",
]
