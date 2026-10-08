"""The platform's chat boxes, through the real routes.

Mint a credential in the console, boot a box with it, watch it claim its
machine and heartbeat with its load, dedicate it to one org, take it away.
Every branch of ``platform.machine`` and ``compute.machine_credential`` is
driven here with the decision row the outbox holds afterwards, and the two
invariants the whole feature exists for are asserted end to end: a credential
decides what a box is (the box cannot say), and a dedicated box serves one
org.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth.machine_token import (
    MACHINE_TOKEN_PREFIX,
    machine_credential_headers,
    parse_machine_credential,
)
from alkera_core.authz import agent_headers
from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.org_machines import free_machine_name
from alkera_core.compute.provider import EC2
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, MachineCredential, OrgComputeAssignment
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.models.org_machines import OrgComputeSettings, OrgMachine
from backend.api.routes.compute.machines import heartbeat_decision_sink
from backend.services.compute.machines import bound_chat_counts
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests._offering_helpers import delete_offerings
from tests._org_machine_rows import make_org_machine
from tests.conftest import OrgWithAdmin, login, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MACHINES = "/admin/v1/machines"


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the boxes these cases claim inside the ready window for the whole case.

    A box reads ``ready`` only while its last heartbeat is newer than
    ``compute_heartbeat_ready_seconds`` — forty-five seconds of WALL CLOCK, read
    live on every placement and every status the console serves. Nothing here is
    about that window: these cases claim which box serves which org and what the
    console shows for a box it has just heard from. But each one beats its box
    near the top and then drives a dozen more requests before it asks the
    question, so on a loaded runner — sixty-four workers, ten seconds a request —
    the window closes mid-case and ``place_for_org`` answers ``None`` for a box
    with nothing wrong with it. Pinned past any run's length, the answer is
    decided by placement rather than by how slow the host was. The window itself
    is not left unproven: a box that goes quiet is asserted under a frozen clock
    in ``test_chat_placement_on_readiness.py`` and ``test_compute_machines.py``,
    which is where a claim about liveness belongs.

    Ten minutes rather than an hour: the same pin is used by
    ``test_chat_machine_binding_seam.py``, where a case moves a frozen clock by
    the window and must not step past ``auth_token_ttl_seconds`` (1800) and lose
    its session cookie. Thirteen times the shipped forty-five seconds, and six
    times the slowest case this file has ever taken on a loaded runner.
    """
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The machines page and the pool are global by design: a credential or a
    box another test left behind — a platform box or an org's own — would show
    up here. Every standing box is released and every credential and
    assignment removed first."""
    from sqlalchemy import delete, update

    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()
    # Holding a box for an org makes an org machine and a free offering for
    # its type; both hold the catalog row a later module may clear.
    await delete_offerings(real_session, None)


async def _verify_email(user_id: UUID) -> None:
    """A box user is a verified account: the machine routes refuse a write
    from an unverified one, and the platform-admin fixture is not verified."""
    from alkera_core.models import User

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.email_verified_at = datetime.now(UTC)
        await session.commit()


async def _bound(machine_id: str) -> int:
    """The chats bound to the machine, which is what the page reports as served."""
    async with AsyncSessionLocal() as session:
        counts = await bound_chat_counts(session, [UUID(machine_id)])
    return counts.get(UUID(machine_id), 0)


async def _org_machines(session: AsyncSession, org_id: UUID) -> list[OrgMachine]:
    """The org's live org machines."""
    rows = await session.execute(
        select(OrgMachine).where(OrgMachine.org_team_id == org_id, OrgMachine.deleted_at.is_(None))
    )
    return list(rows.scalars().all())


def _dedicated_url(org_id: UUID | str) -> str:
    return f"/admin/v1/orgs/{org_id}/compute/dedicated"


async def _decisions(org_id: UUID, entity: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


async def _ec2_type(session: AsyncSession) -> ComputeMachineType:
    return await make_machine_type(session, provider=EC2, provider_price_per_minute_nanos=3_200_000)


def _mint_body(mt: ComputeMachineType, **over: Any) -> dict[str, Any]:
    return {
        "label": "tideline-box",
        "provider": mt.provider,
        "instance_type": mt.provider_type_id,
        "region": "us-west-2",
        "tenancy": "dedicated",
        **over,
    }


async def _mint(
    client: AsyncClient, admin: OrgWithAdmin, mt: ComputeMachineType, **over: Any
) -> dict[str, Any]:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(MACHINES, json=_mint_body(mt, **over))
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _box_headers(box: OrgWithAdmin, credential: str | None, session: str) -> dict[str, str]:
    """What the box sends: its box user's device token, the agent assertion,
    and the machine credential beside them."""
    await _verify_email(box.admin_id)
    jwt = await mint_cli_token(user_id=box.admin_id, email=box.admin_email, org_team_id=box.org_id)
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers(session)}
    if credential is not None:
        headers.update(machine_credential_headers(credential))
    return headers


