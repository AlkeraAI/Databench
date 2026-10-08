"""Reports agent activity to the org audit log.

Events carry who/what/when/outcome; trace content stays on this machine.
``enqueue`` never blocks and never raises into a decision path. A background
thread batches events to the backend. If the backend is unreachable, events
spill to a bounded spool under ``~/.alkera/audit/`` and flush later. A refusal
pauses delivery until the token changes:
- After a 401 the spool holds the trail for the re-login
- After a 403 batch and spool drop. The ingest takes a member's events whatever
  the organization's plan is, so a 403 is a statement about THIS credential —
  the standing behind it is gone — and there is nothing for a spool to wait
  for. The code the refusal names is logged, so which one it was is on record.
Delivery is at-least-once; ``detail.event_id`` makes a duplicate attributable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from alkera_core.atomic_io import write_text_atomic
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock, LockHeldError, retrying_lock

from alkera_cli.account.auth_file import Profile, load_profile, org_headers
from alkera_cli.account.binding import resolve_profile_or_none, session_profile_key
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.audit import (
    DecisionRecord,
    set_decision_observer,
)

if TYPE_CHECKING:
    from alkera_cli.observability.otel_export import OtelExporter

logger = logging.getLogger(__name__)

_INGEST_PATH = "/api/v1/org/audit-events/agent"
_BATCH_MAX = 100
_FLUSH_INTERVAL_SECONDS = 3.0
# The server rejects a detail over 4 KB; stay comfortably under it.
_OPERATION_TRUNCATE = 200
_SPOOL_MAX_EVENTS = 10_000
_LOCK_TIMEOUT_SECONDS = 15.0
# Transient faults only (OTLP's retryable set); a 401/403 refusal is handled as
# a credential-gated pause in _post, never blind retry.
_RETRY_STATUSES = frozenset({408, 429})
#: The local-only field an event carries (in the queue and the spool, never on
#: the wire) naming the sign-in profile it must be delivered as.
_PROFILE_TAG = "_alkera_profile"


def _now() -> datetime:
    return datetime.now(UTC)


def _statement_hash(record: DecisionRecord) -> str | None:
    material = record.raw or record.operation
    if not material:
        return None
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _wire_event(
    action: str,
    *,
    session_id: str,
    target: str | None,
    detail: dict[str, Any],
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """The JSON shape the ingest route accepts."""
    return {
        "action": action,
        "session_id": session_id or "unknown",
        "occurred_at": (occurred_at or _now()).isoformat(),
        "target": None if target is None else target[:320],
        "detail": {**detail, "event_id": uuid.uuid4().hex},
    }


def _decision_action(record: DecisionRecord) -> str | None:
    """A SQL allow is data access; anything a human ruled on is an escalation
    (the outcome rides in detail); a non-human reject is an auto-denial. Every
    other allow stays local."""
    if record.decision == "allow" and record.capability == "sql":
        return "agent.data_access"
    if record.decided_by == "human":
        return "agent.decision_escalated"
    if record.decision == "reject":
        return "agent.decision_denied"
    return None


def decision_to_event(record: DecisionRecord) -> dict[str, Any] | None:
    """Map one decision to its org-audit event, or ``None`` when it stays
    local. ``raw`` (the full statement or command line) is never sent."""
    action = _decision_action(record)
    if action is None:
        return None
    detail: dict[str, Any] = {
        "source": record.source,
        "capability": record.capability,
        "effect": record.effect,
        "operation": record.operation[:_OPERATION_TRUNCATE],
        "targets": record.targets[:50],
        "mode": record.mode,
        "decision": record.decision,
        "decided_by": record.decided_by,
        "reasons": record.reasons[:10],
    }
    if record.request_id:
        detail["request_id"] = record.request_id
    statement = _statement_hash(record)
    if action == "agent.data_access" and statement is not None:
        detail["statement_hash"] = statement
    target = record.targets[0] if record.targets else record.capability
    occurred = datetime.fromtimestamp(record.at, tz=UTC) if record.at else None
    return _wire_event(
        action, session_id=record.session_id, target=target, detail=detail, occurred_at=occurred
    )


def _refusal_code(response: httpx.Response) -> str:
    """The error code a structured refusal names, or ``""``.

    A 403 on the ingest is one of several different things — the member row
    behind this token is gone, the account's address was never verified — and
    the box's log is the only place anybody finds out which. The body is
    whatever the server sent, so every shape that is not the envelope reads as
    unnamed rather than raising inside the delivery thread.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("error", "detail"):
        holder = body.get(key)
        if not isinstance(holder, dict):
            continue
        code = holder.get("code")
        if isinstance(code, str):
            return code
    return ""


