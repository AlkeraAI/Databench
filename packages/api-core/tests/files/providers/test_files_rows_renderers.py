"""A workspace object, read as a file: the same bytes everywhere, every time.

A rendering is a wire format. A chat exported today is opened by a reader built
later, diffed against a copy taken from another machine, and hashed by a sync
client that will re-download the file if the hash moves — so the interesting
properties are all about agreement: two processes agree, the database-backed
renderer agrees with the committed golden, and ``head()`` agrees with ``open()``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import drives, objects_bridge
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import ReadOnlyContent
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.providers.registry import ProviderRegistry, RendererRegistry
from alkera_core.files.providers.rows import (
    ChatRenderer,
    ChatTemplateRenderer,
    NotImplementedRendering,
    ResultRenderer,
    RetiredRenderer,
    RetiredRendering,
    RowsProvider,
    UnimplementedRenderer,
    register_rows_providers,
    render_result,
    standard_renderers,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from alkera_core.models.workspace_object import (
    OBJECT_TYPES,
    ChatMessage,
    ObjectPayloadRow,
    WorkspaceObject,
)
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg
from tests.fixtures.files import generate_renderings as goldens

pytestmark = pytest.mark.asyncio

GENERATOR = Path(goldens.__file__).resolve()


# ---- helpers ---------------------------------------------------------------


def _ctx(org: FilesOrg, user_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _member(session: AsyncSession, org: FilesOrg) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(),
        home_org_team_id=org.org_team_id,
        email=f"r-{uuid.uuid4().hex[:12]}@files.test",
        email_domain="files.test",
        first_name="Row",
        last_name="Reader",
    )
    session.add(user)
    await session.commit()
    return user.id


async def _seed_object(
    session: AsyncSession,
    org: FilesOrg,
    owner_id: uuid.UUID,
    *,
    object_type: str,
    header: dict[str, Any],
    spec: dict[str, Any] | None = None,
) -> WorkspaceObject:
    """The object row a golden's header describes, so a rendering of it can be
    compared byte for byte against the committed bytes."""
    await session.execute(
        delete(WorkspaceObject).where(WorkspaceObject.id == uuid.UUID(header["id"]))
    )
    row = WorkspaceObject(
        id=uuid.UUID(header["id"]),
        org_team_id=org.org_team_id,
        logical_id=header["logical_id"],
        namespace=header["namespace"],
        type=object_type,
        title=header["title"],
        version=header["version"],
        owner_user_id=owner_id,
        visibility_scope="private",
        spec=spec or {},
    )
    session.add(row)
    await session.commit()
    return row


async def _object_node(
    session: AsyncSession, factory: FilesFactory, obj: WorkspaceObject, name: str
) -> FileNode:
    """A node projecting ``obj``, named the way the bridge names one."""
    drive = await factory.drive()
    tree = await factory.tree(name, drive=drive)
    node = tree[name]
    node.kind = "object"
    node.target_object_id = obj.id
    await session.commit()
    return node


async def _read(provider: RowsProvider, node: FileNode) -> bytes:
    return b"".join([chunk async for chunk in await provider.open(node, None)])


async def _refuse(session: AsyncSession, obj: WorkspaceObject, spec: dict[str, Any]) -> None:
    raise AssertionError("a read-only rendering must not reach an update")


# ---- determinism: two processes, and the committed golden ------------------


#: The variables an interpreter needs to start at all. Windows loads winsock out
#: of %SystemRoot%, so a scrubbed environment that drops it makes `import
#: asyncio` die with WinError 10106 before the renderer runs; the rest let the
#: child find its temp dir and its own DLLs. Nothing here can steer the bytes.
_BOOT_VARS: tuple[str, ...] = (
    "SystemRoot",
    "SystemDrive",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)


def _scrubbed_env(seed: str) -> dict[str, str]:
    """The child's whole environment: a pinned hash seed and nothing that reads.

    Locale, PYTHONPATH and every other ambient knob are gone, so a rendering
    that depended on one shows up as a diff between the two seeds.
    """
    env = {"PYTHONHASHSEED": seed, "PATH": os.defpath}
    for name in _BOOT_VARS:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


@pytest.mark.parametrize("object_type", ["chat", "query", "result"])
def test_two_processes_render_the_same_bytes_and_they_are_the_golden(object_type: str) -> None:
    """The format is agreement, so it is proven the way agreement is proven.

    Two fresh interpreters render the same input; a hash-randomised dict order,
    a locale-dependent float or an unsorted key would show up as a diff between
    them. The golden then pins those bytes against the future.
    """
    runs = [
        subprocess.run(
            [sys.executable, str(GENERATOR), "--emit", object_type],
            check=True,
            capture_output=True,
            env=_scrubbed_env(seed),
        ).stdout
        for seed in ("1", "31337")
    ]
    assert runs[0] == runs[1], "two processes disagree on the rendering"
    assert runs[0] == goldens.golden_path(object_type).read_bytes()


def test_the_golden_input_is_what_the_golden_was_rendered_from() -> None:
    """The committed input is the readable half of the golden.

    Without it a reviewer meeting a re-blessed golden has nothing to diff the
    change of behaviour against — only the bytes that changed.
    """
    for object_type in ("chat", "query", "result"):
        committed = json.loads(goldens.input_path(object_type).read_text(encoding="utf-8"))
        assert committed == goldens.INPUTS[object_type]


# ---- the database-backed renderers agree with the goldens ------------------


async def test_chat_rendering_from_rows_is_the_golden(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """Seeded transcript rows render to the committed chat bytes.

    This is what ties the pure formatter the golden is made with to the query
    the product actually runs: ordering by ``seq``, the fields carried, the
    envelope. Seeding the messages out of order proves the ordering is the
    renderer's and not the insertion's.
    """
    owner = await _member(files_session, files_org)
    document = goldens.CHAT_INPUT
    obj = await _seed_object(
        files_session, files_org, owner, object_type="chat", header=document["object"]
    )
    for message in reversed(document["messages"]):
        files_session.add(
            ChatMessage(
                id=uuid.uuid4(),
                chat_id=obj.id,
                org_team_id=files_org.org_team_id,
                seq=message["seq"],
                role=message["role"],
                kind=message["kind"],
                event_id=message["event_id"],
                payload=message["payload"],
            )
        )
    await files_session.commit()
    node = await _object_node(files_session, files_factory, obj, "weekly.alkerachat")

    provider = RowsProvider(repo, ChatRenderer())
    assert await _read(provider, node) == goldens.golden_path("chat").read_bytes()


async def test_result_rendering_from_paged_rows_is_the_golden(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """Two pages, seeded newest first, render as one CSV in page order."""
    owner = await _member(files_session, files_org)
    document = goldens.RESULT_INPUT
    obj = await _seed_object(
        files_session,
        files_org,
        owner,
        object_type="result",
        header=goldens._RESULT_OBJECT,
        spec={"columns": document["columns"]},
    )
    pages = [document["rows"][:1], document["rows"][1:]]
    for page in (1, 0):
        files_session.add(
            ObjectPayloadRow(
                object_id=obj.id,
                page=page,
                sha256="0" * 64,
                size=0,
                media_type="application/json",
                page_rows=pages[page],
            )
        )
    await files_session.commit()
    node = await _object_node(files_session, files_factory, obj, "run7.alkeraresult")

    provider = RowsProvider(repo, ResultRenderer())
    assert await _read(provider, node) == goldens.golden_path("result").read_bytes()


# ---- head() and open() are the same bytes ----------------------------------


@pytest.mark.parametrize(
    ("object_type", "renderer_name"),
    [
        pytest.param("chat", "chat", id="chat"),
        pytest.param("chat_template", "chat_template", id="chat_template"),
        pytest.param("result", "result", id="result"),
    ],
)
async def test_head_hash_is_the_hash_of_the_opened_bytes(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    object_type: str,
    renderer_name: str,
) -> None:
    """A sync client trusts ``head()`` and downloads what ``open()`` yields; if
    the two disagreed it would re-download the file forever."""
    owner = await _member(files_session, files_org)
    renderers = {
        "chat": ChatRenderer(),
        "chat_template": ChatTemplateRenderer(),
        "result": ResultRenderer(),
    }
    spec = {"columns": goldens.RESULT_INPUT["columns"]} if object_type == "result" else {}
    header = {
        "id": str(uuid.uuid4()),
        "logical_id": f"o-{uuid.uuid4().hex[:8]}",
        "namespace": "workspace",
        "title": "Head vs open",
        "version": 1,
    }
    obj = await _seed_object(
        files_session, files_org, owner, object_type=object_type, header=header, spec=spec
    )
    if object_type == "result":
        files_session.add(
            ObjectPayloadRow(
                object_id=obj.id,
                page=0,
                sha256="0" * 64,
                size=0,
                media_type="application/json",
                page_rows=goldens.RESULT_INPUT["rows"],
            )
        )
        await files_session.commit()
    extension = objects_bridge.POINTER_EXTENSIONS[object_type]
    node = await _object_node(files_session, files_factory, obj, f"x.{extension}")

    provider = RowsProvider(repo, renderers[renderer_name])
    info = await provider.head(node, None)
    body = await _read(provider, node)
    digests = hash_bytes(body)
    assert info.size == len(body)
    assert info.content_hash == digests.content_hash.hex()
    assert info.block_hash == digests.block_hash.hex()
    assert info.mime == renderers[renderer_name].mime


class _WriteBody:
    """The provider's write body, spelled locally so the test does not depend on
    the upload layer to make one."""

    def __init__(self, stream: Any, size: int) -> None:
        self.stream = stream
        self.size = size
        self.mime_hint: str | None = None


async def test_a_read_only_rendering_refuses_before_it_reads_the_body(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A chat cannot be rewritten by writing its file — and the refusal happens
    before a byte of the body is consumed, so a 10 GB body costs nothing."""
    owner = await _member(files_session, files_org)
    obj = await _seed_object(
        files_session,
        files_org,
        owner,
        object_type="chat",
        header=goldens.CHAT_INPUT["object"],
    )
    node = await _object_node(files_session, files_factory, obj, "weekly.alkerachat")
    consumed = False

    async def body() -> Any:
        nonlocal consumed
        consumed = True
        yield b"{}"

    provider = RowsProvider(repo, ChatRenderer())
    with pytest.raises(ReadOnlyContent):
        await provider.write(node, _WriteBody(body(), 2), if_match=node.etag)
    assert not consumed, "the body was read before the refusal"