def _pod() -> str:
    """A fresh instance id: registrations outlive a test (released, never
    deleted), so a pod id reused across tests would find an old row."""
    return f"i-{uuid4().hex[:12]}"


def _claim(pod: str | None = None, **over: Any) -> dict[str, Any]:
    return {
        "provider_pod_id": pod or _pod(),
        "name": "box",
        "capacity": 8,
        "daemon_version": "0.5.0",
        # A real platform box reports the sandbox mode it enforces; a pool box is
        # gVisor (a "none" box would be kept off the pool by the placement guard).
        "sandbox": "gvisor",
        **over,
    }


# --------------------------------------------------------------------------- #
# the console: mint, list, revoke
# --------------------------------------------------------------------------- #


async def test_the_machines_page_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get(MACHINES)).status_code == 401
    assert (await client.post(MACHINES, json={})).status_code == 401


async def test_a_tenant_admin_cannot_reach_the_machines_page(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(MACHINES)).status_code == 403
    assert (await client.get(_dedicated_url(org_admin.org_id))).status_code == 403


async def test_support_reads_the_machines_page_and_the_allow_is_recorded(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(MACHINES)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": []}
    rows = await _decisions(platform_support.org_id, "platform_machine")
    assert _effects(rows) == [("allow", "staff_read")]
    (row,) = rows
    assert row.visibility == "platform"
    assert row.payload["policy"] == "platform.machine"
    assert row.payload["attrs"] == {
        "platform_staff": True,
        "platform_admin": False,
        "operation": "list",
        "org_exists": True,
    }


async def test_support_may_not_mint_and_the_deny_survives(
    client: AsyncClient, platform_support: OrgWithAdmin, real_session: AsyncSession
) -> None:
    mt = await _ec2_type(real_session)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(MACHINES, json=_mint_body(mt))
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "Platform admin role required"
    rows = await _decisions(platform_support.org_id, "platform_machine")
    assert _effects(rows) == [("deny", "platform_admin_required")]
    assert rows[0].payload["attrs"]["operation"] == "mint"
    async with AsyncSessionLocal() as session:
        held = await session.execute(select(MachineCredential))
        assert held.scalars().all() == []


async def test_an_admin_mints_a_credential_shown_once_and_stored_as_a_digest(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt)
    assert minted["credential"].startswith(MACHINE_TOKEN_PREFIX)
    machine = minted["machine"]
    assert machine["tenancy"] == "dedicated"
    assert machine["provider"] == EC2
    assert machine["instance_type"] == mt.provider_type_id
    assert machine["status"] == "none"
    assert machine["machine_id"] is None
    assert machine["true_cost_per_minute_nanos"] == 3_200_000

    async with AsyncSessionLocal() as session:
        row = await session.get(MachineCredential, UUID(machine["credential_id"]))
        assert row is not None
        assert row.token_hash != minted["credential"]
        assert minted["credential"] not in row.token_hash
    rows = await _decisions(platform_admin.org_id, "platform_machine")
    assert _effects(rows) == [("allow", "admin_changes")]

    listed = await client.get(MACHINES)
    assert [m["credential_id"] for m in listed.json()["items"]] == [machine["credential_id"]]
    assert "credential" not in listed.json()["items"][0]


async def test_minting_against_an_unknown_or_inactive_type_is_refused(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    inactive = await make_machine_type(real_session, provider=EC2, active=False)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    missing = await client.post(MACHINES, json=_mint_body(inactive, instance_type="no-such"))
    assert missing.status_code == 404
    off = await client.post(MACHINES, json=_mint_body(inactive))
    assert off.status_code == 404


async def test_a_refused_mint_answers_a_code_not_the_refusals_text(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ValueError`` raised anywhere under the mint can carry a host, a URL
    or a runtime's stderr; the answer is a code and a fixed sentence, and the
    text stays in the server log."""
    from backend.services.credentials import machine_credentials

    leak = "stderr: refused by https://10.0.4.17:8443/mint for i-0abc123def4567890"

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError(leak)

    mt = await _ec2_type(real_session)
    monkeypatch.setattr(machine_credentials, "mint", _refuse)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(MACHINES, json=_mint_body(mt))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "machine_mint_refused"
    for fragment in ("stderr", "https://", "10.0.4.17", "i-0abc123def4567890"):
        assert fragment not in resp.text


async def test_revoking_ends_the_boxs_standing_and_its_assignment(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt)
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    pod = _pod()
    claimed = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert claimed.status_code == 201, claimed.text
    machine_id = claimed.json()["id"]
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assigned = await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": machine_id})
    assert assigned.status_code == 200, assigned.text

    gone = await client.delete(f"{MACHINES}/{minted['machine']['credential_id']}")
    assert gone.status_code == 204
    again = await client.delete(f"{MACHINES}/{minted['machine']['credential_id']}")
    assert again.status_code == 204

    listed = await client.get(MACHINES)
    (row,) = listed.json()["items"]
    assert row["revoked_at"] is not None
    assert row["assigned_org_id"] is None
    assert (await client.get(_dedicated_url(org_admin.org_id))).json()["machine_id"] is None
    reclaim = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert reclaim.status_code == 401
    assert reclaim.json()["error"]["code"] == "machine_credential_refused"


async def test_revoking_an_unknown_credential_is_not_found(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.delete(f"{MACHINES}/{UUID(int=5)}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# the box: claim and heartbeat on its credential
# --------------------------------------------------------------------------- #


async def test_a_refused_claim_says_a_fixed_sentence_and_never_the_services_reason(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.compute import machines as machine_service

    leaked = "pod i-0abc123def at http://10.0.4.17:8443/internal was claimed by box-7"

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError(leaked)

    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="pool", label="pool-1")
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    monkeypatch.setattr(machine_service, "register_platform_machine", _refuse)

    resp = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=headers)
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "machine_claim_refused"
    assert error["message"] == "This machine credential can't claim this machine."
    for fragment in ("i-0abc123def", "10.0.4.17", "http://", "box-7", "claimed by"):
        assert fragment not in resp.text


async def test_a_box_claims_the_machine_its_credential_names_and_heartbeats_its_load(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="pool", label="pool-1")
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    pod = _pod()

    first = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["provider"] == EC2
    assert body["status"] == "starting"
    machine_id = body["id"]

    second = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert second.status_code == 200
    assert second.json()["id"] == machine_id

    beat = await client.post(
        f"/api/v1/machines/{machine_id}/heartbeat",
        json={"capacity": 10, "chats_served": 3, "daemon_version": "0.5.1"},
        headers=headers,
    )
    assert beat.status_code == 204
    silent = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    assert silent.status_code == 204

    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        assert alloc.tenancy == "pool"
        assert alloc.capacity == 10
        assert alloc.chats_served == 3
        assert alloc.daemon_version == "0.5.1"
        assert alloc.price_per_minute_nanos == 0
        assert alloc.true_cost_per_minute_nanos == 3_200_000
        assert alloc.grant_id is None
        credential = await session.get(MachineCredential, UUID(minted["machine"]["credential_id"]))
        assert credential is not None and credential.machine_id == alloc.id
        assert credential.last_used_at is None

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    (listed,) = (await client.get(MACHINES)).json()["items"]
    assert listed["status"] == "ready"
    # The page counts the chats bound to the box, not the beat's report of it.
    assert listed["chats_served"] == await _bound(listed["machine_id"])
    assert listed["capacity"] == 10
    assert listed["daemon_version"] == "0.5.1"
    assert listed["machine_name"] == "box"
    # Two claims and two beats, three rows. A box re-presents its credential on
    # every beat, so the beat IS a credential decision — which is what lets a
    # revoke reach a box that is already serving. But the second beat decided
    # exactly what the first did, and a box beats four times a minute forever,
    # so the repeat is not written again (see ``SettledRepeatSink``). Each claim
    # keeps its own row: only the heartbeat route is folded.
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows) == [("allow", "machine_self")] * 3
    assert rows[0].payload["attrs"] == {
        "is_machine": True,
        "credential_live": True,
        "machine_matches": True,
        "own_standing": True,
        "runs_org_workers": False,
        "org_reached": True,
    }


async def test_a_claim_without_a_credential_is_refused_and_registers_nothing(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    headers = await _box_headers(platform_admin, None, "box-1")
    pod = _pod()
    resp = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "machine_credential_required"
    assert resp.json()["error"]["message"] == "A machine credential is required"
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows) == [("deny", "machine_credential_required")]
    async with AsyncSessionLocal() as session:
        held = await session.execute(
            select(ComputeAllocation).where(ComputeAllocation.provider_machine_id == pod)
        )
        assert held.scalars().all() == []


async def test_a_stray_header_that_is_not_a_credential_reads_as_none(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    assert parse_machine_credential({"X-Alkera-Machine-Credential": "not-a-credential"}) is None
    headers = await _box_headers(platform_admin, None, "box-1")
    headers["X-Alkera-Machine-Credential"] = "Bearer nope"
    resp = await client.post("/api/v1/machines/claim", json=_claim(), headers=headers)
    assert resp.status_code == 401
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows) == [("deny", "machine_credential_required")]


async def test_a_credential_that_was_never_minted_is_refused(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    headers = await _box_headers(platform_admin, MACHINE_TOKEN_PREFIX + "x" * 64, "box-1")
    resp = await client.post("/api/v1/machines/claim", json=_claim(), headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "machine_credential_refused"
    assert resp.json()["error"]["message"] == "Machine credential refused"
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows) == [("deny", "credential_refused")]
    assert rows[0].payload["attrs"]["is_machine"] is True


async def test_a_credential_claimed_by_one_box_cannot_be_claimed_by_another(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt)
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    first, second = _pod(), _pod()
    assert (
        await client.post("/api/v1/machines/claim", json=_claim(first), headers=headers)
    ).status_code == 201

    other = await client.post("/api/v1/machines/claim", json=_claim(second), headers=headers)
    assert other.status_code == 404
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows) == [("allow", "machine_self"), ("deny", "not_this_machine")]
    async with AsyncSessionLocal() as session:
        held = await session.execute(
            select(ComputeAllocation).where(ComputeAllocation.provider_machine_id == second)
        )
        assert held.scalars().all() == []


async def test_the_credential_decides_what_the_box_is_not_the_box(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A dedicated credential registers a dedicated machine whatever the box
    says, and the plain register route (a member's own box, admitted by a
    grant) is not how a platform box comes up: it has no grant and is refused
    there."""
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="dedicated")
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    claimed = await client.post("/api/v1/machines/claim", json=_claim(), headers=headers)
    assert claimed.status_code == 201
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(claimed.json()["id"]))
        assert alloc is not None and alloc.tenancy == "dedicated"

    plain = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": EC2,
            "provider_pod_id": "i-plain",
            "name": "box",
            "machine_type_code": mt.provider_type_id,
        },
        headers=headers,
    )
    assert plain.status_code == 429


