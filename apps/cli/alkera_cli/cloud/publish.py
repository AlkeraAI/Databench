"""Harness event → chat-doc entry: the pure half of the publisher.

The mirror publishes every harness event as ONE durable ``append`` entry — a
``ChatTranscriptEntry``, ``kind`` the event type and ``payload`` the event as
the harness emitted it — and every token delta as a ``chunk`` on the ephemeral
lane. The envelope model lives in ``alkera_core.schemas.objects`` because the
server persists and re-reads it; spelling it there rather than here is what
keeps the two sides of the boundary from drifting. Two rules the browser relies
on live here:

* an entry is bounded well under the event log's payload cap. A tool result
  is already a preview beside a blob handle (the delivery door never inlines
  more than 50 rows), but a text part or a wide preview can still be large, so
  an entry over budget is shrunk in place — preview rows dropped from the tail,
  text cut — and stamped ``truncated``; the full rows never ride the doc lane;
* consecutive token deltas of one part are coalesced into a single chunk so a
  fast model does not turn into hundreds of frames a second.

Everything here is data in, data out; the mirror owns the timing and the wire.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alkera_core.events import MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES
from alkera_core.schemas.chat import (
    NON_PERSISTED_EVENT_TYPES,
    AgentMessageChunk,
    AgentThoughtChunk,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PartUpdated,
    PermissionRequest,
    PermissionResolved,
    QuestionAnswered,
    QuestionRejected,
    QuestionRequest,
    ToolCall,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import ChatTranscriptEntry, TranscriptRole

from alkera_cli.harness.sandbox import memory_limit_exceeded

EntryRole = TranscriptRole

#: Headroom for the doc.op row around one entry: the envelope (doc id, peer,
#: epoch, seq, kind, op id, intent, the empty ``fields``/``meta`` bags, two
#: schema versions), the team id and the relay flag. Generous on purpose.
_ENVELOPE_HEADROOM = 2048
#: One append entry must fit the event log AND the websocket frame that carries
#: it there, with the envelope around it — whichever of the two is smaller. An
#: entry sized against the log alone is what put the box in a reconnect loop the
#: day the log's cap was raised past the frame cap.
ENTRY_MAX_BYTES = min(MAX_PAYLOAD_BYTES, MAX_FRAME_BYTES) - _ENVELOPE_HEADROOM
#: The text kept when a payload has to be cut to a preview.
PREVIEW_TEXT_CHARS = 2048
#: How many rows a shrunk tabular preview keeps at minimum before the text
#: fallback takes over.
MIN_PREVIEW_ROWS = 5


def entry_size(entry: dict[str, Any]) -> int:
    """The bytes the event log measures an entry at (``payload_size`` renders
    with default separators and ASCII escapes; so do we)."""
    return len(json.dumps(entry, ensure_ascii=True).encode("utf-8"))


class RoleIndex:
    """Remembers which message a part belongs to, so a part's entry carries the
    role of its message rather than a guess."""

    def __init__(self) -> None:
        self._message_roles: dict[str, str] = {}

    def observe(self, event: Any) -> None:
        if isinstance(event, MessageCreated):
            self._message_roles[event.message_id] = event.role

    def role_for(self, event: Any) -> EntryRole:
        if isinstance(event, MessageCreated):
            return _entry_role(event.role)
        if isinstance(event, MessageCompleted):
            return _entry_role(self._message_roles.get(event.message_id, "assistant"))
        if isinstance(event, (ToolCall, ToolCallUpdate)):
            return "tool"
        if isinstance(event, PartStarted):
            if event.part_type == "tool_call":
                return "tool"
            return _entry_role(self._message_roles.get(event.message_id, "assistant"))
        if isinstance(event, PartCreated):
            part = event.part
            if getattr(part, "type", None) == "tool_call":
                return "tool"
            message_id = getattr(part, "message_id", None)
            if isinstance(message_id, str):
                return _entry_role(self._message_roles.get(message_id, "assistant"))
            return "assistant"
        if isinstance(event, PartUpdated):
            return "assistant"
        if isinstance(
            event,
            (
                PermissionRequest,
                PermissionResolved,
                QuestionRequest,
                QuestionAnswered,
                QuestionRejected,
            ),
        ):
            return "assistant"
        return "system"


def _entry_role(role: str) -> EntryRole:
    if role in ("user", "assistant", "tool", "system"):
        return role  # type: ignore[return-value]
    return "assistant"


def is_chunk(event: Any) -> bool:
    """Whether the event rides the ephemeral lane (never the durable one)."""
    return getattr(event, "event_type", None) in NON_PERSISTED_EVENT_TYPES


#: What the reader is told when the agent process died under their turn, as
#: data rather than a sentence spelled at each producer. A cloud reader owns
#: neither the machine nor a terminal on it, so neither sentence names the
#: backend, an editor, a process or a return code — the return code stays in
#: the machine's log, where the operator reads it.
AGENT_EXIT_COPY: Mapping[str, str] = {
    # The mirror itself went down under the turn — a redeploy, or a supervisor
    # repointing it after a gateway hostname change. The answer is simply gone,
    # and asking again is the whole remedy.
    "restarted": "The workspace restarted while answering; ask the question again.",
    # Something the mirror did not do ended the agent. The reader's move is the
    # same, but we do not claim a restart we cannot account for.
    "stopped": "The workspace stopped answering; ask again.",
}

#: What the reader is told when the sandbox killed the chat's process for
#: memory: the limit, in the reader's units, and that the process was stopped
#: — never that the workspace stopped answering, which would send them to ask
#: the same question into the same limit.
MEMORY_LIMIT_COPY = "The process used more than the chat's {limit} memory limit and was stopped."

#: The adapter's synthetic crash detail. A negative code is a signal — which is
#: how a stop or a restart reaches the agent — so it reads as a restart even
#: when the mirror has not yet noticed it is going down.
_AGENT_EXIT = re.compile(r"agent exited unexpectedly\s*\(rc=(-?\d+)\)", re.IGNORECASE)

#: Payload fields a terminal event keeps its failure sentence in.
_DETAIL_KEYS = ("detail", "error_detail")


def memory_limit_words(memory_mb: int) -> str:
    """A limit in MiB as the reader reads it: whole gigabytes where it is
    one (``2 GB``), a half where it is that (``1.5 GB``), megabytes below one."""
    if memory_mb >= 1024:
        gb = memory_mb / 1024
        return f"{int(gb)} GB" if gb == int(gb) else f"{gb:.1f} GB"
    return f"{memory_mb} MB"


def agent_exit_notice(detail: str, *, stopping: bool = False) -> str | None:
    """The reader-facing sentence for a raw agent-exit ``detail``.

    ``None`` when ``detail`` is not one — every other failure keeps the words
    its producer chose. A kill for memory names the limit whatever else the
    detail says: the reader's next move is different (make the work fit, or
    a bigger plan), and "ask again" would only run into the limit again.
    """
    exceeded = memory_limit_exceeded(detail)
    if exceeded is not None:
        return MEMORY_LIMIT_COPY.format(limit=memory_limit_words(exceeded))
    match = _AGENT_EXIT.search(detail)
    if match is None:
        return None
    by_signal = int(match.group(1)) < 0
    return AGENT_EXIT_COPY["restarted" if stopping or by_signal else "stopped"]


def append_entry(
    event: Any, roles: RoleIndex, *, limit: int = ENTRY_MAX_BYTES, stopping: bool = False
) -> dict[str, Any]:
    """The durable entry for one persisted harness event, bounded to ``limit``.

    The envelope is ``ChatTranscriptEntry`` — the shape the cloud persists and
    reads back — so the machine and the server spell it once, and the row
    carries the version stamp that lets it ever be migrated.
    """
    entry = ChatTranscriptEntry(
        event_id=str(event.event_id),
        role=roles.role_for(event),
        kind=str(getattr(event, "event_type", "") or "unknown"),
        payload=_reader_facing(event.model_dump(mode="json"), stopping=stopping),
    ).model_dump(mode="json")
    return bound_entry(entry, limit=limit)


def _reader_facing(payload: dict[str, Any], *, stopping: bool) -> dict[str, Any]:
    """``payload`` with a raw agent-exit detail replaced by its sentence."""
    for key in _DETAIL_KEYS:
        detail = payload.get(key)
        if not isinstance(detail, str):
            continue
        notice = agent_exit_notice(detail, stopping=stopping)
        if notice is not None:
            payload[key] = notice
    return payload


def bound_entry(entry: dict[str, Any], *, limit: int = ENTRY_MAX_BYTES) -> dict[str, Any]:
    """``entry`` if it fits ``limit``; otherwise a shrunk copy stamped
    ``payload.truncated = True`` and ``payload.truncated_bytes`` — how many
    bytes the entry lost, so a reader is told the SIZE of what is missing and
    not merely that something is. Shrinking tries, in order: dropping tabular
    preview rows from the tail (a tool result), cutting long text fields (a
    text part, tool output text), and finally replacing the payload with a
    short text preview of itself. A blob handle in the payload survives every
    step so the browser can still fetch the full result."""
    before = entry_size(entry)
    if before <= limit:
        return entry
    shrunk: dict[str, Any] = json.loads(json.dumps(entry))
    payload = shrunk["payload"]
    if not isinstance(payload, dict):
        payload = {"value": payload}
        shrunk["payload"] = payload
    payload["truncated"] = True
    # Budget the loss field at its widest BEFORE shrinking: the loss is always
    # smaller than the size the entry came in at, so a field sized against
    # `before` can only get narrower when the real figure replaces it. Stamping
    # it afterwards would push a just-fitting entry back over the limit.
    payload["truncated_bytes"] = before
    if _shrink_preview_rows(shrunk, limit) or _shrink_text(shrunk, limit):
        return _stamp_loss(shrunk, before)
    original = entry["payload"]
    text = json.dumps(original, ensure_ascii=True)
    shrunk["payload"] = {
        "event_type": original.get("event_type") if isinstance(original, dict) else None,
        "event_id": entry["event_id"],
        "truncated": True,
        "truncated_bytes": before,
        "preview": text[:PREVIEW_TEXT_CHARS],
        "blob": _find_blob(original),
    }
    return _stamp_loss(shrunk, before)


def _stamp_loss(entry: dict[str, Any], before: int) -> dict[str, Any]:
    """Replace the budgeted placeholder with the bytes actually dropped."""
    entry["payload"]["truncated_bytes"] = max(0, before - entry_size(entry))
    return entry


def _find_blob(value: Any) -> dict[str, Any] | None:
    """The first ``{sha256, size, media_type}`` handle inside ``value``."""
    if isinstance(value, dict):
        blob = value.get("blob")
        if isinstance(blob, dict) and isinstance(blob.get("sha256"), str):
            return blob
        for child in value.values():
            found = _find_blob(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_blob(child)
            if found is not None:
                return found
    return None


def _preview_holders(value: Any) -> list[dict[str, Any]]:
    """Every dict under ``value`` carrying a ``preview_rows`` (or ``rows``) list."""
    holders: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("preview_rows"), list) or isinstance(value.get("rows"), list):
            holders.append(value)
        for child in value.values():
            holders.extend(_preview_holders(child))
    elif isinstance(value, list):
        for child in value:
            holders.extend(_preview_holders(child))
    return holders


def _shrink_preview_rows(entry: dict[str, Any], limit: int) -> bool:
    holders = _preview_holders(entry["payload"])
    if not holders:
        return False
    while entry_size(entry) > limit:
        progressed = False
        for holder in holders:
            key = "preview_rows" if isinstance(holder.get("preview_rows"), list) else "rows"
            rows = holder[key]
            if len(rows) > MIN_PREVIEW_ROWS:
                rows.pop()
                holder["truncated"] = True
                progressed = True
        if not progressed:
            return False
    return True


def _text_fields(value: Any) -> list[tuple[dict[str, Any], str]]:
    fields: list[tuple[dict[str, Any], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and len(child) > PREVIEW_TEXT_CHARS:
                fields.append((value, key))
            else:
                fields.extend(_text_fields(child))
    elif isinstance(value, list):
        for child in value:
            fields.extend(_text_fields(child))
    return fields


def _shrink_text(entry: dict[str, Any], limit: int) -> bool:
    fields = _text_fields(entry["payload"])
    if not fields:
        return False
    for holder, name in sorted(fields, key=lambda pair: -len(pair[0][pair[1]])):
        holder[name] = holder[name][:PREVIEW_TEXT_CHARS]
        holder["truncated"] = True
        if entry_size(entry) <= limit:
            return True
    return entry_size(entry) <= limit


@dataclass
class _Run:
    event: dict[str, Any]
    text: list[str] = field(default_factory=list)


class ChunkCoalescer:
    """Merges consecutive token deltas of one part into a single chunk event.

    ``add`` folds a delta into the run for its ``(event_type, message_id,
    part_id)``; ``flush`` hands back one event per run, the text concatenated
    and the last delta's ``sequence`` / ``is_final`` kept, in first-seen order.
    The mirror flushes on a short timer, so a burst becomes a few frames."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str, str], _Run] = {}

    def __len__(self) -> int:
        return len(self._runs)

    def add(self, event: AgentMessageChunk | AgentThoughtChunk) -> None:
        key = (event.event_type, event.message_id, event.part_id)
        dumped = event.model_dump(mode="json")
        run = self._runs.get(key)
        if run is None:
            self._runs[key] = _Run(event=dumped, text=[event.text])
            return
        run.text.append(event.text)
        # The latest delta's identity + flags; the text is the concatenation.
        run.event = dumped

    def flush(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for run in self._runs.values():
            merged = dict(run.event)
            merged["text"] = "".join(run.text)
            out.append(merged)
        self._runs.clear()
        return out


__all__ = [
    "ENTRY_MAX_BYTES",
    "MIN_PREVIEW_ROWS",
    "PREVIEW_TEXT_CHARS",
    "ChunkCoalescer",
    "EntryRole",
    "RoleIndex",
    "append_entry",
    "bound_entry",
    "entry_size",
    "is_chunk",
]
