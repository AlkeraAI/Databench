"""The org-machine wire shapes: round trips, bounds, and what a reader may see."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pytest
from alkera_core.schemas import compute as compute_schemas
from alkera_core.schemas import org_machines as om_schemas
from alkera_core.schemas.compute import (
    BoxMachineCard,
    MachineHeartbeatRequest,
    MachineHeartbeatResponse,
)
from alkera_core.schemas.compute_machines import GpuSample, MachineResources
from alkera_core.schemas.objects import WorkspaceSpec
from alkera_core.schemas.org_machines import (
    PLATFORM_ONLY_SHAPES,
    AudienceEntry,
    GpuSpec,
    MachineCard,
    MachineSpec,
    MachineUsageRead,
    MachineUsageRow,
    OfferingAdminRead,
    OrgComputeSettingsUpdate,
    OrgMachineDetail,
    OrgMachinePurchase,
    OrgMachineUpdate,
    WorkspaceMachineMoveRead,
    WorkspaceMachineRead,
)
from pydantic import BaseModel, ValidationError

AT = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

CARD = MachineCard(
    kind="org_machine",
    org_machine_id="om-1",
    name="Trainer",
    spec=MachineSpec(
        offering_name="A100 80 GB",
        provider="runpod",
        region="us-east",
        gpu=GpuSpec(name="NVIDIA A100", count=1, memory_gb=80),
        vcpu=16,
        memory_gb=128,
        disk_gb=100,
        rate_per_minute_nanos=1_000,
        storage_rate_per_minute_nanos=None,
    ),
    state="starting",
    step="installing",
    step_started_at=AT,
    step_expected_seconds=240,
    stop_reason="",
)

DETAIL = OrgMachineDetail(
    id="om-1",
    name="Trainer",
    card=CARD,
    use_mode="assigned",
    acquisition="granted",
    free_until=AT,
    audience=[AudienceEntry(kind="team", team_id="t-1", label="Research")],
    idle_stop_minutes=30,
    monthly_cap_nanos=None,
    owner_team_id="t-0",
    owner_team_name="Acme",
    version=3,
    can_manage=True,
    can_use=True,
    spend_this_cycle_nanos=5,
    credit_state="low",
    runway_minutes=90,
    created_at=AT,
    workspaces=[{"id": "w-1", "name": "Main"}],
    timeline=[{"at": AT, "words": "Started"}],
)

ROUND_TRIPS: list[BaseModel] = [
    CARD,
    MachineCard(kind="shared", name="Shared machines", state="shared"),
    DETAIL,
    WorkspaceMachineRead(
        card=CARD,
        pin="om-1",
        active_move=WorkspaceMachineMoveRead(
            id="mv-1", workspace_id="w-1", state="draining", requested_at=AT
        ),
        can_move=True,
        targets=[MachineCard(kind="shared", name="Shared machines", state="shared"), CARD],
    ),
    MachineUsageRead(
        rows=[
            MachineUsageRow(
                org_machine_id="om-1",
                name="Trainer",
                day=date(2026, 10, 5),
                running_minutes=60,
                stopped_minutes=0,
                compute_nanos=10,
                storage_nanos=1,
                refund_nanos=0,
            )
        ]
    ),
    BoxMachineCard(
        name="Trainer",
        gpu=GpuSpec(name="NVIDIA A100", count=1, memory_gb=80),
        vcpu=16,
        memory_gb=128,
        disk_gb=100,
        billed_per_minute=True,
        idle_stop_minutes=None,
    ),
    MachineHeartbeatResponse(card=None),
]


@pytest.mark.parametrize("model", ROUND_TRIPS, ids=lambda m: type(m).__name__)
def test_round_trip(model: BaseModel) -> None:
    again = type(model).model_validate_json(model.model_dump_json())
    assert again == model


def _purchase(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "offering_id": "o-1",
        "name": "Trainer",
        "storage_gb": 100,
        "audience": [{"kind": "org"}],
        "idle_stop_minutes": None,
        "idempotency_key": "k" * 16,
    }
    body.update(fields)
    return body


@pytest.mark.parametrize(
    ("fields", "ok"),
    [
        pytest.param({}, True, id="defaults"),
        pytest.param({"name": "x"}, True, id="one-character-name"),
        pytest.param({"name": "x" * 64}, True, id="sixty-four-character-name"),
        pytest.param({"name": ""}, False, id="empty-name"),
        pytest.param({"name": "   "}, False, id="whitespace-only-name"),
        pytest.param({"name": " " + "x" * 64 + " "}, True, id="blanks-around-a-full-name"),
        pytest.param({"name": "x" * 65}, False, id="sixty-five-character-name"),
        pytest.param({"idempotency_key": "k" * 7}, False, id="short-idempotency-key"),
        pytest.param({"idempotency_key": "k" * 8}, True, id="eight-character-idempotency-key"),
        pytest.param({"idempotency_key": "k" * 65}, False, id="long-idempotency-key"),
        pytest.param({"storage_gb": 0}, False, id="no-storage"),
        pytest.param({"idle_stop_minutes": 4}, False, id="idle-under-five-minutes"),
        pytest.param({"idle_stop_minutes": 5}, True, id="idle-at-five-minutes"),
        pytest.param({"monthly_cap_nanos": -1}, False, id="negative-cap"),
        pytest.param({"use_mode": "dedicated"}, False, id="unknown-use-mode"),
    ],
)
def test_purchase_bounds(fields: dict[str, Any], ok: bool) -> None:
    if ok:
        OrgMachinePurchase.model_validate(_purchase(**fields))
    else:
        with pytest.raises(ValidationError):
            OrgMachinePurchase.model_validate(_purchase(**fields))


@pytest.mark.parametrize(
    "model",
    [pytest.param(OrgMachinePurchase, id="purchase"), pytest.param(OrgMachineUpdate, id="update")],
)
def test_a_name_is_stored_without_its_surrounding_blanks(model: type[BaseModel]) -> None:
    body = _purchase(name="  Lab A ") if model is OrgMachinePurchase else {"name": "  Lab A "}
    assert model.model_validate(body).model_dump()["name"] == "Lab A"


def test_a_purchase_must_state_its_idle_stop_and_defaults_to_assigned() -> None:
    body = _purchase()
    del body["idle_stop_minutes"]
    with pytest.raises(ValidationError, match="idle_stop_minutes"):
        OrgMachinePurchase.model_validate(body)
    purchase = OrgMachinePurchase.model_validate(_purchase())
    assert (purchase.use_mode, purchase.owner_team_id, purchase.monthly_cap_nanos) == (
        "assigned",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("body", "sent", "values"),
    [
        pytest.param({}, set(), {}, id="nothing-sent-changes-nothing"),
        pytest.param(
            {"idle_stop_minutes": None},
            {"idle_stop_minutes"},
            {"idle_stop_minutes": None},
            id="null-clears-the-idle-stop",
        ),
        pytest.param(
            {"monthly_cap_nanos": 7, "name": "B"},
            {"monthly_cap_nanos", "name"},
            {"monthly_cap_nanos": 7, "name": "B"},
            id="two-fields",
        ),
    ],
)
def test_update_tells_not_sent_from_null(
    body: dict[str, Any], sent: set[str], values: dict[str, Any]
) -> None:
    update = OrgMachineUpdate.model_validate(body)
    assert update.model_fields_set == sent
    assert update.model_dump(include=sent) == values


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": ""}, id="empty-name"),
        pytest.param({"name": " \t "}, id="whitespace-only-name"),
        pytest.param({"idle_stop_minutes": 1}, id="idle-too-short"),
        pytest.param({"use_mode": "shared"}, id="unknown-use-mode"),
    ],
)
def test_update_refuses_what_a_purchase_would(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OrgMachineUpdate.model_validate(body)


def test_settings_update_refuses_a_negative_awake_pool() -> None:
    with pytest.raises(ValidationError):
        OrgComputeSettingsUpdate(min_awake_pool=-1)


# ---- heartbeat and spec additions are optional ----------------------------------


def test_an_old_daemons_heartbeat_still_parses() -> None:
    beat = MachineHeartbeatRequest.model_validate(
        {"capacity": 6, "resources": {"cpu_percent": 1.0}}
    )
    assert beat.last_activity_at is None
    assert beat.resources is not None and beat.resources.gpus == []


def test_a_new_heartbeat_carries_gpus_and_activity() -> None:
    beat = MachineHeartbeatRequest(
        resources=MachineResources(
            gpus=[
                GpuSample(
                    index=0,
                    name="NVIDIA A100",
                    memory_used_bytes=1,
                    memory_total_bytes=2,
                    utilization_percent=50.0,
                )
            ]
        ),
        last_activity_at=AT,
    )
    again = MachineHeartbeatRequest.model_validate_json(beat.model_dump_json())
    assert again == beat


@pytest.mark.parametrize("utilization", [-0.1, 100.1])
def test_gpu_utilization_is_a_percentage(utilization: float) -> None:
    with pytest.raises(ValidationError):
        GpuSample(index=0, utilization_percent=utilization)


def test_the_workspace_pin_is_optional_and_round_trips() -> None:
    assert WorkspaceSpec().machine_pin is None
    pinned = WorkspaceSpec(machine_pin="om-1")
    dumped = pinned.model_dump(mode="json")
    assert dumped["schema_version"] == "1.3.0"
    assert WorkspaceSpec.model_validate(dumped).machine_pin == "om-1"
    # A 1.1.0 row, written before the pin, reads as unpinned.
    assert WorkspaceSpec.model_validate({"schema_version": "1.1.0"}).machine_pin is None
    # A 1.2.0 row, written before a machine could be lost, has lost nothing.
    older = WorkspaceSpec.model_validate({"schema_version": "1.2.0", "machine_pin": "om-1"})
    assert (older.lost_machine_id, older.fell_back_at) == (None, None)


# ---- what a tenant may see ---------------------------------------------------------

FORBIDDEN = re.compile(r"(true_)?cost|margin|markup|provider_price")


def _models(module: object) -> list[type[BaseModel]]:
    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value is not BaseModel
        and value.__module__ == getattr(module, "__name__", "")
    ]


@pytest.mark.parametrize(
    "model",
    [m for m in _models(om_schemas) if m.__name__ not in PLATFORM_ONLY_SHAPES]
    + _models(compute_schemas),
    ids=lambda m: m.__name__,
)
def test_no_tenant_shape_carries_a_cost_field(model: type[BaseModel]) -> None:
    assert [f for f in model.model_fields if FORBIDDEN.search(f)] == []


def test_the_platform_only_list_names_real_shapes_and_the_admin_read_carries_the_figures() -> None:
    """Guards the guard: the exemptions exist, and the platform read does carry
    what a tenant read must not, so the walk above is testing the boundary."""
    names = {m.__name__ for m in _models(om_schemas)}
    assert names >= PLATFORM_ONLY_SHAPES
    assert {"markup_bps", "provider_price_per_minute_nanos"} <= set(OfferingAdminRead.model_fields)


def test_a_sample_without_gpus_dumps_exactly_what_an_older_daemon_sent() -> None:
    """The heartbeat stores the dump; a beat with no GPUs must not grow a key
    the daemon never sent, and a beat with GPUs must keep them."""
    sample = {"cpu_percent": 1.0, "memory_used_bytes": 2}
    assert "gpus" not in MachineResources.model_validate(sample).model_dump()
    gpu = {
        "index": 0,
        "name": "A100",
        "memory_used_bytes": 1,
        "memory_total_bytes": 2,
        "utilization_percent": 3.0,
    }
    dumped = MachineResources.model_validate({**sample, "gpus": [gpu]}).model_dump()
    assert dumped["gpus"] == [gpu]
