"""What a box may ship of its own logs, and the one rule that decides it.

A box ships its supervisor's system and worker-lifecycle events, and nothing
else: never a chat's content, a file, a prompt, a tool's input or output, a
token or a credential. The rule is an allowlist on two levels. An event name
not in :data:`EVENT_FIELDS` is dropped whole; a field the event does not list
is dropped, and a listed field whose value is not of its declared kind is
dropped too. Free text (an exception's message) is the one kind that could
carry something unexpected, so it is cut to :data:`TEXT_MAX_CHARS` and every
secret-shaped run in it is replaced first.

:func:`sanitize` is applied twice with the same table: on the box, before an
event enters the shipper's buffer, and on the server, before the backend
re-emits an event a box posted. A box that was changed to send more still
lands nothing outside the table.

Standard library only: the box's root process imports this, and it keeps
structlog, Rich and the database layer out.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from alkera_core.compute.box_isolation import IsolationProfile
from alkera_core.compute.worker_faults import WorkerFault

#: The kind a field's value must be. ``id`` is a UUID (an org, a chat), ``code``
#: one of a fixed set of short words, ``command`` a unit command line with no
#: arguments that could carry data, ``text`` free text, scrubbed and cut.
FieldKind = Literal["int", "number", "bool", "id", "code", "command", "text"]

#: The reason codes a refusal carries (``alkera_cli.supervisor.org_events.REFUSAL_CODES``
#: and its ``other``) and the faults a worker fails with
#: (:class:`~alkera_core.compute.worker_faults.WorkerFault`): the only values a
#: ``reason`` field may take.
REASON_CODES: Final = frozenset(
    {"another_org", "not_routed", "other", *(fault.value for fault in WorkerFault)}
)
#: The values each ``code`` field may take.
CODE_VALUES: Final[Mapping[str, frozenset[str]]] = {
    "reason": REASON_CODES,
    "profile": frozenset(profile.value for profile in IsolationProfile),
}

FIELD_KINDS: Final[Mapping[str, FieldKind]] = {
    "slot": "int",
    "org_id": "id",
    "chat_id": "id",
    "pid": "int",
    "final": "bool",
    "backoff": "number",
    "ran": "number",
    "restarts": "int",
    "window": "number",
    "error": "text",
    "reason": "code",
    "profile": "code",
    "count": "int",
    "idle_seconds": "number",
    "waited": "number",
    "killed": "bool",
    "command": "command",
}

#: The shipper's own report that its buffer overflowed: ``count`` events were
#: dropped, oldest first, since the last report.
SHIPPER_DROPPED: Final = "box.logs.dropped"

#: The events a box's supervisor emits about its org workers' lives
#: (``alkera_cli.supervisor.org_events`` emits them; the backend re-emits what a
#: box posts), spelled once here for both.
WORKER_STARTED: Final = "supervisor.worker.started"
WORKER_START_FAILED: Final = "supervisor.worker.start_failed"
WORKER_READY: Final = "supervisor.worker.ready"
WORKER_STOPPING: Final = "supervisor.worker.stopping"
WORKER_EXITED: Final = "supervisor.worker.exited"
WORKER_CRASH_LOOP: Final = "supervisor.worker.crash_loop"
WORKER_PROTOCOL_BROKEN: Final = "supervisor.worker.protocol_broken"
WORKER_KILLED: Final = "supervisor.worker.killed"
EARLIER_WORKERS_OUTLASTED: Final = "supervisor.earlier_workers.outlasted"
ORG_ADMISSION_REFUSED: Final = "supervisor.org_admission.refused"
ORG_REAPED: Final = "supervisor.org.reaped"
ORG_REAP_FAILED: Final = "supervisor.org.reap_failed"
SLOTS_EXHAUSTED: Final = "supervisor.slots.exhausted"
UNIT_COMMAND_TIMED_OUT: Final = "supervisor.unit_command.timed_out"
#: Once at startup: the isolation profile the box's probe found.
ISOLATION_PROBED: Final = "supervisor.isolation.probed"
PREREQUISITES_MISSING: Final = "supervisor.prerequisites.missing"

#: Every event a box may ship, with the fields it may carry.
EVENT_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    WORKER_STARTED: frozenset({"slot", "org_id", "pid"}),
    WORKER_START_FAILED: frozenset({"org_id", "reason", "error"}),
    WORKER_READY: frozenset({"slot", "org_id"}),
    WORKER_STOPPING: frozenset({"slot", "org_id", "final"}),
    WORKER_EXITED: frozenset({"slot", "org_id", "backoff", "ran"}),
    WORKER_CRASH_LOOP: frozenset({"slot", "org_id", "restarts", "window", "backoff", "reason"}),
    WORKER_PROTOCOL_BROKEN: frozenset({"slot", "org_id", "error"}),
    WORKER_KILLED: frozenset({"slot", "org_id"}),
    EARLIER_WORKERS_OUTLASTED: frozenset({"waited", "killed"}),
    ORG_ADMISSION_REFUSED: frozenset({"slot", "org_id", "chat_id", "reason", "count"}),
    ORG_REAPED: frozenset({"slot", "org_id", "idle_seconds"}),
    ORG_REAP_FAILED: frozenset({"slot", "org_id", "error"}),
    SLOTS_EXHAUSTED: frozenset({"org_id", "error"}),
    UNIT_COMMAND_TIMED_OUT: frozenset({"command"}),
    ISOLATION_PROBED: frozenset({"profile"}),
    PREREQUISITES_MISSING: frozenset({"error"}),
    SHIPPER_DROPPED: frozenset({"count"}),
}
#: The supervisor's events: every shippable event but the shipper's own.
SUPERVISOR_EVENTS: Final = tuple(e for e in EVENT_FIELDS if e != SHIPPER_DROPPED)
#: The supervisor's events about the box as a whole. Every other one is about
#: one org's worker or data and names that org itself (``org_id``, the slot's),
#: so nothing downstream fills it in from whatever identity is ambient where
#: the line is logged. A new event is about an org unless it is listed here.
BOX_EVENTS: Final = frozenset(
    {EARLIER_WORKERS_OUTLASTED, UNIT_COMMAND_TIMED_OUT, ISOLATION_PROBED, PREREQUISITES_MISSING}
)
ORG_EVENTS: Final = frozenset(SUPERVISOR_EVENTS) - BOX_EVENTS

#: The levels an event may be shipped at.
LEVELS: Final = frozenset({"debug", "info", "warning", "error", "critical"})

TEXT_MAX_CHARS: Final = 300
COMMAND_MAX_CHARS: Final = 120
TIMESTAMP_MAX_CHARS: Final = 40
#: The most events one request to the backend carries.
MAX_BATCH_EVENTS: Final = 50
#: The most bytes one request's JSON body may take on the wire. The edge WAF
#: refuses bodies past 8 KB on every path it does not exempt, so a batch is
#: packed under that with room for the envelope.
MAX_BATCH_BYTES: Final = 7 * 1024
#: The ingestion route, on the machine credential.
INGEST_PATH: Final = "/api/v1/machines/me/logs"

REDACTED: Final = "[redacted]"

_COMMAND_RE = re.compile(r"[A-Za-z0-9@._: -]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_AUTH_RE = re.compile(r"\b(?:bearer|basic|token)\s*[:=]?\s*\S+", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(
    r"\b(?:password|passwd|secret|credential|api[_-]?key|access[_-]?key|token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_PREFIXED_RE = re.compile(
    r"\b(?:alk_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{8,}|(?:AKIA|ASIA)[0-9A-Z]{12,})"
)
#: A long unbroken run of token characters: what a key, a token or an encoded
#: blob looks like. A UUID (an id the event may name) is kept, and an absolute
#: path is judged a segment at a time.
_RUN_RE = re.compile(r"[A-Za-z0-9_+/=-]{24,}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_RUN_MIN: Final = 24
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _blob(text: str) -> bool:
    """Whether ``text`` is long enough, once its UUIDs are set aside, to be a
    key or a token."""
    return len(_UUID_RE.sub("", text)) >= _RUN_MIN


def _scrub_run(match: re.Match[str]) -> str:
    run = match.group(0)
    if run.startswith("/"):
        return "/".join(REDACTED if _blob(part) else part for part in run.split("/"))
    return REDACTED if _blob(run) else run


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower() and len(value) == 36
    except ValueError:
        return False


def scrub_text(value: str) -> str:
    """Free text with every secret-shaped run replaced, control characters
    flattened, cut to :data:`TEXT_MAX_CHARS`."""
    out = _CONTROL_RE.sub(" ", value[: TEXT_MAX_CHARS * 4])
    for pattern in (_JWT_RE, _KEY_VALUE_RE, _AUTH_RE, _PREFIXED_RE):
        out = pattern.sub(REDACTED, out)
    out = _RUN_RE.sub(_scrub_run, out)
    return out[:TEXT_MAX_CHARS]


def _value(kind: FieldKind, field: str, value: object) -> tuple[bool, object]:
    """``(kept, value)``: the value as it may be shipped, or not kept."""
    if value is None:
        # An absent figure (a crash loop with no slot held) says so plainly.
        return kind in ("int", "number"), None
    if kind == "bool":
        return isinstance(value, bool), value
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool), value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False, None
        return math.isfinite(value), value
    if not isinstance(value, str):
        return False, None
    if kind == "id":
        return _is_uuid(value), value.lower()
    if kind == "code":
        return value in CODE_VALUES.get(field, frozenset()), value
    if kind == "command":
        ok = len(value) <= COMMAND_MAX_CHARS and _COMMAND_RE.fullmatch(value) is not None
        return ok and not any(_blob(word) for word in value.split()), value
    return True, scrub_text(value)


def sanitize_fields(event: str, fields: Mapping[str, object]) -> dict[str, object] | None:
    """The fields of ``event`` that may leave the box, or ``None`` for an
    event that may not leave at all."""
    allowed = EVENT_FIELDS.get(event)
    if allowed is None:
        return None
    kept: dict[str, object] = {}
    for name, value in fields.items():
        if name not in allowed:
            continue
        ok, clean = _value(FIELD_KINDS[name], name, value)
        if ok:
            kept[name] = clean
    return kept


def sanitize_level(level: object) -> str:
    """A level the shipper and the backend use; anything else is ``info``."""
    text = str(level).lower()
    return text if text in LEVELS else "info"


def sanitize_timestamp(value: object) -> str | None:
    """An ISO 8601 timestamp as the box wrote it, or ``None``."""
    if not isinstance(value, str) or len(value) > TIMESTAMP_MAX_CHARS:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def sanitize(
    event: object, level: object, timestamp: object, fields: Mapping[str, object]
) -> dict[str, object] | None:
    """One shippable record (``timestamp``, ``level``, ``event`` and the kept
    fields), or ``None`` when the event may not be shipped."""
    if not isinstance(event, str):
        return None
    kept = sanitize_fields(event, fields)
    if kept is None:
        return None
    return {
        "timestamp": sanitize_timestamp(timestamp),
        "level": sanitize_level(level),
        "event": event,
        **kept,
    }


__all__ = [
    "BOX_EVENTS",
    "CODE_VALUES",
    "COMMAND_MAX_CHARS",
    "EARLIER_WORKERS_OUTLASTED",
    "EVENT_FIELDS",
    "FIELD_KINDS",
    "INGEST_PATH",
    "ISOLATION_PROBED",
    "LEVELS",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_EVENTS",
    "ORG_ADMISSION_REFUSED",
    "ORG_EVENTS",
    "ORG_REAPED",
    "ORG_REAP_FAILED",
    "PREREQUISITES_MISSING",
    "REASON_CODES",
    "REDACTED",
    "SHIPPER_DROPPED",
    "SLOTS_EXHAUSTED",
    "SUPERVISOR_EVENTS",
    "TEXT_MAX_CHARS",
    "UNIT_COMMAND_TIMED_OUT",
    "WORKER_CRASH_LOOP",
    "WORKER_EXITED",
    "WORKER_KILLED",
    "WORKER_PROTOCOL_BROKEN",
    "WORKER_READY",
    "WORKER_STARTED",
    "WORKER_START_FAILED",
    "WORKER_STOPPING",
    "FieldKind",
    "sanitize",
    "sanitize_fields",
    "sanitize_level",
    "sanitize_timestamp",
    "scrub_text",
]
