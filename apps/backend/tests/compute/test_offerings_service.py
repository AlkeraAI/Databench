"""The catalog of machines Alkera sells: who sees what, and what may be sold.

``visible_offerings`` is the one answer to "may this org buy this"; every
reason an offering is hidden is a row of the table below, against the same
org, so a filter dropped from the query turns its row red. The writes refuse
an offering that could not be sold as sent, and each refusal is its own case.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alkera_core.compute.provider import CONTAINER
from alkera_core.config import settings
from alkera_core.models.compute import ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering, ComputeOfferingOrg
from alkera_core.schemas.org_machines import OfferingCreate, OfferingRead, OfferingUpdate
from backend.services.compute import offerings as svc
from backend.services.org import teams as team_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests._offering_helpers import OfferingFactory
from tests.conftest import make_org_enterprise

pytestmark = pytest.mark.asyncio

#: What a provider charges per minute for the test type: 1,000,003 nanos, so
#: a markup that does not divide it evenly shows whether the rate rounds up.
PROVIDER_PRICE = 1_000_003


def _all_configured(_: ComputeMachineType) -> bool:
    return True


@pytest.fixture(autouse=True)
def _saas(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SaaS deployment unless a case says otherwise: a test run with no
    Stripe keys would otherwise read as self-hosted, where every org is
    enterprise."""
    monkeypatch.setattr(settings, "self_hosted", False)


@pytest_asyncio.fixture
async def offerings(real_session: AsyncSession) -> AsyncIterator[OfferingFactory]:
    factory = OfferingFactory(real_session)
    yield factory
    await factory.close()


async def _org(session: AsyncSession, name: str) -> UUID:
    org, _admin = await team_service.create_org_with_admin(
        session,
        org_name=f"{name}-{uuid4().hex[:8]}",
        admin_email=f"{name}-{uuid4().hex[:8]}@example.com",
        admin_password="pass-12345-pass",
        admin_first_name="Test",
        admin_last_name="Admin",
    )
    await session.commit()
    return org.id


async def _visible_ids(session: AsyncSession, org_id: UUID, **kw: Any) -> set[UUID]:
    rows = await svc.visible_offerings(session, org_id, **kw)
    return {row.offering.id for row in rows}


# ---- who sees an offering ------------------------------------------------------------


@pytest.mark.parametrize(
    ("offering", "machine_type", "org", "visible"),
    [
        pytest.param({"audience": "all"}, {}, "plain", True, id="all-is-sold-to-everyone"),
        pytest.param(
            {"audience": "enterprise"}, {}, "plain", False, id="enterprise-hidden-from-a-plan-org"
        ),
        pytest.param(
            {"audience": "enterprise"},
            {},
            "enterprise",
            True,
            id="enterprise-sold-to-an-enterprise-org",
        ),
        pytest.param(
            {"audience": "enterprise"},
            {},
            "self_hosted",
            True,
            id="enterprise-sold-to-any-org-of-a-self-hosted-deployment",
        ),
        pytest.param({"audience": "listed"}, {}, "listed", True, id="listed-sold-to-a-listed-org"),
        pytest.param(
            {"audience": "listed"}, {}, "plain", False, id="listed-hidden-from-an-unlisted-org"
        ),
        pytest.param(
            {"audience": "all", "retired_at": datetime(2026, 1, 1, tzinfo=UTC)},
            {},
            "plain",
            False,
            id="retired",
        ),
        pytest.param(
            {"audience": "all", "purchasable": False}, {}, "plain", False, id="not-purchasable"
        ),
        pytest.param({"audience": "all"}, {"active": False}, "plain", False, id="type-inactive"),
        pytest.param(
            {"audience": "all"},
            {"available_for_new": False},
            "plain",
            False,
            id="type-out-of-stock-for-new-machines",
        ),
        pytest.param(
            {"audience": "all"}, {"configured": False}, "plain", False, id="provider-unconfigured"
        ),
    ],
)
async def test_visibility_table(
    real_session: AsyncSession,
    offerings: OfferingFactory,
    monkeypatch: pytest.MonkeyPatch,
    offering: dict[str, Any],
    machine_type: dict[str, Any],
    org: str,
    visible: bool,
) -> None:
    configured = machine_type.pop("configured", True)
    mt = await make_machine_type(real_session, **machine_type)
    plain = await _org(real_session, "plain")
    other = await _org(real_session, "other")
    target = plain
    if org == "enterprise":
        await make_org_enterprise(plain)
    if org == "self_hosted":
        monkeypatch.setattr(settings, "self_hosted", True)
    listed = [plain] if org == "listed" else [other]
    made = await offerings.make(
        mt, org_ids=listed if offering["audience"] == "listed" else (), **offering
    )
    # A second, always-sellable offering beside it: the table's subject is the
    # one row, never an empty answer.
    control = await offerings.make(await make_machine_type(real_session), audience="all")

    seen = await _visible_ids(
        real_session, target, configured=lambda t: configured or t.id != mt.id
    )

    assert control.id in seen
    assert (made.id in seen) is visible


