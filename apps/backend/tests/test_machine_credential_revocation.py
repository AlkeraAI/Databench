"""A platform box whose machine credential is gone speaks for nothing.

A pool or dedicated box boots with a credential a platform admin minted, claims
its machine with it, and from then on speaks through its box user's device
token plus the agent assertion naming the machine. Revoking the credential is
how the platform takes the box away, so the revoke has to reach every door the
box can knock on — not only the heartbeat that re-presents the credential, but
the chat relay routes, the socket ticket, the machine banner and the Files
drive, none of which carry the credential header at all.

Each door is driven through the real route with the box's real session. The
refusal is ONE answer — a 401 carrying ``machine_credential_refused`` — for a
revoked credential, a credential that was rotated away and a credential nobody
ever minted, so the answer says nothing about which it was; and the refusal is
on record as an ``authz.decision`` row.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX, machine_credential_headers
from alkera_core.authz import agent_headers
from alkera_core.compute.machines import verify_machine_assertion
from alkera_core.compute.provider import EC2
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, MachineCredential, OrgComputeAssignment, User
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from backend.api.routes.compute.machines import heartbeat_decision_sink
from backend.services.chats import chat_service
from freezegun import freeze_time
from httpx import AsyncClient, Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MACHINES = "/admin/v1/machines"
REFUSED = "machine_credential_refused"


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the ready window; a loaded runner must not close it
    mid-case and turn a credential answer into a liveness one."""
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


@pytest.fixture(autouse=True)
def _forget_beats() -> Iterator[None]:
    heartbeat_decision_sink().clear()
    yield
    heartbeat_decision_sink().clear()


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy.in_(("pool", "dedicated")))
        .values(state="released")
    )
    await real_session.commit()


async def _verify_email(user_id: UUID) -> None:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.email_verified_at = datetime.now(UTC)
        await session.commit()


async def _mint(client: AsyncClient, admin: OrgWithAdmin, **over: Any) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        mt = await make_machine_type(
            session, provider=EC2, provider_price_per_minute_nanos=3_200_000
        )
        await session.commit()
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(
        MACHINES,
        json={
            "label": "box",
            "provider": mt.provider,
            "instance_type": mt.provider_type_id,
            "region": "us-west-2",
            "tenancy": "dedicated",
            **over,
        },
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _revoke(client: AsyncClient, admin: OrgWithAdmin, credential_id: str) -> None:
    await login(client, admin.admin_email, admin.admin_password)
    assert (await client.delete(f"{MACHINES}/{credential_id}")).status_code == 204
    client.cookies.clear()


class Box:
    """One platform box: its device token, its credential, its machine."""

    def __init__(self, token: str, credential: str, pod: str) -> None:
        self.token = token
        self.credential = credential
        self.pod = pod
        self.machine_id = ""

    def as_machine(self, *, credential: bool = False) -> dict[str, str]:
        """What the box sends once it has adopted its machine id."""
        headers = {"Authorization": f"Bearer {self.token}", **agent_headers(self.machine_id)}
        if credential:
            headers.update(machine_credential_headers(self.credential))
        return headers

    def booting(self, credential: str | None = None) -> dict[str, str]:
        """What the box sends before it knows its machine id."""
        headers = {"Authorization": f"Bearer {self.token}", **agent_headers("booting")}
        headers.update(machine_credential_headers(credential or self.credential))
        return headers


async def _claimed_box(
    client: AsyncClient, admin: OrgWithAdmin, *, pod: str | None = None, **mint: Any
) -> tuple[Box, dict[str, Any]]:
    minted = await _mint(client, admin, **mint)
    # The box speaks with its own device token only: the console session the
    # mint signed in with would otherwise win over the Bearer (cookie first).
    client.cookies.clear()
    await _verify_email(admin.admin_id)
    token = await mint_cli_token(
        user_id=admin.admin_id, email=admin.admin_email, org_team_id=admin.org_id
    )
    box = Box(token, minted["credential"], pod or f"i-{uuid4().hex[:12]}")
    claimed = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": box.pod, "name": "box", "capacity": 8, "daemon_version": "1"},
        headers=box.booting(),
    )
    assert claimed.status_code in (200, 201), claimed.text
    box.machine_id = claimed.json()["id"]
    return box, minted


async def _bound_chat(real_session: AsyncSession, admin: OrgWithAdmin, machine_id: str) -> str:
    owner, _ = await make_member(real_session, org_id=admin.org_id, verified=True)
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Quarterly plan",
        client_id=None,
        machine_id=machine_id,
        machine_status="ready",
    )
    await real_session.commit()
    return str(chat.id)


