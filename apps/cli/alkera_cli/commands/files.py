"""The ``alkera files`` typer app.

A façade over :mod:`alkera_cli.files.push`, :mod:`alkera_cli.files.pull`,
:mod:`alkera_cli.files.mount` and the operator verbs, and nothing more: it
resolves the signed-in session, hands the library a client, and prints the
summary. Every decision a push or a pull
makes lives in the library so the daemon and the box agent can make the same
ones without a terminal.
"""

from __future__ import annotations

import os
import signal
import types
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, NoReturn

import httpx
import typer
from alkera_sdk import AlkeraClient
from alkera_sdk.client import AlkeraAuthError, AlkeraHTTPError
from rich.console import Console

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.account.binding import profile_for_project, profile_for_sync
from alkera_cli.commands.files_admin import admin_app
from alkera_cli.files.mount import (
    AlreadyMountedError,
    LeaseSupersededError,
    MountStatus,
    NotMountedError,
    mount,
    mounts,
    unmount,
    watch,
)
from alkera_cli.files.progress import TransferProgress
from alkera_cli.files.pull import LocalChangesError, PullSummary, pull
from alkera_cli.files.push import PushSummary, TransferTimeoutError, push
from alkera_cli.files.walk import EXCLUDE_PRESETS
from alkera_cli.host.paths import existing_project_directory

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

files_app = typer.Typer(help="Push and pull org files.", no_args_is_help=True)
#: The operator verbs (`scrub`, `fsck`, `verify`, `gc`, `quarantine`, …) are a
#: sub-app rather than commands on `files` so an operator's tooling is never a
#: typo away from a user's.
files_app.add_typer(admin_app, name="admin")

_console = Console()

#: A transfer is minutes-long, not seconds-long. The SDK defaults every phase
#: to 5 s, which a 100 MB push or pull spends waiting on the server to hash,
#: store and commit a single part — so the default aborts the transfer rather
#: than the stall it was written for. Every Files verb builds its client here,
#: and the raw-``httpx`` helpers in ``push``/``pull`` are handed that same
#: client, so setting it once covers the whole library.
FILES_TRANSFER_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


#: The exit code every "sign in again" refusal leaves, so a script can branch
#: on it without reading English.
SIGN_IN_EXIT: Final = 2

_SIGN_IN = "Your sign-in has expired — run `alkera login`"

#: Every refusal a customer can cause, as the sentence they read and the code
#: the shell sees. Keyed by the API's own error code first (it is the precise
#: fact) and by the status second (the family). Adding a refusal is adding a
#: row: no command grows its own `except`, and nothing here is a traceback.
_REFUSALS: Final[dict[str | int, tuple[str, int]]] = {
    "files.leased": ("That folder is checked out by someone else", 1),
    "leased": ("That folder is checked out by someone else", 1),
    401: (_SIGN_IN, SIGN_IN_EXIT),
    403: ("You do not have permission to do that", 1),
    404: ("No file or folder at that path", 2),
    409: ("Someone else changed that while you were working — try again", 1),
    413: ("That file is larger than this organization allows", 1),
    422: ("The server would not accept that name", 2),
    507: ("Your organization is out of storage", 1),
}


def _sentence_for(refusal: AlkeraHTTPError, *, subject: str | None) -> tuple[str, int]:
    """One refusal, in the customer's words, plus the code the shell sees."""
    if isinstance(refusal, AlkeraAuthError) and refusal.status == 401:
        return _SIGN_IN, SIGN_IN_EXIT
    said, code = _REFUSALS.get(
        refusal.code or "", _REFUSALS.get(refusal.status, ("Alkera could not do that", 1))
    )
    if refusal.status == 404 and subject:
        said = f"No file or folder at {subject}"
    if refusal.code in {"files.leased", "leased"}:
        holder = _holder_of(refusal)
        said = (
            f"That folder is checked out by {holder}; wait for them to unmount "
            "or ask them to release it"
            if holder
            else f"{said}; wait for them to unmount or ask them to release it"
        )
    return said, code


