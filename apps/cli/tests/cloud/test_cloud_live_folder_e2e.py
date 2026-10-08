"""What a real agent writes reaches the drive while its turn is still running.

The live push is the difference between "the chat saved at the end" and "the
reader watched the file appear". Every other proof of it drives the sync with a
scripted stream of changes — a test hands it the events an operating system
would have produced. Nothing there proves the one thing the feature rests on:
that the changes a REAL agent subprocess makes, at the moment and in the shape
it makes them, are what the watcher actually sees.

So this drives the whole chain with nothing scripted between the model and the
disk: a real bun-driven opencode against the scripted mock provider calls its
own ``write`` tool, the file lands in the chat's working directory the way any
turn's file does, and a real ``watchfiles`` watch over that directory is what
notices. The drive on the other side is a double — the Files routes are proved
against the real backend elsewhere, and the claim here is about the watcher and
what it hands over, not about HTTP.

Two things make the timing a fact rather than a race. The scripted turn holds
itself open with a shell command beside the write, so "before the turn ended"
is a window of seconds rather than a photo finish; and the moment the turn
reached its terminal status is recorded from the event stream and compared with
the moment the drive was handed the bytes. A live push that only landed at the
checkpoint would miss that window by the whole hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import (
    FOLLOWUP_KEY,
    parallel_tool_call_chunks,
    text_chunks,
)
from alkera_cli.files.live_sync import (
    InboundEntry,
    LiveBatchAnswer,
    LiveCadence,
    LiveEntry,
    LiveSync,
    TreeAnswer,
    TreeEntry,
    TreeWatcher,
)
from alkera_cli.files.mount import MountRecord, SelfFence
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import Event, PermissionRequest, SessionStatusChanged

pytestmark = [
    pytest.mark.opencode_e2e,
    # The hold that keeps the turn open past the write is a POSIX shell command,
    # and the tool that runs it is the parent-hosted shell that only exists there.
    pytest.mark.skipif(os.name != "posix", reason="the scripted hold runs a POSIX shell"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: What the turn writes. A bare name, so it lands at the top of the chat's
#: working directory — where a turn's deliverable goes.
PAGE = "q3-report.html"
PAGE_TEXT = "<!doctype html><title>Q3</title><h1>Q3 revenue</h1><p>Up 12%.</p>"

#: How long the turn keeps itself open after the write. Long enough that a
#: live push landing "during the turn" is a claim about the push rather than
#: about how fast this machine happened to be.
HOLD_SECONDS = 3

#: The cadence the test leases under. Smaller than the default because the
#: window being measured is one turn rather than one work session; the numbers
#: are the server's to choose, which is why they are a field and not a constant.
CADENCE = LiveCadence(debounce_ms=50, batch_every_ms=50, settle_ms=100)

#: The chat's own records. They sit beside the working directory, and none of
#: them is the chat's work, so none of them is the drive's to hold.
BOX_RECORDS = ("manifest.json", "chat.jsonl", "trace.digest.json", "cost_ledger.jsonl")

#: The two a turn always rewrites: the transcript it appends to and the manifest
#: pinning the agent session it ran under. Their churn is what makes their
#: absence from the drive mean something.
STIRRED_RECORDS = ("manifest.json", "chat.jsonl")


class _SystemClock:
    """The real monotonic clock.

    Every other suite drives the sync with a clock it steps by hand. Here the
    turn is a real subprocess taking real time, so the cadence has to elapse the
    same way it does on a box.
    """

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class _RecordingDrive:
    """The drive side of the live plane: what it was handed, and when.

    It reads the bytes off disk when it is asked to upload, exactly as the real
    uploader does, so a stale or half-written upload is visible as wrong bytes
    rather than hidden behind a recorded argument.
    """

    root: Path

    stored: dict[str, bytes] = field(default_factory=dict)
    uploaded_at: dict[str, float] = field(default_factory=dict)
    nodes: dict[str, str] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    trashed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    batches: int = 0
    _next: int = 0

    def _node(self, path: str) -> str:
        if path not in self.nodes:
            self._next += 1
            self.nodes[path] = f"node-{self._next}"
        return self.nodes[path]

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        for entry in entries:
            if entry.op == "delete":
                if entry.path in self.nodes:
                    self.trashed.append(self.nodes.pop(entry.path))
                continue
            if entry.op == "rename" and entry.from_ in self.nodes:
                assert entry.from_ is not None
                node_id = self.nodes.pop(entry.from_)
                self.nodes[entry.path] = node_id
                self.renamed.append((node_id, entry.from_, entry.path))
                continue
            self._node(entry.path)
        return TreeAnswer(live_seq=1, applied=len(entries))

    def resolve(self, paths: Sequence[str]) -> dict[str, str]:
        return {path: self.nodes[path] for path in paths if path in self.nodes}

    def upload(self, rel_path: str, node_id: str, size: int, **_: Any) -> None:
        self.stored[rel_path] = (self.root / rel_path).read_bytes()
        self.uploaded_at[rel_path] = time.monotonic()

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        self.batches += 1
        for entry in entries:
            self.states[entry.node_id] = entry.state
        return LiveBatchAnswer(live_seq=self.batches, pending=len(entries))

    def inbound(self) -> list[InboundEntry]:
        return []

    def item(self, node_id: str) -> Mapping[str, Any]:
        return {"id": node_id}

    def download(self, node_id: str, into: Path, **_: Any) -> None:
        raise AssertionError("this turn takes nothing from the drive")

    # -- what a test asks it -------------------------------------------------

    def heard_of(self) -> set[str]:
        """Every path the drive was ever asked to make a node for."""
        return {path for path in self.nodes if path}

    def state_of(self, relative: str) -> str:
        """The last state the drive was told for ``relative``."""
        return self.states.get(self.nodes.get(relative, ""), "")


def _write_then_hold() -> dict[str, Any]:
    """One assistant turn: write the page, and keep the turn open beside it.

    Both calls ride a single assistant message, so the write is issued first and
    the hold runs alongside it — the turn cannot finish until the shell command
    does, which is what turns "landed during the turn" into something that can
    be asserted instead of raced.
    """
    return {
        "*": parallel_tool_call_chunks(
            [
                ("write", {"filePath": PAGE, "content": PAGE_TEXT}),
                (
                    "bash",
                    {
                        "command": f"sleep {HOLD_SECONDS}",
                        "description": "keep working while the report is reviewed",
                    },
                ),
            ]
        ),
        FOLLOWUP_KEY: text_chunks(f"Written up: [Q3 report]({PAGE})"),
    }


def _allowing_broker(prompts: list[PermissionRequest]) -> PermissionBroker:
    """Records every ask and allows it, so a wrongly prompted call still lets
    the turn finish and a test fails on the record rather than on a timeout."""

    async def _resolve(request: PermissionRequest) -> str:
        prompts.append(request)
        return "allow_once"

    return PermissionBroker(_resolve, default_timeout_seconds=30.0)


def _live_sync(working_dir: Path, drive: _RecordingDrive, stop: threading.Event) -> LiveSync:
    """The holder's live push over the chat's working directory.

    The watcher is the real one: the point of this test is that an OS watch over
    the directory an agent actually writes in sees what that agent does.
    """
    return LiveSync(
        root=working_dir,
        root_path="home/dana/Chats/e2e/scratch",
        record=MountRecord(
            node_id="chat-node",
            org_path="home/dana/Chats/e2e",
            heartbeat_every=15.0,
        ),
        cadence=CADENCE,
        api=drive,
        watcher=TreeWatcher(working_dir, cadence=CADENCE, stop=stop),
        fence=SelfFence(grace=30.0),
        clock=_SystemClock(),
    )


async def _drain(sub: Any, *, budget_seconds: float = 180.0) -> tuple[list[Event], float]:
    """One whole turn, and the instant its terminal status arrived."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out, time.monotonic()
        try:
            async with asyncio.timeout(remaining):
                event: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out, time.monotonic()
        out.append(event)
        if isinstance(event, SessionStatusChanged):
            if event.status == "running":
                seen_running = True
            elif event.status in ("idle", "error") and seen_running:
                return out, time.monotonic()


