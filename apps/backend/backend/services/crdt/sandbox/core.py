"""Everything the CRDT lane does with Loro, as plain functions over bytes.

This is the only module in the backend that imports ``loro``, and only the
sandbox worker process imports it: a Loro build that aborts on hostile input
takes that worker down, never the process serving sockets (a test pins that
the backend never loads it). The worker keeps a small cache of live documents
so a keystroke is validated against a document already in memory; the cache is
an optimisation the backend never relies on — every request names the
``(epoch, log_seq)`` it expects, and a miss answers ``need`` so the backend
sends the document again.

A client update is never imported into a cached document directly. It is
imported into a FORK; the fork is checked against the document type's
:class:`DocRules`; and what is committed and broadcast is the CANONICAL delta the
fork exports from the version before it — never the bytes the client sent. The
cached document moves forward only when the backend says the delta committed.

The fork is a standing one: each cached document keeps a SHADOW, a second copy
holding exactly its history, that the next update is imported into. The first
import into a freshly made fork pays for decoding the whole history again (a
tenth of a second per keystroke on a document of 100k edits, which saturated
the worker under two people typing), while an import into a copy already in
use costs microseconds. When a validated update commits, the shadow becomes
the document and the old document takes the committed delta and becomes the
shadow; an update refused for any reason throws its shadow away, so nothing a
refused update brought in is ever kept or read again.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar

import loro
from loro import (
    CounterSpan,
    EphemeralStore,
    ExportMode,
    IdSpan,
    ImportStatus,
    LoroDoc,
    VersionVector,
)

#: The Loro version this process runs, reported to the backend and stored
#: beside every snapshot it writes.
LORO_VERSION: Final[str] = str(loro.LORO_VERSION)

#: Loro peer ids at or below this are never minted for anyone. Every peer
#: that writes — a person's tab, or the seed of an epoch — is minted above it
#: from one sequence, so no operation id is ever written twice: not by two
#: tabs, and not by two epochs of one document.
SERVER_PEER_MAX: Final = 1023

#: How far ahead of this host's clock a change's timestamp may be (seconds).
#: Loro stamps every later change with at least the newest timestamp it
#: depends on, so a stamp further ahead would be copied onto everyone's edits.
MAX_TIMESTAMP_SKEW_SECONDS: Final = 24 * 60 * 60

_FATAL = (KeyboardInterrupt, SystemExit, GeneratorExit)
_T = TypeVar("_T")


class SandboxError(Exception):
    """A request the core refuses; ``code`` is what the backend is told."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class Refused(Exception):  # noqa: N818 - a verdict, not a fault
    """A strategy's refusal of a text it was asked to take (a notebook file in
    a newer major format, or past the caps): the seed answers
    ``not_editable`` and a merge answers ``reject``, both with ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _guard(action: Callable[[], _T], code: str) -> _T:
    """Run a Loro call; anything it raises becomes :class:`SandboxError`.

    Loro surfaces a decode failure (and a Rust panic) as a bare
    ``BaseException``, so this is the one place the core catches that wide —
    and it lets the interpreter's own exits, and a strategy's
    :class:`Refused`, through."""
    try:
        return action()
    except (*_FATAL, Refused):
        raise
    except BaseException as exc:
        raise SandboxError(code, f"{type(exc).__name__}: {str(exc)[:200]}") from None


def _pending(status: ImportStatus) -> bool:
    """Whether an import left operations waiting on history the document lacks."""
    return status.pending is not None and not status.pending.is_empty


def new_doc() -> LoroDoc:
    # Loro's stub declares ``LoroDoc.__init__`` without a return annotation,
    # which strict mypy reads as an untyped call; the constructor takes nothing.
    return LoroDoc()  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# Document rules
# ---------------------------------------------------------------------------


def _text_of(doc: LoroDoc, name: str) -> str:
    return str(doc.get_text(name).to_string())


class DocStrategy(Protocol):
    """What one document type admits, and everything the lane does that
    depends on the shape of its content.

    The lane itself only moves Loro bytes and version vectors; whatever reads
    or writes the document's containers goes through the type's strategy, so a
    type whose content is one root text (a draft, a file) and a type whose
    content is a tree of maps and texts (a notebook) share every other line.
    A strategy is registered by name (:func:`register`) and named by the
    backend on every request (``rules``).

    * ``judge`` refuses an update whose operations touch what the type does
      not admit, or that leaves the document in a state it does not admit;
    * ``describe`` says what an admitted update touched (small, JSON), for the
      backend to record; empty for a type with nothing to say;
    * ``project`` is what the store keeps beside every write (small by
      contract: it rides a frame blob and a JSONB column on every keystroke);
    * ``render`` is the document's content as the text its source holds;
    * ``write_seed`` writes a text into a fresh document, ``anchor`` writes
      one operation that changes nothing, and ``rewrite`` edits a document
      showing ``current`` into ``target`` (``keep``: adding only);
    * ``normalize`` writes whatever brings the document back to the type's
      normal form, and says whether it wrote anything;
    * ``caret`` judges an ephemeral caret value and returns what the backend
      may know of it;
    * ``warm_sample`` is a small content of the type that reaches every part
      of it (a notebook's: a cell of every kind). The worker seeds it, judges
      an edit on it and renders it before it takes a request
      (:func:`warm_all`), so the type's one-time costs (the imports and
      parser tables its first request would otherwise load) are never spent
      inside a request's budget.
    """

    @property
    def name(self) -> str: ...

    @property
    def max_update_ops(self) -> int: ...

    @property
    def max_text_bytes(self) -> int:
        """The largest whole text a seed or a merge takes (UTF-8 bytes)."""
        ...

    def project(self, doc: LoroDoc) -> dict[str, Any]: ...

    def judge(self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]) -> str: ...

    def describe(
        self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    def render(self, doc: LoroDoc) -> str: ...

    def write_seed(self, doc: LoroDoc, text: str, known: Mapping[str, str] | None) -> None: ...

    def anchor(self, doc: LoroDoc) -> None: ...

    def rewrite(self, doc: LoroDoc, current: str, target: str, *, keep: bool) -> None: ...

    def normalize(self, doc: LoroDoc) -> bool: ...

    def caret(self, value: Mapping[str, Any]) -> dict[str, Any]: ...

    def warm_sample(self) -> str: ...


@dataclass(frozen=True, slots=True)
class DocRules:
    """The strategy of a type whose content is one root text.

    ``containers`` is every container an operation may touch, spelled as Loro's
    JSON export spells it (``cid:root-<name>:<Type>``); ``op_types`` every
    operation content type. ``text_caps`` caps the UTF-8 size of named root
    texts (an update may not grow one past its cap; it may always shrink it).
    ``cursor_containers`` is where an ephemeral caret may point. ``content``
    names the root text that IS the document's content: what a seed fills,
    what a new epoch starts from and what a session writes back.

    ``project`` is what the store keeps beside every write (see
    :class:`DocStrategy`); a type whose content is large never puts the
    content in it.
    """

    name: str
    containers: frozenset[str]
    op_types: frozenset[str]
    text_caps: Mapping[str, int]
    max_update_ops: int
    cursor_containers: frozenset[str]
    content: str
    project: Callable[[LoroDoc], dict[str, Any]]

    @property
    def max_text_bytes(self) -> int:
        return self.text_caps[self.content]

    def judge(self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]) -> str:
        for change in changes:
            for op in change["ops"]:
                if op.get("container") not in self.containers:
                    return "container"
                content = op.get("content")
                if not isinstance(content, dict) or content.get("type") not in self.op_types:
                    return "op_type"
        sizes_before = _guard(lambda: self._text_sizes(base), "internal")
        for name, size in _guard(lambda: self._text_sizes(fork), "reject").items():
            if size > self.text_caps[name] and size > sizes_before[name]:
                return "text_too_large"
        return ""

    def describe(
        self, base: LoroDoc, fork: LoroDoc, changes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {}

    def _text_sizes(self, doc: LoroDoc) -> dict[str, int]:
        return {name: int(doc.get_text(name).len_utf8) for name in self.text_caps}

    def render(self, doc: LoroDoc) -> str:
        return _text_of(doc, self.content)

    def write_seed(self, doc: LoroDoc, text: str, known: Mapping[str, str] | None) -> None:
        if text:
            doc.get_text(self.content).insert(0, text)

    def anchor(self, doc: LoroDoc) -> None:
        """A character typed and taken back in the content text."""
        text = doc.get_text(self.content)
        text.insert(0, " ")
        text.delete(0, 1)
        doc.commit()

    def rewrite(self, doc: LoroDoc, current: str, target: str, *, keep: bool) -> None:
        _rewrite(doc.get_text(self.content), current, target, keep=keep)

    def normalize(self, doc: LoroDoc) -> bool:
        return False

    def caret(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not set(value) <= _CURSOR_KEYS or not value:
            raise SandboxError("ephemeral", "a caret is an anchor and a focus")
        for part in value.values():
            container = _cursor_container(part)
            if not isinstance(container, loro.ContainerID.Root) or (
                f"cid:root-{container.name}:{container.container_type}"
                not in self.cursor_containers
            ):
                raise SandboxError("ephemeral", "a caret points into a container it may not")
        return {}

    def warm_sample(self) -> str:
        return "warm"


def _project_workspace(doc: LoroDoc) -> dict[str, Any]:
    # The draft keeps its text in the projection: it is capped at 32 KiB, and
    # it is what a quarantine re-seeds the draft from and what the lane's
    # rollback (migration 0174's downgrade) writes back to the op log.
    text = _text_of(doc, "draft")
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _project_file(doc: LoroDoc) -> dict[str, Any]:
    text = _text_of(doc, "content")
    data = text.encode("utf-8")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "lines": text.count("\n") + 1 if text else 0,
    }


CHAT_WORKSPACE: Final = DocRules(
    name="chat_draft",
    containers=frozenset({"cid:root-draft:Text"}),
    op_types=frozenset({"insert", "delete"}),
    text_caps={"draft": 32 * 1024},
    max_update_ops=100_000,
    cursor_containers=frozenset({"cid:root-draft:Text"}),
    content="draft",
    project=_project_workspace,
)

#: A text file co-edited in a chat's working folder. Its hard cap sits at
#: twice the soft cap the registry tells clients (1 MiB) and far under the
#: snapshot size that restarts the history (4 MiB), so a new epoch started
#: from a full file never needs restarting again at once.
FILE: Final = DocRules(
    name="file",
    containers=frozenset({"cid:root-content:Text"}),
    op_types=frozenset({"insert", "delete"}),
    text_caps={"content": 2 * 1024 * 1024},
    max_update_ops=2 * 1024 * 1024,
    cursor_containers=frozenset({"cid:root-content:Text"}),
    content="content",
    project=_project_file,
)

RULES: Final[dict[str, DocStrategy]] = {rules.name: rules for rules in (CHAT_WORKSPACE, FILE)}


def register(strategy: DocStrategy) -> DocStrategy:
    """Register a document type's strategy under its name (a module defining
    one calls this at import; the worker imports every such module)."""
    known = RULES.get(strategy.name)
    if known is not None and known is not strategy:
        raise ValueError(f"a strategy named {strategy.name!r} is registered already")
    RULES[strategy.name] = strategy
    return strategy


#: The peers a warm-up writes as: minted-range ids no stored document uses,
#: on a cache of its own that is dropped after.
_WARM_SEED_PEER: Final = SERVER_PEER_MAX + 1
_WARM_TAB_PEER: Final = SERVER_PEER_MAX + 2


def warm(strategy: DocStrategy) -> None:
    """Run ``strategy``'s :meth:`~DocStrategy.warm_sample` through the
    lane's own path, on a cache of its own: a seed, one edit judged on the
    cached document, and its content rendered."""
    cache = DocCache(max_docs=1, max_bytes=64 * 1024 * 1024)
    key = f"warm:{strategy.name}"
    seeded = seed(
        cache, key=key, epoch=1, rules=strategy, text=strategy.warm_sample(), peer=_WARM_SEED_PEER
    )
    tab = new_doc()
    tab.peer_id = _WARM_TAB_PEER
    tab.import_(seeded.snapshot)
    before = tab.oplog_vv
    strategy.anchor(tab)
    validate(
        cache,
        key=key,
        epoch=1,
        log_seq=0,
        rules=strategy,
        peers=frozenset({_WARM_TAB_PEER}),
        update=bytes(tab.export(ExportMode.Updates(before))),
    )
    content(cache, key=key, epoch=1, log_seq=0, rules=strategy)


def warm_all() -> dict[str, str]:
    """Warm every registered type (:func:`warm`), and say how each went. A
    type that cannot warm still serves: its first request pays what the
    warm-up would have, and the report says so."""
    report: dict[str, str] = {}
    for name, strategy in RULES.items():
        started = time.perf_counter()
        try:
            warm(strategy)
        except Exception as exc:  # a failed warm-up must never stop the worker
            report[name] = f"failed: {type(exc).__name__}"
            continue
        report[name] = f"{(time.perf_counter() - started) * 1000:.0f} ms"
    return report


def rules_for(name: Any) -> DocStrategy:
    rules = RULES.get(name) if isinstance(name, str) else None
    if rules is None:
        raise SandboxError("unknown_rules", f"no rules named {name!r}")
    return rules


# ---------------------------------------------------------------------------
# Version vectors
# ---------------------------------------------------------------------------


def encode_vv(doc: LoroDoc) -> bytes:
    encoded: bytes = _guard(lambda: bytes(doc.oplog_vv.encode()), "internal")
    return encoded


def same_vv(doc: LoroDoc, encoded: bytes) -> bool:
    """Whether ``doc`` holds exactly the history ``encoded`` names.

    Compared as vectors, never as bytes: the encoding follows a hash map's
    order, so one vector encodes differently from one document to the next
    once enough peers have written it."""
    try:
        expected = decode_vv(encoded)
    except SandboxError:
        return False
    diff = _guard(lambda: doc.oplog_vv.diff(expected), "corrupt")
    return diff.forward.is_empty and diff.retreat.is_empty


def decode_vv(data: bytes) -> VersionVector:
    value: VersionVector = _guard(lambda: VersionVector.decode(data), "bad_vv")
    return value


def _forward(before: VersionVector, after: VersionVector) -> dict[int, tuple[int, int]]:
    """Each peer's counter range ``after`` holds that ``before`` does not."""
    spans: dict[int, tuple[int, int]] = _guard(
        lambda: dict(before.diff(after).forward.inner()), "bad_vv"
    )
    return {int(peer): (int(start), int(end)) for peer, (start, end) in spans.items()}


