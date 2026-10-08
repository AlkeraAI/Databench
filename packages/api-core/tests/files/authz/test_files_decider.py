"""Every step of the decider, one invariant per test.

The nodes and drives are real rows built by ``files_factory``, so a change to a
column default or a constraint shows up here rather than in a hand-rolled
object that agrees with the test by construction. The decision itself is pure,
which is what lets the flag table be exhaustive.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files.authz.actions import FilesAction as A
from alkera_core.files.authz.decider import (
    CHAT_SUBTYPE,
    HELD_BIT,
    NO_DOWNLOAD_BIT,
    NO_RESHARE_BIT,
    WORKSPACE_SUBTYPE,
    AccessFacts,
    LadderDecider,
    NodeFlag,
    effective_role,
)
from alkera_core.files.authz.grants import Grant, GrantOrigin, ace_to_grant
from alkera_core.files.authz.grants import Principal as GrantPrincipal
from alkera_core.files.authz.ladder import DEFAULT_LADDER, RoleLadder
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

ROLES = ("reader", "commenter", "writer", "manager", "owner")


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org_id,
            credential=CredentialKind.JWT,
        )
    )


def _agent_ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    user = ActingPrincipal(kind=PrincipalKind.USER, id=str(user_id), org_id=org_id)
    agent = ActingPrincipal(
        kind=PrincipalKind.AGENT,
        id="session-1",
        org_id=org_id,
        credential=CredentialKind.AGENT_HEADER,
    )
    return ActingContext(
        acting_principal=agent, delegating_user=user, delegation_chain=(user, agent)
    )


def _user_grant(user_id: uuid.UUID, role: str) -> Grant:
    return Grant(principal=GrantPrincipal(kind="user", id=user_id), role=role)


@pytest.fixture
async def scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, FileNode, Sequence[FileNode]]:
    """A drive with ``a/b.txt``: the node under test plus its real chain."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    node = made["a/b.txt"]
    return drive, node, (made["a"], node)


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_a_direct_user_grant_yields_exactly_that_rung(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, role)], drive, AccessFacts()
    )

    assert access.in_org is True
    assert access.role == role
    assert access.allowed_actions == frozenset(a.value for a in DEFAULT_LADDER.actions_for(role))


async def test_no_grant_is_no_access(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, [], drive, AccessFacts())

    assert access.role is None
    assert access.allowed_actions == frozenset()
    assert access.in_org is True


async def test_a_node_in_another_org_is_not_in_org(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    stranger = _ctx(uuid.uuid4(), files_org.member_id)

    access = effective_role(
        stranger, node, chain, [_user_grant(files_org.member_id, "owner")], drive, AccessFacts()
    )

    assert access.in_org is False
    assert access.role is None
    assert access.allowed_actions == frozenset()


# -- flags ---------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_no_download_drops_export_for_every_role_and_keeps_read(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    node.flags |= NO_DOWNLOAD_BIT
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, role)], drive, AccessFacts()
    )

    assert A.EXPORT.value not in access.allowed_actions
    assert A.READ.value in access.allowed_actions
    assert NodeFlag.NO_DOWNLOAD.value in access.flags


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_no_reshare_drops_share_for_every_role(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    node.flags |= NO_RESHARE_BIT
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, role)], drive, AccessFacts()
    )

    assert A.SHARE.value not in access.allowed_actions
    # It narrows sharing and nothing else: a manager still writes.
    if role in ("writer", "manager", "owner"):
        assert A.WRITE.value in access.allowed_actions


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_held_drops_write_and_delete_for_every_role(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    node.flags |= HELD_BIT
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, role)], drive, AccessFacts()
    )

    assert A.WRITE.value not in access.allowed_actions
    assert A.DELETE.value not in access.allowed_actions
    assert A.READ.value in access.allowed_actions


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_locked_drops_write_for_everyone_but_the_holder(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    node.state = "locked"
    ctx = _ctx(files_org.org_team_id, files_org.member_id)
    grants = [_user_grant(files_org.member_id, role)]
    writes = role in ("writer", "manager", "owner")

    someone_else = effective_role(
        ctx, node, chain, grants, drive, AccessFacts(lock_holder_id=uuid.uuid4())
    )
    the_holder = effective_role(
        ctx, node, chain, grants, drive, AccessFacts(lock_holder_id=files_org.member_id)
    )

    assert A.WRITE.value not in someone_else.allowed_actions
    assert (A.WRITE.value in the_holder.allowed_actions) is writes


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
async def test_frozen_leaves_only_the_read_only_actions(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, role: str
) -> None:
    drive, node, chain = scene
    drive.frozen_reason = "over_quota"
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, role)], drive, AccessFacts()
    )

    assert access.allowed_actions <= {
        A.READ.value,
        A.EXPORT.value,
        A.LEASE_REQUEST.value,
        A.COPY.value,
    }
    assert A.READ.value in access.allowed_actions
    assert NodeFlag.FROZEN.value in access.flags


async def test_frozen_and_no_download_together_leave_metadata_only(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    drive.frozen_reason = "teardown"
    node.flags |= NO_DOWNLOAD_BIT
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx, node, chain, [_user_grant(files_org.member_id, "owner")], drive, AccessFacts()
    )

    assert access.allowed_actions == frozenset({A.READ.value, A.LEASE_REQUEST.value, A.COPY.value})


# -- principals ----------------------------------------------------------


