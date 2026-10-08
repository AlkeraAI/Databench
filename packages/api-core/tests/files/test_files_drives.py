"""The org tree skeleton: idempotence under a race, and who can see what.

The skeleton is the part of Files that decides what a brand-new member sees the
first time they open the product, so the cases below are written from that
member's point of view: what a listing of the root shows them, what happens when
they address a folder that is not theirs, and what the drive refuses to build in
the first place.
"""

from __future__ import annotations

import uuid
from typing import Any, get_args

import pytest
from alkera_core.authz.chat_scope import scope_readable
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import acl_intern, drives
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import AccessFacts, effective_role
from alkera_core.files.authz.defaults import ORG_POLICY_OPEN, ORG_POLICY_RESTRICTED
from alkera_core.files.authz.grants import FilesGrantSource
from alkera_core.files.errors import ContainerReadOnly, InvalidRequest, NotFound
from alkera_core.files.history import NodeChangeReason
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.stores import FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesOrg

pytestmark = pytest.mark.asyncio


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
    """One configured store, the only thing ``ensure_org_drive`` needs from outside."""
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
    """A real user with real membership rows in every team in ``teams``."""
    user = User(
        id=uuid.uuid4(),
        home_org_team_id=org.org_team_id,
        email=f"m-{uuid.uuid4().hex[:12]}@files.test",
        email_domain="files.test",
        first_name="Fresh",
        last_name="Member",
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


async def _count(session: AsyncSession, sql: str, params: dict[str, Any]) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


def _facts(*, team_ids: list[uuid.UUID], org_admin: bool = False) -> AccessFacts:
    return AccessFacts(team_ids=frozenset(team_ids), org_admin=org_admin)


async def _reachable(
    repo: FilesRepo, ctx: ActingContext, node: FileNode, facts: AccessFacts
) -> bool:
    """Whether this caller may read one node, through the real decider."""
    drive = await repo.drive(DriveId(node.drive_id))
    assert drive is not None
    chain = await repo.chain(node)
    grants = await FilesGrantSource().grants_for(repo, node, chain)
    return effective_role(ctx, node, chain, grants, drive, facts).allows(FilesAction.READ)


async def _named(repo: FilesRepo, parent: FileNode, name: bytes) -> FileNode | None:
    for child in await repo.siblings(NodeId(parent.id)):
        if bytes(child.name) == name:
            return child
    return None


# ---- the skeleton ---------------------------------------------------------


async def test_ensure_org_drive_builds_the_whole_skeleton(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """One drive, a traversal-only root with no grants, and the three folders."""
    store_id = await _store(files_session)
    async with repo.transaction():
        drive = await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id
        )
        assert drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        assert root.traversal_only is True
        assert root.acl_id is None, "a grant on the root would trickle down to everything"
        assert root.path_ids.count(".") == 0, "the root's path is its own label alone"

        names = {bytes(child.name) for child in await repo.siblings(NodeId(root.id))}
        assert names == {drives.SHARED_NAME, drives.HOME_NAME, drives.TEAMS_NAME}

        shared = await _named(repo, root, drives.SHARED_NAME)
        home = await _named(repo, root, drives.HOME_NAME)
        teams = await _named(repo, root, drives.TEAMS_NAME)
        assert shared is not None and home is not None and teams is not None
        assert shared.traversal_only is False and shared.acl_id is not None
        assert home.traversal_only is True and home.acl_id is None
        assert teams.traversal_only is True and teams.acl_id is None


async def test_ensure_org_drive_is_idempotent(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Called twice it reads the drive it made, rather than building a second."""
    store_id = await _store(files_session)
    async with repo.transaction():
        first = await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id
        )
    async with repo.transaction():
        second = await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id
        )
    assert first.id == second.id
    assert first.root_node_id == second.root_node_id
    assert (
        await _count(
            files_session,
            "SELECT count(*) FROM file_nodes WHERE drive_id = :d",
            {"d": first.id},
        )
        == 4
    ), "the root and its three folders, once each"


async def test_the_domain_created_hook_fires_once_with_the_new_domains_id(
    files_org: FilesOrg, repo: FilesRepo, files_session: AsyncSession
) -> None:
    """The caller that holds a store stamps a domain's prefix as this
    deployment's from this hook, so it has to fire in the call that inserted the
    domain -- with that domain's id -- and never again for the same domain."""
    store_id = await _store(files_session)
    seen: list[uuid.UUID] = []

    async def stamp(domain_id: uuid.UUID) -> None:
        seen.append(domain_id)

    async with repo.transaction():
        drive = await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id, on_domain_created=stamp
        )
    async with repo.transaction():
        await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id, on_domain_created=stamp
        )

    assert seen == [drive.dedup_domain_id]