async def test_the_listed_org_sees_it_and_no_other_does(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session)
    listed = await _org(real_session, "listed")
    stranger = await _org(real_session, "stranger")
    made = await offerings.make(mt, org_ids=[listed], audience="listed")

    assert made.id in await _visible_ids(real_session, listed, configured=_all_configured)
    assert made.id not in await _visible_ids(real_session, stranger, configured=_all_configured)


async def test_the_default_predicate_asks_the_provider_registry(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    """With no predicate passed, a provider this deployment cannot start
    machines at (the container provider never can) hides its offerings."""
    container = await make_machine_type(real_session, provider=CONTAINER)
    hidden = await offerings.make(container, audience="all")
    org = await _org(real_session, "plain")
    assert svc.provider_configured(container) is False
    assert hidden.id not in await _visible_ids(real_session, org)


async def test_visible_offerings_come_in_catalog_order(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "plain")
    late = await offerings.make(mt, sort_order=-50, name="B")
    early = await offerings.make(mt, sort_order=-100, name="A")
    ids = [
        row.offering.id
        for row in await svc.visible_offerings(real_session, org, configured=_all_configured)
    ]
    assert ids.index(early.id) < ids.index(late.id)


# ---- what an org is shown -------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "rate"),
    [
        pytest.param(
            {"pricing_mode": "pass_through", "markup_bps": 0},
            PROVIDER_PRICE,
            id="pass-through-is-the-provider-price",
        ),
        pytest.param(
            {"pricing_mode": "pass_through", "markup_bps": 2500},
            1_250_004,  # ceil(1_000_003 * 1.25)
            id="markup-rounds-up",
        ),
        pytest.param(
            {"pricing_mode": "fixed", "fixed_rate_per_minute_nanos": 1000},
            1000,
            id="fixed-ignores-the-provider-price",
        ),
    ],
)
async def test_the_customer_rate_and_no_cost_on_the_tenant_shape(
    real_session: AsyncSession, offerings: OfferingFactory, over: dict[str, Any], rate: int
) -> None:
    mt = await make_machine_type(real_session, provider_price_per_minute_nanos=PROVIDER_PRICE)
    made = await offerings.make(mt, **over)

    read = svc.offering_read(made, mt)
    admin = await svc.admin_offering(real_session, made.id)

    assert read.rate_per_minute_nanos == rate
    assert admin.rate_per_minute_nanos == rate
    assert admin.provider_price_per_minute_nanos == PROVIDER_PRICE
    dumped = read.model_dump()
    assert not {k for k in dumped if "cost" in k or "markup" in k or "provider_price" in k}
    assert set(dumped) == set(OfferingRead.model_fields)


async def test_a_gpu_type_reads_its_card(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session, compute_class="gpu")
    mt.gpu_name = "NVIDIA A40"
    mt.gpu_memory_gb = 48
    await real_session.commit()
    read = svc.offering_read(await offerings.make(mt), mt)
    assert read.gpu is not None
    assert (read.gpu.name, read.gpu.count, read.gpu.memory_gb) == ("NVIDIA A40", 1, 48)
    cpu = await make_machine_type(real_session)
    assert svc.offering_read(await offerings.make(cpu), cpu).gpu is None


# ---- writing the catalog ----------------------------------------------------------------


