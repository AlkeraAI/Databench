"""The daemon's own REST client against the real backend app.

Contract C2/C5 has two consumers: the browser (typed against the generated SDK,
so a shape change fails its build) and the DAEMON, whose client is hand-written
Python that no build checks against the routes. Every test the daemon lane
wrote drives that client at a fake FastAPI app declared inside the test file,
so a body the real route refuses reads green there.

This module closes that gap: :class:`alkera_cli.cloud.rest.CloudRestClient` —
the class the box actually runs — is pointed at the real backend through an
ASGI transport, with a real device JWT and the real agent-assertion headers.
Both sides are production code; nothing here declares a route.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.receipt import ReceiptPrincipal, ResultReceipt
from alkera_cli.cloud.rest import CloudRestClient
from alkera_core.schemas.objects import ResultBlobEnvelope
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

REPO_ROOT = Path(__file__).resolve().parents[3]
#: The receipt a real promote + upload leaves on the object — the bytes the
#: browser's receipt panel is rendered over (``receiptPanel.test.tsx``).
RECEIPT_FROM_ROUTES = (
    REPO_ROOT / "packages/api-core/tests/fixtures/objects/seam/receipt_from_routes.json"
)


async def _daemon(org: OrgWithAdmin, *, agent_id: str = "seam-machine") -> CloudRestClient:
    token = await mint_cli_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
    )
    return CloudRestClient(
        api_url="http://testserver",
        token=token,
        agent_id=agent_id,
        transport=ASGITransport(app=fastapi_app),
    )


def _browser() -> AsyncClient:
    return app_client(base_url="http://testserver")


async def test_the_daemon_registers_and_heartbeats_the_machine_it_runs_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """``CloudRestClient.register_machine`` must be accepted by
    ``POST /api/v1/machines/register``.

    Nothing else in the tree calls that route, so this is the only proof the
    box can announce itself. If the daemon's body is not the one
    ``MachineRegisterRequest`` declares, the box never registers, every chat is
    created with ``machine_status="none"``, and the banner never leaves
    "starting".
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    daemon = await _daemon(org_admin)

    # The facts provisioning wrote into the box's env: the pod it runs on and
    # the catalog code of its flavor. The daemon sends them as the route's own
    # request model, so the route accepts what the box actually posts.
    registered = await daemon.register_machine(
        name="demo-box",
        provider=machine_type.provider,
        provider_pod_id="pod-seam-1",
        machine_type_code=machine_type.provider_type_id,
    )
    machine_id = registered["id"]
    assert machine_id
    assert registered["provider_pod_id"] == "pod-seam-1"

    await daemon.heartbeat_machine(machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        current = await browser.get("/api/v1/machines/current")
        assert current.status_code == 200, current.text
        assert current.json()["machine_id"] == machine_id
        assert current.json()["status"] == "ready"


async def test_the_daemon_reads_the_chats_the_cloud_created(org_admin: OrgWithAdmin) -> None:
    """Discovery: the service polls ``GET /chats`` and reads each chat through
    this client, then filters on ``machine_id`` to decide what it mirrors."""
    daemon = await _daemon(org_admin)
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "seam"})
        assert created.status_code == 201, created.text
        chat_id = created.json()["id"]

    listed = await daemon.list_chats()
    assert chat_id in [item["id"] for item in listed["items"]]
    fetched = await daemon.get_chat(chat_id)
    assert "machine_id" in fetched


async def test_the_daemon_mints_a_socket_ticket_with_its_device_token(
    org_admin: OrgWithAdmin,
) -> None:
    daemon = await _daemon(org_admin)
    assert await daemon.mint_ticket()