def _disposition(response: httpx.Response) -> str:
    """'sent', 'retry' (spool and try again later), or 'dropped' (poison)."""
    if 200 <= response.status_code < 300:
        return "sent"
    if response.status_code in _RETRY_STATUSES or response.status_code >= 500:
        return "retry"
    logger.warning(
        "audit report batch rejected (%s): %s", response.status_code, response.text[:200]
    )
    return "dropped"


def _dropped_count(events: list[dict[str, Any]]) -> int:
    """A swallowed gap marker contributes its own count, not 1."""
    total = 0
    for event in events:
        if event.get("action") == "agent.audit_gap":
            total += int(event.get("detail", {}).get("dropped", 0) or 0)
        else:
            total += 1
    return total


def _trim_to_cap(events: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Bound the spool: keep the newest, lead with a gap marker for the rest.

    The marker takes one of the ``cap`` slots, so a failed retry re-trims
    nothing and the gap total survives however long the outage lasts."""
    if len(events) <= cap:
        return events
    keep = max(cap - 1, 1)
    dropped = _dropped_count(events[: len(events) - keep])
    kept = events[-keep:]
    session_id = str(kept[0].get("session_id", "unknown"))
    gap = _wire_event(
        "agent.audit_gap", session_id=session_id, target=None, detail={"dropped": dropped}
    )
    return [gap, *kept]


class AuditReporter:
    """Batches agent-activity events to the backend without ever blocking.

    Test seams: ``transport``, ``token_provider``, ``start_worker=False`` with
    :meth:`flush_now`.
    """

    def __init__(
        self,
        *,
        api_url: str,
        token_provider: Callable[[], str | None],
        credential_for: Callable[[str], Profile | None] | None = None,
        spool_path: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        spool_max_events: int = _SPOOL_MAX_EVENTS,
        start_worker: bool = True,
    ) -> None:
        self._token_provider = token_provider
        #: The profile an event a chat claimed is delivered as (by its key).
        self._credential_for = credential_for
        # Read the home at construction, never at import: the reporter is built
        # per process, and a home bound at import time cannot be re-pointed.
        self._spool_path = spool_path or paths.ALKERA_HOME / "audit" / "spool.jsonl"
        self._spool_max = spool_max_events
        self._client = httpx.Client(base_url=api_url, transport=transport, timeout=10.0)
        self._queue: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        # The spool is shared by every alkera process of this user (parallel
        # per-worktree daemons), so the read-modify-write takes a cross-process
        # FileLock on top of the in-process lock.
        self._spool_lock = threading.Lock()
        self._spool_file_lock = FileLock(self._spool_path.parent / ".spool.lock")
        # A refused credential pauses delivery until the token CHANGES — a 401
        # heals by re-login, a 403 (not Enterprise) by an upgrade + re-login.
        # No clock, no probing: one request per refused token, ever.
        self._refused_token: str | None = None
        # 401 keeps the spool (the org is entitled; identity is the fault);
        # 403 drops it (the org has no audit feature — nothing is owed).
        self._refusal_spools = True
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._run, name="alkera-audit", daemon=True)
            self._thread.start()

    @classmethod
    def from_stored_auth(cls) -> AuditReporter | None:
        """``None`` when nobody is logged in. Tokens are re-read per delivery,
        so a re-login is picked up without a restart. An event from a chat is
        delivered as the sign-in profile that chat bound, so it lands in that
        chat's org even after a switch; an event no chat claims goes as the
        profile resolved for the process."""
        current = resolve_profile_or_none()
        if current is None:
            return None
        return cls(
            api_url=current.api_url,
            token_provider=lambda: p.token if (p := resolve_profile_or_none()) else None,
            credential_for=load_profile,
        )

    # --- producers (any thread; never block, never raise) ---------------

    def enqueue(self, event: dict[str, Any]) -> None:
        key = session_profile_key(str(event.get("session_id") or ""))
        self._queue.put({**event, _PROFILE_TAG: key} if key else event)

    def on_decision_record(self, record: DecisionRecord) -> None:
        """The ``set_decision_observer`` hook."""
        event = decision_to_event(record)
        if event is not None:
            self.enqueue(event)

    def session_started(self, *, session_id: str, project: str, detail: dict[str, Any]) -> None:
        self.enqueue(
            _wire_event(
                "agent.session_started", session_id=session_id, target=project, detail=detail
            )
        )

    def session_finished(self, *, session_id: str, project: str, detail: dict[str, Any]) -> None:
        self.enqueue(
            _wire_event(
                "agent.session_finished", session_id=session_id, target=project, detail=detail
            )
        )

    def session_cost(
        self, *, session_id: str, connection_usd: dict[str, float], total_usd: float
    ) -> None:
        top = max(connection_usd, key=connection_usd.__getitem__) if connection_usd else None
        detail = {
            "by_connection": {k: round(v, 6) for k, v in connection_usd.items()},
            "total_usd": round(total_usd, 6),
        }
        self.enqueue(_wire_event("agent.cost", session_id=session_id, target=top, detail=detail))

    # --- delivery --------------------------------------------------------

    def flush_now(self) -> None:
        """One synchronous delivery cycle, spool first."""
        self._deliver(self._drain())

    def close(self, timeout_seconds: float = 3.0) -> None:
        """Best-effort final flush, then stop the worker."""
        self._closed.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
            self._thread = None
        else:
            self.flush_now()
        self._client.close()

    def _drain(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return items
            if isinstance(item, dict):
                items.append(item)

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=_FLUSH_INTERVAL_SECONDS)
            except queue.Empty:
                item = None
            pending = ([item] if isinstance(item, dict) else []) + self._drain()
            try:
                self._deliver(pending)
            except Exception:  # the reporter must survive anything
                logger.warning("audit report delivery cycle failed", exc_info=True)
        try:
            self.flush_now()
        except Exception:
            logger.warning("audit report final flush failed", exc_info=True)

    def _deliver(self, fresh: list[dict[str, Any]]) -> None:
        self._spool_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                self._spool_lock,
                retrying_lock(self._spool_file_lock, timeout_seconds=_LOCK_TIMEOUT_SECONDS),
            ):
                self._deliver_locked(fresh)
        except LockHeldError:  # another process is mid-delivery; keep ours queued
            for event in fresh:
                self._queue.put(event)

    def _deliver_locked(self, fresh: list[dict[str, Any]]) -> None:
        events = self._read_spool() + fresh
        if not events:
            return
        groups: dict[str | None, list[dict[str, Any]]] = {}
        for event in events:
            tag = event.get(_PROFILE_TAG)
            groups.setdefault(tag if isinstance(tag, str) and tag else None, []).append(event)
        kept: list[dict[str, Any]] = []
        for tag, group in groups.items():
            kept.extend(self._deliver_group(tag, group))
        self._write_spool(kept)

    def _route(self, tag: str | None) -> tuple[str, str, str] | None:
        """``(url, token, org)`` for a group of events, or None to keep them.
        A claimed event goes only as the profile that claimed it: when that
        profile is gone the event waits for its re-login, never for another."""
        if tag is None:
            token = self._token_provider()
            return (_INGEST_PATH, token, "") if token else None
        profile = self._credential_for(tag) if self._credential_for is not None else None
        if profile is None:
            return None
        return (profile.api_url.rstrip("/") + _INGEST_PATH, profile.token, profile.org_team_id)

    def _deliver_group(self, tag: str | None, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Post one profile's events; return what stays spooled."""
        route = self._route(tag)
        if route is None:
            return events
        url, token, org = route
        if token == self._refused_token:
            return events if self._refusal_spools else []
        self._refused_token = None
        while events:
            outcome = self._post(events[:_BATCH_MAX], token, url=url, org=org)
            if outcome == "retry":
                break
            if outcome == "drop_all":
                events = []
                break
            events = events[_BATCH_MAX:]
        return events

    def _post(
        self, batch: list[dict[str, Any]], token: str, *, url: str = _INGEST_PATH, org: str = ""
    ) -> str:
        headers = org_headers(token, org)
        wire = [{k: v for k, v in event.items() if k != _PROFILE_TAG} for event in batch]
        try:
            response = self._client.post(url, json={"events": wire}, headers=headers)
        except httpx.HTTPError:
            return "retry"
        if response.status_code == 401:
            self._refused_token = token
            self._refusal_spools = True
            return "retry"
        if response.status_code == 403:
            self._refused_token = token
            self._refusal_spools = False
            logger.info(
                "org audit reporting refused (%s); paused until this machine signs in again",
                _refusal_code(response) or "forbidden",
            )
            return "drop_all"
        return _disposition(response)

    def _read_spool(self) -> list[dict[str, Any]]:
        try:
            return list(iter_jsonl(self._spool_path))
        except OSError:
            logger.warning("audit spool unreadable", exc_info=True)
            return []

    def _write_spool(self, events: list[dict[str, Any]]) -> None:
        try:
            if not events:
                self._spool_path.unlink(missing_ok=True)
                return
            events = _trim_to_cap(events, self._spool_max)
            text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
            write_text_atomic(self._spool_path, text)
        except OSError:
            logger.warning("audit spool write failed", exc_info=True)


_default: AuditReporter | None = None
_default_lock = threading.Lock()


def _feed(callback: Callable[[DecisionRecord], None], record: DecisionRecord) -> None:
    try:
        callback(record)
    except Exception:
        logger.warning("decision observer consumer failed", exc_info=True)


def _fan_out(
    callbacks: Sequence[Callable[[DecisionRecord], None]],
) -> Callable[[DecisionRecord], None]:
    def observer(record: DecisionRecord) -> None:
        for callback in callbacks:
            _feed(callback, record)

    return observer


def _install_observer(reporter: AuditReporter | None, exporter: OtelExporter | None) -> None:
    """Fill the sink's single observer slot from both consumers — org-audit
    reporting and OTel export — so neither's presence displaces the other."""
    maybe = (
        reporter.on_decision_record if reporter is not None else None,
        exporter.on_decision_record if exporter is not None else None,
    )
    callbacks = [callback for callback in maybe if callback is not None]
    if len(callbacks) > 1:
        set_decision_observer(_fan_out(callbacks))
    else:
        set_decision_observer(callbacks[0] if callbacks else None)


def reinstall_observer() -> None:
    """Recompose the observer slot from the consumers that are ALIVE right now.
    Teardown paths route here (peeking, never building), so closing one
    consumer can neither strand a shut-down one in the slot nor resurrect it."""
    from alkera_cli.observability.otel_export import peek_exporter

    with _default_lock:
        reporter = _default
    _install_observer(reporter, peek_exporter())


def _create_default() -> AuditReporter | None:
    from alkera_cli.observability.otel_export import default_exporter

    reporter = AuditReporter.from_stored_auth()
    _install_observer(reporter, default_exporter())
    return reporter


def default_reporter() -> AuditReporter | None:
    """The process-wide reporter, shared by a daemon's project runtimes.

    Creating it installs the decision observer. Returns ``None`` while logged
    out and re-checks on the next call, so a mid-process login starts
    reporting."""
    global _default
    with _default_lock:
        if _default is None:
            _default = _create_default()
        return _default


def close_default_reporter() -> None:
    """Flush + stop the shared reporter; the OTel consumer (if any) keeps the
    observer slot."""
    global _default
    with _default_lock:
        reporter, _default = _default, None
    if reporter is None:
        return
    reinstall_observer()
    reporter.close()


__all__ = [
    "AuditReporter",
    "close_default_reporter",
    "decision_to_event",
    "default_reporter",
]
