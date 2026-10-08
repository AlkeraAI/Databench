"""A box serving the chats of one workspace: one lease, one shared tree.

What a box does differently for a chat that is a member of a native workspace,
and the evidence that it does nothing differently for every other chat:

* the row decides (``seat_of``): a workspace of one, a row from a backend that
  says nothing of workspaces, and a member whose folder left its workspace are
  three different answers;
* the custody keys a workspace apart from its chats: it lands beside them,
  is leased as ``workspace``, and moves its shared ``files/`` tree and never
  the chats' ``.chats/`` records;
* two members share one lease and one tree; the first to wake takes the
  workspace, the last to sleep puts it away;
* sleep has two tiers, driven across the real hold windows with freezegun:
  a member's server stops at once; the workspace waits for whatever a member
  left running and for a person still live-editing its files, and under
  memory pressure goes anyway;
* what the box says on a chat it serves in a workspace of one is byte for
  byte what it always said.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _mirror_service import FakeMirror, build_service
from alkera_cli.cloud.custody_layout import (
    CHAT_FOLDER_PURPOSE,
    PULL_LOCAL,
    PULL_WORKSPACE,
    PUSH_LOCAL,
    PUSH_WORKSPACE,
    WORKSPACE_FOLDER_PURPOSE,
    CustodyLayout,
)
from alkera_cli.cloud.faults import TRANSIENT_LIMIT
from alkera_cli.cloud.folder import FolderBusyError, FolderHandBackError
from alkera_cli.cloud.mirror import ChatMirrorRefusedError
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.service import (
    CHAT_UPDATED,
    DRAINED_ENDING,
    FILE_LEASE_CHANGED,
    CloudMirrorService,
)
from alkera_cli.cloud.workspace_host import LIVE_HOLD_SECONDS, Joined, WorkspaceHost
from alkera_cli.cloud.workspace_seat import (
    MOVED_OUT,
    SeatRefused,
    WorkspaceSeat,
    seat_of,
    workspace_key,
)
from alkera_cli.files.pull import pull
from alkera_cli.harness.sandbox_processes import SandboxProcess
from alkera_cli.harness.sandbox_scope import SCOPE_KEY, forget_scope, scope_of
from alkera_core.files.history import LEASE_CHANGED_INBOUND
from freezegun import freeze_time

WS = "8a7c1f2e-0000-4000-8000-000000000001"
WS_KEY = workspace_key(WS)
WS_NODE = "8a7c1f2e-0000-4000-8000-0000000000aa"
FILES_NODE = "8a7c1f2e-0000-4000-8000-0000000000ab"
DRIVE = "8a7c1f2e-0000-4000-8000-0000000000dd"
EVENING = "2026-10-05 17:30:00"
LEFT_RUNNING = (SandboxProcess(pid=15, ppid=1, command="python", state="S"),)


def _member(chat_id: str, **overrides: Any) -> dict[str, Any]:
    """A chat row as a backend that knows workspaces serves a member's."""
    return {
        "id": chat_id,
        "workspace_id": WS,
        "workspace_layout": "native",
        "workspace_node_id": WS_NODE,
        "workspace_files_node_id": FILES_NODE,
        "files_node_id": f"node-{chat_id}",
        "files_drive_id": DRIVE,
        **overrides,
    }


@pytest.fixture(autouse=True)
def _scopes_forgotten() -> Iterator[None]:
    yield
    for chat_id in ("chat-a", "chat-b", "solo"):
        forget_scope(chat_id)


# -- the row decides -------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"id": "c"}, id="a-backend-that-knows-no-workspaces"),
        pytest.param(
            {"id": "c", "workspace_id": WS, "workspace_layout": "adopted"},
            id="a-workspace-of-one",
        ),
        pytest.param(
            {"id": "c", "workspace_layout": "native"}, id="native-but-naming-no-workspace"
        ),
    ],
)
def test_a_chat_on_its_own_is_seated_nowhere(row: dict[str, Any]) -> None:
    assert seat_of(row) is None


def test_a_member_is_seated_in_its_workspace_whichever_case_the_row_spells() -> None:
    expected = WorkspaceSeat(
        workspace_id=WS, node_id=WS_NODE, files_node_id=FILES_NODE, drive_id=DRIVE
    )
    assert seat_of(_member("c")) == expected
    camel = {
        "workspaceId": WS,
        "workspaceLayout": "native",
        "workspaceNodeId": WS_NODE,
        "workspaceFilesNodeId": FILES_NODE,
        "filesDriveId": DRIVE,
    }
    assert seat_of(camel) == expected
    assert expected.custody_row() == {"files_node_id": WS_NODE, "files_drive_id": DRIVE}


@pytest.mark.parametrize("missing", ["workspace_node_id", "workspace_files_node_id"])
def test_a_member_whose_folder_left_its_workspace_is_refused(missing: str) -> None:
    assert seat_of(_member("c", **{missing: None})) == SeatRefused(MOVED_OUT)


# -- the custody keys a workspace apart ------------------------------------------


def test_a_workspace_lands_beside_the_chats_and_is_leased_as_a_workspace(tmp_path: Path) -> None:
    chats = tmp_path / ".alkera" / "chats"
    layout = CustodyLayout.beside(chats)
    assert layout.root("chat-a") == chats / "chat-a"
    assert layout.root(WS_KEY) == tmp_path / ".alkera" / "workspaces" / WS
    assert layout.bound(WS_KEY) == tmp_path / ".alkera" / "workspaces"
    assert layout.bound("chat-a") == chats
    assert (layout.purpose("chat-a"), layout.purpose(WS_KEY)) == (
        CHAT_FOLDER_PURPOSE,
        WORKSPACE_FOLDER_PURPOSE,
    )


