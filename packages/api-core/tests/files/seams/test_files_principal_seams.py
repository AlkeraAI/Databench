"""Two more principal kinds through ``LadderDecider``, facts only.

Future callers each kind stands for:

* ``agent`` — the box agent and a future connector agent acting for a user
  inside a leased subtree. It must never be able to reshare, and must see
  nothing outside its lease.
* ``link`` — a share link, and behind it ``public``. A link grant is data the
  decider already refuses to turn into access for a *user* context, which is
  what keeps a link from widening a human's role by accident.

Neither needed a code change: the kind is an open registry on
:class:`~alkera_core.files.authz.grants.Principal`, and the confinement is a
fact on :class:`~alkera_core.files.authz.decider.AccessFacts`. That is the
claim — the seam admits the caller by registration, not by a new branch.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files.authz.actions import FilesAction as A
from alkera_core.files.authz.decider import AccessFacts, effective_role
from alkera_core.files.authz.grants import Grant
from alkera_core.files.authz.grants import Principal as GrantPrincipal
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _user_ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org_id,
            credential=CredentialKind.JWT,
        )
    )


def _agent_ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    """An agent session delegating for a real user — the box agent's shape."""
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


@pytest.fixture
async def scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]]:
    """``work/`` leased to the agent, ``other/`` outside it."""
    drive = await files_factory.drive()
    made = await files_factory.tree("work/ work/a.txt other/ other/b.txt", drive=drive)
    return drive, made, ()


# ---- the ``agent`` kind ----------------------------------------------------


async def test_an_agent_inside_its_lease_keeps_the_users_write(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, made, _ = scene
    node = made["work/a.txt"]
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    grant = Grant(principal=GrantPrincipal(kind="user", id=files_org.member_id), role="manager")

    access = effective_role(
        ctx,
        node,
        (made["work"], node),
        [grant],
        drive,
        AccessFacts(is_agent=True, leased_subtree=made["work"].id),
    )

    assert access.allows(A.READ)
    assert access.allows(A.WRITE)


async def test_an_agent_never_reshares_even_as_owner(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The confinement drops SHARE unconditionally: an agent cannot hand its
    delegator's reach to anybody else."""
    drive, made, _ = scene
    node = made["work/a.txt"]
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    grant = Grant(principal=GrantPrincipal(kind="user", id=files_org.member_id), role="owner")

    access = effective_role(
        ctx,
        node,
        (made["work"], node),
        [grant],
        drive,
        AccessFacts(is_agent=True, leased_subtree=made["work"].id),
    )

    assert not access.allows(A.SHARE)
    # The same grant without the agent fact does allow it — so the assertion
    # above is the confinement talking, not a weak grant.
    human = effective_role(
        ctx, node, (made["work"], node), [grant], drive, AccessFacts(is_agent=False)
    )
    assert human.allows(A.SHARE)


@pytest.mark.parametrize(
    ("path", "leased", "expect_any"),
    [
        pytest.param("work/a.txt", "work", True, id="inside-the-lease"),
        pytest.param("work", "work", True, id="the-lease-root-itself"),
        pytest.param("other/b.txt", "work", False, id="outside-the-lease"),
        pytest.param("work/a.txt", None, False, id="no-lease-at-all"),
    ],
)
async def test_an_agent_sees_nothing_outside_its_leased_subtree(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]],
    files_org: FilesOrg,
    path: str,
    leased: str | None,
    expect_any: bool,
) -> None:
    drive, made, _ = scene
    node = made[path]
    chain = (made[path.split("/")[0]], node) if "/" in path else (node,)
    ctx = _agent_ctx(files_org.org_team_id, files_org.member_id)
    grant = Grant(principal=GrantPrincipal(kind="user", id=files_org.member_id), role="owner")

    access = effective_role(
        ctx,
        node,
        chain,
        [grant],
        drive,
        AccessFacts(is_agent=True, leased_subtree=None if leased is None else made[leased].id),
    )

    assert bool(access.allowed_actions) is expect_any
    # In-org stays true either way: confinement removes actions, it does not
    # pretend the node belongs to somebody else.
    assert access.in_org is True


# ---- the ``link`` kind -----------------------------------------------------


async def test_a_link_grant_never_satisfies_a_user_context(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """A link grant is data the decider ignores for a human caller: the kind is
    an open registry, so it is dropped rather than raising or matching."""
    drive, made, _ = scene
    node = made["work/a.txt"]
    ctx = _user_ctx(files_org.org_team_id, files_org.member_id)
    link_id = uuid.uuid4()

    access = effective_role(
        ctx,
        node,
        (made["work"], node),
        [Grant(principal=GrantPrincipal(kind="link", id=link_id), role="owner")],
        drive,
        AccessFacts(),
    )

    assert access.role is None
    assert access.allowed_actions == frozenset()


async def test_a_link_grant_cannot_raise_a_users_own_rung(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The asymmetric case: a reader beside an owner-roled link stays a reader."""
    drive, made, _ = scene
    node = made["work/a.txt"]
    ctx = _user_ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        node,
        (made["work"], node),
        [
            Grant(principal=GrantPrincipal(kind="user", id=files_org.member_id), role="reader"),
            Grant(principal=GrantPrincipal(kind="link", id=uuid.uuid4()), role="owner"),
        ],
        drive,
        AccessFacts(),
    )

    assert access.role == "reader"
    assert not access.allows(A.WRITE)
    assert not access.allows(A.DELETE)


async def test_a_link_id_equal_to_the_callers_user_id_still_does_not_match(
    scene: tuple[FileDrive, dict[str, FileNode], Sequence[FileNode]], files_org: FilesOrg
) -> None:
    """The kind is what is matched, never the id alone — so a colliding uuid in
    a future kind's namespace cannot impersonate a user."""
    drive, made, _ = scene
    node = made["work/a.txt"]
    ctx = _user_ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(
        ctx,
        node,
        (made["work"], node),
        [Grant(principal=GrantPrincipal(kind="link", id=files_org.member_id), role="owner")],
        drive,
        AccessFacts(),
    )

    assert access.role is None
