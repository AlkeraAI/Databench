"""The fake drive, watcher and clock the live-push suites drive a sync with.

The fakes are a small server, not mocks: ``FakeLiveApi`` reads the bytes off
disk when it is asked to upload, applies a ``tree`` batch the way the route
does (deletes, then folders, then files, then renames), keeps the node ids it
handed out and remembers what it was told. Every assertion is therefore about
what the *drive* ended up holding or being asked for — never about how many
times a method was called for its own sake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alkera_cli.files.digest import DirDigest, digest_of
from alkera_cli.files.live_sync import (
    AgreedBase,
    ConflictAnswer,
    ConflictTargetGoneError,
    DriveDigests,
    InboundEntry,
    LiveBatchAnswer,
    LiveCadence,
    LiveEntry,
    LiveSync,
    TreeAnswer,
    TreeEntry,
    hash_file,
)
from alkera_cli.files.mount import MountRecord, SelfFence
from alkera_cli.files.tree_watch import Change
from alkera_cli.files.walk import LIVE_SYNC_DEFAULT_PRESETS
from alkera_core.files.conflicts import conflict_rename, conflicted_copy_name

#: The machine the fake drive names a holder's conflicted copies after, and the
#: minute it stamps them with: what the drive's own namer is handed.
MACHINE_NAME = "alkera-demo-box"
CONFLICT_AT = datetime(2026, 9, 24, 3, 25, tzinfo=UTC)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWatcher:
    """A scripted stream of watcher batches."""

    def __init__(self, batches: Sequence[set[tuple[Change, str]]]) -> None:
        self.batches = list(batches)

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for batch in self.batches:
                yield batch

        return stream()


class RefusedError(RuntimeError):
    """What the SDK raises for a refused Files call: the code is on the error."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(f"the server refused the call — {code}")
        self.code = code
        if status is not None:
            self.status_code = status