# ---------------------------------------------------------------------------
# The cache of live documents
# ---------------------------------------------------------------------------


#: How much text a cached document keeps of its earlier versions, by count
#: and by bytes: a base search reads the same few dozen versions over and over.
#: Every state handed out (a read of the latest, a merge's result and the
#: state holding exactly what it sent) is kept as it is made, so the states a
#: later search or a submit's answer names are read without a checkout.
VERSION_TEXTS_KEPT: Final = 256
VERSION_TEXT_BYTES_KEPT: Final = 8 * 1024 * 1024


@dataclass(slots=True)
class _Entry:
    epoch: int
    log_seq: int
    doc: LoroDoc
    size: int
    #: A validated fork waiting for the backend to say its delta committed.
    pending: tuple[int, LoroDoc, bytes] | None = None
    #: The texts of the states a pending merge made, kept once it commits.
    pending_texts: tuple[tuple[bytes, bytes], ...] = ()
    #: A copy holding exactly ``doc``'s history, already used for an import:
    #: the fork the next update is checked on. ``None`` when there is none
    #: yet, or the last one was spent or thrown away.
    shadow: LoroDoc | None = None
    #: The text at earlier versions already read, by the version's encoding.
    #: A version of an epoch never changes, and checking a long document out at
    #: an old version is the slow part of reading it (a third of a second on a
    #: soaked file, for each of the few dozen versions a base search compares).
    texts: OrderedDict[bytes, bytes] = field(default_factory=OrderedDict)

    def take_shadow(self) -> LoroDoc:
        """A fork holding exactly ``doc``'s history, to import into: the
        standing shadow when there is one, else a fresh fork. The caller owns
        it; hand it back with :meth:`keep_shadow` only while it still holds
        nothing the document does not."""
        shadow, self.shadow = self.shadow, None
        if shadow is not None:
            return shadow
        fork: LoroDoc = _guard(self.doc.fork, "internal")
        return fork

    def keep_shadow(self, shadow: LoroDoc) -> None:
        self.shadow = shadow

    def remember(self, at: bytes, text: bytes) -> None:
        self.texts[at] = text
        self.texts.move_to_end(at)
        held = sum(len(kept) for kept in self.texts.values())
        while self.texts and (
            len(self.texts) > VERSION_TEXTS_KEPT or held > VERSION_TEXT_BYTES_KEPT
        ):
            _, dropped = self.texts.popitem(last=False)
            held -= len(dropped)