def _holder_of(refusal: AlkeraHTTPError) -> str | None:
    detail = refusal.detail or {}
    holder = detail.get("holder")
    return _person(holder)


@contextmanager
def _customer_errors(*, subject: str | None = None) -> Iterator[None]:
    """The one place a Files command turns a refusal into a sentence.

    A refusal is not a crash: a leased folder, a mistyped path, an expired
    session and a full drive are all things a person did, and every one of them
    used to reach the terminal as a traceback plus an invitation to file a crash
    report. They stop here — the crash prompt is left for the failures nobody
    asked for, which still travel as they always did.
    """
    try:
        yield
    except AlkeraHTTPError as refusal:
        said, code = _sentence_for(refusal, subject=subject)
        _console.print(f"[red]{said}[/red]")
        raise typer.Exit(code=code) from refusal
    except httpx.TimeoutException as slow:
        _console.print("[red]Alkera did not answer in time — check your connection[/red]")
        raise typer.Exit(code=1) from slow
    except httpx.TransportError as unreachable:
        _console.print("[red]Could not reach Alkera — check your connection[/red]")
        raise typer.Exit(code=1) from unreachable


def _person(holder: object) -> str | None:
    """A holder as a person: the name the API gave, never a bare id if it did."""
    if isinstance(holder, dict):
        for key in ("displayName", "display_name", "name", "fullName", "email"):
            named = holder.get(key)
            if named:
                return str(named)
        identifier = holder.get("id") or holder.get("userId")
        return str(identifier) if identifier else None
    return str(holder) if holder else None


def _enclosing_project(local: Path | None) -> ProjectDirectory | None:
    """The workspace ``local`` sits in (the nearest ancestor with ``.alkera/``)."""
    if local is None:
        return None
    for candidate in (local.resolve(), *local.resolve().parents):
        found = existing_project_directory(candidate)
        if found is not None:
            return found
    return None


def _signed_in_client(local: Path | None = None, *, sync: bool = False) -> AlkeraClient:
    """The API client for the sign-in this transfer acts as, or the CLI's
    refusal to guess.

    When ``local`` is inside a workspace, the workspace's org pin holds: a
    transfer under a sign-in for another org is refused before any request,
    and a first ``sync`` (a push, a mount) pins an unpinned workspace.

    The expiry on the saved credential is honoured *here*, before a call goes
    out: a token the file itself says lapsed cannot succeed, and letting it
    travel only trades one clear sentence for whatever the server says about a
    signature it no longer trusts.
    """
    project = _enclosing_project(local)
    try:
        auth = profile_for_sync(project) if sync else profile_for_project(project)
    except ProfileResolutionError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=SIGN_IN_EXIT) from exc
    if auth is None or not auth.token:
        _console.print("[red]You are not signed in — run `alkera login`[/red]")
        raise typer.Exit(code=SIGN_IN_EXIT)
    if _lapsed(getattr(auth, "expires_at", None)):
        _console.print(f"[red]{_SIGN_IN}[/red]")
        raise typer.Exit(code=SIGN_IN_EXIT)
    return AlkeraClient(
        base_url=auth.api_url,
        token=auth.token,
        org_id=auth.org_team_id or None,
        timeout=FILES_TRANSFER_TIMEOUT,
    )