async def test_a_team_grant_reaches_a_member_through_materialized_memberships(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    team_id = uuid.uuid4()
    grants = [Grant(principal=GrantPrincipal(kind="team", id=team_id), role="writer")]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    member = effective_role(ctx, node, chain, grants, drive, AccessFacts(team_ids={team_id}))
    outsider = effective_role(ctx, node, chain, grants, drive, AccessFacts())

    assert member.role == "writer"
    assert outsider.role is None


async def test_an_org_grant_reaches_every_member_of_that_org(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    grants = [Grant(principal=GrantPrincipal(kind="org", id=files_org.org_team_id), role="reader")]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)
    other_org = [Grant(principal=GrantPrincipal(kind="org", id=uuid.uuid4()), role="owner")]

    assert effective_role(ctx, node, chain, grants, drive, AccessFacts()).role == "reader"
    assert effective_role(ctx, node, chain, other_org, drive, AccessFacts()).role is None


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("link", id="link"),
        pytest.param("service", id="service"),
        pytest.param("public", id="public"),
        pytest.param("something-the-engine-invented", id="unknown"),
    ],
)
async def test_an_unmodelled_principal_kind_is_ignored_not_a_crash(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg, kind: str
) -> None:
    drive, node, chain = scene
    grants = [
        Grant(principal=GrantPrincipal(kind=kind, id=files_org.member_id), role="owner"),
        _user_grant(files_org.member_id, "reader"),
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts())

    assert access.role == "reader"


async def test_the_maximum_over_several_grants_wins(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    team_id = uuid.uuid4()
    grants = [
        _user_grant(files_org.member_id, "reader"),
        Grant(
            principal=GrantPrincipal(kind="team", id=team_id),
            role="manager",
            origin=GrantOrigin.inherited(chain[0].id),
        ),
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts(team_ids={team_id}))

    assert access.role == "manager"


# -- expiry and conditions ------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(timedelta(seconds=1), "writer", id="one-second-before-expiry"),
        pytest.param(timedelta(0), None, id="exactly-at-expiry"),
        pytest.param(timedelta(seconds=-1), None, id="one-second-after-expiry"),
    ],
)
async def test_a_grant_stops_counting_at_its_expiry(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    offset: timedelta,
    expected: str | None,
) -> None:
    drive, node, chain = scene
    grants = [
        Grant(
            principal=GrantPrincipal(kind="user", id=files_org.member_id),
            role="writer",
            expires_at=EPOCH + offset,
        )
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts(now=EPOCH))

    assert access.role == expected


async def test_an_admins_only_team_grant_reaches_admins_only(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    team_id = uuid.uuid4()
    grants = [
        Grant(
            principal=GrantPrincipal(kind="team", id=team_id),
            role="manager",
            conditions={"team_role": "admin"},
        )
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    admin = effective_role(
        ctx,
        node,
        chain,
        grants,
        drive,
        AccessFacts(team_ids={team_id}, team_admin_ids={team_id}),
    )
    plain = effective_role(ctx, node, chain, grants, drive, AccessFacts(team_ids={team_id}))

    assert admin.role == "manager"
    assert plain.role is None


async def test_a_condition_this_build_cannot_evaluate_fails_closed(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """An ABAC clause written for the later engine must never widen access here
    by being unreadable."""
    drive, node, chain = scene
    grants = [
        Grant(
            principal=GrantPrincipal(kind="user", id=files_org.member_id),
            role="owner",
            conditions={"device_posture": "managed"},
        )
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    assert effective_role(ctx, node, chain, grants, drive, AccessFacts()).role is None


# -- overrides and confinement -------------------------------------------


@pytest.mark.parametrize(
    ("held_role", "expected"),
    [
        pytest.param(None, "manager", id="no-grant-floors-at-manager"),
        pytest.param("reader", "manager", id="reader-is-raised"),
        pytest.param("owner", "owner", id="owner-is-not-lowered"),
    ],
)
async def test_an_org_admin_is_at_least_manager_on_their_org_drive(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    held_role: str | None,
    expected: str,
) -> None:
    drive, node, chain = scene
    grants = [] if held_role is None else [_user_grant(files_org.admin_id, held_role)]
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts(org_admin=True))

    assert access.role == expected


async def test_an_org_admin_override_still_obeys_the_flags(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    node.flags |= NO_DOWNLOAD_BIT | HELD_BIT
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(ctx, node, chain, [], drive, AccessFacts(org_admin=True))

    assert access.role == "manager"
    assert A.EXPORT.value not in access.allowed_actions
    assert A.WRITE.value not in access.allowed_actions


async def test_an_agent_never_shares_even_as_owner_inside_its_lease(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, node, chain = scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    grants = [_user_grant(files_org.member_id, "owner")]

    access = effective_role(
        ctx, node, chain, grants, drive, AccessFacts(is_agent=True, leased_subtree=chain[0].id)
    )

    assert A.WRITE.value in access.allowed_actions
    assert A.SHARE.value not in access.allowed_actions


@pytest.mark.parametrize(
    "leased",
    [pytest.param("elsewhere", id="another-subtree"), pytest.param(None, id="no-lease")],
)
async def test_an_agent_outside_its_leased_subtree_gets_nothing(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    leased: str | None,
) -> None:
    drive, node, chain = scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    subtree = uuid.uuid4() if leased == "elsewhere" else None

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(is_agent=True, leased_subtree=subtree),
    )

    assert access.allowed_actions == frozenset()
    # The delegating user, acting directly, still reaches it.
    direct = effective_role(
        _ctx(files_org.org_team_id, files_org.member_id),
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(),
    )
    assert A.WRITE.value in direct.allowed_actions


async def test_the_lease_confines_by_the_derived_chain_not_by_the_node_id(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """A lease on an ancestor covers everything beneath it."""
    drive, node, chain = scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)

    on_ancestor = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "writer")],
        drive,
        AccessFacts(is_agent=True, leased_subtree=chain[0].id),
    )
    on_the_node = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "writer")],
        drive,
        AccessFacts(is_agent=True, leased_subtree=node.id),
    )

    assert A.WRITE.value in on_ancestor.allowed_actions
    assert A.WRITE.value in on_the_node.allowed_actions


