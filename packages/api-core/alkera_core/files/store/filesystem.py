"""The filesystem driver: sharded directories, temp + fsync + link.

The directory *is* the index — there is no side table to fall out of sync
with. Every object write goes through the one local-disk primitive,
:class:`~alkera_core.files.sync.atomic.AtomicWriter`: the bytes land in a
same-directory temp file, are fsynced and verified against the caller's
checksum, and only then are published under the final key; ``if_absent`` is
the ``link`` publish failing with ``EEXIST``. Every path goes through
:func:`resolve_beneath`, so a hostile key raises before any I/O.

The driver takes a :class:`~alkera_core.files.checkpoints.Checkpoints` seam so
the crash harness can SIGKILL it between any two of those steps. Only kill
points are usable here: the write path is synchronous below the stream, and
``reach_sync`` refuses to park a coroutine — a pause point inside the driver
would have to live on an ``await``, which the byte path does not have.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, NoReturn

from blake3 import blake3

from alkera_core.db.locking import io_boundary_class
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.store._beneath import resolve_beneath
from alkera_core.files.store.errors import (
    ChecksumMismatch,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
)
from alkera_core.files.store.keys import (
    DOMAIN_PREFIX,
    KeyLayout,
    split_absolute_key,
    validate_key,
)
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
    resolve_whole_object_checksum,
)
from alkera_core.files.sync.atomic import (
    AtomicWriter,
    PublishMode,
    checkpoints_for,
    default_fsync_dir,
)

CHUNK_BYTES: Final = 1 << 20
"""Streaming granularity. A whole object is never held in memory."""

PARTS_ROOT: Final = ".parts"
"""Where an open multipart session stages its parts, under its own upload id.

It is a dot-leading segment, which ``keys.validate_relative_key`` refuses, so
staged parts can never collide with an object a caller asked for and two
sessions on one key never share a directory."""

TEMP_SUFFIX: Final = ".tmp"

KEY_MARKER: Final = "key"
"""The file an open multipart session writes its object key into.

The staging directory is named for the upload id alone — that is what lets
two sessions on one key run side by side — so the key it is assembling is
recorded beside the parts. It is not a digit, so ``multipart_complete``'s
part scan steps over it."""

INCOMING_PREFIX: Final = "incoming/"

BEFORE_MOVE_PUBLISH: Final = "store.before_move_publish"
"""Reached inside `move`, after the source is known and before either key changes.