def test_a_workspace_pushes_its_shared_tree_and_never_the_chats_records() -> None:
    layout = CustodyLayout.beside(Path("/x/chats"))
    pushed = layout.push(WS_KEY, ["files/gone.txt"], PUSH_LOCAL)
    assert pushed.func is PUSH_WORKSPACE.func
    assert pushed.keywords["skip"] == ["files/gone.txt", ".chats"]
    assert (
        pushed.keywords["exclude_presets_within"]
        == PUSH_WORKSPACE.keywords["exclude_presets_within"]
    )
    # A chat's push is exactly the one it always was, tombstones and all.
    chat = layout.push("chat-a", ["scratch/gone.txt"], PUSH_LOCAL)
    assert chat.func is PUSH_LOCAL.func
    assert chat.keywords == {**PUSH_LOCAL.keywords, "skip": ["scratch/gone.txt"], "bases": {}}
    assert layout.pull("chat-a", PULL_LOCAL).keywords == {
        **PULL_LOCAL.keywords,
        "chat_id": "chat-a",
    }
    assert layout.pull(WS_KEY, PULL_LOCAL).func is PULL_WORKSPACE.func


class _Tree:
    """The slice of ``client.files`` a pull drives: a workspace's folder with
    its shared tree and one chat's records."""

    ROOT = "ws"
    CHILDREN: Mapping[str, list[dict[str, Any]]] = {
        "ws": [
            {"id": "files", "kind": "folder", "name": "files"},
            {"id": "chats", "kind": "folder", "name": ".chats"},
        ],
        "files": [{"id": "sub", "kind": "folder", "name": "data"}],
        "sub": [],
        "chats": [{"id": "rec", "kind": "folder", "name": "c1.alkerachat"}],
        "rec": [],
    }

    def __init__(self) -> None:
        self.listed: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        return {"id": item_id, "kind": "folder", "name": "Pricing.alkeraworkspace", **_CAN}

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        self.listed.append(item_id)
        for row in self.CHILDREN[item_id]:
            yield {**row, **_CAN}


_CAN: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}


def test_a_workspace_pull_brings_down_the_shared_tree_and_never_walks_the_records(
    tmp_path: Path,
) -> None:
    tree = _Tree()
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404))) as http:
        pull(files=tree, http=http, root=tmp_path, source="", node_id="ws", only=b"files")
    assert (tmp_path / "files" / "data").is_dir()
    assert not (tmp_path / ".chats").exists()
    assert "chats" not in tree.listed and "rec" not in tree.listed


# -- one lease, one tree, two members --------------------------------------------