async def test_a_decider_built_on_another_ladder_uses_that_ladder(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The seam proof: a ladder handed in as data decides, with no change to
    anything that calls ``effective_role``."""
    drive, node, chain = scene
    decider = LadderDecider(RoleLadder((("auditor", frozenset({A.READ, A.EXPORT})),)))
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "auditor")],
        drive,
        AccessFacts(),
        decider=decider,
    )

    assert access.role == "auditor"
    assert access.allowed_actions == frozenset({A.READ.value, A.EXPORT.value})


async def test_a_traversal_container_is_readable_by_a_member_with_no_grant(
    files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The signpost rule: `/`, `home/` and `Teams/` carry no grants, so without
    it a member who is not an org admin has no role on the drive root and the
    whole surface answers the opaque 404 — no way in at all.

    READ is all the rule adds. The container must stay unwritable and
    ungrantable, because a member who could write into the org root could plant
    a folder every other member sees.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("signpost/", drive=drive)
    node = made["signpost"]
    node.traversal_only = True
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, (node,), [], drive, AccessFacts())

    assert access.allowed_actions == frozenset({A.READ.value})
    assert access.role is None


async def test_an_ordinary_folder_with_no_grant_stays_unreadable(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The rule is keyed on the flag, not on being in the org.

    Same caller, same empty grant list, one column different: a plain node
    yields nothing. Were the READ unconditional, every node in the org would be
    world-readable and the grant model would mean nothing.
    """
    drive, node, chain = scene
    assert node.traversal_only is False
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, [], drive, AccessFacts())

    assert access.allowed_actions == frozenset()


async def test_a_traversal_container_in_another_org_is_still_nothing(
    files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The cross-org gate runs first and the rule never reaches past it.

    A signpost is listable by every member *of its own org*; a stranger's drive
    root would otherwise name that org's teams and members to anyone who
    guessed the id.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("signpost/", drive=drive)
    node = made["signpost"]
    node.traversal_only = True
    stranger = _ctx(uuid.uuid4(), files_org.member_id)

    access = effective_role(stranger, node, (node,), [], drive, AccessFacts())

    assert access.in_org is False
    assert access.allowed_actions == frozenset()


# ---------------------------------------------------------------------------
# The box that holds a chat folder's lease reads the chat's bytes back down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "held", "kept"),
    [
        pytest.param("agent", "on-the-folder", True, id="the-holder-box-under-its-lease"),
        pytest.param("agent", "on-the-file", True, id="the-holder-box-on-the-node-itself"),
        pytest.param("agent", "elsewhere", False, id="the-box-on-a-folder-it-does-not-hold"),
        pytest.param("agent", None, False, id="the-box-with-no-lease"),
        pytest.param("person", "on-the-folder", False, id="a-person-holding-the-same-lease"),
    ],
)
async def test_no_download_yields_only_to_the_agent_holding_the_lease_over_the_node(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    caller: str,
    held: str | None,
    kept: bool,
) -> None:
    """A chat folder is sealed for everyone — except the box running the chat,
    which must pull the folder it leases into the agent's working directory.

    The escape is the fence a write already carries: a PROVEN machine whose
    live lease covers the node keeps ``EXPORT``. A person with the very same
    lease does not (a mount is not a download), and the box on a folder it
    does not hold, or holding nothing, is refused exactly as before.
    """
    drive, node, chain = scene
    node.flags |= NO_DOWNLOAD_BIT
    ctx = (
        _agent_ctx(files_org.org_team_id, files_org.member_id)
        if caller == "agent"
        else _ctx(files_org.org_team_id, files_org.member_id)
    )
    roots = {
        "on-the-folder": frozenset({chain[0].id}),
        "on-the-file": frozenset({node.id}),
        "elsewhere": frozenset({uuid.uuid4()}),
        None: frozenset(),
    }[held]

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(
            is_agent=caller == "agent",
            leased_subtree=chain[0].id,
            agent_machine_id="box-a" if caller == "agent" else None,
            held_leases=roots,
        ),
    )

    assert A.READ.value in access.allowed_actions
    assert (A.EXPORT.value in access.allowed_actions) is kept
    assert NodeFlag.NO_DOWNLOAD.value in access.flags