async def test_ensure_org_drive_refuses_a_foreign_org(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The org argument and the repo's scope must be the same tenant."""
    store_id = await _store(files_session)
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await drives.ensure_org_drive(repo, _ctx(files_org), uuid.uuid4(), store_id=store_id)


# ---- home and team folders ------------------------------------------------


async def test_ensure_home_folder_twice_returns_the_same_node(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        first = await drives.ensure_home_folder(repo, ctx, files_org.member_id)
    async with repo.transaction():
        second = await drives.ensure_home_folder(repo, ctx, files_org.member_id)
    assert first.id == second.id
    assert first.acl_id is not None, "a home folder is granted to its owner"
    assert bytes(first.name) == str(files_org.member_id).encode(), "stored under the member's id"
    assert first.subtype == drives.HOME_SUBTYPE
    assert drives.home_owner(first) == files_org.member_id


@pytest.mark.parametrize(
    "username",
    [
        pytest.param("a/b", id="separator"),
        pytest.param("a\x00b", id="nul"),
        pytest.param("a\tb", id="control"),
        pytest.param(" lead", id="leading-space"),
        pytest.param("trail ", id="trailing-space"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
        pytest.param("x" * 244, id="too-long"),
    ],
)
async def test_a_system_folder_name_the_grammar_refuses_is_refused_before_it_is_written(
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    username: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The folder-creation helper checks every name it is handed, not only the
    ones the database's separator floor would catch: a derivation that slips
    fails the request here instead of writing a node no machine can hold."""
    admin_ctx, _drive, container = await _skeleton_for(files_session, files_org, repo)
    monkeypatch.setattr(drives, "home_folder_name", lambda _user_id: username)
    async with repo.transaction():
        with pytest.raises(InvalidRequest, match="not a valid name"):
            await drives.ensure_home_folder(repo, admin_ctx, files_org.member_id)
    async with repo.transaction():
        assert await _named(repo, container, username.encode("utf-8")) is None


async def test_a_team_named_with_the_escape_character_keeps_it_escaped(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """``100% Club`` is a folder called ``100%25 Club``: the escape is always
    escaped, which is what keeps it injective -- a team called ``R/D`` and one
    called ``R%2FD`` must not be handed one folder."""
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    club = await _team(files_session, parent=files_org.org_team_id, name="100% Club")
    slash = await _team(files_session, parent=files_org.org_team_id, name="R/D")
    literal = await _team(files_session, parent=files_org.org_team_id, name="R%2FD")
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        club_folder = await drives.ensure_team_folder(repo, ctx, club, team_name="100% Club")
        slash_folder = await drives.ensure_team_folder(repo, ctx, slash, team_name="R/D")
        literal_folder = await drives.ensure_team_folder(repo, ctx, literal, team_name="R%2FD")
    assert bytes(club_folder.name) == b"100%25 Club"
    assert bytes(slash_folder.name) == b"R%2FD"
    assert bytes(literal_folder.name) == b"R%252FD"
    assert slash_folder.id != literal_folder.id


async def test_ensure_team_folder_refuses_an_empty_name(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """An unnamed team folder would be a folder nobody can address."""
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    team = await _team(files_session, parent=files_org.org_team_id, name="Growth")
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await drives.ensure_team_folder(repo, ctx, team, team_name="")


async def test_ensure_home_folder_before_the_drive_is_refused(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """A home folder in an org with no drive is a caller that skipped a step.

    Building the drive here would need a ``store_id`` this path does not have,
    so it raises instead of guessing one.
    """
    async with repo.transaction():
        with pytest.raises(NotFound):
            await drives.ensure_home_folder(repo, _ctx(files_org), files_org.member_id)


async def test_team_folders_are_flat_siblings(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """A sub-team's folder sits beside its parent's, never inside it.

    Nesting would let the tree hand a parent-team member reach into a sub-team,
    which the membership model does not grant.
    """
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    parent_team = await _team(files_session, parent=files_org.org_team_id, name="Platform")
    child_team = await _team(files_session, parent=parent_team, name="Storage")
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        parent_folder = await drives.ensure_team_folder(
            repo, ctx, parent_team, team_name=f"t{parent_team}"[:24]
        )
        child_folder = await drives.ensure_team_folder(
            repo, ctx, child_team, team_name=f"t{child_team}"[:24]
        )
    assert child_folder.parent_id == parent_folder.parent_id
    assert child_folder.depth == parent_folder.depth


# ---- what a fresh member can see -----------------------------------------


async def test_a_fresh_member_sees_shared_their_home_and_their_teams_only(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The AC's central case, asserted through the real readable predicate.

    An org with an admin, two members and a sub-team. One member belongs to the
    sub-team; the other does not. Every folder the first member may read is
    checked, and — the half that catches an over-eager grant — every folder they
    may not.
    """
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    sub_team = await _team(files_session, parent=files_org.org_team_id, name="Storage")
    other_team = await _team(files_session, parent=files_org.org_team_id, name="Growth")
    alice = await _member(files_session, files_org, teams=[files_org.org_team_id, sub_team])
    bob = await _member(files_session, files_org, teams=[files_org.org_team_id, other_team])

    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        alice_home = await drives.ensure_home_folder(repo, admin_ctx, alice)
        bob_home = await drives.ensure_home_folder(repo, admin_ctx, bob)
        alice_team = await drives.ensure_team_folder(
            repo, admin_ctx, sub_team, team_name=f"t{sub_team}"[:24]
        )
        bob_team = await drives.ensure_team_folder(
            repo, admin_ctx, other_team, team_name=f"t{other_team}"[:24]
        )

    alice_ctx = _ctx(files_org, alice)
    alice_facts = _facts(team_ids=[files_org.org_team_id, sub_team])
    async with repo.transaction():
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        shared = await _named(repo, root, drives.SHARED_NAME)
        assert shared is not None

        assert await _reachable(repo, alice_ctx, shared, alice_facts)
        assert await _reachable(repo, alice_ctx, alice_home, alice_facts)
        assert await _reachable(repo, alice_ctx, alice_team, alice_facts)

        assert not await _reachable(repo, alice_ctx, bob_home, alice_facts), (
            "another member's home is not readable"
        )
        assert not await _reachable(repo, alice_ctx, bob_team, alice_facts), (
            "a team she is not in is not readable"
        )

        # The listing agrees with the predicate: `home/` shows her home alone.
        home_container = await _named(repo, root, drives.HOME_NAME)
        assert home_container is not None
        listed = await drives.traversal_children(repo, alice_ctx, home_container, facts=alice_facts)
        assert [node.id for node in listed] == [alice_home.id]

        # And the root shows the three signposts, because they are traversal-only
        # containers plus the one space she is granted on. Without all three she
        # has no way into her own home: the root is the only listing a client
        # can start from, and `home/` is the only node her folder hangs off.
        root_listing = await drives.traversal_children(repo, alice_ctx, root, facts=alice_facts)
        assert {bytes(node.name) for node in root_listing} == {
            drives.SHARED_NAME,
            drives.HOME_NAME,
            drives.TEAMS_NAME,
        }


async def test_another_members_home_resolves_to_the_not_found_class(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Addressed directly, an unreachable home must be indistinguishable from absent.

    The engine turns "no role" into an opaque 404, so what is asserted here is
    the input to that: the caller gets no READ, so the route has nothing to
    reveal.
    """
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    alice = await _member(files_session, files_org, teams=[files_org.org_team_id])
    bob = await _member(files_session, files_org, teams=[files_org.org_team_id])
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        bob_home = await drives.ensure_home_folder(repo, admin_ctx, bob)
        access_for_alice = await _reachable(
            repo, _ctx(files_org, alice), bob_home, _facts(team_ids=[files_org.org_team_id])
        )
        access_for_bob = await _reachable(
            repo, _ctx(files_org, bob), bob_home, _facts(team_ids=[files_org.org_team_id])
        )
    assert access_for_bob, "the owner reads their own home"
    assert not access_for_alice


async def test_a_parent_team_member_cannot_read_a_sub_team_folder(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Membership does not descend, so neither does the folder's audience."""
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    parent_team = await _team(files_session, parent=files_org.org_team_id, name="Platform")
    sub_team = await _team(files_session, parent=parent_team, name="Storage")
    parent_only = await _member(
        files_session, files_org, teams=[files_org.org_team_id, parent_team]
    )
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        sub_folder = await drives.ensure_team_folder(
            repo, admin_ctx, sub_team, team_name=f"t{sub_team}"[:24]
        )
        reachable = await _reachable(
            repo,
            _ctx(files_org, parent_only),
            sub_folder,
            _facts(team_ids=[files_org.org_team_id, parent_team]),
        )
    assert not reachable


async def test_an_org_admin_reaches_every_folder_by_descent(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The negative cases above must not be an accident of nobody having access."""
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    member = await _member(files_session, files_org, teams=[files_org.org_team_id])
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, admin_ctx, member)
        reachable = await _reachable(
            repo, admin_ctx, home, _facts(team_ids=[files_org.org_team_id], org_admin=True)
        )
    assert reachable


# ---- the traversal-only rules --------------------------------------------


@pytest.mark.parametrize(
    "container",
    [
        pytest.param(None, id="drive-root"),
        pytest.param(drives.HOME_NAME, id="home"),
        pytest.param(drives.TEAMS_NAME, id="Teams"),
    ],
)
async def test_a_traversal_only_container_refuses_every_direct_child(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo, container: bytes | None
) -> None:
    """A signpost takes no direct write at all — a folder no more than a file.

    The earlier rule let a *folder* through, which is how the live drive grew a
    folder called ``Test`` beside the real homes: a node under ``home/`` that
    carries no grant of its own, that nobody but an org admin can reach and
    that the owner's Files surface never shows. The only writer under a
    signpost is the system's own ensure, which no longer asks this rule.
    """
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        node = root if container is None else await _named(repo, root, container)
        assert node is not None
        with pytest.raises(ContainerReadOnly) as refused:
            drives.assert_traversal_rules(node)
    assert refused.value.code == "files.container_readonly"
    assert refused.value.status == 422


async def test_a_folder_that_is_not_a_signpost_takes_children(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The negative twin: the refusal is the container's, not every folder's."""
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        shared = await _named(repo, root, drives.SHARED_NAME)
        assert shared is not None
        drives.assert_traversal_rules(shared)


async def test_the_ensure_still_builds_a_home_under_the_signpost_it_closed(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The system path is the one writer left under ``home/``.

    Closing the container to every caller would be a drive with no homes in it
    if the ensure went through the same rule, so this pins that it does not.
    """
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    member = await _member(files_session, files_org, teams=[files_org.org_team_id])
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, ctx, member)
    assert home.kind == "folder"
    assert not home.traversal_only


async def test_a_grant_on_a_traversal_only_folder_is_refused(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """A grant on ``/`` or ``home/`` would trickle down to every member's home."""
    store_id = await _store(files_session)
    ctx = _ctx(files_org)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        home = await _named(repo, root, drives.HOME_NAME)
        shared = await _named(repo, root, drives.SHARED_NAME)
        assert home is not None and shared is not None
        for container in (root, home):
            with pytest.raises(InvalidRequest):
                drives.assert_grantable(container)
        # The twin: /Shared is grantable, so the refusal is about traversal-only
        # containers and not about every folder.
        drives.assert_grantable(shared)


# ---- /Shared reproduces the chat audience ---------------------------------


@pytest.mark.parametrize(
    "org_policy",
    [
        pytest.param(ORG_POLICY_OPEN, id="open-org"),
        pytest.param(ORG_POLICY_RESTRICTED, id="restricted-org"),
    ],
)
async def test_shared_reproduces_the_org_scoped_chat_audience(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo, org_policy: str
) -> None:
    """Every principal that reads an ``org``-scoped chat reads ``/Shared``.

    ``scope_readable`` is the oracle: the same principals are put to both, and
    the two answers must agree for every one of them. Under either sharing
    policy ``/Shared`` is org-wide — the policy decides writer vs reader, not
    who may look.
    """
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    team = await _team(files_session, parent=files_org.org_team_id, name="Growth")
    member = await _member(files_session, files_org, teams=[files_org.org_team_id, team])
    async with repo.transaction():
        await drives.ensure_org_drive(
            repo, admin_ctx, files_org.org_team_id, store_id=store_id, org_policy=org_policy
        )
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        shared = await _named(repo, root, drives.SHARED_NAME)
        assert shared is not None

        principals = [
            (files_org.admin_id, [files_org.org_team_id], True),
            (files_org.member_id, [files_org.org_team_id], False),
            (member, [files_org.org_team_id, team], False),
        ]
        for user_id, team_ids, is_admin in principals:
            oracle = scope_readable(
                is_org_admin=is_admin,
                team_ids=set(team_ids),
                visibility_scope="org",
                is_owner=False,
            )
            files = await _reachable(
                repo,
                _ctx(files_org, user_id),
                shared,
                _facts(team_ids=team_ids, org_admin=is_admin),
            )
            assert files == oracle, f"/Shared disagrees with the org audience for {user_id}"


async def test_a_team_folder_reproduces_the_team_scoped_chat_audience(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """For ``team:<id>`` only that team reads — folder and oracle must agree."""
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    team = await _team(files_session, parent=files_org.org_team_id, name="Growth")
    inside = await _member(files_session, files_org, teams=[files_org.org_team_id, team])
    outside = await _member(files_session, files_org, teams=[files_org.org_team_id])
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        folder = await drives.ensure_team_folder(repo, admin_ctx, team, team_name=f"t{team}"[:24])
        for user_id, team_ids, is_admin in (
            (inside, [files_org.org_team_id, team], False),
            (outside, [files_org.org_team_id], False),
            (files_org.admin_id, [files_org.org_team_id], True),
        ):
            oracle = scope_readable(
                is_org_admin=is_admin,
                team_ids=set(team_ids),
                visibility_scope=f"team:{team}",
                is_owner=False,
            )
            files = await _reachable(
                repo,
                _ctx(files_org, user_id),
                folder,
                _facts(team_ids=team_ids, org_admin=is_admin),
            )
            assert files == oracle


async def test_a_home_folder_reproduces_the_private_chat_audience(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """For ``private`` only the owner (and an org admin) reads."""
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    owner = await _member(files_session, files_org, teams=[files_org.org_team_id])
    other = await _member(files_session, files_org, teams=[files_org.org_team_id])
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, admin_ctx, owner)
        for user_id, is_admin in ((owner, False), (other, False), (files_org.admin_id, True)):
            oracle = scope_readable(
                is_org_admin=is_admin,
                team_ids={files_org.org_team_id},
                visibility_scope="private",
                is_owner=user_id == owner,
            )
            files = await _reachable(
                repo,
                _ctx(files_org, user_id),
                home,
                _facts(team_ids=[files_org.org_team_id], org_admin=is_admin),
            )
            assert files == oracle


# ---- the ACL interning helper --------------------------------------------


async def test_interning_the_same_body_twice_yields_one_row(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Identical permission sets share a row; a different set gets its own."""
    from alkera_core.files.authz.defaults import DriveFolder, default_acl

    body = default_acl(
        DriveFolder.SHARED,
        org_policy=ORG_POLICY_RESTRICTED,
        org_team_id=files_org.org_team_id,
    )
    other = default_acl(
        DriveFolder.HOME,
        org_policy=ORG_POLICY_RESTRICTED,
        org_team_id=files_org.org_team_id,
        subject_id=files_org.member_id,
    )
    async with repo.transaction():
        first = await acl_intern.intern(repo, body)
        second = await acl_intern.intern(repo, body)
        third = await acl_intern.intern(repo, other)
    assert first == second
    assert third != first


async def test_interning_is_order_insensitive(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The same grants in a different order are the same permission set."""
    from alkera_core.files.authz.defaults import DriveFolder, default_acl

    body = list(
        default_acl(
            DriveFolder.SHARED,
            org_policy=ORG_POLICY_RESTRICTED,
            org_team_id=files_org.org_team_id,
        )
    )
    assert len(body) > 1, "the fixture must have something to reorder"
    async with repo.transaction():
        forward = await acl_intern.intern(repo, body)
        backward = await acl_intern.intern(repo, list(reversed(body)))
    assert forward == backward


# ---- the mutation record --------------------------------------------------


async def test_every_skeleton_folder_writes_history_and_an_outbox_row(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The tree is auditable from the moment it exists, and the event is ids only."""
    store_id = await _store(files_session)
    async with repo.transaction():
        drive = await drives.ensure_org_drive(
            repo, _ctx(files_org), files_org.org_team_id, store_id=store_id
        )
    node_ids = [
        row
        for row in (
            await files_session.execute(
                text("SELECT id FROM file_nodes WHERE drive_id = :d"), {"d": drive.id}
            )
        ).scalars()
    ]
    assert len(node_ids) == 4
    for node_id in node_ids:
        assert (
            await _count(
                files_session,
                "SELECT count(*) FROM file_history WHERE node_id = :n AND kind = 'create'",
                {"n": node_id},
            )
            == 1
        ), f"no history row for {node_id}"
    payloads = list(
        (
            await files_session.execute(
                text(
                    "SELECT payload FROM event_outbox "
                    "WHERE type = 'file_node.changed' AND org_id = :o"
                ),
                {"o": files_org.org_team_id},
            )
        ).scalars()
    )
    assert len(payloads) == 4
    reasons = set(get_args(NodeChangeReason))
    for payload in payloads:
        assert set(payload) == {"node_id", "drive_id", "version", "parent_id", "reason"}, (
            "an outbox payload carries ids only — never a name or a path"
        )
        # Every value is an id, a counter, or one word out of a closed
        # vocabulary: nothing a caller could steer into carrying a name.
        assert uuid.UUID(payload["node_id"]) and uuid.UUID(payload["drive_id"])
        assert isinstance(payload["version"], int)
        assert payload["parent_id"] is None or uuid.UUID(payload["parent_id"])
        assert payload["reason"] is None or payload["reason"] in reasons


async def test_the_ino_of_every_skeleton_node_is_unique(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Inos are what a mount addresses a node by, so no two may collide."""
    store_id = await _store(files_session)
    async with repo.transaction():
        drive = await drives.ensure_org_drive(
            repo,
            _ctx(files_org),
            files_org.org_team_id,
            store_id=store_id,
            ino_allocator=InoAllocator(repo, block=2),
        )
    inos = list(
        (
            await files_session.execute(
                text("SELECT ino FROM file_nodes WHERE drive_id = :d"), {"d": drive.id}
            )
        ).scalars()
    )
    assert len(inos) == len(set(inos)) == 4


# ---- a file in the container is not a home ---------------------------------


async def _dropped_into(
    session: AsyncSession,
    repo: FilesRepo,
    drive: Any,
    container: FileNode,
    *,
    by: uuid.UUID,
    name: bytes = b"Receipt-2505-0695 (1).pdf",
    kind: str = "file",
) -> FileNode:
    """A node ``by`` wrote straight into a traversal-only container.

    The library refuses a file there at :func:`drives.assert_traversal_rules`,
    but the write routes never asked it, and an org admin sits at ``manager`` on
    the container by descent — so the live drive holds exactly such rows: a
    receipt, a folder called ``Test``. Seeded as a row, committed on its own so
    it is OLDER than whatever the test writes next.
    """
    async with repo.transaction():
        ino = await InoAllocator(repo).allocate(DriveId(drive.id))
    node = FileNode(
        id=uuid.uuid4(),
        ino=ino,
        drive_id=drive.id,
        org_team_id=drive.org_team_id,
        parent_id=container.id,
        kind=kind,
        name=name,
        name_display=name.decode(),
        name_key=name.decode().casefold(),
        path_ids=f"{container.path_ids}.{ino_label(ino)}",
        depth=container.depth + 1,
        created_by=by,
    )
    session.add(node)
    await session.commit()
    return node


async def _file_dropped_into(
    session: AsyncSession, repo: FilesRepo, drive: Any, container: FileNode, *, by: uuid.UUID
) -> FileNode:
    return await _dropped_into(session, repo, drive, container, by=by)


async def _skeleton_for(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> tuple[ActingContext, Any, FileNode]:
    """The drive and its ``home/`` container, built as the org's admin."""
    store_id = await _store(files_session)
    admin_ctx = _ctx(files_org)
    async with repo.transaction():
        await drives.ensure_org_drive(repo, admin_ctx, files_org.org_team_id, store_id=store_id)
        drive = await repo.drive_for_org()
        assert drive is not None and drive.root_node_id is not None
        root = await repo.node(NodeId(drive.root_node_id))
        assert root is not None
        container = await _named(repo, root, drives.HOME_NAME)
        assert container is not None
    return admin_ctx, drive, container


def test_a_home_folder_name_is_the_member_id_and_nothing_else() -> None:
    """The stored name carries nothing a person could be identified by."""
    member = uuid.uuid4()
    assert drives.home_folder_name(member) == str(member)


def _row(**overrides: Any) -> FileNode:
    owner = overrides.pop("created_by", uuid.UUID("6b1f0b1e-0000-4000-8000-000000000002"))
    values: dict[str, Any] = {
        "kind": "folder",
        "subtype": drives.HOME_SUBTYPE,
        "depth": 2,
        "created_by": owner,
        "name": str(owner).encode(),
    }
    values.update(overrides)
    return FileNode(**values)


OWNER = uuid.UUID("6b1f0b1e-0000-4000-8000-000000000002")


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(_row(), OWNER, id="a-home"),
        pytest.param(_row(subtype=None), None, id="not-stamped"),
        pytest.param(_row(subtype="chat"), None, id="another-subtype"),
        pytest.param(_row(depth=3), None, id="a-copy-deeper-in-the-tree"),
        pytest.param(_row(depth=1), None, id="at-the-container-level"),
        pytest.param(_row(kind="file"), None, id="a-file"),
        pytest.param(_row(name=b"alice"), None, id="named-otherwise"),
        pytest.param(_row(name=str(uuid.uuid4()).encode()), None, id="named-after-someone-else"),
        pytest.param(_row(created_by=None, name=str(OWNER).encode()), None, id="no-creator"),
    ],
)
def test_home_owner_answers_only_for_a_home(row: FileNode, expected: uuid.UUID | None) -> None:
    """Stamp, place, kind and name all have to agree; any one off is an ordinary folder."""
    assert drives.home_owner(row) == expected


async def test_a_file_the_member_dropped_into_the_container_is_not_their_home(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """With only the receipt under ``/home``, the member has NO home yet.

    The founder's shape: nothing ever wrote her folder, but a receipt she
    uploaded into the container carries her id. Finding it would hand the
    caller a PDF as "my files" — and, because the find answers, the ensure
    that would have written her real folder never runs.
    """
    admin_ctx, drive, container = await _skeleton_for(files_session, files_org, repo)
    receipt = await _file_dropped_into(files_session, repo, drive, container, by=files_org.admin_id)

    async with repo.transaction():
        lookup = await drives.lookup_home(repo, files_org.admin_id, drive=drive)
    found = lookup.home
    assert found is None, f"a {found.kind!r} named {bytes(found.name)!r} was taken for a home"
    assert [row.id for row in lookup.strays] == [receipt.id]

    # The ensure then writes her folder beside the receipt, and the find is that folder.
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, admin_ctx, files_org.admin_id)
        after = await drives.find_home_folder(repo, files_org.admin_id, drive=drive)
    assert after is not None
    assert after.id == home.id != receipt.id
    assert after.kind == "folder"


async def test_a_folder_the_member_made_under_the_container_is_not_their_home(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """A folder called ``Test`` she made first is a stray, not her home.

    The live shape: the founder had made ``/home/Test`` before her first visit,
    so the oldest folder of hers under the container was ``Test`` and the drive
    took it for her home — her real one was never ensured because a candidate
    existed. A home is identified by its name as well as its creator, so with
    ``Test`` alone she has no home, and once ``admin`` is ensured beside it the
    answer is ``admin`` however much younger it is.
    """
    admin_ctx, drive, container = await _skeleton_for(files_session, files_org, repo)
    test_folder = await _dropped_into(
        files_session, repo, drive, container, by=files_org.admin_id, name=b"Test", kind="folder"
    )

    async with repo.transaction():
        before = await drives.lookup_home(repo, files_org.admin_id, drive=drive)
    assert before.home is None, "the drive took the founder's Test folder for her home"
    assert [row.id for row in before.strays] == [test_folder.id]

    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, admin_ctx, files_org.admin_id)
        after = await drives.lookup_home(repo, files_org.admin_id, drive=drive)
    assert after.home is not None and after.home.id == home.id != test_folder.id
    assert bytes(after.home.name) == str(files_org.admin_id).encode()
    # The stray is still hers, still beside the home, and still not a home.
    assert [row.id for row in after.strays] == [test_folder.id]


async def test_a_namesake_folder_somebody_else_made_is_not_the_members_home(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The name alone is not the identity either: ``/home/<bob's id>`` made by
    the admin is neither Bob's home (he did not create it) nor the admin's (it
    is not her id) — and it is a stray of the admin's."""
    _admin_ctx, drive, container = await _skeleton_for(files_session, files_org, repo)
    bob = await _member(files_session, files_org, teams=[files_org.org_team_id])
    planted = await _dropped_into(
        files_session,
        repo,
        drive,
        container,
        by=files_org.admin_id,
        name=str(bob).encode(),
        kind="folder",
    )

    async with repo.transaction():
        bobs = await drives.lookup_home(repo, bob, drive=drive)
        admins = await drives.lookup_home(repo, files_org.admin_id, drive=drive)
    assert bobs.home is None and bobs.strays == ()
    assert admins.home is None
    assert [row.id for row in admins.strays] == [planted.id]


async def test_find_home_folder_answers_the_folder_not_the_older_file_beside_it(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """Receipt first (oldest), a teammate's home, then the caller's own: the
    answer is the caller's FOLDER, by kind, by owner and by name — not the
    first child the caller's id happens to stamp, and not the teammate's."""
    admin_ctx, drive, container = await _skeleton_for(files_session, files_org, repo)
    bob = await _member(files_session, files_org, teams=[files_org.org_team_id])
    receipt = await _file_dropped_into(files_session, repo, drive, container, by=files_org.admin_id)
    async with repo.transaction():
        bob_home = await drives.ensure_home_folder(repo, admin_ctx, bob)
        mine = await drives.ensure_home_folder(repo, admin_ctx, files_org.admin_id)

    async with repo.transaction():
        found = await drives.find_home_folder(repo, files_org.admin_id, drive=drive)
        bobs = await drives.find_home_folder(repo, bob, drive=drive)
    assert found is not None and found.id == mine.id
    assert found.kind == "folder" and found.created_by == files_org.admin_id
    assert found.id not in {receipt.id, bob_home.id}
    assert bobs is not None and bobs.id == bob_home.id


async def test_a_folder_named_after_the_members_address_is_no_longer_their_home(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """The address-derived spelling a home used to carry is not an identity any
    more: a folder of that name is a stray, and the home is ensured by id."""
    admin_ctx, drive, container = await _skeleton_for(files_session, files_org, repo)
    alice = await _member(files_session, files_org, teams=[files_org.org_team_id])
    legacy = await _dropped_into(
        files_session, repo, drive, container, by=alice, name=b"alice", kind="folder"
    )
    async with repo.transaction():
        before = await drives.lookup_home(repo, alice, drive=drive)
    assert before.home is None
    assert [row.id for row in before.strays] == [legacy.id]
    async with repo.transaction():
        home = await drives.ensure_home_folder(repo, admin_ctx, alice)
    assert home.id != legacy.id
    assert bytes(home.name) == str(alice).encode()
