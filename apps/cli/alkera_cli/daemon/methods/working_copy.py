"""Daemon methods: a working copy of a chat's files for the editor to open.

A façade over :mod:`alkera_cli.files.working_copy` (what a sync does) and
:mod:`alkera_cli.files.working_copy_live` (when it runs). The editor asks this
daemon to make a copy (``working_copy.open``), to keep the copy that is open in
its window current (``working_copy.attach``), to sync now (after a save), and
to settle a conflict. Every pass that did something, or has something to say,
comes back as a ``working_copy.synced`` notification.

All four methods are additions to the protocol: an editor build that predates
them never calls them, and an editor that meets a daemon without them gets
JSON-RPC's own "method not found" and says the runtime needs an update.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from alkera_sdk import AlkeraClient
from alkera_sdk.client import AlkeraAuthError
from pydantic import Field

from alkera_cli.account.auth_file import api_credentials
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.daemon.logging_setup import get_logger
from alkera_cli.daemon.protocol import _DaemonModel, method, notification
from alkera_cli.daemon.server import AuthRequiredError, register_shutdown_hook
from alkera_cli.files.working_copy import (
    CopyStoppedError,
    CopyTarget,
    HttpTargetReader,
    SyncReport,
    TargetUnavailableError,
    WorkingCopy,
    WorkingCopyAuthError,
    WorkingCopyStore,
)
from alkera_cli.files.working_copy_live import (
    AttachedCopy,
    WorkingCopyRunner,
    WorkingCopySessions,
    sessions_of,
    watch_directory,
)
from alkera_cli.host.backoff import ReconnectBackoff

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer

log = get_logger("alkera.daemon.working_copy")


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class WorkingCopyConflict(_DaemonModel):
    path: str
    reason: str
    """``edited_both`` or ``created_both``; an editor that meets a reason it
    does not know shows the path as a conflict all the same."""


class WorkingCopyRefusal(_DaemonModel):
    path: str
    code: str
    message: str
    """The sentence the person reads."""


class WorkingCopyBackup(_DaemonModel):
    """A local version set aside before it was replaced."""

    path: str
    saved_at: str


class WorkingCopyReport(_DaemonModel):
    """What one pass did, by path relative to the copy's root."""

    downloaded: list[str] = Field(default_factory=list)
    uploaded: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    trashed: list[str] = Field(default_factory=list)
    restored: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    conflicts: list[WorkingCopyConflict] = Field(default_factory=list)
    refused: list[WorkingCopyRefusal] = Field(default_factory=list)
    busy: bool = False
    deletions_held: list[str] = Field(default_factory=list)
    """Files missing here that would have been trashed in Alkera, held because
    there were too many. The editor asks, then calls
    ``working_copy.resolve_deletions``."""
    backups: list[WorkingCopyBackup] = Field(default_factory=list)


def _report(report: SyncReport) -> WorkingCopyReport:
    return WorkingCopyReport(
        downloaded=report.downloaded,
        uploaded=report.uploaded,
        removed=report.removed,
        trashed=report.trashed,
        restored=report.restored,
        pending=report.pending,
        conflicts=[WorkingCopyConflict(path=c.path, reason=c.reason) for c in report.conflicts],
        refused=[
            WorkingCopyRefusal(path=r.path, code=r.code, message=r.message) for r in report.refused
        ],
        busy=report.busy,
        deletions_held=report.deletions_held,
        backups=[
            WorkingCopyBackup(path=path, saved_at=saved) for path, saved in report.backups.items()
        ],
    )


OpenStatus = Literal["opened", "invalid", "unsupported", "not_found", "no_files", "location_taken"]


class WorkingCopyOpenRequest(_DaemonModel):
    kind: str = "chat"
    """What ``id`` names: ``chat`` or ``workspace``. A workspace of one copies
    its chat's working directory (the same copy the chat's link makes); a
    project workspace copies its shared files tree. A kind this build cannot
    copy answers ``unsupported``."""
    id: str
    location: str
    """The absolute directory a NEW copy is made in. A target copied before
    keeps the directory it already has."""


