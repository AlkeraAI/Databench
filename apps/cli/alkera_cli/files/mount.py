"""The mount chain: lease a folder, pull it, own it, hand it back.

``alkera files mount`` is not a filesystem — it is a lease plus a pull plus a
heartbeat. The lease is what makes the local directory the single writer for
the subtree: while it is held every other machine reads the last synced state
and is refused a write with this holder named, and every write this holder
makes carries the fencing headers (``X-Alkera-Lease-Epoch`` and
``X-Alkera-Lease-Instance``) so a holder the server has already superseded
cannot land bytes it thinks it still owns.

Three facts live on disk in ``ALKERA_HOME/files/mounts/<id>.json`` (0600),
because the process that took the lease is not the process that hands it back:
the instance id (so a resumed mount is the *same* holder, not a second one),
the epoch it writes under, and the local root the subtree was pulled into.
``unmount`` reads them, ``mounts`` lists them, and a mount whose process was
killed leaves them behind on purpose — that record is what lets the next
``mount`` on the same directory resume instead of forking a second holder,
and the lease's own expiry plus the server-side reaper are what reclaim it if
nobody ever comes back.

The record file is named by a digest of the local root: one directory is one
mount, so ``unmount <dir>`` is a single read rather than a scan, and mounting
the same directory twice can only ever find the mount already there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import stat
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import httpx
from alkera_core.process import process_alive
from alkera_core.versioning import VersionedModel
from alkera_sdk.client import AlkeraHTTPError, files_namespace
from pydantic import Field

from alkera_cli.files.progress import Progress
from alkera_cli.files.pull import KnownFile, LocalChanges, LocalChangesError, PullSummary, pull
from alkera_cli.files.push import FilesApi, PushSummary, conflict_code, push
from alkera_cli.files.walk import walk
from alkera_cli.host import paths

logger = logging.getLogger(__name__)

__all__ = [
    "LEASE_CONFLICT_CODES",
    "NO_ROOM",
    "AlreadyMountedError",
    "LeaseExpiredError",
    "LeaseSupersededError",
    "LocalChangesError",
    "MountRecord",
    "MountStatus",
    "NotMountedError",
    "SelfFence",
    "UnmountSummary",
    "export",
    "fenced_client",
    "fenced_files",
    "heartbeat",
    "hold",
    "load_record",
    "mount",
    "mount_record_path",
    "mounts",
    "out_of_room",
    "release",
    "unmount",
    "watch",
]

_API: Final = "/api/v1/files"
_EPOCH_HEADER: Final = "X-Alkera-Lease-Epoch"
_INSTANCE_HEADER: Final = "X-Alkera-Lease-Instance"
_BASE_HEADER: Final = "X-Alkera-Lease-Base"

#: How many missed beats end this holder's own permission to write. The server
#: serves a cadence with four beats inside one TTL, so two beats of silence is
#: the last moment a batch can still be landing under a lease we know is live.
SELF_FENCE_BEATS: Final = 2


class NotMountedError(RuntimeError):
    """This directory carries no mount record."""


class AlreadyMountedError(RuntimeError):
    """This directory already holds a mount of a *different* org folder.

    One directory carries exactly one record, so mounting a second folder onto
    it would ``os.replace`` the first record away — and with it the only copy
    of the epoch and instance id that can release the first lease. That folder
    would stay leased to a holder nobody on this machine can address until the
    TTL lapses, so the second mount is refused instead.
    """

    def __init__(self, message: str, *, org_path: str) -> None:
        super().__init__(message)
        self.org_path = org_path


class LeaseSupersededError(RuntimeError):
    """The lease this mount writes under is no longer ours.

    Carries ``holder`` when the server named one, because the whole point of
    the refusal is telling the user who has the folder now.
    """

    def __init__(self, message: str, *, holder: str | None = None) -> None:
        super().__init__(message)
        self.holder = holder


class LeaseExpiredError(LeaseSupersededError):
    """This holder stopped itself: too long since a beat landed.

    A subclass of the supersession, because from the caller's side it is the
    same fact — the folder may no longer be ours — reached by the only route a
    partitioned machine has: its own clock.
    """


@dataclass
class SelfFence:
    """The holder's own deadline, on a monotonic clock.

    The machine that stops hearing from the server is exactly the machine that
    cannot be *told* it lost the folder, so it has to stop itself: after
    :data:`SELF_FENCE_BEATS` beats of silence no further batch leaves here.
    Monotonic rather than wall clock, because a clock a user (or NTP) moves
    backwards would hand a superseded holder more time, not less.
    """

    grace: float
    monotonic: Callable[[], float] = time.monotonic
    _last_beat: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._last_beat = self.monotonic()

    @classmethod
    def for_record(
        cls, record: MountRecord, *, monotonic: Callable[[], float] = time.monotonic
    ) -> SelfFence:
        return cls(grace=record.heartbeat_every * SELF_FENCE_BEATS, monotonic=monotonic)

    def beat(self) -> None:
        """A heartbeat landed: the deadline moves."""
        self._last_beat = self.monotonic()

    @property
    def silent_for(self) -> float:
        return self.monotonic() - self._last_beat

    def expired(self) -> bool:
        return self.grace > 0 and self.silent_for >= self.grace


class MountRecord(VersionedModel):
    """One live mount, as the machine that took it remembers it.

    Persisted, so it is a :class:`VersionedModel`: a newer client's extra
    fields survive an older one's round-trip, and the reader that resumes a
    mount may well be a different build than the writer that took it.
    """

    SCHEMA_VERSION = "1.1.0"

    instance_id: str = ""
    """The lease instance. Reused on resume — the same holder, not a second."""

    drive_id: str = ""
    node_id: str = ""
    org_path: str = ""
    local_root: str = ""
    machine: str = ""
    purpose: str = "mount"
    epoch: int = 0
    pid: int = 0
    acquired_at: str = ""
    expires_at: str = ""
    heartbeat_every: float = 15.0
    sync_interval: float = 5.0

    live: dict[str, Any] = Field(default_factory=dict)
    """The cadence the grant served for streaming changes as they are written.

    Kept verbatim rather than parsed into fields because the server decides it
    and may serve a key this build has never heard of; a resumed mount that
    dropped an unknown one would stream under a cadence nobody chose. Empty on
    a lease taken without the live plane, which is every mount by default.
    """


@dataclass(frozen=True, slots=True)
class MountStatus:
    """A record plus what the *server* says about the lease behind it."""

    record: MountRecord
    held: bool
    """The server still lists this lease, at this epoch, as mine."""
    running: bool
    """The process that took the mount is still alive on this machine."""
    expires_at: str | None = None
    last_sync_at: str | None = None

    @property
    def lapsed(self) -> bool:
        """Whether the lease the *server* reported has already run out."""
        deadline = _instant(self.expires_at)
        return deadline is not None and deadline <= datetime.now(UTC)

    @property
    def state(self) -> str:
        """Whether the folder is still checked out to this machine.

        The lease is the only thing that decides it: the server's own epoch and
        its own expiry. A local process is not the question — ``mount
        --no-hold`` takes the folder and exits by design, and calling that
        mount stale seconds after it succeeded told the customer the exact
        opposite of the truth. Whether anything is beating for it is a separate
        fact the verbose listing carries.
        """
        return "live" if self.held and not self.lapsed else "stale"


def _instant(value: str | None) -> datetime | None:
    """One wire timestamp as an aware instant, or ``None`` if it is not one."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class UnmountSummary:
    """What handing the folder back did."""

    record: MountRecord
    push: PushSummary