@dataclass
class FakeLiveApi:
    """A drive that lists rows, hands out node ids and keeps what it was given."""

    root: Path
    refuse: str | None = None
    refuse_on: str = "upload"
    clock: FakeClock | None = None

    nodes: dict[str, str] = field(default_factory=dict)
    dirs: dict[str, str] = field(default_factory=dict)
    stored: dict[str, bytes] = field(default_factory=dict)
    uploads: list[str] = field(default_factory=list)
    uploaded_to: dict[str, str] = field(default_factory=dict)
    #: The base each upload was fenced on, in order: ``(path, base)``.
    bases: list[tuple[str, AgreedBase | None]] = field(default_factory=list)
    #: Each node's etag, moved on by every version the drive takes.
    etags: dict[str, int] = field(default_factory=dict)
    trashed: list[str] = field(default_factory=list)
    renames: list[tuple[str, str, str]] = field(default_factory=list)
    batches: list[list[LiveEntry]] = field(default_factory=list)
    trees: list[list[TreeEntry]] = field(default_factory=list)
    #: What the drive was asked for, in order, with the clock at the time:
    #: ``("tree", n)`` for a batch of n rows, ``("upload", path)`` for bytes.
    events: list[tuple[str, Any, float]] = field(default_factory=list)
    #: The conflicted copies the drive made, by the staging token that asked:
    #: ``(copy path, conflict_of, bytes)``. A token asked again is answered
    #: with the copy it already made, as the real idempotency key does.
    copies: dict[str, tuple[str, str | None, bytes]] = field(default_factory=dict)
    #: Called after the drive has made a copy and before it answers — a test
    #: that raises here is a holder that died between the two.
    after_copy: Callable[[str], None] | None = None
    #: The drive's ``files_conflict_copies_max``: past this many copies of one
    #: node, a further displacement is filed as a new version of the newest
    #: copy, under that copy's id and name. ``None`` is no ceiling.
    copies_max: int | None = None
    #: Nodes trashed while a copy's session was open: a submission naming one
    #: is refused at the commit, every time its key is replayed, and the
    #: tokens it was refused under are kept here in order.
    gone_at_complete: set[str] = field(default_factory=set)
    gone_refusals: list[str] = field(default_factory=list)
    _answers: dict[str, ConflictAnswer] = field(default_factory=dict)
    #: The batch ids the route has applied, with what it answered: a batch id
    #: seen again is answered the same and applies nothing, as the route does.
    applied_batches: dict[str, TreeAnswer] = field(default_factory=dict)
    #: The batch ids of every ``tree`` call, in order, repeats included.
    batch_ids: list[str] = field(default_factory=list)
    #: What the drive holds per path — size and mtime for a file — the facts
    #: its digests are computed from.
    facets: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: Every digest request, as the list of directories it named.
    digest_requests: list[list[str]] = field(default_factory=list)
    #: Every digest request that asked for children, as the directories it
    #: asked to list.
    name_requests: list[list[str]] = field(default_factory=list)
    #: Raised by ``tree`` AFTER the batch is applied: the process dying
    #: between the drive's answer and the journal's ack.
    crash_after_tree: BaseException | None = None
    _next: int = 0

    def _mint(self, path: str) -> str:
        self._next += 1
        return f"node-{self._next}-{path or 'root'}"

    def _now(self) -> float:
        return self.clock.monotonic() if self.clock is not None else 0.0

    def _folders_for(self, path: str) -> None:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            self.dirs.setdefault(folder, self._mint(folder))

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        self._maybe_refuse("tree")
        self.batch_ids.append(batch_id)
        if batch_id in self.applied_batches:
            return self.applied_batches[batch_id]
        answer = self._apply_tree(entries)
        self.applied_batches[batch_id] = answer
        if self.crash_after_tree is not None:
            crash, self.crash_after_tree = self.crash_after_tree, None
            raise crash
        return answer

    def _apply_tree(self, entries: Sequence[TreeEntry]) -> TreeAnswer:
        self.trees.append(list(entries))
        self.events.append(("tree", len(entries), self._now()))
        ordered = sorted(
            entries,
            key=lambda entry: {"delete": 0, "upsert": 1 if entry.kind == "dir" else 2}.get(
                entry.op, 3
            ),
        )
        for entry in ordered:
            if entry.op == "delete":
                self._delete(entry.path)
            elif entry.op == "upsert" and entry.kind == "dir":
                self._folders_for(entry.path)
                self.dirs.setdefault(entry.path, self._mint(entry.path))
            elif entry.op == "rename" and entry.from_ in self.nodes:
                assert entry.from_ is not None
                node_id = self.nodes.pop(entry.from_)
                self._folders_for(entry.path)
                self.nodes[entry.path] = node_id
                self.facets.pop(entry.from_, None)
                self.facets[entry.path] = (entry.size or 0, entry.mtime_ns or 0)
                self.renames.append((node_id, entry.from_, entry.path))
            else:
                self._folders_for(entry.path)
                self.nodes.setdefault(entry.path, self._mint(entry.path))
                self.facets[entry.path] = (entry.size or 0, entry.mtime_ns or 0)
        return TreeAnswer(live_seq=len(self.trees), applied=len(entries), landing_count=0)

    def _delete(self, path: str) -> None:
        self.facets.pop(path, None)
        if path in self.nodes:
            self.trashed.append(self.nodes.pop(path))
        if path in self.dirs:
            self.trashed.append(self.dirs.pop(path))
            for inside in [key for key in self.nodes if key.startswith(f"{path}/")]:
                self.nodes.pop(inside)
            for inside in [key for key in self.dirs if key.startswith(f"{path}/")]:
                self.dirs.pop(inside)

    def digests(self, paths: Sequence[str], *, names: Sequence[str] = ()) -> DriveDigests:
        """Each named directory the drive lists, digested from its own rows,
        and the children of each directory ``names`` asks to list — refused
        as the route refuses one that is not also among ``paths``."""
        self._maybe_refuse("digests")
        self.digest_requests.append(list(paths))
        if names:
            assert set(names) <= set(paths), "the route answers 422"
            self.name_requests.append(list(names))
        answer: dict[str, DirDigest] = {}
        for directory in paths:
            if directory and directory not in self.dirs:
                continue
            answer[directory] = digest_of(self._children(directory))
        owed = self._owed()

        def holds_owed(path: str) -> bool:
            return any(row == path or row.startswith(f"{path}/") for row in owed)

        children: dict[str, list[str]] = {}
        for directory in names:
            children[directory] = []
            if directory and directory not in self.dirs:
                continue
            for _, raw, _, _ in self._children(directory):
                name = raw.decode()
                if not holds_owed(f"{directory}/{name}" if directory else name):
                    children[directory].append(name)
        return DriveDigests(digests=answer, children=children)

    def _children(self, directory: str) -> list[tuple[Any, bytes, int, int]]:
        found: list[tuple[Any, bytes, int, int]] = []
        for path in [*self.dirs, *self.nodes]:
            parent, _, name = path.rpartition("/")
            if parent != directory:
                continue
            if path in self.dirs:
                found.append(("dir", name.encode(), 0, 0))
            else:
                size, mtime_ns = self.facets.get(path, (0, 0))
                found.append(("file", name.encode(), size, mtime_ns))
        return found

    def _owed(self) -> set[str]:
        """The paths of the rows the drive has queued for the holder to take:
        the route never lists them, or anything holding one, as children."""
        return set()

    def resolve(self, paths: Sequence[str]) -> dict[str, str]:
        self._maybe_refuse("resolve")
        return {path: self.nodes[path] for path in paths if path in self.nodes}

    def upload(
        self, rel_path: str, node_id: str, size: int, *, base: AgreedBase | None = None
    ) -> str | None:
        self._maybe_refuse("upload")
        # The bytes the drive ends up holding are whatever is on disk NOW,
        # which is the only way a test can tell a stale upload from a fresh one.
        self.stored[rel_path] = (self.root / rel_path).read_bytes()
        self.uploads.append(rel_path)
        self.bases.append((rel_path, base))
        self.events.append(("upload", rel_path, self._now()))
        # An upload without a node id is filed by path, as the real push is:
        # it lands on the row the path names, or makes one.
        self.uploaded_to[rel_path] = node_id or self.nodes.setdefault(
            rel_path, self._mint(rel_path)
        )
        # Every version the drive takes moves the node's etag on, as it does.
        filed = self.uploaded_to[rel_path]
        self.etags[filed] = self.etags.get(filed, 0) + 1
        return str(self.etags[filed])

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        self._maybe_refuse("live_batch")
        self.batches.append(list(entries))
        return LiveBatchAnswer(live_seq=len(self.batches), pending=len(entries))

    def inbound(self) -> list[InboundEntry]:
        return []

    def item(self, node_id: str) -> dict[str, Any]:
        return {"id": node_id}

    def download(self, node_id: str, into: Path, **_: Any) -> None:
        raise AssertionError("the live push never downloads")

    def submit_conflict(
        self, staged: Path, relative: str, *, conflict_of: str | None, token: str
    ) -> ConflictAnswer:
        """File the staged bytes as a new node beside ``relative``, named by
        the drive's own conflicted-copy namer, ``(2)`` and on when taken."""
        self._maybe_refuse("conflict")
        if conflict_of in self.gone_at_complete:
            self.gone_refusals.append(token)
            raise ConflictTargetGoneError(f"{relative}: node {conflict_of} is trashed")
        known = self._answers.get(token)
        if known is None and conflict_of is not None and conflict_of not in self.nodes.values():
            # As the drive does: the displaced node must still be a live file
            # the drive lists. A trashed one is a 404, and nothing is kept.
            raise RefusedError("files.not_found", status=404)
        if known is None and conflict_of is not None and self.copies_max is not None:
            earlier = [copy for copy, of, _data in self.copies.values() if of == conflict_of]
            if len(earlier) >= self.copies_max:
                newest = earlier[-1]
                self.copies[token] = (newest, conflict_of, staged.read_bytes())
                known = ConflictAnswer(
                    node_id=self.nodes[newest],
                    name=newest.rpartition("/")[2],
                    conflict_id=f"conflict-{token}",
                )
                self._answers[token] = known
        if known is None:
            parent, _, name = relative.rpartition("/")

            def taken(candidate: bytes) -> bool:
                spelled = candidate.decode("utf-8", "surrogateescape")
                return (f"{parent}/{spelled}" if parent else spelled) in self.nodes

            chosen = conflict_rename(
                conflicted_copy_name(name.encode(), MACHINE_NAME, CONFLICT_AT), taken
            ).decode("utf-8", "surrogateescape")
            copy = f"{parent}/{chosen}" if parent else chosen
            node_id = self._mint(copy)
            self.nodes[copy] = node_id
            self.copies[token] = (copy, conflict_of, staged.read_bytes())
            known = ConflictAnswer(node_id=node_id, name=chosen, conflict_id=f"conflict-{token}")
            self._answers[token] = known
        if self.after_copy is not None:
            self.after_copy(token)
        return known

    def copy_bytes(self) -> dict[str, bytes]:
        """What each conflicted copy on the drive holds, by its path."""
        return {copy: data for copy, _of, data in self.copies.values()}

    def _maybe_refuse(self, verb: str) -> None:
        if self.refuse is not None and verb == self.refuse_on:
            raise RefusedError(self.refuse)

    def states(self) -> dict[str, str]:
        """The last state the drive was told for each node."""
        return {entry.node_id: entry.state for batch in self.batches for entry in batch}

    def listed(self) -> dict[str, TreeEntry]:
        """The last row the drive was sent for each path, across every batch."""
        return {entry.path: entry for batch in self.trees for entry in batch}