async def test_a_revoked_credential_refuses_the_running_boxs_next_heartbeat(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Revoking is how a platform admin takes a box away, and it has to reach a
    box that is already serving. The beat carries the credential, so the beat
    after the revoke is refused — which is what makes the box hand its chats
    back instead of serving on until the meter reaps it twenty minutes later."""
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="pool", label="pool-1")
    headers = await _box_headers(platform_admin, minted["credential"], "box-1")
    claimed = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=headers)
    assert claimed.status_code == 201, claimed.text
    machine_id = claimed.json()["id"]
    beat = f"/api/v1/machines/{machine_id}/heartbeat"
    assert (await client.post(beat, headers=headers)).status_code == 204

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    revoked = await client.delete(f"{MACHINES}/{minted['machine']['credential_id']}")
    assert revoked.status_code == 204

    after = await client.post(beat, headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "machine_credential_refused"
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows)[-1] == ("deny", "credential_refused")


async def test_a_box_beating_on_another_boxs_credential_is_not_found(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A machine id is on every chat the box serves, so learning one must not be
    enough to beat for it: the credential has to be the one that claimed it."""
    mt = await _ec2_type(real_session)
    mine = await _mint(client, platform_admin, mt, tenancy="pool", label="mine")
    theirs = await _mint(client, platform_admin, mt, tenancy="pool", label="theirs")
    my_headers = await _box_headers(platform_admin, mine["credential"], "box-mine")
    claimed = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=my_headers)
    machine_id = claimed.json()["id"]

    their_headers = await _box_headers(platform_admin, theirs["credential"], "box-theirs")
    beat = await client.post(
        f"/api/v1/machines/{machine_id}/heartbeat", json={"chats_served": 99}, headers=their_headers
    )

    assert beat.status_code == 404
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        assert alloc.chats_served == 0


