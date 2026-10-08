"""What an org machine reads as, from the allocation backing it.

One row per state and step, including the asymmetric ones: a failed machine
nobody wants on is stopped, not failed; a capacity failure on a machine
wanted on is waiting for hardware; a ready machine past the heartbeat window
is unreachable, crossed with a frozen clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.machines import ready_window
from alkera_core.compute.org_machines import (
    DEFAULT_TIMINGS,
    ProviderTimings,
    in_audience,
    machine_card,
    org_machine_state,
    register_timings,
    timings_for,
)
from alkera_core.compute.worker_faults import UNHEALTHY_MESSAGE
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import OrgMachine, OrgMachineAudience
from freezegun import freeze_time

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _om(**fields: Any) -> OrgMachine:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_team_id": uuid.uuid4(),
        "owner_team_id": uuid.uuid4(),
        "offering_id": uuid.uuid4(),
        "name": "Trainer",
        "acquisition": "purchased",
        "free_until": None,
        "use_mode": "assigned",
        "storage_gb": 100,
        "desired_power": "on",
        "stop_reason": "",
        "deleted_at": None,
        "version": 1,
        "updated_at": NOW - timedelta(minutes=1),
    }
    values.update(fields)
    return OrgMachine(**values)


def _alloc(state: str, **fields: Any) -> ComputeAllocation:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "state": state,
        "provider_machine_id": "",
        "registered_jti": "",
        "last_heartbeat_at": None,
        "failure_kind": "",
        "capacity_gave_up_at": None,
        "drain_kind": None,
        "wake_requested_at": None,
        "state_changed_at": NOW - timedelta(seconds=20),
        "created_at": NOW - timedelta(minutes=5),
        "ready_at": None,
        "price_per_minute_nanos": 0,
        "storage_price_per_minute_nanos": 0,
    }
    values.update(fields)
    return ComputeAllocation(**values)


FRESH = NOW - timedelta(seconds=1)

ROWS = [
    pytest.param(_om(), None, ("starting", "reserving"), id="on-with-nothing-backing-it"),
    pytest.param(_om(desired_power="off"), None, ("stopped", None), id="off-with-nothing"),
    pytest.param(_om(), _alloc("pending"), ("starting", "reserving"), id="pending"),
    pytest.param(
        _om(), _alloc("provisioning"), ("starting", "reserving"), id="provisioning-without-a-pod"
    ),
    pytest.param(
        _om(),
        _alloc("provisioning", provider_machine_id="pod-1"),
        ("starting", "booting"),
        id="provisioning-with-a-pod",
    ),
    pytest.param(
        _om(), _alloc("bootstrapping"), ("starting", "installing"), id="bootstrapping-unclaimed"
    ),
    pytest.param(
        _om(),
        _alloc("bootstrapping", registered_jti="jti-1"),
        ("starting", "connecting"),
        id="bootstrapping-claimed",
    ),
    pytest.param(
        _om(), _alloc("ready"), ("starting", "connecting"), id="ready-but-never-heartbeat"
    ),
    pytest.param(_om(), _alloc("ready", last_heartbeat_at=FRESH), ("running", None), id="running"),
    pytest.param(
        _om(),
        _alloc("ready", last_heartbeat_at=FRESH, fault_code="cgroup_refused", fault_since=FRESH),
        ("unhealthy", None),
        id="answering-but-no-worker-can-serve",
    ),
    pytest.param(
        _om(),
        _alloc("ready", last_heartbeat_at=FRESH, fault_since=FRESH, fault_until=FRESH),
        ("running", None),
        id="a-fault-that-has-cleared",
    ),
    pytest.param(
        _om(),
        _alloc("ready", last_heartbeat_at=NOW - timedelta(hours=1), fault_code="exited"),
        ("unreachable", None),
        id="silence-outranks-a-fault",
    ),
    pytest.param(
        _om(desired_power="off"),
        _alloc("draining", last_heartbeat_at=FRESH),
        ("stopping", None),
        id="draining-toward-off",
    ),
    pytest.param(
        _om(),
        _alloc("draining", last_heartbeat_at=FRESH, drain_kind="move"),
        ("running", None),
        id="draining-while-wanted-on-is-still-running",
    ),
    pytest.param(_om(desired_power="off"), _alloc("releasing"), ("stopping", None), id="releasing"),
    pytest.param(_om(desired_power="off"), _alloc("asleep"), ("stopped", None), id="asleep"),
    pytest.param(_om(), _alloc("asleep"), ("stopped", None), id="asleep-wanted-on-no-wake-yet"),
    pytest.param(
        _om(),
        _alloc("asleep", wake_requested_at=NOW - timedelta(seconds=5)),
        ("starting", "booting"),
        id="asleep-with-a-wake-pending",
    ),
    pytest.param(
        _om(),
        _alloc("failed", failure_kind="capacity"),
        ("waiting_for_hardware", None),
        id="capacity-wanted-on-waits-for-hardware",
    ),
    pytest.param(
        _om(desired_power="off"),
        _alloc("failed", failure_kind="capacity"),
        ("stopped", None),
        id="capacity-wanted-off-is-stopped",
    ),
    pytest.param(
        _om(),
        _alloc("failed", failure_kind="capacity", capacity_gave_up_at=NOW - timedelta(minutes=1)),
        ("failed", None),
        id="capacity-window-ended-couldnt-start",
    ),
    pytest.param(
        _om(),
        _alloc("asleep", failure_kind="capacity", wake_requested_at=NOW - timedelta(minutes=2)),
        ("waiting_for_hardware", None),
        id="start-refused-for-no-gpu-waits-for-hardware",
    ),
    pytest.param(
        _om(),
        _alloc("asleep", failure_kind="capacity", capacity_gave_up_at=NOW - timedelta(minutes=1)),
        ("failed", None),
        id="start-gave-up-after-the-window-couldnt-start",
    ),
    pytest.param(
        _om(desired_power="off"),
        _alloc("asleep", failure_kind="capacity", capacity_gave_up_at=NOW - timedelta(minutes=1)),
        ("stopped", None),
        id="gave-up-but-wanted-off-is-stopped",
    ),
    pytest.param(
        _om(),
        _alloc("failed", failure_kind="quota"),
        ("failed", None),
        id="other-failure-wanted-on-is-failed",
    ),
    pytest.param(
        _om(desired_power="off"),
        _alloc("failed", failure_kind="quota"),
        ("stopped", None),
        id="failed-wanted-off-is-not-failed",
    ),
    pytest.param(_om(), _alloc("lost"), ("failed", None), id="lost-wanted-on"),
    pytest.param(
        _om(), _alloc("lost", failure_kind="capacity"), ("failed", None), id="lost-is-never-waiting"
    ),
    pytest.param(_om(), _alloc("released"), ("failed", None), id="released-wanted-on"),
    pytest.param(
        _om(desired_power="off"), _alloc("released"), ("stopped", None), id="released-wanted-off"
    ),
    pytest.param(
        _om(deleted_at=NOW - timedelta(days=1)),
        _alloc("ready", last_heartbeat_at=FRESH),
        ("deleted", None),
        id="deleted-outranks-everything",
    ),
]


@pytest.mark.parametrize(("om", "alloc", "expected"), ROWS)
def test_org_machine_state(
    om: OrgMachine, alloc: ComputeAllocation | None, expected: tuple[str, str | None]
) -> None:
    assert org_machine_state(om, alloc, now=NOW) == expected


def test_an_unknown_allocation_state_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unknown state"):
        org_machine_state(_om(), _alloc("hibernating"), now=NOW)


@pytest.mark.parametrize("state", ["ready", "draining"])
def test_a_box_that_stops_beating_turns_unreachable_as_the_window_passes(state: str) -> None:
    """Crosses the heartbeat window with the ambient clock: running just
    inside it, unreachable from the moment it is reached."""
    alloc = _alloc(state, last_heartbeat_at=NOW, drain_kind="move")
    om = _om()
    with freeze_time(NOW) as frozen:
        frozen.move_to(NOW + ready_window() - timedelta(seconds=1))
        assert org_machine_state(om, alloc, now=datetime.now(UTC)) == ("running", None)
        frozen.move_to(NOW + ready_window())
        assert org_machine_state(om, alloc, now=datetime.now(UTC)) == ("unreachable", None)


# ---- the card -------------------------------------------------------------------


def _offering(**fields: Any) -> ComputeOffering:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "machine_type_id": uuid.uuid4(),
        "name": "A100 80 GB",
        "pricing_mode": "pass_through",
        "markup_bps": 1_000,
        "fixed_rate_per_minute_nanos": None,
        "storage_gb_default": 50,
        "storage_gb_max": 500,
        "storage_rate_per_gb_month_nanos": 43_200,
        "region": "us-east",
        "audience": "all",
    }
    values.update(fields)
    return ComputeOffering(**values)


def _gpu_type(**fields: Any) -> ComputeMachineType:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": "runpod",
        "provider_type_id": "a100",
        "display_name": "A100",
        "compute_class": "gpu",
        "gpu_count": 2,
        "gpu_name": "NVIDIA A100",
        "gpu_memory_gb": 80,
        "vcpu": 16,
        "memory_gb": 128,
        "disk_gb": 20,
        "provider_price_per_minute_nanos": 10_000_000,
    }
    values.update(fields)
    return ComputeMachineType(**values)


def test_the_card_withholds_rates_from_a_reader_who_may_not_see_them() -> None:
    om, alloc = _om(), _alloc("ready", last_heartbeat_at=FRESH, price_per_minute_nanos=11)
    shown = machine_card(om, alloc, _offering(), _gpu_type(), now=NOW, include_rate=True)
    hidden = machine_card(om, alloc, _offering(), _gpu_type(), now=NOW, include_rate=False)
    assert shown.spec is not None and hidden.spec is not None
    assert shown.spec.rate_per_minute_nanos == 11
    assert hidden.spec.rate_per_minute_nanos is None
    assert hidden.spec.storage_rate_per_minute_nanos is None


def test_the_card_shows_the_pinned_rate_of_a_live_machine_not_todays_price() -> None:
    """A price change never re-rates a running machine, so neither does the card."""
    alloc = _alloc(
        "ready",
        last_heartbeat_at=FRESH,
        price_per_minute_nanos=5,
        storage_price_per_minute_nanos=3,
    )
    card = machine_card(_om(), alloc, _offering(), _gpu_type(), now=NOW, include_rate=True)
    assert card.spec is not None
    assert (card.spec.rate_per_minute_nanos, card.spec.storage_rate_per_minute_nanos) == (5, 3)


def test_a_stopped_machine_shows_what_a_start_would_pin() -> None:
    card = machine_card(
        _om(desired_power="off", stop_reason="user"),
        _alloc("released"),
        _offering(),
        _gpu_type(),
        now=NOW,
        include_rate=True,
    )
    assert card.spec is not None
    # 10_000_000 * 1.1 and 100 GB at 43_200 a GB-month (100 a minute).
    assert card.spec.rate_per_minute_nanos == 11_000_000
    assert card.spec.storage_rate_per_minute_nanos == 100
    assert card.stop_reason == "user"


@pytest.mark.parametrize(
    ("free_until", "rate"),
    [
        pytest.param(NOW + timedelta(days=1), 0, id="granted-and-still-free"),
        pytest.param(NOW, 11_000_000, id="granted-and-free-period-over"),
    ],
)
def test_a_granted_machine_is_free_only_until_free_until(free_until: datetime, rate: int) -> None:
    card = machine_card(
        _om(acquisition="granted", free_until=free_until, desired_power="off"),
        None,
        _offering(),
        _gpu_type(),
        now=NOW,
        include_rate=True,
    )
    assert card.spec is not None and card.spec.rate_per_minute_nanos == rate


def test_the_card_describes_the_hardware() -> None:
    card = machine_card(_om(), None, _offering(), _gpu_type(), now=NOW, include_rate=False)
    assert card.kind == "org_machine"
    assert card.spec is not None
    assert card.spec.gpu is not None
    assert (card.spec.gpu.name, card.spec.gpu.count, card.spec.gpu.memory_gb) == (
        "NVIDIA A100",
        2,
        80,
    )
    assert (card.spec.vcpu, card.spec.memory_gb, card.spec.disk_gb) == (16, 128, 100)
    assert (card.spec.provider, card.spec.region) == ("runpod", "us-east")
    cpu = machine_card(
        _om(), None, _offering(), _gpu_type(gpu_count=0), now=NOW, include_rate=False
    )
    assert cpu.spec is not None and cpu.spec.gpu is None


def test_the_card_carries_the_steps_expected_duration_from_the_registry() -> None:
    kind, compute_class = f"test-{uuid.uuid4().hex[:6]}", "gpu"
    machine_type = _gpu_type(provider=kind, compute_class=compute_class)
    alloc = _alloc("bootstrapping", state_changed_at=NOW - timedelta(seconds=40))
    before = machine_card(_om(), alloc, _offering(), machine_type, now=NOW, include_rate=False)
    assert before.step == "installing"
    assert before.step_expected_seconds == DEFAULT_TIMINGS.installing == 240
    assert before.step_started_at == NOW - timedelta(seconds=40)

    register_timings(kind, compute_class, ProviderTimings(reserving=5, booting=6, installing=7))
    after = machine_card(_om(), alloc, _offering(), machine_type, now=NOW, include_rate=False)
    assert after.step_expected_seconds == 7
    # Registered per kind AND class: the same kind's cpu machines keep the default.
    assert timings_for(kind, "cpu") == DEFAULT_TIMINGS


def test_a_machine_that_is_not_starting_carries_no_step() -> None:
    card = machine_card(
        _om(),
        _alloc("ready", last_heartbeat_at=FRESH),
        _offering(),
        _gpu_type(),
        now=NOW,
        include_rate=False,
    )
    assert (card.state, card.step, card.step_started_at, card.step_expected_seconds) == (
        "running",
        None,
        None,
        None,
    )


def test_timings_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        register_timings("x", "gpu", ProviderTimings(reserving=-1, booting=0, installing=0))


# ---- audience ---------------------------------------------------------------------

USER = uuid.uuid4()
TEAM = uuid.uuid4()


def _grant(om: OrgMachine, kind: str, **fields: Any) -> OrgMachineAudience:
    return OrgMachineAudience(
        org_team_id=fields.pop("org_team_id", om.org_team_id),
        org_machine_id=fields.pop("org_machine_id", om.id),
        grantee_kind=kind,
        **fields,
    )


def _audience_case(kind: str, extra: dict[str, Any], expected: bool, case: str) -> Any:
    return pytest.param(kind, extra, expected, id=case)


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        _audience_case("org", {}, True, "the-whole-org"),
        _audience_case("team", {"team_id": TEAM}, True, "a-team-the-user-is-in"),
        _audience_case("team", {"team_id": uuid.uuid4()}, False, "a-team-the-user-is-not-in"),
        _audience_case("user", {"user_id": USER}, True, "the-user"),
        _audience_case("user", {"user_id": uuid.uuid4()}, False, "someone-else"),
        _audience_case(
            "org", {"org_machine_id": uuid.uuid4()}, False, "a-grant-on-another-machine"
        ),
        _audience_case("org", {"org_team_id": uuid.uuid4()}, False, "a-grant-of-another-org"),
    ],
)
def test_in_audience_of_an_assigned_machine(
    kind: str, fields: dict[str, Any], expected: bool
) -> None:
    om = _om(use_mode="assigned")
    rows = [_grant(om, kind, **fields)]
    assert in_audience(om, rows, user_id=USER, user_team_ids={TEAM}) is expected


def test_an_assigned_machine_with_no_audience_serves_nobody() -> None:
    assert in_audience(_om(), [], user_id=USER, user_team_ids={TEAM}) is False


def test_a_pool_machine_serves_every_member() -> None:
    assert in_audience(_om(use_mode="pool"), [], user_id=USER, user_team_ids=set()) is True


def test_a_deleted_machine_serves_nobody() -> None:
    om = _om(use_mode="pool", deleted_at=NOW)
    assert in_audience(om, [_grant(om, "org")], user_id=USER, user_team_ids={TEAM}) is False


def test_an_unhealthy_machine_tells_its_org_why_in_the_server_s_words_only() -> None:
    alloc = _alloc(
        "ready",
        last_heartbeat_at=FRESH,
        fault_code="cgroup_refused",
        fault_summary="could not make org 0's cgroup: /sys/fs/cgroup/cgroup.subtree_control",
        fault_since=FRESH,
    )
    card = machine_card(_om(), alloc, _offering(), _gpu_type(), now=NOW, include_rate=False)
    assert card.state == "unhealthy"
    assert card.fault is not None
    assert card.fault.code == "cgroup_refused"
    assert card.fault.message == UNHEALTHY_MESSAGE
    # The box's own words and the moment are the console's, not the org's.
    assert card.fault.summary == "" and card.fault.since is None


def test_a_running_machine_carries_no_fault() -> None:
    alloc = _alloc("ready", last_heartbeat_at=FRESH)
    card = machine_card(_om(), alloc, _offering(), _gpu_type(), now=NOW, include_rate=False)
    assert card.fault is None