class _Custody:
    """The folder custody as the workspace host drives it, recording each verb."""

    def __init__(self, root: Path, *, busy: bool = False) -> None:
        self.layout = CustodyLayout.beside(root / ".alkera" / "chats")
        self.busy = busy
        self.taken: list[tuple[str, dict[str, Any], str]] = []
        self.handed_back: list[tuple[str, str | None]] = []
        self.pushed: list[str] = []
        self.streamed: list[tuple[str, Path]] = []
        self.held_keys: set[str] = set()
        #: Folders given back with nothing pushed: taken for a start that failed.
        self.released: list[str] = []
        #: What each hand-back was told: whether to recover, whether it is gone.
        self.told: dict[str, tuple[bool, bool]] = {}

    @property
    def enabled(self) -> bool:
        return True

    def take(self, chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any:
        if self.busy:
            raise FolderBusyError(chat_id, holder="another-box")
        self.taken.append((chat_id, dict(chat), instance))
        self.held_keys.add(chat_id)
        return object()

    def held(self, chat_id: str) -> Any:
        if chat_id not in self.held_keys:
            return None
        return SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)

    def local_root(self, chat_id: str) -> Path:
        return self.layout.root(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        self.streamed.append((chat_id, working_dir))

    def push(self, chat_id: str) -> Any:
        self.pushed.append(chat_id)

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self.handed_back.append((chat_id, ending))
        self.told[chat_id] = (recover, gone)
        self.held_keys.discard(chat_id)

    def release(self, chat_id: str) -> bool:
        self.released.append(chat_id)
        self.held_keys.discard(chat_id)
        return True

    def owed(self) -> list[str]:
        return []


class _Refusals:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []
        self.kinds: list[str] = []

    async def __call__(self, chat_id: str, reason: str, kind: str) -> None:
        self.said.append((chat_id, reason))
        self.kinds.append(kind)


class _Work:
    """What the workspace's sandbox runs besides its agent servers."""

    def __init__(self) -> None:
        self.running: tuple[SandboxProcess, ...] | None = ()
        self.read: list[str] = []

    def __call__(self, key: str) -> tuple[SandboxProcess, ...] | None:
        self.read.append(key)
        return self.running


def _monotonic() -> float:
    return time.monotonic()


async def _inline(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def _host(
    custody: _Custody,
    *,
    work: _Work | None = None,
    refusals: _Refusals | None = None,
    released: list[str] | None = None,
    memory: dict[str, int] | None = None,
) -> WorkspaceHost:
    async def release(key: str) -> None:
        if released is not None:
            released.append(key)

    return WorkspaceHost(
        folders=custody,
        instance_of=lambda key: f"box-1:{key}",
        refuse=refusals or _Refusals(),
        clock=_monotonic,
        sandbox_work=work,
        memory_of=(lambda key: (memory or {}).get(key)),
        release_sandbox=release,
        in_thread=_inline,
    )


async def test_two_members_share_one_lease_and_one_tree(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    host = _host(custody)

    assert await host.join("chat-a", _member("chat-a")) is Joined.MEMBER
    assert await host.join("chat-b", _member("chat-b")) is Joined.MEMBER

    # The first member to wake takes the workspace's folder; the second finds it held.
    assert custody.taken == [
        (WS_KEY, {"files_node_id": WS_NODE, "files_drive_id": DRIVE}, f"box-1:{WS_KEY}")
    ]
    shared = custody.layout.root(WS_KEY) / "files"
    assert custody.streamed == [(WS_KEY, shared)]
    assert host.working_dir("chat-a") == host.working_dir("chat-b") == shared
    assert host.live_key("chat-a") == host.live_key("chat-b") == WS_KEY
    assert host.keys() == [WS_KEY]
    assert host.members(WS_KEY) == {"chat-a", "chat-b"}
    # Every process of either member runs as the workspace's uid.
    assert scope_of("chat-a").tree == scope_of("chat-b").tree == WS_KEY
    assert host.sandbox_bag("chat-a") == {SCOPE_KEY: WS_KEY}


async def test_a_chat_on_its_own_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    host = _host(custody)
    solo = {"id": "solo", "workspace_id": WS, "workspace_layout": "adopted"}

    assert await host.join("solo", solo) is Joined.SOLO
    assert custody.taken == []
    assert host.live_key("solo") == "solo"
    assert host.working_dir("solo") is None
    assert host.sandbox_bag("solo") == {}
    assert host.report_facets("solo") == {}
    assert scope_of("solo").member is False
    assert await host.leave("solo") is None


async def test_a_workspace_another_box_holds_serves_no_member_here(tmp_path: Path) -> None:
    custody = _Custody(tmp_path, busy=True)
    host = _host(custody)

    assert await host.join("chat-a", _member("chat-a")) is Joined.NOT_HERE
    assert host.seat("chat-a") is None
    assert host.keys() == []


async def test_a_member_whose_folder_left_its_workspace_is_refused_on_its_banner(
    tmp_path: Path,
) -> None:
    refusals = _Refusals()
    custody = _Custody(tmp_path)
    host = _host(custody, refusals=refusals)

    joined = await host.join("chat-a", _member("chat-a", workspace_node_id=None))

    assert joined is Joined.NOT_HERE
    assert refusals.said == [("chat-a", MOVED_OUT)]
    assert refusals.kinds == ["workspace_unservable"]
    assert custody.taken == []


async def test_the_last_member_to_sleep_puts_the_workspace_away(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    released: list[str] = []
    host = _host(custody, released=released, memory={WS_KEY: 612})
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))

    assert await host.leave("chat-a", ending="idle") == "awake"
    assert custody.handed_back == []
    assert host.report_facets("chat-a") == {
        "workspace_sandbox": "awake",
        "workspace_memory_mb": 612,
    }

    assert await host.leave("chat-b", ending="idle") == "asleep"
    assert custody.handed_back == [(WS_KEY, "idle")]
    assert released == [WS_KEY]
    assert host.keys() == []
    assert host.report_facets("chat-b")["workspace_sandbox"] == "asleep"


async def test_waking_a_member_wakes_the_workspace_again(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))
    await host.leave("chat-a", ending="idle")

    assert await host.join("chat-a", _member("chat-a")) is Joined.MEMBER
    assert [taken[0] for taken in custody.taken] == [WS_KEY, WS_KEY]
    assert host.state(WS_KEY) == "awake"


async def test_a_process_left_running_keeps_the_workspace_up_until_it_ends(
    tmp_path: Path,
) -> None:
    """The second tier: no member is awake, but something a member started
    still runs in the sandbox. The workspace waits for it, then goes."""
    custody = _Custody(tmp_path)
    work = _Work()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        host = _host(custody, work=work)
        await host.join("chat-a", _member("chat-a"))
        work.running = LEFT_RUNNING

        assert await host.leave("chat-a", ending="idle") == "awake"
        assert custody.handed_back == []

        frozen.tick(timedelta(hours=3))
        assert await host.sweep() == []
        assert host.keys() == [WS_KEY]

        work.running = ()
        assert await host.sweep() == [WS_KEY]
        assert custody.handed_back == [(WS_KEY, "idle")]


async def test_a_process_holds_the_workspace_for_as_long_as_it_runs(tmp_path: Path) -> None:
    """As a process a command left behind holds a chat, one a member left
    running holds its workspace, days on; a sandbox that cannot be read
    holds nothing."""
    custody = _Custody(tmp_path)
    work = _Work()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        host = _host(custody, work=work)
        await host.join("chat-a", _member("chat-a"))
        work.running = LEFT_RUNNING
        await host.leave("chat-a", ending="idle")

        frozen.tick(timedelta(days=3))
        assert await host.sweep() == []
        assert work.read.count(WS_KEY) >= 2

        work.running = None  # the sandbox can no longer be read
        assert await host.sweep() == [WS_KEY]


async def test_a_live_edit_holds_the_workspace_for_its_window_and_no_longer(
    tmp_path: Path,
) -> None:
    """Somebody typing into a shared file through the live editor keeps the
    shared tree on the box for the hold window after their last write; then
    the workspace goes, and their next edit lands on the drive directly."""
    custody = _Custody(tmp_path)
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        host = _host(custody)
        await host.join("chat-a", _member("chat-a"))
        frozen.tick(timedelta(minutes=5))
        host.note_used(WS_KEY)
        assert await host.leave("chat-a", ending="idle") == "awake"

        frozen.tick(timedelta(seconds=LIVE_HOLD_SECONDS - 60))
        assert await host.sweep() == []
        host.note_used("chat-a")  # an edit seen through a member's key counts too
        frozen.tick(timedelta(seconds=LIVE_HOLD_SECONDS - 1))
        assert await host.sweep() == []
        frozen.tick(timedelta(seconds=2))
        assert await host.sweep() == [WS_KEY]
        assert custody.handed_back == [(WS_KEY, "idle")]


async def test_under_memory_pressure_a_workspace_held_only_by_work_goes(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    work = _Work()
    host = _host(custody, work=work)
    await host.join("chat-a", _member("chat-a"))
    work.running = LEFT_RUNNING
    await host.leave("chat-a", ending="idle")
    assert await host.sweep() == []

    assert await host.sweep(pressure=True) == [WS_KEY]
    assert custody.handed_back == [(WS_KEY, "evicted")]


async def test_a_workspace_with_a_member_awake_is_never_swept(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        host = _host(custody)
        await host.join("chat-a", _member("chat-a"))
        frozen.tick(timedelta(days=3))
        assert await host.sweep(pressure=True) == []
        assert custody.handed_back == []


async def test_a_lost_workspace_lease_stops_every_member(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    released: list[str] = []
    host = _host(custody, released=released)
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))

    stop = await host.to_stop(WS_KEY, {"chat-a": 1, "chat-b": 2})

    assert stop == ["chat-a", "chat-b"]
    assert host.keys() == []
    assert released == [WS_KEY]
    # A chat's own lost lease stops that chat and nothing else.
    assert await host.to_stop("chat-x", {"chat-x": 1}) == ["chat-x"]


# -- the service, end to end over the custody ------------------------------------


def _member_service(
    tmp_path: Path, custody: _Custody, **kwargs: Any
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    service, built = build_service(tmp_path, clock=_monotonic, folders=custody, **kwargs)  # type: ignore[arg-type]
    return service, built


async def test_the_service_takes_the_workspace_before_each_members_records(
    tmp_path: Path,
) -> None:
    custody = _Custody(tmp_path)
    service, built = _member_service(tmp_path, custody)

    await service._ensure_mirror("chat-a", _member("chat-a"))
    await service._ensure_mirror("chat-b", _member("chat-b"))

    assert [taken[0] for taken in custody.taken] == [WS_KEY, "chat-a", "chat-b"]
    assert set(built) == {"chat-a", "chat-b"}
    # The beat keeps the workspace's lease beside each chat's own.
    assert service._workspaces.custody_keys(service._mirrors) == ["chat-a", "chat-b", WS_KEY]

    await service._release_mirror("chat-a", ending="idle")
    assert custody.handed_back == [("chat-a", "idle")]
    await service._release_mirror("chat-b", ending="idle")
    assert custody.handed_back == [("chat-a", "idle"), ("chat-b", "idle"), (WS_KEY, "idle")]


async def test_a_member_whose_records_cannot_be_taken_leaves_the_workspace(
    tmp_path: Path,
) -> None:
    custody = _Custody(tmp_path)

    original = custody.take

    def take(chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any:
        if chat_id == "chat-a":
            raise FolderBusyError(chat_id)
        return original(chat_id, chat, instance=instance)

    custody.take = take  # type: ignore[method-assign]
    service, built = _member_service(tmp_path, custody)

    await service._ensure_mirror("chat-a", _member("chat-a"))

    assert built == {}
    assert custody.handed_back == [(WS_KEY, None)]
    assert service._workspaces.keys() == []


async def test_the_connections_are_read_for_every_workspace_the_box_holds(
    tmp_path: Path,
) -> None:
    """A held workspace's notebook kernels run here, so the box's connections
    client asks that workspace's door, and stops once the workspace is put
    away (a box on its machine credential reads per chat and per workspace)."""
    import respx
    from alkera_cli.cloud_sync.client import (
        install_connections_client,
        resolve_connections_client,
    )

    custody = _Custody(tmp_path)
    service, _ = _member_service(tmp_path, custody, token="alk_machine_test")
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"connections": []})

    try:
        service.schema_cards  # noqa: B018 - building it installs the box's client
        client = resolve_connections_client()
        assert client is not None
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__regex=r".*/connections$").mock(side_effect=answer)
            assert await client.fetch_records() == []
            assert asked == []
            await service._ensure_mirror("chat-a", _member("chat-a"))
            assert service.held_workspace_ids() == [WS]
            await client.fetch_records()
            assert f"/api/v1/workspaces/{WS}/connections" in asked
            await service._release_mirror("chat-a", ending="idle")
            assert service.held_workspace_ids() == []
    finally:
        install_connections_client(None)


async def test_the_box_s_connections_client_speaks_the_bearer_the_box_holds_now(
    tmp_path: Path,
) -> None:
    """An org worker's credential is replaced every few minutes; the
    connections client the box installs (the one a notebook's SQL leases
    through) signs each request with the bearer the box holds at that moment,
    not the one it started with."""
    import respx
    from alkera_cli.cloud.rest import BoxCredential
    from alkera_cli.cloud_sync.client import (
        install_connections_client,
        resolve_connections_client,
    )

    current = ["alk_machine_first"]
    rest = CloudRestClient(
        api_url="http://127.0.0.1:1",
        token=BoxCredential(current[0], read=lambda: current[0]),
        agent_id=None,
    )
    service, _ = _member_service(tmp_path, _Custody(tmp_path), token="alk_machine_first", rest=rest)
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        return httpx.Response(200, json={"connections": []})

    try:
        service.schema_cards  # noqa: B018 - building it installs the box's client
        await service._ensure_mirror("chat-a", _member("chat-a"))
        client = resolve_connections_client()
        assert client is not None
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__regex=r".*/connections$").mock(side_effect=answer)
            await client.fetch_records()
            current[0] = "alk_machine_second"
            assert rest.credential.refresh()
            sent.clear()
            await client.fetch_records()
        assert sent and set(sent) == {"Bearer alk_machine_second"}
    finally:
        install_connections_client(None)


