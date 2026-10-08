"""The notebook routes' logic: who is asking, what they may do, and doing it.

Every route names the notebook by its drive and node (``{drive_id}/{item_id}``)
and is decided by the Files policy first, exactly as the drive decides the
file: a stranger, a node in another org and a missing node are the same
opaque 404. On top of that:

* **Document ops** (the agent and box peer route) take Files WRITE, and the
  write must be one that could land: a box names the lease it holds on the
  notebook's folder and passes its fence (it writes as that lease's machine);
  anyone else writes only while no lease holds the folder or the holder's
  lease takes inbound writes. The batch is applied on a Loro peer by the CRDT
  lane (``CrdtDocs.notebooks``); the cells it touched are recorded as edited
  by the writer, and the writer is told who else is in those cells.
* **Runs, kernel actions, widget messages and installs** act through the
  kernel, so they are decided by the ``notebook.run`` policy through
  :func:`backend.authz.enforce` (a decision row either way) and then carried
  to the box by the :class:`NotebookTransport`.
* **Reads** (the view, the activity digest, output blobs and widget assets)
  take Files READ.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from alkera_core.authz import ActingContext, Action, Decision, ResourceType
from alkera_core.authz.resource import Resource
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.ids import NodeId
from alkera_core.models import User
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks import edits as edit_log
from alkera_core.notebooks.channel import NbCommFrame
from alkera_core.notebooks.models import NotebookRun
from alkera_core.notebooks.runs import (
    FOLDER_NOT_HELD,
    NOT_SENT,
    announce_platform_events,
    end_runs,
    nb_channel,
)
from alkera_core.notebooks.schemas import (
    Activity,
    ActivityItem,
    FrameAttached,
    FrameAttachRequest,
    RebaseResult,
    RunAccepted,
    RunRequest,
    TablePage,
)
from alkera_core.schemas.realtime import CrdtEphemeralPayload, DocEnvelope, encode_b64
from alkera_core.schemas.realtime.machine import NotebookRequestOp
from alkera_notebook.document.editing import (
    CARET_UNCONFIRMED_FOR,
    EDIT_WITHOUT_CARET_FOR,
    FocusClaim,
    editing_now,
    holds_for,
)
from alkera_notebook.engine.models import (
    CellAfterOp,
    CellNotice,
    EnvListing,
    EnvPackages,
    KernelInfo,
    NotebookOpsResult,
    NotebookView,
    Presence,
    Settings,
)
from alkera_notebook.outputs import InlineImage, inline_image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt import CrdtError, DocRef, NotebookOpError, file_name, name_text
from backend.services.files import (
    FilesContext,
    FolderHolder,
    authorized_node,
    build_files_context,
    folder_holder,
)
from backend.services.notebooks import frames, names, views
from backend.services.notebooks.callers import (
    Caller,
    KernelScope,
    Target,
    Where,
    agent_chat_in_workspace,
    is_notebook,
    lease_admits_writes,
    runner_of,
)
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.errors import (
    CommRefusedError,
    KernelAnswerError,
    KernelSilentError,
    NoMachineError,
    NotebookUnavailableError,
    OpRefusedError,
)
from backend.services.notebooks.feed import NotebookFeed, current_kernel
from backend.services.notebooks.runs import end_unanswered
from backend.services.notebooks.stored import blob_node
from backend.services.notebooks.transport import KernelHost, NotebookTransport
from backend.services.realtime import EPHEMERAL_ENTITY, EphemeralTooLargeError

#: A notebook's file name ends with this.
#: How recent another actor's edit or caret must be to be named in a notice.
NOTICE_WINDOW: Final = timedelta(seconds=15)
#: How long the agent's caret stays at the end of its last edit.
AGENT_CARET_SECONDS: Final = 10.0
#: How long a run waits for the document to include the requester's frontier.
FRONTIER_WAIT_SECONDS: Final = 2.0

#: What the decision rows of a widget message from the notebook channel
#: record in place of an HTTP verb (their path is the channel).
SOCKET_METHOD: Final = "NB_CHANNEL_COMM"
#: How the read of the caller's rung on the kernel's folder is recorded.
SCOPE_METHOD: Final = "NOTEBOOK_RUN_SCOPE"
#: The fields of a channel widget message the box is sent, as POST
#: ``.../comm`` sends them.
_COMM_KEYS: Final = frozenset({"frame_id", "comm_id", "msg_id", "content", "buffers"})


# -- the CRDT lane's notebook facade, as this module uses it ---------------------


class NotebookAppliedLike(Protocol):
    token: str
    repeat: bool
    cells: list[dict[str, Any]]
    created: list[str]
    touched: list[str]
    notices: list[dict[str, Any]]
    caret: bytes | None
    peer: int


class NotebookDocViewLike(Protocol):
    token: str
    covered: bool
    view: dict[str, Any]


class NotebookDocs(Protocol):
    async def apply(
        self,
        ref: DocRef,
        *,
        ops: list[dict[str, Any]],
        base_token: str | None,
        submit_id: str | None,
        agent_id: str | None,
        author: User | None,
        actor_key: str | None = None,
    ) -> NotebookAppliedLike: ...

    async def view(
        self, ref: DocRef, *, frontier: str | None = None, wait_seconds: float = 0.0
    ) -> NotebookDocViewLike: ...

    def graph_at(self, ref: DocRef, token: str) -> dict[str, Any] | None: ...

    async def rebase_update(
        self, ref: DocRef, *, epoch: int, update: bytes, author: User
    ) -> str: ...

    def holder_wrote(self, ref: DocRef) -> None:
        """The machine holding the notebook's folder wrote to it: tell that
        machine the document moved even when it never read the file as text
        (only it writes the notebook's file while it holds the folder)."""
        ...


class RecordedDecider(Protocol):
    """Decides an action for a caller with no HTTP request and records the
    decision (an allow in ``db``, a deny in a committed session of its own),
    returning it rather than raising: ``backend.authz.decide_on_record``,
    handed in by the layer that may call authz."""

    async def __call__(
        self,
        db: AsyncSession,
        ctx: ActingContext,
        action: Action,
        resource: Resource,
        attrs: Mapping[str, object],
        *,
        method: str,
        path: str,
    ) -> Decision: ...


# -- the service -------------------------------------------------------------------


@dataclass
class NotebookService:
    """One per application. Every collaborator is injected."""

    transport: NotebookTransport
    feed: NotebookFeed
    carets: CaretBoard
    #: Decides and records ``notebook.run`` for a widget message from the
    #: notebook channel, which has no HTTP request to ``enforce`` on.
    decide: RecordedDecider
    docs: NotebookDocs | None = None
    #: How an ephemeral hub event leaves this process (the agent's caret).
    publish_ephemeral: Callable[[HubEvent], Awaitable[None]] | None = None
    #: Platform widget bundles by SHA-256 (``@alkera/widgets`` and the like),
    #: served to any reader of a notebook.
    bundles: Mapping[str, bytes] = field(default_factory=dict)
    #: The platform bundles by ``(module, version)``: the hash in ``bundles``.
    bundle_modules: Mapping[tuple[str, str], str] = field(default_factory=dict)
    #: How long a request the engine answers waits for the answer.
    answer_seconds: float = 10.0
    #: How long a request waits for its answer before it is sent again, under
    #: the same id (the box serves a request id once). A request sent while
    #: the box's channel was down (its worker restarting) is never delivered;
    #: the next one is, once the box is back.
    resend_seconds: float = 2.0
    #: How long a request is sent again while the box answers that it has not
    #: yet taken the notebook's folder (a box just started or woken takes its
    #: folders a few seconds after its worker is ready).
    ready_seconds: float = 120.0
    #: How long an install's outcome is waited for: a build may take minutes.
    install_answer_seconds: float = 900.0
    #: How long an install waits before it is sent again under the same id.
    install_resend_seconds: float = 30.0
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal
    #: The deliveries still waiting for the box's answer.
    _deliveries: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)

    async def settle(self) -> None:
        """Wait until no delivery is still waiting on the box."""
        while self._deliveries:
            await asyncio.gather(*list(self._deliveries), return_exceptions=True)

    async def close(self) -> None:
        """Stop waiting on the box for anything."""
        tasks = list(self._deliveries)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _docs(self) -> NotebookDocs:
        if self.docs is None:
            raise NotebookUnavailableError("notebooks are not served by this process")
        return self.docs

    # -- document ops ------------------------------------------------------------

    async def rebase(
        self, db: AsyncSession, target: Target, caller: Caller, *, epoch: int, update: bytes
    ) -> RebaseResult:
        """Carry a person's unsent update from before a history restart into
        the current epoch (an editor's; agents and boxes send operations,
        which carry over by their token)."""
        await db.commit()
        if caller.user is None or caller.kind != "person":
            raise CrdtError("forbidden", "only an editor hands over unsent typing")
        token = await self._docs().rebase_update(
            target.ref, epoch=epoch, update=update, author=caller.user
        )
        return RebaseResult(token=token)

    async def apply_ops(
        self,
        db: AsyncSession,
        target: Target,
        caller: Caller,
        *,
        ops: list[dict[str, Any]],
        base_token: str | None,
        submit_id: str | None,
    ) -> NotebookOpsResult:
        """Apply a batch as ``caller`` and answer what it did."""
        await db.commit()
        try:
            applied = await self._docs().apply(
                target.ref,
                ops=ops,
                base_token=base_token,
                submit_id=submit_id,
                agent_id=caller.agent_id,
                # An agent edits for the person whose session it runs in: the
                # document is written back to the drive as them (a write back
                # needs a person the drive lets write the file).
                author=caller.user,
                actor_key=caller.actor_key,
            )
        except NotebookOpError as exc:
            raise OpRefusedError(exc.op_code, exc.op_message, exc.index) from exc
        notices = [CellNotice.model_validate(one) for one in applied.notices]
        if not applied.repeat:
            now = self.clock()
            if applied.touched:
                notices.extend(await self._notices(db, target, caller, applied.touched, now=now))
            # Every writer through this route is recorded here (a person's
            # socket edits are recorded by the document type as they commit,
            # which this route's operations never pass through).
            if applied.touched:
                await edit_log.record_edits(
                    db,
                    org_id=target.org_id,
                    item_id=target.item_id,
                    cell_ids=applied.touched,
                    actor_key=caller.actor_key,
                    actor_kind=caller.kind,
                    user_id=caller.user.id if caller.user is not None else None,
                    agent_id=caller.agent_id if caller.kind == "agent" else None,
                    actor_display=caller.display[:255],
                    submit_id=submit_id,
                    at=now,
                )
                if caller.kind != "person":
                    await self._announce_presence(db, target)
                await db.commit()
            if caller.machine_id is not None:
                # The box holds the folder, so only it writes the notebook's
                # file: it is told now, joined as a text peer or not, so an
                # edit only its agent made still reaches the file.
                self._docs().holder_wrote(target.ref)
            if applied.caret is not None:
                await self._relay_caret(target, caller, applied.caret, applied.peer)
        return NotebookOpsResult(
            token=applied.token,
            repeat=applied.repeat,
            cells=[CellAfterOp.model_validate(one) for one in applied.cells],
            created=list(applied.created),
            notices=notices,
            graph=views.graph_summary(self._docs().graph_at(target.ref, applied.token)),
        )

    async def _notices(
        self,
        db: AsyncSession,
        target: Target,
        caller: Caller,
        cells: Sequence[str],
        *,
        now: datetime,
    ) -> list[CellNotice]:
        """``concurrent_edit`` for a cell another actor edited within the
        window, ``caret_present`` for one a person's caret is in."""
        found: list[CellNotice] = []
        editors = await edit_log.recent_editors(
            db,
            org_id=target.org_id,
            item_id=target.item_id,
            cell_ids=cells,
            since=now - NOTICE_WINDOW,
            exclude_actor_key=caller.actor_key,
        )
        carets = self.carets.present(
            target.item_id,
            cells,
            within=NOTICE_WINDOW.total_seconds(),
            exclude_user=str(caller.user.id)
            if caller.kind == "person" and caller.user is not None
            else None,
        )
        known = names.Names(db)
        await known.load(
            [row.user_id for rows in editors.values() for row in rows]
            + [views.caret_user(caret) for held in carets.values() for caret in held]
        )
        for cell_id, rows in sorted(editors.items()):
            who = ", ".join(dict.fromkeys(views.edit_name(known, row) for row in rows))
            found.append(
                CellNotice(
                    cell_id=cell_id,
                    kind="concurrent_edit",
                    message=f"{who} edited this cell in the last 15 seconds",
                    by=who,
                )
            )
        for cell_id, held in sorted(carets.items()):
            who = ", ".join(dict.fromkeys(views.caret_name(known, caret) for caret in held))
            found.append(
                CellNotice(
                    cell_id=cell_id,
                    kind="caret_present",
                    message=f"{who} has the cursor in this cell",
                    by=who,
                )
            )
        return found

    async def _announce_presence(self, db: AsyncSession, target: Target) -> None:
        """Tell everyone on the notebook's channel who is in which cell now.

        Sent after an edit by an actor that publishes no caret (an agent, the
        machine): its edits are its only presence, and without this the
        readers learn of them only when they next read the view. Each entry
        says how long it holds (``expires_in``), so a reader lets an agent go
        once it stops editing, with nothing more sent. The caller commits."""
        doc = await self._docs().view(target.ref)
        cell_ids = [
            str(raw["id"])
            for raw in doc.view.get("cells") or []
            if isinstance(raw, Mapping) and raw.get("id")
        ]
        present = await self.presence(db, target, cell_ids)
        await announce_platform_events(
            db,
            org_id=target.org_id,
            item_id=target.item_id,
            events=[views.presence_event(present)],
        )

    async def _relay_caret(self, target: Target, caller: Caller, data: bytes, peer: int) -> None:
        """Show the writer's caret at the end of its last edit to everyone on
        the document (the lane stamped its store with a 10 s timeout)."""
        if self.publish_ephemeral is None:
            return
        ref = target.ref
        envelope = DocEnvelope(
            doc_id=ref.doc_id,
            doc_type=cast(Any, ref.doc_type),
            epoch=1,
            peer_id="srv:0",
            seq=0,
            kind="crdt",
            payload=CrdtEphemeralPayload(
                data_b64=encode_b64(data),
                loro_peer=peer,
                user_id=None,
                display_name=caller.display[:128],
            ).model_dump(mode="json"),
        )
        event = HubEvent(
            lane="ephemeral",
            org_id=target.org_id,
            type="doc.crdt_ephemeral",
            entity=EPHEMERAL_ENTITY,
            entity_id=ref.channel,
            version=0,
            visibility="org",
            payload={"envelope": envelope.model_dump(mode="json")},
            channel=ref.channel,
        )
        try:
            await self.publish_ephemeral(event)
        except EphemeralTooLargeError:
            return

    # -- reading -------------------------------------------------------------------

    async def view(self, db: AsyncSession, target: Target | Where) -> NotebookView:
        """The engine's view of the notebook: the live document's cells and
        settings, each cell's run state as the kernel's latest snapshot
        reported it (``not_run`` when no kernel has reported one), the
        kernel's state and who is where."""
        await db.commit()
        doc = await self._docs().view(target.ref)
        settings = views.view_settings(doc.view)
        replay = self.feed.replay(target.item_id)
        snapshot_view = views.snapshot_view(replay.snapshot)
        kernel = await self._kernel_info(db, target, settings, snapshot_view)
        states = views.run_state(snapshot_view, kernel)
        named = await self._named_runs(
            db,
            target,
            [queued.run_id for queued in kernel.queue]
            + [views.last_run_id(state) for state in states.values()],
        )
        kernel = views.named_queue(kernel, named)
        cells = [
            views.cell_state(raw, states, named)
            for raw in doc.view.get("cells") or []
            if isinstance(raw, Mapping)
        ]
        presence_list = await self.presence(db, target, [cell.id for cell in cells])
        path = (
            name_text(target.node.name)
            if isinstance(target, Target)
            else await file_name(db, target.org_id, str(target.item_id)) or ""
        )
        return NotebookView(
            path=path,
            token=doc.token,
            settings=settings,
            kernel=kernel,
            cells=cells,
            presence=presence_list,
            read_only_reason=doc.view.get("read_only_reason"),
            output_frame_url=frames.output_frame_url(),
        )

    async def _named_runs(
        self, db: AsyncSession, target: Target | Where, run_ids: Sequence[str | None]
    ) -> dict[str, views.NamedRun]:
        """``engine run id -> who ran it, named, and its row`` for the runs a
        view mentions that the platform recorded."""
        wanted = {one for one in run_ids if one}
        if not wanted:
            return {}
        rows = list(
            (
                await db.execute(
                    select(NotebookRun).where(
                        NotebookRun.org_id == target.org_id,
                        NotebookRun.item_id == target.item_id,
                        NotebookRun.engine_run_id.in_(wanted),
                    )
                )
            ).scalars()
        )
        known = names.Names(db)
        await known.load([row.requested_by_user_id for row in rows])
        return {
            str(row.engine_run_id): views.NamedRun(actor=views.run_actor(known, row), run=row)
            for row in rows
        }

    async def view_for(self, *, org_id: uuid.UUID, item_id: uuid.UUID) -> NotebookView:
        """The view a socket joining the notebook channel is sent (the caller
        has decided the person may read the notebook)."""
        async with self.session_factory() as db:
            try:
                return await self.view(db, Where(org_id=org_id, item_id=item_id))
            finally:
                await db.rollback()

    async def presence(
        self, db: AsyncSession, target: Target | Where, cell_ids: Sequence[str]
    ) -> list[Presence]:
        """Who is in each cell now, by the one rule every document store
        answers with (``alkera_notebook.document.editing``): a person is
        where their caret stands (the browser clears it when they leave the
        cell), and an actor with no caret (an agent, the machine) is where
        it edited lately. A person's edits claim nothing: their caret does.
        Carets first, then newest. The engine's ``editing`` reads it."""
        now = self.clock()
        board_now = self.carets.now()
        present = self.carets.present(
            target.item_id, cell_ids, within=CARET_UNCONFIRMED_FOR.total_seconds()
        )
        editors = await edit_log.recent_editors(
            db,
            org_id=target.org_id,
            item_id=target.item_id,
            cell_ids=cell_ids,
            since=now - EDIT_WITHOUT_CARET_FOR,
        )
        known = names.Names(db)
        await known.load(
            [views.caret_user(caret) for carets in present.values() for caret in carets]
            + [row.user_id for rows in editors.values() for row in rows]
        )
        claims = [
            FocusClaim(
                actor_id=views.caret_actor(caret),
                display_name=views.caret_name(known, caret),
                kind="person",
                cell_id=cell_id,
                at=now - timedelta(seconds=max(board_now - caret.at, 0.0)),
                caret=True,
            )
            for cell_id, carets in present.items()
            for caret in carets
        ] + [
            FocusClaim(
                actor_id=row.actor_key,
                display_name=views.edit_name(known, row),
                kind=cast(Any, row.actor_kind),
                cell_id=cell_id,
                at=row.last_at,
                caret=False,
            )
            for cell_id, rows in editors.items()
            for row in rows
            if row.actor_kind != "person"
        ]
        return [
            Presence(
                who=info.display_name,
                kind=info.kind,
                cell_id=cell_id,
                at=info.at,
                actor_id=info.actor_id,
                caret=info.caret,
                expires_in=holds_for(info, now=now).total_seconds(),
            )
            for cell_id, infos in editing_now(claims, now=now).items()
            for info in infos
        ]

    async def kernel_state(self, db: AsyncSession, target: Target | Where) -> KernelInfo:
        """The notebook's kernel as the platform last heard of it."""
        await db.commit()
        doc = await self._docs().view(target.ref)
        snapshot = views.snapshot_view(self.feed.replay(target.item_id).snapshot)
        return await self._kernel_info(db, target, views.view_settings(doc.view), snapshot)

    async def _kernel_info(
        self,
        db: AsyncSession,
        target: Target | Where,
        settings: Settings,
        snapshot: Mapping[str, Any] | None,
    ) -> KernelInfo:
        """The notebook's kernel as the platform last heard of it (``absent``
        when it has none), with what the kernel's latest snapshot said of its
        environment, memory and queue when that snapshot is of this kernel."""
        reactivity = settings.reactivity
        kernel = await current_kernel(db, org_id=target.org_id, item_id=target.item_id)
        if kernel is None:
            return KernelInfo(state="absent", env=None, reactivity=reactivity)
        reported: Mapping[str, Any] = {}
        if snapshot is not None:
            found = snapshot.get("kernel")
            if isinstance(found, Mapping) and found.get("kernel_id") == kernel.kernel_id:
                reported = found
        known = {
            "env": None,
            "state": kernel.state,
            "reactivity": reactivity,
            "started_at": kernel.started_at,
            "kernel_id": kernel.kernel_id,
            "seq": int(kernel.seq),
        }
        lent = {k: reported[k] for k in ("env", "memory_bytes", "queue") if k in reported}
        try:
            return KernelInfo.model_validate({**known, **lent})
        except ValidationError:
            # What a newer or broken engine reported is left out; the
            # platform's own record of the kernel still answers.
            return KernelInfo.model_validate(known)

    async def activity(
        self,
        db: AsyncSession,
        target: Target,
        *,
        since: datetime,
        exclude_actor: str | None,
    ) -> Activity:
        """Edits and runs since ``since``, oldest first (the digest's source),
        each naming who: a person, or an agent and the person it acts for."""
        edits = await edit_log.edits_since(
            db,
            org_id=target.org_id,
            item_id=target.item_id,
            since=since,
            exclude_actor_key=exclude_actor,
        )
        runs = [
            run
            for run in (
                await db.execute(
                    select(NotebookRun)
                    .where(
                        NotebookRun.org_id == target.org_id,
                        NotebookRun.item_id == target.item_id,
                        NotebookRun.created_at >= since,
                    )
                    .order_by(NotebookRun.created_at)
                    .limit(500)
                )
            ).scalars()
            if exclude_actor is None or views.run_actor_key(run) != exclude_actor
        ]
        known = names.Names(db)
        await known.load(
            [row.user_id for row in edits] + [run.requested_by_user_id for run in runs]
        )
        items: list[ActivityItem] = [
            ActivityItem(
                at=row.last_at,
                actor=known.actor(
                    kind=row.actor_kind,
                    actor_key=row.actor_key,
                    user_id=row.user_id,
                    recorded=row.actor_display,
                ),
                kind="edit",
                cell_ids=[row.cell_id],
            )
            for row in edits
        ]
        for run in runs:
            items.append(
                ActivityItem(
                    at=run.created_at,
                    actor=views.run_actor(known, run),
                    kind="run",
                    cell_ids=views.run_cells(run),
                    run_id=run.engine_run_id or str(run.run_id),
                    status=run.status,
                )
            )
        items.sort(key=lambda item: item.at)
        return Activity(since=since, items=items)

    # -- acting through the kernel -----------------------------------------------------

    async def run_facts(
        self, db: AsyncSession, files: FilesContext, target: Target
    ) -> tuple[Resource, dict[str, object], Target]:
        """What the ``notebook.run`` decision is made on for this caller: the
        notebook as a resource, the facts the policy reads, and the target
        bound to the folder those facts name (a request then acts there and
        nowhere else). The route decides (``backend.authz.enforce``), which
        records it either way."""
        ctx = files.ctx
        holder = await folder_holder(db, ctx, target.item_id)
        attrs: dict[str, object] = {
            "rung": target.allowed.access.role or "",
            "scope_held": holder is not None,
            "scope_rung": await self._scope_rung(db, files, holder),
            "lease_admits_writes": await lease_admits_writes(files, target.node),
        }
        if ctx.is_agent:
            attrs["agent_chat_in_workspace"] = await agent_chat_in_workspace(
                db, target, ctx.acting_principal.id
            )
        resource = Resource(
            type=ResourceType.NOTEBOOK, id=str(target.item_id), org_id=target.org_id
        )
        return resource, attrs, dataclasses.replace(target, scope=KernelScope(holder))

    @staticmethod
    async def _scope_rung(
        db: AsyncSession, files: FilesContext, holder: FolderHolder | None
    ) -> str:
        """The caller's Files rung on the folder the kernel binds; ``""`` for
        none, or when no machine holds it."""
        if holder is None:
            return ""
        scope = await authorized_node(
            db,
            files.repo,
            files.ctx,
            NodeId(holder.lease_node_id),
            FilesAction.READ,
            method=SCOPE_METHOD,
            path=f"node:{holder.lease_node_id}",
        )
        return "" if scope is None else scope.access.role or ""

    async def host(self, db: AsyncSession, files: FilesContext, target: Target) -> KernelHost:
        """The machine holding the notebook's folder, where its kernel runs:
        the one ``notebook.run`` decided on, when it did."""
        if target.scope is not None:
            holder = target.scope.holder
        else:
            holder = await folder_holder(db, files.ctx, target.item_id)
        if holder is None:
            raise NoMachineError("no machine holds this notebook's folder")
        return KernelHost(
            org_id=target.org_id,
            drive_id=target.drive_id,
            item_id=target.item_id,
            machine_id=holder.machine_id,
            lease_node_id=holder.lease_node_id,
            path=holder.path,
        )

    async def run(
        self,
        db: AsyncSession,
        files: FilesContext,
        target: Target,
        caller: Caller,
        body: RunRequest,
    ) -> RunAccepted:
        """Take the targets' text at the requester's frontier (waiting for the
        document to include it), record the run and hand it to the box."""
        if body.client_run_id is not None:
            earlier = (
                await db.execute(
                    select(NotebookRun).where(
                        NotebookRun.org_id == target.org_id,
                        NotebookRun.item_id == target.item_id,
                        NotebookRun.client_run_id == body.client_run_id,
                    )
                )
            ).scalar_one_or_none()
            if earlier is not None:
                return RunAccepted(
                    run_id=str(earlier.run_id),
                    status=cast(Any, earlier.status),
                    frontier_included=earlier.frontier_included,
                    repeat=True,
                )
        host = await self.host(db, files, target)
        await db.commit()
        doc = await self._docs().view(
            target.ref, frontier=body.frontier, wait_seconds=FRONTIER_WAIT_SECONDS
        )
        submitted = views.targets_text(doc.view, body.target.model_dump(mode="json"))
        run_id = uuid.uuid4()
        trigger = {"all": "run_all", "stale": "run_stale"}.get(body.target.kind, "run")
        run = NotebookRun(
            run_id=run_id,
            org_id=target.org_id,
            drive_id=target.drive_id,
            item_id=target.item_id,
            client_run_id=body.client_run_id,
            actor_kind=caller.kind,
            requested_by_user_id=caller.user.id if caller.user is not None else None,
            requested_by_agent=caller.agent_id if caller.kind == "agent" else None,
            actor_display=caller.display[:255],
            trigger=trigger,
            target=body.target.model_dump(mode="json"),
            frontier=body.frontier,
            frontier_included=doc.covered,
            submitted={
                "at_token": doc.token,
                "cells": {
                    cell_id: {
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "bytes": len(text.encode("utf-8")),
                    }
                    for cell_id, text in submitted.items()
                },
            },
            status="queued",
            engine_run_id=str(run_id),
        )
        db.add(run)
        await db.commit()
        # Sent under the run's own id: the box's answer names the run, and
        # its events follow it (``runs.follow_runs``).
        request = {
            "run_id": str(run_id),
            "target": body.target.model_dump(mode="json"),
            "at_token": doc.token,
            "trigger": trigger,
            "confirm_expensive": body.confirm_expensive,
            "requested_by": views.requested_by(caller),
        }
        waiting = self.feed.expect(str(run_id))
        try:
            await self.transport.send(host, "run", request, request_id=run_id)
        except Exception:
            self.feed.forget(str(run_id))
            # Nothing will answer a request that was never sent.
            await self._end_unsent(run_id, org_id=target.org_id)
            raise
        except BaseException:
            self.feed.forget(str(run_id))
            raise
        self._deliver(host, "run", request, run_id, waiting)
        return RunAccepted(
            run_id=str(run_id),
            status="queued",
            frontier_included=doc.covered,
            submitted=submitted,
        )

    async def to_kernel(
        self,
        db: AsyncSession,
        files: FilesContext,
        target: Target,
        caller: Caller,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
    ) -> uuid.UUID:
        """Carry a kernel action, a widget message or an install to the box."""
        host = await self.host(db, files, target)
        await db.commit()
        return await self.transport.send(
            host, op, {**body, "requested_by": views.requested_by(caller)}
        )

    async def env_action(
        self,
        db: AsyncSession,
        files: FilesContext,
        target: Target,
        caller: Caller,
        action: str,
        packages: Sequence[str],
    ) -> uuid.UUID:
        """Carry an environment action (``install``, ``build``, ``remove``,
        ``cancel``) to the box and, once its answer arrives, announce the
        outcome on the notebook's channel as an ``env.install`` event of no
        kernel and no sequence (the platform's word, like a refused run):
        ``{type, action, status: "ok" | "error", packages, message?, code?,
        env_id?}`` (``code`` is the box's refusal code on an error it named).
        A box that never answers is announced as an error too, so a person
        waiting on it is never left without one."""
        host = await self.host(db, files, target)
        await db.commit()
        request_id = uuid.uuid4()
        op: NotebookRequestOp = "env_install" if action == "install" else "env_action"
        body: dict[str, Any] = {
            "packages": list(packages),
            "requested_by": views.requested_by(caller),
        }
        if op == "env_action":
            body["action"] = action
        waiting = self.feed.expect(str(request_id))
        try:
            await self.transport.send(host, op, body, request_id=request_id)
        except BaseException:
            self.feed.forget(str(request_id))
            raise

        async def outcome() -> None:
            try:
                answer = await self._answer(
                    host,
                    op,
                    body,
                    request_id,
                    waiting,
                    within=self.install_answer_seconds,
                    every=self.install_resend_seconds,
                )
            finally:
                self.feed.forget(str(request_id))
            event = install_event(action, list(packages), answer)
            async with self.session_factory() as session:
                await announce_platform_events(
                    session, org_id=host.org_id, item_id=host.item_id, events=[event]
                )
                await session.commit()

        task = asyncio.get_running_loop().create_task(outcome())
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)
        return request_id

    async def _host_for(
        self, db: AsyncSession, *, org_id: uuid.UUID, item_id: uuid.UUID, user: User
    ) -> KernelHost | None:
        """The machine holding a notebook's folder, read as ``user`` (whose
        right to the notebook the caller has just decided)."""
        ctx = ActingContext.for_user(user_id=user.id, org_id=org_id, email=user.email)
        holder = await folder_holder(db, ctx, item_id)
        if holder is None or holder.drive_id is None:
            return None
        return KernelHost(
            org_id=org_id,
            drive_id=holder.drive_id,
            item_id=item_id,
            machine_id=holder.machine_id,
            lease_node_id=holder.lease_node_id,
            path=holder.path,
        )

    async def comm_from_socket(
        self,
        *,
        org_id: uuid.UUID,
        item_id: uuid.UUID,
        user: User,
        agent_id: str | None,
        frame: NbCommFrame,
    ) -> uuid.UUID:
        """A widget message sent on the notebook channel, decided as POST
        ``.../comm`` decides it: the Files policy admits the notebook, the
        ``notebook.run`` policy decides on the same facts (recorded either
        way), and the frame must be the sender's on this notebook. Raises
        :class:`CommRefusedError` for a refusal; returns the request id the
        box was sent."""
        ctx = (
            ActingContext.for_agent(
                user_id=user.id, org_id=org_id, email=user.email, session_id=agent_id
            )
            if agent_id
            else ActingContext.for_user(user_id=user.id, org_id=org_id, email=user.email)
        )
        where = nb_channel(item_id)
        async with self.session_factory() as db:
            files = await build_files_context(db, ctx)
            allowed = await authorized_node(
                db,
                files.repo,
                ctx,
                NodeId(item_id),
                FilesAction.READ,
                method=SOCKET_METHOD,
                path=where,
            )
            if allowed is None or not is_notebook(allowed.node):
                await db.commit()
                raise CommRefusedError("not_found", "no such notebook", lost=True)
            target = Target(
                org_id=org_id,
                drive_id=uuid.UUID(str(allowed.node.drive_id)),
                item_id=item_id,
                allowed=allowed,
            )
            caller = await runner_of(db, files)
            resource, attrs, target = await self.run_facts(db, files, target)
            decision = await self.decide(
                db, ctx, Action.RUN, resource, attrs, method=SOCKET_METHOD, path=where
            )
            if not decision.allowed:
                raise CommRefusedError(
                    "not_found" if decision.as_not_found else "forbidden",
                    decision.message,
                    lost=decision.as_not_found,
                )
            if self.frame_of(target, caller, frame.frame_id) is None:
                await db.rollback()
                raise CommRefusedError("not_found", "no such frame")
            try:
                body = frame.model_dump(mode="json", include=set(_COMM_KEYS))
                return await self.to_kernel(db, files, target, caller, "comm", body)
            except NoMachineError as exc:
                await db.commit()
                raise CommRefusedError("notebook.no_machine", str(exc)) from exc

    async def ask_snapshot(
        self, *, org_id: uuid.UUID, item_id: uuid.UUID, user: User
    ) -> uuid.UUID | None:
        """Ask the box for a fresh snapshot of the kernel's state; it arrives
        on the notebook channel as an event."""
        async with self.session_factory() as db:
            host = await self._host_for(db, org_id=org_id, item_id=item_id, user=user)
            await db.rollback()
        if host is None:
            return None
        return await self.transport.send(host, "snapshot", {})

    # -- requests the engine answers ---------------------------------------------------

    async def ask(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Carry a request to the engine and wait for its ``answer`` event
        (which the box posts like any other event, so it reaches every
        replica); the answer's ``result``. Raises :class:`KernelAnswerError`
        for a refusal and :class:`KernelSilentError` when none comes."""
        request_id = uuid.uuid4()
        waiting = self.feed.expect(str(request_id))
        try:
            await self.transport.send(host, op, body, request_id=request_id)
            answer = await self._answer(host, op, body, request_id, waiting)
        finally:
            self.feed.forget(str(request_id))
        if answer is None:
            raise KernelSilentError(ASK_SILENT_MESSAGE)
        error = answer.get("error")
        if isinstance(error, Mapping):
            raise KernelAnswerError(
                str(error.get("code") or "refused"), str(error.get("message") or "")
            )
        result = answer.get("result")
        return dict(result) if isinstance(result, Mapping) else {}

    async def _answer(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
        request_id: uuid.UUID,
        waiting: asyncio.Future[dict[str, Any]],
        *,
        within: float | None = None,
        every: float | None = None,
    ) -> dict[str, Any] | None:
        """The answer to a request already sent once, sending it again under
        the same id every ``every`` (:attr:`resend_seconds`) seconds until it
        comes or ``within`` (:attr:`answer_seconds`) pass (``None`` then).

        A ``folder_not_held`` answer (the box has not yet taken the folder its
        lease names) is not the answer: the request is sent again, and the
        box's word that it is alive restarts the wait, for at most
        :attr:`ready_seconds` in all. A box still not holding it then is
        answered with its ``folder_not_held``."""
        loop = asyncio.get_running_loop()
        window = self.answer_seconds if within is None else within
        deadline = loop.time() + window
        ready_by = loop.time() + max(window, self.ready_seconds)
        interval = self.resend_seconds if every is None else every
        key = str(request_id)
        #: The box's last ``folder_not_held``: what the wait ends with, once it
        #: runs out, when the box spoke at all.
        not_held: dict[str, Any] | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return not_held
            try:
                answer = await asyncio.wait_for(
                    asyncio.shield(waiting), timeout=min(interval, remaining)
                )
            except TimeoutError:
                if loop.time() >= deadline:
                    return not_held
            else:
                if not _folder_not_held(answer):
                    return answer
                not_held = answer
                if loop.time() >= ready_by:
                    return answer
                deadline = min(ready_by, loop.time() + window)
                waiting = self.feed.expect(key)
                await asyncio.sleep(min(interval, max(0.0, ready_by - loop.time())))
            await self.transport.send(host, op, body, request_id=request_id)

    async def _end_unsent(
        self, run_id: uuid.UUID, *, org_id: uuid.UUID, reason: str = NOT_SENT
    ) -> None:
        """End a run the box never took: its request never reached the
        machine channel (``not_sent``), or the box never took the folder its
        lease names (``folder_not_held``)."""
        async with self.session_factory() as db:
            run = await db.get(NotebookRun, run_id, with_for_update=True)
            if run is None or run.org_id != org_id:
                return
            await end_runs(db, [run], status="refused", reason=reason, at=self.clock())
            await db.commit()

    def _deliver(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Mapping[str, Any],
        run_id: uuid.UUID,
        waiting: asyncio.Future[dict[str, Any]],
    ) -> None:
        """See a run request already sent once through to the box's answer
        (sending it again while the box is away); a run the box never
        answers ends ``refused`` (``machine_silent``) rather than waiting
        forever."""

        async def deliver() -> None:
            try:
                answer = await self._answer(host, op, body, run_id, waiting)
            finally:
                self.feed.forget(str(run_id))
            if answer is not None and _folder_not_held(answer):
                await self._end_unsent(run_id, org_id=host.org_id, reason=FOLDER_NOT_HELD)
                return
            if answer is not None:
                return
            async with self.session_factory() as db:
                await end_unanswered(db, org_id=host.org_id, run_id=run_id, at=self.clock())
                await db.commit()

        task = asyncio.get_running_loop().create_task(deliver())
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def attach_frame(
        self,
        db: AsyncSession,
        files: FilesContext,
        target: Target,
        caller: Caller,
        request: FrameAttachRequest,
    ) -> FrameAttached:
        """Attach a person's output frame at the engine's widget hub: a frame
        id only they (and, when they named one, only their socket) are
        addressed by, and the comm-open replays of the frame's models."""
        if caller.user is None:
            raise KernelAnswerError("frame_needs_person", "an output frame is a person's")
        host = await self.host(db, files, target)
        await db.commit()
        frame_id = frames.mint(
            owner=caller.user.id, item_id=target.item_id, peer_id=request.peer_id
        )
        result = await self.ask(
            host,
            "frame_attach",
            {
                "frame_id": frame_id,
                "output_id": request.output_id,
                "model_ids": request.model_ids,
                "requested_by": views.requested_by(caller),
            },
        )
        opens = result.get("opens")
        return FrameAttached(
            frame_id=frame_id,
            opens=[dict(one) for one in opens if isinstance(one, Mapping)]
            if isinstance(opens, list)
            else [],
        )

    def frame_of(self, target: Target, caller: Caller, frame_id: str) -> frames.FrameRef | None:
        """The frame ``frame_id`` when it is ``caller``'s, on this notebook."""
        found = frames.verify(frame_id, item_id=target.item_id)
        if found is None or caller.user is None or found.owner != caller.user.id:
            return None
        return found

    async def detach_frame(
        self, db: AsyncSession, files: FilesContext, target: Target, caller: Caller, frame_id: str
    ) -> None:
        host = await self.host(db, files, target)
        await db.commit()
        await self.transport.send(
            host,
            "frame_detach",
            {"frame_id": frame_id, "requested_by": views.requested_by(caller)},
        )

    async def detach_frames(
        self, *, org_id: uuid.UUID, item_id: uuid.UUID, user: User, peer_id: str | None
    ) -> None:
        """A socket left the notebook: the hub drops the frames narrowed to
        it."""
        async with self.session_factory() as db:
            host = await self._host_for(db, org_id=org_id, item_id=item_id, user=user)
            await db.rollback()
        if host is None:
            return
        await self.transport.send(
            host,
            "frame_detach",
            {
                "prefix": frames.owner_prefix(owner=user.id, peer_id=peer_id),
                "requested_by": views.person_requested_by(user),
            },
        )

    async def table_page(
        self, db: AsyncSession, files: FilesContext, target: Target, query: Mapping[str, Any]
    ) -> TablePage:
        """A page of a table output: the kernel's own table page, passed on
        as the box answered it."""
        host = await self.host(db, files, target)
        await db.commit()
        result = await self.ask(host, "table", query)
        try:
            return TablePage.model_validate({**result, "limit": query["limit"]})
        except ValidationError as exc:
            # A box from before table pages had one shape answers another.
            raise KernelAnswerError("notebook.table_unreadable", TABLE_UNREADABLE_MESSAGE) from exc

    async def envs(self, db: AsyncSession, files: FilesContext, target: Target) -> EnvListing:
        host = await self.host(db, files, target)
        await db.commit()
        return EnvListing.model_validate(await self.ask(host, "envs", {}))

    async def env_packages(
        self, db: AsyncSession, files: FilesContext, target: Target, env_id: str
    ) -> EnvPackages:
        host = await self.host(db, files, target)
        await db.commit()
        return EnvPackages.model_validate(await self.ask(host, "env_packages", {"env_id": env_id}))

    def resolve_module(self, item_id: uuid.UUID, module: str, version: str) -> str | None:
        """The hash of a widget module's code: a platform bundle, or a module
        this notebook's kernel offered; ``None`` otherwise."""
        bundle = self.bundle_modules.get((module, version))
        if bundle is not None:
            return bundle
        return self.feed.module(item_id, module, version)

    # -- blobs and widget assets ---------------------------------------------------------

    async def blob_node(
        self, files: FilesContext, target: Target, sha256: str, *, only_ext: str | None = None
    ) -> FileNode | None:
        """:func:`stored.blob_node`, as the routes reach it."""
        return await blob_node(files, target, sha256, only_ext=only_ext)

    def live_image(self, target: Target, sha256: str) -> InlineImage | None:
        """The raster image ``sha256`` names among the outputs the notebook's
        cells show now, as this replica heard the kernel report them; the
        same outputs :meth:`view` hands a reader."""
        replay = self.feed.replay(target.item_id)
        bundles = views.live_bundles(views.snapshot_view(replay.snapshot), replay.events)
        return inline_image(bundles, sha256)


#: What a request the box never answered reads as to the person who asked
#: (the same request may be tried again: the route says when).
ASK_SILENT_MESSAGE: Final = "The machine did not answer. Try again."


def _folder_not_held(answer: Mapping[str, Any]) -> bool:
    """Whether the box answered that it does not hold the folder."""
    error = answer.get("error")
    return isinstance(error, Mapping) and error.get("code") == FOLDER_NOT_HELD


#: What a table page this server cannot read is refused as.
TABLE_UNREADABLE_MESSAGE: Final = "The machine answered a table page this server can't read."
#: What an install the box never answered is announced as.
INSTALL_SILENT_MESSAGE: Final = "The machine serving this notebook did not answer."


def install_event(
    action: str, packages: list[str], answer: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The ``env.install`` event an environment action's answer is announced
    as: ``ok`` with the environment it built, or ``error`` with what went
    wrong (the engine's message, or that no answer came)."""
    event: dict[str, Any] = {"type": "env.install", "action": action, "packages": packages}
    if answer is None:
        return {**event, "status": "error", "message": INSTALL_SILENT_MESSAGE}
    error = answer.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or "") or "The install failed."
        found = {**event, "status": "error", "message": message[:2000]}
        code = error.get("code")
        if isinstance(code, str) and code:
            found["code"] = code[:64]
        return found
    event["status"] = "ok"
    result = answer.get("result")
    env = result.get("env") if isinstance(result, Mapping) else None
    env_id = env.get("env_id") if isinstance(env, Mapping) else None
    if isinstance(env_id, str) and env_id:
        event["env_id"] = env_id
    return event


__all__ = [
    "AGENT_CARET_SECONDS",
    "FRONTIER_WAIT_SECONDS",
    "NOTICE_WINDOW",
    "SOCKET_METHOD",
    "NotebookDocs",
    "NotebookService",
    "RecordedDecider",
]