# ---------------------------------------------------------------------------
# A proven box is the machine; only the chat's OWN box is that chat's machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "elsewhere", "kept"),
    [
        pytest.param("box-a", "none", True, id="its-own-chat"),
        pytest.param("box-a", "unbound", True, id="a-chat-no-box-has-taken"),
        pytest.param("box-a", "this-chat", False, id="a-chat-another-box-runs"),
        pytest.param("box-a", "another-chat", True, id="another-chat-is-not-this-one"),
        pytest.param(None, "none", False, id="an-assertion-nobody-verified"),
    ],
)
async def test_the_machines_actions_need_the_chat_this_box_actually_runs(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    machine: str | None,
    elsewhere: str,
    kept: bool,
) -> None:
    """One box credential serves every chat that box runs, so "this caller is a
    machine" is answered identically on a colleague's sleeping conversation.
    What separates the two is the chat's own binding, and it is read off the
    CHAIN: the folder that carries the binding is the chat above the file, not
    the file the box is writing. A chat nothing has taken yet is bound to no
    machine and stays open to the first box that takes it.
    """
    drive, node, chain = scene
    chat = chain[0]
    chat.subtype = "chat"
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    bound = {
        "none": frozenset[uuid.UUID](),
        "unbound": frozenset[uuid.UUID](),
        "this-chat": frozenset({chat.id}),
        "another-chat": frozenset({uuid.uuid4()}),
    }[elsewhere]

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(
            is_agent=True,
            leased_subtree=chat.id,
            agent_machine_id=machine,
            chats_bound_elsewhere=bound,
        ),
    )

    assert (A.WRITE.value in access.allowed_actions) is kept
    assert (A.LEASE.value in access.allowed_actions) is kept
    assert (A.SNAPSHOT.value in access.allowed_actions) is kept
    # Whatever the answer, the operator's own reach is untouched: the binding
    # narrows what acting AS the machine buys, never what the human may see.
    assert A.READ.value in access.allowed_actions
    assert access.chat_bound_elsewhere is (elsewhere == "this-chat")


async def test_a_folder_that_is_no_chat_is_never_asked_whose_box_runs_it(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The two facts have to agree before anything narrows. An ordinary shared
    folder a box mounts is nobody's conversation, so even a fact naming it
    leaves the machine's actions alone — the rule is scoped to chats, and a
    stale id cannot turn an ordinary mount read-only."""
    drive, node, chain = scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(
            is_agent=True,
            leased_subtree=chain[0].id,
            agent_machine_id="box-a",
            chats_bound_elsewhere=frozenset({chain[0].id}),
        ),
    )

    assert A.WRITE.value in access.allowed_actions
    assert access.in_chat_subtree is False


@pytest.mark.parametrize(
    ("is_agent", "machine", "opens"),
    [
        pytest.param(True, "box-a", True, id="a-verified-box-holds-it"),
        pytest.param(True, None, False, id="an-assertion-nobody-verified-holds-nothing"),
        pytest.param(False, None, False, id="a-person-who-mounted-it-is-not-the-box"),
    ],
)
async def test_only_a_proven_machine_holds_a_lease_past_a_seal(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    is_agent: bool,
    machine: str | None,
    opens: bool,
) -> None:
    """Holding the lease is the ONE way bytes leave a sealed chat, so what
    counts as holding it is the narrowest thing that works.

    The agent assertion is two headers on the caller's own session and a box's
    machine id is published on every chat it serves, so "an agent that sent the
    headers" is any member who read the id off a row. The escape opens for a
    verified machine and for nothing else — not for an unverified assertion,
    and not for the person who mounted the same folder on their laptop, whose
    download is a download.
    """
    drive, node, chain = scene
    node.flags |= NO_DOWNLOAD_BIT
    ctx = (
        _agent_ctx(files_org.org_team_id, files_org.member_id)
        if is_agent
        else _ctx(files_org.org_team_id, files_org.member_id)
    )

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(
            is_agent=is_agent,
            leased_subtree=chain[0].id if is_agent else None,
            agent_machine_id=machine,
            held_leases=frozenset({chain[0].id}),
        ),
    )

    assert (A.EXPORT.value in access.allowed_actions) is opens


async def test_the_held_lease_never_widens_past_export(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """Holding the lease answers one question — may the bytes come down — and
    nothing else: a held node stays held for the holder, and an agent still
    never shares."""
    drive, node, chain = scene
    node.flags |= NO_DOWNLOAD_BIT | HELD_BIT
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        node,
        chain,
        [_user_grant(files_org.member_id, "owner")],
        drive,
        AccessFacts(
            is_agent=True,
            leased_subtree=chain[0].id,
            agent_machine_id="box-a",
            held_leases=frozenset({chain[0].id}),
        ),
    )

    assert A.EXPORT.value in access.allowed_actions
    assert A.WRITE.value not in access.allowed_actions
    assert A.DELETE.value not in access.allowed_actions
    assert A.SHARE.value not in access.allowed_actions


# -- the org-admin floor stops at a chat's folder -----------------------------


@pytest.fixture
async def chat_scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, dict[str, FileNode]]:
    """``home/Chats/quarterly/notes.txt`` where ``quarterly`` is a chat's
    folder — marked by its ``subtype``, which is what the decider reads."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        "home/ home/Chats/ home/Chats/quarterly/ home/Chats/quarterly/notes.txt", drive=drive
    )
    made["home/Chats/quarterly"].subtype = CHAT_SUBTYPE
    return drive, made


def _chain(made: dict[str, FileNode], path: str) -> Sequence[FileNode]:
    parts = path.split("/")
    return tuple(made["/".join(parts[: n + 1])] for n in range(len(parts) - 1))


