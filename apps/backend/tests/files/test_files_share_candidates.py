"""The share dialog's people picker, as a member who is not an org admin uses it.

The picker used to read the org admin's member table, so a plain member was
refused on every keystroke and read "nobody matches" for a colleague's exact
email. The candidates read is decided as a SHARE of the node the dialog is
open on, through the Files policy, and every branch of that decision is pinned
here against the real route: the owner of a home is answered, a reader is
refused visibly, a member with no grant and a stranger's drive are told
nothing exists — and each decision leaves its ``authz.decision`` row.

The answer itself is the set the grant route admits and nothing wider: this
org's active people and its teams. A same-named user of another org, a
deactivated colleague and an empty query all stay off it.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import NOT_FOUND, refusal
from alkera_core.authz.policies.files import FORBIDDEN
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_READER
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from backend.services.files.directory import CANDIDATE_LIMIT
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _drive(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"]), str(response.json()["homeId"])


def _url(drive_id: str, node_id: str | uuid.UUID) -> str:
    return f"{BASE}/drives/{drive_id}/items/{node_id}/share-candidates"


async def _member(client: AsyncClient, files_org: FilesOrgFixture) -> AsyncClient:
    return await login(app_client(), files_org.member.email, files_org.member_password)


async def _share_decisions(org_id: uuid.UUID, node_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == node_id,
            )
            .order_by(EventOutbox.id)
        )
        return [row.payload for row in rows.scalars().all() if row.payload["action"] == "share"]


async def _grant(fx: FilesFixtures, files_org: FilesOrgFixture, node: FileNode, role: str) -> None:
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), role)
    await fx.repo.session.commit()


def _ids(response: Any) -> list[tuple[str, str]]:
    return [(one["principal"]["kind"], one["principal"]["id"]) for one in response.json()["value"]]


# --------------------------------------------------------------------------- #
# who is answered
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("exact", id="exact_email"),
        pytest.param("upper", id="email_in_another_case"),
        pytest.param("name", id="name_fragment"),
    ],
)
async def test_a_plain_member_finds_a_colleague_from_their_own_home(
    client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    query: str,
) -> None:
    """The broken case, end to end: a member who is no admin anywhere opens
    Share on their own home and types the owner's email or name. The colleague
    is offered, with the email the dialog shows as the second line."""
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    typed = {
        "exact": files_org.org.admin_email,
        "upper": files_org.org.admin_email.upper(),
        "name": "test adm",
    }[query]
    response = await member.get(_url(drive_id, home_id), params={"q": typed})
    assert response.status_code == 200, response.text
    offered = {one["principal"]["id"]: one for one in response.json()["value"]}
    admin = offered.get(str(files_org.org.admin_id))
    assert admin is not None, response.json()
    assert admin["principal"]["kind"] == "user"
    assert admin["email"] == files_org.org.admin_email


async def test_a_member_with_full_access_on_a_colleagues_folder_may_search(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """Full access carries the onward share, so it carries the search too —
    and the allow is on record under the share action."""
    folder = await fx.node(b"team-plans", kind="folder")
    await _grant(fx, files_org, folder, ROLE_MANAGER)
    member = await _member(client, files_org)
    drive_id, _home = await _drive(member)
    response = await member.get(_url(drive_id, folder.id), params={"q": files_org.member.email})
    assert response.status_code == 200, response.text
    assert ("user", str(files_org.member.id)) in _ids(response)


async def test_the_owner_of_the_node_is_never_a_candidate(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A share has nothing to give the person who owns the item, so the server
    leaves them off the picker rather than the dialog comparing ids. The same
    person is offered on a node someone else owns."""
    folder = await fx.node(b"team-plans", kind="folder", created_by=files_org.org.admin_id)
    await _grant(fx, files_org, folder, ROLE_MANAGER)
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)

    on_theirs = await member.get(_url(drive_id, folder.id), params={"q": files_org.org.admin_email})
    on_mine = await member.get(_url(drive_id, home_id), params={"q": files_org.org.admin_email})

    assert on_theirs.status_code == 200, on_theirs.text
    assert ("user", str(files_org.org.admin_id)) not in _ids(on_theirs)
    assert ("user", str(files_org.org.admin_id)) in _ids(on_mine)

    rows = await _share_decisions(files_org.org.org_id, str(folder.id))
    assert [row["effect"] for row in rows] == ["allow"]
    assert rows[-1]["policy"] == "files.access"


