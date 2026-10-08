"""Objects are files: the node exists before the object's transaction commits.

Every case here is written from the object service's point of view, because
that is who calls this bridge: it opens one transaction, inserts its row, awaits
the bridge, and commits. So the tests never commit between the two halves —
they assert inside the open transaction and then roll it back, which is the only
way to prove that a crash between "object row written" and "node written"
cannot leave an object without a node.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import drives, names, objects_bridge
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import (
    ARTIFACT_BIT,
    NO_DOWNLOAD_BIT,
    AccessFacts,
    effective_role,
)
from alkera_core.files.authz.grants import FilesGrantSource
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.namespace import Namespace, NodeAttrs
from alkera_core.files.providers.registry import POINTER_EXTENSIONS as MOUNT_EXTENSIONS
from alkera_core.files.providers.registry import object_type_of
from alkera_core.files.repo import FilesRepo
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.stores import FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from alkera_core.models.workspace_object import WorkspaceObject
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesOrg

pytestmark = pytest.mark.asyncio


class _RollbackError(Exception):
    """Ends a transaction the way a failed object write would, without committing."""


def _ctx(org: FilesOrg, user_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _store(session: AsyncSession) -> uuid.UUID:
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single"],
    )
    session.add(store)
    await session.commit()
    return store.id


async def _member(session: AsyncSession, org: FilesOrg, *, teams: list[uuid.UUID]) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(),
        home_org_team_id=org.org_team_id,
        email=f"o-{uuid.uuid4().hex[:12]}@files.test",
        email_domain="files.test",
        first_name="Object",
        last_name="Owner",
    )
    session.add(user)
    await session.flush()
    for team_id in teams:
        session.add(TeamMembership(user_id=user.id, team_id=team_id, role=TeamRole.MEMBER))
    await session.commit()
    return user.id


async def _team(session: AsyncSession, *, parent: uuid.UUID, name: str) -> uuid.UUID:
    team = Team(id=uuid.uuid4(), parent_team_id=parent, name=name)
    session.add(team)
    await session.commit()
    return team.id


async def _object(
    session: AsyncSession,
    org: FilesOrg,
    owner_id: uuid.UUID,
    *,
    type: str = "chat",
    title: str = "Quarterly numbers",
    scope: str = "private",
    team_id: uuid.UUID | None = None,
    commit: bool = True,
) -> WorkspaceObject:
    """One workspace object row, the way the object service writes one."""
    row = WorkspaceObject(
        id=uuid.uuid4(),
        org_team_id=org.org_team_id,
        logical_id=f"obj-{uuid.uuid4().hex[:12]}",
        namespace="workspace",
        type=type,
        title=title,
        owner_user_id=owner_id,
        team_id=team_id,
        visibility_scope=scope,
        spec={},
    )
    session.add(row)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return row


async def _count(session: AsyncSession, sql: str, params: dict[str, Any]) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def _outbox_rows(
    session: AsyncSession, node_id: uuid.UUID, *, in_files_role: bool = False
) -> int:
    """The announcements standing for one node.

    ``event_outbox`` is a platform table the Files role holds no grant on — the
    production write drops to the login role for exactly that reason — so a
    count taken inside an open Files transaction does the same and takes the
    role back, leaving the transaction as it found it.
    """
    if in_files_role:
        await session.execute(text("SET LOCAL ROLE NONE"))
    try:
        return await _count(
            session,
            "SELECT count(*) FROM event_outbox WHERE entity_id = :id",
            {"id": str(node_id)},
        )
    finally:
        if in_files_role:
            await session.execute(text("SET LOCAL ROLE alkera_files_app"))


async def _may_read(
    repo: FilesRepo, ctx: ActingContext, node: FileNode, facts: AccessFacts
) -> bool:
    """Whether this caller may read the node, through the real decider."""
    drive = await repo.drive(DriveId(node.drive_id))
    assert drive is not None
    chain = await repo.chain(node)
    grants = await FilesGrantSource().grants_for(repo, node, chain)
    return effective_role(ctx, node, chain, grants, drive, facts).allows(FilesAction.READ)


async def _skeleton(repo: FilesRepo, org: FilesOrg, session: AsyncSession) -> None:
    """The org drive, committed, so each case starts where the product does."""
    store_id = await _store(session)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, _ctx(org), org.org_team_id, store_id=store_id)
    await session.commit()


# ---- the node exists before the object's transaction commits ---------------


async def test_object_has_its_node_before_the_creating_transaction_commits(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Both rows are written in one transaction, so neither can outlive the other.

    The assertions run inside the open transaction — an object row and its node,
    both visible, neither committed — and the rollback then proves they were the
    same unit of work: an object with no node is not a state this code can reach.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])

    # The object service's own INSERT, uncommitted. It runs before the role
    # switch because ``alkera_files_app`` is deliberately granted nothing on
    # ``workspace_objects``; ``transaction()`` then joins this same transaction,
    # which is what makes the two writes one unit of work.
    obj = await _object(files_session, files_org, owner, commit=False)
    assert files_session.in_transaction(), "the object row is not committed yet"

    with pytest.raises(_RollbackError):
        async with repo.transaction():
            node = await objects_bridge.node_for_object(
                repo, _ctx(files_org, owner), obj, clock=FakeClock(EPOCH)
            )
            assert node.kind == "folder", "a chat's node is the chat's folder"
            assert node.target_object_id == obj.id
            # The node is readable inside the transaction that made the object.
            assert await _count(
                files_session,
                "SELECT count(*) FROM file_nodes WHERE target_object_id = :id",
                {"id": obj.id},
            )
            node_id = node.id
            raise _RollbackError

    assert (
        await _count(
            files_session,
            "SELECT count(*) FROM workspace_objects WHERE id = :id",
            {"id": obj.id},
        )
        == 0
    )
    assert (
        await _count(
            files_session, "SELECT count(*) FROM file_nodes WHERE id = :id", {"id": node_id}
        )
        == 0
    ), "the node must not survive a rollback of the object that owns it"


# ---- a chat is a folder ---------------------------------------------------


async def _children(session: AsyncSession, parent_id: uuid.UUID) -> dict[bytes, str]:
    rows = await session.execute(
        text("SELECT name, kind FROM file_nodes WHERE parent_id = :parent"),
        {"parent": parent_id},
    )
    return {bytes(row[0]): row[1] for row in rows}


@pytest.mark.parametrize(
    ("object_type", "expected_kind", "expected_children"),
    [
        pytest.param("chat", "folder", set(objects_bridge.CHAT_FOLDER_CHILDREN), id="chat"),
        pytest.param(
            "chat_template",
            "folder",
            {b"scratch", b"README.md"},
            id="chat_template",
        ),
        pytest.param("workspace", "folder", {b"files", b".chats"}, id="workspace"),
        pytest.param("query", "object", set(), id="query"),
        pytest.param("result", "object", set(), id="result"),
    ],
)
async def test_a_node_is_a_folder_when_the_object_owns_one_and_a_pointer_otherwise(
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    object_type: str,
    expected_kind: str,
    expected_children: set[bytes],
) -> None:
    """Two object types own a directory, for two different reasons.

    A chat owns a working directory a run leases. A chat template owns the same
    directory — the files a new chat starts with — plus the ``README.md`` that
    tells a reader what the template is for. A promoted result is a frozen
    table and a saved query is no longer a folder at all: both stay a single
    pointer file, which is what keeps this a real distinction rather than
    "everything is a folder now".
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, type=object_type, commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        assert node.kind == expected_kind
        assert node.subtype == object_type, "the wire facet reads the type off the node"
        assert node.target_object_id == obj.id
        children = await _children(files_session, node.id)
    assert set(children) == expected_children
    # A chat's children are all directories. A template's `README.md` is an
    # `object` node — derived from the same row the folder points at, which is
    # what keeps it current with no rewrite.
    expected_child_kinds = {
        name: "object" if name == b"README.md" else "folder" for name in expected_children
    }
    assert children == expected_child_kinds
    await files_session.rollback()