@pytest.mark.parametrize(
    "where", ["home/Chats/quarterly", "home/Chats/quarterly/notes.txt"], ids=["folder", "inside"]
)
@pytest.mark.parametrize(
    ("held_role", "expected"),
    [
        pytest.param(None, None, id="no-grant-is-no-access"),
        pytest.param("reader", "reader", id="a-granted-reader-is-not-raised"),
        pytest.param("owner", "owner", id="the-owner-is-not-lowered"),
    ],
)
async def test_the_floor_does_not_reach_into_a_chat_folder_by_default(
    chat_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    held_role: str | None,
    expected: str | None,
) -> None:
    """Without the deployment's say-so an org admin is, inside a chat's folder,
    exactly what the chat's owner made them: nothing, or the rung granted."""
    drive, made = chat_scene
    node, chain = made[where], _chain(made, where)
    grants = [] if held_role is None else [_user_grant(files_org.admin_id, held_role)]
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts(org_admin=True))

    assert access.role == expected
    assert access.in_chat_subtree is True
    if expected is None:
        assert access.allowed_actions == frozenset()


@pytest.mark.parametrize(
    "where", ["home/Chats/quarterly", "home/Chats/quarterly/notes.txt"], ids=["folder", "inside"]
)
@pytest.mark.parametrize(
    ("held_role", "expected"),
    [
        pytest.param(None, "manager", id="no-grant-floors-at-manager"),
        pytest.param("reader", "manager", id="reader-is-raised"),
        pytest.param("owner", "owner", id="owner-is-not-lowered"),
    ],
)
async def test_the_deployment_that_opens_the_chat_lets_the_floor_in(
    chat_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    held_role: str | None,
    expected: str,
) -> None:
    drive, made = chat_scene
    node, chain = made[where], _chain(made, where)
    grants = [] if held_role is None else [_user_grant(files_org.admin_id, held_role)]
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(
        ctx, node, chain, grants, drive, AccessFacts(org_admin=True, org_admin_reaches_chats=True)
    )

    assert access.role == expected


async def test_outside_a_chat_folder_the_floor_is_unchanged(
    chat_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """The narrowing is about chat folders and nothing else: the ``Chats``
    folder beside them, and the home above, still floor at ``manager``."""
    drive, made = chat_scene
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)
    for where in ("home", "home/Chats"):
        access = effective_role(
            ctx, made[where], _chain(made, where), [], drive, AccessFacts(org_admin=True)
        )
        assert access.role == "manager", where
        assert access.in_chat_subtree is False


async def test_a_member_who_is_not_an_admin_is_untouched_by_the_chat_rule(
    chat_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    drive, made = chat_scene
    where = "home/Chats/quarterly/notes.txt"
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [_user_grant(files_org.member_id, "writer")],
        drive,
        AccessFacts(),
    )

    assert access.role == "writer"


async def test_an_admins_expiring_read_inside_a_chat_is_on_loan(
    chat_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """The floor was what made an admin's read "standing", so with the floor
    suppressed inside a chat an admin holding only an expiring grant there
    reads on loan — and the copy rules treat it as such. Outside the chat the
    floor stands and the read is not on loan, as before."""
    drive, made = chat_scene
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)
    expiring = Grant(
        principal=GrantPrincipal(kind="user", id=files_org.admin_id),
        role="reader",
        expires_at=EPOCH + timedelta(days=1),
    )
    facts = AccessFacts(org_admin=True, now=EPOCH)

    inside = "home/Chats/quarterly/notes.txt"
    on_loan = effective_role(ctx, made[inside], _chain(made, inside), [expiring], drive, facts)
    assert on_loan.role == "reader"
    assert on_loan.read_via_conditional_grant is True

    outside = "home/Chats"
    standing = effective_role(ctx, made[outside], _chain(made, outside), [expiring], drive, facts)
    assert standing.role == "manager"
    assert standing.read_via_conditional_grant is False


# -- the box running a chat holds a rung of its own on the chat's folder ------


def _box_facts(
    made: dict[str, FileNode],
    *,
    runs: bool,
    machine: str | None = "box-a",
    holds: str | None = None,
) -> AccessFacts:
    """An org admin's box: the operator is an org admin (so the floor would
    apply outside chats), the assertion was verified as ``machine`` unless it
    is ``None``, ``runs`` says whether the chat folder is one it runs, and
    ``holds`` names a folder whose live lease the request proved it holds."""
    chat = made["home/Chats/quarterly"].id
    return AccessFacts(
        org_admin=True,
        is_agent=True,
        leased_subtree=made["home"].id,
        agent_machine_id=machine,
        held_leases=frozenset({made[holds].id}) if holds is not None else frozenset(),
        chats_run_here=frozenset({chat}) if runs else frozenset(),
        chats_bound_elsewhere=frozenset() if runs else frozenset({chat}),
    )


@pytest.mark.parametrize(
    "where", ["home/Chats/quarterly", "home/Chats/quarterly/notes.txt"], ids=["folder", "inside"]
)
@pytest.mark.parametrize(
    ("runs", "machine", "expected"),
    [
        pytest.param(True, "box-a", "writer", id="the-chats-own-box-holds-the-machine-rung"),
        pytest.param(False, "box-a", None, id="another-boxs-chat-is-nothing"),
        pytest.param(True, None, None, id="an-unverified-assertion-is-nothing"),
    ],
)
async def test_the_box_running_a_chat_holds_its_own_rung_there(
    chat_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    runs: bool,
    machine: str | None,
    expected: str | None,
) -> None:
    """The floor stops at the chat folder for the box as for its operator; what
    reaches in is the chat's binding, and only for a machine that was proven."""
    drive, made = chat_scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [],
        drive,
        _box_facts(made, runs=runs, machine=machine),
    )

    assert access.role == expected
    if expected is None:
        assert access.allowed_actions == frozenset()
    else:
        assert {A.READ.value, A.WRITE.value, A.LEASE.value, A.SNAPSHOT.value} <= set(
            access.allowed_actions
        )
        assert A.SHARE.value not in access.allowed_actions
        assert A.LEASE_FORCE.value not in access.allowed_actions
        assert A.DELETE.value not in access.allowed_actions
        assert access.read_via_conditional_grant is False