@dataclass(slots=True)
class DocCache:
    """The worker's live documents, least recently used first out."""

    max_docs: int = 256
    max_bytes: int = 64 * 1024 * 1024
    _entries: OrderedDict[str, _Entry] = field(default_factory=OrderedDict)

    def get(self, key: str, epoch: int, log_seq: int) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None or entry.epoch != epoch or entry.log_seq != log_seq:
            return None
        self._entries.move_to_end(key)
        return entry

    def at_least(self, key: str, epoch: int, log_seq: int) -> _Entry | None:
        """The entry for ``key`` when it holds epoch ``epoch`` at ``log_seq``
        or past it: a later position of one epoch holds every earlier version
        of it, so a read of a named earlier version is answered from it."""
        entry = self._entries.get(key)
        if entry is None or entry.epoch != epoch or entry.log_seq < log_seq:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: str, entry: _Entry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_docs or (
            len(self._entries) > 1 and self.total_bytes() > self.max_bytes
        ):
            self._entries.popitem(last=False)

    def drop(self, key: str) -> None:
        self._entries.pop(key, None)

    def total_bytes(self) -> int:
        return sum(entry.size for entry in self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def position(self, key: str) -> tuple[int, int] | None:
        entry = self._entries.get(key)
        return None if entry is None else (entry.epoch, entry.log_seq)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Loaded:
    vv: bytes
    projection: dict[str, Any]


def load(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    rules: DocStrategy,
    snapshot: bytes,
    updates: list[bytes],
    expect_vv: bytes | None,
) -> Loaded:
    """Build ``key`` at ``(epoch, log_seq)`` from its snapshot and the log after
    it, check it against what the backend stored, and cache it.

    Every logged update must import whole: a stored delta that leaves anything
    pending, or a resulting version vector other than the stored one, means
    the stored state is not what was committed, and the backend quarantines the
    document rather than serve it."""
    doc = new_doc()
    if snapshot:
        status = _guard(lambda: doc.import_(snapshot), "corrupt")
        if _pending(status):
            raise SandboxError("corrupt", "the snapshot depends on history it does not hold")
    if updates:
        # In one batch: imported one by one, each update paid for the whole
        # document's state again, and a log of 500 small edits took 12 s to load
        # (past the load budget, so the document could not be opened at all);
        # as a batch it takes milliseconds.
        status = _guard(functools.partial(doc.import_batch, updates), "corrupt")
        if _pending(status):
            raise SandboxError("corrupt", "a logged update depends on missing history")
    vv = _guard(lambda: encode_vv(doc), "corrupt")
    if expect_vv is not None and not same_vv(doc, expect_vv):
        raise SandboxError("corrupt", "the rebuilt version vector is not the stored one")
    projection = _guard(lambda: rules.project(doc), "corrupt")
    cache.put(
        key,
        _Entry(
            epoch=epoch,
            log_seq=log_seq,
            doc=doc,
            size=len(snapshot) + sum(len(update) for update in updates),
        ),
    )
    return Loaded(vv=vv, projection=projection)


@dataclass(frozen=True, slots=True)
class Validated:
    """The verdict on one client update.

    ``outcome`` is ``ok`` (``delta`` is the canonical bytes to commit),
    ``dup`` (nothing new), ``resync`` (it depends on history the server does
    not hold) or ``reject`` (``reason`` names the rule it broke)."""

    outcome: str
    reason: str = ""
    delta: bytes = b""
    vv: bytes = b""
    projection: dict[str, Any] = field(default_factory=dict)
    ops: int = 0
    #: A merge's: the version whose content is the source's after it.
    base_vv: bytes = b""
    #: What the type says the update touched (:meth:`DocStrategy.describe`).
    notes: dict[str, Any] = field(default_factory=dict)
    #: A merge's: that version's content (UTF-8).
    at_text: bytes = b""


def _changes(
    fork: LoroDoc, spans: dict[int, tuple[int, int]], rules: DocStrategy
) -> str | list[dict[str, Any]]:
    """The imported changes as Loro's JSON spells them, or the first rule they
    break that holds for every type (their count, a commit message, a
    timestamp from the future, a shape that does not read)."""
    total = sum(end - start for start, end in spans.values())
    if total > rules.max_update_ops:
        return "too_many_ops"
    found: list[dict[str, Any]] = []
    for peer, (start, end) in spans.items():
        raw = _guard(
            functools.partial(fork.export_json_in_id_span, IdSpan(peer, CounterSpan(start, end))),
            "reject",
        )
        changes = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(changes, list):
            return "unreadable"
        latest = time.time() + MAX_TIMESTAMP_SKEW_SECONDS
        for change in changes:
            ops = change.get("ops") if isinstance(change, dict) else None
            if not isinstance(ops, list):
                return "unreadable"
            # A commit message would be stored and broadcast with the edit;
            # no editor writes one.
            if change.get("msg"):
                return "message"
            stamp = change.get("timestamp")
            if not isinstance(stamp, int) or stamp > latest:
                return "timestamp"
            if not all(isinstance(op, dict) for op in ops):
                return "unreadable"
            found.append(change)
    return found


def _check_ops(
    base: LoroDoc, fork: LoroDoc, spans: dict[int, tuple[int, int]], rules: DocStrategy
) -> tuple[str, list[dict[str, Any]]]:
    """The first rule the imported operations break (``""`` for none), and
    the changes they are."""
    changes = _changes(fork, spans, rules)
    if isinstance(changes, str):
        return changes, []
    return _guard(lambda: rules.judge(base, fork, changes), "reject"), changes


def validate(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    rules: DocStrategy,
    peers: frozenset[int],
    update: bytes,
) -> Validated | None:
    """Judge ``update`` from a client that may write as any of ``peers`` (the
    Loro peers the server ever handed this person for this document) against
    the cached document; ``None`` when the cache does not hold ``key`` at
    ``(epoch, log_seq)`` and the backend must load it first."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    entry.pending, entry.pending_texts = None, ()
    base = entry.doc
    # The cached document was itself built from client bytes (an admitted
    # fork becomes it), so it is read through the guard too.
    before: VersionVector = _guard(lambda: base.oplog_vv, "internal")
    # Every return below but ``dup`` and ``ok`` drops the fork: it may hold
    # what the refused update brought in (operations, or changes Loro keeps
    # waiting for history), and the next update gets a clean one.
    fork = entry.take_shadow()
    try:
        status = _guard(lambda: fork.import_(update), "reject")
    except SandboxError:
        return Validated(outcome="reject", reason="undecodable")
    if _pending(status):
        return Validated(outcome="resync", vv=encode_vv(base))
    # Everything read off the fork from here on runs over the client's
    # bytes: a Loro call that aborts refuses the update, never the worker.
    spans = _forward(before, _guard(lambda: fork.oplog_vv, "reject"))
    if not spans:
        # Nothing new came in and nothing waits: the fork still holds
        # exactly the document's history.
        entry.keep_shadow(fork)
        return Validated(outcome="dup", vv=encode_vv(base))
    if not set(spans) <= peers:
        return Validated(outcome="reject", reason="peer")
    reason, changes = _check_ops(base, fork, spans, rules)
    if reason:
        return Validated(outcome="reject", reason=reason)
    delta: bytes = _guard(lambda: bytes(fork.export(ExportMode.Updates(before))), "internal")
    vv = _guard(lambda: encode_vv(fork), "reject")
    projection = _guard(lambda: rules.project(fork), "reject")
    notes = _guard(lambda: rules.describe(base, fork, changes), "reject")
    entry.pending = (log_seq + 1, fork, delta)
    return Validated(
        outcome="ok",
        delta=delta,
        vv=vv,
        projection=projection,
        ops=sum(end - start for start, end in spans.values()),
        notes=notes,
    )


def advance(cache: DocCache, *, key: str, epoch: int, log_seq: int, delta: bytes) -> bool:
    """The backend committed ``delta`` at ``log_seq``: move the cached document
    forward. ``False`` (and the entry dropped) when the cache cannot follow,
    so the next request loads afresh rather than validate against a document
    that is not the stored one. A cache already at or past ``log_seq`` in this
    epoch holds the commit (it was caught up from the log): it stays."""
    entry = cache.get(key, epoch, log_seq - 1)
    if entry is None:
        position = cache.position(key)
        if position is not None and position[0] == epoch and position[1] >= log_seq:
            return True
        cache.drop(key)
        return False
    pending, entry.pending = entry.pending, None
    texts, entry.pending_texts = entry.pending_texts, ()
    if pending is not None and pending[0] == log_seq and pending[2] == delta:
        # The validated fork becomes the document, and the document it
        # replaces takes the same delta and stands as the next shadow.
        previous, entry.doc = entry.doc, pending[1]
        entry.shadow = _caught_up(previous, delta, entry.doc)
        for version, text in texts:
            entry.remember(version, text)
    else:
        try:
            status = _guard(lambda: entry.doc.import_(delta), "corrupt")
        except SandboxError:
            cache.drop(key)
            return False
        if _pending(status):
            cache.drop(key)
            return False
        shadow, entry.shadow = entry.shadow, None
        if shadow is not None:
            entry.shadow = _caught_up(shadow, delta, entry.doc)
    entry.log_seq = log_seq
    entry.size += len(delta)
    cache.put(key, entry)
    return True


def _caught_up(doc: LoroDoc, delta: bytes, target: LoroDoc) -> LoroDoc | None:
    """``doc`` after importing the committed ``delta``, when that leaves it
    holding exactly ``target``'s history: a shadow for ``target``. ``None``
    when it does not (the next update then forks afresh)."""
    try:
        status = _guard(lambda: doc.import_(delta), "internal")
    except SandboxError:
        return None
    if _pending(status) or not same_vv(doc, encode_vv(target)):
        return None
    return doc


@dataclass(frozen=True, slots=True)
class Exported:
    mode: str
    data: bytes
    vv: bytes


def export(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    since: bytes | None,
) -> Exported | None:
    """What a peer holding ``since`` is missing: Loro updates when its vector is
    readable, the whole document otherwise, at ``log_seq`` or any later
    position the cache holds (it moves only on a commit, so every position it
    holds is committed). ``None`` on a cache miss. A reader asks from a row it
    read without a lock: an exact match would send the worker back to that
    older position, and the next write would have to bring it forward again."""
    entry = cache.at_least(key, epoch, log_seq)
    if entry is None:
        return None
    doc = entry.doc
    if since is not None:
        try:
            vv = decode_vv(since)
            data = _guard(lambda: bytes(doc.export(ExportMode.Updates(vv))), "internal")
            return Exported(mode="updates", data=data, vv=encode_vv(doc))
        except SandboxError:
            pass
    data = _guard(lambda: bytes(doc.export(ExportMode.Snapshot())), "internal")
    return Exported(mode="snapshot", data=data, vv=encode_vv(doc))


@dataclass(frozen=True, slots=True)
class Seeded:
    snapshot: bytes
    vv: bytes
    projection: dict[str, Any]
    #: The version whose content is the seed's ``base`` (the whole seed when
    #: it had none).
    base_vv: bytes


def seed(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    rules: DocStrategy,
    text: str,
    peer: int,
    base: str | None = None,
    known: Mapping[str, str] | None = None,
    blank: bool = False,
) -> Seeded:
    """A new document holding ``text``, written by ``peer`` (minted for this
    seed alone), cached at ``(epoch, 0)``.

    ``base`` is the content of the source the document is written back to,
    when that differs from ``text`` (a new epoch of a file whose last edits
    are not written back yet): the seed writes ``base`` first and then the
    edit that turns it into ``text``, so the new epoch holds a version whose
    content IS the source's, and ``base_vv`` names it. An outside change to
    the source is merged from there (:func:`merge`). A ``blank`` seed is an
    empty document that reads no text (a closed session's epoch): an empty
    text is not one the type's reader accepts as a file."""
    if peer <= SERVER_PEER_MAX:
        raise SandboxError("bad_request", "a seed is written by a minted peer")
    if blank and (text or base is not None):
        raise SandboxError("bad_request", "a blank seed holds no text")
    doc = new_doc()
    doc.peer_id = peer
    first = text if base is None else base
    try:
        if not blank:
            _guard(lambda: rules.write_seed(doc, first, known), "bad_request")
    except Refused as refused:
        raise SandboxError("not_editable", refused.reason) from None
    _guard(doc.commit, "internal")
    if _guard(lambda: doc.oplog_vv.get_last(peer), "internal") is None:
        # Nothing to seed still writes an operation: every later edit then
        # depends on this epoch's own seed, so an edit carried into another
        # epoch's document is missing history there, never text it adopts.
        _guard(lambda: rules.anchor(doc), "internal")
    base_vv = encode_vv(doc)
    if base is not None and base != text:
        shown = _guard(lambda: rules.render(doc), "internal")
        try:
            _guard(lambda: rules.rewrite(doc, shown, text, keep=False), "bad_request")
        except Refused as refused:
            raise SandboxError("not_editable", refused.reason) from None
        _guard(doc.commit, "internal")
    if _guard(lambda: rules.normalize(doc), "internal"):
        _guard(doc.commit, "internal")
    snapshot: bytes = _guard(lambda: bytes(doc.export(ExportMode.Snapshot())), "internal")
    # Serve from a document built from the snapshot, exactly as a load would,
    # so the next writer's operations never depend on this process's peer.
    loaded = load(
        cache,
        key=key,
        epoch=epoch,
        log_seq=0,
        rules=rules,
        snapshot=snapshot,
        updates=[],
        expect_vv=None,
    )
    projection = loaded.projection
    # A type whose per-update projection leaves something out for speed
    # (a notebook's rendered hash) gives it whole for a seed, stored once.
    at_rest = getattr(rules, "project_at_rest", None)
    entry = cache.get(key, epoch, 0)
    if at_rest is not None and entry is not None:
        projection = _guard(lambda: at_rest(entry.doc), "internal")
    return Seeded(snapshot=snapshot, vv=loaded.vv, projection=projection, base_vv=base_vv)


#: A changed block past this many characters is replaced whole rather than
#: diffed character by character (the character diff is quadratic in it).
#: The most work one diff of a changed span may do, as the product of the two
#: sides' lengths (lines, or characters within a line block): difflib's
#: matcher is quadratic in it. Past it the span is replaced whole, which a
#: merge still applies correctly, only less finely.
DIFF_BUDGET: Final = 4_000_000


def text_edits(
    current: str, target: str, *, whole_lines: bool = False
) -> list[tuple[int, int, str]]:
    """The edits that turn ``current`` into ``target``, as ``(at, remove,
    insert)`` in code points of ``current``, front to back. ``whole_lines``
    leaves a changed block of lines whole instead of diffing it by character.

    Lines are matched first and only the changed blocks are diffed by
    character, and nothing that is the same on both sides is ever deleted and
    typed again. That matters for a merge: a concurrent insert holds on to the
    characters beside it, and a diff that deletes and retypes an unchanged
    line break (as a raw character diff of two texts may) leaves such an
    insert beside nothing, and the merge then places it on another line.

    The work is bounded (:data:`DIFF_BUDGET`): the shared start and end are
    trimmed in linear time first, and a span too large to diff in budget is
    replaced whole, so one large change can never hold the worker past its
    deadline."""
    before = current.splitlines(keepends=True)
    after = target.splitlines(keepends=True)
    head = 0
    while head < min(len(before), len(after)) and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < min(len(before), len(after)) - head
        and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    old_lines = before[head : len(before) - tail]
    new_lines = after[head : len(after) - tail]
    at0 = sum(len(piece) for piece in before[:head])
    if not old_lines and not new_lines:
        return []
    if len(old_lines) * len(new_lines) > DIFF_BUDGET:
        return [(at0, sum(len(piece) for piece in old_lines), "".join(new_lines))]
    starts = [at0]
    for piece in old_lines:
        starts.append(starts[-1] + len(piece))
    edits: list[tuple[int, int, str]] = []
    lines = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in lines.get_opcodes():
        if tag == "equal":
            continue
        at = starts[i1]
        old_block = "".join(old_lines[i1:i2])
        new_block = "".join(new_lines[j1:j2])
        if tag != "replace" or whole_lines or len(old_block) * len(new_block) > DIFF_BUDGET:
            edits.append((at, len(old_block), new_block))
            continue
        chars = difflib.SequenceMatcher(None, old_block, new_block, autojunk=False)
        for ctag, c1, c2, d1, d2 in chars.get_opcodes():
            if ctag != "equal":
                edits.append((at + c1, c2 - c1, new_block[d1:d2]))
    return edits


def _rewrite(text: Any, current: str, target: str, *, keep: bool = False) -> None:
    """Edit ``text`` (holding ``current``) into ``target`` (see
    :func:`text_edits`), back to front so each edit's offsets still hold.

    ``keep`` takes only what ``target`` adds and removes nothing. Its lines
    land whole after the lines they would have replaced: added character by
    character between characters that stay, a rewritten word interleaves with
    the old one (``keep`` and ``new`` read ``nkewep``)."""
    for at, remove, insert in reversed(text_edits(current, target, whole_lines=keep)):
        if keep:
            if not insert:
                continue
            after = at + remove
            replaced = current[at:after]
            if replaced and not replaced.endswith("\n"):
                insert = ("\r\n" if "\r\n" in current else "\n") + insert
            text.insert(after, insert)
            continue
        if remove:
            text.delete(at, remove)
        if insert:
            text.insert(at, insert)


def merge(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    rules: DocStrategy,
    peer: int,
    base_vv: bytes,
    text: str,
    keep: bool = False,
) -> Validated | None:
    """Merge a change made to the source outside the session.

    ``keep`` merges only what the change adds: for a change whose writer could
    not say which version it was made on, where a removal may only be the
    writer not having seen what others wrote.

    ``base_vv`` names the version of the document whose content the source
    held before the outside change; ``text`` is what the source holds now.
    The change is written by ``peer`` on a fork at ``base_vv`` (``stale_peer``
    when the peer wrote since: a writer reuses one minted peer while its
    bases hold all it wrote, since every peer the document has makes each
    later import dearer), so it is concurrent with every edit made in the session
    since, and Loro merges the two: the people's live edits stay, and the
    outside change lands around them. The verdict is ``ok`` (``delta`` is
    the canonical bytes to commit, ``base_vv`` the version whose content now
    IS the source's), ``dup`` (the source still holds the base's content) or
    ``reject``. ``None`` on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if peer <= SERVER_PEER_MAX:
        raise SandboxError("bad_request", "a merge is written by a minted peer")
    entry.pending, entry.pending_texts = None, ()
    doc = entry.doc
    base = decode_vv(base_vv)
    before: VersionVector = _guard(lambda: doc.oplog_vv, "internal")
    if not _guard(lambda: before.diff(base).forward.is_empty, "internal"):
        raise SandboxError("bad_base", "the base is not a version of this document")
    if _guard(lambda: before.get_last(peer), "internal") != _guard(
        lambda: base.get_last(peer), "internal"
    ):
        # The peer wrote since the base: its next operation ids are past what
        # a fork at the base would write, and a fork there would write ids the
        # document already holds.
        raise SandboxError("stale_peer", "the peer wrote since the base")
    if len(text.encode("utf-8")) > rules.max_text_bytes:
        return Validated(outcome="reject", reason="text_too_large")
    fork: LoroDoc = _guard(lambda: doc.fork_at(doc.vv_to_frontiers(base)), "internal")
    fork.peer_id = peer
    shown = _guard(lambda: rules.render(fork), "internal")
    if shown == text:
        return Validated(outcome="dup", vv=encode_vv(doc), base_vv=base_vv)
    try:
        _guard(lambda: rules.rewrite(fork, shown, text, keep=keep), "internal")
    except Refused as refused:
        return Validated(outcome="reject", reason=refused.reason)
    _guard(fork.commit, "internal")
    if _guard(lambda: base.diff(fork.oplog_vv).forward.is_empty, "internal"):
        # The text differs from the base's only in what the type does not
        # keep (a notebook file's formatting): nothing to merge.
        return Validated(outcome="dup", vv=encode_vv(doc), base_vv=base_vv)
    change: bytes = _guard(lambda: bytes(fork.export(ExportMode.Updates(base))), "internal")
    merged = entry.take_shadow()
    status = _guard(lambda: merged.import_(change), "internal")
    if _pending(status):  # pragma: no cover - the fork's history is the document's
        raise SandboxError("internal", "a merge depended on history the document lacks")
    merged.peer_id = peer
    if _guard(lambda: rules.normalize(merged), "internal"):
        _guard(merged.commit, "internal")
    delta: bytes = _guard(lambda: bytes(merged.export(ExportMode.Updates(before))), "internal")
    entry.pending = (log_seq + 1, merged, delta)
    vv, at_vv = encode_vv(merged), encode_vv(fork)
    at_text = _guard(lambda: rules.render(fork), "internal").encode("utf-8")
    # Both are named in the answer to the writer and read back once the merge
    # commits; kept only then (a peer's ids are written again by its next
    # merge when this one never commits, so an uncommitted state's vector can
    # later name another state).
    entry.pending_texts = (
        (vv, _guard(lambda: rules.render(merged), "internal").encode("utf-8")),
        (at_vv, at_text),
    )
    return Validated(
        outcome="ok",
        delta=delta,
        vv=vv,
        projection=_guard(lambda: rules.project(merged), "internal"),
        base_vv=at_vv,
        at_text=at_text,
    )


def snapshot(
    cache: DocCache, *, key: str, epoch: int, log_seq: int, expect_vv: bytes | None = None
) -> Exported | None:
    """The whole cached document, for compaction, checked against the vector
    the store holds for it (``corrupt`` when it is not that document). ``None``
    on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if expect_vv is not None and not same_vv(entry.doc, expect_vv):
        cache.drop(key)
        raise SandboxError("corrupt", "the cached document is not the stored one")
    data = _guard(lambda: bytes(entry.doc.export(ExportMode.Snapshot())), "internal")
    return Exported(mode="snapshot", data=data, vv=encode_vv(entry.doc))


def content(
    cache: DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    rules: DocStrategy,
    at: bytes | None = None,
) -> bytes | None:
    """The cached document's content text as UTF-8: what a new epoch starts
    from and what a session writes back to its source. ``at`` names an
    earlier version of the document to read instead (the one its source last
    matched). ``None`` on a cache miss.

    A read of a named version is answered by a worker that holds the epoch at
    ``log_seq`` or past it. People typing move the worker on while a session
    pass reads what it read the row at, and a read that insisted on the exact
    position chased them and gave up as busy for as long as they typed."""
    entry = cache.get(key, epoch, log_seq) if at is None else cache.at_least(key, epoch, log_seq)
    if entry is None:
        return None
    doc = entry.doc
    if at is not None and not same_vv(doc, at):
        known = entry.texts.get(at)
        if known is not None:
            entry.texts.move_to_end(at)
            return known
        version = decode_vv(at)
        if not _guard(lambda: doc.oplog_vv.diff(version).forward.is_empty, "internal"):
            raise SandboxError("bad_base", "the version is not one of this document")
        fork = _guard(lambda: entry.doc.fork_at(entry.doc.vv_to_frontiers(version)), "internal")
        read: str = _guard(lambda: rules.render(fork), "internal")
        entry.remember(at, read.encode("utf-8"))
        return entry.texts[at]
    text: str = _guard(lambda: rules.render(doc), "internal")
    return text.encode("utf-8")


def latest(
    cache: DocCache, *, key: str, epoch: int, log_seq: int, rules: DocStrategy
) -> tuple[bytes, bytes] | None:
    """The cached document's content as UTF-8 at the position the worker
    holds, ``log_seq`` or past it, and the version vector there: a committed
    state (the worker moves only onto committed deltas), read without
    checking the document out at an older version (the slow part, on a long
    document). ``None`` on a cache miss."""
    entry = cache.at_least(key, epoch, log_seq)
    if entry is None:
        return None
    text: str = _guard(lambda: rules.render(entry.doc), "internal")
    data, vv = text.encode("utf-8"), encode_vv(entry.doc)
    entry.remember(vv, data)
    return data, vv


@dataclass(frozen=True, slots=True)
class Salvaged:
    """What a rebuild of a broken history recovered: the content text, and
    how many of the logged updates it took and skipped."""

    text: str
    applied: int
    skipped: int


def salvage(*, rules: DocStrategy, snapshot: bytes, updates: list[bytes]) -> Salvaged:
    """Rebuild a document whose stored history no longer loads whole, as far
    as it goes: the snapshot, then every logged update that still imports,
    one at a time, skipping each that fails or depends on history the
    document lacks. Never cached: what it rebuilds is a rescue, not a state
    the store holds.

    Refused (``corrupt``) when the snapshot itself does not import: every
    update was made on it, so nothing after it could be trusted to stand for
    the content."""
    doc = new_doc()
    if snapshot:
        status = _guard(lambda: doc.import_(snapshot), "corrupt")
        if _pending(status):
            raise SandboxError("corrupt", "the snapshot depends on history it does not hold")
    applied = skipped = 0
    for update in updates:
        try:
            status = _guard(functools.partial(doc.import_, update), "corrupt")
        except SandboxError:
            skipped += 1
            continue
        if _pending(status):
            skipped += 1
        else:
            applied += 1
    text: str = _guard(lambda: rules.render(doc), "corrupt")
    return Salvaged(text=text, applied=applied, skipped=skipped)


#: The keys an ephemeral caret value may carry, and how large each may be.
_CURSOR_KEYS: Final = frozenset({"anchor", "focus"})
_MAX_CURSOR_BYTES: Final = 512
#: What an ephemeral store encodes for a key it holds nothing about.
_NOTHING_ENCODED: Final = bytes(EphemeralStore(1).encode("0"))


def _cursor_container(part: Any) -> Any:
    """The container an encoded cursor points into."""
    if not isinstance(part, bytes) or len(part) > _MAX_CURSOR_BYTES:
        raise SandboxError("ephemeral", "a caret position is a short encoded cursor")
    cursor = _guard(functools.partial(loro.Cursor.decode, part), "ephemeral")
    return _guard(functools.partial(getattr, cursor, "container"), "ephemeral")


@dataclass(frozen=True, slots=True)
class Caret:
    """A caret update re-encoded, and what the type lets the backend know of
    it (a notebook's: the cell it is in)."""

    encoded: bytes
    facts: dict[str, Any] = field(default_factory=dict)


def ephemeral(*, rules: DocStrategy, peer: int, data: bytes) -> bytes:
    """``data`` re-encoded from what it decodes to, when it only sets this
    peer's own caret; :class:`SandboxError` otherwise.

    The relay passes on the re-encoding, never the bytes the client sent, so
    whatever else the client packed in goes nowhere."""
    return caret(rules=rules, peer=peer, data=data).encoded


def caret(*, rules: DocStrategy, peer: int, data: bytes) -> Caret:
    """:func:`ephemeral`, with what the type says about the caret."""
    store = EphemeralStore(60_000)
    _guard(lambda: store.apply(data), "ephemeral")
    states = _guard(store.get_all_states, "ephemeral")
    own = str(peer)
    if not states:
        # A deleted caret (the tab lost focus) decodes to no state but keeps
        # its tombstone; only the sender's own one is passed on.
        cleared: bytes = _guard(lambda: bytes(store.encode(own)), "ephemeral")
        if cleared == _NOTHING_ENCODED:
            raise SandboxError(
                "ephemeral", "an ephemeral update may only clear the sender's own key"
            )
        return Caret(encoded=cleared)
    if set(states) != {own}:
        raise SandboxError("ephemeral", "an ephemeral update may only set the sender's own key")
    value = states[own]
    if not isinstance(value, dict):
        raise SandboxError("ephemeral", "a caret is an anchor and a focus")
    facts = rules.caret(value)
    encoded: bytes = _guard(lambda: bytes(store.encode(own)), "ephemeral")
    return Caret(encoded=encoded, facts=facts)


#: Public names for the modules of the types' strategies (the lane's own code
#: keeps the short private spellings).
guard = _guard
pending_import = _pending
forward_spans = _forward
changes_of = _changes
cursor_container = _cursor_container
rewrite_text = _rewrite


__all__ = [
    "CHAT_WORKSPACE",
    "FILE",
    "LORO_VERSION",
    "MAX_TIMESTAMP_SKEW_SECONDS",
    "RULES",
    "SERVER_PEER_MAX",
    "Caret",
    "DocCache",
    "DocRules",
    "DocStrategy",
    "Exported",
    "Loaded",
    "Refused",
    "Salvaged",
    "SandboxError",
    "Seeded",
    "Validated",
    "advance",
    "caret",
    "changes_of",
    "content",
    "cursor_container",
    "decode_vv",
    "encode_vv",
    "ephemeral",
    "export",
    "forward_spans",
    "guard",
    "latest",
    "load",
    "merge",
    "pending_import",
    "register",
    "rewrite_text",
    "rules_for",
    "salvage",
    "seed",
    "snapshot",
    "text_edits",
    "validate",
    "warm",
    "warm_all",
]
