"""The append-only permission record: every decision, and the intents behind them.

Two logs live next to the chat they belong to. ``decisions.jsonl`` holds one
:class:`DecisionRecord` per gated decision, including the auto-allow and
auto-reject that never reach a human. ``intents.jsonl`` holds the structured
acknowledgments an agent files to proceed through a refusal, which is why the
two share a module: a decision that reads "allowed, covered by intent" is only
readable next to the declaration that covered it.

A write failure is LOUD. The sink raises, and the decision that could not be
recorded fails closed. Availability over audit is exactly the behavior that let
a gate decide in silence.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from alkera_core.atomic_io import append_line
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock, retrying_lock
from alkera_core.versioning import VersionedModel
from pydantic import Field, ValidationError

from alkera_cli.contracts.tool_types import ActionDescriptor
from alkera_cli.plugins.plugin_base.urns import (
    COLUMN_SEP,
    dataset_relation_fqn,
    parse_column_urn,
)

logger = logging.getLogger(__name__)


class AuditUnavailableError(RuntimeError):
    """The decision could not be recorded, so it must not be granted."""


# Process-wide observer for freshly-recorded decisions. The org-audit reporter
# subscribes here so every sink instance feeds one pipeline, like a logging
# handler. None in tests unless a test sets it.
_observer: Callable[[DecisionRecord], None] | None = None


def set_decision_observer(observer: Callable[[DecisionRecord], None] | None) -> None:
    """Install (or clear, with ``None``) the process-wide decision observer."""
    global _observer
    _observer = observer


class DecisionRecord(VersionedModel):
    """One audited permission decision. Self-contained (the descriptor flattened
    into the queryable fields) so the log reads without joining anything."""

    SCHEMA_VERSION = "1.1.0"

    at: float = 0.0
    """Epoch seconds the decision was made."""
    session_id: str = ""
    request_id: str = ""
    tool_call_id: str | None = None
    source: str = "harness"
    """``harness`` (adapter permission seam) | ``sql_gate`` | ``daemon_tool``."""
    capability: str = ""
    effect: str = ""
    operation: str = ""
    raw: str | None = None
    targets: list[str] = Field(default_factory=list)
    mode: str = ""
    decision: str = ""
    """``allow`` | ``prompt`` | ``reject``."""
    decided_by: str = ""
    """``read`` | ``rule`` | ``mode`` | ``floor`` | ``confidence`` | ``kind`` |
    ``human`` | ``judge`` | ``bypass`` | ``impact`` | ``intent`` | ``unsure`` |
    ``run`` (a notebook run a person ran or approved)."""
    reasons: list[str] = Field(default_factory=list)
    target_urns: list[str] = Field(default_factory=list)
    """The graph identity of what the action changes, added in 1.1.0. ``targets``
    keeps the names the statement used; a URN is what joins to lineage."""
    impact_urns: list[str] = Field(default_factory=list)
    """The downstream assets the change reaches."""
    impact_status: str = ""
    """``resolved`` | ``degraded``, or ``""`` when no graph was consulted."""
    impact_category: str = ""
    """The worst downstream category: ``breaking`` | ``potentially_breaking`` |
    ``non_breaking``."""
    intent_id: str = ""
    """The declaration that covered this write, when one did."""

    @classmethod
    def from_descriptor(
        cls,
        descriptor: ActionDescriptor,
        *,
        decision: str,
        decided_by: str,
        mode: str = "",
        source: str = "harness",
        session_id: str = "",
        request_id: str = "",
        tool_call_id: str | None = None,
        at: float | None = None,
        extra_reasons: list[str] | None = None,
    ) -> DecisionRecord:
        return cls(
            at=at if at is not None else time.time(),
            session_id=session_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
            source=source,
            capability=descriptor.capability,
            effect=str(descriptor.effect),
            operation=descriptor.operation,
            raw=descriptor.raw,
            targets=[t.name for t in descriptor.targets],
            mode=mode,
            decision=decision,
            decided_by=decided_by,
            reasons=[*descriptor.reasons, *(extra_reasons or [])],
        )

    def with_impact(self, impact: Any, *, intent_id: str = "") -> DecisionRecord:
        """A copy carrying what the graph said and the declaration that covered it.
        ``impact`` is an ``ImpactAssessment`` (loosely typed so the audit layer
        keeps no lineage import); ``None`` leaves the fields empty, which reads as
        "no graph was consulted"."""
        if impact is None and not intent_id:
            return self
        return self.model_copy(
            update={
                "target_urns": list(getattr(impact, "targets", ()) or ()),
                "impact_urns": [a.urn for a in getattr(impact, "affected", ()) or ()],
                "impact_status": str(getattr(impact, "status", "") or ""),
                "impact_category": str(getattr(impact, "category", "") or ""),
                "intent_id": intent_id,
            }
        )


class DecisionSink:
    """Appends :class:`DecisionRecord`s to ``<dir>/decisions.jsonl``.

    ``directory`` is usually a CHAT directory — the audit log lives next to that
    chat's ``chat.jsonl`` so each conversation owns its own decision history
    (``ChatSession`` builds one per chat). A project-level sink (``project.path``)
    backs the rare non-chat ``tool.call`` path.

    Pairs a cross-process ``FileLock`` with an in-process ``threading.Lock`` (the
    ``CostLedger`` lesson — a shared FileLock under the daemon's
    ``asyncio.to_thread`` pool would otherwise starve), since the same log may be
    written from the CLI, the daemon, and worker threads at once."""

    def __init__(self, directory: Path, *, lock_timeout_seconds: float = 2.0) -> None:
        self._dir = Path(directory)
        self._path = self._dir / "decisions.jsonl"
        self._lock = FileLock(self._dir / ".decisions.lock")
        self._lock_timeout = lock_timeout_seconds
        self._tlock = threading.Lock()

    @property
    def directory(self) -> Path:
        """Where this sink's logs live. The intent ledger sits beside them."""
        return self._dir

    def record(self, rec: DecisionRecord) -> None:
        """Append one record, raising :class:`AuditUnavailableError` when the append
        fails. The caller turns that into a refusal: a decision no one can read
        back later is not a decision the gate may grant."""
        try:
            with self._tlock, retrying_lock(self._lock, timeout_seconds=self._lock_timeout):
                self._path.parent.mkdir(parents=True, exist_ok=True)
                append_line(self._path, rec.model_dump_json())
        except Exception as exc:
            logger.warning("decisions.jsonl append failed", exc_info=True)
            detail = str(exc) or "the decisions log could not be written"
            raise AuditUnavailableError(detail) from exc
        finally:
            # Fed even when the local append failed; the two channels are
            # independent, and the org reporter is not a reason to fail closed.
            if _observer is not None:
                try:
                    _observer(rec)
                except Exception:
                    logger.warning("decision observer failed", exc_info=True)

    def read(self) -> list[DecisionRecord]:
        """Every readable record (a torn trailing line is skipped by
        ``iter_jsonl``; an unreadable mid-file row is dropped)."""
        out: list[DecisionRecord] = []
        for raw in iter_jsonl(self._path):
            try:
                out.append(DecisionRecord.model_validate(raw))
            except ValidationError:
                continue
        return out