# -- what a workspace of one says, unchanged --------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={})


@pytest.mark.parametrize("ending", [None, "idle"])
async def test_a_chat_on_its_own_reports_exactly_what_it_always_did(
    tmp_path: Path, ending: str | None
) -> None:
    """The golden: the publisher-state body a box sends for a workspace of
    one carries no workspace word at all."""
    recorder = _Recorder()
    rest = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine:x",
        transport=httpx.MockTransport(recorder),
    )
    service, _ = _member_service(tmp_path, _Custody(tmp_path), rest=rest)
    await service._workspaces.join("solo", {"id": "solo", "workspace_layout": "adopted"})

    await service.report_publisher_state(
        "solo", "asleep" if ending else "publishing", ending=ending
    )

    expected: dict[str, Any] = {"state": "asleep" if ending else "publishing", "reason": ""}
    if ending is not None:
        expected["ending"] = ending
    assert recorder.bodies == [("/api/v1/chats/solo/publisher-state", expected)]


async def test_a_member_says_how_its_workspace_is_on_every_report(tmp_path: Path) -> None:
    recorder = _Recorder()
    rest = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine:x",
        transport=httpx.MockTransport(recorder),
    )
    service, _ = _member_service(tmp_path, _Custody(tmp_path), rest=rest)
    await service._workspaces.join("chat-a", _member("chat-a"))

    await service.report_publisher_state("chat-a", "publishing")

    assert recorder.bodies[-1][1]["workspace_sandbox"] == "awake"
    assert recorder.bodies[-1][1]["state"] == "publishing"