async def test_a_chat_folder_carries_no_download_and_a_saved_query_does_not(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The refusal is a capability, not a route check.

    ``NO_DOWNLOAD`` drops EXPORT for everyone — the owner included — so the
    content route, the single-file serve, the download operation and the
    archive walk all refuse the chat from the one place capability is computed.
    A saved query is an ordinary artefact and keeps its export.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    facts = AccessFacts(team_ids=frozenset(), org_admin=False)
    ctx = _ctx(files_org, owner)

    chat = await _object(files_session, files_org, owner, type="chat", commit=False)
    query = await _object(files_session, files_org, owner, type="query", commit=False)
    async with repo.transaction():
        chat_node = await objects_bridge.node_for_object(repo, ctx, chat, clock=FakeClock(EPOCH))
        query_node = await objects_bridge.node_for_object(repo, ctx, query, clock=FakeClock(EPOCH))
        assert not await _may(repo, ctx, chat_node, facts, FilesAction.EXPORT)
        assert await _may(repo, ctx, chat_node, facts, FilesAction.READ), (
            "a chat is still readable — only its bytes may not leave"
        )
        assert await _may(repo, ctx, query_node, facts, FilesAction.EXPORT)
    await files_session.rollback()


async def _may(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    facts: AccessFacts,
    action: FilesAction,
) -> bool:
    drive = await repo.drive(DriveId(node.drive_id))
    assert drive is not None
    chain = await repo.chain(node)
    grants = await FilesGrantSource().grants_for(repo, node, chain)
    return effective_role(ctx, node, chain, grants, drive, facts).allows(action)


# ---- a chat's files live in one working directory --------------------------


async def _folder_under(
    repo: FilesRepo, ctx: ActingContext, parent: FileNode, name: bytes
) -> FileNode:
    """A plain folder a person (or an older chat's layout) put beside the rest."""
    namespace = Namespace(repo, ctx, FakeClock(EPOCH), ino=InoAllocator(repo))
    return await namespace.create(
        DriveId(parent.drive_id),
        NodeId(parent.id),
        "folder",
        name,
        attrs=NodeAttrs(mode=0o755),
        conflict="rename",
    )


async def _flags_of(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT flags FROM file_nodes WHERE id = :id"), {"id": node_id}
            )
        ).scalar_one()
    )