A test parked here sees the store exactly between the two effects of a move; a
driver whose move is not atomic shows the gap it leaves.
"""

AFTER_TEMP_WRITE: Final = "store.after_temp_write"
"""Every byte is in the temp file; nothing is fsynced and nothing is published."""

AFTER_FSYNC: Final = "store.after_fsync"
"""The temp file is durable; the final key does not exist yet."""

AFTER_LINK: Final = "store.after_link"
"""The final key now names the bytes; the parent directory is not yet fsynced."""

#: The driver's own kill points, in the order a write reaches them. The writer's
#: ``atomic.*`` points fire as well, so a harness can kill at either name.
STORE_CHECKPOINTS: Final = (AFTER_TEMP_WRITE, AFTER_FSYNC, AFTER_LINK)

# The driver names the commit step once for both publish modes: what matters to
# a caller is that the key became visible, not which syscall made it visible.
_DRIVER_ALIAS: Final = {
    "atomic.after_fsync_file": AFTER_FSYNC,
    "atomic.after_rename": AFTER_LINK,
    "atomic.after_link": AFTER_LINK,
}


def kill_points(mode: PublishMode = "link") -> tuple[str, ...]:
    """Every checkpoint one object write reaches, in order, driver and writer alike.

    A crash harness parametrizes over this so a new step in either the driver or
    the writer is killed at without editing the test.
    """
    names: list[str] = [AFTER_TEMP_WRITE]
    for name in checkpoints_for(mode):
        names.append(name)
        alias = _DRIVER_ALIAS.get(name)
        if alias is not None:
            names.append(alias)
    return tuple(names)


#: The production checkpoints object: every checkpoint is free. A module-level
#: singleton so the default is not a call in an argument default.
NO_CHECKPOINTS: Final[Checkpoints] = NoopCheckpoints()

Clock = Callable[[], datetime]

FILESYSTEM_CAPABILITIES: Final = StoreCapabilities(
    conditional_write=True,
    presigned=False,
    range_signing=False,
    versioning=False,
    object_lock=False,
    lifecycle=False,
    scoped_credentials=False,
    storage_classes=False,
    strong_read_after_write=True,
    kms=False,
    max_object_bytes=1 << 43,
    min_part_bytes=1 << 20,
    max_part_bytes=1 << 33,
    max_parts=10_000,
    atomic_move=True,
)


@io_boundary_class("object store, filesystem")
class FilesystemStore:
    """An :class:`~alkera_core.files.store.protocol.ObjectStore` over a directory tree."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock,
        layout: KeyLayout = "domain",
        capabilities: StoreCapabilities = FILESYSTEM_CAPABILITIES,
        checkpoints: Checkpoints = NO_CHECKPOINTS,
    ) -> None:
        self.layout = layout
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._root_real = Path(os.path.realpath(self._root))
        self._clock = clock
        self.capabilities = capabilities
        self._checkpoints = checkpoints
        writer_hook = checkpoints.as_hook()

        def hook(name: str) -> None:
            writer_hook(name)
            alias = _DRIVER_ALIAS.get(name)
            if alias is not None:
                writer_hook(alias)

        self._hook = hook

    # -- paths -----------------------------------------------------------

    def _path(self, key: str) -> Path:
        validate_key(key, self.layout)
        if self.layout == "bucket":
            # The admin handle is rooted above every domain directory, so it
            # resolves the *relative* half beneath the domain's own root — the
            # containment guarantee is the same one a domain handle gets, and
            # the prefix is a validated uuid, not a caller's bytes.
            prefix, relative = split_absolute_key(key)
            return resolve_beneath(self._root / prefix, relative)
        return resolve_beneath(self._root, key)

    def _staging(self, handle: UploadHandle) -> Path:
        """Where ``handle``'s parts live: its own directory, not the key's.

        Keying the staging area by the upload id is what lets two sessions
        open on the same key run side by side without either seeing the
        other's parts.
        """
        validate_key(handle.key, self.layout)
        if not handle.upload_id.isalnum():
            raise InvalidRequest(f"upload id {handle.upload_id!r} is not a plain token")
        # Built from two validated tokens rather than resolved as a key: the
        # dot namespace is exactly what the key validator refuses, which is
        # what makes it unreachable by a caller.
        return self._root / PARTS_ROOT / handle.upload_id

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root_real).as_posix()

    # -- writes ----------------------------------------------------------

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: Literal["hot", "cold"] | None = None,
    ) -> PutResult:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # ``if_absent`` IS the publish mode: a linked publish on a taken key
        # fails with EEXIST, so there is no window between "does it exist" and
        # "write it" for a second writer to slip through.
        mode: PublishMode = "link" if if_absent else "replace"
        written, digest = await self._write_stream(
            target, data, mode=mode, checksum=checksum, what=key, declared_size=size
        )
        return PutResult(key=key, size=written, checksum=digest, etag=digest.hex())

    async def _write_stream(
        self,
        target: Path,
        data: AsyncIterator[bytes],
        *,
        mode: PublishMode,
        checksum: bytes,
        what: str,
        declared_size: int | None = None,
    ) -> tuple[int, bytes]:
        """Stream ``data`` through the one atomic writer, hashing as it goes.

        The checksum and the declared size are both compared while the bytes
        are still in the temp file, so a short or over-long stream leaves the
        target untouched and nothing under the final key.
        """
        hasher = blake3()
        written = 0
        try:
            with AtomicWriter(target, mode=mode, on_checkpoint=self._hook) as handle:
                async for chunk in data:
                    for offset in range(0, len(chunk), CHUNK_BYTES):
                        piece = chunk[offset : offset + CHUNK_BYTES]
                        handle.write(piece)
                        hasher.update(piece)
                        written += len(piece)
                digest = hasher.digest()
                self._checkpoints.reach_sync(AFTER_TEMP_WRITE)
                if declared_size is not None and written != declared_size:
                    raise InvalidRequest(
                        f"{what}: declared {declared_size} bytes, streamed {written}"
                    )
                if digest != checksum:
                    raise ChecksumMismatch(
                        f"{what}: declared {checksum.hex()}, computed {digest.hex()}"
                    )
        except FileExistsError as exc:
            raise PreconditionFailed(f"{target.name}: key already exists") from exc
        return written, digest

    # -- reads -----------------------------------------------------------

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise NotFound(key)
        size = path.stat().st_size
        if range is None:
            start, end = 0, size - 1
        else:
            start, end = range
            if start < 0 or end < start or start >= size:
                raise PreconditionFailed(f"{key}: range {range!r} is not satisfiable")
            end = min(end, size - 1)
        return _stream_range(path, start, end)

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        stat = path.stat()
        return ObjectInfo(size=stat.st_size, checksum=None, etag=None, storage_class=None)

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def with_checkpoints(self, checkpoints: Checkpoints) -> FilesystemStore:
        """The same store over the same tree, reporting to `checkpoints`.

        The conformance suite needs a second handle that pauses mid-move while
        the first one keeps answering reads; without it a test can only poll
        around an `os.replace` it can never catch in the middle.
        """
        return FilesystemStore(
            self._root, clock=self._clock, capabilities=self.capabilities, checkpoints=checkpoints
        )

    async def move(self, src: str, dst: str) -> None:
        source = self._path(src)
        destination = self._path(dst)
        if not source.exists():
            raise NotFound(src)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._checkpoints.reach(BEFORE_MOVE_PUBLISH)
        os.replace(source, destination)
        # A rename carries the source file's mtime, and the destination is the
        # instant the bytes became visible under *this* key — the staging file
        # they were written into is not. Anything that reads an object's age
        # (the sweep's horizon, the `deleted/` window) must see the publish, or
        # a sweep stamped after the bytes were staged reads a just-published
        # object as older than its own horizon and collects it.
        os.utime(destination)
        default_fsync_dir(destination.parent)

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        found: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self._root_real):
            for filename in filenames:
                key = self._relative(Path(dirpath) / filename)
                if _is_driver_scratch(key):
                    # A reconciler compares this listing against the rows it
                    # knows about, so a half-published temp file or a staged
                    # part must not read as an object it has never heard of.
                    continue
                if key.startswith(prefix) and (after is None or key > after):
                    found.append(key)
        found.sort()
        page = found[:limit]
        next_after = page[-1] if len(found) > limit else None
        return ListPage(keys=page, next_after=next_after)

    # -- multipart -------------------------------------------------------

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        handle = UploadHandle(key=key, upload_id=uuid.uuid4().hex, size=size)
        staging = self._staging(handle)
        staging.mkdir(parents=True, exist_ok=True)
        (staging / KEY_MARKER).write_text(key, encoding="utf-8")
        return handle

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        if part_no < 1 or part_no > self.capabilities.max_parts:
            # A bad part number wastes one call; the session is still usable.
            raise PreconditionFailed(f"{handle.key}: part number {part_no} is out of range")
        if size > self.capabilities.max_part_bytes:
            # Refused on the declared size, before a single byte is read.
            await self._refuse(
                handle,
                f"part {part_no} declares {size} bytes, over {self.capabilities.max_part_bytes}",
            )
        parts_dir = self._staging(handle)
        if not parts_dir.is_dir():
            raise NotFound(f"{handle.key}: no open multipart session")
        written, digest = await self._write_stream(
            parts_dir / str(part_no),
            data,
            mode="replace",
            checksum=checksum,
            what=f"{handle.key} part {part_no}",
            declared_size=size,
        )
        return PartResult(part_no=part_no, size=written, checksum=digest, etag=digest.hex())

    async def _refuse(self, handle: UploadHandle, why: str) -> NoReturn:
        """Refuse a part and close the session, so nothing is left half-open.

        A part the driver itself rejects can never be re-sent under this
        handle, so leaving the session open would only strand parts for a
        sweeper to find. A *transient* wire failure is deliberately not routed
        here: that part is retryable and the session has to survive it.
        """
        await self.multipart_abort(handle)
        raise PreconditionFailed(f"{handle.key}: {why}")

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        parts_dir = self._staging(handle)
        if not parts_dir.is_dir():
            raise NotFound(f"{handle.key}: no open multipart session")
        declared = sorted(parts, key=lambda part: part.part_no)
        on_disk = sorted(int(entry.name) for entry in parts_dir.iterdir() if entry.name.isdigit())
        if [part.part_no for part in declared] != on_disk:
            raise PreconditionFailed(
                f"{handle.key}: part list {[p.part_no for p in declared]} does not match {on_disk}"
            )
        for part in declared:
            path = parts_dir / str(part.part_no)
            if path.stat().st_size != part.size:
                raise PreconditionFailed(f"{handle.key}: part {part.part_no} size does not match")
            if _hash_file(path) != part.checksum:
                raise PreconditionFailed(
                    f"{handle.key}: part {part.part_no} checksum does not match"
                )

        target = self._path(handle.key)
        target.parent.mkdir(parents=True, exist_ok=True)
        hasher = blake3()
        written = 0
        try:
            with AtomicWriter(target, mode="link", on_checkpoint=self._hook) as sink:
                for part in declared:
                    with (parts_dir / str(part.part_no)).open("rb") as source:
                        while chunk := source.read(CHUNK_BYTES):
                            sink.write(chunk)
                            hasher.update(chunk)
                            written += len(chunk)
                self._checkpoints.reach_sync(AFTER_TEMP_WRITE)
        except FileExistsError as exc:
            raise PreconditionFailed(f"{target.name}: key already exists") from exc
        shutil.rmtree(parts_dir)
        digest = resolve_whole_object_checksum(
            handle.key, declared=checksum, assembled=hasher.digest(), parts=declared
        )
        return PutResult(key=handle.key, size=written, checksum=digest, etag=digest.hex())

    async def multipart_abort(self, handle: UploadHandle) -> None:
        parts_dir = self._staging(handle)
        if parts_dir.is_dir():
            shutil.rmtree(parts_dir)

    # -- the janitor's admin surface -------------------------------------

    async def list_incoming(
        self, *, after: str | None = None, limit: int = 1000
    ) -> tuple[Sequence[str], str | None]:
        """One keyset page of staged upload objects, in this driver's namespace.

        A domain-rooted handle answers ``incoming/<session>/…``; a bucket-rooted
        one answers the absolute ``domains/<uuid>/incoming/<session>/…``, which
        is the same namespace its ``delete`` and ``written_at`` take, so a page
        round-trips through the sweeper without a second translation.
        """
        found = sorted(
            key
            for key in self._walk_keys()
            if _incoming_tail(key) is not None and (after is None or key > after)
        )
        page = found[:limit]
        return page, (page[-1] if len(found) > limit else None)

    async def written_at(self, key: str) -> datetime | None:
        """When the bytes under ``key`` became visible, from the file's mtime.

        ``move`` re-stamps the destination, so this is the instant the object
        was published under *this* key rather than when it was staged.
        """
        path = self._path(key)
        if not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    async def list_incomplete(self) -> Sequence[tuple[str, str, datetime]]:
        """``(upload_id, key, initiated_at)`` for every session still staging parts."""
        pending: list[tuple[str, str, datetime]] = []
        for staging_root in self._staging_roots():
            if not staging_root.is_dir():
                continue
            for session in sorted(staging_root.iterdir()):
                marker = session / KEY_MARKER
                if not session.is_dir() or not marker.is_file():
                    # A session directory with no marker was written by a
                    # driver that predates it, or is mid-creation; aborting it
                    # would need a key we do not have, so it is left alone.
                    continue
                pending.append(
                    (
                        session.name,
                        marker.read_text(encoding="utf-8"),
                        datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC),
                    )
                )
        return pending

    async def abort(self, upload_id: str, key: str) -> None:
        """Drop one session's staged parts. Missing is success — the goal is gone."""
        if not upload_id.isalnum():
            raise InvalidRequest(f"upload id {upload_id!r} is not a plain token")
        for staging_root in self._staging_roots():
            session = staging_root / upload_id
            if session.is_dir():
                shutil.rmtree(session)

    def _staging_roots(self) -> tuple[Path, ...]:
        """Every ``.parts`` directory a writer on this tree stages into.

        A bucket-rooted handle sits *above* the per-domain roots the request
        path opens, and each of those has its own staging area, so the admin
        handle has to look into all of them or it reports no incomplete upload
        on the only layout the janitor ever runs against.
        """
        roots = [self._root / PARTS_ROOT]
        if self.layout == "bucket":
            domains = self._root / DOMAIN_PREFIX.rstrip("/")
            if domains.is_dir():
                roots.extend(sorted(entry / PARTS_ROOT for entry in domains.iterdir()))
        return tuple(roots)

    def _walk_keys(self) -> list[str]:
        found: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self._root_real):
            for filename in filenames:
                key = self._relative(Path(dirpath) / filename)
                if not _is_driver_scratch(key):
                    found.append(key)
        return found

    # -- unsupported capabilities ---------------------------------------

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        raise NotImplementedError("the filesystem driver cannot presign; use proxied transfer")

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        raise NotImplementedError("the filesystem driver cannot presign; use proxied transfer")

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        raise NotImplementedError("the filesystem driver vends no credentials; use PrefixGuard")


async def _stream_range(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def _hash_file(path: Path) -> bytes:
    hasher = blake3()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.digest()


def _is_driver_scratch(key: str) -> bool:
    """Whether ``key`` names a driver's own scratch file rather than an object."""
    # Every scratch name the driver writes is dot-leading: the ``.parts/``
    # staging root and the AtomicWriter's ``.<name>.tmp`` publishes alike, and
    # ``validate_relative_key`` refuses that namespace to a caller.
    return any(
        segment.startswith(".") or segment.endswith(TEMP_SUFFIX) for segment in key.split("/")
    )


def _incoming_tail(key: str) -> str | None:
    """The ``incoming/…`` part of ``key``, or ``None`` when it stages nothing.

    Both key namespaces answer here: a domain-relative key *is* its own tail, and
    a bucket-absolute one carries it after the ``domains/<uuid>/`` prefix.
    """
    if key.startswith(INCOMING_PREFIX):
        return key
    if key.startswith(DOMAIN_PREFIX):
        _domain, _, rest = key[len(DOMAIN_PREFIX) :].partition("/")
        if rest.startswith(INCOMING_PREFIX):
            return rest
    return None
