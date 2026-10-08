"""The shared auth dependencies: team-admin tenancy, and the CI-token checkout.

Two invariants that no single route can be trusted to hold on its own:

- ``require_team_admin`` authorizes by permission descent, which is a pure
  membership walk. Without a tenancy predicate of its own it hands a foreign
  tenant's teams to anyone holding a stray cross-org ADMIN row, and the routers
  that rely on it alone (team connections, team invitations) have no second
  check to catch that.
- ``require_ci_token`` must stamp ``last_used_at`` durably WITHOUT ever holding
  two pooled DB connections at once. Holding two is a deadlock: enough
  simultaneous gate calls each wait for a connection none of them can release.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal, engine
from alkera_core.models import OrgMembership, TeamMembership, TeamRole
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


async def _second_org(session: AsyncSession) -> tuple[UUID, str, str]:
    """A fully separate tenant: (root team id, admin email, admin password)."""
    from backend.services.org import teams as team_service

    email = f"otheradmin-{secrets.token_hex(6)}@otherorg.dev"
    password = "other-pass-12345"
    org, _admin = await team_service.create_org_with_admin(
        session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=email,
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password=password,
    )
    await session.commit()
    return org.id, email, password


# --------------------------------------------------------------------------- #
# require_team_admin — tenancy
# --------------------------------------------------------------------------- #


async def test_a_cross_org_admin_row_does_not_unlock_the_other_tenant(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The defect this closes: permission descent asks only "is there an ADMIN row
    anywhere up this team's chain?" and never "is this team even in my org?". One
    stray cross-org membership row therefore handed an outsider full team-admin
    control of another tenant — their warehouse connections, their invitations —
    with every audit entry filed under the WRONG org.

    The rows below are written directly on purpose: the person is made an admin
    of the other org's root as well (the schema refuses a seat without the org
    membership behind it), and the request is made on their home-org session.
    The DEPENDENCY must refuse, because the request's org is the credential's,
    whatever else the person holds. 404 (not 403) matches the opaque not-found
    convention — a foreign team id is never confirmed to exist.
    """
    other_org_id, _, _ = await _second_org(real_session)
    real_session.add(OrgMembership(user_id=org_admin.admin_id, org_team_id=other_org_id))
    real_session.add(
        TeamMembership(user_id=org_admin.admin_id, team_id=other_org_id, role=TeamRole.ADMIN)
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{other_org_id}/connections")
    assert resp.status_code == 404


async def test_an_admin_still_reaches_their_own_teams(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The tenancy predicate must not break the normal case."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/connections")
    assert resp.status_code == 200


async def test_a_non_admin_in_the_same_org_is_still_403_not_404(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The asymmetric case: only a FOREIGN team is hidden. An in-org member who
    simply lacks the admin role keeps the honest 403 — over-rotating everything to
    404 would erase the difference between "not yours" and "not allowed"."""
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await real_session.commit()
    assert member_pw is not None
    await login(client, member.email, member_pw)
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/connections")
    assert resp.status_code == 403


async def test_a_team_id_that_does_not_exist_is_404(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from uuid import uuid4

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{uuid4()}/connections")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# require_ci_token — one pooled connection at a time
# --------------------------------------------------------------------------- #


class _PoolWatch:
    """Peak simultaneous pool checkouts, measured from the engine's own events."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def _out(self, *_: Any) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def _in(self, *_: Any) -> None:
        self.current -= 1


@pytest.fixture
def pool_watch() -> Iterator[_PoolWatch]:
    watch = _PoolWatch()
    event.listen(engine.sync_engine, "checkout", watch._out)
    event.listen(engine.sync_engine, "checkin", watch._in)
    try:
        yield watch
    finally:
        event.remove(engine.sync_engine, "checkout", watch._out)
        event.remove(engine.sync_engine, "checkin", watch._in)


async def _mint_ci_token(org_id: UUID, user_id: UUID) -> str:
    from backend.services.credentials import ci_tokens as ci_token_service

    async with AsyncSessionLocal() as session:
        _row, raw = await ci_token_service.mint(
            session, org_id=org_id, created_by_id=user_id, label="pool-watch"
        )
        await session.commit()
    return raw


async def test_a_gate_call_never_holds_two_pooled_connections(
    client: AsyncClient, org_admin: OrgWithAdmin, pool_watch: _PoolWatch
) -> None:
    """The deadlock this closes: the dependency resolved the token on the REQUEST
    session (checking out a connection held for the whole request) and then opened
    a second session to stamp ``last_used_at``, checking out a second connection
    while the first was still held. At the pool's size that is a cycle — enough
    simultaneous gate calls each wait for a connection none of them can release,
    and every request the process serves, gate or not, fails until the pool
    timeout. The stamp still needs its own committed session (it must survive the
    request's rollback), so the fix is to never overlap the two.

    Measured from the engine's own checkout/checkin events: the peak concurrent
    checkout across one whole gate request must be 1."""
    raw = await _mint_ci_token(org_admin.org_id, org_admin.admin_id)

    # Settle any connection the fixtures left checked out, then measure.
    pool_watch.current = 0
    pool_watch.peak = 0
    resp = await client.post(
        "/api/v1/gate/snapshots/lookup",
        json={"repo": "acme/warehouse", "candidates": ["a" * 40]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert pool_watch.peak == 1, (
        f"peak {pool_watch.peak} concurrent pool checkouts on one gate request -- the "
        "CI-token dependency is holding a second connection while the first is out"
    )


async def test_a_failed_gate_call_still_stamps_last_used_at(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The reason the stamp has its own session at all: a request that later fails
    rolls the request transaction back, and a stolen token being probed must still
    leave a usage trace. Pinned here alongside the pool fix so a future
    "just use the request session" simplification cannot silently drop it."""
    from alkera_core.models import CiToken
    from backend.services.credentials import ci_tokens as ci_token_service
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        row, raw = await ci_token_service.mint(
            session,
            org_id=org_admin.org_id,
            created_by_id=org_admin.admin_id,
            label="probe",
            repo="acme/allowed",
        )
        token_id = row.id
        await session.commit()

    # Repo-scoped token pointed at a different repo -> the route refuses.
    resp = await client.post(
        "/api/v1/gate/snapshots/lookup",
        json={"repo": "acme/forbidden", "candidates": ["a" * 40]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403

    async with AsyncSessionLocal() as session:
        stamped = (
            await session.execute(select(CiToken).where(CiToken.id == token_id))
        ).scalar_one()
    assert stamped.last_used_at is not None


# ---------------------------------------------------------------------------
# No credential is ever read from a query string
# ---------------------------------------------------------------------------

BACKEND_PACKAGE = Path(__file__).resolve().parents[1] / "backend"


async def test_no_websocket_credential_reader_is_left_in_the_auth_dependencies() -> None:
    """The socket gateway authenticates by a single-use ticket carried in the
    Sec-WebSocket-Protocol header and by nothing else. A helper that read a
    session token from ``?token=`` outlived every caller it had; a credential
    in a query string lands in every access log on the path, so no such
    reader may exist for a future route to pick up."""
    from backend.auth import dependencies

    assert not hasattr(dependencies, "ws_current_user")
    assert "ws_current_user" not in dependencies.__all__


@pytest.mark.parametrize(
    "needle",
    [
        pytest.param('query_params.get("token")', id="get"),
        pytest.param('query_params["token"]', id="index"),
        pytest.param("query_params.get('token')", id="get-single-quoted"),
    ],
)
async def test_no_backend_module_reads_a_token_from_the_query_string(needle: str) -> None:
    offenders = sorted(
        str(path.relative_to(BACKEND_PACKAGE))
        for path in BACKEND_PACKAGE.rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    )
    assert offenders == []