async def test_an_org_box_still_beats_with_no_credential_at_all(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The credential check is only for a box that presents one: a customer's
    own box registers under its org's grant and never carries one."""
    from tests._compute_helpers import make_grant

    mt = await make_machine_type(real_session, provider="runpod")
    await make_grant(
        real_session,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        created_by=org_admin.admin_id,
    )
    await _verify_email(org_admin.admin_id)
    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers("own-box")}
    registered = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": _pod(),
            "name": "their own box",
            "machine_type_code": mt.provider_type_id,
        },
        headers=headers,
    )
    assert registered.status_code == 201, registered.text

    beat = await client.post(
        f"/api/v1/machines/{registered.json()['id']}/heartbeat",
        json={"capacity": 4, "chats_served": 1, "daemon_version": "0.5.0"},
        headers=headers,
    )

    assert beat.status_code == 204


async def _register_org_box(
    client: AsyncClient,
    owner: OrgWithAdmin,
    real_session: AsyncSession,
    *,
    chats_served: int = 2,
) -> str:
    """An org's own box, registered under its grant with no credential, which
    then beats once with its load. Returns the machine id."""
    from tests._compute_helpers import make_grant

    mt = await make_machine_type(real_session, provider="runpod")
    await make_grant(
        real_session, org_team_id=owner.org_id, machine_type_id=mt.id, created_by=owner.admin_id
    )
    await _verify_email(owner.admin_id)
    jwt = await mint_cli_token(
        user_id=owner.admin_id, email=owner.admin_email, org_team_id=owner.org_id
    )
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers(f"own-{uuid4().hex[:8]}")}
    # A console session cookie outranks the bearer; the box is its org's device.
    client.cookies.clear()
    registered = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": _pod(),
            "name": "their own box",
            "machine_type_code": mt.provider_type_id,
        },
        headers=headers,
    )
    assert registered.status_code == 201, registered.text
    machine_id = str(registered.json()["id"])
    beat = await client.post(
        f"/api/v1/machines/{machine_id}/heartbeat",
        json={"capacity": 4, "chats_served": chats_served, "daemon_version": "0.5.0"},
        headers=headers,
    )
    assert beat.status_code == 204, beat.text
    return machine_id