class NotedDecisionSink:
    """A sink that records every decision with one more reason: the occasion
    it was made on, such as a mode change having an ask decided again."""

    def __init__(self, sink: DecisionSink, note: str) -> None:
        self._sink = sink
        self._note = note

    @property
    def directory(self) -> Path:
        return self._sink.directory

    def record(self, rec: DecisionRecord) -> None:
        self._sink.record(rec.model_copy(update={"reasons": [*rec.reasons, self._note]}))


# ---------------------------------------------------------------------------
# Declared intent -- the acknowledgment that turns a refusal into a proceed
# ---------------------------------------------------------------------------


class DeclaredIntent(VersionedModel):
    """The assets a write means to break, and why."""

    SCHEMA_VERSION = "1.0.0"

    intent_id: str = ""
    session_id: str = ""
    at: float = 0.0
    reason: str = ""
    """The agent's own words for why the break is wanted."""
    assets: list[str] = Field(default_factory=list)
    """The declared set, as the agent named it (a URN or a dotted relation name)."""

    def keys(self) -> set[str]:
        """Every spelling this declaration covers, lowercased for comparison."""
        out: set[str] = set()
        for asset in self.assets:
            out |= declared_keys(asset)
        return out


def declared_keys(asset: str) -> set[str]:
    """The spellings one DECLARED asset covers. A relation covers itself under its
    URN and dotted names (and, through :func:`coverage_keys` on the affected side,
    its columns); a declared column covers only that column, because acknowledging
    one column says nothing about its siblings. A ``#`` with no column after it
    names no column at all and so covers nothing, since falling back to the
    relation would make one character an acknowledgment of every column."""
    s = asset.strip().lower()
    if not s:
        return set()
    if COLUMN_SEP in s:
        return {s} if parse_column_urn(s)[1] else set()
    return coverage_keys(s)


