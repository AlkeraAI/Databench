"""A reply that shows a chart is published only once the chart is on the drive.

The agent writes ``charts/active.png`` and replies ``![Active prompts](charts/
active.png)``. The web transcript and the Slack relay both read the reply off
the chat document and then fetch the chart from the drive, so the reply must
not reach the document before the chart's bytes reach the drive. Left to the
live sync alone it did: a file that fresh waits out the settle time, the reply
went first, and every reader met a placeholder (the web) or no picture (Slack)
until something asked the box for the bytes.

The rig is the real thing on both sides of the ordering: the mirror's publish
pump, flusher and publisher over a real runtime with the fake adapter, and a
real :class:`LiveSync` on its own thread over a small fake drive. The chat
document records, with every op it is sent, what the drive held at that moment
-- so "reply visible implies bytes readable" is asserted as exactly that.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.folder import ChatFolders, HeldFolder
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.transport import FALLBACK_SIZES
from alkera_cli.files.live_sync import LiveCadence, LiveSync
from alkera_cli.files.tree_watch import Change
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import AgentMessageChunk, PartCreated, TextPart
from files._live_sync_fakes import FakeClock, FakeLiveApi
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

pytestmark = pytest.mark.asyncio

CHAT_ID = "5c272302-65bd-48c5-b8c4-e121e7b6c3e3"
OWNER = "cf81f58c-9884-4e28-b8f9-469dfed97673"
CHART = b"\x89PNG\r\n\x1a\n" + b"active prompts" * 6000
REPLY = "Here is the breakdown:\n\n![Active prompts by category and locale](charts/active.png)\n"
#: A bound on a hang, never a wait an assertion depends on.
HANG = 20.0


@dataclass
class _Doc:
    """The chat document, recording each op with what the drive held when it
    was sent."""

    drive: FakeLiveApi
    live: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    can_write: bool = True
    state: dict[str, Any] = field(default_factory=dict)
    appends: list[tuple[dict[str, Any], dict[str, bytes]]] = field(default_factory=list)
    chunks: list[tuple[dict[str, Any], dict[str, bytes]]] = field(default_factory=list)

    def on_op(self, _handler: Any) -> None: ...

    def on_snapshot(self, _handler: Any) -> None: ...

    def close(self) -> None: ...

    async def send_op(
        self,
        intent: str,
        *,
        events: Sequence[dict[str, Any]] = (),
        meta: dict[str, Any] | None = None,
        op_id: str | None = None,
    ) -> None:
        del meta, op_id
        if intent == "append":
            for entry in events:
                self.appends.append((entry, dict(self.drive.stored)))

    async def send_chunk(self, events: Sequence[dict[str, Any]]) -> bool:
        for event in events:
            self.chunks.append((event, dict(self.drive.stored)))
        return True

    def reply_append(self) -> tuple[dict[str, Any], dict[str, bytes]] | None:
        for entry, held in self.appends:
            part = entry.get("payload", {}).get("part", {})
            if entry.get("kind") == "part.created" and "charts/active.png" in part.get("text", ""):
                return entry, held
        return None


class _TickingWatcher:
    """What the production watcher does between writes: hand the loop an
    empty batch on its own cadence, so a queued promotion is taken."""

    def __init__(self) -> None:
        self.stop = threading.Event()

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            while not self.stop.is_set():
                yield set()
                await asyncio.sleep(0.005)

        return stream()


@dataclass
class _Rig:
    mirror: ChatMirror
    adapter: FakeAdapter
    doc: _Doc
    drive: FakeLiveApi
    sync: LiveSync
    tree: Path


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + HANG
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


def _held(folders: ChatFolders, chat_root: Path, sync: LiveSync) -> None:
    """The folder as the service holds it once the chat is served: the chat
    folder on disk, its live sync on the working directory inside it."""
    folders._held[CHAT_ID] = cast(HeldFolder, SimpleNamespace(root=chat_root, live=sync))


@asynccontextmanager
async def _rig(
    tmp_path: Path, *, landing: bool = True, landing_seconds: float = HANG, run_sync: bool = True
) -> AsyncIterator[_Rig]:
    project = ProjectDirectory(tmp_path / ".alkera")
    project.chats().create(session_id=CHAT_ID, title="charts").close()
    chat_root = tmp_path / "chat"
    tree = chat_root / "scratch"
    tree.mkdir(parents=True)
    drive = FakeLiveApi(root=tree)
    watcher = _TickingWatcher()
    # The settle time outlasts the test: the ordinary content queue cannot send
    # the chart in time, so only a landing can.
    sync = _sync(tree, drive, FakeClock(), cadence=LiveCadence(settle_ms=600_000), watcher=watcher)
    folders = ChatFolders(chats_root=tmp_path / "chats")
    _held(folders, chat_root, sync)

    async def land_files(targets: list[str]) -> frozenset[str]:
        return await asyncio.to_thread(folders.land, CHAT_ID, targets, timeout=landing_seconds)

    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(project, adapter_factory=factory)
    doc = _Doc(drive=drive)
    doc.live.set()
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, SimpleNamespace(sizes=FALLBACK_SIZES)),
        rest=CloudRestClient(
            api_url="http://reply.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        chunk_interval=0.01,
        land_files=land_files if landing else None,
    )
    mirror._doc = cast(Any, doc)
    mirror._ready.set()
    session = await runtime.open_chat(CHAT_ID)
    mirror._session = session
    syncing = threading.Thread(target=lambda: asyncio.run(sync.run()), daemon=True)
    if run_sync:
        syncing.start()
    tasks = [
        asyncio.create_task(mirror._pump(session.subscribe())),
        asyncio.create_task(mirror._publisher()),
        asyncio.create_task(mirror._flusher()),
    ]
    try:
        yield _Rig(
            mirror=mirror,
            adapter=factory.adapters[-1],
            doc=doc,
            drive=drive,
            sync=sync,
            tree=tree,
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        watcher.stop.set()
        if run_sync:
            syncing.join(HANG)
        await session.close()
        await runtime.close_all()


def _write_chart(tree: Path) -> None:
    """The agent's tool has just written the chart."""
    path = _write(tree, "charts/active.png", CHART)
    now = time.time_ns()
    os.utime(path, ns=(now, now))