async def test_a_chat_is_born_with_its_working_directory_and_nothing_else(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """One child, named by the one constant, and it is where deliverables live.

    A second folder — for outputs, for attachments — was a second answer to
    "where does my file go". The working directory is the only child a chat is
    born with, and it carries the deliverable marker so what the run produces
    there is a report the member can take, not a sealed working file.
    """
    assert objects_bridge.CHAT_SANDBOX_FOLDER is not None
    assert objects_bridge.CHAT_FOLDER_CHILDREN == (objects_bridge.CHAT_SANDBOX_FOLDER,)

    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo, _ctx(files_org, owner), obj, clock=FakeClock(EPOCH)
        )
        children = await _children(files_session, node.id)
        assert children == {objects_bridge.CHAT_SANDBOX_FOLDER: "folder"}
        working = await objects_bridge.working_folder_node(repo, node)
        assert working is not None and working.parent_id == node.id
        flags = await _flags_of(files_session, working.id)
        assert flags & ARTIFACT_BIT, "what the run produces there is a deliverable"
        assert not flags & NO_DOWNLOAD_BIT, "and a deliverable can be taken away"
    await files_session.rollback()


async def test_a_chats_files_are_looked_up_at_its_working_directory_and_nowhere_else(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The lookup answers with the working directory: not with an ``outputs/``
    an older chat still carries — that is an ordinary folder, listed like one
    and never looked in — and not for a folder that merely shares the name
    under something that is not a chat. A trashed working directory is none.
    """
    assert objects_bridge.CHAT_SANDBOX_FOLDER is not None
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    obj = await _object(files_session, files_org, owner, commit=False)
    async with repo.transaction():
        chat = await objects_bridge.node_for_object(repo, ctx, obj, clock=FakeClock(EPOCH))
        legacy = await _folder_under(repo, ctx, chat, b"outputs")
        assert (await _children(files_session, chat.id))[b"outputs"] == "folder"
        assert not await _flags_of(files_session, legacy.id) & ARTIFACT_BIT, (
            "a folder a chat merely carries is not where its deliverables live"
        )

        working = await objects_bridge.working_folder_node(repo, chat)
        assert working is not None
        assert working.name == objects_bridge.CHAT_SANDBOX_FOLDER
        assert working.id != legacy.id
        assert await objects_bridge.working_folder_nodes(files_session, [chat.id]) == {
            chat.id: working.id
        }

        # A plain folder holding a child by the same name is not a conversation.
        plain = await _folder_under(repo, ctx, working, b"plain")
        await _folder_under(repo, ctx, plain, objects_bridge.CHAT_SANDBOX_FOLDER)
        assert await objects_bridge.working_folder_node(repo, plain) is None
        assert await objects_bridge.working_folder_nodes(files_session, [plain.id]) == {}

        # Trashed, the working directory is gone: nothing is minted by a read.
        await files_session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": working.id}
        )
        assert await objects_bridge.working_folder_nodes(files_session, [chat.id]) == {}
        assert await objects_bridge.working_folder_node(repo, chat) is None
    await files_session.rollback()


# ---- the grant matches the object's audience ------------------------------


@pytest.mark.parametrize(
    ("scope_kind", "expected_principal"),
    [
        pytest.param("org", "org", id="org-scope-grants-the-org"),
        pytest.param("team", "team", id="team-scope-grants-that-team"),
        pytest.param("private", None, id="private-scope-grants-nobody"),
    ],
)
async def test_node_grant_matches_the_object_visibility_scope(
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    scope_kind: str,
    expected_principal: str | None,
) -> None:
    """A chat's node has the audience the chat has — decided by the real decider.

    The assertion is not "a ``file_shares`` row exists": it is that an outsider
    to the object's audience is refused READ and a member of it is allowed, so
    inverting the grant derivation fails the test on both halves.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    team_id = await _team(
        files_session, parent=files_org.org_team_id, name=f"t{uuid.uuid4().hex[:6]}"
    )
    reader = await _member(files_session, files_org, teams=[team_id])
    outsider = await _member(files_session, files_org, teams=[])
    scope = {"org": "org", "team": f"team:{team_id}", "private": "private"}[scope_kind]

    obj = await _object(files_session, files_org, owner, scope=scope, commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        shares = await repo.shares_of(NodeId(node.id))
        kinds = {share.principal_kind for share in shares}
        assert kinds == (set() if expected_principal is None else {expected_principal})

        reader_allowed = await _may_read(
            repo, _ctx(files_org, reader), node, AccessFacts(team_ids=frozenset({team_id}))
        )
        outsider_allowed = await _may_read(
            repo, _ctx(files_org, outsider), node, AccessFacts(team_ids=frozenset())
        )
        owner_allowed = await _may_read(
            repo, _ctx(files_org, owner), node, AccessFacts(team_ids=frozenset())
        )
        await files_session.rollback()

    assert owner_allowed, "the creator reads their own object's node in every scope"
    if scope_kind == "org":
        assert reader_allowed and outsider_allowed
    elif scope_kind == "team":
        assert reader_allowed, "a member of the object's team reads it"
        assert not outsider_allowed, "someone outside the team does not"
    else:
        assert not reader_allowed and not outsider_allowed


# ---- names ----------------------------------------------------------------


@pytest.mark.parametrize("object_type", list(objects_bridge.POINTER_EXTENSIONS), ids=str)
def test_the_tree_name_and_the_mount_name_are_the_same_extension(object_type: str) -> None:
    """One extension per object type, spelled the same in both registries.

    The bridge names the node a member sees in the drive; the provider registry
    names the pointer a mount writes and inverts it to resolve the node back to
    its object type. If the two drift, ``object_type_of`` answers ``None`` for
    every node the bridge ever created and a mount can serve none of them.
    """
    assert MOUNT_EXTENSIONS[object_type] == f".{objects_bridge.POINTER_EXTENSIONS[object_type]}", (
        "the provider registry must spell the bridge's extension with a leading dot"
    )


def test_the_two_registries_cover_exactly_the_same_object_types() -> None:
    assert set(MOUNT_EXTENSIONS) == set(objects_bridge.POINTER_EXTENSIONS)


def test_a_bridge_named_node_resolves_back_to_its_object_type() -> None:
    """The round trip the mount depends on: name it, then read the type off it.

    This is the reason the two registries have to agree — the only input
    ``object_type_of`` gets is the name the bridge chose.
    """
    for object_type in objects_bridge.POINTER_EXTENSIONS:
        obj = WorkspaceObject(
            id=uuid.uuid4(),
            org_team_id=uuid.uuid4(),
            logical_id="l-1",
            namespace="workspace",
            type=object_type,
            title="Weekly rollup",
            version=1,
            status="ready",
            spec={},
            owner_user_id=uuid.uuid4(),
            visibility_scope="private",
        )
        node = FileNode(
            id=uuid.uuid4(),
            name=objects_bridge.pointer_name(obj),
            target_object_id=obj.id,
        )
        assert object_type_of(node) == object_type


@pytest.mark.parametrize(
    ("type", "title", "expected_suffix"),
    [
        pytest.param("chat", "Quarterly numbers", b"Quarterly numbers.alkerachat", id="chat-title"),
        pytest.param("query", "Top spend", b"Top spend.alkeraquery", id="query-title"),
        pytest.param("result", "Rollup", b"Rollup.alkeraresult", id="result-title"),
        pytest.param("board", "Ops", b"Ops.alkeraboard", id="board-title"),
        pytest.param("app", "Runbook", b"Runbook.alkeraapp", id="app-title"),
    ],
)
async def test_object_node_name_uses_the_registered_extension(
    files_session: AsyncSession,
    files_org: FilesOrg,
    type: str,
    title: str,
    expected_suffix: bytes,
) -> None:
    """The extension comes from the registry, per object type — not from the title."""
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, type=type, title=title)
    assert objects_bridge.pointer_name(obj) == expected_suffix


@pytest.mark.parametrize(
    ("title", "describe"),
    [
        pytest.param("", "empty", id="empty-title-falls-back-to-the-logical-id"),
        pytest.param("   ", "blank", id="blank-title-falls-back-to-the-logical-id"),
        pytest.param("...", "dots", id="dots-only-title-falls-back-to-the-logical-id"),
    ],
)
async def test_untitled_object_is_named_for_its_logical_id(
    files_session: AsyncSession, files_org: FilesOrg, title: str, describe: str
) -> None:
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, title=title)
    assert objects_bridge.pointer_name(obj) == obj.logical_id.encode() + b".alkerachat"


