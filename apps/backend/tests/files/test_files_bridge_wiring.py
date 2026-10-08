"""The bridge, as the product actually reaches it.

Every test here drives a real service function — ``object_service``,
``chat_service``, ``membership_service``, ``team_service`` — rather than the
Files library, because the thing being proven is the wiring: that the node is
written by the caller's own transaction, that deleting the object is the only
direction deletion travels, and that with ``files_enabled`` off none of it
happens at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import APP_ROLE, ORG_SETTING, FilesRepo
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from alkera_core.models.workspace_object import WorkspaceObject
from backend.services.chats import chat_service
from backend.services.objects import object_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member

pytestmark = pytest.mark.asyncio


async def _nodes_for(session: AsyncSession, org_id: uuid.UUID) -> list[FileNode]:
    """Every node in ``org_id``, read as the caller sees them mid-transaction."""
    repo = FilesRepo(session, OrgScope(org_team_id=org_id))
    await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": ORG_SETTING, "value": str(org_id)},
    )
    rows = (await session.execute(repo.select_nodes())).scalars().all()
    await session.execute(text("SET LOCAL ROLE NONE"))
    return list(rows)


async def _node_for_object(
    session: AsyncSession, org_id: uuid.UUID, object_id: uuid.UUID
) -> FileNode | None:
    for node in await _nodes_for(session, org_id):
        if node.target_object_id != object_id or node.trashed_at is not None:
            continue
        # A replication context's members point at the same object as their
        # folder; the node FOR the object is the container.
        if node.subtype and ":" in node.subtype:
            continue
        return node
    return None


async def _etag_of(session: AsyncSession, node_id: uuid.UUID) -> int:
    """The node's etag as the row holds it, not as the identity map remembers it."""
    return int(
        (
            await session.execute(
                text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
            )
        ).scalar_one()
    )