async def test_a_box_going_away_puts_away_a_workspace_something_held_up(
    tmp_path: Path,
) -> None:
    """A hold keeps the shared tree on a box that stays up. A box that is
    going away takes its sandboxes with it, so what the hold protected is
    gone already; the lease is handed back rather than left to keep every
    other box off the tree until it lapses. A workspace a member is still
    in is left to that member's own release."""
    custody = _Custody(tmp_path)
    work = _Work()
    host = _host(custody, work=work)
    await host.join("chat-a", _member("chat-a"))
    work.running = LEFT_RUNNING
    host.note_used(WS_KEY)
    assert await host.leave("chat-a", ending="idle") == "awake"
    assert await host.sweep() == []

    assert await host.put_away_every(ending="drained") == [WS_KEY]
    assert custody.handed_back == [(WS_KEY, "drained")]
    assert host.keys() == []

    other = _Custody(tmp_path / "other")
    busy = _host(other)
    await busy.join("chat-b", _member("chat-b"))
    assert await busy.put_away_every(ending="drained") == []
    assert other.handed_back == []


async def test_the_service_stopping_hands_back_a_held_up_workspace(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    service, _built = _member_service(tmp_path, custody)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    service._workspaces.note_used(WS_KEY)  # somebody was typing into a shared file

    await service.stop()

    assert ("chat-a", DRAINED_ENDING) in custody.handed_back
    assert (WS_KEY, DRAINED_ENDING) in custody.handed_back


# -- a workspace deleted, a person's edit, the box's own writes -----------------


async def test_a_workspace_whose_last_member_was_deleted_is_handed_back_as_gone(
    tmp_path: Path,
) -> None:
    """The backend said the member that left last was deleted: a shared tree
    the drive no longer answers for is the workspace's deletion, and nothing
    of it stays on the box. A member that merely slept says nothing of the
    kind, and an earlier member's deletion does not speak for the last one."""
    custody = _Custody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))
    await host.leave("chat-a", gone=True)
    await host.leave("chat-b", gone=True)
    assert custody.told[WS_KEY] == (True, True)

    slept = _Custody(tmp_path / "slept")
    host = _host(slept)
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))
    await host.leave("chat-a", gone=True)
    await host.leave("chat-b", ending="idle")
    assert slept.told[WS_KEY] == (True, False)


async def test_a_lost_workspace_lease_lets_the_custody_forget_without_landing_it(
    tmp_path: Path,
) -> None:
    custody = _Custody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))

    assert await host.lost(WS_KEY) == frozenset({"chat-a"})
    assert custody.told[WS_KEY] == (False, False)
    assert custody.held(WS_KEY) is None


class _StreamRest(CloudRestClient):
    """The chat routes and the event stream, the stream under the test's hand.
    A chat in ``deleted`` reads 404, as a tombstone does."""

    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.deleted: set[str] = set()
        self.said: list[tuple[str, str]] = []

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        if chat_id in self.deleted:
            raise CloudApiError(
                404, {"detail": "Not found"}, method="GET", path=f"/chats/{chat_id}"
            )
        return _member(chat_id)

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.said.append((chat_id, state))
        return {}

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self.frames.get()

    def ring(self, kind: str, entity_id: str, **payload: Any) -> None:
        self.frames.put_nowait(
            {"id": None, "type": kind, "data": {"type": kind, "entity_id": entity_id, **payload}}
        )


async def _streaming(
    tmp_path: Path, custody: _Custody
) -> tuple[CloudMirrorService, _StreamRest, asyncio.Task[None]]:
    rest = _StreamRest()
    service, _built = _member_service(tmp_path, custody, rest=rest)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    return service, rest, asyncio.create_task(service._stream_loop())


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


@pytest.mark.parametrize(
    ("reason", "gone"),
    [
        pytest.param("deleted", True, id="the-stream-said-deleted"),
        pytest.param(None, False, id="a-not-found-alone"),
    ],
)
async def test_a_chat_the_stream_says_was_deleted_leaves_nothing_of_it_or_its_workspace(
    tmp_path: Path, reason: str | None, gone: bool
) -> None:
    """Deleting a workspace deletes its chats and rings each one's doorbell
    with ``reason: deleted``. The box stops the member and hands its records
    and the shared tree back as gone, so neither stays on its disk. A chat
    that only reads 404 (a box that lost its sight of a live chat is told the
    same) is handed back as before, its tree kept if the drive says nothing."""
    custody = _Custody(tmp_path)
    service, rest, stream = await _streaming(tmp_path, custody)
    try:
        rest.deleted.add("chat-a")
        rest.ring(CHAT_UPDATED, "chat-a", **({"reason": reason} if reason else {}))
        await _until(lambda: WS_KEY in custody.told)
    finally:
        stream.cancel()
    assert custody.told["chat-a"] == (False, gone)
    assert custody.told[WS_KEY] == (True, gone)
    assert "chat-a" not in service._mirrors


@pytest.mark.parametrize(
    ("reason", "held"),
    [
        pytest.param(LEASE_CHANGED_INBOUND, True, id="a-persons-write-holds-it"),
        pytest.param(None, False, id="a-cleared-entry-does-not"),
        pytest.param("live_batch", False, id="the-boxs-own-batch-does-not"),
    ],
)
async def test_only_a_write_the_drive_queued_holds_the_workspace(
    tmp_path: Path, reason: str | None, held: bool
) -> None:
    """A person typing into a shared file holds the workspace on the box for
    the hold window. The box's own writes ring the same lease, and holding
    the workspace for those kept every workspace up for fifteen minutes after
    its last turn."""
    custody = _Custody(tmp_path)
    service, rest, stream = await _streaming(tmp_path, custody)
    try:
        rest.ring(FILE_LEASE_CHANGED, f"node-{WS_KEY}", **({"reason": reason} if reason else {}))
        await _until(lambda: rest.frames.empty())
        for _ in range(5):
            await asyncio.sleep(0)
        await service._release_mirror("chat-a", ending="idle")
    finally:
        stream.cancel()
    assert (WS_KEY in service._workspaces.keys()) is held


