"""One person, two orgs: objects and the people Files names belong to the request's org.

The subject is a person in org A (their home) who also belongs to org B
(``two_org_identity``, multi-org on). Two halves:

* a workspace object made on the B credential lives in B, and the B listing
  shows B's objects, never A's;
* every Files read that names people (share candidates, a grantee, the writer
  a conflicted copy is named after) asks whether the person holds an active
  membership in the drive's org. The person's home org says nothing: they are a
  member of B although their row names A, and they stop being one the moment
  their B membership is deactivated.

The Files routes' isolation itself is the two-org fuzz's subject
(``files/test_files_two_org_fuzz.py``); this suite covers what it cannot see.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.authz import RoleResolver
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.conflict_auto import ANONYMOUS_WRITER, writer_name
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.models import MembershipStatus, OrgMembership, WorkspaceObject
from alkera_core.models.files.acl import FileAcl, FileShare
from backend.services.files.facts import caller_facts
from backend.services.files.store import set_store_factory
from backend.services.objects import object_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import TwoOrg, app_client, mint_cli_token
from tests.files._files_kit import FilesFixtures

pytestmark = [pytest.mark.spread]

FILES = "/api/v1/files"


@pytest.fixture
def files_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "files-store"
    root.mkdir()
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)
    monkeypatch.setattr(settings, "files_inline_operations", True)
    yield
    set_store_factory(None)


@pytest_asyncio.fixture
async def pair(two_org_identity: TwoOrg, multi_org: None) -> TwoOrg:
    return two_org_identity


@pytest_asyncio.fixture
async def as_b(pair: TwoOrg) -> AsyncIterator[AsyncClient]:
    """The two-org person on their org-B credential."""
    async with app_client(headers={"Authorization": f"Bearer {pair.token_b}"}) as client:
        yield client


@pytest_asyncio.fixture
async def admin_b(pair: TwoOrg) -> AsyncIterator[AsyncClient]:
    """Org B's own admin, whose only org is B."""
    token = await mint_cli_token(
        user_id=pair.admin_b.id, email=pair.admin_b.email, org_team_id=pair.org_b
    )
    async with app_client(headers={"Authorization": f"Bearer {token}"}) as client:
        yield client


async def _end_membership(session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID) -> None:
    await session.execute(
        update(OrgMembership)
        .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_id)
        .values(status=MembershipStatus.DEACTIVATED)
    )
    await session.commit()


# --------------------------------------------------------------------------
# workspace objects
# --------------------------------------------------------------------------


async def test_an_object_made_on_b_lives_in_b(
    real_session: AsyncSession, pair: TwoOrg, as_b: AsyncClient
) -> None:
    client_id = f"result-{uuid.uuid4().hex[:8]}"

    response = await as_b.post(
        "/api/v1/objects", json={"type": "result", "title": "made on B", "client_id": client_id}
    )

    assert response.status_code == 201, response.text
    row = await real_session.get(WorkspaceObject, uuid.UUID(response.json()["id"]))
    assert row is not None
    assert (row.org_team_id, row.owner_user_id) == (pair.org_b, pair.user.id)


async def test_a_client_id_taken_in_a_is_free_in_b(
    real_session: AsyncSession, pair: TwoOrg, as_b: AsyncClient
) -> None:
    """A create is idempotent on the client's id within one org. The same id the
    person used in A names nothing in B, so B gets a new object rather than a
    read of A's."""
    client_id = f"result-{uuid.uuid4().hex[:8]}"
    in_a, _ = await object_service.create_object(
        real_session,
        owner=pair.user,
        org_id=pair.org_a,
        type="result",
        title="A's",
        spec={},
        logical_id=client_id,
        visibility_scope="org",
    )
    await real_session.commit()

    response = await as_b.post(
        "/api/v1/objects", json={"type": "result", "title": "B's", "client_id": client_id}
    )

    assert response.status_code == 201, response.text
    assert response.json()["id"] != str(in_a.id)
    assert response.json()["title"] == "B's"


async def test_the_b_listing_shows_b_objects_and_none_of_a(
    real_session: AsyncSession, pair: TwoOrg, as_b: AsyncClient
) -> None:
    made: dict[uuid.UUID, uuid.UUID] = {}
    for org_id in (pair.org_a, pair.org_b):
        obj, _ = await object_service.create_object(
            real_session,
            owner=pair.user,
            org_id=org_id,
            type="board",
            title="the plan",
            spec={},
            visibility_scope="org",
        )
        made[org_id] = obj.id
    await real_session.commit()

    response = await as_b.get("/api/v1/objects")

    assert response.status_code == 200, response.text
    listed = {item["id"] for item in response.json()["items"]}
    assert str(made[pair.org_b]) in listed
    assert str(made[pair.org_a]) not in listed


# --------------------------------------------------------------------------
# the people Files names
# --------------------------------------------------------------------------


async def _home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{FILES}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"]), str(response.json()["homeId"])


async def _candidates(client: AsyncClient, query: str) -> set[str]:
    drive_id, home_id = await _home(client)
    response = await client.get(
        f"{FILES}/drives/{drive_id}/items/{home_id}/share-candidates", params={"q": query}
    )
    assert response.status_code == 200, response.text
    return {one["principal"]["id"] for one in response.json()["value"]}


async def test_a_b_member_whose_home_is_a_is_a_share_candidate_in_b(
    pair: TwoOrg, files_on: None, admin_b: AsyncClient
) -> None:
    assert str(pair.user.id) in await _candidates(admin_b, pair.user.email)


