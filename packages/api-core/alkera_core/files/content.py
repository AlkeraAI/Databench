"""A node's bytes: write a version, read one back.

Two operations live here and they are deliberately asymmetric.

``put_version`` takes bytes and nothing else. There is no call path that names a
content hash and gets a version: a hash is an identifier, never a credential, so
the only way to point a node at content is to stream the content. The stream is
read exactly once — BLAKE3, the 4 MiB block digests and the MIME sniff all come
off the same pass — and what happens next depends on the size class only:

* at or below the inline cap the bytes are a column in the version row;
* above it a ``transfer_mode=single`` upload session row is created *first* (it
  owns the temp object, so a crash leaves bytes the sweeper can attribute), the
  object always lands under ``incoming/<session>/object``, and then **exactly
  one** further store call publishes it — a ``move`` onto the content key when
  no object with those bytes exists yet, or a ``delete`` of the temp when one
  does. Both branches cost the same calls in the same order, so a caller can
  never learn from timing or from their own bill whether someone else in the
  org already holds those bytes.

Only after ``head`` confirms the published object does one transaction write the
version, swap the node's head with a compare-and-swap on its etag, append the
history row, emit the outbox row, settle the quota hold and close the session.
Nothing before that commit is visible to a reader.

``open`` is the inverse and trusts nothing: every 4 MiB block is checked against
the digest recorded at write time *before* it is yielded, so a store that flips
or drops a byte raises rather than handing a caller bytes it cannot vouch for.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Table, func, select, text, update
from sqlalchemy.orm.util import identity_key

from alkera_core.config import settings
from alkera_core.db.locking import io_outside_locks
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.errors import (
    Conflict,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
    StoreUnavailable,
)
from alkera_core.files.fsck import OBJECT_SIZE_MISMATCH
from alkera_core.files.gc import claim_object
from alkera_core.files.hashing import BLOCK_BYTES, Digests, StreamHasher, verify_block
from alkera_core.files.history import NodeChangeReason, emit_node_changed, owner_ref, record
from alkera_core.files.ids import DriveId, NodeId, SessionId, VersionId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import (
    LeaseContext,
    admitted_inbound,
    fenced_write_for,
    is_final_push,
    is_hand_back,
)
from alkera_core.files.namespace import assert_not_moving
from alkera_core.files.quota import CeilingsResolver, QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sniff import SNIFF_BYTES, SniffResult, sniff_bytes
from alkera_core.files.store import keys
from alkera_core.files.store.errors import ChecksumMismatch, StoreError, Throttled, Unavailable
from alkera_core.files.store.errors import InvalidRequest as StoreInvalidRequest
from alkera_core.files.store.errors import NotFound as StoreNotFound
from alkera_core.files.store.protocol import ObjectInfo
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.uploads import UploadService
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import FileUploadSession
from alkera_core.models.files.versions import VERSION_SOURCES, FileVersion

if TYPE_CHECKING:
    from alkera_core.authz.principal import ActingContext

_NODES: Final[Table] = cast(Table, FileNode.__table__)
_SESSIONS: Final[Table] = cast(Table, FileUploadSession.__table__)

#: The default for a caller that is not driving an interleaving test.
_NO_CHECKPOINTS: Final = NoopCheckpoints()


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


#: The name of the whole-object temp under an ``incoming/<session>/`` prefix.
SINGLE_OBJECT: Final = "object"

#: How long a commit keeps re-reading ``head`` for an object the store has taken
#: but not yet made visible. Past it the write fails closed: a version row is
#: never written on the hope that the bytes will show up.
HEAD_WINDOW: Final = timedelta(seconds=30)
HEAD_RETRY_DELAY: Final = timedelta(seconds=1)

#: The sniffed types a content response may serve ``inline``. Until the malware
#: scanner lands nothing else is rendered in a browsing context, however benign
#: it looks: an unscanned object a viewer executes is the whole risk, and the
#: list is keyed on what the server sniffed, never on what a client claimed.
#:
#: ``application/pdf`` is on it because an artefact a reader cannot open is not
#: a deliverable — and because a PDF is the one document format whose risk is
#: in the browser's own viewer rather than in the origin serving it: it has no
#: DOM, so nothing it contains can read this origin's cookies or storage. It
#: renders under a CSP of its own (``backend.content_app.csp_for``), and it
#: reaches that branch only when the SERVER sniffed it as a PDF.
#:
#: ``text/html`` is on it for the same reason as the PDF, and only because the
#: policy it renders under takes the "with extra steps" out of stored XSS. A
#: page served here runs under ``sandbox`` with NO allowances (an opaque origin,
#: no script, no forms, no popups, no top-level navigation) and under fetch
#: directives that let through nothing but inline CSS and ``data:`` assets — so
#: it can neither execute nor call back to this origin, and everything it draws
#: it brought with it. That is exactly the self-contained page an agent is told
#: to produce; anything richer than that was never renderable here anyway.
#:
#: ``image/svg+xml`` joins it on the same terms and with a stricter policy —
#: sandboxed, no script, and no network of its own, so the one thing that made a
#: drawing dangerous (it is a document that can fetch and execute) is gone. The
#: four media types are inert: a browser decodes them, they have no DOM and no
#: fetch, and a chart recorded as a video is as much a deliverable as a PDF.
INLINE_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/json",
        "application/pdf",
        "text/csv",
        "text/html",
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/wav",
    }
)

#: The ``version_metadata`` keys a restored version carries over. They describe
#: the bytes, not the write that produced them, so they belong to the restored
#: row exactly as much as ``content_hash`` does: without ``block_hashes`` the
#: reinstated version has no per-block digests and every read of it would be
#: unverifiable, and without ``dedup_domain_id`` nothing records which domain's
#: prefix the object it points at lives under.
_RESTORE_CARRIED_METADATA: Final[tuple[str, ...]] = ("block_hashes", "dedup_domain_id")

#: The columns a restored version copies from the one it reinstates. Identity,
#: ordering and provenance are re-derived; everything that describes the BYTES is
#: carried across unchanged, because they are literally the same bytes.
_RESTORE_CARRIED: Final[tuple[str, ...]] = (
    "size_bytes",
    "content_hash",
    "block_hash",
    "manifest_id",
    "inline_bytes",
    "store_key",
    "mime_sniffed",
    "scan_state",
    "scanned_at",
    "scan_engine_version",
)

__all__ = [
    "HEAD_WINDOW",
    "INLINE_MIME_TYPES",
    "SINGLE_OBJECT",
    "ContentService",
    "VersionInfo",
    "disposition_for",
]


def _holder_claim(lease: LeaseContext | None) -> bool:
    """Whether the caller claims to be the lease holder — decided for real only
    by the fence, which is why a stale base is judged in two halves."""
    return lease is not None and lease.epoch is not None


def _refuse_stale_before_fence(
    node: FileNode, *, if_match: int, lease: LeaseContext | None, park: bool
) -> None:
    """Refuse a stale ``if_match`` from anyone who is not even claiming the lease.

    Before the fence, so an ordinary stale write is the 412 it always was and
    never a lease refusal in front of it.
    """
    if park and not _holder_claim(lease):
        raise Conflict("files.lease_mismatch", "only the folder's holder may park a version")
    if node.etag != if_match and not park and not _holder_claim(lease):
        raise PreconditionFailed(message=f"node {node.id} moved on")


def _refuse_stale_after_fence(
    node: FileNode,
    *,
    if_match: int,
    covering: Any | None,
    lease: LeaseContext | None,
    park: bool,
) -> None:
    """Refuse a stale ``if_match`` the fence did not prove to be the holder's.

    A caller that named an epoch on a folder nobody leases passes the fence
    with nothing covering it, and is an ordinary writer.
    """
    if is_hand_back(covering, lease):
        return
    if park:
        raise Conflict("files.lease_mismatch", "only the folder's holder may park a version")
    if node.etag != if_match:
        raise PreconditionFailed(message=f"node {node.id} moved on")


def disposition_for(mime_sniffed: str) -> Literal["inline", "attachment"]:
    """How a content response must present ``mime_sniffed``.

    Pure and allowlisted: an unknown or new type is an ``attachment``, so a
    sniffer that learns a type before this table does cannot make it renderable.
    """
    return "inline" if mime_sniffed in INLINE_MIME_TYPES else "attachment"


def _save_reason(handed_back: bool, origin: Mapping[str, Any] | None) -> NodeChangeReason | None:
    """What a committed write tells the clients it was: a lease holder's own
    bytes landing, a co-edited document's write-back, or neither."""
    if handed_back:
        return "live_saved"
    return "live_doc_saved" if origin else None


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """What a caller learns about the version their bytes ended up in."""

    id: VersionId
    seq: int
    size: int
    content_hash: str
    block_hash: str
    mime: str
    unchanged: bool
    """True when the node's head already held these bytes, so no version was written."""


