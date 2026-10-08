"""The finished tool result a promote names, and the rows it uploads.

A reader saves a query's result from the chat; the box finds that result in
the chat's transcript by the tool call id the browser read off its card, and
reads the rows to upload off the result's stored blob (or the preview the card
showed, for a result that stored none). Pure over the transcript's events and
the project's blob store, so it runs without a mirror around it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from alkera_core.schemas.chat import Event, PartCreated, ToolCall, ToolCallPart, ToolCallUpdate

from alkera_cli.plugins.plugin_base.result_blob import ResultBlobEnvelope, read_envelope

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore


def find_tool_result(
    events: Iterable[Event], event_id: str
) -> tuple[ToolCallPart, datetime, datetime | None] | None:
    """The finished tool result a promote names.

    ONE identity names a tool result across the seam: the TOOL CALL ID. It
    is the only id both sides hold — the browser reads it off the folded
    card (`part.callId`), and every event the harness emits for the call
    carries it. A transcript event id names an EVENT, and one call produces
    several (`tool.call`, its updates, the finalized part), so it can never
    be the name of "the result".

    An agent-run query reaches the transcript as `tool.call` +
    `tool.call_update`, with no finalized part at all, so a result is
    assembled from those when there is no part to return. The transcript
    event id is still accepted as a fallback: a result promoted by an older
    client named the event, and an object already created under that name
    must not be stranded.
    """
    call_times: dict[str, datetime] = {}
    calls: dict[str, ToolCall] = {}
    by_call: tuple[ToolCallPart, datetime, datetime | None] | None = None
    by_event: tuple[ToolCallPart, datetime, datetime | None] | None = None
    for event in events:
        if isinstance(event, ToolCall):
            call_times.setdefault(event.tool_call_id, event.time)
            calls.setdefault(event.tool_call_id, event)
        if isinstance(event, ToolCallUpdate) and event.tool_call_id == event_id:
            part = _part_from_update(event, calls.get(event_id))
            # Last write wins: a call may stream several updates and only
            # the terminal one carries the output.
            if part is not None:
                by_call = (part, event.time, call_times.get(event_id))
        if isinstance(event, PartCreated) and isinstance(event.part, ToolCallPart):
            if event.part.call_id == event_id:
                by_call = (_unwrap_call_tool(event.part), event.time, call_times.get(event_id))
            elif event.event_id == event_id:
                by_event = (
                    _unwrap_call_tool(event.part),
                    event.time,
                    call_times.get(event.part.call_id),
                )
    return by_call or by_event


def result_envelope(blobs: Callable[[], BlobStore], output: dict[str, Any]) -> ResultBlobEnvelope:
    """The rows a tool result's ``output`` carries: its stored blob when it
    names one (``blobs`` opens the store only then), else its preview rows.
    Raises ``ValueError`` (or the store's ``FileNotFoundError``) when it
    carries none."""
    blob = output.get("blob")
    if isinstance(blob, dict) and isinstance(blob.get("sha256"), str):
        envelope = read_envelope(blobs(), blob["sha256"])
        if envelope is None:
            raise ValueError("the stored blob is not a result envelope")
        return envelope
    columns = output.get("columns")
    rows = output.get("preview_rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("the tool result carries no rows")
    return ResultBlobEnvelope(
        kind="rows",
        columns=[str(c) for c in columns],
        rows=[list(r) for r in rows if isinstance(r, list)],
        total=len(rows),
    )


_CALL_TOOL_WRAPPER = re.compile(r"^(?:mcp__[^_]+__|alkera[_-])?call_tool$", re.IGNORECASE)


def _unwrap_call_tool(part: ToolCallPart) -> ToolCallPart:
    """The inner tool's call, when this part is a ``call_tool`` wrapper.

    The loopback MCP server serializes an alkera tool's result into ONE text
    block, so the wrapper's output reaches the transcript as a JSON string with
    the real result nested under ``result``, and the real arguments under
    ``input.args``. The reader in the browser does exactly this before its card
    reads the rows (``unwrapToolResult`` in ``@alkera/chat-model``); a promote
    has to see the same rows the reader was looking at when they pressed save,
    so the two unwrap the same way.
    """
    if not _CALL_TOOL_WRAPPER.match(part.name.strip()):
        return part
    inner_name = part.input.get("name")
    if not isinstance(inner_name, str) or not inner_name:
        return part
    args = part.input.get("args")
    output: Any = part.output
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = part.output
    if isinstance(output, dict) and len(output) == 1 and "result" in output:
        output = output["result"]
    return part.model_copy(
        update={
            "name": inner_name,
            "input": args if isinstance(args, dict) else {},
            "output": output if isinstance(output, (dict, str)) else None,
        }
    )


def _part_from_update(update: ToolCallUpdate, call: ToolCall | None) -> ToolCallPart | None:
    """The finished tool result carried by a ``tool.call_update``, as the part a
    promote uploads.

    An agent-run tool never produces a finalized ``PartCreated`` — the harness
    announces the call and then updates it — so the result has to be assembled
    from the pair. Only a TERMINAL update is a result: a running one has no
    output to upload.
    """
    if update.status not in ("completed", "error"):
        return None
    # The wrapper's result arrives as a JSON STRING; keep it verbatim so the
    # unwrap below can parse it exactly as the reader's card did.
    output = update.output if isinstance(update.output, (dict, str)) else None
    tool_input = update.input if isinstance(update.input, dict) else None
    if tool_input is None and call is not None:
        tool_input = call.input
    return _unwrap_call_tool(
        ToolCallPart(
            part_id=f"{update.tool_call_id}-part",
            message_id=call.message_id if call is not None else update.tool_call_id,
            call_id=update.tool_call_id,
            name=call.tool_name if call is not None else "",
            input=tool_input or {},
            state="error" if update.status == "error" else "completed",
            output=output,
            error_text=update.error_text,
        )
    )