def _lapsed(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    deadline = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return deadline <= datetime.now(UTC)


@files_app.command("push")
def push_command(
    directory: Annotated[Path, typer.Argument(help="The local directory to push.")],
    destination: Annotated[str, typer.Argument(help="The org path to push it into.")],
    respect_gitignore: Annotated[
        bool,
        typer.Option(
            "--respect-gitignore/--no-respect-gitignore",
            help="Fold paths the repository's .gitignore excludes (default on).",
        ),
    ] = True,
    exclude_preset: Annotated[
        list[str] | None,
        typer.Option("--exclude-preset", help=f"One of: {', '.join(sorted(EXCLUDE_PRESETS))}."),
    ] = None,
    include_link_targets: Annotated[
        bool,
        typer.Option("--include-link-targets", help="Also stage the targets of canonical links."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Also print the lease epoch, instance and node ids."),
    ] = False,
) -> None:
    """Walk ``directory`` into ``destination``, uploading only what changed."""
    presets = tuple(exclude_preset or ())
    unknown = [name for name in presets if name not in EXCLUDE_PRESETS]
    if unknown:
        _console.print(f"[red]unknown --exclude-preset: {', '.join(unknown)}[/red]")
        raise typer.Exit(code=2)
    if not directory.is_dir():
        _console.print(f"[red]{directory} is not a directory[/red]")
        raise typer.Exit(code=2)

    counter = TransferProgress()
    with _signed_in_client(directory, sync=True) as api, _customer_errors(subject=destination):
        try:
            summary = push(
                files=api.files,
                http=api.raw_client.get_httpx_client(),
                root=directory,
                dest=destination,
                respect_gitignore=respect_gitignore,
                exclude_presets=presets,
                include_link_targets=include_link_targets,
                progress=counter,
            )
        except TransferTimeoutError as stalled:
            counter.finish()
            _console.print(f"[red]{stalled}[/red]")
            raise typer.Exit(code=1) from stalled
        finally:
            counter.finish()
    _report(summary, verbose=verbose)


def _report(summary: PushSummary, *, verbose: bool = False) -> None:
    """What the push did, in the words of someone who did not write it.

    ``specials`` and ``pointers skipped`` are the library's vocabulary for a
    fifo and for a shortcut to a chat or a result; a customer reading the line
    has no way to learn either from the line. The counts stay — they are the
    proof the tree is whole — but they say what they are, and the internal
    spelling is available to whoever asks for ``--verbose``.
    """
    said = [
        f"{summary.uploaded} uploaded ({summary.bytes_uploaded} bytes)",
        f"{summary.unchanged} already up to date",
        f"{summary.folders} folders",
        f"{summary.symlinks} shortcuts",
    ]
    if summary.specials:
        said.append(f"{summary.specials} system files (pipes and devices)")
    if summary.excluded:
        said.append(f"{summary.excluded} skipped by your ignore rules")
    if summary.pointers_skipped:
        said.append(
            f"{summary.pointers_skipped} skipped: shortcuts to chats and results are not files"
        )
    _console.print(", ".join(said))
    if verbose:
        _console.print(
            f"[dim]specials={summary.specials} pointers_skipped={summary.pointers_skipped} "
            f"excluded={summary.excluded}[/dim]"
        )
    for warning in summary.warnings:
        _console.print(f"[yellow]warning:[/yellow] {warning}")


@files_app.command("pull")
def pull_command(
    source: Annotated[str, typer.Argument(help="The org path to pull.")],
    directory: Annotated[Path, typer.Argument(help="The local directory to pull it into.")],
    local_root: Annotated[
        Path | None,
        typer.Option(
            "--local-root",
            help="Where the org tree lives on this machine; canonical links resolve under it.",
        ),
    ] = None,
    trusted: Annotated[
        bool,
        typer.Option(
            "--trusted",
            help="Follow a symlinked directory already on disk instead of refusing it.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Also print the lease epoch, instance and node ids."),
    ] = False,
) -> None:
    """Materialize the org subtree ``source`` into ``directory``."""
    if directory.exists() and not directory.is_dir():
        _console.print(f"[red]{directory} is not a directory[/red]")
        raise typer.Exit(code=2)

    counter = TransferProgress()
    with _signed_in_client(directory) as api, _customer_errors(subject=source):
        try:
            summary = pull(
                files=api.files,
                http=api.raw_client.get_httpx_client(),
                root=directory,
                source=source,
                local_root=os.fsencode(local_root) if local_root is not None else None,
                trusted=trusted,
                progress=counter,
            )
        finally:
            counter.finish()
    _report_pull(summary, verbose=verbose)


def _report_pull(summary: PullSummary, *, verbose: bool = False) -> None:
    """What the pull did — see :func:`_report` on why the words are these."""
    said = [
        f"{summary.files} files ({summary.bytes_downloaded} bytes)",
        f"{summary.unchanged} already up to date",
        f"{summary.folders} folders",
        f"{summary.symlinks} shortcuts",
    ]
    if summary.resumed:
        said.append(f"{summary.resumed} continued from where they stopped")
    if summary.kept:
        said.append(f"{summary.kept} left alone: you have unsaved changes in them")
    if summary.specials:
        said.append(f"{summary.specials} system files (pipes and devices)")
    if summary.pointers:
        said.append(f"{summary.pointers} shortcuts to chats and results")
    if summary.deduped:
        said.append(f"{summary.deduped} second copies taken from disk rather than downloaded")
    if summary.undownloadable:
        said.append(f"{summary.undownloadable} left on the server: they cannot be downloaded")
    if summary.unpullable:
        said.append(f"{summary.unpullable} left on the server: their names are too long for disk")
    _console.print(", ".join(said))
    for kept in summary.kept_paths:
        _console.print(f"[yellow]kept your version of {os.fsdecode(kept)}[/yellow]")
    if verbose:
        _console.print(
            f"[dim]specials={summary.specials} pointers={summary.pointers} "
            f"deduped={summary.deduped} resumed={summary.resumed} kept={summary.kept}[/dim]"
        )
    for warning in summary.warnings:
        _console.print(f"[yellow]warning:[/yellow] {warning}")


@files_app.command("mount")
def mount_command(
    source: Annotated[str, typer.Argument(help="The org folder to lease and mount.")],
    directory: Annotated[Path, typer.Argument(help="The local directory to mount it at.")],
    hold_open: Annotated[
        bool,
        typer.Option(
            "--hold/--no-hold",
            help="Stay in the foreground heartbeating (default). --no-hold mounts and exits.",
        ),
    ] = True,
    watching: Annotated[
        bool,
        typer.Option(
            "--watch/--no-watch",
            help=(
                "Export the folder every sync interval, beside the heartbeat that keeps "
                "the lease (default). --no-watch only heartbeats."
            ),
        ),
    ] = True,
    streaming: Annotated[
        bool,
        typer.Option(
            "--live/--no-live",
            help=(
                "Ask the server for the streaming cadence as well, so changes can "
                "go up as they are written instead of only on the sync interval."
            ),
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Also print the lease epoch, instance and node ids."),
    ] = False,
) -> None:
    """Lease ``source``, pull it into ``directory``, and hold it.

    The command stays in the foreground because the lease is only alive while
    something beats for it: Ctrl-C stops the beat and leaves the mount in
    place, and `alkera files unmount` is what hands the folder back. While it
    holds it also exports the folder on the served sync cadence, so a mount
    that is never unmounted still has a cloud copy seconds behind it.

    ``--live`` is off unless asked for. A lease granted the streaming cadence
    costs the server a per-change plane it otherwise never opens, and a plain
    mount does not want one: it is the single writer it has always been, and
    the sync interval is what it exports on.
    """
    if directory.exists() and not directory.is_dir():
        _console.print(f"[red]{directory} is not a directory[/red]")
        raise typer.Exit(code=2)

    counter = TransferProgress()
    with _signed_in_client(directory, sync=True) as api, _customer_errors(subject=source):
        try:
            record, summary = mount(
                files=api.files,
                http=api.raw_client.get_httpx_client(),
                source=source,
                root=directory,
                live=streaming,
                pull_tree=pull,
                progress=counter,
            )
        except AlreadyMountedError as taken:
            counter.finish()
            _console.print(f"[red]{taken}[/red]")
            raise typer.Exit(code=2) from taken
        except LocalChangesError as unsaved:
            counter.finish()
            _unsaved(unsaved, directory)
        except LeaseSupersededError as refusal:
            counter.finish()
            _refused(refusal)
        finally:
            counter.finish()
        _console.print(f"checked out {source} into {directory}")
        if verbose:
            _console.print(f"[dim]epoch={record.epoch} instance={record.instance_id}[/dim]")
        _report_pull(summary, verbose=verbose)
        if not hold_open:
            return
        stopping = _StopOnSignal()
        try:
            watch(
                files=api.files if watching else None,
                http=api.raw_client.get_httpx_client(),
                record=record,
                stop=stopping,
                push_tree=push,
            )
        except LeaseSupersededError as refusal:
            _refused(refusal)
        _console.print("[yellow]stopped syncing; the folder is still checked out to you[/yellow]")


def _unsaved(unsaved: LocalChangesError, directory: Path) -> NoReturn:
    """The refusal that saves the work: name the files, then the way out."""
    named = ", ".join(os.fsdecode(one) for one in unsaved.paths[:5])
    if len(unsaved.paths) > 5:
        named += f" and {len(unsaved.paths) - 5} more"
    _console.print(
        f"[red]You have unsaved changes here that the cloud copy does not have: {named}. "
        f"Run `alkera files unmount {directory}` to send them up first[/red]"
    )
    raise typer.Exit(code=2)


class _StopOnSignal:
    """A ``stop()`` a terminal signal flips, so a held mount ends cleanly."""

    def __init__(self) -> None:
        self.stopped = False
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, self._flip)

    def _flip(self, _number: int, _frame: types.FrameType | None) -> None:
        self.stopped = True

    def __call__(self) -> bool:
        return self.stopped


@files_app.command("unmount")
def unmount_command(
    directory: Annotated[Path, typer.Argument(help="The mounted local directory.")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Also print the lease epoch, instance and node ids."),
    ] = False,
) -> None:
    """Push everything back under the lease, then release it in one call."""
    counter = TransferProgress()
    with _signed_in_client() as api, _customer_errors():
        try:
            summary = unmount(
                files=api.files,
                http=api.raw_client.get_httpx_client(),
                root=directory,
                push_tree=push,
            )
        except NotMountedError as absent:
            _console.print(f"[red]{absent}[/red]")
            raise typer.Exit(code=2) from absent
        except LeaseSupersededError as refusal:
            _refused(refusal)
        finally:
            counter.finish()
    _console.print(f"checked {summary.record.org_path} back in")
    if verbose:
        _console.print(f"[dim]epoch={summary.record.epoch}[/dim]")
    _report(summary.push, verbose=verbose)


@files_app.command("mounts")
def mounts_command(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Also print the lease epoch, instance and node ids."),
    ] = False,
) -> None:
    """List this machine's mounts and what the server says about each lease."""
    with _signed_in_client() as api, _customer_errors():
        rows = mounts(files=api.files, http=api.raw_client.get_httpx_client())
    if not rows:
        _console.print("nothing checked out here")
        return
    for status in rows:
        _console.print(_mount_line(status, verbose=verbose))


def _mount_line(status: MountStatus, *, verbose: bool) -> str:
    """One mount, said the way the person who ran the mount would say it.

    ``--no-hold`` is a supported way to take a folder — it checks out and
    exits — so whether a process is beating is not what decides the word. The
    lease is: its epoch, and its expiry, both from the server.
    """
    if status.state == "live":
        said = "[green]checked out to you[/green]"
    else:
        said = "[yellow]no longer checked out to you[/yellow]"
    line = f"{said} {status.record.org_path} -> {status.record.local_root}"
    if status.state == "live" and not status.running:
        line += " (nothing is syncing it right now)"
    if verbose:
        line += (
            f" [dim](epoch {status.record.epoch}, instance {status.record.instance_id}, "
            f"machine {status.record.machine}, pid {status.record.pid}, "
            f"held={status.held}, running={status.running}, expires_at={status.expires_at})[/dim]"
        )
    return line


def _refused(refusal: LeaseSupersededError) -> NoReturn:
    """The fence, reported the way it matters: who has the folder now."""
    if refusal.holder:
        said = (
            f"That folder is checked out by {refusal.holder}; wait for them to "
            "unmount or ask them to release it"
        )
    else:
        said = "That folder is checked out by someone else; ask them to release it"
    _console.print(f"[red]{said}[/red]")
    raise typer.Exit(code=1)