# ---------------------------------------------------------------------------
# The record store
# ---------------------------------------------------------------------------


def mounts_dir(*, home: Path | None = None) -> Path:
    return (home if home is not None else paths.ALKERA_HOME) / "files" / "mounts"


def mount_record_path(root: Path, *, home: Path | None = None) -> Path:
    """Where this local directory's mount record lives.

    Keyed by a digest of the resolved root so the name carries no path bytes a
    filesystem could refuse, and so one directory can only ever hold one mount.
    """
    digest = hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()
    return mounts_dir(home=home) / f"{digest}.json"


def load_record(root: Path, *, home: Path | None = None) -> MountRecord | None:
    """The record for this directory, or ``None`` when it is not mounted."""
    return _read(mount_record_path(root, home=home))


def load_records(*, home: Path | None = None) -> list[MountRecord]:
    directory = mounts_dir(home=home)
    if not directory.is_dir():
        return []
    found = [_read(entry) for entry in sorted(directory.glob("*.json"))]
    return [record for record in found if record is not None]


def _read(path: Path) -> MountRecord | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return MountRecord.model_validate(raw)
    except ValueError:
        return None


def save_record(record: MountRecord, *, home: Path | None = None) -> Path:
    """Write the record 0600, atomically: a reader sees one state or the other.

    The mode is set on the descriptor rather than after the rename, so the
    bytes are never briefly world-readable — the record names a machine, a
    node and a live credential's worth of context about what this user holds.
    """
    path = mount_record_path(Path(record.local_root), home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def remove_record(root: Path, *, home: Path | None = None) -> None:
    mount_record_path(root, home=home).unlink(missing_ok=True)
    forget_tree_mark(root, home=home)


# ---------------------------------------------------------------------------
# The tree as the last batch that landed left it
# ---------------------------------------------------------------------------


def tree_mark_path(root: Path, *, home: Path | None = None) -> Path:
    """Where the fingerprint of the tree the last landed batch pushed lives:
    beside the mount record, under a suffix the record listing never reads."""
    return mount_record_path(root, home=home).with_suffix(".mark")


def tree_mark(root: Path, *, also: str = "") -> str:
    """A fingerprint of everything a batch could send from ``root``.

    Every entry the push's own walk would consider, by its path, kind, size,
    modified time, mode, link target and extended attributes — so a file
    written, touched, chmodded, renamed, added or removed moves the mark, and a
    tree nobody wrote does not. This machine's own hold on the tree (its lock
    and journal files) is left out: it changes whenever the folder is opened,
    and it is never what a batch exists to send. ``also`` is whatever else
    decides what the batch sends — the paths a holder must leave alone — so a
    change to it is a change to the tree as far as the batch is concerned.

    Size, write time and mode come from each entry's own metadata, not from
    the directory listing the walk read: Windows answers a listing from the
    folder's index, which catches up with a file only once every handle on it
    is closed, so a scanner still reading a file just written would move a
    listing-based mark between two batches over a tree nobody wrote.
    """
    digest = hashlib.blake2b(digest_size=32)
    salt = also.encode("utf-8", "surrogateescape")
    digest.update(len(salt).to_bytes(8, "big") + salt)
    rows: list[bytes] = []
    base = os.fsencode(Path(root))
    for entry in walk(Path(root), skip_local_state=True):
        if entry.skipped is not None:
            continue
        try:
            own = os.lstat(os.path.join(base, *entry.relative.split(b"/")))
        except OSError:
            # Gone since the walk listed it: the next walk will not list it
            # either, so it is no part of the tree the next mark compares.
            continue
        xattrs = b"".join(
            len(key).to_bytes(4, "big") + key + len(value).to_bytes(4, "big") + value
            for key, value in sorted(entry.xattrs.items())
        )
        rows.append(
            b"\0".join(
                (
                    entry.relative,
                    entry.kind.value.encode(),
                    str(own.st_size).encode(),
                    str(own.st_mtime_ns).encode(),
                    str(stat.S_IMODE(own.st_mode)).encode(),
                    entry.link_target or b"",
                    xattrs,
                )
            )
        )
    for row in sorted(rows):
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LandedTree:
    """What the last batch that landed left behind: the tree's mark, and the
    held folder's etag the snapshot was accepted under."""

    mark: str
    etag: str


def landed_tree(root: Path, *, home: Path | None = None) -> LandedTree | None:
    try:
        raw = json.loads(tree_mark_path(root, home=home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mark, etag = raw.get("mark"), raw.get("etag")
    if not isinstance(mark, str) or not mark or not isinstance(etag, str):
        return None
    return LandedTree(mark=mark, etag=etag)


def _remember_landed(root: Path, landed: LandedTree, *, home: Path | None = None) -> None:
    path = tree_mark_path(root, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".mark.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"mark": landed.mark, "etag": landed.etag}), encoding="utf-8")
    os.replace(temp, path)


def forget_tree_mark(root: Path, *, home: Path | None = None) -> None:
    tree_mark_path(root, home=home).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The lease routes the generated SDK does not model
# ---------------------------------------------------------------------------


def fence_headers(epoch: int, instance_id: str) -> dict[str, str]:
    """The headers every write from a mount carries.

    The epoch and the instance, and nothing else. A write from a mount is
    bounded by the drive's ceilings like anybody else's — including the last
    one before a release, which the drive may refuse for want of room; the
    bytes then stay on this disk and the record stays with them, which is what
    a person needs in order to make room and push again.
    """
    # This build's If-Match names the version its bytes were made on (a box
    # that no longer knows it fences on that version, not on the drive's head).
    return {_EPOCH_HEADER: str(epoch), _INSTANCE_HEADER: instance_id, _BASE_HEADER: "agreed"}


@contextmanager
def fenced(http: httpx.Client, record: MountRecord) -> Iterator[httpx.Client]:
    """Run a block with every request on ``http`` carrying the fence.

    The headers go on the client rather than being threaded through ``push``
    because "every write carries the epoch" has to be true of the *whole*
    push — the tree call, the upload session, the content PUT and the attrs
    PATCH — and a client-level header is the only shape that cannot forget one.
    """
    headers = fence_headers(record.epoch, record.instance_id)
    previous = {name: http.headers.get(name) for name in headers}
    http.headers.update(headers)
    try:
        yield http
    finally:
        for name, value in previous.items():
            if value is None:
                http.headers.pop(name, None)
            else:
                http.headers[name] = value


def fenced_client(http_factory: Callable[[], httpx.Client], record: MountRecord) -> httpx.Client:
    """A client of this holder's own, fenced for as long as it exists.

    :func:`fenced` puts the headers on a *shared* client for the length of one
    block, which is correct for a process holding one folder and wrong for one
    holding several: while a chat's push has the shared client fenced, every
    other chat's request on it is signed with that chat's epoch and instance.
    A holder that owns its client cannot do that to anybody — the pair is set
    once and stays, and the client the rest of the process uses is never
    touched.

    The transport is shared with the client the factory answers, so a folder's
    client costs no second connection pool. That is also why nothing here
    closes one: ``httpx.Client.close`` closes the transport, and a folder
    handing its lease back would take every other folder's connections with
    it. A released folder simply drops the reference.

    The bearer is not copied: each request carries the one the shared client
    carries at that moment. A lease outlives the credential it was taken
    under (an org worker's is replaced every few minutes), and a copy would
    sign every later request with one that has expired.
    """
    base = http_factory()
    transport = getattr(base, "_transport", None)
    headers = httpx.Headers(base.headers)
    headers.pop("Authorization", None)
    headers.update(fence_headers(record.epoch, record.instance_id))
    return httpx.Client(
        transport=transport if isinstance(transport, httpx.BaseTransport) else None,
        base_url=base.base_url,
        headers=headers,
        timeout=base.timeout,
        follow_redirects=base.follow_redirects,
        # A base client that signs each request (a box's) lends its signer; one
        # with a fixed bearer header lends the header, read at each request.
        auth=base.auth if base.auth is not None else _BearerOf(base),
    )


class _BearerOf(httpx.Auth):
    """The ``Authorization`` another client carries, read at each request."""

    def __init__(self, source: httpx.Client) -> None:
        self._source = source

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        bearer = self._source.headers.get("Authorization")
        if bearer is not None and "Authorization" not in request.headers:
            request.headers["Authorization"] = bearer
        yield request


@runtime_checkable
class _Rebindable(Protocol):
    """A Files namespace that can say which client it speaks on."""

    def on_client(self, http: httpx.Client) -> FilesApi: ...


def fenced_files(files: FilesApi, http: httpx.Client) -> FilesApi:
    """The Files namespace whose writes leave on ``http``.

    A push writes through two objects: the raw client (the tree call, the
    content PUT) and the SDK's Files namespace (the upload session). The fence
    is a header on a client, so a namespace still bound to the box's shared
    client opens its upload sessions unfenced and the server refuses them as
    somebody else's write — which is exactly what a folder on its own client
    (:func:`fenced_client`) would otherwise cause.

    A namespace that cannot be re-bound is handed back as it is: it has no
    client of its own to speak on, so there is nothing the fence could ride.
    """
    return files.on_client(http) if isinstance(files, _Rebindable) else files


def _object(response: httpx.Response) -> dict[str, Any]:
    parsed: Any = response.json() if response.content else {}
    return parsed if isinstance(parsed, dict) else {}


@contextmanager
def _fence_refusals() -> Iterator[None]:
    """Turn the SDK's 409 into the fence refusal the mount chain acts on.

    Every lease call goes through here, so "the folder is no longer ours" is
    one exception type whatever raised it — the namespace, or a fenced push
    deeper in the chain (:func:`superseded_from` handles that one).
    """
    try:
        yield
    except AlkeraHTTPError as exc:
        refusal = superseded_from(exc)
        if refusal is None:
            raise
        raise refusal from exc


def _conflict_message(response: httpx.Response) -> str:
    body = _object(response)
    error = body.get("error") if isinstance(body.get("error"), Mapping) else body
    message = error.get("message") if isinstance(error, Mapping) else None
    return str(message) if message else "the lease on this folder is no longer ours"


#: What a holder is called, best first. The lease facet may name the person
#: outright, or carry only the id the row is keyed by — a raw uuid is a last
#: resort, never the sentence a customer reads if the server said more.
_HOLDER_NAMES: Final = ("displayName", "display_name", "name", "fullName", "email")


def _person(holder: Any) -> str | None:
    """A holder rendered the way a person would introduce themselves."""
    if isinstance(holder, Mapping):
        for key in _HOLDER_NAMES:
            named = holder.get(key)
            if named:
                return str(named)
        identifier = holder.get("id") or holder.get("userId")
        return str(identifier) if identifier else None
    return str(holder) if holder else None


def _holder_in(layers: Sequence[Any]) -> str | None:
    """Who the server says holds the folder, when it is allowed to say."""
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        holder = layer.get("holder") or layer.get("holderName")
        machine = layer.get("machine")
        if holder is None:
            nested = layer.get("details")
            if isinstance(nested, Mapping):
                holder, machine = nested.get("holder"), nested.get("machine")
        named = _person(holder)
        if named is not None:
            return f"{named} on {machine}" if machine else named
    return None


def _holder_of(response: httpx.Response) -> str | None:
    body = _object(response)
    return _holder_in([body, body.get("error"), body.get("detail")])


#: The two refusals that say the folder is not (or no longer) this holder's.
#: Every other 409 a push can meet — a taken name, a subtree mid-move, a parent
#: in the trash — is about the write, not the lease, and reading it as "the
#: folder was taken from us" made a box abandon its chat's work over a name.
LEASE_CONFLICT_CODES: Final = frozenset({"files.leased", "files.lease_fenced"})


def _machine_named(exc: BaseException, response: httpx.Response | None) -> str | None:
    """The machine a ``files.leased`` refusal names as the holder's."""
    layers: list[Any] = [getattr(exc, "detail", None), getattr(exc, "details", None)]
    if response is not None:
        body = _object(response)
        layers = [body, body.get("error"), body.get("detail"), *layers]
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        machine = layer.get("machine")
        if machine is None and isinstance(layer.get("details"), Mapping):
            machine = layer["details"].get("machine")
        if machine:
            return str(machine)
    return None


def superseded_from(
    exc: BaseException, *, record: MountRecord | None = None
) -> LeaseSupersededError | None:
    """The fence refusal behind an exception a push raised, if that is what it is.

    ``push`` speaks through ``raise_for_status`` and the SDK's own error, so
    the 409 the fence produces arrives wrapped. Translating it here is what
    lets ``unmount`` stop *without releasing* — a holder that has been
    superseded must not hand back a lease it no longer owns.

    Only the lease's own two codes are a supersession. ``files.lease_fenced``
    is the server proving this epoch and instance no longer hold the folder;
    ``files.leased`` is somebody holding it against a write that carried no
    fence — unless the holder it names is ``record``'s own machine, which is
    this holder meeting its own lease and not a change of hands. A 409 with
    any other code is ``None``: the write was refused, the lease was not.
    """
    if isinstance(exc, LeaseSupersededError):
        return exc
    response = getattr(exc, "response", None)
    if not isinstance(response, httpx.Response):
        response = None
    status = response.status_code if response is not None else getattr(exc, "status", None)
    if status != 409:
        return None
    code = conflict_code(exc)
    if code not in LEASE_CONFLICT_CODES:
        return None
    if (
        code == "files.leased"
        and record is not None
        and record.machine
        and _machine_named(exc, response) == record.machine
    ):
        return None
    if response is not None:
        return LeaseSupersededError(_conflict_message(response), holder=_holder_of(response))
    message = getattr(exc, "message", "") or str(exc)
    holder = _holder_in([getattr(exc, "detail", None), getattr(exc, "details", None)])
    return LeaseSupersededError(message, holder=holder)


# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------


def machine_id() -> str:
    """This machine, as the lease badge names it."""
    return socket.gethostname() or "unknown"


def mount(
    *,
    files: FilesApi,
    http: httpx.Client,
    source: str,
    root: Path,
    node_id: str | None = None,
    drive_id: str | None = None,
    machine: str | None = None,
    ttl: int | None = None,
    home: Path | None = None,
    purpose: str = "mount",
    instance: str | None = None,
    inbound: bool = False,
    live: bool = False,
    pull_tree: Callable[..., PullSummary] = pull,
    progress: Progress | None = None,
    on_lapsed: LocalChanges = "refuse",
    known: Mapping[bytes, KnownFile] | None = None,
) -> tuple[MountRecord, PullSummary]:
    """Lease ``source``, pull it into ``root``, and record the mount.

    A record already on this directory for the *same* node is a *resume*, never
    a second holder: the same instance id is sent again, so a lease the server
    still holds is handed straight back at its epoch, and one that lapsed (the
    holder was killed, the TTL ran out, the reaper took it) is re-acquired
    under the same identity. Either way the caller ends up owning the folder
    exactly once.

    A record for a *different* node is refused with
    :class:`AlreadyMountedError` rather than overwritten — see that class for
    why the first lease could otherwise never be released.

    ``purpose`` is why this holder wants the folder — the label the server
    records and a person is shown when they are refused. ``instance`` names the
    holder explicitly, for a caller (a box serving one chat) whose identity
    outlives the record on disk; omitted, a resume reuses the recorded one and
    a first mount mints one.

    ``node_id`` names the folder outright and wins over ``source``, for a caller
    that was handed the id (a chat knows the node it IS). A path is only a name:
    the node under it is whatever is filed there at this instant, so resolving
    one when the id is already known would lease and pull a stranger's folder
    the moment somebody renamed or replaced what sits at that name. It is passed
    on to the pull, so the folder that comes down is the one the lease was taken
    on; a caller that named no id leaves both hops resolving the same path, as
    they always did.

    ``drive_id`` names the drive the folder is on, for a caller that was handed
    it the same way — a chat's record names its drive beside its node. Without
    it the drive is asked for as the caller's own, which is right for a person
    and for an org box on its operator's session, and names nothing for a box
    on its own machine credential: it serves chats in orgs it is no member of,
    and the server answers that such a caller has no drive.

    ``inbound`` asks the server to admit other people's writes into the
    subtree while this holder has it, and ``live`` asks for the cadence that
    lets the holder stream what it writes as it writes it. Both are off by
    default: a plain mount is still the single writer it always was, and a
    holder that did not ask to stream is not handed a cadence it will not use.

    ``on_lapsed`` is what the pull does with a local file whose bytes are not
    the server's when this holder's lease had lapsed between the record and
    the grant (see :func:`_local_changes_policy`): ``refuse`` for a mount a
    person will sort out, ``overwrite`` for a caller that has decided the
    server's copy is the truth once the lease has changed hands.

    ``known`` is what an earlier holder of ``root`` knew about the node each
    file is, handed to the pull so a file the drive renamed or trashed while
    nothing held the folder is recognised by its node rather than its name.
    """
    if drive_id is None:
        drive_id = str(files.drive()["id"])
    wanted = node_id
    item = (
        files.item(drive_id, wanted) if wanted else files.item_by_path(drive_id, source.strip("/"))
    )
    node_id = str(item["id"])

    existing = load_record(root, home=home)
    if existing is not None and existing.node_id != node_id:
        raise AlreadyMountedError(
            f"{Path(root).resolve()} already holds a mount of {existing.org_path!r}; "
            f"run `alkera files unmount {root}` before mounting {source.strip('/')!r} here",
            org_path=existing.org_path,
        )
    instance_id = instance or (existing.instance_id if existing is not None else uuid.uuid4().hex)
    with _fence_refusals():
        grant = files_namespace(http).acquire_lease(
            drive_id,
            node_id,
            instance_id=instance_id,
            machine_id=machine or machine_id(),
            if_match=str(item.get("etag", "")),
            purpose=purpose,
            ttl=ttl,
            inbound=inbound,
            live=live,
        )

    record = MountRecord(
        instance_id=instance_id,
        drive_id=drive_id,
        node_id=node_id,
        org_path=source.strip("/"),
        local_root=str(Path(root).resolve()),
        machine=machine or machine_id(),
        purpose=purpose,
        epoch=int(grant["epoch"]),
        pid=os.getpid(),
        acquired_at=datetime.now(UTC).isoformat(),
        expires_at=str(grant.get("expiresAt", "")),
        heartbeat_every=float(grant.get("heartbeatEvery", 15.0)),
        sync_interval=float(grant.get("syncInterval", 5.0)),
        live=_cadence(grant),
    )
    save_record(record, home=home)
    # A new grant starts with no landed batch: what the pull leaves on disk is
    # sent once by the first batch, whatever an earlier life of this mount sent.
    forget_tree_mark(Path(root), home=home)

    # The pull runs as the holder: every read carries the epoch and instance
    # just granted, exactly as every write will. A folder whose bytes may not
    # leave the platform (a chat's) is served to the box that holds its lease
    # and to nobody else, and the server tells the holder from a stranger by
    # this fence — so a pull that read unfenced would be told, on the folder
    # it was just granted, that there is nothing it may download.
    #
    # The NAMESPACE reads are under it too, and that is load-bearing rather
    # than incidental: what a pull may copy is decided by the `canDownload`
    # the item and children reads answer, and a read outside the fence answers
    # as a stranger — the walk would prune the whole subtree before a byte was
    # asked for and a chat resumed on a second box would open on an empty
    # working directory. The namespace speaks on this very client, so setting
    # the headers here covers both halves; a caller that hands in a namespace
    # bound to some other client has to fence that one itself.
    try:
        with fenced(http, record) as fenced_http:
            summary = pull_tree(
                files=files,
                http=fenced_http,
                root=Path(root),
                source=source,
                node_id=wanted,
                drive_id=drive_id,
                local_changes=_local_changes_policy(
                    existing, record, on_lapsed=on_lapsed, inbound=inbound, known=known
                ),
                progress=progress,
                **({"known": known} if known is not None else {}),
            )
    except LocalChangesError:
        # Refused, not failed: the lease stays so the unmount that settles
        # the named files can push them under it.
        raise
    except Exception:
        _give_back_failed_take(files, http, record, existing, root=Path(root), home=home)
        raise
    return record, summary


def _give_back_failed_take(
    files: FilesApi,
    http: httpx.Client,
    record: MountRecord,
    existing: MountRecord | None,
    *,
    root: Path,
    home: Path | None,
) -> None:
    """The lease was granted and the pull under it failed: nobody holds the
    folder through this take, so the lease goes back now instead of keeping
    every other holder out until its TTL ends. Nothing is pushed, and the
    record on disk is put back as the take found it. Best-effort: a release
    that does not land lapses as it would have."""
    try:
        item = _held_item(files, record)
        with _fence_refusals():
            files_namespace(http).release_lease(
                record.drive_id,
                record.node_id,
                epoch=record.epoch,
                instance_id=record.instance_id,
                if_match=str(item.get("etag", "")),
                final=None,
            )
    except Exception as failed:
        logger.info("%s: the lease of a failed take was not released (%s)", root, failed)
    if existing is None:
        remove_record(root, home=home)
    else:
        save_record(existing, home=home)


def _cadence(grant: Mapping[str, Any]) -> dict[str, Any]:
    """The live block a grant served, as the record keeps it.

    A grant from a server that does not serve the live plane carries none, and
    one that carries something other than a block is not a cadence — either way
    the record says "no live plane here" rather than storing a shape the holder
    would later have to guess at.
    """
    served = grant.get("live")
    return dict(served) if isinstance(served, Mapping) else {}


def _local_changes_policy(
    existing: MountRecord | None,
    granted: MountRecord,
    *,
    on_lapsed: LocalChanges = "refuse",
    inbound: bool = False,
    known: Mapping[bytes, KnownFile] | None = None,
) -> LocalChanges:
    """What the mount's pull may do to a local file the org copy does not have.

    A first mount materializes into a directory the org tree has never been in,
    so ``overwrite`` is the only meaning available. A directory an earlier
    holder left a node map in (``known``) is not one, even with no record: the
    lease was let go with the tree kept, and what is on disk may be work that
    never reached the drive, so it is taken back as a lapsed lease is. A resume is different: while
    this machine held the lease it was the *only* writer, so a file whose bytes
    are not the server's holds an edit nobody has saved yet, and the pull must
    leave it exactly where it is — that is ``keep``.

    Unless the lease takes ``inbound`` writes: then this machine was never the
    only writer. A person's save or a version restore landed on the drive while
    it held the folder, so a file still holding exactly the bytes the two last
    agreed is behind the drive, not edited, and takes the drive's newer bytes
    (``keep_edits``); only a file that moved away from its agreed bytes is kept.

    The third case is the resume whose epoch moved: the lease lapsed and was
    handed back, so somebody else may have written the folder in between and
    neither copy can be called the stale one. Nothing is guessed there — the
    mount refuses and names the files, and the way out is an unmount that
    pushes them up first.
    """
    if existing is None:
        return on_lapsed if known else "overwrite"
    if granted.epoch != existing.epoch:
        return on_lapsed
    return "keep_edits" if inbound else "keep"


def heartbeat(
    *, http: httpx.Client, record: MountRecord, home: Path | None = None, persist: bool = True
) -> MountRecord:
    """One beat. Raises :class:`LeaseSupersededError` when the lease is gone.

    ``persist=False`` leaves the record on disk alone: a beat running beside
    another writer of the record (a hand-back refiling a moved folder) must
    not put back the copy it was handed."""
    with _fence_refusals():
        grant = files_namespace(http).heartbeat_lease(
            record.drive_id, record.node_id, epoch=record.epoch, instance_id=record.instance_id
        )
    beaten = record.model_copy(
        update={
            "epoch": int(grant.get("epoch", record.epoch)),
            "expires_at": str(grant.get("expiresAt", record.expires_at)),
        }
    )
    if persist:
        save_record(beaten, home=home)
    return beaten


def hold(
    *,
    http: httpx.Client,
    record: MountRecord,
    stop: Callable[[], bool],
    home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MountRecord:
    """Beat until ``stop()`` says to stop, or the lease is taken away.

    The beat with nothing exported: the lease stays alive, but the cloud copy
    does not move until ``unmount``. :func:`watch` is the same loop with an
    exporter on it.
    """
    return watch(files=None, http=http, record=record, stop=stop, home=home, sleep=sleep)


class _Batches:
    """The mount's exports, one at a time, off the thread that beats.

    A beat is one request and a batch is a whole tree, so the loop that ran
    them in turn could not beat again until the batch it was waiting for had
    finished — and on a tree of any real size that is long after the lease has
    lapsed. Here the loop hands the batch over and goes on beating; what a
    batch raised is raised at the caller on the loop's next beat, where it can
    act on it, and on the way out if the loop has already stopped.

    One at a time still: a batch that overruns its cadence delays the next one
    rather than racing it, so nothing ever has two pushes of the same tree on
    the wire.
    """

    def __init__(self) -> None:
        self._worker: threading.Thread | None = None
        self._failure: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self, **batch: Any) -> None:
        def run() -> None:
            try:
                export(**batch)
            except Exception as failed:
                self._failure = failed

        self._worker = threading.Thread(target=run, name="alkera-mount-export", daemon=True)
        self._worker.start()

    def join(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join()

    def raise_any(self) -> None:
        """Whatever the last batch raised, raised here — once."""
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure


def watch(
    *,
    files: FilesApi | None,
    http: httpx.Client,
    record: MountRecord,
    stop: Callable[[], bool],
    home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    push_tree: Callable[..., PushSummary] = push,
) -> MountRecord:
    """Hold the folder and keep the cloud copy behind it by a sync interval.

    The lease is kept alive independently of the push. A batch is a whole-tree
    export — an item lookup and an upload per file, the tree re-walked each
    cycle — so a batch and a beat are nothing like the same length, and a beat
    sequenced behind one lands after the lease it was keeping alive has already
    lapsed: a tree of any real size cannot finish inside the TTL, and the
    snapshot the batch ends with is then refused at the fence it lost. So the
    batch runs beside the loop and the loop goes on beating on the served
    cadence, whatever the batch is doing.

    What keeps the exporter honest is the fence rather than the sequencing:
    every batch checks it before anything leaves the machine, and it closes
    after :data:`SELF_FENCE_BEATS` beats of silence — so a mount that is "alive
    but behind" still stops writing, which is what the server's ``stale`` flag
    exposes on the other side. Still one batch at a time: a batch that overruns
    its cadence delays the next one rather than racing it.

    ``files=None`` is the beat-only mount. ``sleep``, ``monotonic`` and
    ``stop`` are injected so a test drives the whole thing without waiting on
    a clock, and so the CLI's signal handler is the only thing that ends it.
    """
    exporting = files is not None
    tick = (
        min(record.heartbeat_every, record.sync_interval) if exporting else record.heartbeat_every
    )
    fence = SelfFence.for_record(record, monotonic=monotonic)
    due = monotonic() + record.sync_interval
    batches = _Batches()
    try:
        while not stop():
            sleep(tick)
            if stop():
                break
            record = heartbeat(http=http, record=record, home=home)
            fence.beat()
            batches.raise_any()
            if files is not None and monotonic() >= due and not batches.running:
                batches.start(
                    files=files,
                    http=http,
                    record=record,
                    fence=fence,
                    push_tree=push_tree,
                    home=home,
                )
                due = monotonic() + record.sync_interval
    finally:
        batches.join()
    batches.raise_any()
    return record


def _batch(summary: PushSummary) -> list[dict[str, Any]]:
    """One exported batch, as the snapshot route records it.

    A count rather than a path list: the route's contract is that the batch
    ran under the holder's epoch and moved ``last_sync_at``, and a per-path
    manifest of a 100k-file tree would be a megabyte of body for a column the
    server never reads back.
    """
    return [
        {
            "uploaded": summary.uploaded,
            "unchanged": summary.unchanged,
            "folders": summary.folders,
            "symlinks": summary.symlinks,
            "specials": summary.specials,
            "bytesUploaded": summary.bytes_uploaded,
        }
    ]


def _push_fenced(
    *,
    files: FilesApi,
    http: httpx.Client,
    record: MountRecord,
    push_tree: Callable[..., PushSummary],
) -> PushSummary:
    """Push this mount's tree with every request carrying the epoch.

    The record names the node this holder leases, so the push starts its
    skeleton there rather than walking down from the drive root — the root is
    a signpost, and a member's push anchored on it was refused before the
    folder they hold was reached.

    A 409 anywhere in the push is the fence, and it arrives wrapped in whatever
    the push layer raised — translating it here is what lets both callers stop
    *without* releasing a lease they no longer own.

    Every push is bounded by the drive's ceilings, the last one included: a
    mount that could write past them on the way out would be a way past them
    at any time, since nothing but this process decides which push is the last.
    """
    try:
        with fenced(http, record) as fenced_http:
            return push_tree(
                files=files,
                http=fenced_http,
                root=Path(record.local_root),
                dest=record.org_path,
                node_id=record.node_id or None,
                # The lease names the drive too: a box on its machine
                # credential has no drive of its own for the push to ask for.
                drive_id=record.drive_id or None,
            )
    except Exception as exc:
        refusal = superseded_from(exc, record=record)
        if refusal is not None:
            raise refusal from exc
        no_room = out_of_room(exc)
        if no_room is not None:
            raise no_room from exc
        raise


#: What the drive answers a write it has no room for. It is a ceiling and not
#: a failure of the write: the bytes are still here and the lease is still
#: ours, so a hand-back that meets it has something to say rather than
#: something to crash over.
NO_ROOM: Final = 507


def out_of_room(exc: BaseException) -> AlkeraHTTPError | None:
    """The drive's "there is no room" refusal, in the form a caller prints.

    The push speaks through ``raise_for_status``, so a full drive arrives as a
    raw ``httpx`` error that nothing downstream reads — and a hand-back over a
    full drive reached a person as a traceback, about a mount whose bytes and
    whose record are both still exactly where they were. Handed on as the SDK's
    own refusal, it is carried by the same one sentence as every other ceiling,
    and the mount is left for the retry that follows making room.
    """
    if isinstance(exc, AlkeraHTTPError) or _refused_status(exc) != NO_ROOM:
        return None
    return AlkeraHTTPError(
        label="files push",
        status=NO_ROOM,
        code=conflict_code(exc) or "files.quota",
        message=str(exc),
        trace_id=None,
    )


def _refused_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    direct = getattr(exc, "status", None)
    return direct if isinstance(direct, int) else None


def _held_item(files: FilesApi, record: MountRecord) -> dict[str, Any]:
    """The node this mount holds, read by the id the lease was granted on.

    Never by the path: the path is only what the node is called right now, and
    a rename, a move, or a name carrying a character a URL path cannot hold
    (`?`, `#`) all make the path form answer about a different node or about
    none. The id is what the lease is on, so it is what the precondition on
    the snapshot and the release is read from. A record written before the id
    was kept falls back to the path, which is all it has.
    """
    if record.node_id:
        return files.item(record.drive_id, record.node_id)
    return files.item_by_path(record.drive_id, record.org_path)


def export(
    *,
    files: FilesApi,
    http: httpx.Client,
    record: MountRecord,
    fence: SelfFence | None = None,
    push_tree: Callable[..., PushSummary] = push,
    home: Path | None = None,
    also: str = "",
) -> PushSummary:
    """One fenced batch: push what changed, then announce it under the epoch.

    A tree that has not moved since the last batch that landed is not pushed:
    no drive read, no item lookup per file — only the snapshot that keeps the
    lease current, under the etag the last one was accepted at. A folder held
    idle for hours was otherwise looked up on the drive file by file on every
    cadence to learn that nothing had changed. The mark is taken before the
    push reads the tree, so a write that lands while the batch runs moves the
    tree past the mark and is sent by the next one. ``also`` is folded into
    the mark (see :func:`tree_mark`).

    This is what makes the cloud copy trail the local one by a sync interval
    rather than by a whole unmount. ``fence`` is checked *before* anything
    leaves the machine: a holder whose beats have stopped landing must not
    write, and it is the only party in a position to know that.
    """
    if fence is not None and fence.expired():
        raise LeaseExpiredError(
            f"no heartbeat has landed for {fence.silent_for:.0f}s "
            f"(the lease allows {fence.grace:.0f}s); this mount has stopped exporting "
            "rather than write under a lease it may already have lost"
        )
    root = Path(record.local_root)
    mark = tree_mark(root, also=also)
    landed = landed_tree(root, home=home)
    if landed is not None and landed.mark == mark:
        # Still announced, so the lease reads as current: the snapshot is what
        # moves ``last_sync_at``. Under the etag it was last accepted at, so
        # nothing is read — and a folder somebody changed since refuses the
        # precondition, which sends the whole batch below as before.
        summary = PushSummary()
        try:
            with _fence_refusals():
                files_namespace(http).push_snapshot(
                    record.drive_id,
                    record.node_id,
                    epoch=record.epoch,
                    instance_id=record.instance_id,
                    changes=_batch(summary),
                    if_match=landed.etag,
                )
        except AlkeraHTTPError as refused:
            if refused.status != PRECONDITION_FAILED:
                raise
        else:
            return summary
    summary = _push_fenced(files=files, http=http, record=record, push_tree=push_tree)
    item = _held_item(files, record)
    etag = str(item.get("etag", ""))
    with _fence_refusals():
        files_namespace(http).push_snapshot(
            record.drive_id,
            record.node_id,
            epoch=record.epoch,
            instance_id=record.instance_id,
            changes=_batch(summary),
            if_match=etag,
        )
    _remember_landed(root, LandedTree(mark=mark, etag=etag), home=home)
    return summary


#: What the snapshot route answers an ``If-Match`` the held folder has moved past.
PRECONDITION_FAILED: Final = 412


def unmount(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    home: Path | None = None,
    push_tree: Callable[..., PushSummary] = push,
    unsynced: Sequence[str] | None = None,
    before_release: Callable[[], None] | None = None,
    ending: str | None = None,
) -> UnmountSummary:
    """Push everything under ``root`` back, then release, then forget the mount.

    ``before_release`` runs after the push and immediately before the release
    is sent: the one point a caller keeping the lease alive beside the push
    must have stopped by, and must not stop before.

    The push runs fenced: a superseded epoch is refused by the server and this
    raises :class:`LeaseSupersededError` **without releasing and without removing
    the record**, because a holder that lost the folder has nothing to hand
    back and the record is what a human needs to see what happened. A push the
    drive refuses for want of room ends the same way — the bytes are still
    here, under a record that still names the lease.

    ``unsynced`` names the files the holder could not land before it let go
    (:func:`unsynced_fields`); None says nothing about them. A file the final
    push itself refused (``PushSummary.failed``) is added to whatever the
    caller named: the release is where the drive learns what stayed behind,
    and a hand-back that lost one file must not read as a clean one.

    ``ending`` is set by a box putting a chat to sleep: the release is then the
    chat-end transition itself, and the chat reads asleep the instant the lease
    is gone.
    """
    record = load_record(root, home=home)
    if record is None:
        raise NotMountedError(f"{root} is not mounted")

    summary = _push_fenced(
        files=files,
        http=http,
        record=record.model_copy(update={"local_root": str(Path(root))}),
        push_tree=push_tree,
    )
    left_behind = list(getattr(summary, "failed", ()) or ())
    if left_behind:
        unsynced = [*(unsynced or ()), *left_behind]

    item = _held_item(files, record)
    if before_release is not None:
        before_release()
    with _fence_refusals():
        files_namespace(http).release_lease(
            record.drive_id,
            record.node_id,
            epoch=record.epoch,
            instance_id=record.instance_id,
            if_match=str(item.get("etag", "")),
            final=_batch(summary),
            **unsynced_fields(unsynced),
            ending=ending,
        )
    remove_record(root, home=home)
    return UnmountSummary(record=record, push=summary)


def release(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    home: Path | None = None,
    unsynced: Sequence[str] | None = None,
) -> MountRecord:
    """Hand the lease under ``root`` back and forget the mount, pushing NOTHING.

    The verb for a holder that never wrote: the folder was taken and pulled,
    and what is on the server is still the truth — so the release carries no
    final batch (the server's ``last_sync_at`` does not move) and nothing on
    this disk goes up, not even what an earlier life left behind. Raises
    :class:`NotMountedError` when there is no record, and
    :class:`LeaseSupersededError` — record kept — when the lease is no longer
    this holder's to give back.
    """
    record = load_record(root, home=home)
    if record is None:
        raise NotMountedError(f"{root} is not mounted")
    item = _held_item(files, record)
    with _fence_refusals():
        files_namespace(http).release_lease(
            record.drive_id,
            record.node_id,
            epoch=record.epoch,
            instance_id=record.instance_id,
            if_match=str(item.get("etag", "")),
            final=None,
            **unsynced_fields(unsynced),
        )
    remove_record(root, home=home)
    return record


#: The most paths a release names as left on the machine; the count is exact.
UNSYNCED_PATHS_REPORTED: Final = 200


def unsynced_fields(unsynced: Sequence[str] | None) -> dict[str, Any]:
    """The release body's account of what did not land: the exact count and
    the first :data:`UNSYNCED_PATHS_REPORTED` paths. Nothing when ``None``."""
    if unsynced is None:
        return {}
    return {
        "unsynced_count": len(unsynced),
        "unsynced_paths": list(unsynced[:UNSYNCED_PATHS_REPORTED]),
    }


def _my_leases(leases: Any, drive_id: str) -> list[dict[str, Any]]:
    """This caller's leases on ``drive_id``, or none the server would name.

    A listing the server refuses is not a reason to hide the mount records:
    every one of them then reads ``stale``, which is the honest answer when we
    cannot ask whether the lease is still ours.
    """
    try:
        rows: list[dict[str, Any]] = leases.my_leases(drive_id)
    except AlkeraHTTPError:
        return []
    return rows


def mounts(*, files: FilesApi, http: httpx.Client, home: Path | None = None) -> list[MountStatus]:
    """Every mount on this machine, with the lease state the server reports.

    A record whose lease the server no longer lists, or whose process is gone,
    is ``stale``: the first is a lease that lapsed or was taken back, the
    second is the SIGKILLed holder whose record is the only thing left of it.
    """
    records = load_records(home=home)
    if not records:
        return []
    leases = files_namespace(http)
    by_drive: dict[str, dict[str, dict[str, Any]]] = {}
    statuses: list[MountStatus] = []
    for record in records:
        if record.drive_id not in by_drive:
            by_drive[record.drive_id] = {
                str(row.get("nodeId")): row for row in _my_leases(leases, record.drive_id)
            }
        row = by_drive[record.drive_id].get(record.node_id)
        held = row is not None and int(row.get("epoch", -1)) == record.epoch
        statuses.append(
            MountStatus(
                record=record,
                held=held,
                running=record.pid > 0 and process_alive(record.pid),
                expires_at=str(row.get("expiresAt")) if row else None,
                last_sync_at=str(row.get("lastSyncAt")) if row and row.get("lastSyncAt") else None,
            )
        )
    return statuses