async def test_a_workspace_whose_hand_back_is_refused_says_nothing_on_a_chat_row(
    tmp_path: Path,
) -> None:
    """A refusal is said on the chat it belongs to. A workspace's shared tree
    has no chat row: asking the backend to record it on one was a 422."""
    rest = _StreamRest()
    service, _built = _member_service(tmp_path, _Custody(tmp_path), rest=rest)
    refused = FolderHandBackError(
        WS_KEY, "files/drives returned 404", transient=False, attempts=3, exhausted=True
    )

    await service._returns.not_handed_back(refused)
    await service._returns.not_handed_back(
        FolderHandBackError("chat-a", "refused", transient=False, attempts=3, exhausted=True)
    )

    assert rest.said == [("chat-a", "refused")]


# -- a hand-back that did not land -------------------------------------------------


class _OwingCustody(_Custody):
    """A custody whose first ``failing`` hand-backs meet a restarting API.
    As the real one does, it keeps the folder held and the hand-back owed,
    and a take of a folder it still holds hands the same one back, debt
    cleared."""

    def __init__(self, root: Path, *, failing: int = 1) -> None:
        super().__init__(root)
        self.failing = failing
        self.owed: set[str] = set()
        self.attempts: list[str] = []

    def take(self, chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any:
        if chat_id in self.held_keys:
            self.owed.discard(chat_id)
            return object()
        return super().take(chat_id, chat, instance=instance)

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self.attempts.append(chat_id)
        if self.failing:
            self.failing -= 1
            self.owed.add(chat_id)
            raise FolderHandBackError(
                chat_id, "connection refused", transient=True, attempts=0, exhausted=False
            )
        self.owed.discard(chat_id)
        return super().hand_back(chat_id, recover=recover, ending=ending, gone=gone)


async def test_a_workspace_whose_hand_back_did_not_land_is_kept_owed_until_a_member_wakes(
    tmp_path: Path,
) -> None:
    """The last member sleeps and the push meets a restarting API. The box
    still holds the folder, so it keeps beating its lease, and says the
    sandbox is asleep. A member that wakes takes the workspace back, debt and
    all; a retry while it runs leaves it alone; its own sleep hands it back."""
    custody = _OwingCustody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))

    assert await host.leave("chat-a", ending="idle") == "awake"
    assert custody.owed == {WS_KEY}
    assert host.keys() == [WS_KEY], "a lease the custody still holds is still beaten"
    assert host.state(WS_KEY) == "asleep"

    assert await host.join("chat-b", _member("chat-b")) is Joined.MEMBER
    assert custody.owed == set(), "waking a member takes the hand-back's debt back"
    assert await host.retry_owed() == []
    assert custody.attempts == [WS_KEY], "nothing is handed back from under a member"
    assert host.state(WS_KEY) == "awake"

    assert await host.leave("chat-b", ending="idle") == "asleep"
    assert custody.handed_back == [(WS_KEY, "idle")]
    assert host.keys() == []


async def test_an_owed_workspace_nobody_woke_is_handed_back_on_the_retry(tmp_path: Path) -> None:
    custody = _OwingCustody(tmp_path)
    released: list[str] = []
    host = _host(custody, released=released)
    await host.join("chat-a", _member("chat-a"))
    await host.leave("chat-a", ending="evicted")
    assert await host.sweep() == [], "the sweep leaves an owed workspace to the retry"

    assert await host.retry_owed() == [WS_KEY]

    assert custody.handed_back == [(WS_KEY, "evicted")], "the retry carries the sleep's reason"
    assert host.keys() == []
    assert await host.retry_owed() == []


async def test_a_hand_back_the_custody_gave_up_on_is_not_kept(tmp_path: Path) -> None:
    """A refusal the custody stopped retrying is no longer held there: the
    workspace is not kept as owed for a lease nobody will beat."""

    class _GaveUp(_Custody):
        def hand_back(self, chat_id: str, **_: Any) -> Any:
            self.held_keys.discard(chat_id)
            raise FolderHandBackError(
                chat_id, "forbidden", transient=False, attempts=3, exhausted=True
            )

    host = _host(_GaveUp(tmp_path))
    await host.join("chat-a", _member("chat-a"))
    assert await host.leave("chat-a", ending="idle") == "awake"
    assert host.keys() == []


# -- a member that never ran -------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(RuntimeError("the agent binary is gone"), id="start-failed"),
        pytest.param(ChatMirrorRefusedError("chat-a", "no_credit"), id="refused-verdict"),
        pytest.param(ChatMirrorRefusedError("chat-a", "timeout"), id="refused-transient"),
    ],
)
async def test_a_member_whose_start_fails_leaves_its_workspace(
    tmp_path: Path, failure: BaseException
) -> None:
    """A member whose agent never started holds nothing: the workspace it
    joined goes back at once instead of being beaten for the life of the box,
    which kept every sibling on another box refused it."""
    custody = _Custody(tmp_path)
    service, built = _member_service(tmp_path, custody, failing_starts={"chat-a": failure})

    await service._ensure_mirror("chat-a", _member("chat-a"))

    assert built["chat-a"].stopped
    assert service._workspaces.members(WS_KEY) == frozenset()
    assert (WS_KEY, None) in custody.handed_back
    assert service._workspaces.keys() == []
    assert WS_KEY not in service._workspaces.custody_keys(service._mirrors)


async def test_a_box_stopping_after_a_failed_start_hands_the_workspace_back(
    tmp_path: Path,
) -> None:
    custody = _Custody(tmp_path)
    service, _built = _member_service(
        tmp_path, custody, failing_starts={"chat-a": RuntimeError("no binary")}
    )
    await service._ensure_mirror("chat-b", _member("chat-b"))
    await service._ensure_mirror("chat-a", _member("chat-a"))

    await service.stop()

    assert service._workspaces.keys() == []
    assert custody.handed_back[-1] == (WS_KEY, DRAINED_ENDING)