def record() -> MountRecord:
    return MountRecord(node_id="chat-node", heartbeat_every=15.0)


def make_sync(
    root: Path,
    api: FakeLiveApi,
    clock: FakeClock,
    *,
    cadence: LiveCadence | None = None,
    watcher: FakeWatcher | None = None,
    hasher: Any = hash_file,
    on_deferred: Any = None,
    root_path: str = "",
    sleep: Any = None,
    chat_id: str = "chat-7",
    journal: Any = None,
    exclude_presets: tuple[str, ...] = LIVE_SYNC_DEFAULT_PRESETS,
    peer: Any = None,
) -> LiveSync:
    deferred: list[str] = []
    return LiveSync(
        root=root,
        root_path=root_path,
        chat_id=chat_id,
        record=record(),
        # Settling is its own subject (``test_files_live_sync_promote``); every
        # other suite writes a file and flushes it in the same instant.
        cadence=cadence or LiveCadence(settle_ms=0),
        api=api,
        watcher=watcher or FakeWatcher([]),
        fence=SelfFence(grace=30.0, monotonic=clock.monotonic),
        clock=clock,
        sleep=sleep or (lambda _seconds: None),
        on_deferred=on_deferred or deferred.append,
        hasher=hasher,
        journal=journal,
        exclude_presets=exclude_presets,
        peer=peer,
    )


