"""The box's notebooks: one engine per workspace, for the agents and the backend.

A box serves a notebook from the folder it holds: the agents of the chats it
runs edit and run notebooks there through their tools, and people in the
browser reach the same notebooks through the backend, which carries their
requests to the box on its machine channel. Both go to ONE engine per
workspace (per custody key: a workspace's ``ws:<id>``, or a chat on its own),
so a person and an agent share a kernel, a queue and a widget hub.

* **Where a workspace is.** :class:`BoxFolders` answers, for a custody key or
  for the lease node the backend names, the folder this box holds: its tree
  on disk (the root the agents and the engine see), the drive and lease, and
  the fence every write about it carries. :class:`HeldFolders` reads it from
  the box's folder custody.
* **The document.** Each workspace's engine edits its notebooks through the
  platform's live document (:class:`~alkera_cli.notebooks.store_loro.LoroDocumentStore`),
  each request under the fence of the folder's lease, so only the holder's
  edits land. A chat's agent edits name the chat, so the backend writes them
  as that agent for the chat's person.
* **The agents.** :meth:`BoxNotebooks.host_factory` is the harness's
  notebook host factory: the chat's agent reaches the engine of the workspace
  its session is a member of (its sandbox scope's tree).
* **The backend.** :meth:`BoxNotebooks.handle` answers a notebook
  ``machine.request`` (``run``, ``kernel``, ``comm``, ``frame_attach``,
  ``frame_detach``, ``table``, ``envs``, ``env_packages``, ``env_install``,
  ``snapshot``) by calling the engine, and every request gets one ``answer``
  event naming it, with the result or the error.
* **The kernel's events.** Each notebook a request or an agent opened gets a
  relay: an engine client of its own whose events are posted, in order, to
  the notebook's events route (:class:`KernelEventSink`), numbered per kernel.
  Events made while no kernel runs (a plan waiting for confirmation, an
  answer) are posted under the notebook's own engine channel id, reported
  ``absent``, so they never read as a kernel that runs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast, get_args

import httpx
from alkera_core.notebooks.limits import (
    INLINE_VALUE_MAX_BYTES,
    fit_event,
    json_bytes,
    map_bundles,
)
from alkera_core.schemas.realtime.machine import NOTEBOOK_FOLDER_NOT_HELD, NotebookMachineRequest
from alkera_notebook.actors import ActingFor
from alkera_notebook.engine.errors import EngineError, NotFoundError
from alkera_notebook.engine.models import (
    Actor,
    EnvAction,
    InspectQuery,
    ReadQuery,
    RunTarget,
)
from alkera_notebook.engine.session import env_failure_message
from alkera_notebook.envs import (
    EnvBuildCancelledError,
    EnvBuildError,
    EnvError,
    EnvNotFoundError,
)
from alkera_notebook.kernels.kernel import KernelStartError
from alkera_notebook.outputs.snapshot import NotebookPlace, store_blob
from alkera_notebook.outputs.state import TABLE_MIME
from alkera_notebook.rpc.frames import RpcError
from alkera_notebook.tools.engine_adapter import EngineHost, EnginePort, EngineWorkspace
from alkera_notebook.tools.port import ActorRef, NotebookHost, NotebookToolError
from alkera_notebook.tree_io import Tree
from pydantic import TypeAdapter

from alkera_cli.files.mount import fence_headers
from alkera_cli.harness.sandbox_layout import RUNTIME_STATE_SUBDIR
from alkera_cli.harness.sandbox_scope import SandboxScope, scope_of
from alkera_cli.notebooks.box_events import (
    POST_BATCH,
    EventOutbox,
    EventsNotDeliveredError,
    KernelEventSink,
    event_json,
)
from alkera_cli.notebooks.engine_host import WorkspaceTenancy, tenancy_of
from alkera_cli.notebooks.store_loro import (
    DocSignals,
    Located,
    LoroDocumentStore,
    chat_of_agent,
)

if TYPE_CHECKING:
    from alkera_notebook.document.store import DocumentStore
    from alkera_notebook.engine.engine import NotebookClient, NotebookEngine, NotebookSession

    from alkera_cli.cloud.folder import HeldFolder

logger = logging.getLogger(__name__)

#: How the box itself is named on the notebooks it relays: it runs nothing of
#: its own, but carries every person's widget messages and output frames.
BOX_ACTOR: Final = Actor(
    kind="system", id="box", display_name="Alkera box", can_edit=False, can_run=True
)
#: How long the kernel stays idle before the box posts the notebook's whole
#: state as a fresh ``snapshot``: the backend's view and a socket joining the
#: channel start from the latest snapshot, so one is posted each time the
#: kernel settles (a run drained, a new kernel up), and a burst of runs (a
#: slider dragged) is one snapshot, not one per run.
SNAPSHOT_SETTLE_SECONDS: Final = 1.0
#: How many request ids a box remembers, so a request delivered twice runs once.
REQUESTS_REMEMBERED: Final = 1024
#: How long a new notebook's file may take to reach the drive before its
#: creation is reported failed.
CREATE_VISIBLE_SECONDS: Final = 30.0
#: The engine channel's prefix: the id events made while no kernel runs are
#: posted under.
ENGINE_CHANNEL_PREFIX: Final = "engine-"

_TARGET: Final[TypeAdapter[Any]] = TypeAdapter(RunTarget)

KernelAction = Literal["status", "interrupt", "interrupt_all", "restart", "shutdown"]
#: The requests a box takes out of their notebook's order: an install runs
#: for minutes, and a widget message or a run must not wait behind it.
UNORDERED_OPS: Final = frozenset({"env_install", "env_action"})
#: The environment actions a person asks for through ``env_action`` (an
#: install has its own op): build it from its spec now, remove packages,
#: and cancel the build under way.
PERSON_ENV_ACTIONS: Final = {"build": "materialize", "remove": "remove", "cancel": "cancel"}
#: The kernel actions that overtake a notebook's waiting requests. Stopping
#: what runs is only worth anything if it does not queue behind the work it
#: is meant to stop (Jupyter gives interrupts a control channel of their own
#: for the same reason).
OVERTAKING_KERNEL_ACTIONS: Final = frozenset({"interrupt", "interrupt_all", "shutdown"})
#: What a request that needs the drive is refused with while the folder's
#: heartbeats are not landing.
FOLDER_PAUSED_MESSAGE: Final = (
    "This machine is reconnecting to the workspace's files. Try again in a moment."
)


#: What a request is refused with in the ordinary course: a decision the
#: engine made (a build that failed, a kernel that could not start, a bad
#: request). Logged as a refusal, without a traceback; anything else is the
#: box failing, logged with one.
EXPECTED_REFUSALS: Final[tuple[type[BaseException], ...]] = (
    EngineError,
    NotebookToolError,
    RpcError,
    EnvError,
    EnvNotFoundError,
    KernelStartError,
    LookupError,
    ValueError,
)


# ---------------------------------------------------------------------------
# Where a workspace's folder is
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoxFolder:
    """One folder this box holds, as its notebooks see it."""

    #: The custody key: a workspace's ``ws:<id>`` or a chat's id.
    key: str
    #: The tree agents and the engine see (the live root under the lease).
    root: Path
    drive_id: str
    lease_node_id: str
    #: The step from the leased folder down to ``root`` (``""`` when equal).
    step: str
    #: The fence every request about this folder's notebooks carries.
    headers: Mapping[str, str]
    #: The node id filed at a path under ``root``, or ``None`` (blocking).
    node_at: Callable[[str], str | None]
    #: The box holds this folder but its last heartbeats did not land, so it
    #: has stopped reading and writing the drive under the lease until one
    #: does. Its kernels and what they hold are untouched meanwhile.
    paused: bool = False

    def under_root(self, lease_path: str) -> str | None:
        """A path the drive spells from the leased folder, as a path under
        :attr:`root`; ``None`` when it is not under it or would leave it."""
        parts = PurePosixPath(lease_path.strip("/")).parts
        if not parts or any(p in ("", ".", "..") for p in parts):
            return None
        step = PurePosixPath(self.step.strip("/")).parts if self.step.strip("/") else ()
        if tuple(parts[: len(step)]) != tuple(step) or len(parts) == len(step):
            return None
        return str(PurePosixPath(*parts[len(step) :]))


class BoxFolders(Protocol):
    """The folders this box holds, by custody key and by lease node."""

    def by_key(self, key: str) -> BoxFolder | None: ...

    def by_lease(self, lease_node_id: str) -> BoxFolder | None: ...


class FolderCustody(Protocol):
    """What :class:`HeldFolders` reads of the box's folder custody
    (:class:`~alkera_cli.cloud.folder.ChatFolders`)."""

    def held(self, chat_id: str) -> HeldFolder | None: ...

    def held_by_lease_node(self, lease_node_id: str) -> HeldFolder | None: ...


FenceHeaders = Callable[[int, str], Mapping[str, str]]


class HeldFolders:
    """:class:`BoxFolders` over the box's folder custody. A folder counts once
    its live sync runs: that names the tree the chat works in and reads the
    drive's rows under the lease's fence. While the sync has fenced itself
    (heartbeats that did not land) the folder is still held, and reads as
    :attr:`BoxFolder.paused`: only custody letting go of it makes it gone."""

    def __init__(self, custody: FolderCustody, *, fence: FenceHeaders | None = None) -> None:
        self._custody = custody
        self._fence = fence or fence_headers

    def by_key(self, key: str) -> BoxFolder | None:
        held = self._custody.held(key)
        return None if held is None else self._folder(held)

    def by_lease(self, lease_node_id: str) -> BoxFolder | None:
        held = self._custody.held_by_lease_node(lease_node_id)
        return None if held is None else self._folder(held)

    def _folder(self, held: HeldFolder) -> BoxFolder | None:
        sync = held.live
        if sync is None:
            return None
        try:
            step = sync.root.relative_to(held.root).as_posix()
        except ValueError:
            return None
        api = sync.api

        def node_at(relative: str) -> str | None:
            return api.resolve([relative]).get(relative)

        record = held.record
        return BoxFolder(
            key=held.chat_id,
            root=sync.root,
            drive_id=record.drive_id,
            lease_node_id=record.node_id,
            step="" if step == "." else step,
            headers=dict(self._fence(record.epoch, record.instance_id)),
            node_at=node_at,
            paused=bool(sync.fenced),
        )


class FolderPausedError(EngineError):
    """The box holds the folder but is not reading or writing the drive
    under its lease just now. Never "not found": the notebook is still
    there, and whoever follows it tries again."""

    name = "folder_paused"


class FolderNotHeldError(LookupError):
    """A request names a leased folder this box does not hold: it has not
    taken the folder yet (a box just started or woken takes its folders a few
    seconds after its worker is ready), or it put the folder's chat to sleep.
    Answered ``NOTEBOOK_FOLDER_NOT_HELD`` and not remembered as served: the
    backend wakes a sleeping chat, and otherwise sends the same request
    again, which is served once the box holds the folder."""


# ---------------------------------------------------------------------------
# The document store each workspace's engine edits through
# ---------------------------------------------------------------------------


class StoreSource(Protocol):
    """Each workspace's document store (:class:`BoxStores` on a box)."""

    def store_for(self, tenancy: WorkspaceTenancy) -> DocumentStore: ...