@pytest.mark.parametrize(
    "where", ["home/Chats/quarterly", "home/Chats/quarterly/notes.txt"], ids=["folder", "inside"]
)
@pytest.mark.parametrize(
    ("machine", "holds", "expected"),
    [
        pytest.param("box-a", "home/Chats/quarterly", "writer", id="the-departing-holder-finishes"),
        pytest.param("box-a", None, None, id="a-box-with-no-lease-is-nothing"),
        pytest.param("box-a", "another", None, id="a-lease-over-another-folder-buys-nothing"),
        pytest.param(None, "home/Chats/quarterly", None, id="an-unverified-holder-is-nothing"),
    ],
)
async def test_the_box_holding_a_chats_live_lease_keeps_its_rung_after_the_chat_moves(
    chat_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    machine: str | None,
    holds: str | None,
    expected: str | None,
) -> None:
    """The binding decides who may take a chat's folder; the lease decides who
    may finish on it. A chat placement moved to another box leaves the box it
    left holding the folder's live lease and the only copy of its last turn's
    writes, and its hand-back — the item read, the push, the release — must
    land as the holder's, not be turned away as a stranger's. The rung is the
    lease's alone: a box with no lease, a lease over some folder outside the
    chat's chain, or a holder whose assertion nobody verified is confined as
    before."""
    drive, made = chat_scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.admin_id)
    facts = _box_facts(
        made, runs=False, machine=machine, holds=None if holds == "another" else holds
    )
    if holds == "another":
        facts = replace(facts, held_leases=frozenset({uuid.uuid4()}))

    access = effective_role(ctx, made[where], _chain(made, where), [], drive, facts)

    assert access.role == expected
    if expected is None:
        assert access.allowed_actions == frozenset()
        if machine is not None:
            assert access.chat_bound_elsewhere is True, "told it is the wrong machine, as before"
    else:
        assert {A.READ.value, A.WRITE.value, A.LEASE.value, A.SNAPSHOT.value} <= set(
            access.allowed_actions
        )
        assert A.SHARE.value not in access.allowed_actions
        assert A.LEASE_FORCE.value not in access.allowed_actions
        assert access.chat_bound_elsewhere is False, "the holder is finishing, not intruding"


async def test_the_machine_rung_does_not_lower_what_the_operator_holds(
    chat_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """A box on its operator's own chat keeps the operator's rung: the machine
    rung is a floor for the chat's box, never a ceiling."""
    drive, made = chat_scene
    where = "home/Chats/quarterly"
    ctx = _agent_ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [_user_grant(files_org.admin_id, "owner")],
        drive,
        _box_facts(made, runs=True),
    )

    assert access.role == "owner"