def _reply_part(text: str = REPLY) -> PartCreated:
    return PartCreated(
        event_id="part-reply",
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        part=TextPart(part_id="p-reply", message_id="m-reply", text=text),
    )


def _chunk(seq: int, text: str, *, final: bool = False) -> AgentMessageChunk:
    return AgentMessageChunk(
        event_id=f"chunk-{seq}",
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        message_id="m-reply",
        part_id="p-reply",
        sequence=seq,
        text=text,
        is_final=final,
    )


async def test_the_reply_naming_a_chart_is_published_after_the_chart_is_on_the_drive(
    tmp_path: Path,
) -> None:
    async with _rig(tmp_path) as rig:
        _write_chart(rig.tree)
        await rig.adapter.feed(_reply_part())
        await _settle(lambda: rig.doc.reply_append() is not None, "the reply to be published")

        published = rig.doc.reply_append()
        assert published is not None
        _entry, held = published
        assert held.get("charts/active.png") == CHART


async def test_without_the_landing_the_same_reply_goes_first_and_the_chart_is_not_there(
    tmp_path: Path,
) -> None:
    """The control: the ordering above is the landing's doing. With nothing
    holding the reply, a chart this fresh is still only on the box when the
    reply is published -- which is the placeholder a reader was shown."""
    async with _rig(tmp_path, landing=False) as rig:
        _write_chart(rig.tree)
        await rig.adapter.feed(_reply_part())
        await _settle(lambda: rig.doc.reply_append() is not None, "the reply to be published")

        published = rig.doc.reply_append()
        assert published is not None
        assert "charts/active.png" not in published[1]


async def test_a_streamed_reference_is_sent_only_once_the_chart_is_on_the_drive(
    tmp_path: Path,
) -> None:
    """The web shows the reply as it streams. The reference arrives in two
    frames; the frame that completes it is the one that must wait."""
    async with _rig(tmp_path) as rig:
        _write_chart(rig.tree)
        await rig.adapter.feed(_chunk(1, "Here is the breakdown:\n\n![Active prompts](charts/"))
        await _settle(lambda: bool(rig.doc.chunks), "the first frame")
        await rig.adapter.feed(_chunk(2, "active.png)\n", final=True))
        await _settle(
            lambda: "active.png)" in "".join(event["text"] for event, _ in rig.doc.chunks),
            "the frame that completes the reference",
        )

        streamed = ""
        for event, held in rig.doc.chunks:
            streamed += event["text"]
            if "charts/active.png)" in streamed:
                assert held.get("charts/active.png") == CHART
                break
        else:
            raise AssertionError("the reference never streamed")


async def test_a_reply_without_chat_files_is_not_held(tmp_path: Path) -> None:
    """Nothing to land, so no sync round is waited for: with the sync not
    running at all, the reply still goes."""
    async with _rig(tmp_path, run_sync=False) as rig:
        await rig.adapter.feed(
            _reply_part("See [the docs](https://example.com/x.png) and `SELECT 1`.")
        )
        await _settle(lambda: bool(rig.doc.appends), "the reply to be published")


