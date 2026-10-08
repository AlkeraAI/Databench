"""E2E proof: a tool that really returns fifty megabytes still fits one frame.

The bound on an oversized tool result is asserted at the unit level against a
synthesized event. What only a real subprocess can prove is that the fifty
megabytes ever get that far in that shape: a real opencode runs a real alkera
``bash`` tool, the command really writes 50 MB to stdout, and the chain that
carries it — the delivery door's blob spill, the translator's
``ToolCallUpdate``, the publisher's entry bound — is the one the product ships
rather than one a test built.

Three claims, in the order the bytes meet them:

* the whole output is KEPT. It lands in the chat's content-addressed blob
  store, so the browser has a way to the fifty megabytes the model never saw;
* what the model is handed is a preview, not the output;
* the row the transcript publishes fits ``MAX_PAYLOAD_BYTES`` and one
  ``MAX_FRAME_BYTES`` websocket frame, and is still the same tool call — not
  dropped, not split, and carrying a pointer that reaches the rest.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.cloud.publish import ENTRY_MAX_BYTES, RoleIndex, append_entry, entry_size
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.events import MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES
from alkera_core.schemas.chat import PermissionRequest, ToolCallUpdate

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="parent-hosted alkera loopback is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: 655,360 lines of 79 characters plus a newline: fifty megabytes of the shape
#: a log tail or a `find /` actually produces, rather than one enormous line.
_LINE = "alkera" * 13 + "x"
_LINES = 655_360
_EXTREME_BYTES = (len(_LINE) + 1) * _LINES

#: Written by the shell itself, so the fifty megabytes are produced by the
#: command and not by anything Python handed it.
_COMMAND = f"yes '{_LINE}' | head -n {_LINES}"


async def _allow(_request: PermissionRequest) -> str:
    return "allow_once"


#: The directory a shortened result writes the rest of itself to. The SESSION
#: owns it (``agent_tree.tool_output``), so it sits under whichever working directory
#: that session was given rather than at one fixed place in the project — found
#: by name here so moving it again can't quietly turn this claim off.
_SPILL_DIR = "tool-output"


def _spilled_bytes(project_root: Path) -> list[int]:
    """Every file a shortened tool result spilled, largest first."""
    return sorted(
        (
            path.stat().st_size
            for spill in project_root.joinpath(".alkera").rglob(_SPILL_DIR)
            if spill.is_dir()
            for path in spill.rglob("*")
            if path.is_file()
        ),
        reverse=True,
    )


def _chat_bytes(project_root: Path) -> list[int]:
    """Every file the chat itself holds — transcript, manifest, blob store —
    largest first, with the spill excluded. The spill is precisely the copy kept
    OUT of the chat, so counting it here would measure the wrong thing."""
    chats = project_root.joinpath(".alkera", "chats")
    if not chats.is_dir():
        return []
    return sorted(
        (
            path.stat().st_size
            for path in chats.rglob("*")
            if path.is_file() and _SPILL_DIR not in path.parts
        ),
        reverse=True,
    )


#: Where a truncated result says the rest of it went. Two truncators can bound
#: this output — the alkera bash tool's own tail-and-spill and, if anything
#: still arrives too wide, opencode's — so the row names whichever one bit,
#: through ``metadata.outputPath`` or in the sentence the model is shown. The
#: claim is about the POINTER, not about which layer wrote it. The path ends at
#: whitespace OR at an escaped newline, because a tool result reaches the row
#: JSON-encoded and the rest of the output then follows the path as ``\n…``
#: rather than as a break the pattern would stop at.
_SAVED_TO = re.compile(r"saved to: ((?:(?!\\n)\S)+)")


def _is_file(path: Path) -> bool:
    """Whether ``path`` is a real file. Total on purpose: a candidate scraped out
    of a result's prose can be longer than any name the OS will even look up, and
    that is an answer of "no", not a crash."""
    try:
        return path.is_file()
    except OSError:
        return False


def _pointers(payload: dict[str, Any]) -> list[Path]:
    """Every on-disk path the published row offers as the way to the rest."""
    found = []
    declared = payload.get("metadata", {}).get("outputPath")
    if isinstance(declared, str):
        found.append(Path(declared))
    output = payload.get("output")
    if isinstance(output, str):
        found.extend(Path(match) for match in _SAVED_TO.findall(output))
    return found


def _find_blob(value: Any) -> dict[str, Any] | None:
    """The first content-addressed handle anywhere in a payload."""
    if isinstance(value, dict):
        if isinstance(value.get("sha256"), str):
            return value
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


async def _run_extreme_tool(tmp_path: Path) -> ToolCallUpdate:
    """Drive a real opencode until the alkera bash tool has written 50 MB.

    Returns the finished ``ToolCallUpdate`` — the event every surface
    downstream is built on.
    """
    assert _EXTREME_BYTES > 50 * 1000 * 1000, _EXTREME_BYTES

    script = {
        "*": tool_call_chunks(
            "alkera_call_tool",
            {"name": "bash", "args": {"command": _COMMAND, "description": "dump the log"}},
        ),
        FOLLOWUP_KEY: text_chunks("that was a lot of output"),
    }

    updates: list[ToolCallUpdate] = []

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_allow, default_timeout_seconds=60.0)
        )
        sub = session.subscribe()

        async def _pump() -> None:
            async for event in sub:
                if isinstance(event, ToolCallUpdate):
                    updates.append(event)

        pump = asyncio.create_task(_pump())
        try:
            await session.send_prompt("dump the log", model=_MODEL)
            for _ in range(3000):
                if any(u.status in {"completed", "error"} for u in updates):
                    break
                await asyncio.sleep(0.02)
        finally:
            pump.cancel()
            await runtime.close_chat(sid)

    finished = [u for u in updates if u.status == "completed"]
    assert finished, (
        f"the tool never completed; saw {[u.status for u in updates]!r} "
        f"{[str(u.error_text)[:400] for u in updates if u.error_text]!r}"
    )
    return finished[-1]


async def test_a_fifty_megabyte_tool_result_is_kept_whole_and_published_bounded(
    tmp_path: Path,
) -> None:
    update = await _run_extreme_tool(tmp_path)

    # 1. The whole output is kept. The bash tool spills the COMPLETE stream to a
    #    file in the session's own working directory once it outgrows the
    #    model-visible window, so nothing the command wrote is lost even though
    #    nothing downstream carries it. Nothing else this chat spills is remotely
    #    that size, so the largest one IS this command's output.
    spilled = _spilled_bytes(tmp_path)
    assert spilled, "the fifty-megabyte command spilled nothing to disk"
    assert spilled[0] >= _EXTREME_BYTES * 0.9, (
        f"the largest spill is {spilled[0]} bytes; the command wrote {_EXTREME_BYTES}"
    )

    # 2. What the chat itself keeps — the transcript and the content-addressed
    #    store a browser fetches from — is orders of magnitude smaller than the
    #    output, because the door's inline cap is what keeps fifty megabytes out
    #    of the context window and off the wire.
    stored = _chat_bytes(tmp_path)
    assert stored, "the chat persisted nothing at all"
    assert stored[0] < _EXTREME_BYTES // 10, stored[0]

    # 3. What the model was handed is a tail plus a pointer, not the output.
    delivered = len(json.dumps(update.model_dump(mode="json"), ensure_ascii=False).encode())
    assert delivered < _EXTREME_BYTES // 100, delivered

    # 4. The published row fits the event log and one websocket frame, is still
    #    the same tool call, and keeps the handle to the rest.
    entry = append_entry(update, RoleIndex())
    size = entry_size(entry)
    assert size <= ENTRY_MAX_BYTES
    assert size <= MAX_PAYLOAD_BYTES
    assert size <= MAX_FRAME_BYTES
    assert entry["kind"] == "tool.call_update"
    assert entry["payload"]["tool_call_id"] == update.tool_call_id
    assert entry["payload"]["status"] == "completed"
    # A row that was cut says how much it lost, so a reader can tell a missing
    # line from fifty megabytes. A row that fit whole says nothing — asserting
    # the figure unconditionally would pin whichever the door happens to do.
    if entry["payload"].get("truncated"):
        assert entry["payload"]["truncated_bytes"] > 0

    # 5. The way back to the whole output rides in the row. A bash result bounds
    #    ITSELF — a tail plus the path it spilled to — so it reaches the
    #    delivery door already small and never earns a blob handle; a pointer is
    #    all a reader has.
    pointers = _pointers(entry["payload"])
    assert pointers, json.dumps(entry["payload"], ensure_ascii=False)[:2000]
    reachable = [path for path in pointers if _is_file(path)]
    assert reachable, f"none of {[str(path)[:200] for path in pointers]!r} exists"

    # 6. And that pointer REACHES the fifty megabytes. Whichever layer bounded
    #    this result, the row names the ONE file that holds every byte: a layer
    #    that shortens an already-shortened result into a file of its own would
    #    leave this naming ~50 KB of preview while the output sat unnamed.
    assert max(path.stat().st_size for path in reachable) >= _EXTREME_BYTES * 0.9, [
        (str(path), path.stat().st_size) for path in reachable
    ]