async def test_the_machine_rung_stops_at_the_chat_it_runs(
    chat_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """Running a chat says nothing about the folders around it: outside the
    chat the box has what its operator has, and a person holding the same
    facts is not a machine at all."""
    drive, made = chat_scene
    facts = _box_facts(made, runs=True)
    box = _agent_ctx(files_org.org_team_id, files_org.member_id)
    person = _ctx(files_org.org_team_id, files_org.member_id)

    beside = effective_role(box, made["home/Chats"], _chain(made, "home/Chats"), [], drive, facts)
    assert beside.role == "manager", "the operator's floor, not the machine rung"

    member_facts = AccessFacts(
        is_agent=False, chats_run_here=facts.chats_run_here, agent_machine_id="box-a"
    )
    where = "home/Chats/quarterly"
    as_person = effective_role(person, made[where], _chain(made, where), [], drive, member_facts)
    assert as_person.role is None


# -- a box on its own machine credential ---------------------------------


def _machine_ctx(
    operator_org: uuid.UUID, machine_id: uuid.UUID, *, served: uuid.UUID | None
) -> ActingContext:
    """A dedicated box minted in ``operator_org`` and assigned to ``served``;
    ``served=None`` is a box that serves nothing but its operator's org."""
    return ActingContext.for_machine(
        machine_id=machine_id,
        credential_id=uuid.uuid4(),
        org_id=operator_org,
        label="box",
        served_org_ids=frozenset() if served is None else frozenset({served}),
    )


async def test_a_box_serving_the_nodes_org_is_in_it_and_runs_its_own_chat(
    files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The tenancy floor is the platform's ``serves``, not the box's own org:
    a box minted in the operator org and assigned to this one is IN the org
    for the drive's purposes, and on a folder of a chat bound to its machine
    it holds the machine rung with nothing a person granted."""
    drive = await files_factory.drive()
    made = await files_factory.tree("chat.alkerachat/ chat.alkerachat/out.txt", drive=drive)
    folder, node = made["chat.alkerachat"], made["chat.alkerachat/out.txt"]
    folder.subtype = CHAT_SUBTYPE
    machine_id = uuid.uuid4()
    box = _machine_ctx(uuid.uuid4(), machine_id, served=files_org.org_team_id)
    facts = AccessFacts(
        is_agent=True,
        agent_machine_id=str(machine_id),
        leased_subtree=folder.id,
        chats_run_here=frozenset({folder.id}),
    )

    access = effective_role(box, node, (folder,), [], drive, facts)

    assert access.in_org is True
    assert access.role == "writer"
    assert A.WRITE.value in access.allowed_actions
    assert A.LEASE.value in access.allowed_actions
    assert A.SHARE.value not in access.allowed_actions
    assert access.in_chat_subtree is True
    assert access.chat_bound_elsewhere is False


async def test_a_box_that_does_not_serve_the_nodes_org_is_not_in_it(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """Assigned to some other org, or to none: the same facts that admit the
    box above admit nothing here, whatever grants the node carries."""
    drive, node, chain = scene
    machine_id = uuid.uuid4()
    facts = AccessFacts(
        is_agent=True,
        agent_machine_id=str(machine_id),
        leased_subtree=drive.root_node_id,
        chats_run_here=frozenset({node.id}),
    )
    for box in (
        _machine_ctx(uuid.uuid4(), machine_id, served=uuid.uuid4()),
        _machine_ctx(uuid.uuid4(), machine_id, served=None),
    ):
        access = effective_role(
            box, node, chain, [_user_grant(files_org.member_id, "owner")], drive, facts
        )
        assert access.in_org is False
        assert access.role is None
        assert access.allowed_actions == frozenset()


async def test_a_box_in_the_nodes_org_outside_its_chats_holds_nothing(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """In the org, proven, and on a node no chat of its own governs: no rung,
    no action — a box's reach is the binding's, never the drive's."""
    drive, node, chain = scene
    box = _machine_ctx(uuid.uuid4(), uuid.uuid4(), served=files_org.org_team_id)
    facts = AccessFacts(
        is_agent=True, agent_machine_id=box.acting_principal.id, leased_subtree=drive.root_node_id
    )

    access = effective_role(box, node, chain, [], drive, facts)

    assert access.in_org is True
    assert access.role is None
    assert access.allowed_actions == frozenset()


# -- the floor stops at a workspace's folder too ---------------------------------


@pytest.fixture
async def workspace_scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, dict[str, FileNode]]:
    """``home/Chats/pricing/files/q3.csv`` where ``pricing`` is a workspace's own
    folder and ``files`` the shared tree its chats work in."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        "home/ home/Chats/ home/Chats/pricing/ home/Chats/pricing/files/ "
        "home/Chats/pricing/files/q3.csv",
        drive=drive,
    )
    made["home/Chats/pricing"].subtype = WORKSPACE_SUBTYPE
    return drive, made


@pytest.mark.parametrize(
    "where",
    ["home/Chats/pricing", "home/Chats/pricing/files", "home/Chats/pricing/files/q3.csv"],
    ids=["folder", "shared-tree", "a-file-in-it"],
)
@pytest.mark.parametrize(
    ("reaches", "expected"),
    [
        pytest.param(False, None, id="by-default-no-floor"),
        pytest.param(True, "manager", id="the-deployment-opens-it"),
    ],
)
async def test_the_floor_stops_at_a_workspaces_folder_like_a_chats(
    workspace_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    reaches: bool,
    expected: str | None,
) -> None:
    """An org admin nobody shared a workspace with is a stranger to its whole
    tree, the way the workspace door refuses them, unless the deployment lets
    admins into private chats."""
    drive, made = workspace_scene
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [],
        drive,
        AccessFacts(org_admin=True, org_admin_reaches_chats=reaches),
    )

    assert access.role == expected
    if expected is None:
        assert access.allowed_actions == frozenset()


async def test_beside_a_workspace_folder_the_floor_is_unchanged(
    workspace_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    drive, made = workspace_scene
    ctx = _ctx(files_org.org_team_id, files_org.admin_id)
    access = effective_role(
        ctx, made["home/Chats"], _chain(made, "home/Chats"), [], drive, AccessFacts(org_admin=True)
    )
    assert access.role == "manager"


# -- expiry is judged even when the caller resolved no clock ----------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(timedelta(hours=1), "writer", id="not-yet-expired"),
        pytest.param(timedelta(hours=-1), None, id="expired"),
    ],
)
async def test_a_caller_with_no_clock_is_judged_at_the_wall_clock(
    scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    offset: timedelta,
    expected: str | None,
) -> None:
    drive, node, chain = scene
    grants = [
        Grant(
            principal=GrantPrincipal(kind="user", id=files_org.member_id),
            role="writer",
            expires_at=datetime.now(UTC) + offset,
        )
    ]
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, node, chain, grants, drive, AccessFacts())

    assert access.role == expected


@pytest.mark.parametrize(
    ("raw", "kept", "expires_at"),
    [
        pytest.param(None, True, None, id="no-expiry"),
        pytest.param(EPOCH.isoformat(), True, EPOCH, id="the-iso-string-an-interned-body-stores"),
        pytest.param(EPOCH, True, EPOCH, id="a-datetime-from-file-shares"),
        pytest.param("2026-01-01T00:00:00", True, EPOCH, id="a-naive-string-is-utc"),
        pytest.param("next tuesday", False, None, id="unreadable-drops-the-grant"),
        pytest.param(12, False, None, id="a-number-drops-the-grant"),
    ],
)
def test_an_ace_keeps_its_expiry_whichever_way_it_was_stored(
    raw: object, kept: bool, expires_at: datetime | None
) -> None:
    """The cached ACL body stores an expiry as an ISO string. Read as no expiry
    at all, an expiring share would never run out on the cached path."""
    ace = {
        "principal_kind": "user",
        "principal_id": str(uuid.uuid4()),
        "role": "writer",
        "expires_at": raw,
    }

    grant = ace_to_grant(ace, node_id=uuid.uuid4())

    assert (grant is not None) == kept
    if grant is not None:
        assert grant.expires_at == expires_at


# -- the box running a workspace holds its rung by the binding, never the floor --


@pytest.fixture
async def box_workspace_scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, dict[str, FileNode]]:
    """``home/Pricing/files/notes.md`` beside ``home/Pricing/.chats/c1/m.json``,
    where ``Pricing`` is a native workspace's folder and ``c1`` a member's."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        "home/ home/Pricing/ home/Pricing/files/ home/Pricing/files/notes.md "
        "home/Pricing/.chats/ home/Pricing/.chats/c1/ home/Pricing/.chats/c1/m.json",
        drive=drive,
    )
    made["home/Pricing"].subtype = WORKSPACE_SUBTYPE
    made["home/Pricing/.chats/c1"].subtype = CHAT_SUBTYPE
    return drive, made