def _statuses(events: list[Event]) -> list[str]:
    return [event.status for event in events if isinstance(event, SessionStatusChanged)]


async def test_the_file_a_real_turn_writes_is_on_the_drive_before_the_turn_ends(
    tmp_path: Path,
) -> None:
    """The live push, from a real agent's ``write`` to the drive's bytes.

    Nothing between the model and the disk is scripted: opencode's own write
    tool puts the file in the chat's working directory, and a real watch over
    that directory is what notices. What is asserted is the whole promise — the
    drive holds the agent's bytes, it was told about the node before they moved,
    and the handover happened while the turn was still running.
    """
    project = ProjectDirectory(tmp_path / ".alkera")
    async with opencode_e2e_runtime(tmp_path, mock_script=_write_then_hold()) as (
        runtime,
        sid,
        _server,
    ):
        # A cloud chat runs in a child of its own folder and calls that child
        # both its working directory and its sandbox. The watch goes on that
        # child, which is what leaves the box's records outside it.
        working_dir = project.chats_path / sid / "scratch"
        working_dir.mkdir(parents=True, exist_ok=True)
        drive = _RecordingDrive(root=working_dir)
        stop = threading.Event()
        pump = asyncio.ensure_future(_live_sync(working_dir, drive, stop).run())
        prompts: list[PermissionRequest] = []
        try:
            session = await runtime.open_chat(
                sid,
                permission_broker=_allowing_broker(prompts),
                working_dir=working_dir,
                sandbox_dir=working_dir,
            )
            sub = session.subscribe()
            await session.send_prompt("write up Q3 as a one-page report", model=_MODEL)
            events, turn_ended_at = await _drain(sub)
            await runtime.close_chat(sid)
        finally:
            stop.set()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(pump, timeout=15.0)

    statuses = _statuses(events)
    assert "idle" in statuses, f"the turn never completed (statuses={statuses})"
    assert "error" not in statuses, f"the turn errored (statuses={statuses})"

    page = working_dir / PAGE
    assert page.is_file(), (
        f"the agent never wrote the page (the working directory holds "
        f"{sorted(entry.name for entry in working_dir.iterdir())})"
    )
    written = page.read_bytes()

    assert PAGE in drive.stored, (
        f"the live sync never handed the agent's file to the drive (it sent {sorted(drive.stored)})"
    )
    assert drive.stored[PAGE] == written, (
        "the drive holds bytes that are not the ones the agent left on disk"
    )

    # The reader is told a file is on its way before its bytes move, so a row
    # can appear while the agent is still writing. A node with no state would
    # be a row the web has nothing to say about.
    assert drive.state_of(PAGE) == "uploading", (
        f"the drive was never told what {PAGE} was doing (states={drive.states})"
    )

    # The whole point: this landed mid-turn. The turn held itself open for
    # HOLD_SECONDS after the write, so a push that only ran at the checkpoint
    # misses this by that whole window.
    landed_at = drive.uploaded_at[PAGE]
    assert landed_at < turn_ended_at, (
        f"the file reached the drive {landed_at - turn_ended_at:.2f}s AFTER the turn "
        f"finished — the reader only saw it once the chat was done"
    )