async def test_a_title_with_a_separator_never_becomes_two_path_components(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """A name is one path component; a title is prose and may hold anything."""
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, title="a/b\\c")
    name = objects_bridge.pointer_name(obj)
    assert b"/" not in name and b"\\" not in name
    assert name == b"a_b_c.alkerachat"


async def test_a_long_title_is_truncated_without_splitting_a_codepoint(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    """The boundary case: a multi-byte title cut at the stem budget still decodes."""
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, title="é" * 200)
    name = objects_bridge.pointer_name(obj)
    stem = name[: -len(b".alkerachat")]
    assert len(stem) <= 180
    assert stem.decode("utf-8") == "é" * (len(stem) // 2)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        pytest.param("quarter\tthree", b"quarter_three.alkerachat", id="a-tab"),
        pytest.param("line\nbreak", b"line_break.alkerachat", id="a-newline"),
        pytest.param("bell\x07ring", b"bell_ring.alkerachat", id="an-ascii-bell"),
        pytest.param("del\x7fete", b"del_ete.alkerachat", id="a-delete"),
        pytest.param("next\x85line", b"next_line.alkerachat", id="a-c1-control"),
        pytest.param("  padded  ", b"padded.alkerachat", id="surrounding-spaces"),
        # Trimmed, not swept: a title wrapped in tabs is named for what it
        # says. An interior tab is a character the title really carries.
        pytest.param("\ttabbed\t", b"tabbed.alkerachat", id="surrounding-tabs"),
        pytest.param("\t in \t out \t", b"in _ out.alkerachat", id="both-at-once"),
        pytest.param("report\u202egnp.exe", b"report_gnp.exe.alkerachat", id="a-bidi-override"),
        pytest.param("a\u2066b\u2069", b"a_b_.alkerachat", id="bidi-isolates"),
    ],
)
async def test_a_title_a_name_could_not_hold_is_repaired_and_not_refused(
    files_session: AsyncSession, files_org: FilesOrg, title: str, expected: bytes
) -> None:
    """A title is prose; the name minted from it has to satisfy the contract.

    The naming contract refuses a control character and a surrounding space, and
    a title is written by a person or generated by a model — so a stray tab in
    an auto-generated title would otherwise fail the whole chat create, inside
    the transaction, with nothing its owner could act on. Repaired rather than
    refused, and each control becomes its own ``_`` so two titles that differed
    only there still differ here.
    """
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, title=title)
    minted = objects_bridge.pointer_name(obj)
    assert minted == expected
    names.validate(minted)  # the whole point: the namespace will take it


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("a/b", id="separator"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\x00b", id="nul"),
        *(
            pytest.param(f"a{chr(cp)}b", id=f"control-{cp:02x}")
            for cp in (*range(0x01, 0x20), 0x7F, 0x80, 0x85, 0x9F)
        ),
        pytest.param(" ", id="one-space"),
        pytest.param("...", id="only-dots"),
        pytest.param(". . .", id="dots-and-spaces"),
        pytest.param("　", id="one-ideographic-space"),
        pytest.param("\t", id="one-tab"),
        pytest.param("é" * 400, id="two-byte-far-over-the-ceiling"),
        pytest.param("日" * 400, id="three-byte-far-over-the-ceiling"),
        pytest.param("a" * 400, id="ascii-far-over-the-ceiling"),
    ],
)
async def test_every_title_mints_a_name_the_namespace_accepts(
    files_org: FilesOrg, title: str
) -> None:
    """Swept across the hostile set rather than one case at a time.

    ``pointer_name`` feeds ``namespace.create`` directly, so a title it cannot
    reduce is a create that fails inside the object's own transaction. The set
    is every shape the contract refuses: a separator, a NUL, each control
    class, a title made only of characters that get stripped, and three well
    past the byte ceiling in one-, two- and three-byte scripts.

    The object is never persisted — Postgres will not hold a NUL in a text
    column, and ``pointer_name`` reads only the title, the type and the
    logical id.
    """
    row = WorkspaceObject(
        id=uuid.uuid4(),
        org_team_id=files_org.org_team_id,
        logical_id=f"obj-{uuid.uuid4().hex[:12]}",
        namespace="workspace",
        type="chat",
        title=title,
        owner_user_id=uuid.uuid4(),
        visibility_scope="private",
        spec={},
    )
    names.validate(objects_bridge.pointer_name(row))