async def test_a_promoted_result_accepts_the_receipt_the_daemon_actually_builds(
    org_admin: OrgWithAdmin,
) -> None:
    """Promote, end to end across the boundary.

    The browser promotes a transcript event; the daemon uploads the payload
    with the receipt ``ChatMirror._receipt_for`` produces. That receipt leaves
    ``role`` and ``duration_ms`` as ``None`` whenever the SQL tool result did
    not carry a role or the call's start time is unknown — which is the normal
    case today, because ``SqlQueryResult`` has no ``role`` field at all. The
    cloud's ``Receipt`` declares both non-optional, so the upload is refused
    and the result stays in ``pending_upload`` forever.
    """
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat = (await browser.post("/api/v1/chats", json={"title": "promote seam"})).json()
        promoted = await browser.post(
            f"/api/v1/chats/{chat['id']}/promote",
            json={
                "event_id": "call-1-result",
                "title": "Share of voice",
                "columns": [{"name": "day", "label": None}],
                "chart_spec": None,
            },
        )
        assert promoted.status_code == 201, promoted.text
        object_id = promoted.json()["id"]
        assert promoted.json()["status"] == "pending_upload"

    daemon = await _daemon(org_admin, agent_id=chat["id"])
    envelope = ResultBlobEnvelope(
        kind="rows", columns=["day", "mentions"], rows=[["2026-09-01", 12]], total=1
    )
    receipt = ResultReceipt(
        sql="select day, count(*) from prompt_runs group by day",
        connection_name="planetscale-read",
        role=None,
        engine="postgres",
        principal=ReceiptPrincipal(user_id=str(org_admin.admin_id), agent_id=chat["id"]),
        executed_at=datetime.now(UTC),
        row_count=1,
        duration_ms=None,
        params={},
        event_id="call-1-result",
    )

    body: dict[str, Any] = await daemon.upload_payload(
        object_id,
        envelope=envelope.model_dump(mode="json"),
        receipt=receipt.model_dump(mode="json"),
    )
    assert body["status"] == "ready"

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        rows = await browser.get(f"/api/v1/objects/{object_id}/rows")
        assert rows.status_code == 200, rows.text
        assert rows.json()["total"] == 1


async def test_the_receipt_the_routes_store_is_the_receipt_the_browser_panel_reads(
    org_admin: OrgWithAdmin,
) -> None:
    """The receipt a real promote + upload leaves on the object.

    The browser's receipt panel is tested against ``generate.py``'s dump, whose
    ``principal_chain`` is a hand-authored ``{acting, chain}``. The routes write
    something else: ``promoted_by`` at the promote and ``uploaded_by`` at the
    upload, each an actor-chain record the server resolves, with the daemon's
    own ``principal`` riding alongside as an extra. ``principalSummary`` copes
    with both, so there is no defect today — but the free test reads a shape no
    route produces, which is the exact species of green that hid round 2's five
    mismatches.

    So the stored receipt is recorded here from the real routes and read back by
    ``receiptPanel.test.tsx``: whatever the panel renders, it renders over what
    a promotion actually leaves behind.
    """
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat = (await browser.post("/api/v1/chats", json={"title": "receipt seam"})).json()
        promoted = await browser.post(
            f"/api/v1/chats/{chat['id']}/promote",
            json={
                "event_id": "call-7-result",
                "title": "Share of voice by customer",
                "columns": [{"name": "day", "label": None}],
                "chart_spec": None,
            },
        )
        assert promoted.status_code == 201, promoted.text
        object_id = promoted.json()["id"]

    daemon = await _daemon(org_admin, agent_id=chat["id"])
    envelope = ResultBlobEnvelope(
        kind="rows", columns=["day", "orders"], rows=[["2026-09-05", 12]], total=1
    )
    receipt = ResultReceipt(
        sql="select day, count(*) from orders where customer = %(customer)s group by day",
        connection_name="Tideline Postgres",
        role="analytics_readonly",
        engine="postgres",
        principal=ReceiptPrincipal(user_id=str(org_admin.admin_id), agent_id=chat["id"]),
        executed_at=datetime.now(UTC),
        row_count=1,
        duration_ms=412,
        params={"customer": "acme"},
        event_id="call-7-result",
    )
    assert (
        await daemon.upload_payload(
            object_id,
            envelope=envelope.model_dump(mode="json"),
            receipt=receipt.model_dump(mode="json"),
        )
    )["status"] == "ready"

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        stored = await browser.get(f"/api/v1/objects/{object_id}")
        assert stored.status_code == 200, stored.text
    on_record: dict[str, Any] = stored.json()["spec"]["receipt"]

    if os.environ.get("SEAM_FIXTURE_WRITE") == "1":
        RECEIPT_FROM_ROUTES.write_text(json.dumps(on_record, indent=2, sort_keys=True) + "\n")

    # The two halves of the chain the SERVER stamps, neither of them the
    # machine's to write — a nested actor-chain record each, not the flat
    # `{acting, chain}` the generated spec dump carries.
    chain = on_record["principal_chain"]
    assert sorted(chain) == ["promoted_by", "uploaded_by"], chain
    assert chain["promoted_by"]["acting"]["id"] == str(org_admin.admin_id)
    assert chain["promoted_by"]["acting"]["kind"] == "user"
    # The upload is the machine acting FOR the member: the agent is the actor
    # and the member is who it delegates from.
    assert chain["uploaded_by"]["acting"]["kind"] == "agent"
    assert chain["uploaded_by"]["delegating_user"]["id"] == str(org_admin.admin_id)
    # The machine's own principal rides alongside as an extra, never inside the
    # chain the server owns.
    assert on_record["principal"]["user_id"] == str(org_admin.admin_id)

    recorded = json.loads(RECEIPT_FROM_ROUTES.read_text())
    assert sorted(recorded) == sorted(on_record), "re-record with SEAM_FIXTURE_WRITE=1"
    assert sorted(recorded["principal_chain"]) == sorted(chain), (
        "the browser's panel is tested against a principal chain the routes no longer write"
    )