# ---- CSV quoting -----------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        pytest.param("plain", "plain", id="bare"),
        pytest.param("a,b", '"a,b"', id="comma"),
        pytest.param('say "hi"', '"say ""hi"""', id="quote"),
        pytest.param("line\nbreak", '"line\nbreak"', id="newline"),
        pytest.param("line\rbreak", '"line\rbreak"', id="carriage-return"),
        pytest.param("nul\x00byte", '"nul\x00byte"', id="nul"),
        pytest.param("tab\tstop", '"tab\tstop"', id="tab"),
        pytest.param("", "", id="empty"),
    ],
)
def test_a_result_cell_is_quoted_exactly_when_rfc_4180_needs_it(cell: str, expected: str) -> None:
    """The negative twin matters as much as the positive: a renderer that quoted
    everything would still parse, and would still be wrong."""
    rendered = render_result(["c"], [[cell]]).decode()
    assert rendered == f"c\r\n{expected}\r\n"


def test_a_result_row_keeps_the_declared_header_order() -> None:
    """The header is the object's column order; a row is positional under it."""
    rendered = render_result(["b", "a"], [[1, 2]]).decode()
    assert rendered == "b,a\r\n1,2\r\n"


# ---- the registry names every type -----------------------------------------


def test_the_registry_lists_every_object_type_including_the_unrendered_ones() -> None:
    """``board`` and ``app`` are registered before anyone renders them.

    A missing registration is a 500 the first time a user opens such a node; a
    registered refusal is a typed 422, and the day the real renderer lands it is
    a swap here rather than a new registration in every caller.
    """
    renderers = standard_renderers(update_query=_refuse)
    assert set(renderers.object_types()) == {
        "chat",
        "query",
        "report",
        "result",
        "board",
        "app",
        "workspace",
        "chat_template",
        # A derived member of a folder is registered the same way:
        # `chat_template:README.md` is a renderer key and a node subtype at once.
        "chat_template:README.md",
    }
    assert not RendererRegistry.is_writable(renderers.get("chat"))
    assert not RendererRegistry.is_writable(renderers.get("result"))
    assert not RendererRegistry.is_writable(renderers.get("chat_template"))
    # Nothing writes back a retired type either: its rendering is gone, so a
    # mount cannot resurrect the row through the file surface.
    assert not RendererRegistry.is_writable(renderers.get("query"))
    assert not RendererRegistry.is_writable(renderers.get("report"))