async def test_the_boxs_own_records_are_outside_the_watch_a_real_turn_stirs(
    tmp_path: Path,
) -> None:
    """A real turn churns the chat's records — and none of them is the chat's work.

    The manifest, the transcript and the digest are rewritten continuously while
    a turn runs; they are facts about the box holding the chat, not files a
    member should find in the drive. Watching the working directory rather than
    the chat folder is what keeps them out, and only a real turn stirs them hard
    enough for their absence to mean anything.
    """
    project = ProjectDirectory(tmp_path / ".alkera")
    async with opencode_e2e_runtime(tmp_path, mock_script=_write_then_hold()) as (
        runtime,
        sid,
        _server,
    ):
        chat_dir = project.chats_path / sid
        working_dir = chat_dir / "scratch"
        working_dir.mkdir(parents=True, exist_ok=True)
        drive = _RecordingDrive(root=working_dir)
        stop = threading.Event()
        pump = asyncio.ensure_future(_live_sync(working_dir, drive, stop).run())
        try:
            session = await runtime.open_chat(
                sid,
                permission_broker=_allowing_broker([]),
                working_dir=working_dir,
                sandbox_dir=working_dir,
            )
            sub = session.subscribe()
            await session.send_prompt("write up Q3 as a one-page report", model=_MODEL)
            events, _ended_at = await _drain(sub)
            await runtime.close_chat(sid)
        finally:
            stop.set()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(pump, timeout=15.0)

    assert "idle" in _statuses(events), f"the turn never completed ({_statuses(events)})"

    # The records have to have been written, or their absence from the drive
    # proves nothing at all.
    stirred = [name for name in STIRRED_RECORDS if (chat_dir / name).is_file()]
    assert stirred == list(STIRRED_RECORDS), (
        f"this turn left the box's records untouched, so nothing was kept out: {stirred}"
    )
    assert (chat_dir / "chat.jsonl").stat().st_size > 0, "the transcript this turn wrote is empty"

    heard = drive.heard_of()
    assert PAGE in heard, f"the drive heard of nothing the agent wrote ({sorted(heard)})"
    for record in BOX_RECORDS:
        assert record not in heard, f"the box's {record} was published to the drive"
    assert not [path for path in heard if path.startswith(".runtime")], (
        f"the harness's per-chat storage reached the drive ({sorted(heard)})"
    )
    for path in heard:
        assert (working_dir / path).exists(), (
            f"the drive was told about {path!r}, which is nowhere in the working directory"
        )
