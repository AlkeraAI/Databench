"""Workspace objects through the real routes: saving, editing under a version
guard, reading rows, exporting, and the one upload that fills a promoted
result.

Three of these deserve a reviewer's attention in particular. The version guard
pins that ``0`` is NOT "no precondition" — the failure mode it prevents is a
client that never read the row silently overwriting one that did. The payload
upload pins that a browser session, however privileged, cannot deliver customer
row data into our store: only an agent principal acting for a user can, and
only while the result is still waiting for it. And the retired kinds pin that a
client which still saves a query or a report is told the kind is gone, by name
and before its body is read as one — not handed a validation error about a
literal the shape no longer spells.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import EventOutbox, ObjectPayloadRow, TeamRole, WorkspaceObject
from alkera_core.models.workspace_object import OBJECT_TYPES, RETIRED_OBJECT_TYPES
from alkera_core.schemas.objects import ResultBlobEnvelope, ResultSpec
from backend.api.routes.objects import objects as objects_routes
from backend.services.objects import object_service, result_store
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, app_client, login, make_member, mint_cli_token

pytestmark = pytest.mark.asyncio


#: What this route still mints: a result a machine has yet to fill. Declared
#: columns keep it a shape a reader could render, which is what the generic
#: create / edit / audience cases below need an object for.
RESULT_SPEC: dict[str, Any] = {"columns": [{"name": "day", "label": "Day"}, {"name": "n"}]}


async def _other_org_client(client: AsyncClient) -> None:
    from datetime import UTC, datetime

    async with AsyncSessionLocal() as session:
        _org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        admin.email_verified_at = datetime.now(UTC)
        email = admin.email
        await session.commit()
    await login(client, email, "other-pass-12345")


async def _events(org_id: UUID, type_: EventType) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == type_.value)
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _create(client: AsyncClient, **body: Any) -> dict[str, Any]:
    payload = {
        "type": "result",
        "title": "Daily prompts",
        "spec": RESULT_SPEC,
        "client_id": f"o-{uuid4().hex[:8]}",
        **body,
    }
    response = await client.post("/api/v1/objects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _promoted_result(client: AsyncClient, chat_id: str, event_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/chats/{chat_id}/promote", json={"event_id": event_id, "title": "Rows"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _chat(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Ops"})
    assert response.status_code == 201
    return response.json()


def _envelope(count: int) -> dict[str, Any]:
    return ResultBlobEnvelope(
        kind="rows",
        columns=["day", "n"],
        rows=[[f"2026-09-{index + 1:02d}", index] for index in range(count)],
        total=count,
    ).model_dump(mode="json")


def _receipt() -> dict[str, Any]:
    return {
        "sql": "SELECT day, n FROM prompts",
        "connection_name": "Tideline Postgres",
        "role": "analytics_readonly",
        "engine": "postgres",
        "executed_at": "2026-09-07T12:00:00+00:00",
        "row_count": 2,
        "duration_ms": 41,
        "params": {"customer": "acme"},
        "definitions_referenced": ["daily_orders"],
    }


def _receipt_the_machine_learned_nothing_from() -> dict[str, Any]:
    """What ``alkera_cli.cloud.receipt.ResultReceipt`` dumps when the tool
    output named no role and no start time was recorded — nulls where the
    server once demanded a string and an int, plus the daemon's own
    ``principal`` / ``event_id`` fields, which are extras here.

    The backend may not import ``alkera_cli`` (CLAUDE.md's layering rule), so
    this is a transcription; what keeps it honest is the producer-driven parity
    test, ``packages/api-core/tests/schemas/objects/test_receipt_seam.py``.
    """
    return {
        "schema_version": "1.0.0",
        "sql": "SELECT day, n FROM prompts",
        "connection_id": None,
        "connection_name": "Tideline Postgres",
        "role": None,
        "engine": "postgres",
        "principal": {
            "schema_version": "1.0.0",
            "user_id": "",
            "agent_id": "",
            "chain": [],
            "metadata": {},
        },
        "executed_at": None,
        "row_count": 2,
        "duration_ms": None,
        "params": {"customer": "acme"},
        "event_id": "ev-unknown",
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Create, read, list
# ---------------------------------------------------------------------------


async def test_a_saved_result_is_created_and_announced(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = await _create(client)
    assert body["type"] == "result"
    assert body["version"] == 1
    assert body["status"] == "pending_upload"
    assert [column["name"] for column in body["spec"]["columns"]] == ["day", "n"]
    announced = await _events(org_admin.org_id, EventType.WORKSPACE_OBJECT_CHANGED)
    assert announced[-1].entity_id == body["id"]
    assert announced[-1].payload == {"type": "result", "version": 1}


# ---------------------------------------------------------------------------
# The kinds a chat template replaced: gone by name, not by a shape that
# happens no longer to spell them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", list(RETIRED_OBJECT_TYPES))
async def test_saving_a_kind_that_was_retired_says_so_and_saves_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, retired: str
) -> None:
    """A client that has not caught up still posts one. It is told the kind is
    gone — with the kind named, so the message is actionable — and no row is
    left behind for a reader to find."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    before = len((await client.get("/api/v1/objects")).json()["items"])
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": retired,
            "title": "Daily prompts",
            "spec": {"sql_template": "SELECT 1", "engine": "postgres"},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 410, response.text
    error = response.json()["error"]
    assert error["code"] == "object_type_retired"
    assert error["details"]["type"] == retired
    assert retired in error["message"]
    assert len((await client.get("/api/v1/objects")).json()["items"]) == before


@pytest.mark.parametrize("retired", list(RETIRED_OBJECT_TYPES))
async def test_a_retired_kind_is_refused_before_its_body_is_read_as_a_shape(
    client: AsyncClient, org_admin: OrgWithAdmin, retired: str
) -> None:
    """The answer has to be the retirement, not a validation error listing the
    literals the create still spells: a body that is wrong in every other way
    too still comes back 410, which is only true while the refusal is decided
    ahead of the shape."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={"type": retired, "title": "", "spec": "not an object"},
    )
    assert response.status_code == 410, response.text
    assert response.json()["error"]["code"] == "object_type_retired"


async def test_an_unknown_kind_is_still_a_shape_refusal_rather_than_a_retirement(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Only the kinds that were actually retired answer 410; a kind that never
    existed is the vocabulary refusing it, so the two failures stay tellable
    apart by whoever reads the response."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "spreadsheet",
            "title": "Daily prompts",
            "spec": RESULT_SPEC,
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("retired", list(RETIRED_OBJECT_TYPES))
async def test_a_retired_kind_named_by_a_stranger_is_still_unauthenticated(
    client: AsyncClient, retired: str
) -> None:
    """The retirement is not a reason to answer a caller who never signed in:
    who may talk to this route is decided before what they asked for is."""
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": retired,
            "title": "Daily prompts",
            "spec": {},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 401, response.text


async def test_the_re_run_a_saved_query_existed_for_is_gone(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Nothing in this workspace re-runs an object any more, so the route that
    relayed a run onto a chat's machine is not merely refused — it is not
    routed at all."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    response = await client.post(
        f"/api/v1/objects/{obj['id']}/rerun", json={"params": {"customer": "acme"}}
    )
    assert response.status_code == 404, response.text
    assert not [
        route
        for route in objects_routes.router.routes
        if getattr(route, "path", "").endswith("/rerun")
    ]


async def test_a_tombstoned_object_is_not_editable_either(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A deleted row answers the same opaque not-found to a write as to a read
    — the tombstone is not a row a caller can talk their way back into."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(obj["id"]))
        assert row is not None
        row.deleted_at = 1_700_000_000.0
        await session.commit()
    response = await client.put(
        f"/api/v1/objects/{obj['id']}",
        json={"title": "Renamed", "expected_version": 1},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Not found"


async def test_creating_twice_with_one_client_id_returns_the_same_object(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"o-{uuid4().hex[:8]}"
    first = await _create(client, client_id=client_id)
    second = await _create(client, client_id=client_id, title="Renamed")
    assert first["id"] == second["id"]
    assert second["title"] == "Daily prompts"


async def test_the_list_filters_by_type(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _create(client)
    chat = await _chat(client)
    listed = await client.get("/api/v1/objects", params={"type": "result"})
    ids = {item["id"] for item in listed.json()["items"]}
    assert result["id"] in ids
    assert chat["id"] not in ids


async def test_an_object_of_another_org_is_an_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    async with app_client() as intruder:
        await _other_org_client(intruder)
        foreign = await intruder.get(f"/api/v1/objects/{obj['id']}")
        absent = await intruder.get(f"/api/v1/objects/{uuid4()}")
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json()["error"]["message"] == absent.json()["error"]["message"] == "Not found"


async def test_a_cross_org_read_leaves_a_deny_row_naming_the_probed_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The tenancy floor is the engine's, so a probe of another tenant's id is
    on record — which is the whole reason the object is not filtered out in
    SQL before a decision is made."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    before = len(await _events(org_admin.org_id, EventType.AUTHZ_DECISION))
    async with app_client() as intruder:
        await _other_org_client(intruder)
        assert (await intruder.get(f"/api/v1/objects/{obj['id']}")).status_code == 404
    rows = await _events(org_admin.org_id, EventType.AUTHZ_DECISION)
    assert len(rows) == before + 1
    denial = rows[-1]
    assert denial.entity == "workspace_object"
    assert denial.entity_id == obj["id"]
    assert denial.payload["effect"] == "deny"
    assert denial.payload["reason"] == "cross_org"
    assert denial.visibility == "platform", "a decision row is never on a tenant stream"


# ---------------------------------------------------------------------------
# The version guard
# ---------------------------------------------------------------------------


async def test_an_edit_naming_the_version_it_read_is_accepted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    response = await client.put(
        f"/api/v1/objects/{obj['id']}", json={"title": "Renamed", "expected_version": 1}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["title"] == "Renamed"


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(0, id="zero_is_not_no_precondition"),
        pytest.param(2, id="a_version_from_the_future"),
        pytest.param(-1, id="a_negative_version"),
    ],
)
async def test_an_edit_naming_the_wrong_version_is_a_conflict(
    client: AsyncClient, org_admin: OrgWithAdmin, expected: int
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    response = await client.put(
        f"/api/v1/objects/{obj['id']}", json={"title": "Renamed", "expected_version": expected}
    )
    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "version_conflict"
    assert body["details"]["current_version"] == 1
    unchanged = await client.get(f"/api/v1/objects/{obj['id']}")
    assert unchanged.json()["title"] == "Daily prompts"
    assert unchanged.json()["version"] == 1


async def test_expected_version_is_required(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """A write that does not say which row it read is a write that did not
    read one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    response = await client.put(f"/api/v1/objects/{obj['id']}", json={"title": "Renamed"})
    assert response.status_code == 422


async def test_a_teammate_may_read_but_not_edit(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # Addressed to the org on purpose: the subject is the owner-only write on
    # a row a colleague may read, not the default audience.
    obj = await _create(client, visibility_scope="org")
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        readable = await other.get(f"/api/v1/objects/{obj['id']}")
        writable = await other.put(
            f"/api/v1/objects/{obj['id']}", json={"title": "Mine now", "expected_version": 1}
        )
    assert readable.status_code == 200
    assert writable.status_code == 403
    denial = (await _events(org_admin.org_id, EventType.AUTHZ_DECISION))[-1]
    assert denial.entity_id == obj["id"]
    assert (denial.payload["effect"], denial.payload["reason"]) == (
        "deny",
        "owner_or_org_admin_required",
    )


async def test_a_spec_outside_its_schema_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    response = await login(client, org_admin.admin_email, org_admin.admin_password)
    assert response is client
    bad = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Bad",
            "spec": {"columns": [{"name": 7}]},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# The payload upload — the one place customer rows cross into our store
# ---------------------------------------------------------------------------


async def _agent_client(
    client: AsyncClient, org: OrgWithAdmin, session_id: str = "sess-01J7Q3M8"
) -> dict[str, str]:
    token = await mint_cli_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
    )
    return {"Authorization": f"Bearer {token}", **agent_headers(session_id)}


async def test_the_receipt_exactly_as_the_daemon_builds_it_is_accepted_whole(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The daemon's receipt carries a few things this schema does not name —
    its ``principal`` block, the transcript ``event_id``, its own
    ``schema_version`` — and an empty role for a connection with no credential.
    None of that may refuse the upload, and the five inline facts land."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-1")
    headers = await _agent_client(client, org_admin)
    daemon_receipt = {
        "schema_version": "1.0.0",
        "sql": "select id from orders where customer = %(customer)s",
        "connection_id": "rec-42",
        "connection_name": "pg-main",
        "role": "",
        "engine": "duckdb",
        "principal": {
            "schema_version": "1.0.0",
            "user_id": str(org_admin.admin_id),
            "agent_id": chat["id"],
            "chain": [str(org_admin.admin_id), chat["id"]],
        },
        "executed_at": "2026-09-07T12:00:04.600000Z",
        "row_count": 120,
        "duration_ms": 0,
        "params": {"customer": "acme"},
        "event_id": "ev-1",
    }
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": daemon_receipt},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["spec"]["receipt"]
    assert receipt["connection_id"] == "rec-42"
    assert receipt["connection_name"] == "pg-main"
    assert receipt["role"] == ""
    assert receipt["engine"] == "duckdb"
    assert receipt["executed_at"] == "2026-09-07T12:00:04.600000Z"
    assert receipt["row_count"] == 120
    assert receipt["duration_ms"] == 0
    assert receipt["principal_chain"], "the promoter recorded at create time survives"


async def test_the_machine_fills_a_promoted_result_and_it_becomes_ready(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-1")
    headers = await _agent_client(client, org_admin)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["version"] == 2
    # The receipt renders inline, so every fact the daemon sent is pinned whole.
    receipt = body["spec"]["receipt"]
    assert receipt["sql"] == "SELECT day, n FROM prompts"
    assert receipt["role"] == "analytics_readonly"
    assert receipt["connection_name"] == "Tideline Postgres"
    assert receipt["engine"] == "postgres"
    assert receipt["executed_at"] == "2026-09-07T12:00:00+00:00"
    assert receipt["row_count"] == 2
    assert receipt["duration_ms"] == 41
    assert receipt["params"] == {"customer": "acme"}
    assert body["spec"]["payload"]["sha256"], "the handle is derived server-side"
    assert body["spec"]["total_rows"] == 2

    rows = await client.get(f"/api/v1/objects/{result['id']}/rows")
    assert rows.status_code == 200
    assert rows.json() == {
        "columns": ["day", "n"],
        "keys": ["day", "n"],
        "rows": [["2026-09-01", 0], ["2026-09-02", 1]],
        "total": 2,
    }


async def test_a_receipt_that_learned_no_role_or_duration_still_fills_the_result(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The seam that stranded every promotion: the machine sends null where it
    learned nothing, and a reader that demanded a value 422'd the upload — so
    the object waited for a payload that could never be accepted. It is
    accepted, the result goes ready, and the unknown fields read back as null
    rather than as an invented ``""``."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-unknown")
    headers = await _agent_client(client, org_admin)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt_the_machine_learned_nothing_from()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    receipt = body["spec"]["receipt"]
    assert receipt["role"] is None
    assert receipt["duration_ms"] is None
    assert receipt["executed_at"] is None
    assert receipt["connection_name"] == "Tideline Postgres"
    assert receipt["principal_chain"]["promoted_by"], "the promoter is still on the chain"
    rows = await client.get(f"/api/v1/objects/{result['id']}/rows")
    assert rows.json()["total"] == 2, "the rows the receipt describes are there"


# ---------------------------------------------------------------------------
# The payload that is NOT coming
#
# A promote the machine cannot honour used to leave the object waiting for ever:
# its page read "Saving…", the list read "0 rows", and the reason was written
# only into the chat the reader had already navigated away from.
# ---------------------------------------------------------------------------


async def test_the_machine_can_say_the_payload_is_not_coming_and_why(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-fail")
    headers = await _agent_client(client, org_admin)
    reason = "no tool result with event id prt_07ad57d1c0016yL2P6MozuMrVf"

    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload/failed",
        json={"reason": reason},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["spec"]["failure_reason"] == reason
    # The promoter recorded at create time is not lost by the failure.
    assert body["spec"]["receipt"]["principal_chain"]["promoted_by"]
    fresh = await client.get(f"/api/v1/objects/{result['id']}")
    assert fresh.json()["spec"]["failure_reason"] == reason


async def test_a_browser_session_can_never_fail_a_payload(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Saying the rows are not coming is the machine's word, exactly as
    delivering them is: the result's own owner cannot mark it failed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-fail-2")

    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload/failed", json={"reason": "nope"}
    )

    assert response.status_code == 403
    fresh = await client.get(f"/api/v1/objects/{result['id']}")
    assert fresh.json()["status"] == "pending_upload"


async def test_a_delivered_result_is_never_retro_failed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-fail-3")
    headers = await _agent_client(client, org_admin)
    delivered = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert delivered.status_code == 200

    refused = await client.post(
        f"/api/v1/objects/{result['id']}/payload/failed",
        json={"reason": "too late"},
        headers=headers,
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "payload_already_settled"
    fresh = await client.get(f"/api/v1/objects/{result['id']}")
    assert fresh.json()["status"] == "ready"


async def test_a_failed_result_still_refuses_to_hand_out_rows(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """An empty table would read as a query that came back with nothing."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-fail-4")
    headers = await _agent_client(client, org_admin)
    await client.post(
        f"/api/v1/objects/{result['id']}/payload/failed",
        json={"reason": "the result payload is unavailable"},
        headers=headers,
    )

    rows = await client.get(f"/api/v1/objects/{result['id']}/rows")
    assert rows.status_code in (404, 409)


async def test_a_browser_session_can_never_upload_a_payload(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The owner of the result, in their own session, is still refused: this
    action belongs to the machine alone."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-2")
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(1), "receipt": _receipt()},
    )
    assert response.status_code == 403
    denial = (await _events(org_admin.org_id, EventType.AUTHZ_DECISION))[-1]
    assert denial.entity_id == result["id"]
    assert (denial.payload["effect"], denial.payload["reason"]) == (
        "deny",
        "agent_principal_required",
    )
    fresh = await client.get(f"/api/v1/objects/{result['id']}")
    assert fresh.json()["status"] == "pending_upload"


async def test_a_second_upload_against_a_ready_result_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A receipted answer is not rewritable: the first payload is the one the
    receipt describes."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-3")
    headers = await _agent_client(client, org_admin)
    first = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(9), "receipt": _receipt()},
        headers=headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "payload_already_delivered"
    rows = await client.get(f"/api/v1/objects/{result['id']}/rows")
    assert rows.json()["total"] == 2, "the first payload stands"


async def test_an_upload_for_another_org_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-4")
    async with app_client() as intruder:
        await _other_org_client(intruder)
        me = await intruder.get("/api/v1/auth/me")
        assert me.status_code == 200
        token = await mint_cli_token(
            user_id=UUID(me.json()["id"]),
            email=me.json()["email"],
            org_team_id=UUID(me.json()["org_team_id"]),
        )
        response = await intruder.post(
            f"/api/v1/objects/{result['id']}/payload",
            json={"envelope": _envelope(1), "receipt": _receipt()},
            headers={"Authorization": f"Bearer {token}", **agent_headers("sess-x")},
        )
    assert response.status_code == 404
    async with AsyncSessionLocal() as session:
        stored = await session.get(WorkspaceObject, UUID(result["id"]))
    assert stored is not None and stored.status == "pending_upload"


async def test_a_large_payload_spills_into_rows_and_still_reads_back(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_store, "INLINE_LIMIT_BYTES", 1)
    monkeypatch.setattr(result_store, "ROWS_PER_PAGE", 10)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-5")
    headers = await _agent_client(client, org_admin)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(25), "receipt": _receipt()},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["spec"]["inline"] is None
    async with AsyncSessionLocal() as session:
        pages = await session.execute(
            select(ObjectPayloadRow).where(ObjectPayloadRow.object_id == UUID(result["id"]))
        )
        assert len(list(pages.scalars().all())) == 3
    page = await client.get(
        f"/api/v1/objects/{result['id']}/rows", params={"offset": 8, "limit": 5}
    )
    assert [row[1] for row in page.json()["rows"]] == [8, 9, 10, 11, 12]
    assert page.json()["total"] == 25


async def test_the_row_reader_caps_its_page(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _ready_result(client, org_admin, "ev-6")
    over = await client.get(
        f"/api/v1/objects/{result['id']}/rows", params={"offset": 0, "limit": 1001}
    )
    assert over.status_code == 422
    at_cap = await client.get(
        f"/api/v1/objects/{result['id']}/rows", params={"offset": 0, "limit": 1000}
    )
    assert at_cap.status_code == 200


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def test_the_csv_export_is_an_attachment_with_escaped_cells(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-7")
    headers = await _agent_client(client, org_admin)
    hostile = ResultBlobEnvelope(
        kind="rows",
        columns=["day", "note"],
        rows=[["2026-09-01", '=HYPERLINK("http://evil","click")']],
        total=1,
    ).model_dump(mode="json")
    upload = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": hostile, "receipt": _receipt()},
        headers=headers,
    )
    assert upload.status_code == 200
    response = await client.get(f"/api/v1/objects/{result['id']}/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf"), "the utf-8 byte-order mark leads the file"
    assert response.content.decode("utf-8-sig") == (
        'day,note\r\n2026-09-01,"\'=HYPERLINK(""http://evil"",""click"")"\r\n'
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        pytest.param("Enterprise SOV deliverable", "Enterprise-SOV-deliverable", id="plain_title"),
        pytest.param("Q3 / EMEA — spend", "Q3-EMEA-spend", id="punctuation_becomes_one_dash"),
        pytest.param(
            "Cafe\u0301 na\u00efve r\u00e9sum\u00e9",
            "Cafe-naive-resume",
            id="accents_fold_to_ascii",
        ),
        pytest.param("\u58f2\u4e0a", None, id="a_title_with_no_ascii_falls_back_to_the_id"),
        pytest.param("", None, id="an_untitled_result_falls_back_to_the_id"),
        pytest.param("  ---  ", None, id="a_title_of_only_separators_falls_back_to_the_id"),
        pytest.param("A" * 200, "A" * 80, id="a_long_title_is_capped_at_eighty_characters"),
    ],
)
def test_the_csv_download_is_named_from_the_title(title: str, expected: str | None) -> None:
    obj = WorkspaceObject(
        id=UUID("3f2a9c00-0000-4000-8000-000000000001"),
        title=title,
        created_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
    )
    stem = expected if expected is not None else str(obj.id)
    assert objects_routes._csv_filename(obj) == f"{stem}-2026-09-05.csv"


async def test_the_csv_export_names_the_download_after_the_result(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    promoted = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={"event_id": "ev-name", "title": "Enterprise SOV / deliverable"},
    )
    assert promoted.status_code == 201, promoted.text
    result = promoted.json()
    headers = await _agent_client(client, org_admin)
    upload = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert upload.status_code == 200
    response = await client.get(f"/api/v1/objects/{result['id']}/export.csv")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert result["id"] not in disposition, "the uuid is not what the person sees"
    assert re.fullmatch(
        r'attachment; filename="Enterprise-SOV-deliverable-\d{4}-\d{2}-\d{2}\.csv"', disposition
    ), disposition


async def test_an_export_of_another_orgs_result_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], "ev-8")
    async with app_client() as intruder:
        await _other_org_client(intruder)
        response = await intruder.get(f"/api/v1/objects/{result['id']}/export.csv")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Re-run
# ---------------------------------------------------------------------------


async def test_an_anonymous_caller_is_refused(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/objects")).status_code == 401
    assert (await client.get(f"/api/v1/objects/{uuid4()}/rows")).status_code == 401
    assert (await client.get(f"/api/v1/objects/{uuid4()}/export.csv")).status_code == 401


# ---------------------------------------------------------------------------
# The payload upload: its ceiling, its chain, and its race
# ---------------------------------------------------------------------------


async def _pending_result(client: AsyncClient, event_id: str) -> dict[str, Any]:
    chat = await _chat(client)
    return await _promoted_result(client, chat["id"], event_id)


async def _stored(object_id: str) -> WorkspaceObject:
    async with AsyncSessionLocal() as session:
        obj = await session.get(WorkspaceObject, UUID(object_id))
        assert obj is not None
        return obj


@pytest.mark.parametrize(
    ("cap", "rows", "status"),
    [
        pytest.param(100_000, 2, 200, id="under_the_ceiling_is_accepted"),
        pytest.param(512, 40, 413, id="over_the_ceiling_is_refused"),
    ],
)
async def test_the_upload_has_a_byte_ceiling_read_from_settings(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    rows: int,
    status: int,
) -> None:
    """The ceiling is data, not a constant: an operator sizes it per deployment,
    and an upload past it is a plain 413 that leaves the result untouched."""
    monkeypatch.setattr(settings, "objects_payload_max_bytes", cap)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _pending_result(client, "ev-cap")
    headers = await _agent_client(client, org_admin)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(rows), "receipt": _receipt()},
        headers=headers,
    )
    assert response.status_code == status, response.text
    stored = await _stored(result["id"])
    if status == 413:
        assert isinstance(response.json()["error"]["message"], str)
        assert str(cap) in response.json()["error"]["message"]
        assert stored.status == "pending_upload"
        assert stored.spec.get("inline") is None
        async with AsyncSessionLocal() as session:
            pages = await session.execute(
                select(ObjectPayloadRow).where(ObjectPayloadRow.object_id == UUID(result["id"]))
            )
            assert list(pages.scalars().all()) == []
    else:
        assert stored.status == "ready"


def test_the_default_payload_ceiling_is_sixteen_mebibytes() -> None:
    from alkera_core.config import Settings

    assert Settings.model_fields["objects_payload_max_bytes"].default == 16 * 1024 * 1024


async def test_an_uploaded_receipt_cannot_rewrite_the_server_stamped_chain(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """``promoted_by`` was stamped from the promoter's acting context when the
    result was created; the machine's receipt may add to the chain but never
    replace who promoted, and the server records who uploaded."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _pending_result(client, "ev-chain")
    stamped = result["spec"]["receipt"]["principal_chain"]["promoted_by"]
    assert stamped["acting"]["id"] == str(org_admin.admin_id)
    headers = await _agent_client(client, org_admin, session_id="sess-upload")
    forged = {
        **_receipt(),
        "principal_chain": {
            "promoted_by": {"acting": {"id": "someone-else", "kind": "user"}},
            "uploaded_by": {"acting": {"id": "not-this-machine", "kind": "agent"}},
            "machine": "box-1",
        },
    }
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": forged},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    chain = response.json()["spec"]["receipt"]["principal_chain"]
    assert chain["promoted_by"] == stamped, "the client's promoted_by is ignored"
    assert chain["uploaded_by"]["acting"]["kind"] == "agent"
    assert chain["uploaded_by"]["acting"]["id"] == "sess-upload"
    assert chain["uploaded_by"]["delegating_user"]["id"] == str(org_admin.admin_id)
    assert chain["machine"] == "box-1", "the machine's own facts still join the chain"


async def test_a_racing_second_upload_is_a_conflict_not_a_crash(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two deliveries of one payload race: the second reads the row at version
    1, and by the time it takes the lock the first has bumped it. That is a
    409 carrying the live version, never a 500."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _pending_result(client, "ev-race")
    headers = await _agent_client(client, org_admin)
    real_store = result_store.store_payload

    async def store_then_lose_the_race(*args: Any, **kwargs: Any) -> dict[str, Any]:
        placement = await real_store(*args, **kwargs)
        # The first delivery lands between this request's read and its lock.
        async with AsyncSessionLocal() as other:
            row = await other.get(WorkspaceObject, UUID(result["id"]))
            assert row is not None
            row.version += 1
            await other.commit()
        return placement

    monkeypatch.setattr(result_store, "store_payload", store_then_lose_the_race)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "version_conflict"
    assert body["details"]["current_version"] == 2


@pytest.mark.parametrize(
    ("make_result", "refused_as", "reason"),
    [
        pytest.param(
            lambda client: _create(client, type="result", spec={}, visibility_scope="org"),
            403,
            "owner_or_org_admin_required",
            id="org_visible_result",
        ),
        pytest.param(
            lambda client: _pending_result(client, "ev-not-owner"),
            404,
            "not_in_audience",
            id="promoted_out_of_a_private_chat",
        ),
    ],
)
async def test_an_agent_acting_for_a_member_who_is_not_the_owner_cannot_upload(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    make_result: Any,
    refused_as: int,
    reason: str,
) -> None:
    """The forged-relay shape: a machine presents a real member's JWT plus the
    agent headers, but that member does not own the result. On a result the
    org may read, the agent branch is satisfied and the owner branch refuses,
    on record; a result promoted out of a private chat is private like the
    chat, so the member is outside its audience and told it does not exist
    before the owner branch is ever reached."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await make_result(client)
    assert result["status"] == "pending_upload"
    member, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    token = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    # A fresh client: the session cookie wins over a Bearer when both are sent,
    # so the member's machine must present its credential alone.
    async with app_client() as machine:
        response = await machine.post(
            f"/api/v1/objects/{result['id']}/payload",
            json={"envelope": _envelope(1), "receipt": _receipt()},
            headers={"Authorization": f"Bearer {token}", **agent_headers("sess-forged")},
        )
    assert response.status_code == refused_as, response.text
    denial = (await _events(org_admin.org_id, EventType.AUTHZ_DECISION))[-1]
    assert denial.entity_id == result["id"]
    assert (denial.payload["effect"], denial.payload["reason"]) == ("deny", reason)
    assert (await _stored(result["id"])).status == "pending_upload"


# ---------------------------------------------------------------------------
# Create and edit: idempotency decides, a receipt is immutable, a chart binds
# ---------------------------------------------------------------------------


async def _team_scoped_result(
    org: OrgWithAdmin, *, logical_id: str, spec: dict[str, Any] | None = None
) -> tuple[WorkspaceObject, UUID]:
    """A ready result addressed to one team of ``org``, owned by its admin.
    Returns the object and the team id so a test can put a member on it."""
    from alkera_core.models import User
    from backend.services.objects import object_service

    async with AsyncSessionLocal() as session:
        team = await team_service.create_subteam(
            session, org_team_id=org.org_id, name=f"Ops {secrets.token_hex(3)}"
        )
        admin = await session.get(User, org.admin_id)
        assert admin is not None
        obj, created = await object_service.create_object(
            session,
            owner=admin,
            org_id=admin.home_org_team_id,
            type="result",
            title="Team rows",
            spec=spec
            or {
                **ResultSpec().model_dump(mode="json"),
                "receipt": {**_receipt(), "principal_chain": {"promoted_by": {"who": "admin"}}},
                "inline": _envelope(2),
                "total_rows": 2,
                "envelope_columns": ["day", "n"],
            },
            logical_id=logical_id,
            team_id=team.id,
        )
        assert created
        await session.commit()
        return obj, team.id


async def _member_client(
    client: AsyncClient, org: OrgWithAdmin, real_session: Any, *, team_id: UUID | None = None
) -> AsyncClient:
    from backend.services.org import memberships as membership_service

    member, password = await make_member(
        real_session, org_id=org.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    if team_id is not None:
        await membership_service.add_member(real_session, team_id=team_id, user_id=member.id)
        await real_session.commit()
    other = app_client()
    await login(other, member.email, password)
    return other


async def test_a_retried_create_decides_read_on_the_object_it_would_return(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A colliding client_id used to hand back whatever row held it — another
    team's result, spec and all — under a CREATE decision about the caller's
    own audience. The row that is returned is the one that is decided on, and
    outside the caller's audience it is the policy's opaque not-found."""
    logical_id = f"promote:{uuid4()}:ev-1"
    obj, _team = await _team_scoped_result(org_admin, logical_id=logical_id)
    outsider = await _member_client(client, org_admin, real_session)
    before = len(await _events(org_admin.org_id, EventType.AUTHZ_DECISION))
    response = await outsider.post(
        "/api/v1/objects",
        json={"type": "result", "title": "Mine", "spec": {}, "client_id": logical_id},
    )
    await outsider.aclose()
    assert response.status_code == 404, response.text
    assert "SELECT" not in response.text and "inline" not in response.text
    rows = await _events(org_admin.org_id, EventType.AUTHZ_DECISION)
    assert len(rows) == before + 1
    denial = rows[-1]
    assert denial.entity_id == str(obj.id)
    assert (denial.payload["action"], denial.payload["effect"], denial.payload["reason"]) == (
        "read",
        "deny",
        "not_in_audience",
    )


async def test_a_retried_create_by_a_reader_in_the_audience_returns_the_object(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    logical_id = f"promote:{uuid4()}:ev-2"
    obj, team_id = await _team_scoped_result(org_admin, logical_id=logical_id)
    teammate = await _member_client(client, org_admin, real_session, team_id=team_id)
    response = await teammate.post(
        "/api/v1/objects",
        json={"type": "result", "title": "Mine", "spec": {}, "client_id": logical_id},
    )
    await teammate.aclose()
    assert response.status_code == 201, response.text
    assert response.json()["id"] == str(obj.id)
    allowed = (await _events(org_admin.org_id, EventType.AUTHZ_DECISION))[-1]
    assert (allowed.entity_id, allowed.payload["action"], allowed.payload["effect"]) == (
        str(obj.id),
        "read",
        "allow",
    )


async def test_a_client_id_held_by_an_object_of_another_type_is_a_conflict(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A chat's client_id shares the namespace: a result create naming it must
    not be answered with the chat row dressed as a result."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"c-{uuid4().hex[:8]}"
    chat = await client.post("/api/v1/chats", json={"title": "Ops", "client_id": client_id})
    assert chat.status_code == 201
    response = await client.post(
        "/api/v1/objects",
        json={"type": "result", "title": "R", "spec": RESULT_SPEC, "client_id": client_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "client_id_in_use"


async def test_a_tombstoned_object_is_neither_returned_nor_resurrected(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A deleted row still holds its logical id under the unique constraint;
    a retried create must not hand the tombstone back as if it were live."""
    from backend.services.objects import object_service

    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"o-{uuid4().hex[:8]}"
    obj = await _create(client, client_id=client_id)
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(obj["id"]))
        assert row is not None
        row.deleted_at = 1_700_000_000.0
        await session.commit()
        found = await object_service.find_by_logical_id(
            session,
            org_team_id=org_admin.org_id,
            namespace=row.namespace,
            logical_id=client_id,
        )
    assert found is None, "a tombstone is not a live object"
    response = await client.post(
        "/api/v1/objects",
        json={"type": "result", "title": "Again", "spec": RESULT_SPEC, "client_id": client_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "client_id_retired"
    assert (await client.get(f"/api/v1/objects/{obj['id']}")).status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("receipt", _receipt(), id="receipt"),
        pytest.param("inline", _envelope(1), id="inline_rows"),
        pytest.param("payload", {"sha256": "a" * 64, "size": 1}, id="payload_handle"),
        pytest.param("total_rows", 5, id="total_rows"),
        pytest.param("envelope_columns", ["x"], id="envelope_columns"),
    ],
)
async def test_a_created_result_cannot_carry_what_only_the_machine_writes(
    client: AsyncClient, org_admin: OrgWithAdmin, field: str, value: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Forged",
            "spec": {field: value},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "server_owned_field"
    assert body["details"]["field"] == field


async def test_a_created_result_waits_for_its_machine_and_names_its_promoter(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Without a machine's upload a result has no rows, so it is created
    waiting for one, and the receipt's promoted_by is the caller — stamped
    here, never taken from the body."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Shell",
            "spec": {"columns": [{"name": "day", "label": "Day"}]},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending_upload"
    chain = body["spec"]["receipt"]["principal_chain"]
    assert chain["promoted_by"]["acting"]["id"] == str(org_admin.admin_id)
    assert body["spec"]["columns"][0]["label"] == "Day"


async def _ready_result(client: AsyncClient, org: OrgWithAdmin, event_id: str) -> dict[str, Any]:
    result = await _pending_result(client, event_id)
    headers = await _agent_client(client, org)
    response = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        pytest.param(
            "receipt", {**_receipt(), "role": "superuser"}, "receipt_immutable", id="receipt"
        ),
        pytest.param("inline", _envelope(9), "immutable_field", id="inline_rows"),
        pytest.param("payload", {"sha256": "b" * 64, "size": 9}, "immutable_field", id="handle"),
        pytest.param("total_rows", 9, "immutable_field", id="total_rows"),
        pytest.param("envelope_columns", ["x", "y"], "immutable_field", id="envelope_columns"),
        pytest.param("source_chat_id", str(uuid4()), "immutable_field", id="source_chat_id"),
        pytest.param("source_event_id", "ev-other", "immutable_field", id="source_event_id"),
    ],
)
async def test_an_edit_cannot_change_what_the_receipt_describes(
    client: AsyncClient, org_admin: OrgWithAdmin, field: str, value: Any, code: str
) -> None:
    """A ready result's receipt, payload and provenance are the trust surface:
    an edit that changes any of them is refused and the row is untouched."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, f"ev-imm-{field}")
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={"spec": {**ready["spec"], field: value}, "expected_version": ready["version"]},
    )
    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == code
    if code == "immutable_field":
        assert body["details"]["field"] == field
    fresh = (await client.get(f"/api/v1/objects/{ready['id']}")).json()
    assert fresh["version"] == ready["version"]
    assert fresh["spec"] == ready["spec"]


async def test_an_edit_that_echoes_the_receipt_unchanged_may_rename_a_column(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """GET, edit the labels, PUT the spec back: the receipt travels along
    unchanged and that is not a rewrite. The stored payload placement and the
    receipt survive the edit byte for byte."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, "ev-rename")
    edited = {
        **ready["spec"],
        "columns": [{"name": "day", "label": "Day"}, {"name": "n", "label": "Prompts"}],
    }
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={"spec": edited, "expected_version": ready["version"]},
    )
    assert response.status_code == 200, response.text
    spec = response.json()["spec"]
    assert [column["label"] for column in spec["columns"]] == ["Day", "Prompts"]
    for field in ("receipt", "inline", "payload", "total_rows", "envelope_columns"):
        assert spec[field] == ready["spec"][field], field


async def test_an_edit_may_send_only_the_part_it_changes(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A partial spec — just the columns — is merged over the stored one rather
    than replacing it, so a client cannot drop the payload by omission."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, "ev-partial")
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={
            "spec": {"columns": [{"name": "day", "label": "Day"}, {"name": "n", "label": "N"}]},
            "expected_version": ready["version"],
        },
    )
    assert response.status_code == 200, response.text
    spec = response.json()["spec"]
    assert spec["inline"] == ready["spec"]["inline"]
    assert spec["receipt"] == ready["spec"]["receipt"]
    rows = await client.get(f"/api/v1/objects/{ready['id']}/rows")
    assert rows.json()["total"] == 2


async def test_a_column_edit_must_name_columns_the_payload_has(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, "ev-badcol")
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={
            "spec": {"columns": [{"name": "day"}, {"name": "revenue", "label": "Revenue"}]},
            "expected_version": ready["version"],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "unknown_column"
    assert body["details"]["field"] == "columns[1].name"
    assert "revenue" not in body["message"], "the message names the slot, not the value"


@pytest.mark.parametrize(
    ("channel", "field"),
    [pytest.param("x", "dya", id="x_off_by_a_typo"), pytest.param("y", "revenue", id="y_absent")],
)
async def test_a_chart_must_bind_to_the_columns_by_key(
    client: AsyncClient, org_admin: OrgWithAdmin, channel: str, field: str
) -> None:
    """A chart names column KEYS. A field that is not a key of this result is
    refused with the channel named, so a renamed label can never break it and
    a typo never renders an empty figure."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, f"ev-chart-{channel}")
    encoding = {
        "x": {"field": "day", "type": "temporal"},
        "y": {"field": "n", "type": "quantitative"},
    }
    encoding[channel] = {**encoding[channel], "field": field}
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={
            "spec": {"chart_spec": {"mark": "line", "encoding": encoding}},
            "expected_version": ready["version"],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "chart_field_unbound"
    assert body["details"]["field"] == f"encoding.{channel}.field"
    assert field not in body["message"]


async def test_a_renamed_column_keeps_the_chart_bound_and_the_rows_expose_keys(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The rows page carries the display labels AND the stable keys, in the
    same order, so a renderer binds a chart by key while showing the label."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ready = await _ready_result(client, org_admin, "ev-keys")
    response = await client.put(
        f"/api/v1/objects/{ready['id']}",
        json={
            "spec": {
                "columns": [{"name": "day", "label": "Day"}, {"name": "n", "label": "Prompts"}],
                "chart_spec": {
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "day", "type": "temporal"},
                        "y": {"field": "n", "type": "quantitative"},
                    },
                },
            },
            "expected_version": ready["version"],
        },
    )
    assert response.status_code == 200, response.text
    rows = await client.get(f"/api/v1/objects/{ready['id']}/rows")
    assert rows.status_code == 200
    assert rows.json()["columns"] == ["Day", "Prompts"]
    assert rows.json()["keys"] == ["day", "n"]
    export = await client.get(f"/api/v1/objects/{ready['id']}/export.csv")
    assert export.text.lstrip("﻿").split("\r\n")[0] == "Day,Prompts"


async def test_a_created_result_with_declared_columns_binds_its_chart_to_them(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Shell",
            "spec": {
                "columns": [{"name": "day"}, {"name": "n"}],
                "chart_spec": {
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "day", "type": "temporal"},
                        "y": {"field": "orders", "type": "quantitative"},
                    },
                },
            },
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "chart_field_unbound"


async def test_a_cross_org_edit_is_an_opaque_not_found_on_record(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    obj = await _create(client)
    before = len(await _events(org_admin.org_id, EventType.AUTHZ_DECISION))
    async with app_client() as intruder:
        await _other_org_client(intruder)
        response = await intruder.put(
            f"/api/v1/objects/{obj['id']}", json={"title": "Taken", "expected_version": 1}
        )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Not found"
    rows = await _events(org_admin.org_id, EventType.AUTHZ_DECISION)
    assert len(rows) == before + 1
    assert (rows[-1].payload["action"], rows[-1].payload["reason"]) == ("write", "cross_org")
    assert (await client.get(f"/api/v1/objects/{obj['id']}")).json()["title"] == "Daily prompts"


# ---------------------------------------------------------------------------
# Reading a result that is still saving, and the export's encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [pytest.param("rows", id="rows"), pytest.param("export.csv", id="csv")]
)
async def test_a_result_still_waiting_for_its_payload_says_so(
    client: AsyncClient, org_admin: OrgWithAdmin, path: str
) -> None:
    """Before the machine delivers, there is no document to hand out: an empty
    table or a header-only file would read as "the query returned nothing",
    which is not what happened."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _pending_result(client, f"ev-wait-{path}")
    response = await client.get(f"/api/v1/objects/{result['id']}/{path}")
    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "payload_pending"
    assert isinstance(body["message"], str) and body["message"]


async def test_the_csv_export_opens_as_utf8_in_a_spreadsheet(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The file starts with the UTF-8 byte-order mark so a spreadsheet that
    otherwise assumes the local code page reads accented text correctly; a
    UTF-8 reader that honours the mark sees the same document as before."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    result = await _pending_result(client, "ev-bom")
    headers = await _agent_client(client, org_admin)
    accented = ResultBlobEnvelope(
        kind="rows",
        columns=["customer", "n"],
        rows=[["Société Générale", 3], ["Zoë's Café", 1]],
        total=2,
    ).model_dump(mode="json")
    upload = await client.post(
        f"/api/v1/objects/{result['id']}/payload",
        json={"envelope": accented, "receipt": _receipt()},
        headers=headers,
    )
    assert upload.status_code == 200
    response = await client.get(f"/api/v1/objects/{result['id']}/export.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in response.headers["content-type"]
    assert response.content.decode("utf-8-sig") == (
        "customer,n\r\nSociété Générale,3\r\nZoë's Café,1\r\n"
    )
    assert response.content.count(b"\xef\xbb\xbf") == 1, "the mark appears once, at the start"


# ---------------------------------------------------------------------------
# The listing: filtered to the audience, without a decision row per row
# ---------------------------------------------------------------------------


async def test_the_listing_omits_what_the_caller_may_not_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A team-scoped result is listed for a member of that team and for an org
    admin, and omitted — not refused, omitted — for a member of the org who
    is not on the team. The predicate is the policy's READ answer."""
    logical_id = f"promote:{uuid4()}:ev-list"
    obj, team_id = await _team_scoped_result(org_admin, logical_id=logical_id)
    outsider = await _member_client(client, org_admin, real_session)
    teammate = await _member_client(client, org_admin, real_session, team_id=team_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    async def listed_ids(who: AsyncClient) -> set[str]:
        response = await who.get("/api/v1/objects", params={"type": "result", "limit": 200})
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    try:
        assert str(obj.id) not in await listed_ids(outsider)
        assert str(obj.id) in await listed_ids(teammate)
        assert str(obj.id) in await listed_ids(client)
    finally:
        await outsider.aclose()
        await teammate.aclose()


async def test_the_listing_leaves_no_decision_rows(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A page of fifty objects must not put fifty rows on the audit lane: the
    listing computes the READ answer with the shared predicate and files
    nothing — for the objects it shows and the ones it omits alike."""
    logical_id = f"promote:{uuid4()}:ev-quiet"
    await _team_scoped_result(org_admin, logical_id=logical_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(3):
        # Org-visible so the outsider's page shows them; the team-scoped row
        # above is the one the outsider's page omits.
        await _create(client, visibility_scope="org")
    outsider = await _member_client(client, org_admin, real_session)
    before = len(await _events(org_admin.org_id, EventType.AUTHZ_DECISION))
    try:
        for who in (client, outsider):
            response = await who.get("/api/v1/objects", params={"limit": 200})
            assert response.status_code == 200
            assert len(response.json()["items"]) >= 3
    finally:
        await outsider.aclose()
    assert len(await _events(org_admin.org_id, EventType.AUTHZ_DECISION)) == before


@pytest.mark.parametrize("object_type", list(OBJECT_TYPES), ids=str)
async def test_every_type_the_column_admits_reads_back_through_the_route(
    client: AsyncClient, org_admin: OrgWithAdmin, object_type: str
) -> None:
    """The route's read shape carries the model's whole vocabulary.

    The shape once spelled a list of its own, and a chat template — a type the
    column admits and the Files tree lists — answered 500 from every read door
    the first time one was fetched by id. A row the database takes is a row the
    route hands back, by id and in the filtered listing, with its type intact;
    the retired kinds are in the table too, because they still load.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    object_id = uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            WorkspaceObject(
                id=object_id,
                org_team_id=org_admin.org_id,
                logical_id=f"vocab-{uuid4().hex[:8]}",
                namespace="workspace",
                type=object_type,
                title="Admitted",
                status="ready",
                version=1,
                spec={},
                owner_user_id=org_admin.admin_id,
                visibility_scope="org",
            )
        )
        await session.commit()

    by_id = await client.get(f"/api/v1/objects/{object_id}")
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["type"] == object_type

    listed = await client.get("/api/v1/objects", params={"type": object_type})
    assert listed.status_code == 200, listed.text
    assert str(object_id) in {item["id"] for item in listed.json()["items"]}


# ---------------------------------------------------------------------------
# a saved result's audience: private until its creator names one
# ---------------------------------------------------------------------------


async def test_a_saved_result_is_private_until_its_creator_names_an_audience(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The generic create used to resolve a team-less audience to the org, so
    a result was readable org-wide the moment it existed. A result is the
    creator's alone until they say otherwise — the row says so, the CREATE
    decision was made over that audience, and a colleague gets the opaque
    not-found on it and never sees it listed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _create(client, title="Pipeline by rep")
    assert created["visibility_scope"] == "private"
    allowed = (await _events(org_admin.org_id, EventType.AUTHZ_DECISION))[-1]
    assert (allowed.entity_id, allowed.payload["action"], allowed.payload["effect"]) == (
        created["id"],
        "create",
        "allow",
    )
    assert allowed.payload["attrs"]["visibility_scope"] == "private"

    outsider = await _member_client(client, org_admin, real_session)
    by_id = await outsider.get(f"/api/v1/objects/{created['id']}")
    assert by_id.status_code == 404, by_id.text
    listed = await outsider.get("/api/v1/objects", params={"type": "result"})
    await outsider.aclose()
    assert listed.status_code == 200, listed.text
    assert created["id"] not in listed.text
    assert "Pipeline by rep" not in listed.text


async def test_the_creator_reads_their_own_private_result(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _create(client)
    mine = await client.get(f"/api/v1/objects/{created['id']}")
    assert mine.status_code == 200, mine.text
    listed = await client.get("/api/v1/objects", params={"type": "result"})
    assert created["id"] in {item["id"] for item in listed.json()["items"]}


async def test_an_org_audience_is_readable_by_a_colleague(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _create(client, visibility_scope="org")
    assert created["visibility_scope"] == "org"
    colleague = await _member_client(client, org_admin, real_session)
    seen = await colleague.get(f"/api/v1/objects/{created['id']}")
    await colleague.aclose()
    assert seen.status_code == 200, seen.text


async def test_a_team_audience_is_the_teams_alone(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    async with AsyncSessionLocal() as session:
        team = await team_service.create_subteam(
            session, org_team_id=org_admin.org_id, name=f"Ops {secrets.token_hex(3)}"
        )
        await session.commit()
        team_id = team.id
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _create(client, visibility_scope=f"team:{team_id}")
    assert created["visibility_scope"] == f"team:{team_id}"

    teammate = await _member_client(client, org_admin, real_session, team_id=team_id)
    outsider = await _member_client(client, org_admin, real_session)
    on_team = await teammate.get(f"/api/v1/objects/{created['id']}")
    off_team = await outsider.get(f"/api/v1/objects/{created['id']}")
    await teammate.aclose()
    await outsider.aclose()
    assert on_team.status_code == 200, on_team.text
    assert off_team.status_code == 404, off_team.text


async def test_an_audience_outside_the_org_answers_like_a_team_that_does_not_exist(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """An org admin passes the audience check on any team — the admin reads
    every scope of the org — so the route has to ask whether the team is the
    org's at all. A team of another org and a team that never existed get one
    answer, byte for byte, and neither leaves a row behind."""
    async with AsyncSessionLocal() as session:
        other_org, _ = await team_service.create_org_with_admin(
            session,
            org_name=f"Elsewhere {secrets.token_hex(3)}",
            admin_email=f"elsewhere-{secrets.token_hex(4)}@example.com",
            admin_first_name="Else",
            admin_last_name="Where",
            admin_password="elsewhere-pass-12345",
        )
        foreign = await team_service.create_subteam(
            session, org_team_id=other_org.id, name="Their ops"
        )
        await session.commit()
        foreign_id = foreign.id
    await login(client, org_admin.admin_email, org_admin.admin_password)

    answers = []
    for scope in (f"team:{foreign_id}", f"team:{uuid4()}"):
        client_id = f"o-{uuid4().hex[:8]}"
        response = await client.post(
            "/api/v1/objects",
            json={
                "type": "result",
                "title": "Theirs",
                "spec": RESULT_SPEC,
                "client_id": client_id,
                "visibility_scope": scope,
            },
        )
        body = response.json()
        # The trace id is minted per request; everything else must match.
        body.get("error", {}).pop("trace_id", None)
        answers.append((response.status_code, body))
        async with AsyncSessionLocal() as session:
            left = await object_service.find_by_logical_id(
                session, org_team_id=org_admin.org_id, namespace="workspace", logical_id=client_id
            )
        assert left is None, f"a refused create under {scope} still wrote a row"
    assert answers[0][0] == 404, answers
    assert answers[0] == answers[1], "a foreign team and a missing team must be one answer"


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param("team:", id="team-with-no-id"),
        pytest.param("team:not-a-uuid", id="team-with-a-bad-id"),
        pytest.param("everyone", id="a-word-the-grammar-lacks"),
        pytest.param("", id="empty"),
        pytest.param("PRIVATE", id="wrong-case"),
    ],
)
async def test_a_malformed_audience_is_a_422(
    client: AsyncClient, org_admin: OrgWithAdmin, scope: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "R",
            "spec": RESULT_SPEC,
            "client_id": f"o-{uuid4().hex[:8]}",
            "visibility_scope": scope,
        },
    )
    assert response.status_code == 422, response.text