class WorkingCopyOpenResponse(_DaemonModel):
    status: OpenStatus
    message: str = ""
    root: str | None = None
    title: str = ""
    report: WorkingCopyReport | None = None


class WorkingCopyAttachRequest(_DaemonModel):
    root: str


class WorkingCopyAttachResponse(_DaemonModel):
    attached: bool
    """False when ``root`` is not a working copy this machine made."""
    kind: str = ""
    id: str = ""
    title: str = ""


class WorkingCopySyncRequest(_DaemonModel):
    """A save: send what changed under ``root``. Nothing is read back; the
    drive's changes arrive through the realtime stream."""

    root: str


class WorkingCopySyncResponse(_DaemonModel):
    attached: bool
    report: WorkingCopyReport | None = None


class WorkingCopyResolveRequest(_DaemonModel):
    root: str
    path: str
    keep: Literal["mine", "theirs"]


class WorkingCopyResolveResponse(_DaemonModel):
    attached: bool
    report: WorkingCopyReport | None = None


class WorkingCopyResolveDeletionsRequest(_DaemonModel):
    root: str
    apply: bool
    """True trashes the held files in Alkera; false brings them back here."""


class WorkingCopyResolveDeletionsResponse(_DaemonModel):
    attached: bool
    report: WorkingCopyReport | None = None


class WorkingCopySetDirtyRequest(_DaemonModel):
    """The files under ``root`` with unsaved edits in the editor, as paths
    relative to it. The whole set every time, so a lost message heals on the
    next one."""

    root: str
    paths: list[str] = Field(default_factory=list)


class WorkingCopySetDirtyResponse(_DaemonModel):
    attached: bool


@notification("working_copy.synced")
class WorkingCopySyncedNotification(_DaemonModel):
    """A pass of an attached copy did something, or failed outright."""

    root: str
    report: WorkingCopyReport | None = None
    error: str | None = None
    auth_required: bool = False
    """The stored sign-in was refused; the copy stops until the editor
    attaches again after a sign-in."""
    access_lost: bool = False
    """The copied tree is no longer this account's (a revoked share, a deleted
    folder). The copy stops for good and its local files are left as they
    are; ``error`` is the sentence to show."""
    detached: bool = False
    """The local folder is gone. The copy stops and nothing is changed in
    Alkera; ``error`` is the sentence to show."""


#: The reasons a target is refused are the statuses ``open`` answers with.
_REFUSED: dict[str, OpenStatus] = {
    "invalid": "invalid",
    "unsupported": "unsupported",
    "not_found": "not_found",
    "no_files": "no_files",
}

#: What the editor is told about a pass that failed outright. The exception
#: itself can name URLs and local paths; it goes to the daemon's log instead.
#: A copy that stopped for good says why in its own sentence, which names only
#: the copy.
SIGN_IN_REFUSED_MESSAGE = "Sign in to Alkera again to keep this folder in sync."
SYNC_FAILED_MESSAGE = "Couldn't sync this folder. Alkera will try again."


def _credentials() -> tuple[str, str]:
    """The signed-in user's API URL and token, read per call."""
    credentials = api_credentials()
    if credentials is None:
        raise AuthRequiredError("missing")
    return credentials


def _store() -> WorkingCopyStore:
    """Where records live, read per call so ``ALKERA_HOME`` is honoured."""
    return WorkingCopyStore()


def _frames(api_url: str, token: str) -> AsyncIterator[Mapping[str, Any]]:
    """The signed-in user's realtime stream. Its opened marker is already the
    runner's: both spell ``alkera.stream.opened``."""

    async def stream() -> AsyncIterator[Mapping[str, Any]]:
        rest = CloudRestClient(api_url=api_url, token=token, agent_id=None)
        async for event in rest.events():
            yield event

    return stream()


def _closer(client: AlkeraClient) -> Callable[[], None]:
    return client.raw_client.get_httpx_client().close


def _sign_in_refused(exc: BaseException) -> bool:
    """The stored sign-in was refused: the one failure the editor can fix.
    Any other refusal (a 403 is what this identity earned) keeps its own."""
    return isinstance(exc, WorkingCopyAuthError) or (
        isinstance(exc, AlkeraAuthError) and exc.status == 401
    )


