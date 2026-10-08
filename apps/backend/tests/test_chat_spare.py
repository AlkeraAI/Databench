"""A chat warmed ahead of its owner's first message, through the real routes.

The wait a person feels on the first message of a new chat is the box taking
the chat's folder and spawning the agent. While a person is on the chat page
the server keeps one chat warmed for them — placed, leased, its session open —
and hidden; their first send claims it, and whatever is not claimed is reaped.
Every case here drives the browser's routes with a cookie and the box's with
its device token plus the agent assertion, and asserts what a person can see,
what the box can see, and what is left in the database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.objects import chat_spares
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login, make_member
from tests.test_chat_machine_binding_seam import _daemon_headers, _heartbeat, _register
from tests.test_chat_placement_on_readiness import _kill_row

pytestmark = pytest.mark.asyncio


def _browser() -> AsyncClient:
    return app_client(base_url="http://testserver")


async def _box_up(real_session: AsyncSession, org: OrgWithAdmin, pod: str) -> str:
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org, machine_type.provider_type_id, pod)
    await _heartbeat(org, machine_id)
    return machine_id


async def _warm(browser: AsyncClient) -> str:
    response = await browser.post("/api/v1/chats/spare")
    assert response.status_code == 200, response.text
    state: str = response.json()["state"]
    return state


async def _spare_row(owner_id: UUID) -> WorkspaceObject | None:
    async with AsyncSessionLocal() as session:
        standing = await chat_spares.spares_of_owner(session, owner_id=owner_id)
        assert len(standing) <= 1, "one org here, so at most one spare"
        return standing[0] if standing else None


async def _chat_rows(owner_id: UUID) -> list[WorkspaceObject]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.owner_user_id == owner_id, WorkspaceObject.type == "chat"
            )
        )
        return list(rows.scalars().all())


async def _chat_nodes(chat_id: UUID) -> list[FileNode]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(FileNode).where(FileNode.target_object_id == chat_id))
        return list(rows.scalars().all())


async def _decisions(org_id: UUID, *, action: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == "authz.decision")
            .order_by(EventOutbox.id)
        )
        return [row for row in rows.scalars().all() if row.payload.get("action") == action]


async def _box_lists(org: OrgWithAdmin, machine_id: str) -> list[dict[str, Any]]:
    async with _browser() as daemon:
        listed = await daemon.get(
            "/api/v1/chats",
            headers=await _daemon_headers(org, agent_id=machine_id),
        )
    assert listed.status_code == 200, listed.text
    items: list[dict[str, Any]] = listed.json()["items"]
    return items


# ---------------------------------------------------------------------------
# Warming
# ---------------------------------------------------------------------------


async def test_the_page_heartbeat_warms_one_spare_placed_on_the_orgs_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box_up(real_session, org_admin, "pod-spare-1")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        assert await _warm(browser) == "warm", "a second beat keeps the one spare"
    spare = await _spare_row(org_admin.admin_id)
    assert spare is not None
    spec = spare.spec
    assert spec["spare"] is True
    assert spec["machine_id"] == machine_id
    assert spec["model"] is not None, "pinned like a create"
    assert spare.title == ""
    assert len(await _chat_rows(org_admin.admin_id)) == 1, "at most one spare per person"
    assert len(await _chat_nodes(spare.id)) == 1, "its folder exists for the lease"


async def test_no_spare_without_a_live_machine(org_admin: OrgWithAdmin) -> None:
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "none"
    assert await _spare_row(org_admin.admin_id) is None
    assert await _chat_rows(org_admin.admin_id) == []


async def test_the_box_warms_nothing_for_itself(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box_up(real_session, org_admin, "pod-spare-box")
    async with _browser() as daemon:
        response = await daemon.post(
            "/api/v1/chats/spare", headers=await _daemon_headers(org_admin, agent_id=machine_id)
        )
    assert response.status_code == 200 and response.json()["state"] == "none"
    assert await _spare_row(org_admin.admin_id) is None


# ---------------------------------------------------------------------------
# Invisibility
# ---------------------------------------------------------------------------


async def test_a_spare_is_invisible_to_people_and_to_its_box_until_it_is_claimed(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box_up(real_session, org_admin, "pod-spare-2")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        listed = await browser.get("/api/v1/chats")
        assert [item["id"] for item in listed.json()["items"]] == []
        assert (await browser.get(f"/api/v1/chats/{spare.id}")).status_code == 404
        drive = await browser.get("/api/v1/files/drives")
        assert drive.status_code == 200, drive.text
        drive_id = drive.json()["id"]
        node = (await _chat_nodes(spare.id))[0]
        parent = await browser.get(
            f"/api/v1/files/drives/{drive_id}/items/{node.parent_id}/children"
        )
        assert parent.status_code == 200, parent.text
        assert [row["id"] for row in parent.json()["value"]] == [], "not in ~/Chats"
    # Nobody has written in it, so no agent starts for it: its box does not
    # list it either. The owner's first send claims it and the box takes it then.
    assert await _box_lists(org_admin, machine_id) == []


async def test_the_spares_folder_reads_by_id_for_its_owner_and_404s_for_anyone_else(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Hiding the folder from listings must not cost the read that mounts it.

    The box holds the spare's lease and opens the folder by id; hiding it
    inside the shared title fold left a single-item read with no row to
    return and the route answered 500 to the machine."""
    await _box_up(real_session, org_admin, "pod-spare-read")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        node = (await _chat_nodes(spare.id))[0]
        drive_id = (await browser.get("/api/v1/files/drives")).json()["id"]
        read = await browser.get(f"/api/v1/files/drives/{drive_id}/items/{node.id}")
        assert read.status_code == 200, read.text
        assert read.json()["id"] == str(node.id)
        listed = await browser.get(
            f"/api/v1/files/drives/{drive_id}/items/{node.parent_id}/children"
        )
        assert [row["id"] for row in listed.json()["value"]] == [], "and still no listing"

    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    async with _browser() as colleague:
        await login(colleague, member.email, password)
        refused = await colleague.get(f"/api/v1/files/drives/{drive_id}/items/{node.id}")
        assert refused.status_code == 404, refused.text