@pytest.mark.parametrize("stopping", [False, True], ids=["sweep", "box-stopping"])
async def test_a_member_the_box_no_longer_serves_holds_nothing(
    tmp_path: Path, stopping: bool
) -> None:
    """Belt to the leave above: a member absent from what the box serves (a
    stop cut short before it left) does not keep the workspace up."""
    custody = _Custody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))

    if stopping:
        assert await host.put_away_every(ending="drained", served={"chat-b"}) == []
        assert await host.put_away_every(ending="drained", served=set()) == [WS_KEY]
    else:
        assert await host.sweep(served={"chat-b"}) == []
        assert await host.sweep(served=set()) == [WS_KEY]
    assert host.seat("chat-a") is None
    # Neither member left deleted: the tree is not treated as the workspace's deletion.
    assert custody.told[WS_KEY] == (True, False)


# -- another box holds the workspace -----------------------------------------------


async def test_a_member_whose_workspace_another_box_holds_is_told_so_once_per_holder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    custody = _Custody(tmp_path, busy=True)
    refusals = _Refusals()
    host = _host(custody, refusals=refusals)

    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.workspace_host"):
        for _ in range(TRANSIENT_LIMIT + 2):
            assert await host.join("chat-a", _member("chat-a")) is Joined.NOT_HERE

    assert [chat for chat, _ in refusals.said] == ["chat-a"]
    assert "another machine (another-box)" in refusals.said[0][1], "the banner names the holder"
    assert refusals.kinds == ["workspace_elsewhere"], "a reader is shown it as a wait"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "another-box" in warned[0].getMessage()

    custody.busy = False  # the other box let it go; this one takes it
    assert await host.join("chat-a", _member("chat-a")) is Joined.MEMBER
    await host.leave("chat-a")
    custody.busy = True  # and it is taken elsewhere again: said again once it outlasts its tries
    for _ in range(TRANSIENT_LIMIT):
        assert await host.join("chat-a", _member("chat-a")) is Joined.NOT_HERE
    assert len(refusals.said) == 2


async def test_a_workspace_the_previous_machine_is_handing_back_is_not_a_refusal(
    tmp_path: Path,
) -> None:
    """A wake or a switch: the machine that last held the workspace still has
    it for a few passes while it hands it back. That is a state the chat
    passes through, so nothing is said on the chat, and the take that lands
    afterwards starts the count afresh."""
    custody = _Custody(tmp_path, busy=True)
    refusals = _Refusals()
    host = _host(custody, refusals=refusals)

    for _ in range(TRANSIENT_LIMIT - 1):
        assert await host.join("chat-a", _member("chat-a")) is Joined.NOT_HERE
    custody.busy = False
    assert await host.join("chat-a", _member("chat-a")) is Joined.MEMBER
    await host.leave("chat-a")
    custody.busy = True
    for _ in range(TRANSIENT_LIMIT - 1):
        assert await host.join("chat-a", _member("chat-a")) is Joined.NOT_HERE

    assert refusals.said == []


# -- the lease lost while a member wakes ---------------------------------------------