async def _outbox_count(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM event_outbox"))).scalar_one())


@pytest.fixture
def files_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.scoped import FilesystemScoped
    from backend.services.files.store import set_store_factory

    root = tmp_path / "bridge-store"
    root.mkdir()
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    yield
    set_store_factory(None)


@pytest.fixture
def files_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_enabled", False)


@pytest_asyncio.fixture
async def owner(real_session: AsyncSession, org_admin: OrgWithAdmin) -> User:
    return (
        await real_session.execute(select(User).where(User.id == org_admin.admin_id))
    ).scalar_one()


async def _create_chat(
    session: AsyncSession, owner: User, title: str = "A chat"
) -> WorkspaceObject:
    obj, created = await object_service.create_object(
        session, owner=owner, type="chat", title=title, spec={}, org_id=owner.home_org_team_id
    )
    assert created
    return obj


async def _created_chat(session: AsyncSession, owner: User, title: str) -> WorkspaceObject:
    """A chat made by a request of its own, committed: the next write is
    another request, which takes the chat's rows in their own order."""
    obj = await _create_chat(session, owner, title=title)
    await session.commit()
    return obj


async def test_object_create_writes_its_node_in_the_same_transaction(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    obj = await _create_chat(real_session, owner)
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None, "the node must be readable before the create commits"
    assert node.kind == "folder"
    assert node.name.endswith(b".alkerachat")


async def test_a_rolled_back_create_leaves_no_node(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    org_id, obj_id = owner.home_org_team_id, (await _create_chat(real_session, owner, "Doomed")).id
    assert await _node_for_object(real_session, org_id, obj_id) is not None
    await real_session.rollback()
    assert await _node_for_object(real_session, org_id, obj_id) is None
    assert (
        await real_session.execute(
            select(func.count()).select_from(WorkspaceObject).where(WorkspaceObject.id == obj_id)
        )
    ).scalar_one() == 0


async def test_deleting_the_chat_trashes_its_node(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """The chat's folder is binned, and stops naming the chat.

    The node is found by the id it had before the delete, not by the object it
    used to point at: a deleted chat is a tombstone that is not coming back, so
    the folder is detached from it and what a restore yields is an ordinary
    folder rather than a door onto retired rows.
    """
    obj = await _created_chat(real_session, owner, title="Doomed chat")
    before = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert before is not None
    await chat_service.delete_chat(real_session, chat=obj)
    assert await _node_for_object(real_session, owner.home_org_team_id, obj.id) is None
    every = await _nodes_for(real_session, owner.home_org_team_id)
    binned = [n for n in every if n.id == before.id]
    assert len(binned) == 1
    assert binned[0].trashed_at is not None
    assert binned[0].trash_op_id is not None, "the Trash listing joins through the op row"
    assert binned[0].target_object_id is None
    assert not [n for n in every if n.target_object_id == obj.id]


async def test_an_update_bumps_the_etag_and_emits_exactly_one_row(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    obj = await _created_chat(real_session, owner, title="Before")
    before = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert before is not None
    etag_before = await _etag_of(real_session, before.id)
    rows_before = await _outbox_count(real_session)
    await object_service.apply_update(
        real_session, obj=obj, expected_version=obj.version, title="After"
    )
    assert await _node_for_object(real_session, owner.home_org_team_id, obj.id) is not None
    assert await _etag_of(real_session, before.id) == etag_before + 1
    assert await _outbox_count(real_session) == rows_before + 1


async def test_a_new_membership_creates_the_member_home_folder(
    real_session: AsyncSession, org_admin: OrgWithAdmin, files_on: None
) -> None:
    local = f"joiner{uuid.uuid4().hex[:8]}"
    member, _ = await make_member(
        real_session, org_id=org_admin.org_id, email=f"{local}@example.test", verified=True
    )
    await membership_service.remove_member(
        real_session,
        membership=await membership_service.get(
            real_session, team_id=org_admin.org_id, user_id=member.id
        ),
    )
    await membership_service.add_member(real_session, team_id=org_admin.org_id, user_id=member.id)
    homes = [
        n
        for n in await _nodes_for(real_session, org_admin.org_id)
        if n.name == str(member.id).encode()
    ]
    assert len(homes) == 1, "the member's home is created when they join"
    assert homes[0].kind == "folder"
    assert homes[0].subtype == "home"


async def test_a_new_team_creates_its_folder(
    real_session: AsyncSession, org_admin: OrgWithAdmin, files_on: None
) -> None:
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Platform"
    )
    folders = [n for n in await _nodes_for(real_session, org_admin.org_id) if n.name == b"Platform"]
    assert len(folders) == 1
    assert folders[0].kind == "folder"
    assert team.name == "Platform"


async def test_files_off_writes_no_nodes_and_leaves_the_object_flow_intact(
    real_session: AsyncSession, owner: User, files_off: None
) -> None:
    obj = await _create_chat(real_session, owner, title="Dark")
    assert obj.title == "Dark"
    updated = await object_service.apply_update(
        real_session, obj=obj, expected_version=obj.version, title="Still dark"
    )
    assert updated.title == "Still dark"
    await real_session.commit()
    await chat_service.delete_chat(real_session, chat=updated)
    assert updated.deleted_at != 0
    assert await _nodes_for(real_session, owner.home_org_team_id) == []


async def _home_names(session: AsyncSession, org_id: uuid.UUID) -> list[bytes]:
    """The names of the folders directly under the drive's ``home/`` container."""
    nodes = await _nodes_for(session, org_id)
    containers = [n.id for n in nodes if n.name == b"home"]
    return sorted(n.name for n in nodes if n.parent_id in containers)


async def _rejoin(session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Take the member out of the org and put them back through the service, so the
    home folder is created by the real join path rather than by the fixture."""
    await membership_service.remove_member(
        session,
        membership=await membership_service.get(session, team_id=org_id, user_id=user_id),
    )
    await membership_service.add_member(session, team_id=org_id, user_id=user_id)


async def test_a_home_folder_is_named_by_the_member_id_and_never_the_address(
    real_session: AsyncSession, org_admin: OrgWithAdmin, files_on: None
) -> None:
    local = f"alice{uuid.uuid4().hex[:8]}"
    member, _ = await make_member(
        real_session, org_id=org_admin.org_id, email=f"{local}@x.com", verified=True
    )
    await _rejoin(real_session, org_id=org_admin.org_id, user_id=member.id)
    names = await _home_names(real_session, org_admin.org_id)
    assert str(member.id).encode() in names
    assert not [name for name in names if local.encode() in name], (
        "no home in the org carries the member's address"
    )


async def test_two_members_sharing_a_local_part_get_a_home_each_by_id(
    real_session: AsyncSession, org_admin: OrgWithAdmin, files_on: None
) -> None:
    local = f"alice{uuid.uuid4().hex[:8]}"
    one, _ = await make_member(
        real_session, org_id=org_admin.org_id, email=f"{local}@x.com", verified=True
    )
    two, _ = await make_member(
        real_session, org_id=org_admin.org_id, email=f"{local}@y.com", verified=True
    )
    await _rejoin(real_session, org_id=org_admin.org_id, user_id=one.id)
    await _rejoin(real_session, org_id=org_admin.org_id, user_id=two.id)
    names = await _home_names(real_session, org_admin.org_id)
    assert str(one.id).encode() in names
    assert str(two.id).encode() in names


async def test_an_address_change_keeps_the_same_home(
    real_session: AsyncSession, org_admin: OrgWithAdmin, files_on: None
) -> None:
    """The home is the member's by id: a new address finds the same folder and
    writes no second one."""
    member, _ = await make_member(
        real_session,
        org_id=org_admin.org_id,
        email=f"before{uuid.uuid4().hex[:8]}@x.com",
        verified=True,
    )
    await _rejoin(real_session, org_id=org_admin.org_id, user_id=member.id)
    before = await _home_names(real_session, org_admin.org_id)
    member.email = f"after{uuid.uuid4().hex[:8]}@y.com"
    await real_session.flush()
    await _rejoin(real_session, org_id=org_admin.org_id, user_id=member.id)
    after = await _home_names(real_session, org_admin.org_id)
    assert after == before
    assert after.count(str(member.id).encode()) == 1


# --------------------------------------------------------------------------
# a replication context is a real folder
# --------------------------------------------------------------------------


async def _create_template(
    session: AsyncSession, owner: User, title: str = "Weekly"
) -> WorkspaceObject:
    obj, created = await object_service.create_object(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title=title,
        spec={"title": title, "brief": "Pull the weekly numbers."},
    )
    assert created
    return obj


async def _children_of(
    session: AsyncSession, org_id: uuid.UUID, parent_id: uuid.UUID
) -> list[FileNode]:
    return [
        node
        for node in await _nodes_for(session, org_id)
        if node.parent_id == parent_id and node.trashed_at is None
    ]


async def test_a_chat_template_is_a_folder_with_listable_members(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """A ``.alkerachat.template`` is a folder someone can open.

    A single pointer file answers a listing with nothing, so the files a new
    chat starts with and the README explaining what the template is for would
    both be unreachable from the drive.
    """
    obj = await _create_template(real_session, owner)
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None
    assert node.kind == "folder"
    children = await _children_of(real_session, owner.home_org_team_id, node.id)
    assert sorted(bytes(child.name) for child in children) == [b"README.md", b"scratch"]
    by_name = {bytes(child.name): child for child in children}
    assert by_name[b"scratch"].kind == "folder"
    assert by_name[b"README.md"].kind == "object"
    assert by_name[b"README.md"].target_object_id == obj.id
    assert by_name[b"README.md"].subtype == "chat_template:README.md"


async def test_a_derived_member_serves_the_bytes_the_one_renderer_produces(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """The member's content is derived, and derived by the SAME function.

    ``README.md`` holds what the one renderer produces for this object — not a
    copy taken at create time — so an edit is visible with nothing rewritten
    anywhere.
    """
    from alkera_core.files.providers.derived_members import render_member
    from alkera_core.files.providers.rows import (
        RowsProvider,
        chat_template_document,
        context_member_renderers,
    )

    obj = await _create_template(real_session, owner)
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None
    members = {
        bytes(c.name): c for c in await _children_of(real_session, owner.home_org_team_id, node.id)
    }
    renderers = {r.object_type: r for r in context_member_renderers()}

    repo = FilesRepo(real_session, OrgScope(org_team_id=owner.home_org_team_id))
    expected = render_member("chat_template", "README.md", chat_template_document(obj))
    provider = RowsProvider(repo, renderers["chat_template:README.md"])
    chunks = [chunk async for chunk in await provider.open(members[b"README.md"], None)]
    assert b"".join(chunks) == expected
    assert provider.materialize(members[b"README.md"]) == "bytes"
    assert b"# Weekly" in expected
    assert b"Pull the weekly numbers." in expected


async def test_updating_the_object_changes_the_member_bytes_without_rewriting_them(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """Re-render on update, no churn: the member has no stored version at all."""
    from alkera_core.files.providers.rows import RowsProvider, context_member_renderers

    obj = await _create_template(real_session, owner)
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None
    members = {
        bytes(c.name): c for c in await _children_of(real_session, owner.home_org_team_id, node.id)
    }
    readme_node = members[b"README.md"]
    before_etag = await _etag_of(real_session, readme_node.id)

    obj.spec = {"title": "Weekly", "brief": "Now ask about the region too."}
    obj.version += 1
    await object_service.bump_object_node(real_session, obj)
    await real_session.flush()

    repo = FilesRepo(real_session, OrgScope(org_team_id=owner.home_org_team_id))
    renderer = {r.object_type: r for r in context_member_renderers()}["chat_template:README.md"]
    provider = RowsProvider(repo, renderer)
    rendered = b"".join([chunk async for chunk in await provider.open(members[b"README.md"], None)])
    assert b"Now ask about the region too." in rendered
    # Derived content is never stored, so there is nothing to churn…
    assert await provider.versions(members[b"README.md"]) == ()
    # …but the change token still has to move, or a conditional GET answers 304.
    assert await _etag_of(real_session, readme_node.id) > before_etag


async def test_an_object_update_resolves_the_container_and_not_a_member(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """``live_node_for`` resolves the CONTAINER.

    Two nodes now point at one template. An unfiltered lookup returns an
    arbitrary one, so the object's own etag bump could land on ``README.md``
    and a delete could trash ``README.md`` while leaving the template standing.
    """
    from alkera_core.files.objects_bridge import live_node_for

    obj = await _create_template(real_session, owner)
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None

    repo = FilesRepo(real_session, OrgScope(org_team_id=owner.home_org_team_id))
    async with repo.transaction():
        resolved = await live_node_for(repo, obj.id)
    assert resolved is not None
    assert resolved.id == node.id
    assert resolved.kind == "folder"


async def test_deleting_the_template_trashes_the_folder_not_its_readme(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    obj = await _create_template(real_session, owner)
    # Made and deleted by two requests, as a person does.
    await real_session.commit()
    node = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert node is not None
    await object_service.tombstone_object_node(real_session, obj)
    await real_session.flush()
    trashed = (
        await real_session.execute(
            text("SELECT trashed_at IS NOT NULL FROM file_nodes WHERE id = :id"), {"id": node.id}
        )
    ).scalar_one()
    assert trashed is True


# --------------------------------------------------------------------------
# a chat's seal covers the chat; a sealed folder still seals what it holds
# --------------------------------------------------------------------------


async def _write_under(
    session: AsyncSession, owner: User, parent: FileNode, name: bytes, *, kind: str = "file"
) -> FileNode:
    """One node created the way the agent creates one: through the namespace."""
    from alkera_core.authz.principal import ActingContext
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.ids import DriveId, NodeId
    from alkera_core.files.namespace import Namespace, NodeAttrs

    repo = FilesRepo(session, OrgScope(org_team_id=owner.home_org_team_id))
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with repo.transaction():
        return await Namespace(repo, ctx, SystemClock()).create(
            DriveId(parent.drive_id),
            NodeId(parent.id),
            kind,
            name,
            attrs=NodeAttrs(mode=0o644 if kind == "file" else 0o755),
            conflict="rename",
        )


async def test_the_chats_seal_stops_at_the_chat_folder(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """The conversation does not leave as a file; its working material does.

    A chat folder carries NO_DOWNLOAD so neither the chat nor a zip of it is a
    way to export the conversation. What it holds is a different thing: the
    working directory is ordinary files a person opened, edited and expects to
    take away — and so is a file written at the chat folder's own top level.
    The seal is marked SELF_ONLY on the folder, so the descent clears it one
    level down and never re-derives it from a name.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT, SEAL_SELF_ONLY_BIT
    from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER

    assert CHAT_SANDBOX_FOLDER is not None
    obj = await _create_chat(real_session, owner)
    chat = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert chat is not None
    assert chat.flags & NO_DOWNLOAD_BIT, "the chat folder itself is the sealed one"
    assert chat.flags & SEAL_SELF_ONLY_BIT, "and its seal covers itself alone"
    folders = {
        bytes(c.name): c for c in await _children_of(real_session, owner.home_org_team_id, chat.id)
    }
    assert set(folders) == {CHAT_SANDBOX_FOLDER}, "one child: the working directory"
    for parent in (folders[CHAT_SANDBOX_FOLDER], chat):
        written = await _write_under(real_session, owner, parent, b"report.pdf")
        assert not written.flags & NO_DOWNLOAD_BIT
        # The mark itself does not travel: a folder deeper down is not a container
        # that may quietly un-seal things a future writer seals.
        assert not written.flags & SEAL_SELF_ONLY_BIT


async def test_the_deliverable_marker_is_sticky_down_the_subtree(
    real_session: AsyncSession, owner: User, files_on: None
) -> None:
    """A file the agent nests inside a folder of its working directory is a
    deliverable too.

    Otherwise the exception would hold for exactly one level and an agent that
    organised its output into a subdirectory would seal it again.
    """
    from alkera_core.files.authz.decider import ARTIFACT_BIT, NO_DOWNLOAD_BIT
    from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER

    assert CHAT_SANDBOX_FOLDER is not None
    obj = await _create_chat(real_session, owner)
    chat = await _node_for_object(real_session, owner.home_org_team_id, obj.id)
    assert chat is not None
    working = {
        bytes(c.name): c for c in await _children_of(real_session, owner.home_org_team_id, chat.id)
    }[CHAT_SANDBOX_FOLDER]
    assert working.flags & ARTIFACT_BIT, "what the run produces lives in its working directory"
    nested = await _write_under(real_session, owner, working, b"q3", kind="folder")
    deep = await _write_under(real_session, owner, nested, b"report.pdf")
    assert deep.flags & ARTIFACT_BIT
    assert not deep.flags & NO_DOWNLOAD_BIT


def test_a_child_takes_only_the_restricting_half_of_its_parents_flags() -> None:
    """The mask is a mask: a bit that is not declared inheritable does not travel."""
    from alkera_core.files.authz.decider import (
        ARTIFACT_BIT,
        HELD_BIT,
        NO_DOWNLOAD_BIT,
        NO_RESHARE_BIT,
        SEAL_SELF_ONLY_BIT,
        flags_for_child,
    )

    sealed = NO_DOWNLOAD_BIT | NO_RESHARE_BIT | HELD_BIT
    assert flags_for_child(sealed) == NO_DOWNLOAD_BIT
    assert flags_for_child(0) == 0
    # A folder that seals only ITSELF — a chat — hands its children nothing;
    # every other sealed folder still seals what it holds, which is the whole
    # difference between a conversation and a report.
    assert flags_for_child(sealed | SEAL_SELF_ONLY_BIT) == 0
    assert not flags_for_child(sealed | SEAL_SELF_ONLY_BIT) & SEAL_SELF_ONLY_BIT
    assert flags_for_child(sealed | SEAL_SELF_ONLY_BIT, artifact=True) == ARTIFACT_BIT
    # A legal hold and a no-reshare are decisions about THAT node, not its
    # subtree, so they stay where they were put.
    assert not flags_for_child(sealed) & (HELD_BIT | NO_RESHARE_BIT)
    # The exception clears the bit and marks the subtree, both at once.
    assert flags_for_child(sealed, artifact=True) == ARTIFACT_BIT
    assert flags_for_child(ARTIFACT_BIT | NO_DOWNLOAD_BIT) == ARTIFACT_BIT