async def test_a_reaped_spares_folder_is_hidden_while_its_lease_lets_go(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The row goes first and the folder once the box has handed the lease
    back; in between, the folder must not surface in Chats under the name
    the spare's logical id gave it."""
    await _box_up(real_session, org_admin, "pod-spare-orphan")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        node = (await _chat_nodes(spare.id))[0]
        async with AsyncSessionLocal() as session:
            row = await chat_spares.find_spare(
                session, owner_id=org_admin.admin_id, org_id=org_admin.org_id
            )
            assert row is not None
            await chat_spares.erase_row(session, row)
            await session.commit()
        assert len(await _chat_nodes(spare.id)) == 1, "the folder is still there"
        drive_id = (await browser.get("/api/v1/files/drives")).json()["id"]
        parent = await browser.get(
            f"/api/v1/files/drives/{drive_id}/items/{node.parent_id}/children"
        )
        assert parent.status_code == 200, parent.text
        assert [row["id"] for row in parent.json()["value"]] == []
    async with AsyncSessionLocal() as session:
        assert await chat_spares.purge_orphaned_nodes(session) >= 1
        await session.commit()
    assert await _chat_nodes(spare.id) == []


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_the_first_send_claims_the_spare_and_it_becomes_the_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-3")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        created = await browser.post("/api/v1/chats", json={"claim_spare": True})
        assert created.status_code == 201, created.text
        assert created.json()["id"] == str(spare.id), "the spare is the chat"
        assert await _spare_row(org_admin.admin_id) is None
        sent = await browser.post(
            f"/api/v1/chats/{spare.id}/messages", json={"text": "hello there", "client_id": "m1"}
        )
        assert sent.status_code in (201, 202), sent.text
        listed = await browser.get("/api/v1/chats")
        rows = listed.json()["items"]
        assert [row["id"] for row in rows] == [str(spare.id)]
        assert rows[0]["title"], "named by its first prompt, as today"
        assert (await browser.get(f"/api/v1/chats/{spare.id}")).status_code == 200
    claims = await _decisions(org_admin.org_id, action="claim")
    assert [(row.payload["effect"], row.payload["reason"]) for row in claims] == [
        ("allow", "owner_claims")
    ]
    assert claims[0].payload["resource"]["id"] == str(spare.id)


async def test_a_claim_with_no_spare_creates_as_today(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-4")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"claim_spare": True, "title": "Ops"})
        assert created.status_code == 201, created.text
        assert created.json()["title"] == "Ops"
    rows = await _chat_rows(org_admin.admin_id)
    assert len(rows) == 1 and not rows[0].spec.get("spare")
    assert await _decisions(org_admin.org_id, action="claim") == []


async def test_a_claim_re_pins_the_composers_picks(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-5")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        warmed_effort = spare.spec["model"]["effort"]
        efforts = [e for e in spare.spec["model"]["efforts"] if e != warmed_effort]
        assert efforts, "the catalog model offers another effort to pick"
        assert spare.spec["permission_mode"] != "plan"
        created = await browser.post(
            "/api/v1/chats",
            json={"claim_spare": True, "effort": efforts[0], "permission_mode": "plan"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["id"] == str(spare.id)
        assert body["model"]["effort"] == efforts[0]
        assert body["permission_mode"] == "plan"
    async with AsyncSessionLocal() as session:
        relays = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_admin.org_id,
                EventOutbox.type == EventType.DOC_OP.value,
            )
        )
        relayed = json.dumps(
            [
                row.payload
                for row in relays.scalars().all()
                if str(spare.id) in json.dumps(row.payload)
            ]
        )
    # The box already running the session hears both picks before the prompt.
    assert '"plan"' in relayed and f'"{efforts[0]}"' in relayed, relayed


async def test_a_spare_on_a_dead_machine_is_skipped_and_reaped(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box_up(real_session, org_admin, "pod-spare-6")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        await _kill_row(machine_id)
        created = await browser.post("/api/v1/chats", json={"claim_spare": True})
        assert created.status_code == 201, created.text
        assert created.json()["id"] != str(spare.id), "a dead box's spare is not handed out"
    rows = await _chat_rows(org_admin.admin_id)
    assert [row.id for row in rows] == [UUID(created.json()["id"])], "the spare's row is gone"
    assert await _chat_nodes(spare.id) == [], "and its folder, not into the trash"


async def test_two_concurrent_claims_take_one_spare_and_one_create(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-7")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        first, second = await asyncio.gather(
            browser.post("/api/v1/chats", json={"claim_spare": True}),
            browser.post("/api/v1/chats", json={"claim_spare": True}),
        )
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    ids = {first.json()["id"], second.json()["id"]}
    assert str(spare.id) in ids and len(ids) == 2
    rows = await _chat_rows(org_admin.admin_id)
    assert len(rows) == 2 and not any(row.spec.get("spare") for row in rows)


async def test_a_chat_started_from_a_context_never_claims(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A copy, a Slack mention and a context-started chat are placed like
    composer chats but never take the spare: the flag is ignored beside a
    source, and the spare stands for the composer."""
    await _box_up(real_session, org_admin, "pod-spare-8")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        created = await browser.post("/api/v1/chats", json={"title": "plain"})
        assert created.status_code == 201 and created.json()["id"] != str(spare.id)
    assert await _spare_row(org_admin.admin_id) is not None


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------


async def test_logout_reaps_the_spare(real_session: AsyncSession, org_admin: OrgWithAdmin) -> None:
    await _box_up(real_session, org_admin, "pod-spare-9")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        out = await browser.post("/api/v1/auth/logout")
        assert out.status_code == 200, out.text
    assert await _chat_rows(org_admin.admin_id) == []
    assert await _chat_nodes(spare.id) == []


async def test_the_sweep_reaps_an_idle_spare_and_keeps_a_beating_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-10")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
    spare = await _spare_row(org_admin.admin_id)
    assert spare is not None
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        # The sweep is global: another test's stale spare may be reaped here too,
        # so the count is not pinned -- only that THIS fresh beat survives it.
        await chat_spares.reap_stale(session, now=now)
        await session.commit()
    assert await _spare_row(org_admin.admin_id) is not None, "a fresh beat keeps it"
    async with AsyncSessionLocal() as session:
        reaped = await chat_spares.reap_stale(
            session, now=now + chat_spares.IDLE_CUTOFF + timedelta(seconds=1)
        )
        await session.commit()
    assert reaped >= 1, "this spare, and any another test left standing"
    assert await _chat_rows(org_admin.admin_id) == []
    assert await _chat_nodes(spare.id) == [], "no folder, and nothing in the trash"


async def test_a_spare_outlives_no_maximum_age_however_often_it_beats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await _box_up(real_session, org_admin, "pod-spare-11")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
    spare = await _spare_row(org_admin.admin_id)
    assert spare is not None
    late = datetime.now(UTC) + chat_spares.MAX_AGE + timedelta(seconds=1)
    async with AsyncSessionLocal() as session:
        row = await chat_spares.find_spare(
            session, owner_id=org_admin.admin_id, org_id=org_admin.org_id
        )
        assert row is not None
        await chat_spares.stamp_active(session, row, now=late)
        await session.commit()
    async with AsyncSessionLocal() as session:
        assert await chat_spares.reap_stale(session, now=late) >= 1
        await session.commit()
    assert await _chat_rows(org_admin.admin_id) == []


async def test_a_reaped_spares_folder_is_swept_even_with_no_creator_recorded(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """``file_nodes.created_by`` is nullable — a folder that arrived by a copy,
    or by a path that stamped no actor, records nobody. The orphan pass reads
    that column as the principal it purges under, and one pass sweeps every
    org, so a single creator-less folder must not end the pass and strand every
    other orphan behind it."""
    await _box_up(real_session, org_admin, "pod-spare-no-creator")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        node = (await _chat_nodes(spare.id))[0]
        async with AsyncSessionLocal() as session:
            row = await chat_spares.find_spare(
                session, owner_id=org_admin.admin_id, org_id=org_admin.org_id
            )
            assert row is not None
            await chat_spares.erase_row(session, row)
            await session.execute(
                text("UPDATE file_nodes SET created_by = NULL WHERE id = :id"), {"id": node.id}
            )
            await session.commit()

    async with AsyncSessionLocal() as session:
        orphans = await chat_spares.orphaned_spare_nodes(session)
        assert (node.org_team_id, node.id, node.id) in orphans, (
            "the folder stands in for the creator it does not record"
        )
        assert await chat_spares.purge_orphaned_nodes(session) >= 1
        await session.commit()
    assert await _chat_nodes(spare.id) == [], "a creator-less folder is purged, not skipped"


async def _standing_spares(owner_id: UUID) -> list[UUID]:
    """Every spare the owner holds — the count the unique index promises is 1,
    read without `find_spare`, which raises rather than reports when it is 2."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(WorkspaceObject.id).where(
                WorkspaceObject.owner_user_id == owner_id,
                WorkspaceObject.type == "chat",
                WorkspaceObject.deleted_at == 0,
                WorkspaceObject.spec["spare"].as_boolean().is_(True),
            )
        )
        return list(rows.scalars().all())


async def test_two_warms_racing_for_one_owner_both_answer_and_leave_one_spare(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two tabs, or a page that mounts the warmer twice, warm at the same
    moment. Both read no spare, both warm — and "at most one spare per owner"
    is a partial unique index, so the loser used to take a unique violation
    out to the page as a 500 on a call the page cannot act on. Both must get
    the ordinary answer, and the owner must still hold exactly one spare."""
    await _box_up(real_session, org_admin, "pod-spare-race")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        both = await asyncio.gather(
            browser.post("/api/v1/chats/spare"),
            browser.post("/api/v1/chats/spare"),
        )
    assert [response.status_code for response in both] == [200, 200], [r.text for r in both]
    assert {response.json()["state"] for response in both} == {"warm"}
    assert len(await _standing_spares(org_admin.admin_id)) == 1, (
        "one owner, one spare, however many tabs raced"
    )


async def test_a_slow_catalog_does_not_hold_the_owner_lock_against_another_tab(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-owner lock serializes the read-and-insert, nothing wider.

    A warm asks the gateway which model a new chat opens on. Holding the
    owner's lock across that call makes one slow outbound request the wait
    every other tab's warm takes — and a gateway slower than ``lock_timeout``
    turns into a 503 on the page, the same unanswerable error the lock was
    added to remove. With the first caller parked inside the catalog, the
    second must still get all the way to its own catalog call.
    """
    await _box_up(real_session, org_admin, "pod-spare-catalog")
    from backend.services.chats import catalog as chat_catalog

    real_fetch = chat_catalog.fetch_catalog
    arrived: list[int] = []
    both_in = asyncio.Event()
    let_go = asyncio.Event()

    async def parked(*args: Any, **kwargs: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both_in.set()
        await let_go.wait()
        return await real_fetch(*args, **kwargs)

    monkeypatch.setattr(chat_catalog, "fetch_catalog", parked)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        racing = asyncio.gather(
            browser.post("/api/v1/chats/spare"),
            browser.post("/api/v1/chats/spare"),
        )
        try:
            # Ten seconds is only the failure bound: the second caller either
            # arrives at once or is stuck behind the first one's lock, where it
            # dies on `lock_timeout` instead and never arrives at all.
            await asyncio.wait_for(both_in.wait(), timeout=10)
        except TimeoutError:  # pragma: no cover - the assertion below reports it
            pass
        finally:
            let_go.set()
        both = await racing
    assert len(arrived) == 2, "the second warm never reached the catalog the first one parked in"
    assert [response.status_code for response in both] == [200, 200], [r.text for r in both]
    assert len(await _standing_spares(org_admin.admin_id)) == 1


async def test_a_warm_that_meets_a_spare_already_standing_answers_the_same_way(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The serialized case the race resolves into: the second caller finds the
    first one's spare and stamps it rather than warming a second."""
    await _box_up(real_session, org_admin, "pod-spare-standing")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        first = await _standing_spares(org_admin.admin_id)
        assert await _warm(browser) == "warm"
    assert await _standing_spares(org_admin.admin_id) == first, "the same spare, not a second"


async def test_one_unpurgeable_orphan_does_not_strand_the_rest(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan pass sweeps every org, so no single folder may end it.

    A folder whose rows are already half gone raises something the library
    never declared — a bare ``NoResultFound`` out of the ORM rather than a
    ``FilesError`` — and a pass that lets that escape strands every orphan
    behind it, in every other org, on every subsequent run.
    """
    from alkera_core.files.trash import Trash
    from sqlalchemy.exc import NoResultFound

    await _box_up(real_session, org_admin, "pod-spare-resilient")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        assert await _warm(browser) == "warm"
        spare = await _spare_row(org_admin.admin_id)
        assert spare is not None
        healthy = (await _chat_nodes(spare.id))[0]
        async with AsyncSessionLocal() as session:
            row = await chat_spares.find_spare(
                session, owner_id=org_admin.admin_id, org_id=org_admin.org_id
            )
            assert row is not None
            await chat_spares.erase_row(session, row)
            await session.commit()

    # A foreign orphan, first in the pass, whose purge blows up in a way no
    # `except FilesError` sees.
    broken = UUID(int=0xB0BB1E)
    real_orphans = chat_spares.orphaned_spare_nodes

    async def _with_broken_first(db: AsyncSession) -> list[tuple[UUID, UUID, UUID]]:
        found = await real_orphans(db)
        return [(healthy.org_team_id, broken, healthy.org_team_id), *found]

    real_purge = Trash.purge

    async def _purge(self: Trash, node_id: Any, **kwargs: Any) -> None:
        if UUID(str(node_id)) == broken:
            raise NoResultFound("No row was found when one was required")
        await real_purge(self, node_id, **kwargs)

    monkeypatch.setattr(chat_spares, "orphaned_spare_nodes", _with_broken_first)
    monkeypatch.setattr(Trash, "purge", _purge)

    async with AsyncSessionLocal() as session:
        purged = await chat_spares.purge_orphaned_nodes(session)
        await session.commit()
    assert purged >= 1, "the healthy orphan is purged behind the broken one"
    assert await _chat_nodes(spare.id) == []


async def test_warming_into_a_main_workspace_whose_folder_is_trashed_remakes_the_folder(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several chats per workspace a spare is filed in its owner's main
    workspace. A main workspace cannot be deleted, so a trashed folder is made
    again on the next chat: the beat warms a spare in a fresh folder instead of
    answering 500 or leaving the person with no spare for good."""
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    await _box_up(real_session, org_admin, "pod-spare-trashed")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        main = (await browser.get("/api/v1/workspaces/main")).json()
        trashed = UUID(main["files_node_id"])
        async with AsyncSessionLocal() as session:
            await session.execute(text("SET LOCAL row_security = off"))
            await session.execute(
                text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
                {"id": trashed},
            )
            await session.commit()

        assert await _warm(browser) == "warm"
        again = (await browser.get("/api/v1/workspaces/main")).json()
    assert again["id"] == main["id"]
    assert UUID(again["files_node_id"]) != trashed
    assert await _spare_row(org_admin.admin_id) is not None


async def test_a_spare_whose_folder_the_drive_refuses_is_not_warmed_and_never_a_500(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The person holds the folder new chats are filed in (a mount from their
    laptop), so the drive refuses the spare's folder. The beat answers that
    nothing is warm; reading the refusal after the rollback used to load the
    person's row outside the async greenlet and answer 500, once a minute."""
    import uuid as uuid_module

    from tests.files._files_kit import node_etag

    await _box_up(real_session, org_admin, "pod-spare-refused")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        made = await browser.post("/api/v1/chats", json={"title": "First"})
        assert made.status_code == 201, made.text
        folder = await real_session.get(FileNode, UUID(made.json()["files_node_id"]))
        assert folder is not None and folder.parent_id is not None
        parent, drive = folder.parent_id, folder.drive_id
        await real_session.commit()
        held = await browser.post(
            f"/api/v1/files/drives/{drive}/items/{parent}/lease",
            json={"instanceId": "laptop", "machineId": "laptop", "purpose": "mount"},
            headers={
                "Idempotency-Key": uuid_module.uuid4().hex,
                "If-Match": await node_etag(real_session, parent),
            },
        )
        assert held.status_code == 200, held.text

        for _ in range(2):
            response = await browser.post("/api/v1/chats/spare")
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "none"
    assert await _spare_row(org_admin.admin_id) is None
