"""Two people, three orgs: every credential reaches its own org and no other.

U belongs to orgs A (home) and B; V belongs to orgs B and C (home C). Four
credentials exist: U@A, U@B, V@B, V@C. Two credentials in the same org (U@B
and V@B) share that org's data, as two colleagues do; every pair in different
orgs, in both directions, reaches nothing of the other's org. A shared org (B)
is the case a single "person to org" assumption gets wrong: U reaching C
through V, or V reaching A through U, because each is a member of B.

Every case drives the real app against real Postgres with multi-org on, and
each org holds data of its own, so "nothing of the other org" can never pass by
returning nothing at all.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from itertools import permutations

import pytest
import pytest_asyncio
from alkera_core.auth import register_token
from alkera_core.auth.tenancy import ORG_HEADER, MembershipRefused
from alkera_core.models import TokenType, User
from backend.auth.membership_tokens import mint_for_membership
from backend.services.org import memberships as membership_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import _unique_email, _unique_org_name, app_client, make_member

pytestmark = [pytest.mark.usefixtures("multi_org")]


@dataclass(frozen=True)
class Credential:
    name: str
    person: str
    org: str
    org_id: uuid.UUID
    token: str


@dataclass
class World:
    orgs: dict[str, uuid.UUID]
    users: dict[str, User]
    credentials: dict[str, Credential]


async def _org(session: AsyncSession, label: str) -> uuid.UUID:
    org, _admin = await team_service.create_org_with_admin(
        session,
        org_name=_unique_org_name(),
        admin_email=_unique_email(f"admin-{label}"),
        admin_first_name="Admin",
        admin_last_name=label.upper(),
        admin_password="admin-pass-12345",
    )
    await session.commit()
    return org.id


async def _join(session: AsyncSession, user: User, org_id: uuid.UUID) -> None:
    await org_membership_service.create(session, user_id=user.id, org_team_id=org_id)
    await membership_service.add_member(session, team_id=org_id, user_id=user.id)
    await session.commit()


@pytest_asyncio.fixture
async def world(real_session: AsyncSession) -> World:
    orgs = {label: await _org(real_session, label) for label in ("a", "b", "c")}
    u, _ = await make_member(
        real_session, org_id=orgs["a"], email=_unique_email("u"), verified=True
    )
    v, _ = await make_member(
        real_session, org_id=orgs["c"], email=_unique_email("v"), verified=True
    )
    await _join(real_session, u, orgs["b"])
    await _join(real_session, v, orgs["b"])
    credentials: dict[str, Credential] = {}
    for person, user, org in (("u", u, "a"), ("u", u, "b"), ("v", v, "b"), ("v", v, "c")):
        token, claims = await mint_for_membership(real_session, user, orgs[org], kind="cli")
        await register_token(real_session, claims=claims, token_type=TokenType.CLI)
        name = f"{person}@{org}"
        credentials[name] = Credential(name, person, org, orgs[org], token)
    await real_session.commit()
    return World(orgs=orgs, users={"u": u, "v": v}, credentials=credentials)


def _client(credential: Credential) -> AsyncClient:
    return app_client(headers={"Authorization": f"Bearer {credential.token}"})


#: Every ordered pair of credentials in different orgs.
CROSS_PAIRS = [
    (first, second)
    for first, second in permutations(("u@a", "u@b", "v@b", "v@c"), 2)
    if first.split("@")[1] != second.split("@")[1]
]


def test_the_cross_pairs_cover_both_directions_and_skip_colleagues() -> None:
    assert ("u@a", "v@c") in CROSS_PAIRS and ("v@c", "u@a") in CROSS_PAIRS
    assert ("u@a", "v@b") in CROSS_PAIRS and ("v@b", "u@a") in CROSS_PAIRS
    assert ("u@b", "v@b") not in CROSS_PAIRS
    assert len(CROSS_PAIRS) == 10


async def _share(credential: Credential) -> str:
    item_id = f"fact-{uuid.uuid4().hex[:10]}"
    async with _client(credential) as client:
        response = await client.post(
            "/api/v1/kb/promote",
            json={
                "item_id": item_id,
                "kind": "note",
                "title": f"from {credential.name}",
                "body": "b",
                "target_scope": f"org:{credential.org_id}",
                "content_updated_at": time.time(),
            },
        )
    assert response.status_code == 200, response.text
    return item_id


async def _items(credential: Credential) -> set[str]:
    async with _client(credential) as client:
        response = await client.get("/api/v1/kb/items")
    assert response.status_code == 200, response.text
    return {row["item_id"] for row in response.json()["items"]}


async def _teams(credential: Credential) -> set[str]:
    async with _client(credential) as client:
        response = await client.get("/api/v1/teams")
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


async def test_knowledge_is_read_only_in_the_credentials_org(world: World) -> None:
    shared = {name: await _share(cred) for name, cred in world.credentials.items()}
    seen = {name: await _items(cred) for name, cred in world.credentials.items()}
    for first, second in CROSS_PAIRS:
        assert shared[second] not in seen[first], f"{first} read what {second} shared"
    # Colleagues in B see each other's B items, and each credential its own.
    assert {shared["u@b"], shared["v@b"]} <= seen["u@b"]
    assert {shared["u@b"], shared["v@b"]} <= seen["v@b"]
    for name in world.credentials:
        assert shared[name] in seen[name]


async def test_teams_are_listed_only_in_the_credentials_org(world: World) -> None:
    listed = {name: await _teams(cred) for name, cred in world.credentials.items()}
    for first, second in CROSS_PAIRS:
        other_root = str(world.credentials[second].org_id)
        assert other_root not in listed[first], f"{first} listed {second}'s org"
    for name, cred in world.credentials.items():
        assert str(cred.org_id) in listed[name]


@pytest.mark.parametrize(("first", "second"), CROSS_PAIRS)
async def test_a_credential_asserting_another_orgs_header_is_refused(
    world: World, first: str, second: str
) -> None:
    """The org header is an assertion checked against the credential, never a
    selector: naming the other org is a conflict, not a switch."""
    credential = world.credentials[first]
    async with _client(credential) as client:
        response = await client.get(
            "/api/v1/teams", headers={ORG_HEADER: str(world.credentials[second].org_id)}
        )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    ("person", "org"),
    [
        pytest.param("u", "c", id="u-cannot-enter-c-through-b"),
        pytest.param("v", "a", id="v-cannot-enter-a-through-b"),
    ],
)
async def test_sharing_an_org_admits_nobody_to_the_other_persons_orgs(
    real_session: AsyncSession, world: World, person: str, org: str
) -> None:
    """No credential is minted for an org the person has no membership in,
    however the two people are connected."""
    with pytest.raises(MembershipRefused):
        await mint_for_membership(real_session, world.users[person], world.orgs[org], kind="cli")
    await real_session.rollback()


async def test_leaving_the_shared_org_cuts_only_that_orgs_credentials(
    real_session: AsyncSession, world: World
) -> None:
    """V is removed from B: V@B stops working and V@C keeps working, and U's
    credentials in A and B are untouched."""
    membership = await org_membership_service.get(
        real_session, user_id=world.users["v"].id, org_team_id=world.orgs["b"]
    )
    assert membership is not None
    await org_membership_service.remove(real_session, membership, actor=None)
    await real_session.commit()
    from alkera_core.auth import revocation

    revocation._cache.reset()
    statuses = {}
    for name, credential in world.credentials.items():
        async with _client(credential) as client:
            statuses[name] = (await client.get("/api/v1/teams")).status_code
    assert statuses == {"u@a": 200, "u@b": 200, "v@b": 401, "v@c": 200}