def _workspace_box_facts(
    made: dict[str, FileNode], *, runs: bool, machine: str | None = "box-a"
) -> AccessFacts:
    """An org admin's box, as for a chat: the floor would apply outside a
    chat, the assertion verified unless ``machine`` is ``None``."""
    workspace = made["home/Pricing"].id
    return AccessFacts(
        org_admin=True,
        is_agent=True,
        leased_subtree=made["home"].id,
        agent_machine_id=machine,
        workspaces_run_here=frozenset({workspace}) if runs else frozenset(),
        workspaces_bound_elsewhere=frozenset() if runs else frozenset({workspace}),
    )


@pytest.mark.parametrize(
    "where",
    ["home/Pricing", "home/Pricing/files", "home/Pricing/files/notes.md"],
    ids=["the-folder", "the-shared-tree", "a-shared-file"],
)
@pytest.mark.parametrize(
    ("runs", "machine", "expected"),
    [
        pytest.param(True, "box-a", "writer", id="the-workspaces-own-box-holds-the-machine-rung"),
        pytest.param(False, "box-a", None, id="another-boxs-workspace-is-nothing"),
        pytest.param(True, None, None, id="an-unverified-assertion-is-nothing"),
    ],
)
async def test_a_box_reaches_a_workspaces_tree_by_its_binding_and_never_by_the_floor(
    box_workspace_scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    where: str,
    runs: bool,
    machine: str | None,
    expected: str | None,
) -> None:
    """The operator is an org admin, whose floor reaches a workspace's files
    for the person: it does not travel with their box. What reaches in is the
    workspace's binding, and only for a proven machine."""
    drive, made = box_workspace_scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.admin_id)

    access = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [],
        drive,
        _workspace_box_facts(made, runs=runs, machine=machine),
    )

    assert access.role == expected
    assert access.in_workspace_subtree is True
    assert access.workspace_bound_elsewhere is (not runs)
    if expected is None:
        assert access.allowed_actions == frozenset()
    else:
        assert {A.READ.value, A.WRITE.value, A.LEASE.value, A.SNAPSHOT.value} <= set(
            access.allowed_actions
        )
        assert A.LEASE_FORCE.value not in access.allowed_actions


async def test_a_deployment_that_opens_workspaces_to_admins_opens_them_to_the_person_only(
    box_workspace_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """With admins let into private trees the org admin reaches a workspace's
    files; their box, which the workspace is not bound to, still does not."""
    drive, made = box_workspace_scene
    where = "home/Pricing/files/notes.md"
    person = effective_role(
        _ctx(files_org.org_team_id, files_org.admin_id),
        made[where],
        _chain(made, where),
        [],
        drive,
        AccessFacts(org_admin=True, org_admin_reaches_chats=True),
    )
    agent = effective_role(
        _agent_ctx(files_org.org_team_id, files_org.admin_id),
        made[where],
        _chain(made, where),
        [],
        drive,
        replace(_workspace_box_facts(made, runs=False), org_admin_reaches_chats=True),
    )

    assert person.role == "manager"
    assert agent.role is None


async def test_a_members_records_are_governed_by_the_chat_not_the_workspace(
    box_workspace_scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """Inside ``.chats/<chat>`` the innermost folder a machine runs is the
    chat's: the workspace's binding does not reach a member's records, the
    chat's own does."""
    drive, made = box_workspace_scene
    ctx = _agent_ctx(files_org.org_team_id, files_org.admin_id)
    where = "home/Pricing/.chats/c1/m.json"
    facts = _workspace_box_facts(made, runs=True)

    by_workspace = effective_role(ctx, made[where], _chain(made, where), [], drive, facts)
    assert by_workspace.in_workspace_subtree is False
    assert by_workspace.in_chat_subtree is True
    assert by_workspace.role is None

    by_chat = effective_role(
        ctx,
        made[where],
        _chain(made, where),
        [],
        drive,
        replace(facts, chats_run_here=frozenset({made["home/Pricing/.chats/c1"].id})),
    )
    assert by_chat.role == "writer"