@pytest.mark.parametrize("object_type", ["query", "report"])
async def test_a_retired_type_refuses_at_read_time_and_says_so(object_type: str) -> None:
    """A retired object still resolves; reading it as a file answers "gone".

    Its rows are still there to be converted, listed and trashed, so the node
    has to resolve to something. What it must not do is serve bytes nobody
    writes any more, or fall through to the generic "no renderer" a missing
    registration would produce.
    """
    with pytest.raises(RetiredRendering) as raised:
        await RetiredRenderer(object_type).render(None, None)  # type: ignore[arg-type]
    assert raised.value.status == 410
    assert raised.value.code == "files.rendering_retired"
    assert object_type in str(raised.value)


@pytest.mark.parametrize("object_type", ["board", "app", "workspace"])
async def test_an_unrendered_type_refuses_typed_at_read_time(object_type: str) -> None:
    with pytest.raises(NotImplementedRendering) as raised:
        await UnimplementedRenderer(object_type).render(None, None)  # type: ignore[arg-type]
    assert raised.value.status == 422
    assert raised.value.code == "files.rendering_not_implemented"


def test_every_renderer_gets_its_rows_provider_from_the_registry(repo: FilesRepo) -> None:
    """A provider per registered type, keyed the way the registry resolves a node."""
    providers = ProviderRegistry()
    register_rows_providers(providers, standard_renderers(update_query=_refuse), repo)
    assert set(providers.kinds()) == {
        "rows:chat",
        "rows:query",
        "rows:report",
        "rows:result",
        "rows:board",
        "rows:app",
        "rows:workspace",
        "rows:chat_template",
        "rows:chat_template:README.md",
    }