@pytest.mark.parametrize("presents_credential", [True, False], ids=["beat", "bare-beat"])
async def test_a_revoked_box_hears_a_refusal_its_daemon_stops_on(
    real_session: AsyncSession, platform_admin: OrgWithAdmin, presents_credential: bool
) -> None:
    """The daemon stops serving only on the refusal codes it knows are final
    (``FATAL_CLAIM_REFUSALS``); anything else it treats as an outage and beats
    through for ever. So the refusal the REAL route answers a revoked box with
    has to be one of those codes, read by the REAL client — a fake route that
    answered the code the daemon expects proved nothing about the server,
    which answered ``forbidden``."""
    from alkera_cli.cloud.rest import CloudApiError
    from alkera_cli.cloud.service import FATAL_CLAIM_REFUSALS
    from alkera_core.compute.provider import EC2
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import User

    async with AsyncSessionLocal() as session:
        admin = await session.get(User, platform_admin.admin_id)
        assert admin is not None
        admin.email_verified_at = datetime.now(UTC)
        await session.commit()
    machine_type = await make_machine_type(
        real_session, provider=EC2, provider_price_per_minute_nanos=1
    )
    async with _browser() as console:
        await login(console, platform_admin.admin_email, platform_admin.admin_password)
        minted = await console.post(
            "/admin/v1/machines",
            json={
                "label": "seam-box",
                "provider": machine_type.provider,
                "instance_type": machine_type.provider_type_id,
                "region": "us-west-2",
                "tenancy": "pool",
            },
        )
        assert minted.status_code == 201, minted.text
        credential = minted.json()["credential"]
        daemon = await _daemon(platform_admin, agent_id="booting")
        claimed = await daemon.claim_machine(
            credential=credential,
            name="seam-box",
            provider_pod_id=f"i-{os.urandom(6).hex()}",
            capacity=4,
            daemon_version="1",
        )
        machine_id = claimed["id"]
        box = daemon.for_agent(machine_id)
        await box.heartbeat_machine(machine_id, credential=credential)

        revoked = await console.delete(
            f"/admin/v1/machines/{minted.json()['machine']['credential_id']}"
        )
        assert revoked.status_code == 204

    with pytest.raises(CloudApiError) as refused:
        await box.heartbeat_machine(
            machine_id, credential=credential if presents_credential else ""
        )
    assert refused.value.status == 401
    assert refused.value.code in FATAL_CLAIM_REFUSALS