async def _refusals(org_id: UUID) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "machine_credential",
            )
            .order_by(EventOutbox.id)
        )
        return [r.payload for r in rows.scalars().all() if r.payload["effect"] == "deny"]


def _opaque(resp: Response) -> dict[str, Any]:
    """The refusal a client sees, without the per-request trace id."""
    error = dict(resp.json()["error"])
    error.pop("trace_id", None)
    return {"status": resp.status_code, **error}


def _doors(
    box: Box, chat_id: str
) -> dict[str, tuple[str, str, dict[str, Any] | None, dict[str, str]]]:
    """Every door a platform box knocks on, spelled as the daemon spells it."""
    return {
        "chat-read": ("GET", f"/api/v1/chats/{chat_id}", None, box.as_machine()),
        "chat-messages": ("GET", f"/api/v1/chats/{chat_id}/messages", None, box.as_machine()),
        "publisher-state": (
            "PUT",
            f"/api/v1/chats/{chat_id}/publisher-state",
            {"state": "publishing", "reason": ""},
            box.as_machine(),
        ),
        "socket-ticket": ("POST", "/api/v1/ws/tickets", None, box.as_machine()),
        "machine-banner": ("GET", "/api/v1/machines/current", None, box.as_machine()),
        "files-drive": ("GET", "/api/v1/files/drives", None, box.as_machine()),
        "heartbeat-with-credential": (
            "POST",
            f"/api/v1/machines/{box.machine_id}/heartbeat",
            None,
            box.as_machine(credential=True),
        ),
        "heartbeat-without-credential": (
            "POST",
            f"/api/v1/machines/{box.machine_id}/heartbeat",
            None,
            box.as_machine(),
        ),
        "claim-again": (
            "POST",
            "/api/v1/machines/claim",
            {"provider_pod_id": box.pod, "name": "box", "capacity": 8, "daemon_version": "1"},
            box.as_machine(credential=True),
        ),
    }


DOORS = [
    "chat-read",
    "chat-messages",
    "publisher-state",
    "socket-ticket",
    "machine-banner",
    "files-drive",
    "heartbeat-with-credential",
    "heartbeat-without-credential",
    "claim-again",
]


@pytest.mark.parametrize("door", DOORS)
async def test_a_revoked_box_is_refused_at_every_door(
    door: str, client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    box, minted = await _claimed_box(client, platform_admin)
    chat_id = await _bound_chat(real_session, platform_admin, box.machine_id)
    method, path, body, headers = _doors(box, chat_id)[door]

    # The box serves: its read of the chat it publishes lands.
    before = await client.get(f"/api/v1/chats/{chat_id}", headers=box.as_machine())
    assert before.status_code == 200, before.text

    await _revoke(client, platform_admin, minted["machine"]["credential_id"])
    client.cookies.clear()
    refused_before = len(await _refusals(platform_admin.org_id))

    resp = await client.request(method, path, json=body, headers=headers)

    assert resp.status_code == 401, (door, resp.text)
    assert resp.json()["error"]["code"] == REFUSED, (door, resp.text)
    rows = await _refusals(platform_admin.org_id)
    assert len(rows) == refused_before + 1, door
    assert rows[-1]["reason"] == "credential_refused"
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(box.machine_id))
        assert alloc is not None
        assert alloc.chats_served == 0, "a refused beat stamps nothing"