# ---- the object update bumps the node and announces it exactly once --------


async def test_an_object_update_bumps_the_node_and_emits_one_delta_row(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The file surface moves when the object does — once, and only if it commits.

    A watcher treats each outbox row as a change to fetch, so two rows for one
    edit costs a second render; a row that survived the caller's rollback would
    announce an edit that never happened.
    """
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single"],
    )
    files_session.add(store)
    await files_session.commit()
    async with repo.transaction():
        await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store.id
        )
    await files_session.commit()

    owner = await _member(files_session, files_org)
    obj = await _seed_object(
        files_session,
        files_org,
        owner,
        object_type="query",
        header={
            "id": str(uuid.uuid4()),
            "logical_id": f"q-{uuid.uuid4().hex[:8]}",
            "namespace": "workspace",
            "title": "Top accounts",
            "version": 1,
        },
        spec={"sql": "select 1"},
    )
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        node_id = node.id
        before = node.etag
    await files_session.commit()
    baseline = await _outbox_rows(files_session, node_id)

    async with repo.transaction():
        bumped = await objects_bridge.bump_for_object_update(repo, _ctx(files_org, owner), obj.id)
        assert bumped is not None
        assert bumped.etag == before + 1, "the update did not move the node's change token"
        assert await _outbox_rows(files_session, node_id, in_files_role=True) == baseline + 1, (
            "one edit must announce itself exactly once"
        )
        await files_session.rollback()

    assert await _outbox_rows(files_session, node_id) == baseline, (
        "a rolled-back update announced a change that never happened"
    )
    etag_now = (
        await files_session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()
    assert etag_now == before, "the etag survived a rolled-back update"


async def _outbox_rows(
    session: AsyncSession, node_id: uuid.UUID, *, in_files_role: bool = False
) -> int:
    """The announcements standing for one node.

    ``event_outbox`` is a platform table the Files role holds no grant on, so a
    count taken inside an open Files transaction drops the role the way the
    production write does and takes it back, leaving the transaction as it found
    it.
    """
    if in_files_role:
        await session.execute(text("SET LOCAL ROLE NONE"))
    try:
        result = await session.execute(
            text("SELECT count(*) FROM event_outbox WHERE entity_id = :id"),
            {"id": str(node_id)},
        )
        return int(result.scalar_one())
    finally:
        if in_files_role:
            await session.execute(text("SET LOCAL ROLE alkera_files_app"))


@pytest.mark.parametrize("object_type", list(OBJECT_TYPES), ids=str)
def test_every_object_type_the_model_admits_has_a_renderer(object_type: str) -> None:
    """The vocabulary and the registry are one list, checked per member.

    ``standard_renderers`` is built by hand, so a type added to the model's
    CHECK vocabulary without a registration here is not a compile error — it is
    a 500 the first time someone opens one of those nodes in a mount, a pull or
    the content route. Parametrized rather than a set comparison so the failure
    names the type that has no renderer.
    """
    renderers = standard_renderers(update_query=_refuse)
    assert object_type in renderers.object_types()
    assert renderers.get(object_type).mime, "a renderer must declare the mime it serves"