async def test_the_machines_page_lists_an_org_box_that_has_no_credential(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """A box an org registered on its own grant serves chats with no platform
    credential behind it; the fleet page still shows it, once, as registered."""
    machine_id = await _register_org_box(client, org_admin, real_session, chats_served=2)

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    listed = await client.get(MACHINES)

    assert listed.status_code == 200, listed.text
    rows = [m for m in listed.json()["items"] if m["id"] == machine_id]
    assert len(rows) == 1
    (row,) = rows
    assert row["origin"] == "registered"
    assert row["credential_id"] is None
    assert row["tenancy"] == "org"
    assert row["dedicated_org"] is None
    assert row["machine_id"] == machine_id
    assert row["liveness"] == "ready"
    assert row["status"] == "ready"
    assert row["chats_served"] == await _bound(machine_id)
    assert row["capacity"] == 4
    assert row["heartbeat_at"] is not None

    detail = await client.get(f"{MACHINES}/{machine_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == machine_id
    assert detail.json()["credential_id"] is None
    assert detail.json()["origin"] == "registered"
    assert detail.json()["chats_served"] == await _bound(machine_id)
    # Reading it is the fleet page's; draining or terminating it is not.
    drained = await client.post(f"{MACHINES}/{machine_id}/drain", json={"reason": "no"})
    assert drained.status_code == 404


async def test_the_box_reported_load_stays_the_box_while_the_console_splits_parked_chats(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """A box's heartbeat counts every session it mirrors, parked ones too, and
    that figure is what placement reads; the console reads the chats instead.
    Reading the console never rewrites the box's figure, and placement's own
    bound count still counts the parked chats."""
    from alkera_core.models import WorkspaceObject

    machine_id = await _register_org_box(client, org_admin, real_session, chats_served=5)
    async with AsyncSessionLocal() as session:
        for i in range(5):
            session.add(
                WorkspaceObject(
                    org_team_id=org_admin.org_id,
                    logical_id=f"beat-{uuid4().hex[:10]}",
                    namespace="workspace",
                    type="chat",
                    title="",
                    version=1,
                    status="ready",
                    spec={
                        "machine_id": machine_id,
                        "mirror_state": "asleep" if i < 3 else "awake",
                    },
                    owner_user_id=org_admin.admin_id,
                    visibility_scope="private",
                )
            )
        await session.commit()

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    row = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}[machine_id]
    assert (row["chats_served"], row["chats_asleep"]) == (2, 3)
    detail = (await client.get(f"{MACHINES}/{machine_id}")).json()
    assert (detail["chats_served"], detail["chats_asleep"]) == (2, 3)

    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        assert alloc.chats_served == 5
    assert await _bound(machine_id) == 5


@pytest.mark.parametrize(
    ("state", "listed"),
    [
        pytest.param("released", False, id="released-is-gone"),
        pytest.param("failed", False, id="failed-is-gone"),
        pytest.param("lost", True, id="lost-stays-visible"),
    ],
)
async def test_a_box_past_its_life_leaves_the_machines_page(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    state: str,
    listed: bool,
) -> None:
    machine_id = await _register_org_box(client, org_admin, real_session)
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = state
        await session.commit()

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    ids = [m["id"] for m in (await client.get(MACHINES)).json()["items"]]

    assert (machine_id in ids) is listed


async def test_a_claimed_credentials_box_is_one_row_beside_an_unclaimed_credential(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """The machine a credential claimed is reached through its credential, so
    the standing-machine walk must not list it a second time; a credential no
    box claimed yet is still a row of its own, and an org's box sits beside them."""
    mt = await _ec2_type(real_session)
    minted, claimed_id = await _claimed_box(client, platform_admin, mt, label="claimed")
    idle = await _mint(client, platform_admin, mt, label="idle")
    own_id = await _register_org_box(client, org_admin, real_session)

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    items = (await client.get(MACHINES)).json()["items"]

    assert [m["id"] for m in items].count(claimed_id) == 1
    (claimed,) = [m for m in items if m["id"] == claimed_id]
    assert claimed["credential_id"] == minted["machine"]["credential_id"]
    (unclaimed,) = [m for m in items if m["credential_id"] == idle["machine"]["credential_id"]]
    assert unclaimed["machine_id"] is None
    assert unclaimed["label"] == "idle"
    assert [m["id"] for m in items].count(own_id) == 1
    assert len(items) == 3


# --------------------------------------------------------------------------- #
# dedicating a box to an org
# --------------------------------------------------------------------------- #


async def _claimed_box(
    client: AsyncClient, platform_admin: OrgWithAdmin, mt: ComputeMachineType, **over: Any
) -> tuple[dict[str, Any], str]:
    minted = await _mint(client, platform_admin, mt, **over)
    headers = await _box_headers(
        platform_admin, minted["credential"], f"box-{minted['machine']['label']}"
    )
    pod = f"i-{minted['machine']['credential_id'][:8]}"
    claimed = await client.post("/api/v1/machines/claim", json=_claim(pod), headers=headers)
    assert claimed.status_code == 201, claimed.text
    # Beats as a current box does: one worker process per org, each in
    # namespaces of its own, so a pool box is not held to one org.
    await client.post(
        f"/api/v1/machines/{claimed.json()['id']}/heartbeat",
        json={"capabilities": [BoxCapability.ORG_WORKERS, BoxCapability.ORG_ISOLATION]},
        headers=headers,
    )
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    return minted, str(claimed.json()["id"])


async def test_an_admin_assigns_a_dedicated_box_and_the_org_runs_on_it_alone(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mt = await _ec2_type(real_session)
    _, pool_id = await _claimed_box(client, platform_admin, mt, tenancy="pool", label="pool-1")
    _, dedicated_id = await _claimed_box(client, platform_admin, mt, label="theirs")

    before = await client.get(_dedicated_url(org_admin.org_id))
    assert before.status_code == 200
    assert before.json() == {
        "org_team_id": str(org_admin.org_id),
        "machine_id": None,
        "fallback_to_pool": False,
        "machine": None,
        "assigned_at": None,
    }

    resp = await client.put(
        _dedicated_url(org_admin.org_id),
        json={"machine_id": dedicated_id, "fallback_to_pool": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine_id"] == dedicated_id
    assert body["fallback_to_pool"] is True
    assert body["machine"]["assigned_org_id"] == str(org_admin.org_id)
    assert body["machine"]["label"] == "theirs"

    # The box is held as the org's granted pool machine, never as the
    # one-box-per-org row, and serves no other org.
    async with AsyncSessionLocal() as session:
        held = await _org_machines(session, org_admin.org_id)
        assert [(om.acquisition, om.use_mode, str(om.current_allocation_id)) for om in held] == [
            ("granted", "pool", dedicated_id)
        ]
        box = await session.get(ComputeAllocation, UUID(dedicated_id))
        assert box is not None
        assert (box.org_machine_id, box.tenant_org_id) == (held[0].id, org_admin.org_id)
        assert held[0].free_until is not None and held[0].free_until > datetime.now(UTC)
        assert await session.get(OrgComputeAssignment, org_admin.org_id) is None
        fallback = await session.get(OrgComputeSettings, org_admin.org_id)
        assert fallback is not None and fallback.shared_pool_fallback is True
        assert await _org_machines(session, platform_admin.org_id) == []
    assert pool_id != dedicated_id

    rows = await _decisions(platform_admin.org_id, "platform_machine")
    assert ("allow", "admin_changes") in _effects(rows)
    assign_row = [r for r in rows if r.payload["attrs"]["operation"] == "assign"]
    assert len(assign_row) == 1
    assert assign_row[0].payload["attrs"]["org_exists"] is True

    (listed,) = [m for m in (await client.get(MACHINES)).json()["items"] if m["label"] == "theirs"]
    assert listed["assigned_org_id"] == str(org_admin.org_id)
    assert listed["assigned_org_name"]


async def test_dedicated_assign_name_taken(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """The org already has a machine called what the box is labelled (names are
    unique per org without case). Holding the box takes the next free name
    rather than failing the insert: no concurrency is needed for this to be a
    500 when the name is used as it comes."""
    mt = await _ec2_type(real_session)
    _, dedicated_id = await _claimed_box(client, platform_admin, mt, label="theirs")
    async with AsyncSessionLocal() as session:
        box = await session.get(ComputeAllocation, UUID(dedicated_id))
        assert box is not None and box.name
        wanted = box.name
    await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type=mt,
        name=wanted.upper(),
        with_allocation=False,
    )

    resp = await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": dedicated_id})

    assert resp.status_code == 200, resp.text
    async with AsyncSessionLocal() as session:
        names = sorted(om.name for om in await _org_machines(session, org_admin.org_id))
    assert names == sorted([wanted.upper(), f"{wanted} 2"])


async def test_a_free_machine_name_steps_past_every_taken_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _ec2_type(real_session)
    for name in ("Machine 1", "machine 1 2"):
        await make_org_machine(
            real_session,
            org_id=org_admin.org_id,
            user_id=org_admin.admin_id,
            machine_type=mt,
            name=name,
            with_allocation=False,
        )
    long = "x" * 64
    await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type=mt,
        name=long,
        with_allocation=False,
    )

    free = await free_machine_name(real_session, org_id=org_admin.org_id, wanted="Machine 1")
    assert free == "Machine 1 3"
    assert await free_machine_name(real_session, org_id=org_admin.org_id, wanted="spare") == "spare"
    assert await free_machine_name(real_session, org_id=org_admin.org_id, wanted=long) == (
        "x" * 62 + " 2"
    )


async def test_unassigning_returns_the_org_to_the_pool(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mt = await _ec2_type(real_session)
    _, pool_id = await _claimed_box(client, platform_admin, mt, tenancy="pool", label="pool-1")
    _, dedicated_id = await _claimed_box(client, platform_admin, mt, label="theirs")
    assert (
        await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": dedicated_id})
    ).status_code == 200

    resp = await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": None})
    assert resp.status_code == 200
    assert resp.json()["machine_id"] is None
    rows = await _decisions(platform_admin.org_id, "platform_machine")
    assert [r.payload["attrs"]["operation"] for r in rows][-1] == "unassign"

    async with AsyncSessionLocal() as session:
        assert await session.get(OrgComputeAssignment, org_admin.org_id) is None
        assert await _org_machines(session, org_admin.org_id) == []
        box = await session.get(ComputeAllocation, UUID(dedicated_id))
        assert box is not None and box.org_machine_id is None
    assert pool_id != dedicated_id


async def test_a_box_dedicated_to_one_org_cannot_be_given_to_another(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mt = await _ec2_type(real_session)
    _, dedicated_id = await _claimed_box(client, platform_admin, mt, label="theirs")
    assert (
        await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": dedicated_id})
    ).status_code == 200
    taken = await client.put(
        _dedicated_url(platform_admin.org_id), json={"machine_id": dedicated_id}
    )
    assert taken.status_code == 409
    async with AsyncSessionLocal() as session:
        assert await session.get(OrgComputeAssignment, platform_admin.org_id) is None
        assert await _org_machines(session, platform_admin.org_id) == []


async def test_a_box_an_org_let_go_of_is_not_handed_to_another_org(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """Unassigning does not clean a disk. The box may still hold the first
    org's chat files, so a second org gets a fresh machine, never this one;
    the first org may have it back."""
    mt = await _ec2_type(real_session)
    _, dedicated_id = await _claimed_box(client, platform_admin, mt, label="theirs")
    first = _dedicated_url(org_admin.org_id)
    assert (await client.put(first, json={"machine_id": dedicated_id})).status_code == 200
    assert (await client.put(first, json={"machine_id": None})).status_code == 200

    moved = await client.put(
        _dedicated_url(platform_admin.org_id), json={"machine_id": dedicated_id}
    )

    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "machine_held_another_org"
    async with AsyncSessionLocal() as session:
        assert await session.get(OrgComputeAssignment, platform_admin.org_id) is None
        assert await _org_machines(session, platform_admin.org_id) == []
        box = await session.get(ComputeAllocation, UUID(dedicated_id))
        assert box is not None and box.tenant_org_id == org_admin.org_id
    back = await client.put(first, json={"machine_id": dedicated_id})
    assert back.status_code == 200, back.text


@pytest.mark.parametrize(
    "tenancy", [pytest.param("pool", id="a-pool-box"), pytest.param(None, id="no-box-at-all")]
)
async def test_only_a_registered_dedicated_box_can_be_assigned(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    tenancy: str | None,
) -> None:
    machine_id = str(UUID(int=7))
    if tenancy is not None:
        mt = await _ec2_type(real_session)
        _, machine_id = await _claimed_box(client, platform_admin, mt, tenancy=tenancy, label="p")
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": machine_id})
    assert resp.status_code == 404
    assert (
        await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": "nope"})
    ).status_code == 404


async def test_support_reads_an_assignment_but_may_not_change_it(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    assert (await client.get(_dedicated_url(org_admin.org_id))).status_code == 200
    resp = await client.put(_dedicated_url(org_admin.org_id), json={"machine_id": None})
    assert resp.status_code == 403
    rows = await _decisions(platform_support.org_id, "platform_machine")
    assert _effects(rows) == [("allow", "staff_read"), ("deny", "platform_admin_required")]


async def test_an_unknown_org_is_an_opaque_not_found_to_staff(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    missing = UUID(int=42)
    assert (await client.get(_dedicated_url(missing))).status_code == 404
    assert (await client.put(_dedicated_url(missing), json={"machine_id": None})).status_code == 404
    rows = await _decisions(platform_admin.org_id, "platform_machine")
    assert _effects(rows) == [("deny", "org_not_found"), ("deny", "org_not_found")]


async def test_a_platform_box_is_metered_at_its_true_cost_and_billed_nothing(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The plane runs the box for its tenants: the row carries the catalog's
    true cost for the margin ledger and a zero rate, with no grant and no
    funding account, so the meter can never debit a customer for it."""
    mt = await _ec2_type(real_session)
    _, machine_id = await _claimed_box(client, platform_admin, mt, label="theirs")
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        assert alloc.true_cost_per_minute_nanos == 3_200_000
        assert alloc.price_per_minute_nanos == 0
        assert await compute_funding().allocation_funding(session, alloc.id) is None
        assert alloc.grant_id is None
        assert alloc.created_at > datetime.now(UTC) - timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# the beat's decision is kept, its repeats are not
# --------------------------------------------------------------------------- #


async def _retire(*machine_ids: str) -> None:
    """Release the pool boxes a case left ready. A ready pool box is placeable
    by ANY org, so one left behind binds itself to the next test's chat."""
    async with AsyncSessionLocal() as session:
        for machine_id in machine_ids:
            alloc = await session.get(ComputeAllocation, UUID(machine_id))
            if alloc is not None:
                alloc.state = "released"
                alloc.released_at = datetime.now(UTC)
        await session.commit()


@pytest.fixture(autouse=True)
def _forget_beats() -> Iterator[None]:
    """Each case starts with no remembered beat, so one test's suppressed run
    cannot decide what the next one records."""
    heartbeat_decision_sink().clear()
    yield
    heartbeat_decision_sink().clear()


async def test_a_run_of_identical_beats_leaves_one_decision_row(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A box beats every fifteen seconds forever. The first beat's allow is the
    record that it was entitled to serve; the identical copies behind it say
    nothing more and used to accumulate at four rows a minute per box, so they
    are dropped for an hour."""
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="pool", label="steady")
    headers = await _box_headers(platform_admin, minted["credential"], "box-steady")
    claimed = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=headers)
    assert claimed.status_code == 201, claimed.text
    machine_id = claimed.json()["id"]
    beat = f"/api/v1/machines/{machine_id}/heartbeat"

    before = len(await _decisions(platform_admin.org_id, "machine_credential"))
    for _ in range(3):
        assert (await client.post(beat, headers=headers)).status_code == 204

    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows[before:]) == [("allow", "machine_self")]
    await _retire(machine_id)


async def test_a_beat_after_a_revoke_is_recorded_however_long_the_run_before_it(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Suppression only ever hides a repeat of what is already on record. The
    beat that changes — the refusal after an admin takes the credential away —
    is the one an auditor needs, so it is written whatever came before it."""
    mt = await _ec2_type(real_session)
    minted = await _mint(client, platform_admin, mt, tenancy="pool", label="revoked-run")
    headers = await _box_headers(platform_admin, minted["credential"], "box-revoked")
    claimed = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=headers)
    machine_id = claimed.json()["id"]
    beat = f"/api/v1/machines/{machine_id}/heartbeat"
    before = len(await _decisions(platform_admin.org_id, "machine_credential"))
    for _ in range(3):
        assert (await client.post(beat, headers=headers)).status_code == 204

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (
        await client.delete(f"{MACHINES}/{minted['machine']['credential_id']}")
    ).status_code == 204

    assert (await client.post(beat, headers=headers)).status_code == 401
    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows[before:]) == [
        ("allow", "machine_self"),
        ("deny", "credential_refused"),
    ]
    await _retire(machine_id)


async def test_a_second_boxs_beat_is_its_own_run_not_a_repeat_of_the_first(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Two boxes in one org beat the same route with the same decision behind
    them. They are different credentials, so the second box's first beat is the
    first record of ITS entitlement, not a repeat of the other box's."""
    mt = await _ec2_type(real_session)
    beats: list[tuple[str, dict[str, str]]] = []
    machine_ids: list[str] = []
    for label in ("twin-a", "twin-b"):
        minted = await _mint(client, platform_admin, mt, tenancy="pool", label=label)
        headers = await _box_headers(platform_admin, minted["credential"], f"box-{label}")
        claimed = await client.post("/api/v1/machines/claim", json=_claim(_pod()), headers=headers)
        assert claimed.status_code == 201, claimed.text
        machine_ids.append(claimed.json()["id"])
        beats.append((f"/api/v1/machines/{machine_ids[-1]}/heartbeat", headers))

    before = len(await _decisions(platform_admin.org_id, "machine_credential"))
    for path, headers in beats:
        for _ in range(2):
            assert (await client.post(path, headers=headers)).status_code == 204

    rows = await _decisions(platform_admin.org_id, "machine_credential")
    assert _effects(rows[before:]) == [
        ("allow", "machine_self"),
        ("allow", "machine_self"),
    ]
    await _retire(*machine_ids)