# ---- delete directions ----------------------------------------------------


async def test_deleting_the_object_tombstones_its_node(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The node is trashed with the reason that says who trashed it and why."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        node_id = node.id
    await files_session.commit()

    async with repo.transaction():
        trashed = await objects_bridge.tombstone_for_object(repo, _ctx(files_org, owner), obj.id)
        assert trashed is not None
    await files_session.commit()

    row = (
        await files_session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()
    assert row is not None, "the node is trashed when its object is deleted"
    reason = (
        await files_session.execute(
            text("SELECT after FROM file_history WHERE node_id = :id AND kind = 'trash'"),
            {"id": node_id},
        )
    ).scalar_one()
    assert reason["metadata"]["reason"] == objects_bridge.REASON_OBJECT_DELETED


async def test_trashing_the_node_leaves_the_object_row_untouched(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The reverse direction does not exist: a file view cannot destroy a chat."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        node_id = node.id
    await files_session.commit()

    # A member trashes the node directly, the way the namespace does.
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": node_id}
        )
    await files_session.commit()

    live = (
        await files_session.execute(
            text("SELECT deleted_at, version FROM workspace_objects WHERE id = :id"),
            {"id": obj.id},
        )
    ).one()
    assert live.deleted_at == 0, "trashing a node never tombstones the object"
    assert live.version == 1


async def test_tombstone_is_a_no_op_when_the_object_has_no_node(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """An object made before this bridge existed must still be deletable."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner)
    async with repo.transaction():
        assert (
            await objects_bridge.tombstone_for_object(repo, _ctx(files_org, owner), obj.id) is None
        )


# ---- the update bump ------------------------------------------------------


async def test_bump_emits_exactly_one_outbox_row_in_the_callers_transaction(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """One edit, one announcement — and nothing at all if the caller rolls back.

    Two rows for one edit would make a sync client fetch the object twice; a row
    that survives a rollback would announce an edit that never happened.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        node_id = node.id
        before_etag = node.etag
    await files_session.commit()

    baseline = await _outbox_rows(files_session, node_id)

    async with repo.transaction():
        bumped = await objects_bridge.bump_for_object_update(repo, _ctx(files_org, owner), obj.id)
        assert bumped is not None
        assert bumped.etag == before_etag + 1
        assert await _outbox_rows(files_session, node_id, in_files_role=True) == baseline + 1, (
            "exactly one outbox row, written in the caller's own transaction"
        )
    await files_session.commit()

    assert await _outbox_rows(files_session, node_id) == baseline + 1

    # The same call, rolled back, leaves neither the row nor the bump behind.
    async with repo.transaction():
        await objects_bridge.bump_for_object_update(repo, _ctx(files_org, owner), obj.id)
        await files_session.rollback()

    assert await _outbox_rows(files_session, node_id) == baseline + 1, (
        "a rolled-back bump announces nothing"
    )
    etag_now = (
        await files_session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()
    assert etag_now == before_etag + 1


# ---- attachments ----------------------------------------------------------


async def test_link_attachment_records_through_the_callback_and_refuses_a_ghost(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Files checks the node is real and in the org; the chat writes its own spec."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner)
    recorded: list[tuple[uuid.UUID, NodeId]] = []

    async def record(chat_id: uuid.UUID, node_id: NodeId) -> None:
        recorded.append((chat_id, node_id))

    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo,
            _ctx(files_org, owner),
            obj,
            clock=FakeClock(EPOCH),
        )
        chat_id = uuid.uuid4()
        await objects_bridge.link_attachment(
            repo, _ctx(files_org, owner), chat_id, NodeId(node.id), record_reference=record
        )
        assert recorded == [(chat_id, NodeId(node.id))]

        with pytest.raises(NotFound):
            await objects_bridge.link_attachment(
                repo,
                _ctx(files_org, owner),
                chat_id,
                NodeId(uuid.uuid4()),
                record_reference=record,
            )
        assert len(recorded) == 1, "a node that does not exist is never handed to the chat"
        await files_session.rollback()


async def test_an_object_from_another_org_is_refused(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Any,
    repo: FilesRepo,
) -> None:
    """The repo's scope is the org; an object outside it never gets a node here."""
    await _skeleton(repo, files_org, files_session)
    other = await files_org_factory()
    owner = await _member(files_session, other, teams=[])
    obj = await _object(files_session, other, owner)
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await objects_bridge.node_for_object(repo, _ctx(files_org), obj, clock=FakeClock(EPOCH))


# ---- reading the type back off a name -------------------------------------


@pytest.mark.parametrize(
    ("name", "subtype", "expected"),
    [
        pytest.param(b"Weekly.alkerachat.template", "", "chat_template", id="template-suffix"),
        pytest.param(b"Weekly.alkerachat", "", "chat", id="chat-suffix"),
        pytest.param(b"Weekly.alkeraquery", "", "query", id="query-suffix"),
        # A name whose last dotted segment is a registered extension's TAIL is
        # not that extension: only the whole suffix counts.
        pytest.param(b"x.template", "", None, id="bare-template-is-no-extension"),
        pytest.param(
            b"x.template", "report:spec.json", "report:spec.json", id="bare-template-falls-back"
        ),
        # One character past the extension and it is a different name.
        pytest.param(b"a.alkerachat.templatex", "", None, id="near-miss-suffix"),
        pytest.param(
            b"a.alkerachat.templatex",
            "report:README.md",
            "report:README.md",
            id="near-miss-falls-back",
        ),
        # The longer registered suffix wins over the shorter one it contains.
        pytest.param(
            b"Weekly.alkerachat.template", "chat", "chat_template", id="longest-suffix-wins"
        ),
    ],
)
def test_the_object_type_is_read_off_the_longest_registered_suffix(
    name: bytes, subtype: str, expected: str | None
) -> None:
    """A pointer name carries its type in its suffix, and the suffix may hold a dot.

    ``.alkerachat.template`` is a chat template and ``.alkerachat`` a chat, so
    resolving on the last dot alone would read every template as an unnamed
    node and serve it the chat renderer. Anything that is not a whole
    registered suffix falls through to the node's own ``subtype``, which is
    what a derived member of a folder carries.
    """
    node = FileNode(id=uuid.uuid4(), name=name, target_object_id=uuid.uuid4(), subtype=subtype)
    assert object_type_of(node) == expected


def test_a_node_that_points_at_no_object_has_no_type() -> None:
    """The suffix is only read for a node that stands for an object."""
    node = FileNode(id=uuid.uuid4(), name=b"Weekly.alkerachat.template", target_object_id=None)
    assert object_type_of(node) is None


# ---- a folder-backed object is filed in its own place ----------------------


@pytest.mark.parametrize(
    ("object_type", "place"),
    [
        pytest.param("chat", drives.CHATS_NAME, id="chat"),
        pytest.param("chat_template", drives.CHAT_TEMPLATES_NAME, id="chat_template"),
        pytest.param("workspace", drives.CHATS_NAME, id="workspace"),
    ],
)
async def test_each_folder_object_is_filed_in_the_place_its_kind_names(
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    object_type: str,
    place: bytes,
) -> None:
    """A template saves next to the other templates, a chat next to the chats.

    Both places are ordinary folders directly under the owner's home, created
    on the way by whoever makes the first one, and which place a kind uses is
    read off its registration rather than branched on at each call site.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(files_session, files_org, owner, type=object_type, commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo, _ctx(files_org, owner), obj, clock=FakeClock(EPOCH)
        )
        assert node.parent_id is not None
        parent = await repo.node(NodeId(node.parent_id))
        assert parent is not None
        assert bytes(parent.name) == place
        assert parent.kind == "folder" and parent.target_object_id is None
        assert parent.parent_id is not None
        home = await repo.node(NodeId(parent.parent_id))
        assert home is not None and bytes(home.name) == str(owner).encode()
    await files_session.rollback()


async def test_a_template_is_born_with_its_files_and_a_derived_readme(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The shape a new chat is copied from, and the prose that explains it.

    ``scratch/`` is the working directory the new chat starts with, marked as
    a deliverable the way a chat's is. ``README.md`` is derived from the row —
    no bytes are stored — so editing the brief cannot leave the folder
    describing an older one. Nothing about the root is sealed: a template is
    material to share and copy, not a conversation to protect.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    obj = await _object(
        files_session, files_org, owner, type="chat_template", title="Weekly numbers", commit=False
    )
    obj.spec = {"brief": "Pull the weekly numbers."}
    await files_session.flush()
    async with repo.transaction():
        node = await objects_bridge.node_for_object(
            repo, _ctx(files_org, owner), obj, clock=FakeClock(EPOCH)
        )
        assert node.kind == "folder"
        assert await _flags_of(files_session, node.id) == 0, "a template carries no seal"
        children = {bytes(child.name): child for child in await repo.siblings(NodeId(node.id))}
        assert set(children) == {b"scratch", b"README.md"}
        assert await _flags_of(files_session, children[b"scratch"].id) & ARTIFACT_BIT
        readme = children[b"README.md"]
        assert readme.kind == "object"
        assert readme.subtype == "chat_template:README.md"
        assert readme.target_object_id == obj.id
    await files_session.rollback()


async def test_a_drop_on_a_template_lands_in_the_files_it_hands_a_new_chat(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """ "Put this in the template" means "give it to the chats started from it".

    A file left at the template's root would be invisible to every chat copied
    from it, so the drop resolves to the same working directory the copy reads.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    obj = await _object(files_session, files_org, owner, type="chat_template", commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(repo, ctx, obj, clock=FakeClock(EPOCH))
        namespace = Namespace(repo, ctx, FakeClock(EPOCH), ino=InoAllocator(repo))
        target = await objects_bridge.drop_target_for(repo, ctx, namespace, node)
        assert bytes(target.name) == b"scratch" and target.parent_id == node.id
    await files_session.rollback()


async def test_a_workspace_is_born_with_a_shared_tree_and_a_place_for_its_chats(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """``files/`` is the tree every chat in the workspace and every person
    shares: a deliverable, and where a drop on the workspace lands. ``.chats/``
    holds each chat's own folder and is neither: a file dropped on the
    workspace must never land among chat records. The root carries no seal; each
    chat folder under ``.chats/`` carries its own."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    obj = await _object(files_session, files_org, owner, type="workspace", commit=False)
    async with repo.transaction():
        node = await objects_bridge.node_for_object(repo, ctx, obj, clock=FakeClock(EPOCH))
        assert node.subtype == "workspace"
        assert bytes(node.name).endswith(b".alkeraworkspace")
        assert await _flags_of(files_session, node.id) == 0
        children = {bytes(child.name): child for child in await repo.siblings(NodeId(node.id))}
        assert await _flags_of(files_session, children[b"files"].id) & ARTIFACT_BIT
        assert not await _flags_of(files_session, children[b".chats"].id) & ARTIFACT_BIT
        namespace = Namespace(repo, ctx, FakeClock(EPOCH), ino=InoAllocator(repo))
        target = await objects_bridge.drop_target_for(repo, ctx, namespace, node)
        assert target.id == children[b"files"].id
        chats = await objects_bridge.workspace_chats_folder(repo, node)
        assert chats is not None and chats.id == children[b".chats"].id
    await files_session.rollback()


async def test_a_workspaces_files_are_looked_up_at_its_shared_tree(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The working-folder lookup reads each kind's own folder: a workspace
    answers with ``files/``, never with a child that merely carries a chat's
    working-directory name, and never with its ``.chats/`` records. A chat on
    the same page still answers with its own, in the same call."""
    assert objects_bridge.CHAT_SANDBOX_FOLDER is not None
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    workspace = await _object(files_session, files_org, owner, type="workspace", commit=False)
    chat = await _object(files_session, files_org, owner, type="chat", commit=False)
    async with repo.transaction():
        ws_node = await objects_bridge.node_for_object(repo, ctx, workspace, clock=FakeClock(EPOCH))
        chat_node = await objects_bridge.node_for_object(repo, ctx, chat, clock=FakeClock(EPOCH))
        decoy = await _folder_under(repo, ctx, ws_node, objects_bridge.CHAT_SANDBOX_FOLDER)
        children = {bytes(child.name): child for child in await repo.siblings(NodeId(ws_node.id))}
        shared = children[b"files"]
        chat_working = await objects_bridge.working_folder_node(repo, chat_node)
        assert chat_working is not None

        assert await objects_bridge.working_folder_nodes(
            files_session, [ws_node.id, chat_node.id]
        ) == {ws_node.id: shared.id, chat_node.id: chat_working.id}
        found = await objects_bridge.working_folder_node(repo, ws_node)
        assert found is not None and found.id == shared.id != decoy.id
        leased = await objects_bridge.lease_working_node(repo, ws_node)
        assert leased is not None and leased.id == shared.id

        # Trashed, the shared tree is gone: nothing stands in for it.
        await files_session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": shared.id}
        )
        assert await objects_bridge.working_folder_nodes(files_session, [ws_node.id]) == {}
        assert await objects_bridge.working_folder_node(repo, ws_node) is None
    await files_session.rollback()


async def test_only_a_workspace_folder_has_a_place_for_chats(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """A chat folder, or a plain folder somebody named ``.chats`` into, is not
    a workspace: a chat is never filed in a folder that merely carries the name."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    chat = await _object(files_session, files_org, owner, type="chat", commit=False)
    async with repo.transaction():
        chat_node = await objects_bridge.node_for_object(repo, ctx, chat, clock=FakeClock(EPOCH))
        namespace = Namespace(repo, ctx, FakeClock(EPOCH), ino=InoAllocator(repo))
        await namespace.create(
            DriveId(chat_node.drive_id),
            NodeId(chat_node.id),
            "folder",
            b".chats",
            attrs=NodeAttrs(mode=0o755),
        )
        assert await objects_bridge.workspace_chats_folder(repo, chat_node) is None
    await files_session.rollback()


@pytest.mark.parametrize(
    ("object_type", "sealed"),
    [
        pytest.param("chat", True, id="chat"),
        pytest.param("chat_template", False, id="chat_template"),
    ],
)
async def test_adopting_a_copied_folder_seals_only_what_a_conversation_owns(
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    object_type: str,
    sealed: bool,
) -> None:
    """The copy becomes the new object's node, with that kind's own flags.

    A copied chat is a conversation, so its bytes may not leave as a file. A
    copied template is material a member is meant to take away and start from,
    so it gains nothing — the asymmetry is the registration, not a branch at
    the copy site.
    """
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    obj = await _object(files_session, files_org, owner, type=object_type, commit=False)
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, ctx, owner, ino_allocator=InoAllocator(repo))
        copied = await _folder_under(repo, ctx, home, b"Copy of it")
        node = await objects_bridge.adopt_copied_folder_node(
            repo, ctx, copied, obj, clock=FakeClock(EPOCH)
        )
        assert node.subtype == object_type
        assert node.target_object_id == obj.id
        flags = await _flags_of(files_session, node.id)
        assert bool(flags & NO_DOWNLOAD_BIT) is sealed
        if not sealed:
            assert flags == 0
        children = {bytes(child.name) for child in await repo.siblings(NodeId(node.id))}
        assert b"scratch" in children, "the copy is re-minted whatever the source lacked"
    await files_session.rollback()


async def test_a_plain_folder_is_not_a_folder_backed_object(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The object behind the node decides, never its name or its kind alone."""
    await _skeleton(repo, files_org, files_session)
    owner = await _member(files_session, files_org, teams=[])
    ctx = _ctx(files_org, owner)
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, ctx, owner, ino_allocator=InoAllocator(repo))
        folder = await _folder_under(repo, ctx, home, b"Weekly.alkerachat.template")
        assert objects_bridge.folder_object_kind(folder) is None
        assert set(objects_bridge.FOLDER_OBJECT_KINDS) == {"chat", "chat_template", "workspace"}
    await files_session.rollback()