async def test_a_member_waking_while_a_lost_lease_is_let_go_keeps_its_fresh_take(
    tmp_path: Path,
) -> None:
    """The lost lease's sandbox is still coming down when a member wakes. The
    custody lets go of the old lease before the member takes the folder
    afresh, never after: letting go after would hand back the new grant."""
    custody = _Custody(tmp_path)
    events: list[str] = []
    entered, unblock = asyncio.Event(), asyncio.Event()

    async def release(key: str) -> None:
        entered.set()
        await unblock.wait()

    original_take, original_back = custody.take, custody.hand_back

    def take(chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any:
        events.append(f"take {chat_id}")
        return original_take(chat_id, chat, instance=instance)

    def hand_back(chat_id: str, **kwargs: Any) -> Any:
        events.append(f"let go {chat_id}")
        return original_back(chat_id, **kwargs)

    custody.take, custody.hand_back = take, hand_back  # type: ignore[method-assign]
    host = WorkspaceHost(
        folders=custody,
        instance_of=lambda key: f"box-1:{key}",
        refuse=_Refusals(),
        clock=_monotonic,
        release_sandbox=release,
        in_thread=_inline,
    )
    await host.join("chat-a", _member("chat-a"))

    lost = asyncio.create_task(host.lost(WS_KEY))
    await entered.wait()
    joining = asyncio.create_task(host.join("chat-b", _member("chat-b")))
    for _ in range(5):
        await asyncio.sleep(0)
    unblock.set()

    assert await lost == {"chat-a"}
    assert await joining is Joined.MEMBER
    assert events == [f"take {WS_KEY}", f"let go {WS_KEY}", f"take {WS_KEY}"]
    assert custody.held(WS_KEY) is not None, "the member's fresh grant is still held"


async def test_a_custody_that_will_not_let_go_of_a_lost_lease_is_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    custody = _Custody(tmp_path)

    def refuse(chat_id: str, **_: Any) -> Any:
        raise OSError("disk gone")

    custody.hand_back = refuse  # type: ignore[method-assign]
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.workspace_host"):
        assert await host.lost(WS_KEY) == {"chat-a"}
    assert any("disk gone" in r.getMessage() for r in caplog.records)


# -- the per-lease beat --------------------------------------------------------------


class _PerLeaseCustody(_Custody):
    """A custody over an older backend: no batched beat, one beat per lease;
    the workspace's lease answers that it is somebody else's."""

    def beat(self, chat_id: str) -> bool:
        return chat_id != WS_KEY


async def test_a_lost_workspace_lease_on_the_per_lease_beat_stops_every_member(
    tmp_path: Path,
) -> None:
    custody = _PerLeaseCustody(tmp_path)
    service, built = _member_service(tmp_path, custody)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    await service._ensure_mirror("chat-b", _member("chat-b"))

    await service._beat_folders()

    assert built["chat-a"].stopped and built["chat-b"].stopped
    assert service._mirrors == {}
    assert service._workspaces.keys() == []


# -- whose deletion it is ------------------------------------------------------------


async def test_a_deleted_last_member_does_not_speak_for_one_that_only_slept(
    tmp_path: Path,
) -> None:
    """A slept, B is deleted last. The workspace was not deleted: A's work is
    in the shared tree, so a drive that answers nothing for the folder must
    not have it discarded."""
    custody = _Custody(tmp_path)
    host = _host(custody)
    await host.join("chat-a", _member("chat-a"))
    await host.join("chat-b", _member("chat-b"))
    await host.leave("chat-a", ending="idle")
    await host.leave("chat-b", gone=True)
    assert custody.told[WS_KEY] == (True, False)


# -- the box's own bookkeeping ---------------------------------------------------------


async def test_a_workspace_id_that_is_not_an_id_never_names_a_directory(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    refusals = _Refusals()
    host = _host(custody, refusals=refusals)

    joined = await host.join("chat-a", {**_member("chat-a"), "workspace_id": "../../etc"})

    assert joined is Joined.NOT_HERE
    assert custody.taken == []
    assert [chat for chat, _ in refusals.said] == ["chat-a"]


async def test_the_workspace_locks_go_with_the_workspaces(tmp_path: Path) -> None:
    custody = _Custody(tmp_path)
    host = _host(custody)
    for _ in range(3):
        await host.join("chat-a", _member("chat-a"))
        await host.leave("chat-a", ending="idle")
    assert host._locks == {}


async def test_the_service_drops_a_put_away_workspaces_custody_lock(tmp_path: Path) -> None:
    custody = _PerLeaseCustody(tmp_path)
    service, _built = _member_service(tmp_path, custody)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    custody.beat = lambda chat_id: True  # type: ignore[method-assign]
    await service._beat_folders()
    assert WS_KEY in service._folder_locks

    await service._release_mirror("chat-a", ending="idle")
    await service._upkeep_folders()

    assert WS_KEY not in service._folder_locks


# -- a box stopping with several workspaces ------------------------------------------


async def test_a_stopping_box_hands_each_workspace_back_on_its_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shared tree too large (or a drive too slow) to hand back within the
    budget costs its own workspace a clean hand-back and nothing else: the
    other is handed back beside it, and the stop ends on time."""
    monkeypatch.setattr("alkera_cli.cloud.service.STOP_RELEASE_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr("alkera_cli.cloud.service.STOP_TASK_GRACE_SECONDS", 0.2)
    other_ws = "8a7c1f2e-0000-4000-8000-000000000002"
    other_key = workspace_key(other_ws)
    custody = _Custody(tmp_path)
    stuck, go_on = threading.Event(), threading.Event()
    original = custody.hand_back

    def hand_back(chat_id: str, **kwargs: Any) -> Any:
        if chat_id == WS_KEY:
            stuck.set()
            go_on.wait(30)
        return original(chat_id, **kwargs)

    custody.hand_back = hand_back  # type: ignore[method-assign]
    service, _built = _member_service(tmp_path, custody)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    await service._ensure_mirror("chat-b", _member("chat-b", workspace_id=other_ws))
    # Somebody is typing into both shared trees: neither goes when its member sleeps.
    service._workspaces.note_used(WS_KEY)
    service._workspaces.note_used(other_key)

    try:
        await asyncio.wait_for(service.stop(), 15)
        assert stuck.is_set()
        assert (other_key, DRAINED_ENDING) in custody.handed_back
        assert f"cloud-workspace-release:{WS_KEY}" in service._abandoned
    finally:
        go_on.set()


# -- a workspace whose chats moved to another machine ----------------------------


async def test_a_moved_last_member_hands_the_workspace_back_whatever_it_left_running(
    tmp_path: Path,
) -> None:
    """A hold keeps the shared tree for a member that only slept here. A
    member bound to another machine takes its workspace with it: what it left
    running cannot hold the tree here, or the machine it moved to could never
    take it."""
    custody = _Custody(tmp_path)
    work = _Work()
    host = _host(custody, work=work)
    await host.join("chat-a", _member("chat-a"))
    work.running = LEFT_RUNNING

    assert await host.leave("chat-a", moved=True) == "asleep"
    assert custody.handed_back == [(WS_KEY, None)]
    assert host.keys() == []


@pytest.mark.parametrize(
    ("bound_to", "handed_back"),
    [
        pytest.param("box-2", True, id="bound-to-another-machine"),
        pytest.param(None, False, id="bound-to-no-machine"),
    ],
)
async def test_a_member_the_row_binds_elsewhere_takes_its_held_up_workspace_along(
    tmp_path: Path, bound_to: str | None, handed_back: bool
) -> None:
    custody = _Custody(tmp_path)
    work = _Work()
    service, built = _member_service(tmp_path, custody, sandbox_work=work)
    service._machine_id = "box-1"
    await service._ensure_mirror("chat-a", _member("chat-a", machine_id="box-1"))
    work.running = LEFT_RUNNING

    await service._take("chat-a", _member("chat-a", machine_id=bound_to), start_owed_turn=False)

    assert built["chat-a"].stopped
    assert ((WS_KEY, None) in custody.handed_back) is handed_back
    assert (service._workspaces.keys() == []) is handed_back


@pytest.mark.parametrize(
    ("still_served", "handed_back"),
    [
        pytest.param(False, True, id="no-member-left-on-the-box"),
        pytest.param(True, False, id="a-member-the-restart-keeps"),
    ],
)
async def test_a_restart_hands_back_a_workspace_none_of_its_chats_is_in(
    tmp_path: Path, still_served: bool, handed_back: bool
) -> None:
    """A restart in place keeps every chat it serves, and their workspace with
    them. A workspace no chat here is in (held up by what a member left
    running) comes back to nobody, so its lease goes back rather than keeping
    every other box off the tree until it lapses."""
    custody = _Custody(tmp_path)
    work = _Work()
    service, _built = _member_service(tmp_path, custody, sandbox_work=work, supervised=True)
    await service._ensure_mirror("chat-a", _member("chat-a"))
    work.running = LEFT_RUNNING
    if not still_served:
        await service._release_mirror("chat-a", ending="idle")
        assert service._workspaces.keys() == [WS_KEY]  # held up by the process

    service.begin_drain()
    assert service.restarting
    await service.stop()

    assert ((WS_KEY, DRAINED_ENDING) in custody.handed_back) is handed_back