async def test_the_answer_is_the_orgs_active_people_and_teams_and_nothing_else(
    client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """One fragment that every principal below carries. Only this org's active
    user and this org's team come back — never the same-named user of another
    org, and never a colleague whose account is deactivated."""
    tag = f"zephyr{secrets.token_hex(3)}"
    colleague, _ = await make_member(
        real_session, org_id=files_org.org.org_id, first_name=tag.title(), verified=True
    )
    gone, _ = await make_member(
        real_session, org_id=files_org.org.org_id, first_name=tag.title(), verified=True
    )
    await real_session.execute(update(User).where(User.id == gone.id).values(is_active=False))
    team = await team_service.create_subteam(
        real_session,
        org_team_id=files_org.org.org_id,
        name=f"{tag} squad",
        parent_team_id=files_org.org.org_id,
    )
    other_org, _other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"{tag} elsewhere",
        admin_email=f"{tag}-{secrets.token_hex(4)}@elsewhere.dev",
        admin_first_name=tag.title(),
        admin_last_name="Stranger",
        admin_password="pw-1234567890",
    )
    await real_session.commit()

    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    response = await member.get(_url(drive_id, home_id), params={"q": tag.upper()})
    assert response.status_code == 200, response.text
    assert sorted(_ids(response)) == sorted(
        [("user", str(colleague.id)), ("team", str(team.id))]
    ), f"another org ({other_org.id}) or a deactivated account leaked: {response.json()}"


async def test_the_org_itself_is_offered_as_a_team_marked_as_the_org(
    client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    root = await team_service.get_by_id(real_session, files_org.org.org_id)
    assert root is not None
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    response = await member.get(_url(drive_id, home_id), params={"q": root.name})
    entry = next(one for one in response.json()["value"] if one["principal"]["id"] == str(root.id))
    assert entry["principal"]["kind"] == "team"
    assert entry["isOrg"] is True


@pytest.mark.parametrize(
    "query",
    [pytest.param("", id="empty"), pytest.param("   ", id="blank")],
)
async def test_an_empty_search_names_nobody(
    client: AsyncClient, files_on: None, files_org: FilesOrgFixture, query: str
) -> None:
    """The read is a search, not a way to page through the directory."""
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    response = await member.get(_url(drive_id, home_id), params={"q": query})
    assert response.status_code == 200, response.text
    assert response.json() == {"value": []}


@pytest.mark.parametrize("wildcard", ["%", "_"])
async def test_a_like_wildcard_is_matched_as_the_character(
    client: AsyncClient, files_on: None, files_org: FilesOrgFixture, wildcard: str
) -> None:
    """``%`` would otherwise match every name in the org."""
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    response = await member.get(_url(drive_id, home_id), params={"q": wildcard})
    assert response.status_code == 200, response.text
    assert response.json() == {"value": []}


async def test_one_search_returns_a_bounded_list(
    client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    tag = f"crowd{secrets.token_hex(3)}"
    for _ in range(CANDIDATE_LIMIT + 3):
        await make_member(real_session, org_id=files_org.org.org_id, first_name=tag, verified=True)
    await real_session.commit()
    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    response = await member.get(_url(drive_id, home_id), params={"q": tag})
    assert len(response.json()["value"]) == CANDIDATE_LIMIT


# --------------------------------------------------------------------------- #
# who is refused, and how
# --------------------------------------------------------------------------- #


async def test_a_reader_is_refused_visibly_and_the_deny_survives(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """Can view does not carry a share, so it does not carry the search: the
    same visible 403 the grant route gives, filed as a share denial."""
    node = await fx.node(b"read-only.txt")
    await _grant(fx, files_org, node, ROLE_READER)
    member = await _member(client, files_org)
    drive_id, _home = await _drive(member)
    response = await member.get(_url(drive_id, node.id), params={"q": files_org.org.admin_email})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == FORBIDDEN

    rows = await _share_decisions(files_org.org.org_id, str(node.id))
    assert [(row["effect"], row["reason"]) for row in rows] == [("deny", "action_not_allowed")]


async def test_a_member_with_no_grant_is_told_nothing_exists(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    node = await fx.node(b"admins-only.txt")
    member = await _member(client, files_org)
    drive_id, _home = await _drive(member)
    response = await member.get(_url(drive_id, node.id), params={"q": files_org.org.admin_email})
    assert response.status_code == 404, response.text
    assert refusal(response) == NOT_FOUND

    rows = await _share_decisions(files_org.org.org_id, str(node.id))
    assert [(row["effect"], row["as_not_found"]) for row in rows] == [("deny", True)]


async def test_a_stranger_learns_nothing_and_the_query_is_never_looked_at(
    client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Another org's member naming this org's drive and home gets the same
    opaque answer an id that was never issued gets — whatever they typed."""
    other_org, _admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other {secrets.token_hex(4)}",
        admin_email=f"other-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="pw-1234567890",
    )
    await real_session.commit()
    stranger_user, password = await make_member(real_session, org_id=other_org.id, verified=True)
    stranger = await login(app_client(), stranger_user.email, password or "")

    member = await _member(client, files_org)
    drive_id, home_id = await _drive(member)
    theirs = await stranger.get(_url(drive_id, home_id), params={"q": files_org.org.admin_email})
    unissued = await stranger.get(_url(drive_id, uuid.uuid4()), params={"q": "x" * 5000})
    assert theirs.status_code == 404
    assert (theirs.status_code, refusal(theirs)) == (unissued.status_code, refusal(unissued))