class _StoreDigest(bytes):
    """A checksum the caller cannot know until the stream it describes has ended.

    A single-call PUT hashes while it uploads, so the digest does not exist when
    the store is asked to take the bytes. This stands in for it: the driver's own
    "what I computed equals what you declared" check passes, and the service then
    compares the digest the driver reports against the one its own pass produced
    — so the bytes are still proven, by the party that has something to prove.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, bytes | bytearray)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return bytes.__hash__(self)


@dataclass(frozen=True, slots=True)
class _Target:
    """The node a put is aimed at, read out before the stream starts."""

    node_id: NodeId
    drive_id: DriveId
    parent_id: UUID
    name: bytes
    etag: int
    size: int
    head_version_id: UUID | None
    #: The folder the bytes land in, for the caller's own team-scoped limits.
    parent_path: str | None = None
    #: Whether the write is the lease holder's own fenced write: what decides
    #: whether its live row clears and whose epoch the version carries.
    hand_back: bool = False
    #: Whether the write is the push that accompanies the release, which no
    #: ceiling refuses. Every other fenced write — the live plane's included —
    #: is bounded by the drive's limits like anyone else's.
    unbounded: bool = False


@dataclass(frozen=True, slots=True)
class _Streamed:
    """Everything one pass over the caller's bytes produced."""

    digests: Digests
    mime: SniffResult
    buffered: bytes | None