async def test_a_drive_that_never_takes_the_chart_holds_the_reply_only_for_the_wait(
    tmp_path: Path,
) -> None:
    """The landing's ceiling: a sync that never runs a round lands nothing,
    and the reply goes once the wait is spent rather than never."""
    async with _rig(tmp_path, landing_seconds=0.05, run_sync=False) as rig:
        _write_chart(rig.tree)
        await rig.adapter.feed(_reply_part())
        await _settle(lambda: rig.doc.reply_append() is not None, "the reply to be published")

        published = rig.doc.reply_append()
        assert published is not None
        assert "charts/active.png" not in published[1]


# -- what a reference names on the box ---------------------------------------


def _landed_sync(tmp_path: Path) -> tuple[ChatFolders, FakeLiveApi]:
    chat_root = tmp_path / "chat"
    tree = chat_root / "scratch"
    tree.mkdir(parents=True)
    drive = FakeLiveApi(root=tree)
    sync = _sync(tree, drive, FakeClock(), cadence=LiveCadence(settle_ms=0))
    _write(tree, "charts/active.png", CHART)
    sync.classify(Change.added, str(tree / "charts/active.png"))
    sync.flush()
    folders = ChatFolders(chats_root=tmp_path / "chats")
    _held(folders, chat_root, sync)
    return folders, drive


@pytest.mark.parametrize(
    ("target", "lands"),
    [
        pytest.param("charts/active.png", True, id="relative-to-the-working-directory"),
        pytest.param("./charts/active.png", True, id="dot-slash"),
        pytest.param("charts/active%2Epng", True, id="percent-escaped"),
        pytest.param(
            f"/opt/alkera-work/.alkera/chats/{CHAT_ID}/scratch/charts/active.png",
            True,
            id="the-boxs-absolute-path-into-this-chat",
        ),
        pytest.param(
            "/opt/alkera-work/.alkera/chats/another-chat/scratch/charts/active.png",
            False,
            id="another-chats-folder",
        ),
        pytest.param(
            f"/opt/alkera-work/.alkera/chats/{CHAT_ID}/notes.md",
            False,
            id="beside-the-working-directory",
        ),
        pytest.param("https://example.com/charts/active.png", False, id="a-url"),
        pytest.param("../scratch/charts/active.png", False, id="a-climb"),
    ],
)
async def test_the_folder_lands_what_a_reference_names_inside_its_working_directory(
    tmp_path: Path, target: str, lands: bool
) -> None:
    folders, _drive = _landed_sync(tmp_path)

    landed = folders.land(CHAT_ID, [target], timeout=HANG)

    assert landed == (frozenset({"charts/active.png"}) if lands else frozenset())


async def test_a_folder_with_no_live_plane_lands_nothing_and_waits_for_nothing(
    tmp_path: Path,
) -> None:
    folders = ChatFolders(chats_root=tmp_path / "chats")
    assert folders.land(CHAT_ID, ["charts/active.png"], timeout=HANG) == frozenset()
    folders._held[CHAT_ID] = cast(HeldFolder, SimpleNamespace(root=tmp_path, live=None))
    assert folders.land(CHAT_ID, ["charts/active.png"], timeout=HANG) == frozenset()


async def test_the_mirror_the_service_builds_lands_through_the_folder_it_holds(
    tmp_path: Path,
) -> None:
    """The wiring: a served chat's mirror is handed the service's landing,
    which reaches the live sync of the folder the service holds for it."""
    from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings

    folders, _drive = _landed_sync(tmp_path)
    # A box that can hold folders at all: the clients are never called here.
    folders._files = cast(Any, object())
    folders._http = cast(Any, object())
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    settings = MirrorSettings(
        api_url="http://127.0.0.1:9",
        token="device-jwt",
        project_dir=tmp_path,
        machine_name="box",
        provider_pod_id="pod-box",
        machine_type_code="cpu3c",
    )
    rest = CloudRestClient(
        api_url=settings.api_url,
        token=settings.token,
        agent_id="machine:box",
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
    )
    service = CloudMirrorService(
        settings, runtime, rest=rest, socket=cast(CloudSocket, object()), folders=folders
    )
    mirror = service._default_mirror(CHAT_ID, {"id": CHAT_ID, "owner_user_id": OWNER})
    try:
        assert mirror._land_files is not None
        assert await mirror._land_files(["charts/active.png"]) == frozenset({"charts/active.png"})
        assert await mirror._land_files(["charts/never.png"]) == frozenset()
    finally:
        await runtime.close_all()