async def test_a_member_of_a_alone_is_never_a_candidate_in_b(
    pair: TwoOrg, files_on: None, admin_b: AsyncClient
) -> None:
    assert str(pair.admin_a.id) not in await _candidates(admin_b, pair.admin_a.email)


async def test_a_deactivated_b_membership_takes_the_person_off_b_candidates(
    real_session: AsyncSession, pair: TwoOrg, files_on: None, admin_b: AsyncClient
) -> None:
    await _end_membership(real_session, pair.user.id, pair.org_b)

    assert str(pair.user.id) not in await _candidates(admin_b, pair.user.email)


async def _grant_to(client: AsyncClient, principal_id: uuid.UUID) -> Any:
    drive_id, home_id = await _home(client)
    item = await client.get(f"{FILES}/drives/{drive_id}/items/{home_id}")
    assert item.status_code == 200, item.text
    return await client.post(
        f"{FILES}/drives/{drive_id}/items/{home_id}/permissions",
        json={"principal": {"kind": "user", "id": str(principal_id)}, "role": "reader"},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": item.headers["ETag"]},
    )


async def test_b_can_share_with_its_member_whose_home_is_a(
    real_session: AsyncSession, pair: TwoOrg, files_on: None, admin_b: AsyncClient
) -> None:
    response = await _grant_to(admin_b, pair.user.id)

    assert response.status_code == 201, response.text
    shares = (
        (
            await real_session.execute(
                select(FileShare).where(FileShare.principal_id == pair.user.id)
            )
        )
        .scalars()
        .all()
    )
    assert [share.org_team_id for share in shares] == [pair.org_b]


@pytest.mark.parametrize(
    "who",
    [
        pytest.param("a-only-member", id="a_member_of_a_alone"),
        pytest.param("ended", id="a_person_whose_b_membership_ended"),
    ],
)
async def test_b_cannot_share_with_someone_outside_b(
    real_session: AsyncSession, pair: TwoOrg, files_on: None, admin_b: AsyncClient, who: str
) -> None:
    if who == "ended":
        await _end_membership(real_session, pair.user.id, pair.org_b)
    target = pair.admin_a.id if who == "a-only-member" else pair.user.id

    response = await _grant_to(admin_b, target)

    assert response.status_code == 404, response.text
    found = await real_session.execute(select(FileShare).where(FileShare.principal_id == target))
    assert found.scalars().all() == []


@pytest.mark.parametrize(
    ("writer", "named"),
    [
        pytest.param("person", True, id="b_member_whose_home_is_a"),
        pytest.param("admin_a", False, id="member_of_a_alone"),
    ],
)
async def test_a_conflicted_copy_names_its_writer_only_inside_the_drive_org(
    real_session: AsyncSession, pair: TwoOrg, files_on: None, writer: str, named: bool
) -> None:
    author = pair.user if writer == "person" else pair.admin_a
    fx = FilesFixtures(real_session, pair.org_b, pair.admin_b.id)
    doc = await fx.node(b"notes.md", kind="file", parent=await fx.shared())
    version = await fx.version(doc, created_by=author.id)
    repo = FilesRepo(real_session, OrgScope(org_team_id=pair.org_b))

    async with repo.transaction():
        shown = await writer_name(repo, version)

    expected = f"{author.first_name} {author.last_name}".strip() if named else ANONYMOUS_WRITER
    assert shown == expected


async def test_files_team_facts_on_b_hold_no_team_of_a(
    real_session: AsyncSession, pair: TwoOrg
) -> None:
    """A team grant on a node is matched against the caller's team ids. The
    person is on a team in each org; on the B credential only B's count."""
    team_ids: dict[uuid.UUID, uuid.UUID] = {}
    for org_id in (pair.org_a, pair.org_b):
        team = await team_service.create_subteam(
            real_session, org_team_id=org_id, name=f"t-{uuid.uuid4().hex[:6]}"
        )
        await membership_service.add_member(real_session, team_id=team.id, user_id=pair.user.id)
        team_ids[org_id] = team.id
    await real_session.commit()
    ctx = ActingContext.for_user(user_id=pair.user.id, org_id=pair.org_b, email=pair.user.email)

    facts = await caller_facts(
        real_session,
        ctx,
        RoleResolver(real_session, ctx, ancestor_chain=team_service.ancestor_chain),
    )

    assert team_ids[pair.org_b] in facts.team_ids
    assert pair.org_b in facts.team_ids
    assert facts.team_ids.isdisjoint({team_ids[pair.org_a], pair.org_a})


async def test_the_person_gets_a_home_in_each_org_they_belong_to(
    real_session: AsyncSession, pair: TwoOrg, files_on: None
) -> None:
    """A home folder's permission set names its owner and nothing about the
    org, so the person's home in B carries the same set as their home in A.
    Each org's drive must intern its own row: a key shared across orgs made
    the second org's insert collide with a row its own read cannot see."""
    homes = []
    for org_id in (pair.org_a, pair.org_b):
        fx = FilesFixtures(real_session, org_id, pair.user.id)
        homes.append(await fx.home())

    acls = (
        await real_session.execute(
            select(FileAcl.org_team_id, FileAcl.body).where(
                FileAcl.id.in_([home.acl_id for home in homes])
            )
        )
    ).all()
    assert sorted(str(org) for org, _body in acls) == sorted([str(pair.org_a), str(pair.org_b)])
    assert acls[0].body == acls[1].body