class ContentService:
    """Write and read the bytes behind a ``file`` node."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore,
        *,
        checkpoints: Checkpoints = _NO_CHECKPOINTS,
        head_window: timedelta = HEAD_WINDOW,
        ceilings: CeilingsResolver | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._head_window = head_window
        self._uploads = UploadService(
            repo, ctx, clock, store, checkpoints=checkpoints, ceilings=ceilings
        )
        self._quota = QuotaService(
            repo, ctx, clock, store, checkpoints=checkpoints, ceilings=ceilings
        )

    # ---- write -----------------------------------------------------------

    async def put_first_version(
        self,
        drive_id: DriveId,
        create: Callable[[], Awaitable[FileNode]],
        data: bytes,
        *,
        mime_hint: str | None = None,
    ) -> tuple[FileNode, VersionInfo]:
        """Make a file with ``create`` and write ``data`` as its first version,
        both in the caller's transaction: a file row never commits without its
        bytes.

        ``create`` makes the node (and any folder above it) through the
        namespace. The drive is taken before it, since the creates lock nodes
        and the write below takes the drive for its quota hold. So the bytes
        go up while this transaction holds the drive and the new rows; that is
        the one place a content write talks to the store under a lock, and it
        is declared here rather than at each caller.
        """
        async with self._repo.transaction():
            await self._repo.lock_drive(drive_id)
            node = await create()
        with io_outside_locks.allow(reason="a new file's first bytes land with its row"):
            info = await self.put_version(
                NodeId(node.id),
                _once(data),
                size_declared=len(data),
                if_match=int(node.etag),
                mime_hint=mime_hint,
            )
        return node, info

    async def put_version(
        self,
        node_id: NodeId,
        data: AsyncIterator[bytes],
        *,
        size_declared: int,
        if_match: int,
        mime_hint: str | None = None,
        lease: LeaseContext | None = None,
        park: bool = False,
        source: str = "upload",
        origin: Mapping[str, Any] | None = None,
    ) -> VersionInfo:
        """Stream ``data`` onto ``node_id`` and publish it as that node's head.

        ``lease`` is what the caller claims about the folder lease it is writing
        under. It is checked twice on purpose: once before a byte is read, so a
        fenced writer never opens a session or takes a quota hold it will lose,
        and once inside the commit transaction, so a lease that changed hands
        while the bytes were streaming still fences the write.

        The lease holder's own write is the one write ``if_match`` does not
        refuse. The holder's base is the etag it last agreed with the drive; a
        head the web moved since is a two-way write, and the drive settles it
        in the commit transaction rather than answering 412 — the holder's
        bytes become the head and the head they displaced is kept as a
        conflicted copy (:mod:`alkera_core.files.conflict_auto`). Everyone
        else's stale ``if_match`` is refused as before.

        ``park`` writes the bytes as a version of the node WITHOUT making them
        the head — the holder's conflict submission, whose bytes were displaced
        on its disk by a write the drive already holds. Only the holder may.

        ``source`` is what the version records as its origin (one of
        :data:`VERSION_SOURCES`): ``upload`` for bytes a client sent, and
        ``document_snapshot`` for a co-edited document written back.

        ``origin`` is where in a co-edited document the bytes were taken from
        (its epoch and version vector), recorded on the version so a pass
        that wrote it, and was cut off before it said so, recognises its own
        write afterwards rather than merging it again.
        """
        if size_declared < 0:
            raise InvalidRequest("files.invalid_size", "declared size may not be negative")
        if source not in VERSION_SOURCES:
            raise InvalidRequest("files.invalid_source", f"no version source {source!r}")
        planned = await self._precheck(node_id, if_match=if_match, lease=lease, park=park)
        # Every store call happens before the drive row is taken. Inside a
        # route's transaction every lock lasts to its end, and the drive row is
        # the one every write in the org queues on: staging and publishing tens
        # of megabytes while holding it put each of those writes behind the
        # upload, past their lock timeout. So the bytes are staged under the
        # session's key, the session is opened with no room held, the object is
        # published, and only then are the drive, the fence and the quota hold
        # taken -- with nothing left to wait on but the database.
        session_id = SessionId(uuid4())
        streamed = await self._absorb(data, session_id, size_declared=size_declared)
        try:
            await self._uploads.open_single(
                planned.drive_id,
                planned.parent_id,
                planned.name,
                node_id=planned.node_id,
                declared_size=size_declared,
                session_id=session_id,
            )
        except BaseException:
            await self._discard_staged(streamed, session_id)
            raise
        try:
            store_key = await self._publish(streamed, session_id)
            target = await self._target(node_id, if_match=if_match, lease=lease, park=park)
            await self._uploads.reserve_single(
                session_id,
                drive_id=target.drive_id,
                declared_size=size_declared,
                parent_path=target.parent_path,
                unbounded=target.unbounded,
            )
        except BaseException:
            await self._release(session_id)
            raise
        return await self._commit(
            target,
            streamed,
            store_key=store_key,
            session_id=session_id,
            if_match=if_match,
            mime_hint=mime_hint,
            lease=lease,
            park=park,
            source=source,
            origin=origin,
        )

    async def _precheck(
        self, node_id: NodeId, *, if_match: int, lease: LeaseContext | None, park: bool
    ) -> _Target:
        """The node a put is aimed at, refused if missing or stale before any
        byte is read.

        A plain read, taking no lock: the decisions that count are taken under
        the locks once the bytes are in the store (:meth:`_target`, then again
        in the commit). This one only spares a write that is already refused
        the cost of sending its bytes to the store, and names the node the
        session is opened against.
        """
        async with self._repo.transaction():
            node = await self._repo.node(node_id)
            if node is None or node.kind != "file" or node.parent_id is None:
                raise NotFound(message=f"file {node_id}")
            _refuse_stale_before_fence(node, if_match=if_match, lease=lease, park=park)
            return _Target(
                node_id=NodeId(node.id),
                drive_id=DriveId(node.drive_id),
                parent_id=node.parent_id,
                name=node.name,
                etag=node.etag,
                size=node.size,
                head_version_id=node.head_version_id,
            )

    async def _discard_staged(self, streamed: _Streamed, session_id: SessionId) -> None:
        """Drop bytes staged for a write whose session could not be opened.

        Best effort: an object left behind has no session row, and the
        reconciliation sweeper deletes it once it is past the grace period.
        """
        if streamed.buffered is not None:
            return
        with contextlib.suppress(StoreError):
            await self._store.delete(keys.incoming_key(session_id, SINGLE_OBJECT))

    async def _target(
        self, node_id: NodeId, *, if_match: int, lease: LeaseContext | None, park: bool = False
    ) -> _Target:
        async with self._repo.transaction():
            node = await self._repo.node(node_id)
            if node is None or node.kind != "file" or node.parent_id is None:
                raise NotFound(message=f"file {node_id}")
            _refuse_stale_before_fence(node, if_match=if_match, lease=lease, park=park)
            # The drive before the fence. Inside a route's transaction these
            # locks are held to its end, and the upload session opened next
            # takes the drive for its quota hold: fenced first, this held the
            # leased folder while waiting for the drive that a create or a
            # promote held while waiting for the folder.
            await self._repo.lock_drive(DriveId(node.drive_id))
            covering = await fenced_write_for(self._repo, node, lease)
            _refuse_stale_after_fence(
                node, if_match=if_match, covering=covering, lease=lease, park=park
            )
            return _Target(
                parent_path=node.path_ids.rsplit(".", 1)[0] if "." in node.path_ids else None,
                hand_back=is_hand_back(covering, lease),
                unbounded=is_final_push(covering, lease),
                node_id=NodeId(node.id),
                drive_id=DriveId(node.drive_id),
                parent_id=node.parent_id,
                name=node.name,
                etag=node.etag,
                size=node.size,
                head_version_id=node.head_version_id,
            )

    async def _absorb(
        self, data: AsyncIterator[bytes], session_id: SessionId, *, size_declared: int
    ) -> _Streamed:
        """Read the caller's bytes once, hashing, sniffing and staging as we go."""
        inline = size_declared <= settings.files_inline_max_bytes
        hasher = StreamHasher()
        head = bytearray()
        buffer = bytearray() if inline else None

        async def tee() -> AsyncIterator[bytes]:
            seen = 0
            async for chunk in data:
                seen += len(chunk)
                if seen > size_declared:
                    raise InvalidRequest(
                        "files.size_mismatch",
                        f"declared {size_declared} bytes, streamed at least {seen}",
                    )
                hasher.update(chunk)
                if len(head) < SNIFF_BYTES:
                    head.extend(chunk[: SNIFF_BYTES - len(head)])
                if buffer is not None:
                    buffer.extend(chunk)
                yield chunk

        if inline:
            async for _ in tee():
                pass
        else:
            try:
                await self._store.put(
                    keys.incoming_key(session_id, SINGLE_OBJECT),
                    tee(),
                    size=size_declared,
                    checksum=_StoreDigest(),
                    if_absent=False,
                )
            except StoreInvalidRequest as exc:
                # The driver refuses a stream that does not match the length it
                # was promised; that is the caller's error, not the store's.
                raise InvalidRequest(
                    "files.size_mismatch",
                    f"declared {size_declared} bytes, streamed {hasher.finalize().size}",
                ) from exc
            except ChecksumMismatch as exc:
                raise InvalidRequest(
                    "files.checksum_mismatch",
                    "the streamed bytes do not match the checksum declared for them",
                ) from exc
            except (Throttled, Unavailable) as exc:
                # A throttle or an outage is not this caller's mistake and not
                # an internal fault: it leaves as a 503, never the platform 500
                # the catch-all would render.
                raise StoreUnavailable("the object store refused the write") from exc
            except StoreError as exc:
                # And neither is an expired scoped credential, a denied bucket
                # or a bucket that was never created. Naming only the classes
                # above would let the rest reach the platform catch-all.
                raise StoreUnavailable("the object store refused the write") from exc
        digests = hasher.finalize()
        if digests.size != size_declared:
            raise InvalidRequest(
                "files.size_mismatch",
                f"declared {size_declared} bytes, streamed {digests.size}",
            )
        return _Streamed(
            digests=digests,
            mime=sniff_bytes(bytes(head)),
            buffered=None if buffer is None else bytes(buffer),
        )

    async def _publish(self, streamed: _Streamed, session_id: SessionId) -> str | None:
        """Move the staged object onto its content key, or drop it as a duplicate.

        Every outcome a caller can reach at will costs the same store calls in
        the same order — put, head, head, one publish call, head — so nothing a
        caller can measure says whether the org already held these bytes.

        The content key is claimed on the session row, under the object's lock,
        BEFORE the store is asked about it. An adopted object is old by
        definition, so age never protects it from the reachability sweep; the
        claim is what does, from this commit until the version row exists. A
        sweep that took the lock first finishes its move first, and the object
        is then found parked and taken back rather than lost.
        """
        if streamed.buffered is not None:
            return None
        temp = keys.incoming_key(session_id, SINGLE_OBJECT)
        content = keys.object_key(streamed.digests.content_hash)
        parked_key = keys.deleted_key(content)
        await self._checkpoints.reach("content.after_store_put")
        async with self._repo.transaction():
            await claim_object(self._repo, session_id, content)
        await self._checkpoints.reach("content.after_claim")
        existing = await self._head(content)
        parked = await self._head(parked_key)
        await self._checkpoints.reach("content.after_head")
        # Adopting an object makes it *this* caller's file, and the staged temp
        # is the only other copy of the bytes. So the object has to prove it is
        # the right one BEFORE the temp goes; the ``head`` above already carries
        # everything the store's contract says about it (a size, and an etag no
        # driver defines as a digest), so the check costs no extra round trip.
        # An object that disagrees has rotted — a half-written restore, a
        # truncated publish — and is not adopted at all: the staged bytes take
        # the key instead, because they are the ones just proved against their
        # own digest. Dropping the temp first would hand the caller the rot and
        # destroy the only good copy in the same breath.
        rotted = existing is not None and existing.size != streamed.digests.size
        # The sweep parked these bytes and nothing has erased them yet: the
        # object goes back where every reader looks for it. Parking is a move,
        # so this is its exact inverse, and it only happens for bytes of the
        # size this caller just proved.
        unpark = existing is None and parked is not None and parked.size == streamed.digests.size
        try:
            if unpark:
                await self._store.move(parked_key, content)
                await self._store.delete(temp)
            elif existing is None or rotted:
                await self._store.move(temp, content)
            else:
                await self._store.delete(temp)
        except StoreError as exc:
            raise StoreUnavailable(f"the object store refused to publish {content}") from exc
        if rotted and existing is not None:
            await self._quarantine_object(
                content, found=existing.size, expected=streamed.digests.size
            )
        await self._verify(content, size=streamed.digests.size)
        return content

    async def _quarantine_object(self, key: str, *, found: int, expected: int) -> None:
        """Put a rotted object in front of an operator, the way ``fsck`` does.

        The good bytes already replaced it, so this is the record of *what was
        wrong* rather than a request to repair it: an operator still has to find
        out which write left a short object under a content-addressed key. Ids
        and sizes only, never a name — the row outlives the request.
        """
        async with self._repo.transaction():
            await self._repo.session.execute(
                text(
                    "INSERT INTO file_quarantine "
                    "(id, org_team_id, kind, ref_id, reason, attempts, detail) "
                    "VALUES (gen_random_uuid(), :org, 'object', :ref, :reason, 0, "
                    "CAST(:detail AS jsonb))"
                ),
                {
                    "org": str(self._repo.scope.org_team_id),
                    "ref": key,
                    "reason": OBJECT_SIZE_MISMATCH,
                    "detail": json.dumps({"found": found, "expected": expected}, sort_keys=True),
                },
            )

    async def _verify(self, key: str, *, size: int) -> None:
        """Re-read ``head`` until the store admits the object, then fail closed."""
        deadline = self._clock.monotonic() + self._head_window.total_seconds()
        while True:
            info = await self._head(key)
            if info is not None:
                if info.size != size:
                    raise ChecksumMismatch(f"{key}: stored {info.size} bytes, expected {size}")
                return
            if self._clock.monotonic() >= deadline:
                raise StoreNotFound(f"{key}: not visible within {self._head_window}")
            await self._sleep()

    async def _head(self, key: str) -> ObjectInfo | None:
        """``head``, with a driver refusal translated into the Files vocabulary.

        An absent object is ``None``, which is the answer the callers branch on.
        A driver that *refuses* — an expired scoped credential, a denied bucket,
        a bucket that was never created — would otherwise reach the platform
        catch-all as an opaque 500 instead of the typed 503 this class of
        failure deserves.
        """
        try:
            return await self._store.head(key)
        except StoreError as exc:
            raise StoreUnavailable(f"the object store could not answer for {key}") from exc

    async def _sleep(self) -> None:
        """Wait one retry step on the injected clock, never on the wall clock."""
        clock = self._clock
        advance = getattr(clock, "advance", None)
        if advance is None:  # pragma: no cover - the production clock cannot be advanced
            raise StoreNotFound("the object is not visible yet")
        advance(HEAD_RETRY_DELAY)

    async def _commit(
        self,
        target: _Target,
        streamed: _Streamed,
        *,
        store_key: str | None,
        session_id: SessionId,
        if_match: int,
        mime_hint: str | None,
        lease: LeaseContext | None,
        park: bool = False,
        source: str = "upload",
        origin: Mapping[str, Any] | None = None,
    ) -> VersionInfo:
        digests = streamed.digests
        content_hash = digests.content_hash.hex()
        await self._checkpoints.reach("content.before_commit")
        async with self._repo.transaction():
            # Drive, folder, node -- the fixed Files lock order: the settle
            # writes the folder's child counts, and a rename holds that folder
            # before it writes the node. Taken *before*
            # the version sequence is allocated: two commits on one node would
            # otherwise both read the same ``max(seq) + 1`` and the loser would
            # surface ``uq_file_versions_node_seq`` as a raw driver error. With
            # the row locked the second commit waits, then sees the etag the
            # first one bumped and is refused as a precondition failure.
            _, _, node = await self._repo.lock_chain(
                target.drive_id, NodeId(target.parent_id), target.node_id
            )
            if node is None:
                raise NotFound(message=f"file {target.node_id}")
            # Inside the commit transaction, not before the upload: a batched
            # move can start while the bytes are streaming, and publishing a
            # head onto a row whose path is being rewritten would leave the
            # version attached to a node the move has already read past. The
            # row is re-read first because this session read it before the
            # upload began, so its mapped copy predates exactly the move this
            # is meant to catch.
            await self._repo.session.refresh(node)
            assert_not_moving(node)
            _refuse_stale_before_fence(node, if_match=if_match, lease=lease, park=park)
            # After the policy decision and inside the transaction that writes:
            # a lease taken while these bytes were streaming fences them here,
            # where the row is already locked and nothing has been published.
            covering = await fenced_write_for(self._repo, node, lease)
            _refuse_stale_after_fence(
                node, if_match=if_match, covering=covering, lease=lease, park=park
            )
            landed_inbound = admitted_inbound(covering) is not None
            # The lease row only when these bytes are its holder's own: that is
            # the one case whose landing resets the lease's clock and stamps the
            # epoch on the version.
            handed_back = covering if is_hand_back(covering, lease) else None
            # The etag the swap is fenced on. Equal to ``if_match`` for every
            # writer but the holder, whose stale base is settled below.
            current = int(node.etag)
            size_before = int(node.size)
            head = await self._head_version(node.head_version_id)
            if park:
                # The holder's displaced bytes, kept as a version beside the
                # head the drive already holds: no swap, no report to settle,
                # and the hold goes back because the head's size is unchanged.
                version = await self._insert(
                    target,
                    streamed,
                    path=node.path_ids,
                    store_key=store_key,
                    content_hash=content_hash,
                    mime_hint=mime_hint,
                    source=source,
                    based_on=if_match,
                    origin=origin,
                    machine=handed_back.machine_id if handed_back is not None else None,
                    base_unknown=handed_back is not None
                    and lease is not None
                    and not lease.base_known,
                )
                await self._close(session_id, bytes_committed=None, drive_id=target.drive_id)
                info = self._info(version, unchanged=False)
            elif (
                head is not None
                and head.content_hash == content_hash
                and head.size_bytes == digests.size
            ):
                await self._close(session_id, bytes_committed=None, drive_id=target.drive_id)
                # The bytes are the drive's already, and may be exactly what
                # the holder reported it has: then its report is settled even
                # though nothing new was written.
                if await self._settle_report(node, size=digests.size, digest=digests.content_hash):
                    await emit_node_changed(
                        self._repo,
                        self._ctx,
                        node_id=target.node_id,
                        drive_id=target.drive_id,
                        version=node.etag,
                        parent_id=NodeId(target.parent_id),
                        reason="live_saved" if handed_back is not None else None,
                    )
                info = self._info(head, unchanged=True)
            else:
                # The holder's base is behind the head: somebody else's write
                # landed since the holder last agreed with the drive. Its bytes
                # are what this write displaces, so where they go is decided —
                # and a copy minted — before the head moves.
                displacing = head if current != if_match and head is not None else None
                settlement = None
                if displacing is not None and handed_back is not None:
                    settlement = await self._plan_displacement(
                        node, displacing, lease, holder_machine=handed_back.machine_id
                    )
                version = await self._insert(
                    target,
                    streamed,
                    path=node.path_ids,
                    store_key=store_key,
                    content_hash=content_hash,
                    mime_hint=mime_hint,
                    source=source,
                    based_on=if_match,
                    origin=origin,
                    machine=handed_back.machine_id if handed_back is not None else None,
                    base_unknown=handed_back is not None
                    and lease is not None
                    and not lease.base_known,
                )
                await self._swap(
                    target,
                    version,
                    if_match=current,
                    mime_class=streamed.mime.mime_class,
                )
                await record(
                    self._repo,
                    self._ctx,
                    node_id=target.node_id,
                    kind="attrs",
                    before={"etag": current, "size": size_before},
                    after={"etag": current + 1, "size": digests.size},
                )
                await self._settle_report(node, size=digests.size, digest=digests.content_hash)
                live = LiveEntriesService(self._repo, self._ctx)
                if handed_back is not None:
                    # The holder's own bytes have landed: whatever it was
                    # reporting about this node is now what the drive holds, so
                    # the live row goes and the lease's clock is reset.
                    await live.clear([target.node_id], reason="saved")
                    await self._mark_synced(
                        target.node_id,
                        lease_node_id=handed_back.node_id,
                        epoch=handed_back.epoch,
                    )
                elif landed_inbound:
                    await live.accept_inbound(node, kind="inbound")
                if settlement is not None and displacing is not None and handed_back is not None:
                    from alkera_core.files import conflict_auto

                    await self._repo.session.refresh(node)
                    displaced_by, copy_target = settlement
                    await conflict_auto.settle_displacement(
                        self._repo,
                        self._ctx,
                        self._clock,
                        node=node,
                        displaced=displacing,
                        kept=version,
                        base=displacing,
                        target=copy_target,
                        arrived_from="holder",
                        kept_by=await conflict_auto.holder_name(self._repo, handed_back),
                        displaced_by=displaced_by,
                    )
                else:
                    await emit_node_changed(
                        self._repo,
                        self._ctx,
                        node_id=target.node_id,
                        drive_id=target.drive_id,
                        version=current + 1,
                        parent_id=NodeId(target.parent_id),
                        reason=_save_reason(handed_back is not None, origin),
                    )
                await self._close(
                    session_id,
                    bytes_committed=digests.size - size_before,
                    drive_id=target.drive_id,
                    # A recount counts a node only once a head version exists,
                    # so the commit that lands the FIRST head is what adds the
                    # file; every later one only moves bytes.
                    nodes_committed=0 if target.head_version_id is not None else 1,
                )
                info = self._info(version, unchanged=False)
        await self._checkpoints.reach("content.after_commit_before_cleanup")
        return info

    async def _plan_displacement(
        self,
        node: FileNode,
        displacing: FileVersion,
        lease: LeaseContext | None,
        *,
        holder_machine: str | None = None,
    ) -> tuple[str, Any]:
        """Who wrote the head the holder is displacing, and where it will go.

        A head a live session wrote back (``document_snapshot``, which only a
        session writes) goes nowhere but the node's history: the session holds
        those bytes and merges the holder's over them from the version the
        holder made its change on, now or when the file is next opened, so a
        copy beside the file would only repeat what the file is about to say.

        A head the same machine sent goes nowhere but the history too: a box's
        two writers (its live plane and its checkpoint push) can each send the
        file from the same disk, one on top of the other, and a box never
        conflicts with itself; its newer bytes are its disk's newer state."""
        from alkera_core.files import conflict_auto

        displaced_by = await conflict_auto.writer_name(self._repo, displacing)
        if displacing.source == "document_snapshot":
            return displaced_by, conflict_auto.CopyTarget(mode="versions_only")
        sent_by = (displacing.version_metadata or {}).get("machine_id")
        if holder_machine and sent_by == holder_machine:
            return displaced_by, conflict_auto.CopyTarget(mode="versions_only")
        target = await conflict_auto.reserve_copy(
            self._repo, self._ctx, self._clock, node=node, displaced_by=displaced_by, lease=lease
        )
        return displaced_by, target

    async def _settle_report(self, node: FileNode, *, size: int, digest: bytes) -> bool:
        """Clear the holder's report on ``node`` when these bytes are what it
        reported, and answer whether it did.

        A report says what the disk holds while the drive holds something else
        or nothing; once a version lands that IS the disk's file, the report is
        spent. The version is the disk's file when its size is the reported
        size and -- when the holder has hashed the file -- its BLAKE3 is the
        reported hash. A version that differs (the disk moved on while the bytes
        were in flight) leaves the report, and the row reads ``behind``.

        The row takes the disk's modified time with it, rather than the moment
        the commit ran: it is the time the freshness pair compares the next
        report against. A node with no report costs nothing -- its row was read
        at the top of the commit -- so an ordinary upload pays no statement.
        """
        if node.holder_size is None:
            return False
        cleared = (
            await self._repo.session.execute(
                text(
                    "UPDATE file_nodes SET holder_size = NULL, holder_mtime_ns = NULL, "
                    "holder_hash = NULL, holder_seq = NULL, "
                    "mtime_ns = coalesce(holder_mtime_ns, mtime_ns) "
                    "WHERE id = :node AND org_team_id = :org AND holder_size = :size "
                    "AND (holder_hash IS NULL OR holder_hash = :digest) RETURNING id"
                ),
                {
                    "node": node.id,
                    "org": self._repo.scope.org_team_id,
                    "size": size,
                    "digest": digest,
                },
            )
        ).first()
        if cleared is None:
            return False
        await self._repo.session.refresh(node)
        return True

    async def _mark_synced(self, node_id: NodeId, *, lease_node_id: UUID, epoch: int) -> None:
        """Stamp the landed version with the epoch that produced it.

        The version row is the audit trail of a hand-back: it says which mount
        of the folder wrote these bytes, so a later reader can tell one machine's
        checkpoint from the next machine's. The lease's own clock moves with it,
        because a lease that just landed bytes has demonstrably synced.
        """
        await self._repo.session.execute(
            text(
                # The column is spelled ``metadata``; the ORM maps it to
                # ``version_metadata`` because the name is taken on Declarative.
                'UPDATE file_versions SET "metadata" = '
                "coalesce(\"metadata\", '{}'::jsonb) || CAST(:meta AS jsonb) "
                "WHERE id = (SELECT head_version_id FROM file_nodes "
                "WHERE id = :node AND org_team_id = :org)"
            ),
            {
                "meta": json.dumps({"live_lease_epoch": int(epoch)}),
                "node": node_id,
                "org": self._repo.scope.org_team_id,
            },
        )
        await self._repo.session.execute(
            text(
                "UPDATE file_leases SET last_sync_at = now() "
                "WHERE node_id = :lease AND org_team_id = :org"
            ),
            {"lease": lease_node_id, "org": self._repo.scope.org_team_id},
        )

    # ---- restore a version ----------------------------------------------

    async def restore_version(
        self,
        node_id: NodeId,
        version_id: VersionId,
        *,
        if_match: int | None = None,
        lease: LeaseContext | None = None,
    ) -> tuple[FileNode, FileVersion]:
        """Make an older version the head again by appending it as a new one.

        Restoring is a forward write, never a rewind: the bytes are already in
        the store and already referenced, so a new version row is appended
        pointing at the same object rather than the versions above it being
        deleted. That is what makes a restore itself undoable, and it is why the
        reachability sweep never sees the object become unreferenced in between.

        Fenced like ``put_version``: what lands is a new head on a node inside
        somebody's mount, and a head the holder never wrote is a head its next
        sync would have to call a conflict. The check runs after the caller has
        authorized and inside their transaction, so a lease taken while the
        request was in flight still refuses it.

        The caller supplies the open transaction and has already authorized, the
        way :class:`~alkera_core.files.namespace.Namespace` verbs are called.
        """
        old = await self._repo.version(version_id)
        if old is None or old.node_id != node_id:
            raise NotFound(message=f"version {version_id}")
        # Drive, folder, node -- the fixed Files lock order: the settle at the
        # end of this transaction takes the drive row and writes the folder's
        # counts, so taking the node first would deadlock against a move or a
        # trash that took the drive first, or a rename holding the folder.
        located = await self._repo.node(node_id)
        if located is None:
            raise NotFound(message=f"file {node_id}")
        _, _, node = await self._repo.lock_chain(
            DriveId(located.drive_id),
            None if located.parent_id is None else NodeId(located.parent_id),
            NodeId(node_id),
        )
        if node is None or node.parent_id is None:
            raise NotFound(message=f"file {node_id}")
        covering = await fenced_write_for(self._repo, node, lease)
        if if_match is not None and node.etag != if_match:
            raise PreconditionFailed(message=f"node {node_id} moved on")
        before = node.etag
        target = _Target(
            node_id=node_id,
            drive_id=DriveId(node.drive_id),
            parent_id=node.parent_id,
            name=node.name,
            etag=before,
            size=node.size,
            head_version_id=node.head_version_id,
        )
        rows = await self._repo.versions_of(node_id)
        made = FileVersion(
            id=uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            seq=max((row.seq for row in rows), default=0) + 1,
            source="restore",
            version_metadata={
                **{
                    key: old.version_metadata[key]
                    for key in _RESTORE_CARRIED_METADATA
                    if key in old.version_metadata
                },
                "restored_from": str(old.id),
                "restored_seq": old.seq,
                # The head the restorer saw and chose to replace: a co-edited
                # document applies the restore as an edit made on it.
                "based_on_etag": before,
            },
            created_by=await owner_ref(self._repo, self._ctx, target.drive_id, node.path_ids),
            **{name: getattr(old, name) for name in _RESTORE_CARRIED},
        )
        await self._repo.add(made)
        await self._repo.flush()
        # The same compare-and-swap a fresh upload publishes through, so a
        # concurrent write on this node loses the race here rather than leaving
        # two heads: nothing about a restore is allowed to be looser.
        await self._swap(target, made, if_match=before, mime_class=node.mime_class)
        await record(
            self._repo,
            self._ctx,
            node_id=node_id,
            kind="attrs",
            before={"etag": before, "size": target.size},
            after={"etag": before + 1, "size": made.size_bytes},
        )
        if admitted_inbound(covering) is not None:
            # Restored inside a folder a machine is holding: its disk still has
            # the bytes the restore replaced. Recorded for it to apply, exactly
            # as an upload let in there is, or the holder never learns the head
            # moved and its next push files the stale bytes over the restore.
            await LiveEntriesService(self._repo, self._ctx).accept_inbound(node, kind="inbound")
        await emit_node_changed(
            self._repo,
            self._ctx,
            node_id=node_id,
            drive_id=DriveId(node.drive_id),
            version=before + 1,
        )
        # A restore commits bytes without ever taking a quota hold, so nothing
        # on the upload path accounts for it. Left out, the drive's cached size
        # disagrees with a recount the moment a restore changes the head's size,
        # and a restore that pushes the drive past its ceiling never freezes it.
        await self._quota.settle_head_swap(
            DriveId(node.drive_id),
            NodeId(node.parent_id),
            bytes_delta=int(made.size_bytes) - target.size,
        )
        return node, made

    async def _head_version(self, version_id: UUID | None) -> FileVersion | None:
        if version_id is None:
            return None
        return await self._repo.version(VersionId(version_id))

    async def _insert(
        self,
        target: _Target,
        streamed: _Streamed,
        *,
        path: str,
        store_key: str | None,
        content_hash: str,
        mime_hint: str | None,
        source: str = "upload",
        based_on: int | None = None,
        origin: Mapping[str, Any] | None = None,
        machine: str | None = None,
        base_unknown: bool = False,
    ) -> FileVersion:
        digests = streamed.digests
        drive = await self._repo.drive(target.drive_id)
        if drive is None:  # pragma: no cover - the node's drive cannot vanish mid-put
            raise NotFound(message=f"drive {target.drive_id}")
        seq = await self._next_seq(target.node_id)
        metadata: dict[str, Any] = {
            "block_hashes": [digest.hex() for digest in digests.block_hashes],
            "dedup_domain_id": str(drive.dedup_domain_id),
        }
        if mime_hint is not None:
            # Diagnostics only: what a client claimed is never what we serve.
            metadata["mime_hint"] = mime_hint
        if based_on is not None:
            # The etag the writer said its bytes were made on (its if-match):
            # what a live session merges this version from.
            metadata["based_on_etag"] = based_on
        if origin:
            metadata["origin"] = dict(origin)
        if base_unknown:
            # A holder that did not say its precondition is the version its
            # bytes were made on (a box from before it was so): a co-edited
            # document merges these bytes as made on an unknown version.
            metadata["base_unknown"] = True
        if machine:
            # The lease holder's own bytes: the machine they came from, which
            # a live document names as the author of what it merges from them.
            metadata["machine_id"] = machine
        version = FileVersion(
            org_team_id=self._repo.scope.org_team_id,
            node_id=target.node_id,
            seq=seq,
            size_bytes=digests.size,
            content_hash=content_hash,
            block_hash=digests.block_hash.hex(),
            inline_bytes=streamed.buffered,
            store_key=store_key,
            mime_sniffed=streamed.mime.mime,
            scan_state="pending",
            source=source,
            version_metadata=metadata,
            created_by=await owner_ref(self._repo, self._ctx, target.drive_id, path),
        )
        await self._repo.add(version)
        await self._repo.flush()
        return version

    async def _next_seq(self, node_id: NodeId) -> int:
        statement = select(func.coalesce(func.max(FileVersion.seq), 0) + 1).where(
            FileVersion.node_id == node_id
        )
        return int((await self._repo.execute_scoped(statement)).scalar_one())

    async def _swap(
        self, target: _Target, version: FileVersion, *, if_match: int, mime_class: str | None
    ) -> None:
        """Publish the version with a compare-and-swap on the node's etag."""

        statement = (
            update(_NODES)
            .where(
                FileNode.id == target.node_id,
                FileNode.org_team_id == self._repo.scope.org_team_id,
                FileNode.etag == if_match,
            )
            .values(
                head_version_id=version.id,
                etag=FileNode.etag + 1,
                size=version.size_bytes,
                mime_class=mime_class,
                mtime_ns=int(self._clock.now().timestamp() * 1_000_000_000),
            )
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        if result.rowcount != 1:
            raise PreconditionFailed(message=f"node {target.node_id} moved on")
        # The swap is Core SQL, so an ORM copy of *this node* in the session
        # still holds the old etag; expiring it keeps a later read in the same
        # session from comparing If-Match against a value that is already gone.
        stale = self._repo.session.identity_map.get(identity_key(FileNode, (target.node_id,)), None)
        if stale is not None:
            await self._repo.session.refresh(stale)

    async def _close(
        self,
        session_id: SessionId,
        *,
        bytes_committed: int | None,
        drive_id: DriveId,
        nodes_committed: int = 0,
    ) -> None:
        """Settle the hold and retire the session in the committing transaction.

        The freeze is re-decided here rather than left to a later sweep because
        the quota check reads committed usage: a drive whose freeze only landed
        on the next pass would keep accepting writes in between, so the
        transaction that takes it past its ceiling is the one that closes it.
        """
        if bytes_committed is None:
            await self._quota.release(session_id)
        else:
            await self._quota.reconcile(
                session_id, actual_bytes=bytes_committed, actual_nodes=nodes_committed
            )
            await self._quota.freeze_if_over(drive_id)
        await self._repo.session.execute(
            update(_SESSIONS)
            .where(
                FileUploadSession.id == session_id,
                FileUploadSession.org_team_id == self._repo.scope.org_team_id,
            )
            .values(state="done")
        )

    async def _release(self, session_id: SessionId) -> None:
        """Give back the hold when the bytes never became a version."""
        async with self._repo.transaction():
            await self._quota.release(session_id)
            await self._repo.session.execute(
                update(_SESSIONS)
                .where(
                    FileUploadSession.id == session_id,
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                )
                .values(state="aborted")
            )

    # ---- read ------------------------------------------------------------

    async def version_info(self, version_id: VersionId) -> VersionInfo:
        async with self._repo.transaction():
            version = await self._repo.version(version_id)
            if version is None:
                raise NotFound(message=f"version {version_id}")
            return self._info(version, unchanged=False)

    async def open(
        self, version_id: VersionId, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        """Stream a version's bytes, verifying every block before it is yielded."""
        async with self._repo.transaction():
            version = await self._repo.version(version_id)
            if version is None:
                raise NotFound(message=f"version {version_id}")
            inline = version.inline_bytes
            store_key = version.store_key
            size = version.size_bytes
            blocks = _blocks(version)
        start, end = _bounds(size, range)
        if inline is not None:
            payload = inline
            _verify_blocks(payload, blocks, first=0)
            return _slice(payload, start, end)
        if store_key is None:  # pragma: no cover - a version has bytes or a key
            raise NotFound(message=f"version {version_id} has no bytes")
        return self._stream(store_key, start=start, end=end, blocks=blocks, size=size)

    async def _stream(
        self, store_key: str, *, start: int, end: int, blocks: tuple[bytes, ...], size: int
    ) -> AsyncIterator[bytes]:
        """Read whole blocks, check each one, then yield only the asked-for span."""
        if end < start:
            return
        # How many digests this version must carry, derived from the size its
        # row records rather than from the tuple itself. Trusting the tuple's
        # own length would leave every block past its end unchecked, which is
        # precisely the corruption this verifier exists to refuse: an absent or
        # truncated ``block_hashes`` is a refusal, never a skipped check.
        expected = _block_count(size)
        if len(blocks) != expected:
            raise ChecksumMismatch(
                f"{store_key}: {len(blocks)} recorded block digests for {expected} blocks"
            )
        first = start // BLOCK_BYTES
        last = end // BLOCK_BYTES
        for index in range(first, last + 1):
            block_start = index * BLOCK_BYTES
            block_end = block_start + BLOCK_BYTES - 1
            chunks: list[bytes] = []
            try:
                body = await self._store.get(store_key, range=(block_start, block_end))
                async for chunk in body:
                    chunks.append(chunk)
            except StoreError as exc:
                # A read the driver refuses outright is not a corrupt object and
                # not a bug here: it leaves typed, the way the write paths do.
                raise StoreUnavailable(f"the object store could not serve {store_key}") from exc
            block = b"".join(chunks)
            if not verify_block(index, block, blocks[index]):
                raise ChecksumMismatch(f"{store_key}: block {index} does not match")
            lo = max(start, block_start) - block_start
            hi = min(end, block_start + len(block) - 1) - block_start
            if hi >= lo:
                yield block[lo : hi + 1]

    def _info(self, version: FileVersion, *, unchanged: bool) -> VersionInfo:
        return VersionInfo(
            id=VersionId(version.id),
            seq=version.seq,
            size=version.size_bytes,
            content_hash=version.content_hash,
            block_hash=version.block_hash,
            mime=version.mime_sniffed,
            unchanged=unchanged,
        )


def _blocks(version: FileVersion) -> tuple[bytes, ...]:
    recorded: Iterable[Any] = version.version_metadata.get("block_hashes", ())
    return tuple(bytes.fromhex(str(digest)) for digest in recorded)


def _bounds(size: int, range: tuple[int, int] | None) -> tuple[int, int]:
    if range is None:
        return 0, size - 1
    start, end = range
    if start < 0 or end < start or (size and start >= size) or (not size and start > 0):
        raise InvalidRequest("files.part_out_of_range", f"range {range!r} is not satisfiable")
    return start, min(end, size - 1)


def _block_count(size: int) -> int:
    """How many 4 MiB blocks ``size`` bytes occupy; zero bytes occupy none."""
    return -(-size // BLOCK_BYTES)


def _verify_blocks(payload: bytes, blocks: tuple[bytes, ...], *, first: int) -> None:
    counted = _block_count(len(payload))
    if len(blocks) != counted:
        raise ChecksumMismatch(f"{len(blocks)} recorded block digests for {counted} blocks")
    for index, expected in enumerate(blocks, start=first):
        offset = index * BLOCK_BYTES
        block = payload[offset : offset + BLOCK_BYTES]
        if not verify_block(index, block, expected):
            raise ChecksumMismatch(f"block {index} does not match")


async def _slice(payload: bytes, start: int, end: int) -> AsyncIterator[bytes]:
    if end >= start:
        yield payload[start : end + 1]