def write(root: Path, relative: str, data: bytes) -> Path:
    where = root / relative
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(data)
    return where


@dataclass
class FakeInboundApi(FakeLiveApi):
    """A drive that also pushes work down onto the box.

    ``download`` writes the first half of the payload, hands control to the
    test, then writes the rest — so a test can look at the directory at the
    exact moment a partial file exists somewhere and see what a reader on the
    box would see.
    """

    queued: list[InboundEntry] = field(default_factory=list)
    paths: dict[str, str] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    on_download: Any = None
    downloads: list[str] = field(default_factory=list)

    def inbound(self) -> list[InboundEntry]:
        return list(self.queued)

    def _owed(self) -> set[str]:
        return {self.paths[entry.node_id] for entry in self.queued if entry.node_id in self.paths}

    def item(self, node_id: str) -> dict[str, Any]:
        described: dict[str, Any] = {"id": node_id, "pathBytes": self.paths.get(node_id, "")}
        if node_id in self.etags:
            described["etag"] = str(self.etags[node_id])
        return described

    def web_write(self, node_id: str, payload: bytes, *, seq: int) -> None:
        """A person replaces the node's bytes on the web: a new version, the
        etag moves on, and the holder is owed the write."""
        self.contents[node_id] = payload
        self.etags[node_id] = self.etags.get(node_id, 0) + 1
        self.queued = [InboundEntry(node_id=node_id, state="inbound", seq=seq)]

    def download(self, node_id: str, into: Path, **_: Any) -> None:
        self._maybe_refuse("download")
        self.downloads.append(node_id)
        into.parent.mkdir(parents=True, exist_ok=True)
        payload = self.contents.get(node_id, b"")
        into.write_bytes(payload[: len(payload) // 2])
        if self.on_download is not None:
            self.on_download(node_id, into)
        into.write_bytes(payload)