def _create(mt: ComputeMachineType, **over: Any) -> OfferingCreate:
    body: dict[str, Any] = {
        "machine_type_id": str(mt.id),
        "name": "CPU large",
        "pricing_mode": "pass_through",
        "markup_bps": 0,
        "storage_gb_default": 20,
        "storage_gb_max": 100,
        "audience": "all",
        **over,
    }
    return OfferingCreate.model_validate(body)


async def test_create_writes_the_row_and_its_orgs(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "listed")
    made = await svc.create_offering(
        real_session,
        _create(
            mt, audience="listed", org_ids=[str(org), str(org)], storage_rate_per_gb_month_nanos=7
        ),
        created_by=None,
        configured=_all_configured,
    )
    await real_session.commit()
    offerings.remember(made.id)

    admin = await svc.admin_offering(real_session, made.id)
    assert admin.org_ids == [str(org)]
    assert admin.audience == "listed"
    assert admin.storage_rate_per_gb_month_nanos == 7


@pytest.mark.parametrize(
    ("over", "code"),
    [
        pytest.param({"pricing_mode": "fixed"}, "fixed_rate_required", id="fixed-needs-a-rate"),
        pytest.param(
            {"storage_gb_default": 50, "storage_gb_max": 10},
            "storage_max_below_default",
            id="storage-max-below-default",
        ),
        pytest.param(
            {"audience": "all", "org_ids": ["ORG"]},
            "org_ids_need_listed",
            id="orgs-only-on-a-listed-offering",
        ),
        pytest.param(
            {"audience": "listed", "org_ids": [str(uuid4())]},
            "org_not_found",
            id="an-unknown-org",
        ),
        pytest.param(
            {"audience": "listed", "org_ids": ["not-a-uuid"]},
            "org_not_found",
            id="a-malformed-org-id",
        ),
        pytest.param(
            {"machine_type_id": str(uuid4())}, "machine_type_not_found", id="no-such-type"
        ),
        pytest.param(
            {"_unconfigured": True}, "provider_not_configured", id="provider-unconfigured"
        ),
    ],
)
async def test_create_refuses(real_session: AsyncSession, over: dict[str, Any], code: str) -> None:
    mt = await make_machine_type(real_session)
    mt_id = mt.id
    org = await _org(real_session, "listed")
    unconfigured = over.pop("_unconfigured", False)
    if "org_ids" in over:
        over["org_ids"] = [str(org) if raw == "ORG" else raw for raw in over["org_ids"]]
    before = await real_session.scalar(
        select(ComputeOffering.id).where(ComputeOffering.machine_type_id == mt_id)
    )

    with pytest.raises(svc.OfferingError) as refused:
        await svc.create_offering(
            real_session,
            _create(mt, **over),
            created_by=None,
            configured=(lambda _: False) if unconfigured else _all_configured,
        )
    await real_session.rollback()

    assert refused.value.code == code
    assert refused.value.status == 422
    assert before is None
    assert (
        await real_session.scalar(
            select(ComputeOffering.id).where(ComputeOffering.machine_type_id == mt_id)
        )
    ) is None


async def test_update_changes_only_what_was_sent(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session, provider_price_per_minute_nanos=PROVIDER_PRICE)
    made = await offerings.make(mt, name="Before", description="kept", markup_bps=100)

    await svc.update_offering(
        real_session,
        made.id,
        OfferingUpdate.model_validate({"name": "After", "markup_bps": 2500}),
        configured=_all_configured,
    )
    await real_session.commit()

    admin = await svc.admin_offering(real_session, made.id)
    assert (admin.name, admin.description, admin.markup_bps) == ("After", "kept", 2500)
    assert admin.rate_per_minute_nanos == 1_250_004


