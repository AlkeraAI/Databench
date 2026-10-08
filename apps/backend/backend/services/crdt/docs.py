"""The Loro CRDT lane's document store: Postgres is the authority, the sandbox
judges, and nothing a client sends is stored as it was sent.

A write (:meth:`CrdtDocs.apply`) runs in the caller's transaction:

1. lock the ``crdt_docs`` row (create it from the type's seed if missing);
2. refuse a document in another epoch, a writer the type's
   ``authorize`` does not admit NOW, an update over the type's size cap, and an
   update this process has seen crash a worker twice;
3. have the sandbox judge the update against the cached document — loading it
   (or catching it up from the log) when the worker does not hold the
   committed position — retrying once on a fresh (warm) worker if one dies;
4. on ``ok``, append the CANONICAL delta to ``crdt_updates``, move the row's
   position, vector and projection, and emit the ``doc.op`` every replica fans
   out (the delta inline up to :data:`CRDT_MAX_INLINE_BYTES`, by reference
   past it).

The caller commits, then calls :meth:`CrdtDocs.after_commit`, which moves the
worker's cache onto the committed delta and folds the log into a snapshot when
it has grown. The ack the peer receives is the vector after the commit, so an
acknowledged edit is never one a crash could lose.

A timeout is never a verdict. Every sandbox request goes through
:meth:`CrdtDocs.sandbox_request`, which turns one that outlived its budget into
``crdt_busy`` with a wait (:mod:`.slow`): the update stays the sender's to
send again, nothing is poisoned and nobody is counted against. Only the
validator's own refusal, or a worker dying on the same bytes twice, refuses
an update.

What cannot be done inside the caller's transaction runs in the background on
its own session: compaction, restarting the history (a new epoch from the
current text) when the document fills, and quarantine — re-seeding a document
whose stored history does not rebuild — each under the same row lock, each
announcing a ``reload`` so every peer rebases onto the new epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from alkera_core.db.locking import LockRank, advisory_key, advisory_xact_lock, held_ranks, lock_rows
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, emit
from alkera_core.events.types import Entity
from alkera_core.logging import get_logger
from alkera_core.models import CrdtDoc, CrdtUpdate, User
from alkera_core.models.crdt_doc import crdt_peer_seq
from alkera_core.schemas.realtime import (
    CRDT_MAX_INLINE_BYTES,
    CRDT_MAX_TRANSFER_BYTES,
    SERVER_PEER_ID,
    CrdtSaving,
    CrdtUpdatePayload,
    DocEnvelope,
    DocType,
    ReloadPayload,
    encode_b64,
)
from sqlalchemy import delete, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

# Every module that registers a document type is imported here, so a store
# built with the default registry serves each of them.
from backend.services.crdt import (
    announce,
    notebook_type,  # noqa: F401
    sweeper,
)
from backend.services.crdt.answers import Applied, Sync
from backend.services.crdt.errors import CorruptHistoryError, CrdtError, busy_for, safe_error
from backend.services.crdt.loro_peers import PEER_LEASE, PEERS_PER_PERSON, LoroPeers
from backend.services.crdt.notebook_peers import NotebookPeers
from backend.services.crdt.registry import (
    Access,
    CrdtDocType,
    CrdtRegistry,
    DocRef,
    Seed,
    SourceStamp,
)
from backend.services.crdt.sandbox.pool import (
    SandboxBusyError,
    SandboxCrashedError,
    SandboxPool,
    SandboxRefusedError,
    SandboxUnavailableError,
)
from backend.services.crdt.sandbox.protocol import MAX_PEERS, Frame, decode_projection
from backend.services.crdt.sessions import IDLE_SECONDS, WRITE_BACK_DELAY_SECONDS, SessionSync
from backend.services.crdt.slow import SlowWork
from backend.services.crdt.switch import LiveSwitch
from backend.services.crdt.text_peers import TextPeers
from backend.services.infra import now as _now
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.sharing import SharedRungCache

log = get_logger(__name__)


def _when_committed(db: AsyncSession, action: Callable[[], None]) -> None:
    """Run ``action`` once ``db``'s transaction commits, and never if it
    rolls back first."""
    settled = False

    def committed(_session: Any) -> None:
        nonlocal settled
        if not settled:
            settled = True
            action()

    def rolled_back(_session: Any) -> None:
        nonlocal settled
        settled = True

    event.listen(db.sync_session, "after_commit", committed, once=True)
    event.listen(db.sync_session, "after_rollback", rolled_back, once=True)


#: The hub event a committed update becomes on every replica (re-exported).
CRDT_UPDATE_EVENT_TYPE = announce.CRDT_UPDATE_EVENT_TYPE
#: How often (seconds) a replica's sweep also deletes the sessions nobody needs.
EXPIRE_EVERY = 600.0
#: How long an update that crashed a worker twice is refused unseen.
POISON_SECONDS = 3600.0
#: How many load crashes at one position before the document is re-seeded.
LOAD_CRASHES_BEFORE_QUARANTINE = 2
#: How soon a session pass refused as busy runs again.
BUSY_RETRY_SECONDS = 0.5
#: The least time between two restarts of one document's history. Each one
#: reloads every tab, so a document that fills again sooner is answered busy.
ROTATION_INTERVAL_SECONDS = 60.0

Maintenance = Literal[
    "compact", "rotate", "quarantine", "write_back", "merge", "idle", "normalize", "warm"
]
#: The passes of a session with a source (:mod:`backend.services.crdt.sessions`).
_SESSION_PASSES: frozenset[str] = frozenset({"write_back", "merge", "idle"})


def admission_slots_for(*, pool_size: int) -> int:
    """The admissions a process runs with: half its database pool (each
    admitted request holds a connection while it waits on the sandbox, and
    the other half stays free for every other route), and at least one."""
    return max(1, pool_size // 2)


@dataclass
class CrdtDocs(LoroPeers):
    """See the module docstring. Every collaborator is injected: the pool, the
    registry, the budgets, the clock and the session factory the background
    work opens its own sessions from."""

    pool: SandboxPool
    registry: CrdtRegistry = field(default_factory=CrdtRegistry)
    validate_budget: float = 2.0
    load_budget: float = 10.0
    #: The most one sandbox request may take while this transaction holds a
    #: document's row. Everyone typing in the document waits on that row, so
    #: a request past it answers busy and lets go rather than make them wait
    #: out their own lock timeout.
    locked_budget: float = 3.0
    clock: Callable[[], float] = time.monotonic
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal
    #: How long after a commit the log is looked at for compaction.
    maintenance_delay: float = 2.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Requests this process lets hold a database session while they wait on
    #: the sandbox (see :meth:`admitted`).
    admission_slots: int = 8
    #: How long after an edit a document with a source is written back, and
    #: how long a session nobody writes to waits before it is compacted.
    write_back_delay: float = WRITE_BACK_DELAY_SECONDS
    idle_delay: float = IDLE_SECONDS
    #: Whether this store runs the session passes on its own triggers. A test
    #: that drives them by hand turns this off, so no background pass races it.
    run_sessions: bool = True
    _admission: asyncio.Semaphore = field(init=False)
    _poison: dict[bytes, float] = field(default_factory=dict, init=False)
    _load_crashes: dict[tuple[str, int, int], int] = field(default_factory=dict, init=False)
    #: Sandbox work that outlived its budget, and how long it waits (:mod:`.slow`).
    slow: SlowWork = field(init=False)
    #: When each document's history was last restarted by this process.
    _rotated: dict[str, float] = field(default_factory=dict, init=False)
    _tasks: dict[tuple[str, str], asyncio.Task[None]] = field(default_factory=dict, init=False)
    _closed: bool = field(default=False, init=False)
    _sweeper: asyncio.Task[None] | None = field(default=None, init=False)
    #: The orgs a sweep looks in; ``None`` is every org (a deployment).
    sweep_orgs: frozenset[uuid.UUID] | None = None
    _expired_at: datetime | None = field(default=None, init=False)
    #: Passes asked for again while one of the same kind was running.
    _again: set[tuple[str, str]] = field(default_factory=set, init=False)
    sessions: SessionSync = field(init=False)
    #: Writers that speak whole texts (the machine holding a file's folder).
    peers: TextPeers = field(init=False)
    #: Writers that speak notebook operations (an agent, a box).
    notebooks: NotebookPeers = field(init=False)
    #: Whether live editing is on for an org (:mod:`.switch`).
    switch: LiveSwitch = field(init=False)

    def __post_init__(self) -> None:
        self.sessions = SessionSync(self)
        self.peers = TextPeers(self)
        self.notebooks = NotebookPeers(self)
        self.switch = LiveSwitch(self.session_factory)
        # Read through the store, so a clock set on it later is the one used.
        self.slow = SlowWork(lambda: self.clock())
        self._admission = asyncio.Semaphore(self.admission_slots)

    @contextlib.asynccontextmanager
    async def admitted(self) -> AsyncIterator[None]:
        """Hold one of this process's admissions for a request that opens a
        session, locks the document and waits on the sandbox. The sandbox
        serves a few requests at a time, and every one waiting for it holds a
        pooled connection and the row lock: without a bound in front of the
        session, one slow document could hold the whole pool and stall every
        route in the process. A request past the bound waits its turn here,
        holding no connection: under load the lane is slow, never refusing."""
        await self._admission.acquire()
        try:
            yield
        finally:
            self._admission.release()

    # -- types and access ----------------------------------------------------

    def type_of(self, ref: DocRef) -> CrdtDocType:
        doc_type = self.registry.get(ref.doc_type)
        if doc_type is None:
            raise CrdtError("not_found", f"{ref.doc_type} is not a CRDT document type")
        return doc_type

    async def access(
        self,
        db: AsyncSession,
        ref: DocRef,
        *,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        rungs: SharedRungCache | None = None,
    ) -> Access:
        """What ``user`` may do with ``ref`` now; ``not_found`` when they may
        not read it. ``agent_id`` is the agent the socket asserts (it bars
        writing); ``machine_id`` is the machine it was VERIFIED to be, the only
        identity compared with a chat's binding."""
        doc_type = self.type_of(ref)
        access = await doc_type.authorize(
            db,
            ref=ref,
            user=user,
            ent=ent,
            agent_id=agent_id,
            machine_id=machine_id,
            rungs=rungs,
        )
        if not access.can_read:
            raise CrdtError("not_found")
        return access

    # -- rows ----------------------------------------------------------------

    @staticmethod
    def _select(ref: DocRef) -> Any:
        return select(CrdtDoc).where(
            CrdtDoc.org_id == ref.org_id,
            CrdtDoc.doc_type == ref.stored_type,
            CrdtDoc.doc_id == ref.doc_id,
        )

    async def row(self, db: AsyncSession, ref: DocRef) -> CrdtDoc | None:
        """The document row as committed, read without a lock: for a pass that
        does its slow work (the sandbox, the source) before it takes one."""
        row: CrdtDoc | None = (
            await db.execute(self._select(ref).execution_options(populate_existing=True))
        ).scalar_one_or_none()
        return row

    async def locked(self, db: AsyncSession, ref: DocRef) -> CrdtDoc | None:
        """The document row, locked for this transaction, as committed now:
        a copy the session already holds from an earlier read is refreshed,
        never trusted (it may predate a write committed since)."""
        try:
            locked = await lock_rows(
                db,
                LockRank.CRDT_DOC,
                self._select(ref).execution_options(populate_existing=True),
            )
        except DBAPIError as exc:
            # Somebody held the row past this request's wait: busy, which the
            # peer (or a session pass) sends again, never a failure that drops
            # what it sent.
            busy = busy_for(exc)
            if busy is None:
                raise
            raise busy from exc
        row: CrdtDoc | None = locked.scalar_one_or_none()
        return row

    async def ensure(self, db: AsyncSession, ref: DocRef) -> CrdtDoc:
        """The document row, locked for this transaction, created from the
        type's seed under epoch 1 when it does not exist yet.

        The type's own lock comes first. Two first opens race on creation: an
        advisory lock on the document serialises them, and the loser finds the
        winner's row. A row that exists is never re-seeded."""
        doc_type = self.type_of(ref)
        await doc_type.lock(db, ref=ref)
        doc = await self.locked(db, ref)
        if doc is not None:
            return doc
        await advisory_xact_lock(db, advisory_key("crdt-doc", ref.key))
        doc = await self.locked(db, ref)
        if doc is not None:
            return doc
        seed = await doc_type.seed(db, ref=ref)
        incarnation = uuid.uuid4()
        seeded = await self._seed(
            ref,
            {
                "op": "seed",
                "key": f"{ref.key}@{incarnation.hex}",
                "epoch": 1,
                "rules": doc_type.rules,
                "peer": await self.seed_peer(db),
            },
            (seed.text.encode("utf-8"),),
            budget=self.load_budget,
        )
        snapshot, vv, projection, base_vv = seeded.blobs
        stamp = seed.stamp
        await db.execute(
            pg_insert(CrdtDoc)
            .values(
                org_id=ref.org_id,
                doc_type=ref.stored_type,
                doc_id=ref.doc_id,
                incarnation=incarnation,
                epoch=1,
                log_seq=0,
                vv=vv,
                snapshot=snapshot,
                snapshot_log_seq=0,
                snapshot_bytes=len(snapshot),
                log_bytes=0,
                doc_schema=doc_type.doc_schema,
                loro_format=str(seeded.header.get("loro") or ""),
                projection={**decode_projection(projection), "by_user_id": None, "at": None},
                seeded_from=seed.source,
                source_etag=None if stamp is None else stamp.etag,
                source_version_id=None if stamp is None else stamp.version_id,
                source_sha256=None if stamp is None else stamp.sha256,
                source_vv=None if stamp is None else base_vv,
                source_epoch=None if stamp is None else 1,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "doc_type", "doc_id"])
        )
        doc = await self.locked(db, ref)
        if doc is None:  # pragma: no cover - inserted just now, under the lock
            raise CrdtError("not_found")
        return doc

    @staticmethod
    def cache_key(ref: DocRef, doc: CrdtDoc) -> str:
        """What a worker files this document under: the document AND which
        life of it the row is (see ``CrdtDoc.incarnation``)."""
        return f"{ref.key}@{doc.incarnation.hex}"

    @staticmethod
    async def seed_peer(db: AsyncSession) -> int:
        """A Loro peer for one epoch's seed, from the sequence every tab's peer
        comes from. A fixed seed peer would write the same operation ids in
        every epoch, and an update from one epoch would then import into
        another as if it belonged there, its text landing wherever the
        colliding ids point. Minted, they never collide: a stray update
        from another epoch can only ever be missing history."""
        minted = await db.scalar(select(crdt_peer_seq.next_value()))
        if minted is None:  # pragma: no cover - nextval always answers
            raise CrdtError("internal", "the peer sequence answered nothing")
        return int(minted)

    # -- the sandbox ---------------------------------------------------------

    async def _seed(
        self,
        ref: DocRef,
        header: dict[str, Any],
        blobs: tuple[bytes, ...] | list[bytes],
        *,
        budget: float,
    ) -> Frame:
        """A seed request: a worker that dies on it, or refuses it, is a
        refusal the peer can retry, never an exception through the socket."""
        try:
            return await self.sandbox_request(ref, header, blobs, budget=budget)
        except SandboxCrashedError as exc:
            raise CrdtError("crdt_busy", "the sandbox restarted", retry_after_ms=250) from exc
        except SandboxRefusedError as exc:
            if exc.code == "not_editable":
                # The type will not take the source's text (a notebook in a
                # newer major format, or past the caps): it is read only.
                raise CrdtError(
                    "not_editable", "this file cannot be edited live", reason=exc.message
                ) from exc
            log.error("crdt.seed.refused", doc=ref.key, code=exc.code, message=exc.message)
            raise CrdtError("crdt_unsupported", "the document cannot be started") from exc

    async def sandbox_request(
        self,
        ref: DocRef,
        header: dict[str, Any],
        blobs: tuple[bytes, ...] | list[bytes],
        *,
        budget: float,
        slow_key: Any = None,
        background: bool = False,
    ) -> Frame:
        """One sandbox request, its failures turned into the lane's refusals.
        The only path to the pool, so the one place a timeout is answered
        (:meth:`SlowWork.send`): busy, never a verdict. ``slow_key`` names
        the work (the document and operation by default)."""
        op = str(header.get("op") or "")
        if LockRank.CRDT_DOC in held_ranks():
            budget = min(budget, self.locked_budget)
        try:
            return await self.slow.send(
                slow_key if slow_key is not None else (ref.key, op),
                lambda: self.pool.request(ref.key, header, blobs, budget_seconds=budget),
                op=op,
                doc=ref.key,
                budget=budget,
                background=background,
            )
        except SandboxBusyError as exc:
            raise CrdtError(
                "crdt_busy",
                "the document is busy; send again shortly",
                retry_after_ms=exc.retry_after_ms,
            ) from exc
        except SandboxUnavailableError as exc:
            raise CrdtError("crdt_unsupported", "the CRDT lane is unavailable") from exc

    async def _log(self, db: AsyncSession, doc: CrdtDoc, after: int) -> list[bytes]:
        rows = (
            await db.execute(
                select(CrdtUpdate.log_seq, CrdtUpdate.data)
                .where(
                    CrdtUpdate.org_id == doc.org_id,
                    CrdtUpdate.doc_type == doc.doc_type,
                    CrdtUpdate.doc_id == doc.doc_id,
                    CrdtUpdate.epoch == doc.epoch,
                    CrdtUpdate.log_seq > after,
                )
                .order_by(CrdtUpdate.log_seq)
            )
        ).all()
        expected = list(range(after + 1, doc.log_seq + 1))
        if [int(row.log_seq) for row in rows] != expected:
            raise CorruptHistoryError("the update log has a gap")
        return [bytes(row.data) for row in rows]

    async def _bring_up(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, need: Frame, *, background: bool = False
    ) -> None:
        """Put the worker at the row's committed position: the log after
        where it stands when it holds this epoch behind, a full load otherwise.
        A worker that dies or refuses while catching up failed on stored
        history, never on the request being judged, so the full load follows,
        and a crash there counts against the document."""
        doc_type = self.type_of(ref)
        position = (ref.key, doc.epoch, doc.log_seq)
        # Under the document's row a catch-up or load gets the short locked
        # budget (:meth:`sandbox_request`), so everyone typing waits for less
        # than their own lock wait. One that does not fit it answers busy and
        # is finished by a pass that holds nothing, so the next holder finds
        # the worker at the row instead of starting the same work again under
        # it, and being refused busy again, for as long as it is slow.
        under_row = LockRank.CRDT_DOC in held_ranks()
        at = need.header.get("at")
        if (
            isinstance(at, list)
            and len(at) == 2
            and at[0] == doc.epoch
            and isinstance(at[1], int)
            and doc.snapshot_log_seq <= at[1] < doc.log_seq
        ):
            tail = await self._log(db, doc, at[1])
            try:
                moved = await self.sandbox_request(
                    ref,
                    {
                        "op": "catchup",
                        "key": self.cache_key(ref, doc),
                        "epoch": doc.epoch,
                        "log_seq": at[1],
                    },
                    tail,
                    budget=self.load_budget,
                    slow_key=position,
                    background=background,
                )
            except CrdtError as exc:
                if under_row and exc.code == "crdt_busy":
                    self._schedule(ref, "warm")
                raise
            except (SandboxCrashedError, SandboxRefusedError) as exc:
                log.warning("crdt.catchup.failed", doc=ref.key, error=f"{type(exc).__name__}")
            else:
                if moved.header.get("advanced") is True:
                    return
        updates = await self._log(db, doc, doc.snapshot_log_seq)
        try:
            await self.sandbox_request(
                ref,
                {
                    "op": "load",
                    "key": self.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                    "rules": doc_type.rules,
                    "expect_vv": True,
                },
                [bytes(doc.snapshot), bytes(doc.vv), *updates],
                budget=self.load_budget,
                # Slow is not corrupt: a load that times out answers busy
                # (:meth:`sandbox_request`) and never counts toward quarantine, so a busy
                # host can never cost a document its history.
                slow_key=position,
                background=background,
            )
        except CrdtError as exc:
            if under_row and exc.code == "crdt_busy":
                self._schedule(ref, "warm")
            raise
        except SandboxCrashedError as exc:
            crashes = self._load_crashes.get(position, 0) + 1
            self._load_crashes[position] = crashes
            if crashes >= LOAD_CRASHES_BEFORE_QUARANTINE:
                raise CorruptHistoryError("loading it crashed the sandbox") from exc
            raise CrdtError(
                "crdt_busy", "the sandbox restarted while loading", retry_after_ms=250
            ) from exc
        except SandboxRefusedError as exc:
            if exc.code == "corrupt":
                raise CorruptHistoryError(exc.message) from exc
            raise
        self._load_crashes.pop(position, None)

    async def at_position(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        header: dict[str, Any],
        blobs: list[bytes],
        *,
        maintenance: bool = False,
        slow_key: Any = None,
    ) -> Frame:
        """A request naming the row's position, the worker brought up to it
        when it answers ``need``. ``maintenance`` is work the server started
        (a write back's read, a merge, a compaction) that nobody waits on
        keystroke by keystroke: it runs on the rebuild budget, not the one a
        single update is judged on. A worker that dies on the request itself is
        replaced and the request sent once more; a second death raises
        :class:`SandboxCrashedError` — the request, not the document, is what
        kills it. A worker that dies while LOADING raises ``crdt_busy`` (or,
        at the same position again, :class:`CorruptHistoryError`). Work that
        outlives its budget raises ``crdt_busy`` (:meth:`sandbox_request`). ``slow_key``
        names the work for that (an update's digest)."""
        crashed = False
        for _ in range(4):
            try:
                budget = self.load_budget if maintenance else self.validate_budget
                reply = await self.sandbox_request(
                    ref, header, blobs, budget=budget, slow_key=slow_key
                )
            except SandboxCrashedError:
                if crashed:
                    raise
                crashed = True
                continue
            if reply.header.get("need") is not True:
                return reply
            await self._bring_up(db, ref, doc, reply)
        raise CrdtError("crdt_busy", "the sandbox could not hold the document", retry_after_ms=500)

    async def warm(self, db: AsyncSession, ref: DocRef) -> None:
        """Load the document's committed history into its worker, holding no
        lock: what a request that found the worker cold while holding the row
        asks for instead of loading under it."""
        doc = await self.row(db, ref)
        if doc is None or doc.snapshot is None:
            return
        # Nobody waits on this pass, so an earlier timeout's wait does not
        # hold it back, and once it loads the wait is over for every holder.
        await self._bring_up(db, ref, doc, Frame(header={}), background=True)

    async def content(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        *,
        at: bytes | None = None,
        maintenance: bool = False,
    ) -> str:
        """The document's content text at the row's committed position (or at
        the earlier version ``at`` names), read from the sandbox, which loads
        it when it does not hold it. Raises :class:`CorruptHistoryError` when
        the stored history does not rebuild."""
        reply = await self.at_position(
            db,
            ref,
            doc,
            {
                "op": "content",
                "key": self.cache_key(ref, doc),
                "epoch": doc.epoch,
                "log_seq": doc.log_seq,
                "rules": self.type_of(ref).rules,
            },
            [] if at is None else [at],
            maintenance=maintenance,
        )
        return reply.blobs[0].decode("utf-8")

    async def latest(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, *, maintenance: bool = False
    ) -> tuple[str, bytes]:
        """The document's content and version vector at the position the
        worker holds, the row's or a later committed one: what a write back
        or a text peer's read takes, without checking out an older version."""
        reply = await self.at_position(
            db,
            ref,
            doc,
            {
                "op": "latest",
                "key": self.cache_key(ref, doc),
                "epoch": doc.epoch,
                "log_seq": doc.log_seq,
                "rules": self.type_of(ref).rules,
            },
            [],
            maintenance=maintenance,
        )
        text, vv = reply.blobs
        return text.decode("utf-8"), vv

    # -- open ----------------------------------------------------------------

    async def sync(
        self, db: AsyncSession, ref: DocRef, *, since: bytes | None, epoch_seen: int | None
    ) -> Sync:
        """What a peer opening ``ref`` is missing. The caller has already
        decided it may read (:meth:`access`). A session with a source is
        opened again from it when it was closed, and checked for a change
        made to its source while it was not watched. A document that exists is
        read without the row lock: a hello takes nothing a writer waits on."""
        seen = await self.row(db, ref)
        doc = seen if seen is not None else await self.ensure(db, ref)
        if self.type_of(ref).source is not None:
            if await self.sessions.reopen(db, ref):
                doc = await self.ensure(db, ref)
            self._schedule(ref, "merge")
            # Told once the open commits: the first open creates the row in
            # this transaction, and a notice that reads before the commit
            # finds no document, tells nobody, and holds back the next
            # open's for a minute.
            _when_committed(db, lambda: self.peers.opened(ref))
        header = {
            "op": "export",
            "key": self.cache_key(ref, doc),
            "epoch": doc.epoch,
            "log_seq": doc.log_seq,
        }
        blobs: list[bytes] = []
        if since is not None and epoch_seen == doc.epoch:
            header["since"] = True
            blobs = [since]
        try:
            reply = await self.at_position(db, ref, doc, header, blobs)
        except CorruptHistoryError as exc:
            self._schedule(ref, "quarantine", reason=str(exc))
            raise CrdtError(
                "crdt_busy", "the document is being repaired", retry_after_ms=1000
            ) from exc
        except SandboxCrashedError as exc:
            raise CrdtError("crdt_busy", "the sandbox restarted", retry_after_ms=250) from exc
        except SandboxRefusedError as exc:
            log.error("crdt.sync.refused", doc=ref.key, code=exc.code, message=exc.message)
            raise CrdtError("crdt_unsupported", "the document cannot be served") from exc
        data, vv = reply.blobs
        if len(data) > CRDT_MAX_TRANSFER_BYTES:
            # More than one transfer may carry: a new epoch from the current
            # content is far smaller, and every tab is moved onto it.
            self._schedule(ref, "rotate", reason="sync_too_large")
            raise CrdtError("crdt_busy", "the document is being restarted", retry_after_ms=1000)
        return Sync(
            mode=str(reply.header.get("mode")),
            data=data,
            vv=vv,
            epoch=doc.epoch,
            doc_schema=doc.doc_schema,
            saving=self.saving_of(ref, doc),
        )

    def saving_of(self, ref: DocRef, doc: CrdtDoc) -> CrdtSaving | None:
        """Whether ``doc``'s edits are reaching its source, as its row
        records it (``save_paused_reason``, set and cleared by whichever
        replica's write back was refused or landed); ``None`` for a type
        that rests nowhere. Read at every sync and reload, so a tab that
        opens or reconnects after saving paused is told without waiting on
        a notice it was not there for."""
        if self.type_of(ref).source is None:
            return None
        return CrdtSaving.of(doc.save_paused_reason)

    # -- write ---------------------------------------------------------------

    def _poisoned(self, digest: bytes) -> bool:
        now = self.clock()
        for key in [k for k, until in self._poison.items() if until <= now]:
            del self._poison[key]
        return digest in self._poison

    async def apply(
        self,
        db: AsyncSession,
        ref: DocRef,
        *,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        epoch: int,
        peer: int,
        update_id: str,
        update: bytes,
        socket_peer_id: str,
        peers: frozenset[int] | None = None,
    ) -> Applied:
        """Judge and store one update; see the module docstring. ``peers`` is
        the writer's peer set when the caller already holds it
        (:meth:`peers_of`, read at its hello); read here otherwise."""
        doc_type = self.type_of(ref)
        access = await self.access(
            db,
            ref,
            user=user,
            ent=ent,
            agent_id=agent_id,
            machine_id=machine_id,
        )
        if not access.can_write:
            raise CrdtError("forbidden", "writing here needs edit rights on the chat")
        doc = await self.ensure(db, ref)
        if epoch != doc.epoch:
            raise CrdtError(
                "stale_epoch",
                "the document moved on",
                epoch=doc.epoch,
                saving=self.saving_of(ref, doc),
            )
        if len(update) > doc_type.limits.max_update_bytes:
            raise CrdtError(
                "crdt_rejected",
                f"an update is at most {doc_type.limits.max_update_bytes} bytes",
                reason="update_too_large",
            )
        digest = hashlib.sha256(update).digest()
        if self._poisoned(digest):
            raise CrdtError("crdt_rejected", "this update cannot be read", reason="poisoned")
        mine = set(peers) if peers is not None else await self.peers_of(db, ref, user=user)
        if len(mine | {peer}) > MAX_PEERS:
            # More peers than a write may name: a new epoch, which only the
            # peers live sockets hold outlive, lets the writer go on.
            self._rotate_or_wait(ref)
            self._schedule(ref, "rotate", reason="too_many_peers")
            raise CrdtError(
                "doc_full",
                "the document's history is full; it is being restarted",
                reason="too_many_peers",
            )
        header = {
            "op": "validate",
            "key": self.cache_key(ref, doc),
            "epoch": doc.epoch,
            "log_seq": doc.log_seq,
            "rules": doc_type.rules,
            "peers": sorted(mine | {peer}),
        }
        try:
            # A timeout answers busy inside (:meth:`sandbox_request`), keyed by the bytes:
            # the update stays the sender's, and is never poisoned for it.
            verdict = await self.at_position(db, ref, doc, header, [update], slow_key=digest)
        except CorruptHistoryError as exc:
            self._schedule(ref, "quarantine", reason=str(exc))
            raise CrdtError(
                "crdt_busy", "the document is being repaired", retry_after_ms=1000
            ) from exc
        except SandboxCrashedError as exc:
            # Two workers died judging these bytes: the first, and the fresh,
            # warm one ``at_position`` sent them to again.
            self._poison[digest] = self.clock() + POISON_SECONDS
            log.error(
                "crdt.update.poisoned",
                doc=ref.key,
                sha256=digest.hex(),
                user_id=str(user.id),
                reason="crash",
            )
            raise CrdtError(
                "crdt_rejected", "this update crashed the validator", reason="validator_crash"
            ) from exc
        except SandboxRefusedError as exc:
            raise CrdtError("crdt_rejected", exc.message, reason=exc.code) from exc
        outcome = verdict.header.get("outcome")
        delta, vv, projection = verdict.blobs
        if outcome == "dup":
            return Applied(changed=False, epoch=doc.epoch, log_seq=doc.log_seq, vv=vv)
        if outcome == "resync":
            raise CrdtError("crdt_resync", "the update depends on history the server lacks")
        if outcome != "ok":
            raise CrdtError(
                "crdt_rejected",
                "the update breaks this document's rules",
                reason=str(verdict.header.get("reason") or "rejected"),
            )
        if doc.snapshot_bytes + doc.log_bytes + len(delta) > doc_type.limits.max_doc_bytes:
            self._rotate_or_wait(ref)
            self._schedule(ref, "rotate", reason="doc_full")
            raise CrdtError("doc_full", "the document's history is full; it is being restarted")
        applied = await self.append(
            db,
            ref,
            doc,
            delta=delta,
            vv=vv,
            projection=decode_projection(projection),
            peer=peer,
            update_id=update_id,
            author=user,
            agent_id=agent_id,
            socket_peer_id=socket_peer_id,
            access=access,
        )
        notes = verdict.header.get("notes")
        if not isinstance(notes, dict) or not notes:
            return applied
        on_write = getattr(doc_type, "on_write", None)
        if on_write is not None:
            await on_write(db, ref=ref, author=user, agent_id=agent_id, notes=notes)
        return dataclasses.replace(applied, notes=notes)

    async def append(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        *,
        delta: bytes,
        vv: bytes,
        projection: Mapping[str, Any],
        peer: int,
        update_id: str,
        author: User | None,
        agent_id: str | None,
        socket_peer_id: str,
        access: Access,
    ) -> Applied:
        """Store a judged delta at the next log position of the locked row
        ``doc`` and build the broadcast it becomes. ``author`` is the person
        who wrote it, ``None`` for a change the server made (an outside
        change to a document's source merged in), which keeps the row's last
        writer as it was."""
        log_seq = doc.log_seq + 1
        db.add(
            CrdtUpdate(
                org_id=ref.org_id,
                doc_type=ref.stored_type,
                doc_id=ref.doc_id,
                epoch=doc.epoch,
                log_seq=log_seq,
                data=delta,
                size=len(delta),
                sha256=hashlib.sha256(delta).digest(),
                update_id=update_id,
                loro_peer=peer,
                author_user_id=None if author is None else author.id,
                agent_id=agent_id,
            )
        )
        previous = doc.projection if isinstance(doc.projection, dict) else {}
        doc.log_seq = log_seq
        doc.vv = vv
        doc.log_bytes = doc.log_bytes + len(delta)
        doc.projection = {
            **projection,
            "by_user_id": str(author.id) if author is not None else previous.get("by_user_id"),
            "at": _now().isoformat(),
        }
        await db.flush()
        payload = CrdtUpdatePayload(
            update_id=update_id,
            loro_peer=peer,
            user_id=None if author is None else str(author.id),
            vv_b64=encode_b64(vv),
            **(
                {"data_b64": encode_b64(delta)}
                if len(delta) <= CRDT_MAX_INLINE_BYTES
                else {"log_ref": log_seq}
            ),
        )
        envelope = DocEnvelope(
            doc_id=ref.doc_id,
            doc_type=cast(DocType, ref.doc_type),
            epoch=doc.epoch,
            peer_id=socket_peer_id,
            seq=0,
            kind="crdt",
            payload=payload.model_dump(mode="json", exclude_none=True),
        )
        # No outbox row: the update is durable in ``crdt_updates`` and
        # announced after the commit (:meth:`broadcast`).
        rows = log_seq - doc.snapshot_log_seq
        return Applied(
            changed=True,
            cache_key=self.cache_key(ref, doc),
            team_id=access.team_id,
            visibility=access.visibility,
            epoch=doc.epoch,
            log_seq=log_seq,
            vv=vv,
            delta=delta,
            envelope=envelope,
            log_bytes=doc.log_bytes,
            log_rows=rows,
            snapshot_bytes=doc.snapshot_bytes,
        )

    def _rotate_or_wait(self, ref: DocRef) -> None:
        """Raise ``crdt_busy`` when ``ref`` was restarted less than
        :data:`ROTATION_INTERVAL_SECONDS` ago: a writer that fills the history
        again at once (typing and cutting large pastes) waits out the interval
        rather than reloading every tab every few updates."""
        last = self._rotated.get(ref.key)
        if last is None:
            return
        wait = last + ROTATION_INTERVAL_SECONDS - self.clock()
        if wait > 0:
            raise CrdtError(
                "crdt_busy",
                "the document's history was restarted moments ago; send again shortly",
                reason="rotation_interval",
                retry_after_ms=max(1, int(wait * 1000)),
            )

    async def _emit(
        self,
        db: AsyncSession,
        ref: DocRef,
        access: Access,
        envelope: DocEnvelope,
        *,
        actor: Mapping[str, Any],
        version: int,
    ) -> None:
        await emit(
            db,
            org_id=ref.org_id,
            type=EventType.DOC_OP,
            entity=Entity.DOC,
            entity_id=ref.channel,
            version=version,
            payload={
                "envelope": envelope.model_dump(mode="json"),
                "team_id": str(access.team_id) if access.team_id else None,
                "relay": False,
            },
            visibility=access.visibility,
            actor=actor,
        )

    async def after_commit(self, ref: DocRef, applied: Applied) -> None:
        """The update committed: move the worker onto it, and fold the log
        when it has grown. Never raises — a worker that cannot follow drops
        the document and loads it on the next request."""
        if not applied.changed:
            return
        with contextlib.suppress(CrdtError, SandboxCrashedError, SandboxRefusedError):
            await self.sandbox_request(
                ref,
                {
                    "op": "advance",
                    "key": applied.cache_key,
                    "epoch": applied.epoch,
                    "log_seq": applied.log_seq,
                },
                [applied.delta],
                budget=self.validate_budget,
            )
        limits = self.type_of(ref).limits
        if (
            applied.log_rows >= limits.compact_log_rows
            or applied.log_bytes >= limits.compact_log_bytes
        ):
            self._schedule(ref, "compact")
        if getattr(self.type_of(ref), "diagnoses_graph", False):
            self.notebooks.changed(ref)
        if applied.notes.get("normal") is False:
            # A person's update left the document out of normal form (two
            # tabs placing one cell): the server peer puts it back.
            self._schedule(ref, "normalize")
        if self.type_of(ref).source is not None:
            self._schedule(ref, "write_back")
            self.peers.changed(ref)

    def left(self, ref: DocRef) -> None:
        """A writer left ``ref``: write it back now, and compact the session
        once nobody has written to it for a while."""
        if self.type_of(ref).source is None:
            return
        self._schedule(ref, "write_back", delay=0.0)
        self._schedule(ref, "idle")

    def check_source(self, ref: DocRef) -> None:
        """Something says ``ref``'s source may have changed, or a held session
        is due its periodic look: merge any outside change, and write back
        anything an earlier pass missed."""
        if self.type_of(ref).source is None:
            return
        self._schedule(ref, "merge")
        self._schedule(ref, "write_back")

    async def broadcast(self, ref: DocRef, applied: Applied) -> None:
        """Announce a committed update to every replica (:func:`announce.broadcast`)."""
        await announce.broadcast(self.session_factory, ref, applied)

    async def announce_saving(
        self, ref: DocRef, *, epoch: int, paused: bool, reason: str = ""
    ) -> None:
        """Say whether edits reach the source (:func:`announce.saving`)."""
        await announce.saving(
            self.session_factory, self.type_of(ref), ref, epoch=epoch, paused=paused, reason=reason
        )

    async def ephemeral(self, ref: DocRef, *, peer: int, data: bytes) -> bytes:
        """``data`` re-encoded when it only moves ``peer``'s own caret."""
        relayed, _ = await self.caret(ref, peer=peer, data=data)
        return relayed

    async def caret(self, ref: DocRef, *, peer: int, data: bytes) -> tuple[bytes, dict[str, Any]]:
        """:meth:`ephemeral`, with what the type lets the server know of the
        caret (a notebook's: the cell it is in; nothing for a text type)."""
        doc_type = self.type_of(ref)
        try:
            reply = await self.sandbox_request(
                ref,
                {"op": "ephemeral", "rules": doc_type.rules, "peer": peer},
                [data],
                budget=self.validate_budget,
            )
        except SandboxCrashedError as exc:
            raise CrdtError("crdt_busy", "the sandbox restarted", retry_after_ms=250) from exc
        except SandboxRefusedError as exc:
            raise CrdtError("crdt_rejected", exc.message, reason=exc.code) from exc
        facts = reply.header.get("caret")
        return reply.blobs[0], facts if isinstance(facts, dict) else {}

    async def logged_delta(
        self, db: AsyncSession, ref: DocRef, *, epoch: int, log_seq: int
    ) -> bytes | None:
        """A committed delta by its log position (a by-reference broadcast);
        ``None`` once compaction has folded it away."""
        data = (
            await db.execute(
                select(CrdtUpdate.data).where(
                    CrdtUpdate.org_id == ref.org_id,
                    CrdtUpdate.doc_type == ref.stored_type,
                    CrdtUpdate.doc_id == ref.doc_id,
                    CrdtUpdate.epoch == epoch,
                    CrdtUpdate.log_seq == log_seq,
                )
            )
        ).scalar_one_or_none()
        return None if data is None else bytes(data)

    # -- Loro peers (the rest are in loro_peers.py) ----------------------------

    async def reader_peer(self, db: AsyncSession) -> int:
        """A Loro peer id for a tab that may only read. Its client sets one
        on the document it holds, but it never writes, so nothing is stored
        or held for it: it is drawn from the same sequence, so it can never be
        anybody's writing peer, and it is never claimable."""
        return await self.seed_peer(db)

    # -- maintenance ---------------------------------------------------------

    def _delay(self, kind: Maintenance) -> float:
        if kind == "compact":
            return self.maintenance_delay
        if kind == "write_back":
            return self.write_back_delay
        if kind == "idle":
            return self.idle_delay
        return 0.0

    def _schedule(
        self, ref: DocRef, kind: Maintenance, *, reason: str = "", delay: float | None = None
    ) -> None:
        if self._closed or (kind in _SESSION_PASSES and not self.run_sessions):
            return
        task_key = (ref.key, kind)
        running = self._tasks.get(task_key)
        if running is not None and not running.done():
            if kind in _SESSION_PASSES:
                # A write committed while this pass held the row may not be in
                # what it read: it runs once more when it ends.
                self._again.add(task_key)
            return
        wait = self._delay(kind) if delay is None else delay
        task = asyncio.create_task(self._maintain(ref, kind, reason=reason, delay=wait))
        self._tasks[task_key] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._tasks.get(task_key) is done:
                self._tasks.pop(task_key, None)
            if task_key in self._again:
                self._again.discard(task_key)
                self._schedule(ref, kind, reason=reason)

        task.add_done_callback(finished)

    async def _maintain(self, ref: DocRef, kind: Maintenance, *, reason: str, delay: float) -> None:
        if delay:
            await self.sleep(delay)
        try:
            if kind in _SESSION_PASSES:
                await self._session_pass(ref, kind)
                return
            if kind == "normalize":
                await self.notebooks.normalize(ref)
                return
            if kind == "warm":
                async with self.admitted(), self.session_factory() as db:
                    await self.warm(db, ref)
                    await db.commit()
                return
            async with self.admitted(), self.session_factory() as db:
                if kind == "compact":
                    await self.compact(db, ref)
                else:
                    await self.restart(
                        db, ref, reason=reason or kind, quarantine=kind == "quarantine"
                    )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the next commit schedules it again
            log.warning(
                "crdt.maintenance.failed",
                doc=ref.key,
                kind=kind,
                error=safe_error(exc),
            )
            if kind in _SESSION_PASSES and isinstance(exc, CrdtError) and exc.code == "crdt_busy":
                # Busy is load, not a verdict. A merge waits for no commit, so
                # without this an outside change waited for the next tick.
                asyncio.get_running_loop().call_later(BUSY_RETRY_SECONDS, self._schedule, ref, kind)

    async def _session_pass(self, ref: DocRef, kind: Maintenance) -> None:
        """One pass of a session with a source, committed, then the outside
        change it merged (if any) announced like any other update."""
        passes = {
            "write_back": self.sessions.write_back,
            "merge": self.sessions.merge_outside,
            "idle": self.sessions.idle,
        }
        # Not admitted as a whole: a pass takes an admission only around its
        # sandbox work (see SessionSync), never across a write to the source,
        # whose store can be slow.
        async with self.session_factory() as db:
            try:
                settled = await passes[kind](db, ref)
            except CorruptHistoryError as exc:
                await db.rollback()
                self._schedule(ref, "quarantine", reason=str(exc))
                return
            await db.commit()
        if settled.applied is not None:
            await self.broadcast(ref, settled.applied)
            await self.after_commit(ref, settled.applied)

    async def compact(self, db: AsyncSession, ref: DocRef) -> bool:
        """Fold the log into a full snapshot (never a shallow one: a peer that
        has been offline must still be able to merge). Restarts the history
        instead when the snapshot alone has outgrown its bound. Returns
        whether anything moved."""
        doc_type = self.type_of(ref)
        doc = await self.locked(db, ref)
        if doc is None or doc.log_seq == doc.snapshot_log_seq:
            return False
        # The worker checks its copy against the stored vector itself: the
        # backend holds the vector only as bytes, and two encodings of one
        # vector need not be the same bytes.
        header = {
            "op": "snapshot",
            "key": self.cache_key(ref, doc),
            "epoch": doc.epoch,
            "log_seq": doc.log_seq,
            "expect_vv": True,
        }
        try:
            reply = await self.at_position(db, ref, doc, header, [bytes(doc.vv)], maintenance=True)
        except CorruptHistoryError as exc:
            await self.restart(db, ref, reason=str(exc), quarantine=True)
            return True
        except SandboxRefusedError as exc:
            if exc.code != "corrupt":
                raise
            await self.restart(db, ref, reason=exc.message, quarantine=True)
            return True
        snapshot, _vv = reply.blobs
        if len(snapshot) > doc_type.limits.rotate_snapshot_bytes:
            await self.restart(db, ref, reason="compacted", quarantine=False)
            return True
        doc.snapshot = snapshot
        doc.snapshot_log_seq = doc.log_seq
        doc.snapshot_bytes = len(snapshot)
        doc.log_bytes = 0
        await db.execute(
            delete(CrdtUpdate).where(
                CrdtUpdate.org_id == ref.org_id,
                CrdtUpdate.doc_type == ref.stored_type,
                CrdtUpdate.doc_id == ref.doc_id,
                CrdtUpdate.epoch == doc.epoch,
                CrdtUpdate.log_seq <= doc.log_seq,
            )
        )
        await db.flush()
        return True

    async def restart(
        self,
        db: AsyncSession,
        ref: DocRef,
        *,
        reason: str,
        quarantine: bool,
        seed: Seed | None = None,
    ) -> int:
        """Start a new epoch holding the document's current content, drop the
        old epoch's history and tell every peer to rebase. A rotation takes
        the content from the document itself (the sandbox's copy at the
        committed position); a quarantine, whose history cannot be trusted to
        rebuild, from the type's recovery (:meth:`CrdtDocType.recover`). A
        rotation that finds the history broken becomes a quarantine.

        ``seed`` names the new epoch's content outright: a session closed
        because its source is gone (no stamp: the source is forgotten), or
        opened again from its source (its stamp). Returns the new epoch."""
        doc_type = self.type_of(ref)
        doc = await self.locked(db, ref)
        if doc is None:
            raise CrdtError("not_found")
        projection = doc.projection if isinstance(doc.projection, dict) else {}
        current = ""
        source = "rotation"
        #: The old epoch's last state, for a type that carries late writes
        #: into the new one (see :meth:`_tail`).
        tail: tuple[bytes, bytes] | None = None
        old_epoch = doc.epoch
        # The content the document's source holds, when it differs from the
        # current content: the new epoch keeps a version that matches it.
        base: str | None = None
        stamp: SourceStamp | None = None
        if seed is not None:
            current, source, stamp = seed.text, seed.source, seed.stamp
        elif not quarantine:
            try:
                current = await self.content(db, ref, doc, maintenance=True)
                if doc.source_vv is not None and doc.source_epoch == doc.epoch:
                    base = await self.content(
                        db, ref, doc, at=bytes(doc.source_vv), maintenance=True
                    )
                tail = await self._tail(db, ref, doc, doc_type)
            except CorruptHistoryError as exc:
                quarantine, reason = True, str(exc)
        lost = False
        if quarantine and seed is None:
            if doc_type.source is not None:
                # What the broken history still holds is kept beside the
                # source before the new epoch starts from the source.
                lost = await self.sessions.rescue(db, ref, doc)
            recovered = await doc_type.recover(db, ref=ref, projection=projection)
            current, source, stamp, base = recovered.text, recovered.source, recovered.stamp, None
        epoch = doc.epoch + 1
        seeded = await self._seed(
            ref,
            {
                "op": "seed",
                "key": self.cache_key(ref, doc),
                "epoch": epoch,
                "rules": doc_type.rules,
                "peer": await self.seed_peer(db),
                "blank": seed is not None and seed.blank,
            },
            (current.encode("utf-8"),) + (() if base is None else (base.encode("utf-8"),)),
            budget=self.load_budget,
        )
        snapshot, vv, seeded_projection, base_vv = seeded.blobs
        await db.execute(
            delete(CrdtUpdate).where(
                CrdtUpdate.org_id == ref.org_id,
                CrdtUpdate.doc_type == ref.stored_type,
                CrdtUpdate.doc_id == ref.doc_id,
            )
        )
        doc.epoch = epoch
        doc.log_seq = 0
        doc.vv = vv
        doc.snapshot = snapshot
        doc.snapshot_log_seq = 0
        doc.snapshot_bytes = len(snapshot)
        doc.log_bytes = 0
        doc.loro_format = str(seeded.header.get("loro") or doc.loro_format)
        doc.projection = {
            **projection,
            **decode_projection(seeded_projection),
        }
        doc.seeded_from = source
        if seed is not None and stamp is None:
            doc.source_etag = doc.source_version_id = doc.source_sha256 = None
            doc.source_vv = doc.source_epoch = None
            doc.source_history = []
        if stamp is not None:
            doc.source_etag = stamp.etag
            doc.source_version_id = stamp.version_id
            doc.source_sha256 = stamp.sha256
        if doc.source_etag is not None:
            # The source is where it was (a rotation) or where the recovery
            # read it (a quarantine); either way ``base_vv`` is the version of
            # the new epoch whose content it holds.
            doc.source_vv = base_vv
            doc.source_epoch = epoch
            # The versions it matched before belong to the epoch just left.
            doc.source_history = []
        await self._forget_peers(db, ref)
        keep_tail = getattr(doc_type, "keep_tail", None)
        if tail is not None and keep_tail is not None and not quarantine:
            await keep_tail(
                db,
                ref=ref,
                epoch=old_epoch,
                snapshot=tail[0],
                vv=tail[1],
                next_epoch=epoch,
                next_base_vv=vv,
            )
        if quarantine:
            doc.quarantined_at = _now()
            log.error("crdt.doc.quarantined", doc=ref.key, reason=reason, epoch=epoch)
        else:
            now = self.clock()
            for key in [
                k for k, at in self._rotated.items() if at + ROTATION_INTERVAL_SECONDS <= now
            ]:
                del self._rotated[key]
            self._rotated[ref.key] = now
            log.info("crdt.doc.rotated", doc=ref.key, reason=reason, epoch=epoch)
        await db.flush()
        reload = DocEnvelope(
            doc_id=ref.doc_id,
            doc_type=cast(DocType, ref.doc_type),
            epoch=epoch,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="reload",
            payload=ReloadPayload(
                epoch=epoch,
                reason="quarantine" if quarantine else "compacted",
                saving=self.saving_of(ref, doc),
            ).model_dump(mode="json"),
        )
        await self._emit(
            db,
            ref,
            Access(can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)),
            reload,
            actor=_system_actor(),
            version=0,
        )
        if lost:
            await self.sessions.announce_lost(ref, epoch=epoch)
        return epoch

    async def _tail(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, doc_type: CrdtDocType
    ) -> tuple[bytes, bytes] | None:
        """The whole document and its vector as the epoch it is restarting
        from ends, for a type that keeps it (``keep_tail``): a writer whose
        token names the old epoch is then merged from exactly the state it
        read rather than from the new epoch's head. ``None`` for any other
        type, and when the sandbox cannot export it (the restart goes on)."""
        if getattr(doc_type, "keep_tail", None) is None:
            return None
        try:
            reply = await self.at_position(
                db,
                ref,
                doc,
                {
                    "op": "snapshot",
                    "key": self.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                },
                [],
                maintenance=True,
            )
        except (CrdtError, SandboxRefusedError):
            return None
        data, vv = reply.blobs
        return data, vv

    async def drain(self) -> None:
        """Wait for every background task scheduled so far (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def sweep_unsaved(self, *, limit: int = sweeper.UNSAVED_SWEEP_LIMIT) -> int:
        """Write back every session holding unsaved edits (:mod:`.sweeper`)."""
        if not self.run_sessions or self._closed:
            return 0
        found = await sweeper.unsaved(
            self.session_factory, self.registry, limit=limit, now=_now(), orgs=self.sweep_orgs
        )
        for ref in found:
            self._schedule(ref, "write_back", delay=0.0)
        await sweeper.report_backlog(self.session_factory, now=_now(), orgs=self.sweep_orgs)
        now = _now()
        if self._expired_at is None or (now - self._expired_at).total_seconds() >= EXPIRE_EVERY:
            # The same replica's housekeeping deletes the sessions nobody
            # needs any more (a file left alone for a month, or closed).
            self._expired_at = now
            await sweeper.expire(self.session_factory, now=now, orgs=self.sweep_orgs)
        return len(found)

    def start_sweeper(self, *, every: float = sweeper.UNSAVED_SWEEP_SECONDS) -> None:
        """Run :meth:`sweep_unsaved` now and every ``every`` seconds until
        :meth:`aclose`. Idempotent."""
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = sweeper.start(self.sweep_unsaved, self.sleep, every=every)

    async def flush_unsaved(
        self, org_id: uuid.UUID, node_id: uuid.UUID
    ) -> dict[str, tuple[int, int]]:
        """Write back the unsaved file sessions at or under ``node_id``
        (:meth:`SessionSync.flush_under`)."""
        return await self.sessions.flush_under(org_id, node_id)

    @property
    def sweeping(self) -> bool:
        """Whether this replica is sweeping for unsaved sessions."""
        return self._sweeper is not None and not self._sweeper.done()

    async def aclose(self) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        self._tasks.clear()
        await self.peers.aclose()


def _system_actor() -> dict[str, Any]:
    from alkera_core.events import actor_system

    return dict(actor_system("crdt"))


__all__ = [
    "CRDT_UPDATE_EVENT_TYPE",
    "LOAD_CRASHES_BEFORE_QUARANTINE",
    "PEERS_PER_PERSON",
    "PEER_LEASE",
    "POISON_SECONDS",
    "ROTATION_INTERVAL_SECONDS",
    "Applied",
    "CorruptHistoryError",
    "CrdtDocs",
    "CrdtError",
    "Sync",
    "admission_slots_for",
    "safe_error",
]
