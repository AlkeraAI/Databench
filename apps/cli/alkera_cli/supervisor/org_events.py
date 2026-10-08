"""The events a box's supervisor emits about its org workers' lives.

Each is one JSON line on standard error with a stable event name and plain
fields (slot numbers, org ids, counts, seconds, fixed reason codes), never a
chat's content, a file, a prompt, a token or a credential: ops alarm on them
by name and the box's log shipper forwards exactly these (:data:`EVENTS`). The
line has the shape structlog's JSON renderer gives the rest of the product
(``timestamp``, ``level``, ``event`` and the fields); it is written with the
standard library because structlog loads Rich, which the box's root process
keeps out (``apps/cli/tests/supervisor/test_supervisor_entry.py``).

Two of them are alarms in their own right. ``WORKER_CRASH_LOOP`` says an
org's worker keeps exiting (:mod:`alkera_cli.supervisor.crash_loop`), and
``ORG_ADMISSION_REFUSED`` says a worker refused a chat the supervisor routed
to it, with a reason code: ``another_org`` means a chat whose own row names a
different org reached the worker, which the isolation alarm counts.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from alkera_core.compute.box_logs import ORG_EVENTS, SUPERVISOR_EVENTS

from alkera_cli.supervisor.slots import Slot

#: Every event the supervisor may emit; the names and the fields each carries
#: are spelled in ``alkera_core.compute.box_logs``.
EVENTS: Final = SUPERVISOR_EVENTS

#: The reasons a worker gives for refusing a routed chat
#: (``alkera_cli/cloud/org_admission.py``), as the codes the event carries. A
#: reason it does not know is ``other``: the worker's own words never reach the
#: log.
REFUSAL_CODES: Final[Mapping[str, str]] = {
    "the chat's row names another org": "another_org",
    "the supervisor has not routed the chat here": "not_routed",
}

#: The logger every event goes to; each record carries its fields as
#: ``event_fields``.
LOGGER: Final = "alkera.supervisor.events"
_log = logging.getLogger(LOGGER)


def emit_for(slot: Slot, event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit an event about the worker or data of ``slot``: its slot and org
    are the slot's own, whatever else is in scope."""
    emit(event, level=level, **fields, slot=slot.index, org_id=slot.org_id)


def emit(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit one of :data:`EVENTS` with plain fields."""
    if event not in EVENTS:
        raise ValueError(f"not a supervisor event: {event}")
    if event in ORG_EVENTS and not fields.get("org_id"):
        raise ValueError(f"{event} is about one org and must name it")
    _log.log(logging.getLevelName(level.upper()), event, extra={"event_fields": fields})


class JsonLines(logging.Formatter):
    """An event record as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        stamp = datetime.fromtimestamp(record.created, UTC).isoformat()
        line = {"timestamp": stamp, "level": record.levelname.lower(), "event": record.msg}
        return json.dumps({**line, **fields}, default=str)


def refusal_code(reason: str) -> str:
    return REFUSAL_CODES.get(reason, "other")


def configure(level: str) -> None:
    """The supervisor's process logs: its events as JSON lines on standard
    error (the box's journal), beside the plain lines of :mod:`logging`."""
    number = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=number,
        stream=sys.stderr,
        format="%(asctime)s supervisor [%(levelname)s] %(name)s: %(message)s",
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLines())
    _log.addHandler(handler)
    _log.propagate = False


__all__ = [
    "EVENTS",
    "LOGGER",
    "REFUSAL_CODES",
    "JsonLines",
    "configure",
    "emit",
    "emit_for",
    "refusal_code",
]