@contextlib.contextmanager
def _sign_in_prompt() -> Iterator[None]:
    """A refused sign-in raised inside becomes the daemon's AuthRequired,
    which the server answers as -32001 for the extension's sign-in flow."""
    try:
        yield
    except (WorkingCopyAuthError, AlkeraAuthError) as exc:
        if not _sign_in_refused(exc):
            raise
        raise AuthRequiredError("rejected") from exc


async def _blocking(work: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one library call off the loop, with a refused sign-in made the
    one condition the editor can fix."""
    with _sign_in_prompt():
        return await asyncio.to_thread(work, *args, **kwargs)


def _failure_message(exc: BaseException) -> str:
    if isinstance(exc, CopyStoppedError):
        return str(exc)
    if _sign_in_refused(exc):
        return SIGN_IN_REFUSED_MESSAGE
    return SYNC_FAILED_MESSAGE


async def _attached_pass(
    server: JsonRpcServer,
    root: str,
    work: Callable[[WorkingCopyRunner], Awaitable[SyncReport]],
) -> WorkingCopyReport | None:
    """Run ``work`` on the copy attached at ``root``. ``None`` when no copy is
    attached there, or the copy stopped for good on this pass."""
    session = sessions_of(server).get(root)
    if session is None:
        return None
    try:
        with _sign_in_prompt():
            report = await work(session.runner)
    except CopyStoppedError as exc:
        await _lose_access(server, root, exc)
        return None
    return _report(report)


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


@method("working_copy.open")
async def working_copy_open(
    server: JsonRpcServer, params: WorkingCopyOpenRequest
) -> WorkingCopyOpenResponse:
    location = Path(params.location)
    if not location.is_absolute():
        return WorkingCopyOpenResponse(
            status="invalid", message="The folder to make the copy in must be an absolute path."
        )
    api_url, token = _credentials()
    client = AlkeraClient(base_url=api_url, token=token)
    try:
        http = client.raw_client.get_httpx_client()
        copy, report = await _blocking(
            WorkingCopy.open,
            CopyTarget(params.kind, params.id),
            location=location,
            files=client.files,
            http=http,
            reader=HttpTargetReader(http),
            store=_store(),
        )
    except TargetUnavailableError as exc:
        return WorkingCopyOpenResponse(
            status=_REFUSED.get(exc.reason, "not_found"), message=str(exc)
        )
    except FileExistsError as exc:
        return WorkingCopyOpenResponse(status="location_taken", message=str(exc))
    finally:
        _closer(client)()
    return WorkingCopyOpenResponse(
        status="opened", root=str(copy.root), title=copy.record.title, report=_report(report)
    )


@method("working_copy.attach")
async def working_copy_attach(
    server: JsonRpcServer, params: WorkingCopyAttachRequest
) -> WorkingCopyAttachResponse:
    sessions = sessions_of(server)
    async with sessions.attach_lock(params.root):
        return await _attach(server, params, sessions)


async def _attach(
    server: JsonRpcServer, params: WorkingCopyAttachRequest, sessions: WorkingCopySessions
) -> WorkingCopyAttachResponse:
    session = sessions.get(params.root)
    if session is not None and session.runner.running:
        record = session.runner.copy.record
        return WorkingCopyAttachResponse(
            attached=True, kind=record.kind, id=record.target_id, title=record.title
        )
    await sessions.stop(params.root)
    api_url, token = _credentials()
    client = AlkeraClient(base_url=api_url, token=token)
    # Finding the record reads every record file: off the loop.
    copy = await asyncio.to_thread(
        WorkingCopy.attach,
        Path(params.root),
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        store=_store(),
    )
    if copy is None:
        _closer(client)()
        return WorkingCopyAttachResponse(attached=False)

    async def on_report(report: SyncReport) -> None:
        await server.notify(
            "working_copy.synced",
            WorkingCopySyncedNotification(root=params.root, report=_report(report)),
        )

    async def on_error(exc: BaseException) -> None:
        refused = _sign_in_refused(exc)
        stopped = exc.reason if isinstance(exc, CopyStoppedError) else ""
        if not (refused or stopped):
            log.warning(
                "working_copy.pass_failed",
                root=params.root,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        await server.notify(
            "working_copy.synced",
            WorkingCopySyncedNotification(
                root=params.root,
                error=_failure_message(exc),
                auth_required=refused,
                access_lost=stopped == "access_lost",
                detached=stopped == "detached",
            ),
        )
        if refused or stopped:
            sessions.stop_later(params.root)

    runner = WorkingCopyRunner(
        copy,
        frames=lambda: _frames(api_url, token),
        watch=watch_directory,
        on_report=on_report,
        on_error=on_error,
        backoff=ReconnectBackoff(),
    )
    sessions.put(params.root, AttachedCopy(runner=runner, close=_closer(client)))
    runner.start()
    runner.request()
    record = copy.record
    return WorkingCopyAttachResponse(
        attached=True, kind=record.kind, id=record.target_id, title=record.title
    )


@method("working_copy.sync")
async def working_copy_sync(
    server: JsonRpcServer, params: WorkingCopySyncRequest
) -> WorkingCopySyncResponse:
    report = await _attached_pass(server, params.root, lambda r: r.sync_now(pull=False))
    return WorkingCopySyncResponse(attached=report is not None, report=report)


@method("working_copy.resolve")
async def working_copy_resolve(
    server: JsonRpcServer, params: WorkingCopyResolveRequest
) -> WorkingCopyResolveResponse:
    report = await _attached_pass(
        server, params.root, lambda r: r.resolve(params.path, params.keep)
    )
    return WorkingCopyResolveResponse(attached=report is not None, report=report)


@method("working_copy.set_dirty")
async def working_copy_set_dirty(
    server: JsonRpcServer, params: WorkingCopySetDirtyRequest
) -> WorkingCopySetDirtyResponse:
    session = sessions_of(server).get(params.root)
    if session is None:
        return WorkingCopySetDirtyResponse(attached=False)
    session.runner.set_dirty(frozenset(params.paths))
    return WorkingCopySetDirtyResponse(attached=True)


async def _lose_access(server: JsonRpcServer, root: str, exc: CopyStoppedError) -> None:
    """Stop the copy at ``root`` for good and tell the editor why."""
    await sessions_of(server).stop(root)
    await server.notify(
        "working_copy.synced",
        WorkingCopySyncedNotification(
            root=root,
            error=_failure_message(exc),
            access_lost=exc.reason == "access_lost",
            detached=exc.reason == "detached",
        ),
    )


@method("working_copy.resolve_deletions")
async def working_copy_resolve_deletions(
    server: JsonRpcServer, params: WorkingCopyResolveDeletionsRequest
) -> WorkingCopyResolveDeletionsResponse:
    report = await _attached_pass(
        server, params.root, lambda r: r.resolve_deletions(apply=params.apply)
    )
    return WorkingCopyResolveDeletionsResponse(attached=report is not None, report=report)


@register_shutdown_hook
async def _stop_every_copy(server: JsonRpcServer) -> None:
    await sessions_of(server).stop_all()


__all__ = [
    "WorkingCopyAttachRequest",
    "WorkingCopyAttachResponse",
    "WorkingCopyBackup",
    "WorkingCopyConflict",
    "WorkingCopyOpenRequest",
    "WorkingCopyOpenResponse",
    "WorkingCopyRefusal",
    "WorkingCopyReport",
    "WorkingCopyResolveDeletionsRequest",
    "WorkingCopyResolveDeletionsResponse",
    "WorkingCopyResolveRequest",
    "WorkingCopyResolveResponse",
    "WorkingCopySessions",
    "WorkingCopySetDirtyRequest",
    "WorkingCopySetDirtyResponse",
    "WorkingCopySyncRequest",
    "WorkingCopySyncResponse",
    "WorkingCopySyncedNotification",
    "sessions_of",
    "working_copy_attach",
    "working_copy_open",
    "working_copy_resolve",
    "working_copy_resolve_deletions",
    "working_copy_set_dirty",
    "working_copy_sync",
]