async def test_retire_and_restore(real_session: AsyncSession, offerings: OfferingFactory) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "plain")
    made = await offerings.make(mt)
    stamp = datetime(2026, 10, 5, 12, tzinfo=UTC)

    await svc.update_offering(
        real_session,
        made.id,
        OfferingUpdate(retired=True),
        configured=_all_configured,
        now=stamp,
    )
    await real_session.commit()
    assert (await svc.admin_offering(real_session, made.id)).retired_at == stamp
    assert made.id not in await _visible_ids(real_session, org, configured=_all_configured)

    # Retiring twice keeps the first stamp.
    await svc.update_offering(
        real_session, made.id, OfferingUpdate(retired=True), configured=_all_configured
    )
    await real_session.commit()
    assert (await svc.admin_offering(real_session, made.id)).retired_at == stamp

    await svc.update_offering(
        real_session, made.id, OfferingUpdate(retired=False), configured=_all_configured
    )
    await real_session.commit()
    assert (await svc.admin_offering(real_session, made.id)).retired_at is None
    assert made.id in await _visible_ids(real_session, org, configured=_all_configured)


async def test_moving_off_listed_drops_the_list(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "listed")
    made = await offerings.make(mt, org_ids=[org], audience="listed")

    await svc.update_offering(
        real_session,
        made.id,
        OfferingUpdate(audience="enterprise"),
        configured=_all_configured,
    )
    await real_session.commit()

    left = (
        await real_session.execute(
            select(ComputeOfferingOrg).where(ComputeOfferingOrg.offering_id == made.id)
        )
    ).all()
    assert left == []
    assert (await svc.admin_offering(real_session, made.id)).org_ids == []


async def test_an_update_keeps_the_list_of_a_listed_offering(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "listed")
    made = await offerings.make(mt, org_ids=[org], audience="listed")
    await svc.update_offering(
        real_session, made.id, OfferingUpdate(name="Renamed"), configured=_all_configured
    )
    await real_session.commit()
    assert (await svc.admin_offering(real_session, made.id)).org_ids == [str(org)]


@pytest.mark.parametrize(
    ("body", "code", "status"),
    [
        pytest.param(
            {"pricing_mode": "fixed"}, "fixed_rate_required", 422, id="to-fixed-without-a-rate"
        ),
        pytest.param({"name": None}, "field_required", 422, id="null-for-a-required-field"),
        pytest.param(
            {"storage_gb_max": 5}, "storage_max_below_default", 422, id="max-below-default"
        ),
        pytest.param(
            {"org_ids": ["ORG"]}, "org_ids_need_listed", 422, id="orgs-on-an-all-offering"
        ),
    ],
)
async def test_update_refuses(
    real_session: AsyncSession,
    offerings: OfferingFactory,
    body: dict[str, Any],
    code: str,
    status: int,
) -> None:
    mt = await make_machine_type(real_session)
    org = await _org(real_session, "plain")
    made = await offerings.make(mt, name="Kept")
    made_id = made.id
    if "org_ids" in body:
        body = {**body, "org_ids": [str(org)]}

    with pytest.raises(svc.OfferingError) as refused:
        await svc.update_offering(
            real_session,
            made_id,
            OfferingUpdate.model_validate(body),
            configured=_all_configured,
        )
    await real_session.rollback()

    assert (refused.value.code, refused.value.status) == (code, status)
    assert (await svc.admin_offering(real_session, made_id)).name == "Kept"


async def test_a_missing_offering_is_not_found(real_session: AsyncSession) -> None:
    with pytest.raises(svc.OfferingError) as refused:
        await svc.update_offering(
            real_session, uuid4(), OfferingUpdate(name="x"), configured=_all_configured
        )
    assert (refused.value.status, refused.value.code) == (404, "offering_not_found")


async def test_moving_to_another_type_needs_a_configured_provider(
    real_session: AsyncSession, offerings: OfferingFactory
) -> None:
    made_id = (await offerings.make(await make_machine_type(real_session))).id
    other_id = (await make_machine_type(real_session)).id
    with pytest.raises(svc.OfferingError) as refused:
        await svc.update_offering(
            real_session,
            made_id,
            OfferingUpdate(machine_type_id=str(other_id)),
            configured=lambda t: t.id != other_id,
        )
    await real_session.rollback()
    assert refused.value.code == "provider_not_configured"

    await svc.update_offering(
        real_session,
        made_id,
        OfferingUpdate(machine_type_id=str(other_id)),
        configured=_all_configured,
    )
    await real_session.commit()
    assert (await svc.admin_offering(real_session, made_id)).machine_type_id == str(other_id)