def coverage_keys(urn: str) -> set[str]:
    """The spellings under which one AFFECTED asset counts as covered: its URN,
    its owning relation, and the dotted relation name -- so a declaration naming a
    table covers the table's columns, which is what a migration means when it
    names the table."""
    s = urn.strip().lower()
    if not s:
        return set()
    keys = {s}
    relation = parse_column_urn(s)[0]
    if relation:
        keys.add(relation)
        parts = dataset_relation_fqn(relation)
        if parts:
            keys.add(".".join(p.lower() for p in parts))
    return keys


def covers(intent: DeclaredIntent, urns: Iterable[str]) -> bool:
    """Whether ``intent`` names every one of ``urns``. Coverage is all-or-nothing,
    so a write that reaches one asset outside the declared set still challenges."""
    declared = intent.keys()
    wanted = [u for u in urns if u]
    if not declared or not wanted:
        return False
    return all(coverage_keys(u) & declared for u in wanted)


def draft_intent(*, session_id: str, assets: Sequence[str], reason: str) -> DeclaredIntent:
    """A declaration as the agent filed it, not yet recorded. The gate weighs it
    against the call it accompanies and COMMITS it only when it covered what that
    call breaks and the call was authorized, so neither a refused attempt nor a
    write that breaks nothing can govern a later retry in a laxer mode."""
    return DeclaredIntent(
        intent_id=secrets.token_hex(8),
        session_id=session_id,
        at=time.time(),
        reason=reason.strip(),
        assets=[a.strip() for a in assets if a and a.strip()],
    )


class IntentLedger:
    """The session's committed declarations, appended to ``<dir>/intents.jsonl``.

    Reads are served from memory after the first load; the file is the durable
    copy a later session (or a human reading the trail) sees. A failed append is
    logged, not raised: the in-memory copy still covers this session's later
    writes, and the decision records carry the same intent id."""

    def __init__(self, directory: Path, *, lock_timeout_seconds: float = 2.0) -> None:
        self._dir = Path(directory)
        self._path = self._dir / "intents.jsonl"
        self._lock = FileLock(self._dir / ".intents.lock")
        self._lock_timeout = lock_timeout_seconds
        self._tlock = threading.Lock()
        self._cache: list[DeclaredIntent] | None = None

    def commit(self, intent: DeclaredIntent) -> None:
        """Record one declaration. An empty asset list is still recorded (the trail
        keeps what the agent claimed) but covers nothing."""
        with self._tlock:
            self._append(intent)
            if self._cache is None:
                self._cache = self._load()
            self._cache.append(intent)

    def covering(self, *, session_id: str, urns: Sequence[str]) -> DeclaredIntent | None:
        """The most recent declaration of this session that names every URN."""
        if not urns:
            return None
        with self._tlock:
            if self._cache is None:
                self._cache = self._load()
            rows = list(self._cache)
        return next(
            (i for i in reversed(rows) if i.session_id == session_id and covers(i, urns)), None
        )

    def _append(self, intent: DeclaredIntent) -> None:
        try:
            with retrying_lock(self._lock, timeout_seconds=self._lock_timeout):
                self._path.parent.mkdir(parents=True, exist_ok=True)
                append_line(self._path, intent.model_dump_json())
        except Exception:
            logger.warning("intents.jsonl append failed", exc_info=True)

    def _load(self) -> list[DeclaredIntent]:
        out: list[DeclaredIntent] = []
        for raw in iter_jsonl(self._path):
            try:
                out.append(DeclaredIntent.model_validate(raw))
            except ValidationError:
                continue
        return out


def ledger_for_sink(sink: Any) -> IntentLedger | None:
    """The intent ledger beside ``sink``'s decisions log, or ``None`` when the sink
    keeps no directory (a test double)."""
    directory = getattr(sink, "directory", None)
    return None if directory is None else IntentLedger(Path(directory))


__all__ = [
    "AuditUnavailableError",
    "DecisionRecord",
    "DecisionSink",
    "DeclaredIntent",
    "IntentLedger",
    "NotedDecisionSink",
    "draft_intent",
    "ledger_for_sink",
    "set_decision_observer",
]