class BoxStores:
    """One :class:`LoroDocumentStore` per workspace, each resolving notebook
    paths under the folder this box holds for it, under its fence.

    The file node at a path is remembered (per folder and lease), so each
    read of a notebook is not also a lookup of its path; the fence is read
    fresh every time. A store whose read answers not found forgets the path,
    so a file replaced at it is found again."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        folders: BoxFolders,
        signals: DocSignals,
        visible_within: float = CREATE_VISIBLE_SECONDS,
    ) -> None:
        self._http = http
        self._folders = folders
        self._signals = signals
        self._visible_within = visible_within
        self._stores: dict[str, LoroDocumentStore] = {}
        #: The file node at a path, per (custody key, lease node, path).
        self._nodes: dict[tuple[str, str, str], str] = {}

    def folder(self, key: str) -> BoxFolder:
        found = self._folders.by_key(key)
        if found is None:
            raise NotFoundError(f"this box does not hold the folder of {key}")
        if found.paused:
            raise FolderPausedError(FOLDER_PAUSED_MESSAGE)
        return found

    async def locate(self, key: str, path: str) -> Located:
        folder = self.folder(key)
        remembered = (key, folder.lease_node_id, path)
        node = self._nodes.get(remembered)
        if node is None:
            node = await asyncio.to_thread(folder.node_at, path)
            if node is None:
                raise NotFoundError(f"no notebook at {path}")
            self._nodes[remembered] = node
        return Located(drive_id=folder.drive_id, item_id=node, headers=folder.headers)

    def forget(self, key: str, path: str) -> None:
        """Drop the node remembered at ``path`` in ``key``'s folder."""
        for remembered in [k for k in self._nodes if k[0] == key and k[2] == path]:
            del self._nodes[remembered]

    def store_for(self, tenancy: WorkspaceTenancy) -> LoroDocumentStore:
        found = self._stores.get(tenancy.key)
        if found is not None:
            return found
        key = tenancy.key

        async def resolve(path: str) -> Located:
            return await self.locate(key, path)

        async def create_file(path: str, text: str) -> Located:
            return await self._create(key, path, text)

        async def remove_file(path: str) -> None:
            await self._remove(key, path)

        def forget(path: str) -> None:
            self.forget(key, path)

        store = LoroDocumentStore(
            http=self._http,
            resolve=resolve,
            signals=self._signals,
            create_file=create_file,
            remove_file=remove_file,
            names_agent_chat=True,
            forget=forget,
        )
        self._stores[key] = store
        return store

    async def _create(self, key: str, path: str, text: str) -> Located:
        """Write a new notebook's file into the held tree (the live sync takes
        it to the drive, as any file the agent writes) and wait for the
        drive's row."""
        folder = self.folder(key)
        await asyncio.to_thread(_write_new, folder.root, path, text)
        self.forget(key, path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._visible_within
        while True:
            node = await asyncio.to_thread(folder.node_at, path)
            if node is not None:
                return Located(drive_id=folder.drive_id, item_id=node, headers=folder.headers)
            if loop.time() >= deadline:
                raise NotFoundError(f"{path} did not reach the drive in time")
            await asyncio.sleep(0.25)

    async def _remove(self, key: str, path: str) -> None:
        """Take back a file :meth:`_create` wrote (its create was refused):
        remove it from the held tree, and wait for the live sync to take it
        off the drive."""
        folder = self.folder(key)
        await asyncio.to_thread(Tree(folder.root).unlink, path, missing_ok=True)
        self.forget(key, path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._visible_within
        while await asyncio.to_thread(folder.node_at, path) is not None:
            if loop.time() >= deadline:
                raise TimeoutError(f"{path} was not taken off the drive in time")
            await asyncio.sleep(0.25)


def _write_new(root: Path, path: str, text: str) -> None:
    """A new file at ``path`` under the held folder ``root``, its directories
    made on the way. Nothing below ``root`` is followed through a link: a
    member who points a folder at another member's tree must not make the
    worker, which can write there, create a file in it."""
    tree = Tree(root)
    parent = PurePosixPath(path).parent
    if parent.parts:
        tree.make_dirs(parent)
    with tree.create(path, mode=0o664) as handle:
        handle.write(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# One notebook's relay
# ---------------------------------------------------------------------------


class _Relay:
    """One open notebook on this box, as the backend sees it: the box's own
    engine client, whose events are posted in order; the clients of the
    people and agents whose requests the backend carried; the frames people
    attached."""

    def __init__(self, session: NotebookSession, where: Located, sink: KernelEventSink) -> None:
        self.session = session
        self.where = where
        self._sink = sink
        self.client: NotebookClient = session.attach(BOX_ACTOR)
        self._clients: dict[str, NotebookClient] = {}
        #: Events made while no kernel runs are posted under this id.
        self.engine_channel = _new_engine_channel()
        self._outbox = EventOutbox(sink, where, self.engine_channel, on_overflow=self._repair)
        self._pumps: list[asyncio.Task[None]] = []
        #: Whether the relay holds events it took from the queue and has not
        #: queued for posting yet.
        self._holding = False
        self._settled: asyncio.Task[None] | None = None
        self._task = asyncio.get_running_loop().create_task(self._pump())

    def client_for(self, requested_by: Mapping[str, Any] | None) -> NotebookClient:
        """The engine client a request is made through: its requester's own,
        so the run, the queue and the activity name who asked.

        A client is made once per requester and kept, so the name it carries
        is refreshed whenever a later request names them: the first request
        to make it may not have carried a name (a frame attach names only the
        frame, whose id names its owner), and every run after must still read
        as that person."""
        who = dict(requested_by or {})
        kind = who.get("kind")
        actor_id = str(who.get("id") or "")
        if kind not in ("person", "agent") or not actor_id:
            return self.client
        display_name = str(who.get("display_name") or "")[:255]
        acting_for = _acting_for(who.get("acting_for")) if kind == "agent" else None
        # Whether they may edit the notebook is the backend's decision, sent
        # with each request; never assumed (absent is no).
        can_edit = who.get("can_edit") is True
        found = self._clients.get(actor_id)
        if found is not None:
            _rename(found, display_name, acting_for, can_edit)
            return found
        found = self.session.attach(
            Actor(
                kind=kind,
                id=actor_id,
                display_name=display_name,
                can_edit=can_edit,
                can_run=True,
                acting_for=acting_for,
            )
        )
        self._clients[actor_id] = found
        # The box's own client relays what everyone is sent; what only
        # this requester is sent (its frames' widget messages) is
        # relayed from here, and the rest let go of as it comes.
        self._pumps.append(asyncio.get_running_loop().create_task(self._pump_frames(found)))
        return found

    def owner_of_frame(
        self, frame_id: str, requested_by: Mapping[str, Any] | None = None
    ) -> NotebookClient:
        """The client of the person a frame id was minted for (its first
        part is their user id): a frame is attached, messaged and detached
        through its owner's client, so the hub's ownership is theirs. The
        request's requester names the owner when it is them (or an agent
        acting for them), so the client carries their name."""
        head = frame_id.split(".", 1)[0]
        try:
            owner = uuid.UUID(hex=head)
        except ValueError:
            raise ValueError("the frame id names no owner") from None
        key = f"user:{owner}"
        return self.client_for(
            {"kind": "person", "id": key, "display_name": _name_of(requested_by, key)}
        )

    async def _pump_frames(self, client: NotebookClient) -> None:
        queue = client.queue
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                if event.type == "frame.message":
                    self.send([event_json(event)])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notebook %s: a frame relay stopped", self.where.item_id)

    def kernel_id(self) -> str | None:
        kernel = self.session.runtime.kernel
        return None if kernel is None else str(kernel.kernel_id)

    def send(
        self, events: Sequence[Mapping[str, Any]], *, urgent: bool = False
    ) -> asyncio.Future[None]:
        """Queue ``events`` in order, each under its kernel (or, with none
        running, the engine channel); the future resolves once they are
        posted. ``urgent`` (an answer) goes ahead of queued events."""
        running = self.kernel_id()
        return self._outbox.put(
            [
                (str(e.get("kernel_id") or running or self.engine_channel), self._out_of_line(e))
                for e in events
            ],
            urgent=urgent,
        )

    async def post(self, events: Sequence[Mapping[str, Any]], *, urgent: bool = False) -> None:
        """:meth:`send`, waiting until the events are posted."""
        await self.send(events, urgent=urgent)

    async def snapshot(self) -> None:
        """The notebook's whole state now, for people joining the channel."""
        view = await self.client.read(ReadQuery())
        await self.post([{"type": "snapshot", "view": view.model_dump(mode="json"), "frames": {}}])

    def _out_of_line(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """``event`` with each output value too large to travel inline stored
        beside the notebook (the snapshot's own blob store, which the backend
        serves by hash) and carried as a reference instead."""
        place = getattr(getattr(self.session, "runtime", None), "place", None)
        if not isinstance(place, NotebookPlace):
            return dict(event)

        def move(bundle: Mapping[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for mime, value in bundle.items():
                if mime != "text/plain" and json_bytes(value) > INLINE_VALUE_MAX_BYTES:
                    try:
                        out[mime] = store_blob(place, mime, value)
                        continue
                    except OSError:
                        logger.warning("notebook %s: an output was not stored", self.where.item_id)
                out[mime] = value
            return out

        moved: dict[str, Any] = map_bundles(event, move)
        return moved

    def _repair(self) -> None:
        """Events were let go: the notebook's whole state stands in for them."""
        self._settle_snapshot()

    def _settle_snapshot(self) -> None:
        """Post a snapshot once the kernel has stayed idle a moment."""
        if self._settled is None or self._settled.done():
            self._settled = asyncio.get_running_loop().create_task(self._snapshot_when_settled())

    async def _snapshot_when_settled(self) -> None:
        """The notebook's whole state, posted once it is exactly the state the
        events already queued for posting leave it in: the kernel idle,
        nothing waiting in the relay, and no event published while the view
        was read (else it is read again a moment later). A run starting
        meanwhile ends the wait; its own idle asks again."""
        runtime = getattr(self.session, "runtime", None)
        try:
            while True:
                await asyncio.sleep(SNAPSHOT_SETTLE_SECONDS)
                if getattr(runtime, "kernel_state", "idle") != "idle":
                    return
                if self._holding or len(self.client.queue):
                    continue
                hub = getattr(runtime, "hub", None)
                before = getattr(hub, "seq", None)
                view = await self.client.read(ReadQuery())
                if self._holding or len(self.client.queue) or getattr(hub, "seq", None) != before:
                    continue
                if getattr(runtime, "kernel_state", "idle") != "idle":
                    return
                self.send(
                    [{"type": "snapshot", "view": view.model_dump(mode="json"), "frames": {}}]
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notebook %s: a settled snapshot was not posted", self.where.item_id)

    async def _pump(self) -> None:
        queue = self.client.queue
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                self._holding = True
                batch = [event]
                while len(queue) and len(batch) < POST_BATCH:
                    more = await queue.get()
                    if more is None:
                        break
                    batch.append(more)
                out: list[dict[str, Any]] = []
                for one in batch:
                    if one.type == "resync":
                        view = getattr(one, "view", None)
                        if view is None:
                            view = await self.client.read(ReadQuery())
                        out.append(
                            {
                                "type": "snapshot",
                                "view": view.model_dump(mode="json"),
                                "frames": {},
                            }
                        )
                    else:
                        out.append(event_json(one))
                # Queued, not waited for: what the kernel makes meanwhile
                # joins the next post instead of waiting for its own.
                self.send(out)
                self._holding = False
                if any(
                    one.type == "kernel.state" and getattr(one, "state", None) == "idle"
                    for one in batch
                ):
                    self._settle_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notebook %s: its event relay stopped", self.where.item_id)

    async def close(self) -> None:
        settled = [self._settled] if self._settled is not None else []
        for task in [self._task, *self._pumps, *settled]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._outbox.close()
        for client in [self.client, *self._clients.values()]:
            with contextlib.suppress(Exception):
                await client.detach()


# ---------------------------------------------------------------------------
# The box's notebooks
# ---------------------------------------------------------------------------

EngineFor = Callable[[WorkspaceTenancy], "NotebookEngine"]
#: The person a chat's agent acts for (the chat's own person), by chat id;
#: ``None`` when the box does not know it.
PersonOf = Callable[[str], ActingFor | None]
TenancyFor = Callable[[str, Path], WorkspaceTenancy]
ScopeOf = Callable[[str], SandboxScope]


def box_tenancy(key: str, root: Path) -> WorkspaceTenancy:
    """A held folder's tenancy: a workspace's environments where every
    member's agent finds them, a chat on its own its runtime state's."""
    if key.startswith("ws:"):
        return tenancy_of(key, root)
    return tenancy_of(key, root, state_dir=root.parent / RUNTIME_STATE_SUBDIR)


class BoxNotebooks:
    """See the module docstring."""

    def __init__(
        self,
        *,
        folders: BoxFolders,
        engine_for: EngineFor,
        events: KernelEventSink,
        locate: Callable[[str, str], Awaitable[Located]],
        tenancy_for: TenancyFor = box_tenancy,
        scope: ScopeOf = scope_of,
        person_of: PersonOf | None = None,
    ) -> None:
        self._folders = folders
        self._person_of = person_of
        self._engine_for = engine_for
        self._events = events
        self._locate = locate
        self._tenancy_for = tenancy_for
        self._scope = scope
        self._workspaces: dict[str, EngineWorkspace] = {}
        self._relays: dict[tuple[str, str], _Relay] = {}
        self._relay_lock = asyncio.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._order: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        #: Per notebook: the channel and last number of answers posted with
        #: no notebook open.
        self._answer_channels: dict[str, tuple[str, int]] = {}

    # -- the engines ------------------------------------------------------------

    def workspace(self, folder: BoxFolder) -> EngineWorkspace:
        """The engine workspace of ``folder``'s custody key, made once."""
        found = self._workspaces.get(folder.key)
        if found is None:
            tenancy = self._tenancy_for(folder.key, folder.root)
            found = EngineWorkspace(folder.root, self._engine_for(tenancy))
            self._workspaces[folder.key] = found
        return found

    def use_people(self, person_of: PersonOf) -> None:
        """Where the person each chat's agent acts for is found."""
        self._person_of = person_of

    def key_of(self, actor: ActorRef) -> str | None:
        """The custody key a chat's agent works under: its workspace's when
        its session is a member, its own chat's otherwise."""
        chat = chat_of_agent(actor)
        return None if chat is None else self._scope(chat).tree

    def host_factory(self, root: Path, actor: ActorRef) -> NotebookHost:
        """The harness's notebook host factory on a box (see the module
        docstring). A chat whose folder this box does not hold, or whose tree
        is not the folder's, has no notebooks here."""
        key = self.key_of(actor)
        folder = None if key is None else self._folders.by_key(key)
        if folder is None:
            raise NotebookToolError(
                "unavailable", "This chat's folder is not held on this box, so it has no notebooks."
            )
        if folder.paused:
            raise NotebookToolError("unavailable", FOLDER_PAUSED_MESSAGE)
        if _real(folder.root) != _real(root):
            raise NotebookToolError(
                "unavailable", "This chat works outside its workspace's folder on this box."
            )
        chat = chat_of_agent(actor)
        if actor.acting_for is None and chat is not None and self._person_of is not None:
            # A chat's agent acts for the chat's person: its runs and edits
            # read "Alkera agent for <name>", never a bare agent.
            actor = actor.model_copy(update={"acting_for": self._person_of(chat)})
        return _RelayedHost(self, folder, self.workspace(folder), actor)

    # -- relays -------------------------------------------------------------------

    async def relay(self, folder: BoxFolder, path: str, where: Located | None = None) -> _Relay:
        """The relay of the notebook at ``path`` in ``folder``, opened once."""
        key = (folder.key, path)
        async with self._relay_lock:
            found = self._relays.get(key)
            if found is not None:
                return found
            session = await self.workspace(folder).engine.open(path)
            located = where or await self._locate(folder.key, path)
            found = _Relay(session, located, self._events)
            self._relays[key] = found
        await found.snapshot()
        return found

    # -- the backend's requests ----------------------------------------------------

    def dispatch(self, request: NotebookMachineRequest) -> asyncio.Task[None]:
        """Start serving ``request`` off the caller's path (the socket's
        reader). A notebook's requests are served in the order they arrived,
        but an install runs beside them (:data:`UNORDERED_OPS`)."""
        lock = None
        if not _overtakes(request):
            lock = self._order.setdefault(str(request.item_id), asyncio.Lock())

        async def serve() -> None:
            if lock is None:
                served = await self._serve(request)
            else:
                async with lock:
                    served = await self._serve(request)
            # The answer is posted with the order let go of: a post the
            # backend is slow to take must not hold up the next request.
            if served is not None:
                await self._answer(request, *served)

        task = asyncio.get_running_loop().create_task(serve())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def handle(self, request: NotebookMachineRequest) -> None:
        """Do what ``request`` asks and post its ``answer`` event."""
        served = await self._serve(request)
        if served is not None:
            await self._answer(request, *served)

    async def _serve(
        self, request: NotebookMachineRequest
    ) -> tuple[_Relay | None, Located, dict[str, Any]] | None:
        """Do what ``request`` asks: the relay it was served through, where
        its notebook is and its ``answer`` event; ``None`` for a request
        already served."""
        request_id = str(request.request_id)
        if request_id in self._seen:
            return None
        self._seen[request_id] = None
        while len(self._seen) > REQUESTS_REMEMBERED:
            self._seen.popitem(last=False)
        where = Located(drive_id=str(request.drive_id), item_id=str(request.item_id))
        relay: _Relay | None = None
        try:
            folder, path = self._find(request)
            where = Located(
                drive_id=str(request.drive_id), item_id=str(request.item_id), headers=folder.headers
            )
            relay = await self.relay(folder, path, where)
            result = await self._do(relay, request.op, dict(request.body))
            answer: dict[str, Any] = {"type": "answer", "request_id": request_id, "result": result}
        except Exception as exc:
            error = _error_of(exc)
            answer = {"type": "answer", "request_id": request_id, "error": error}
            if isinstance(exc, FolderNotHeldError):
                # Served again when the platform sends it again.
                self._seen.pop(request_id, None)
            if isinstance(exc, EXPECTED_REFUSALS):
                logger.info(
                    "notebook request %s (%s) refused: %s: %s",
                    request_id,
                    request.op,
                    error["code"],
                    error["message"],
                )
            else:
                logger.exception("notebook request %s (%s) failed", request_id, request.op)
        return relay, where, answer

    async def _answer(
        self,
        request: NotebookMachineRequest,
        relay: _Relay | None,
        where: Located,
        answer: dict[str, Any],
    ) -> None:
        if relay is not None:
            await relay.post([answer], urgent=True)
        else:
            channel, seq = self._answer_seq(str(request.item_id))
            try:
                # Fitted as an outbox fits what it queues: strict JSON, so
                # the post is never refused for a value it cannot encode.
                await self._events.post(
                    where,
                    kernel_id=channel,
                    state="absent",
                    events=[fit_event({**answer, "seq": seq})],
                )
            except EventsNotDeliveredError as exc:
                # No notebook is open to queue it in; the backend sends the
                # request again while it waits, and that one is answered.
                logger.warning(
                    "notebook request %s: its answer was not delivered (%s)",
                    request.request_id,
                    exc,
                )

    def _answer_seq(self, item_id: str) -> tuple[str, int]:
        """The channel and next number an answer posted with no notebook open
        goes under. Each kernel's numbers count up from one (the backend keeps
        them in an int32 column), so the channel is this process's own and a
        restarted box never posts under numbers an earlier one used."""
        channel, seq = self._answer_channels.get(item_id) or (_new_engine_channel(), 0)
        self._answer_channels[item_id] = (channel, seq + 1)
        while len(self._answer_channels) > REQUESTS_REMEMBERED:
            self._answer_channels.pop(next(iter(self._answer_channels)))
        return channel, seq + 1

    def _find(self, request: NotebookMachineRequest) -> tuple[BoxFolder, str]:
        if request.lease_node_id is None or request.path is None:
            raise LookupError("the request does not say where the notebook is")
        # A paused folder is still found: what needs the drive refuses on
        # its own, and what does not (stopping a kernel) still happens.
        folder = self._folders.by_lease(str(request.lease_node_id))
        if folder is None:
            raise FolderNotHeldError("this box does not hold the notebook's folder")
        path = folder.under_root(request.path)
        if path is None:
            raise LookupError("the notebook is not in the tree this box works in")
        return folder, path

    async def _do(self, relay: _Relay, op: str, body: dict[str, Any]) -> dict[str, Any]:
        handler = _OPS.get(op)
        if handler is None:
            raise LookupError(f"this box does not serve {op}")
        return await handler(relay, body)

    async def put_away(self, key: str) -> None:
        """The workspace ``key`` was put away: its notebooks stop being
        relayed and its engine is closed (a new one is made on next use)."""
        for relay_key in [k for k in self._relays if k[0] == key]:
            await self._relays.pop(relay_key).close()
        workspace = self._workspaces.pop(key, None)
        if workspace is not None:
            with contextlib.suppress(Exception):
                await workspace.close()

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for relay in list(self._relays.values()):
            await relay.close()
        self._relays.clear()
        for workspace in list(self._workspaces.values()):
            with contextlib.suppress(Exception):
                await workspace.close()
        self._workspaces.clear()


def _overtakes(request: NotebookMachineRequest) -> bool:
    """Whether ``request`` is served at once, not in its notebook's order."""
    if request.op in UNORDERED_OPS:
        return True
    return request.op == "kernel" and request.body.get("action") in OVERTAKING_KERNEL_ACTIONS


def _new_engine_channel() -> str:
    """A kernel id for events made while no kernel runs, unique to its poster."""
    return f"{ENGINE_CHANNEL_PREFIX}{uuid.uuid4().hex[:24]}"


def _rename(
    client: NotebookClient, display_name: str, acting_for: ActingFor | None, can_edit: bool
) -> None:
    """Carry a requester's name (and the person an agent acts for) as a
    later request names them, and their edit right as the backend decided it
    for this request; a request naming nobody changes no name."""
    actor = client.actor
    update: dict[str, Any] = {}
    if can_edit != actor.can_edit:
        update["can_edit"] = can_edit
    if display_name and display_name != actor.display_name:
        update["display_name"] = display_name
    if acting_for is not None and acting_for != actor.acting_for:
        update["acting_for"] = acting_for
    if update:
        client.actor = actor.model_copy(update=update)


def _name_of(requested_by: Mapping[str, Any] | None, person_id: str) -> str:
    """The display name a request gives the person ``person_id``: theirs when
    they asked, the acted-for person's when an agent asked for them."""
    who = requested_by or {}
    if who.get("kind") == "person" and str(who.get("id") or "") == person_id:
        return str(who.get("display_name") or "")
    acting = _acting_for(who.get("acting_for")) if who.get("kind") == "agent" else None
    if acting is not None and acting.id == person_id:
        return acting.display_name
    return ""


def _acting_for(raw: object) -> ActingFor | None:
    """The person a requester acts for, as the backend's request names them."""
    if not isinstance(raw, Mapping) or not raw.get("id"):
        return None
    return ActingFor(id=str(raw["id"]), display_name=str(raw.get("display_name") or "")[:255])


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _error_of(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, RpcError):
        # The kernel's own refusal, by its name (``inspect.unknown_column``).
        return {"code": exc.name, "message": exc.message[:500]}
    if isinstance(exc, NotebookToolError):
        return {"code": exc.code, "message": str(exc)}
    if isinstance(exc, EngineError):
        return {"code": exc.name, "message": exc.message}
    if isinstance(exc, KernelStartError):
        # A kernel that could not start, by the engine's reason for it
        # (``env_build_failed``, ``env_unavailable``).
        return {"code": exc.reason, "message": exc.message[:500]}
    if isinstance(exc, EnvBuildCancelledError):
        return {"code": "env_build_cancelled", "message": "The build was cancelled."}
    if isinstance(exc, EnvBuildError):
        return {"code": "env_build_failed", "message": env_failure_message(exc)[:500]}
    if isinstance(exc, (EnvError, EnvNotFoundError)) and not isinstance(exc, ValueError):
        return {"code": "env_unavailable", "message": str(exc)[:500]}
    if isinstance(exc, FolderNotHeldError):
        return {"code": NOTEBOOK_FOLDER_NOT_HELD, "message": str(exc)}
    if isinstance(exc, LookupError):
        return {"code": "not_found", "message": str(exc.args[0] if exc.args else exc)}
    if isinstance(exc, ValueError):
        return {"code": "invalid_request", "message": str(exc)[:500]}
    status = _refused_status(exc)
    if status is not None:
        # The box could not reach the notebook's folder or document (a
        # credential it lost, a lease it no longer holds): said, never silent.
        return {
            "code": "box_refused",
            "message": f"the platform refused the box (HTTP {status}) about this notebook",
        }
    return {"code": "box_error", "message": "the box could not serve this request"}


def _refused_status(exc: BaseException) -> int | None:
    """The HTTP status the platform refused one of the box's own requests
    with, from httpx's error or the SDK's."""
    from alkera_sdk.client import AlkeraHTTPError

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, AlkeraHTTPError):
        return exc.status
    return None


class _RelayedHost(EngineHost):
    """The agent's view of a workspace's notebooks on a box: every notebook
    it opens or creates is relayed, so people watching see what it runs."""

    def __init__(
        self, box: BoxNotebooks, folder: BoxFolder, workspace: EngineWorkspace, actor: ActorRef
    ) -> None:
        super().__init__(workspace, actor)
        self._box = box
        self._folder = folder

    async def open(self, path: str) -> EnginePort:
        port = await super().open(path)
        await self._follow(port.path)
        return port

    async def create(self, path: str, cells: Sequence[Any], settings: dict[str, Any]) -> EnginePort:
        port = await super().create(path, cells, settings)
        await self._follow(port.path)
        return port

    async def _follow(self, path: str) -> None:
        try:
            await self._box.relay(self._folder, path)
        except Exception:
            # The agent's work goes on; people see it once a request opens it.
            logger.warning("notebook %s: not relayed to the backend", path, exc_info=True)


# -- the operations ---------------------------------------------------------------


async def _run(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    target = _TARGET.validate_python(body.get("target"))
    client = relay.client_for(body.get("requested_by"))
    handle = await client.run(
        target,
        frontier=body.get("at_token"),
        confirm_expensive=bool(body.get("confirm_expensive")),
        run_id=str(body["run_id"]) if body.get("run_id") else None,
    )
    # The engine's whole answer: the backend reads the status and the reason
    # (a refusal's, or the run a coalesced request joined) from it.
    return handle.info.model_dump(mode="json")


async def _kernel(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")
    if action not in get_args(KernelAction):
        raise ValueError(f"unknown kernel action {action!r}")
    client = relay.client_for(body.get("requested_by"))
    info = await client.kernel(cast(KernelAction, action))
    return info.model_dump(mode="json")


async def _outputs_clear(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    cell_ids = body.get("cell_ids")
    if cell_ids is not None and not (
        isinstance(cell_ids, list) and all(isinstance(c, str) for c in cell_ids)
    ):
        raise ValueError("cell_ids must be a list of cell ids")
    client = relay.client_for(body.get("requested_by"))
    return {"cleared": await client.clear_outputs(cell_ids)}


async def _comm(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    buffers = [base64.b64decode(b, validate=True) for b in body.get("buffers") or []]
    frame_id = str(body["frame_id"])
    await relay.owner_of_frame(frame_id, body.get("requested_by")).comm_send(
        str(body["comm_id"]),
        str(body["msg_id"]),
        dict(body.get("content") or {}),
        buffers,
        frame_id=frame_id,
    )
    return {}


async def _frame_attach(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    frame_id = str(body["frame_id"])
    attached = relay.owner_of_frame(frame_id, body.get("requested_by")).attach_frame(
        frame_id=frame_id,
        model_ids=list(body.get("model_ids") or []),
        output_id=body.get("output_id"),
    )
    opens = []
    for message in attached.opens:
        one = dict(message.message)
        if message.buffers:
            one["buffers"] = [base64.b64encode(bytes(b)).decode("ascii") for b in message.buffers]
        opens.append(one)
    return {"frame_id": attached.frame_id, "opens": opens}


async def _frame_detach(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    frame_id = body.get("frame_id")
    prefix = body.get("prefix")
    frames = relay.session.runtime.frames
    if isinstance(frame_id, str):
        named = [frame_id]
    elif isinstance(prefix, str) and prefix:
        named = [f for f in frames if f.startswith(prefix)]
    else:
        raise ValueError("a detach names a frame or a prefix")
    for one in named:
        relay.owner_of_frame(one, body.get("requested_by")).detach_frame(one)
    return {"detached": len(named)}


#: Why a table page cannot be read: the kernel no longer holds the frame the
#: cell showed (it was restarted or stopped since), and the output saved with
#: the notebook holds only its first rows.
TABLE_NOT_HELD: Final = "Run the cell again to page this table."


def _shown_table(relay: _Relay, cell_id: str) -> Mapping[str, Any]:
    """The table output a cell shows last, with where it can be paged from
    (its ``source.name``: a global, or the handle the kernel keeps a shown
    frame under)."""
    outputs = relay.session.runtime.outputs.get(cell_id)
    for bundle in [] if outputs is None else reversed(outputs.bundles):
        table = bundle.get(TABLE_MIME)
        source = table.get("source") if isinstance(table, Mapping) else None
        name = source.get("name") if isinstance(source, Mapping) else None
        if isinstance(table, Mapping) and isinstance(name, str) and name:
            return table
    raise NotFoundError(f"cell {cell_id} shows no table that can be paged")


def _saved_page(table: Mapping[str, Any], offset: int, limit: int) -> dict[str, Any] | None:
    """A page cut from the output's own page, when that holds every row of
    the table: the kernel made its rows as it makes every page's, so the cut
    is the page the kernel would answer, with no kernel needed. ``None`` when
    the output holds only the first rows."""
    rows, total, schema = table.get("rows"), table.get("total_rows"), table.get("schema")
    if not isinstance(rows, list) or not isinstance(total, int) or not isinstance(schema, list):
        return None
    if int(table.get("offset") or 0) != 0 or len(rows) < total:
        return None
    return {
        "schema": schema,
        "rows": rows[offset : offset + limit],
        "total_rows": total,
        "offset": offset,
    }


async def _table(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    """A table page (``{schema, rows, total_rows, offset}``): the kernel's
    own, passed on as it is."""
    table = _shown_table(relay, str(body["cell_id"]))
    offset, limit = int(body.get("offset") or 0), int(body.get("limit") or 50)
    sort, filter_sql = list(body.get("sort") or []) or None, body.get("filter_sql")
    if sort is None and filter_sql is None:
        saved = _saved_page(table, offset, limit)
        if saved is not None:
            return saved
    if relay.session.runtime.kernel is None:
        raise NotebookToolError("table_not_held", TABLE_NOT_HELD)
    try:
        result = await relay.client.inspect(
            InspectQuery(
                what="frame",
                name=str(table["source"]["name"]),
                offset=offset,
                limit=limit,
                sort=sort,
                filter_sql=filter_sql,
            )
        )
    except RpcError as exc:
        if exc.name == "inspect.unknown_name":
            raise NotebookToolError("table_not_held", TABLE_NOT_HELD) from exc
        raise
    if result.table is None:
        raise NotebookToolError("table_unreadable", "The kernel answered no table page.")
    return dict(result.table)


async def _envs(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    listing = await relay.client.envs()
    return listing.model_dump(mode="json")


async def _env_packages(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    found = await relay.client.env_packages(str(body["env_id"]))
    # The engine's model whole: the backend validates the answer as it.
    return found.model_dump(mode="json")


async def _env_install(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    client = relay.client_for(body.get("requested_by"))
    packages = [str(p) for p in body.get("packages") or []]
    result = await client.env(EnvAction(action="install", packages=packages))
    return result.model_dump(mode="json")


async def _env_action(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    action = PERSON_ENV_ACTIONS.get(str(body.get("action")))
    if action is None:
        raise ValueError(f"unknown environment action {body.get('action')!r}")
    client = relay.client_for(body.get("requested_by"))
    packages = [str(p) for p in body.get("packages") or []]
    result = await client.env(EnvAction(action=action, packages=packages))  # type: ignore[arg-type]
    return result.model_dump(mode="json")


async def _snapshot(relay: _Relay, body: dict[str, Any]) -> dict[str, Any]:
    await relay.snapshot()
    return {}


_OPS: Final[dict[str, Callable[[_Relay, dict[str, Any]], Awaitable[dict[str, Any]]]]] = {
    "run": _run,
    "kernel": _kernel,
    "outputs_clear": _outputs_clear,
    "comm": _comm,
    "frame_attach": _frame_attach,
    "frame_detach": _frame_detach,
    "table": _table,
    "envs": _envs,
    "env_packages": _env_packages,
    "env_install": _env_install,
    "env_action": _env_action,
    "snapshot": _snapshot,
}

#: The request ops a box answers.
SERVED_OPS: Final = frozenset(_OPS)

__all__ = [
    "BOX_ACTOR",
    "SERVED_OPS",
    "BoxFolder",
    "BoxFolders",
    "BoxNotebooks",
    "BoxStores",
    "HeldFolders",
    "StoreSource",
    "box_tenancy",
]