async def test_a_live_platform_box_must_present_its_credential_on_every_beat(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    """The heartbeat is where a platform box re-proves its credential. A beat
    that leaves the header off would otherwise be decided on the box user's
    session alone, and a box could keep its standing by never sending it."""
    box, _ = await _claimed_box(client, platform_admin)
    beat = f"/api/v1/machines/{box.machine_id}/heartbeat"

    bare = await client.post(beat, json={"chats_served": 5}, headers=box.as_machine())
    assert bare.status_code == 401, bare.text
    assert bare.json()["error"]["code"] == "machine_credential_required"

    good = await client.post(
        beat, json={"chats_served": 5}, headers=box.as_machine(credential=True)
    )
    assert good.status_code == 204, good.text


async def test_revoked_unknown_and_rotated_away_credentials_read_the_same(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    """Three dead credentials, one answer: nothing in the response tells a
    prober which of them named something real."""
    revoked_box, revoked = await _claimed_box(client, platform_admin)
    await _revoke(client, platform_admin, revoked["machine"]["credential_id"])

    rotated_box, rotated_away = await _claimed_box(client, platform_admin)
    # The same pod claims again under a freshly minted credential: rotation.
    fresh = await _mint(client, platform_admin)
    client.cookies.clear()
    again = await client.post(
        "/api/v1/machines/claim",
        json={
            "provider_pod_id": rotated_box.pod,
            "name": "box",
            "capacity": 8,
            "daemon_version": "1",
        },
        headers=rotated_box.booting(fresh["credential"]),
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == rotated_box.machine_id

    unknown = MACHINE_TOKEN_PREFIX + "x" * 64
    claim = {
        "provider_pod_id": f"i-{uuid4().hex[:12]}",
        "name": "box",
        "capacity": 8,
        "daemon_version": "1",
    }
    answers = {
        "revoked": await client.post(
            "/api/v1/machines/claim", json=claim, headers=revoked_box.booting()
        ),
        "rotated-away": await client.post(
            "/api/v1/machines/claim",
            json=claim,
            headers=rotated_box.booting(rotated_away["credential"]),
        ),
        "never-minted": await client.post(
            "/api/v1/machines/claim", json=claim, headers=revoked_box.booting(unknown)
        ),
    }
    shapes = {label: _opaque(resp) for label, resp in answers.items()}
    assert shapes["revoked"] == shapes["rotated-away"] == shapes["never-minted"], shapes
    assert shapes["revoked"]["status"] == 401
    assert shapes["revoked"]["code"] == REFUSED

    # The rotated-away credential no longer beats for the machine; the new one does.
    beat = f"/api/v1/machines/{rotated_box.machine_id}/heartbeat"
    old = await client.post(beat, headers=rotated_box.as_machine(credential=True))
    assert old.status_code == 401, old.text
    rotated_box.credential = fresh["credential"]
    new = await client.post(beat, headers=rotated_box.as_machine(credential=True))
    assert new.status_code == 204, new.text


async def test_a_revoked_box_does_not_take_its_row_back_through_register(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    """``/register`` is the org's own box's door. A platform box whose
    credential was revoked presenting its pod id there must not be handed its
    machine row back — that would restore the standing the revoke took away."""
    box, minted = await _claimed_box(client, platform_admin)
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(box.machine_id))
        assert alloc is not None
        machine_type = await session.get(ComputeMachineType, alloc.machine_type_id)
        assert machine_type is not None
        type_code = machine_type.provider_type_id
        jti_before = alloc.registered_jti
    await _revoke(client, platform_admin, minted["machine"]["credential_id"])
    client.cookies.clear()

    fresh_token = await mint_cli_token(
        user_id=platform_admin.admin_id,
        email=platform_admin.admin_email,
        org_team_id=platform_admin.org_id,
    )
    resp = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": EC2,
            "provider_pod_id": box.pod,
            "name": "box",
            "machine_type_code": type_code,
        },
        headers={"Authorization": f"Bearer {fresh_token}", **agent_headers("booting")},
    )

    assert resp.status_code not in (200, 201) or resp.json()["id"] != box.machine_id, resp.text
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(box.machine_id))
        assert alloc is not None
        assert alloc.registered_jti == jti_before, "the platform row was re-pinned"


async def test_the_assertion_seam_refuses_a_box_whose_credential_is_gone(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    """The one verification every surface consults — REST access facts, the
    socket admission, the Files lease holder — answers ``False`` once the
    credential that holds the machine is revoked."""
    box, minted = await _claimed_box(client, platform_admin)
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(box.machine_id))
        assert alloc is not None
        jti = alloc.registered_jti

    async def verified() -> bool:
        async with AsyncSessionLocal() as session:
            return await verify_machine_assertion(
                session,
                machine_id=box.machine_id,
                org_id=platform_admin.org_id,
                operator_user_id=platform_admin.admin_id,
                credential_id=jti,
            )

    assert await verified() is True
    await _revoke(client, platform_admin, minted["machine"]["credential_id"])
    assert await verified() is False


async def test_an_expired_box_session_is_refused_at_the_relay(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The box's session expires like any device token: past its lifetime the
    relay answers 401, whatever the credential says."""
    with freeze_time(datetime.now(UTC), real_asyncio=True) as frozen:
        box, _ = await _claimed_box(client, platform_admin)
        chat_id = await _bound_chat(real_session, platform_admin, box.machine_id)
        client.cookies.clear()
        live = await client.get(f"/api/v1/chats/{chat_id}", headers=box.as_machine())
        assert live.status_code == 200, live.text
        frozen.move_to(
            datetime.now(UTC) + timedelta(seconds=settings.auth_cli_token_ttl_seconds + 60)
        )
        expired = await client.get(f"/api/v1/chats/{chat_id}", headers=box.as_machine())
    assert expired.status_code == 401, expired.text
