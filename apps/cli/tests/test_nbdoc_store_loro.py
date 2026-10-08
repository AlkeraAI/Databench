"""``LoroDocumentStore`` against a live backend: a real uvicorn serving the real
app (Postgres, Files, the CRDT lane's sandbox workers, the real format API),
and the store speaking to it over HTTP and the realtime socket as a box does.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from alkera_cli.cloud.rest import ws_url_for
from alkera_cli.notebooks.store_loro import (
    DocSignal,
    Located,
    LoroDocumentStore,
    RealtimeDocSignals,
)
from alkera_notebook.document.ops import (
    EditCell,
    InsertCell,
    NotebookOpError,
    ReplaceCell,
    SetSetting,
    TextEdit,
)
from alkera_notebook.engine.errors import ForbiddenError, NotFoundError
from alkera_notebook.engine.models import Actor
from backend.services.files.chat_uploads import put_chat_upload
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import (  # noqa: F401  (fixtures registered by import)
    OrgWithAdmin,
    mint_cli_token,
    org_admin,
    real_session,
    uvicorn_server,
)
from tests.crdt.file_world import acting, file_world
from tests.crdt.test_nbdoc_real_format import FULL

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    pytest.mark.xdist_group("nbdoc_store_loro"),
]

PATH = "analysis/weekly.alknb.py"
WEEKLY = "s7t8v9w0x1"
ORDERS = "m2n3p4q5r6"
EDITOR = Actor(
    kind="agent", id="agent:analyst", display_name="Analyst", can_edit=True, can_run=True
)


@dataclass
class Live:
    store: LoroDocumentStore
    http: httpx.AsyncClient
    item_id: str
    addr: str
    created: dict[str, Located]


@pytest_asyncio.fixture
async def live(
    uvicorn_server: str,  # noqa: F811
    real_session: AsyncSession,  # noqa: F811
    org_admin: OrgWithAdmin,  # noqa: F811
) -> AsyncIterator[Live]:
    fw = await file_world(real_session, org_admin, content=FULL.encode(), name="weekly.alknb.py")
    owner = fw.world.owner.user
    from alkera_core.models.files.tree import FileNode

    node = await real_session.get(FileNode, fw.node_id)
    assert node is not None
    token = await mint_cli_token(user_id=owner.id, email=owner.email, org_team_id=org_admin.org_id)
    where = Located(drive_id=str(node.drive_id), item_id=str(fw.node_id))
    created: dict[str, Located] = {}

    async def resolve(path: str) -> Located:
        if path == PATH:
            return where
        if path in created:
            return created[path]
        raise NotFoundError(f"no notebook at {path}")

    async def create_file(path: str, text: str) -> Located:
        """The box's mirror making a file where the agent named it: here, a
        file handed to the same chat's folder."""
        landed = await put_chat_upload(
            real_session,
            ctx=acting(fw.world.owner),
            chat_id=uuid.UUID(fw.world.ref.doc_id),
            filename=path.rsplit("/", 1)[-1],
            content=text.encode(),
        )
        await real_session.commit()
        created[path] = Located(drive_id=str(node.drive_id), item_id=str(landed.node_id))
        return created[path]

    async with httpx.AsyncClient(
        base_url=f"http://{uvicorn_server}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as http:
        signals = RealtimeDocSignals(http=http, ws_url=ws_url_for(f"http://{uvicorn_server}"))
        yield Live(
            store=LoroDocumentStore(
                http=http, resolve=resolve, signals=signals, create_file=create_file
            ),
            http=http,
            item_id=str(fw.node_id),
            addr=uvicorn_server,
            created=created,
        )


async def test_load_reads_the_live_cells_in_order_with_their_code(live: Live) -> None:
    stored = await live.store.load(PATH)
    cells = stored.document.live_cells()
    assert [c.id for c in cells] == ["a1b2c3d4e5", "f6g7h8j9k0", ORDERS, WEEKLY]
    orders = stored.document.cells[ORDERS]
    assert orders.kind == "sql" and orders.source.startswith("SELECT order_id")
    assert orders.code.startswith("orders = alkera.sql(")
    assert stored.token


async def test_a_snapshot_is_each_named_cells_code_at_one_token(live: Live) -> None:
    whole = await live.store.snapshot(PATH)
    named = await live.store.snapshot(PATH, [WEEKLY])
    assert [c for c, _ in whole.cells] == ["a1b2c3d4e5", "f6g7h8j9k0", ORDERS, WEEKLY]
    assert named.cells == [c for c in whole.cells if c[0] == WEEKLY]
    assert named.token == whole.token


async def test_apply_edits_the_document_and_a_retry_is_not_applied_twice(live: Live) -> None:
    stored = await live.store.load(PATH)
    ops = [
        InsertCell(op="insert", source="total = weekly.sum()", after=WEEKLY, name="total"),
        EditCell(
            op="edit",
            cell_id=WEEKLY,
            edits=[TextEdit(old=".sum()", new='.sum().alias("total")')],
        ),
    ]
    first = await live.store.apply(PATH, ops, stored.token, EDITOR, "store-sub-00001")
    again = await live.store.apply(PATH, ops, stored.token, EDITOR, "store-sub-00001")
    after = await live.store.load(PATH)
    (created,) = first.created
    assert [c.id for c in after.document.live_cells()][-1] == created
    assert after.document.cells[created].source == "total = weekly.sum()"
    assert after.document.cells[WEEKLY].source.count('.alias("total")') == 1
    assert again.token == first.token or again.repeat


async def test_a_loaded_document_holds_only_the_settings_its_file_sets(live: Live) -> None:
    """The view answers a settings read: every effective value and where it
    came from. The engine's document is the file, so inherited defaults and
    ``sources`` never come back as settings the file sets. A ``sources`` key in
    the document broke every notebook tool with "got multiple values for
    keyword argument 'sources'"."""
    stored = await live.store.load(PATH)
    await live.store.apply(
        PATH,
        [SetSetting(key="dataframe", value="pandas")],
        stored.token,
        EDITOR,
        "store-sub-setting",
    )
    after = await live.store.load(PATH)
    # The file sets its format and its data frame library and nothing else:
    # the defaults a reader fills in are not the file's.
    assert dict(after.document.settings) == {"format": "1.0", "dataframe": "pandas"}


async def test_a_refused_operation_is_the_engines_op_error_with_its_index(live: Live) -> None:
    with pytest.raises(NotebookOpError) as refused:
        await live.store.apply(
            PATH,
            [
                ReplaceCell(op="replace", cell_id=ORDERS, source="SELECT 1"),
                EditCell(op="edit", cell_id=WEEKLY, edits=[TextEdit(old="absent", new="x")]),
            ],
            None,
            EDITOR,
            None,
        )
    assert (refused.value.index, refused.value.code) == (1, "edit_not_found")
    stored = await live.store.load(PATH)
    assert stored.document.cells[ORDERS].source.startswith("SELECT order_id")


async def test_an_actor_without_edit_rights_is_refused_before_any_request(live: Live) -> None:
    viewer = Actor(kind="person", id="u", display_name="U", can_edit=False, can_run=False)
    with pytest.raises(ForbiddenError):
        await live.store.apply(
            PATH, [ReplaceCell(op="replace", cell_id=ORDERS, source="x")], None, viewer, None
        )


async def test_a_path_the_resolver_does_not_know_is_not_found(live: Live) -> None:
    with pytest.raises(NotFoundError):
        await live.store.load("elsewhere.alknb.py")


async def test_another_writers_change_arrives_on_changes_with_the_cells_it_touched(
    live: Live,
) -> None:
    ready = asyncio.Event()
    signals = live.store.signals

    class _Told:
        async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
            async for signal in signals.watch(item_id):
                if signal.kind == "ready":
                    ready.set()
                yield signal

    store = LoroDocumentStore(http=live.http, resolve=live.store.resolve, signals=_Told())
    changes = store.changes(PATH)
    first = asyncio.ensure_future(changes.__anext__())
    await asyncio.wait_for(ready.wait(), timeout=10)
    other = LoroDocumentStore(
        http=live.http, resolve=live.store.resolve, signals=live.store.signals
    )
    await other.apply(
        PATH, [ReplaceCell(op="replace", cell_id=ORDERS, source="SELECT 2")], None, EDITOR, None
    )
    change = await asyncio.wait_for(first, timeout=10)
    await changes.aclose()
    assert change.cell_ids == [ORDERS]
    assert change.origin == "ops"
    assert change.token == (await live.store.load(PATH)).token


async def test_a_person_s_edit_through_the_route_does_not_leave_them_in_the_cell(
    live: Live,
) -> None:
    """The store here acts with a person's credential. A person is wherever
    their caret is, and this one holds none, so having edited the cell a
    moment ago is not being in it: nothing waits for them."""
    await live.store.apply(
        PATH, [ReplaceCell(op="replace", cell_id=WEEKLY, source="weekly = 1")], None, EDITOR, None
    )
    assert await live.store.editing(PATH) == {}


class _Signals:
    """Signals a test hands in by hand; ``listening`` is set once the store
    has read the document and waits for the first signal."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[DocSignal] = asyncio.Queue()
        self.listening = asyncio.Event()

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        while True:
            self.listening.set()
            yield await self.queue.get()


async def test_a_burst_of_signals_reads_the_view_and_reports_only_what_moved(live: Live) -> None:
    signals = _Signals()
    store = LoroDocumentStore(http=live.http, resolve=live.store.resolve, signals=signals)
    changes = store.changes(PATH)
    first = asyncio.ensure_future(changes.__anext__())
    await asyncio.wait_for(signals.listening.wait(), timeout=10)
    await live.store.apply(
        PATH, [ReplaceCell(op="replace", cell_id=WEEKLY, source="weekly = 2")], None, EDITOR, None
    )
    signals.queue.put_nowait(DocSignal(kind="update", actor_id="someone"))
    change = await asyncio.wait_for(first, timeout=10)
    assert (change.cell_ids, change.actor_id) == ([WEEKLY], "someone")
    # A signal with nothing new behind it is no change at all.
    signals.queue.put_nowait(DocSignal(kind="update"))
    second = asyncio.ensure_future(changes.__anext__())
    done, _ = await asyncio.wait({second}, timeout=1.0)
    assert not done
    second.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await second


async def test_create_writes_a_new_notebook_and_inserts_its_cells_as_operations(
    live: Live,
) -> None:
    stored = await live.store.create(
        "analysis/fresh.alknb.py",
        [
            InsertCell(op="insert", kind="setup", source="import alkera"),
            InsertCell(op="insert", source="x = 1", name="x"),
        ],
        {"reactivity": "lazy", "header": '"""Fresh."""'},
        EDITOR,
    )
    cells = stored.document.live_cells()
    assert [(c.kind, c.source) for c in cells] == [("setup", "import alkera"), ("python", "x = 1")]
    assert all(len(c.id) == 10 for c in cells)
    assert stored.document.setting("reactivity") == "lazy"
    reread = await live.store.load("analysis/fresh.alknb.py")
    assert reread.token == stored.token


async def test_create_without_a_way_to_make_files_is_refused(live: Live) -> None:
    store = LoroDocumentStore(
        http=live.http, resolve=live.store.resolve, signals=live.store.signals
    )
    with pytest.raises(ForbiddenError):
        await store.create("analysis/none.alknb.py", [], {}, EDITOR)


async def test_a_change_made_while_the_channel_was_joining_is_reported_when_it_is_ready(
    live: Live,
) -> None:
    signals = _Signals()
    store = LoroDocumentStore(http=live.http, resolve=live.store.resolve, signals=signals)
    changes = store.changes(PATH)
    first = asyncio.ensure_future(changes.__anext__())
    await asyncio.wait_for(signals.listening.wait(), timeout=10)
    # Written after the store's first reading, before the channel listens:
    # no update signal will ever name it.
    await live.store.apply(
        PATH, [ReplaceCell(op="replace", cell_id=ORDERS, source="SELECT 9")], None, EDITOR, None
    )
    signals.queue.put_nowait(DocSignal(kind="ready"))
    change = await asyncio.wait_for(first, timeout=10)
    await changes.aclose()
    assert (change.cell_ids, change.actor_id) == ([ORDERS], None)


async def test_a_created_sql_cell_keeps_its_metadata_for_the_template(live: Live) -> None:
    """A cell's ``meta`` (an SQL cell's output variable and connection) is
    what the format renders its code from: create carries it."""
    stored = await live.store.create(
        "analysis/sql.alknb.py",
        [
            InsertCell(
                op="insert",
                kind="sql",
                source="SELECT 1 AS one",
                meta={"output_var": "one", "connection": "Warehouse"},
            )
        ],
        {},
        EDITOR,
    )
    (cell,) = stored.document.live_cells()
    assert (cell.kind, cell.meta.get("output_var"), cell.meta.get("connection")) == (
        "sql",
        "one",
        "Warehouse",
    )
    assert cell.code.startswith("one = alkera.sql(") and 'connection="Warehouse"' in cell.code


async def test_the_box_s_store_writes_its_chat_s_agent_edits_for_the_chat_s_person(
    uvicorn_server: str,  # noqa: F811
    real_session: AsyncSession,  # noqa: F811
    org_admin: OrgWithAdmin,  # noqa: F811
) -> None:
    """The store as the box holding the chat's folder uses it: the box's own
    credential, every request under the fence of its lease, and an agent's
    batch naming its chat. The backend writes the batch as that chat's agent
    for the chat's person (who the file is then saved as); the same store
    without naming the chat writes as the bare machine."""
    from alkera_cli.notebooks.store_loro import agent_actor_id
    from alkera_core.authz.headers import agent_headers
    from alkera_core.models import WorkspaceObject
    from alkera_core.models.files.tree import FileNode
    from alkera_core.notebooks.models import NotebookEdit
    from sqlalchemy import select, update
    from tests.files._boxes import registered_box
    from tests.files._live_holder import MockHolder

    fw = await file_world(real_session, org_admin, content=FULL.encode(), name="weekly.alknb.py")
    owner = fw.world.owner.user
    chat_id = fw.world.ref.doc_id
    token, machine_id = await registered_box(
        real_session, user_id=owner.id, email=owner.email, org_id=fw.world.org_id
    )
    chat = await real_session.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    await real_session.execute(
        update(WorkspaceObject)
        .where(WorkspaceObject.id == chat.id)
        .values(spec={**(chat.spec or {}), "machine_id": machine_id})
    )
    await real_session.commit()
    folder = (
        await real_session.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(chat_id))
        )
    ).scalar_one()
    node = await real_session.get(FileNode, fw.node_id)
    assert node is not None
    async with httpx.AsyncClient(
        base_url=f"http://{uvicorn_server}",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
        timeout=30.0,
    ) as http:
        holder = MockHolder(http, folder.drive_id, folder.id, machine=machine_id)
        taken = await holder.take(
            real_session,
            lambda: {"Idempotency-Key": uuid.uuid4().hex},
            purpose="chat",
            inbound=True,
            live=True,
        )
        assert taken.status_code == 200, taken.text
        where = Located(drive_id=str(node.drive_id), item_id=str(fw.node_id), headers=holder.fence)

        async def resolve(path: str) -> Located:
            return where

        signals = RealtimeDocSignals(http=http, ws_url=ws_url_for(f"http://{uvicorn_server}"))
        agent = Actor(
            kind="agent", id=agent_actor_id(chat_id), display_name="a", can_edit=True, can_run=True
        )
        box_store = LoroDocumentStore(
            http=http, resolve=resolve, signals=signals, names_agent_chat=True
        )
        bare_store = LoroDocumentStore(http=http, resolve=resolve, signals=signals)
        await box_store.apply(
            PATH,
            [ReplaceCell(op="replace", cell_id=WEEKLY, source="weekly = 1")],
            None,
            agent,
            None,
        )
        await bare_store.apply(
            PATH, [ReplaceCell(op="replace", cell_id=ORDERS, source="SELECT 2")], None, agent, None
        )
    rows = (
        (
            await real_session.execute(
                select(NotebookEdit)
                .where(NotebookEdit.item_id == fw.node_id)
                .order_by(NotebookEdit.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.cell_id, r.actor_key, r.user_id) for r in rows] == [
        (WEEKLY, f"agent:{chat_id}", owner.id),
        (ORDERS, f"machine:{machine_id}", None),
    ]
