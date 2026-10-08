"""A grant to a team the caller is actually in reaches them.

The lane helper resolves ``AccessFacts.team_ids``, and the decider matches a
team ACE against that set literally. Resolving it from the org root's ancestor
chain — the shape this replaced — yields ``{org_id}`` alone, because the root
has no parent, so every grant naming a real team, and above all a sub-team,
matched nothing and the node stayed unreadable to the people it was shared
with. These tests pin the membership set and the decision it produces.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import LadderDecider
from alkera_core.files.authz.grants import Grant, Principal
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.tree import FileNode
from backend.api.deps.files_lane import facts_for
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/files/drives/d/items/i",
            "raw_path": b"/api/v1/files/drives/d/items/i",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test")],
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


class _Teams:
    """A parent team the member belongs to, a sub-team beneath it, and one the
    member is in no way related to."""

    def __init__(self, parent_id: uuid.UUID, child_id: uuid.UUID, stranger_id: uuid.UUID) -> None:
        self.parent_id = parent_id
        self.child_id = child_id
        self.stranger_id = stranger_id


@pytest_asyncio.fixture
async def teams(real_session: AsyncSession, files_org: FilesOrgFixture) -> _Teams:
    org_id = files_org.org.org_id
    parent = await team_service.create_subteam(real_session, org_team_id=org_id, name="Platform")
    child = await team_service.create_subteam(
        real_session, org_team_id=org_id, name="Storage", parent_team_id=parent.id
    )
    stranger = await team_service.create_subteam(real_session, org_team_id=org_id, name="Finance")
    await real_session.commit()
    # The membership is written on the CHILD; the service materializes the
    # parent and the org root, which is what makes one read the whole answer.
    await membership_service.add_member(
        real_session, team_id=child.id, user_id=files_org.member.id, role=TeamRole.ADMIN
    )
    await real_session.commit()
    return _Teams(parent.id, child.id, stranger.id)


def _member_ctx(files_org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(
        user_id=files_org.member.id,
        org_id=files_org.org.org_id,
        email=files_org.member.email,
    )


def _admin_ctx(files_org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )


async def test_the_facts_carry_every_materialized_team_not_just_the_org_root(
    real_session: AsyncSession, files_org: FilesOrgFixture, teams: _Teams
) -> None:
    """The org root alone is what the ancestor-chain shape produced; the real
    answer is the sub-team the row was written on and every ancestor of it."""
    facts = await facts_for(_request(), real_session, _member_ctx(files_org))
    assert facts.team_ids == frozenset({files_org.org.org_id, teams.parent_id, teams.child_id})
    assert teams.stranger_id not in facts.team_ids
    # ADMIN on the leaf, MEMBER on the materialized ancestors.
    assert facts.team_admin_ids == frozenset({teams.child_id})
    assert facts.org_admin is False


@pytest.mark.parametrize(
    ("granted_to", "readable"),
    [
        pytest.param("child", True, id="grant-to-the-sub-team-the-caller-is-in"),
        pytest.param("parent", True, id="grant-to-the-parent-team"),
        pytest.param("stranger", False, id="grant-to-a-team-the-caller-is-not-in"),
    ],
)
async def test_a_team_grant_decides_read_by_the_callers_real_memberships(
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    teams: _Teams,
    granted_to: str,
    readable: bool,
) -> None:
    """End to end through the decider: the facts the helper resolves are what a
    team ACE is matched against, so a grant to a sub-team either reaches the
    caller or does not."""
    drive = await fx.drive()
    node = await fx.node(b"quarterly.csv")
    ctx = _member_ctx(files_org)
    facts = await facts_for(_request(), real_session, ctx)
    team_id = {
        "child": teams.child_id,
        "parent": teams.parent_id,
        "stranger": teams.stranger_id,
    }[granted_to]
    grants = [Grant(principal=Principal(kind="team", id=team_id), role="reader", origin="direct")]

    access = LadderDecider().decide(ctx, node, [], grants, drive, facts)

    assert access.in_org is True
    assert access.allows(FilesAction.READ) is readable
    assert (access.role == "reader") is readable


async def test_a_caller_in_no_sub_team_carries_only_their_org_row(
    real_session: AsyncSession, files_org: FilesOrgFixture, teams: _Teams
) -> None:
    """The negative twin: the org admin joined no sub-team, so the sub-team ids
    are absent from their facts and a grant to one cannot reach them — the set
    is memberships, not "every team in the org"."""
    facts = await facts_for(_request(), real_session, _admin_ctx(files_org))
    assert facts.team_ids == frozenset({files_org.org.org_id})
    assert teams.child_id not in facts.team_ids
    assert facts.org_admin is True
    member_facts = await facts_for(_request(), real_session, _member_ctx(files_org))
    assert member_facts.team_ids != facts.team_ids


# ---------------------------------------------------------------------------
# through the real routes
# ---------------------------------------------------------------------------

BASE = "/api/v1/files"


async def _team_reader(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    node: FileNode,
    team_id: uuid.UUID,
) -> AsyncClient:
    """A logged-in member whose ONLY standing on ``node`` is a grant to a team
    they belong to — no user ACE, no admin role."""
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="team", id=team_id), "reader")
    await fx.repo.session.commit()
    member_client = app_client()
    return await login(member_client, files_org.member.email, files_org.member_password)


@pytest.mark.parametrize(
    ("granted_to", "visible"),
    [
        pytest.param("child", True, id="the-sub-team-the-caller-is-in"),
        pytest.param("stranger", False, id="a-team-the-caller-is-not-in"),
    ],
)
async def test_a_sub_team_grant_is_readable_through_items_content_and_feeds(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    teams: _Teams,
    granted_to: str,
    visible: bool,
) -> None:
    """The three read families each built their own ``AccessFacts`` from the org
    root's ancestor chain, so a grant naming a sub-team matched nothing and the
    node came back 404 to the very member it was shared with. The negative twin
    — a grant to a team the caller is not in — must stay invisible, so the fix
    is "their real memberships", not "every team in the org".
    """
    drive = await fx.drive()
    node = await fx.node(b"quarterly.csv")
    await fx.version(node)
    team_id = {"child": teams.child_id, "stranger": teams.stranger_id}[granted_to]
    reader = await _team_reader(client, fx, files_org, node, team_id)

    item = await reader.get(f"{BASE}/drives/{drive.id}/items/{node.id}")
    assert (item.status_code == 200) is visible, item.text

    content = await reader.get(
        f"{BASE}/drives/{drive.id}/items/{node.id}/content", follow_redirects=False
    )
    assert (content.status_code == 404) is not visible, content.text

    found = await reader.get(f"{BASE}/drives/{drive.id}/search", params={"q": "quarterly"})
    assert found.status_code == 200, found.text
    ids = [entry["id"] for entry in found.json()["value"]]
    assert (str(node.id) in ids) is visible, found.text


async def test_an_agent_reads_inside_its_leased_subtree_through_items_and_content(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """Every family but sharing left ``leased_subtree`` unset, and the decider
    empties an agent's action set outside its lease — so an agent was refused
    every node on every one of them. Inside the drive it is leased to it reads
    exactly what its user reads.
    """
    drive = await fx.drive()
    node = await fx.node(b"agent-readable.csv")
    await fx.version(node)
    headers = agent_headers("chat-01.session")

    item = await files_client.get(f"{BASE}/drives/{drive.id}/items/{node.id}", headers=headers)
    assert item.status_code == 200, item.text
    assert item.json()["id"] == str(node.id)

    content = await files_client.get(
        f"{BASE}/drives/{drive.id}/items/{node.id}/content",
        headers=headers,
        follow_redirects=False,
    )
    assert content.status_code == 302, content.text


async def test_an_agent_is_refused_a_node_outside_its_leased_subtree(
    client: AsyncClient, files_on: None, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """The other half of the same fact: the lease confines. A node the leased
    subtree is not an ancestor of is outside it, and the agent is refused it
    however wide its user's own reach is.
    """
    drive = await fx.drive()
    inside = await fx.node(b"inside.csv")
    assert drive.root_node_id is not None
    root = await fx.repo.session.get(FileNode, drive.root_node_id)
    assert root is not None
    chain = [root]
    ctx = ActingContext.for_agent(
        session_id="chat-01.session",
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )
    facts = await facts_for(_request(), fx.repo.session, ctx)
    assert facts.is_agent is True
    grants = [
        Grant(
            principal=Principal(kind="user", id=files_org.org.admin_id),
            role="owner",
            origin="direct",
        )
    ]
    decider = LadderDecider()

    leased = replace(facts, leased_subtree=drive.root_node_id)
    inside_access = decider.decide(ctx, inside, chain, grants, drive, leased)
    assert inside_access.allows(FilesAction.READ) is True

    elsewhere = replace(facts, leased_subtree=uuid.uuid4())
    outside = decider.decide(ctx, inside, chain, grants, drive, elsewhere)
    assert outside.allowed_actions == frozenset()
    assert outside.allows(FilesAction.READ) is False
