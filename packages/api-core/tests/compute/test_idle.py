"""When an org machine stops for idling, and which pool machines stay awake.

The clock is frozen and moved across each window boundary: a minute short of
the window is never idle, the window itself is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.idle import PoolMachine, is_idle, last_work, pool_machines_to_stop
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.org_machines import OrgMachine
from freezegun import freeze_time

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def _om(idle: int | None = 30, **extra: Any) -> OrgMachine:
    return OrgMachine(**{"name": "m", "idle_stop_minutes": idle, **extra})


def _alloc(
    *,
    state: str = "ready",
    ready_at: datetime = T0,
    chats: int = 0,
    activity: datetime | None = None,
) -> ComputeAllocation:
    return ComputeAllocation(
        state=state, state_changed_at=ready_at, chats_served=chats, last_activity_at=activity
    )


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.parametrize(
    ("om", "alloc", "minutes_later", "idle"),
    [
        pytest.param(_om(), _alloc(), 29, False, id="empty-but-not-ready-for-the-window"),
        pytest.param(_om(), _alloc(), 30, True, id="empty-and-ready-for-the-window"),
        pytest.param(
            _om(),
            _alloc(chats=2, activity=T0 + timedelta(minutes=10)),
            39,
            False,
            id="chats-that-worked-29-minutes-ago",
        ),
        pytest.param(
            _om(),
            _alloc(chats=2, activity=T0 + timedelta(minutes=10)),
            40,
            True,
            id="chats-quiet-for-the-window",
        ),
        pytest.param(
            _om(),
            _alloc(chats=1, activity=T0 - timedelta(hours=5)),
            29,
            False,
            id="activity-before-the-wake-counts-from-the-wake",
        ),
        pytest.param(
            _om(),
            _alloc(chats=1, activity=None),
            30,
            True,
            id="no-activity-reported-counts-from-ready",
        ),
        pytest.param(_om(idle=None), _alloc(), 600, False, id="never-stops"),
        pytest.param(_om(), _alloc(state="draining"), 600, False, id="already-draining"),
        pytest.param(_om(), _alloc(state="bootstrapping"), 600, False, id="still-starting"),
        pytest.param(_om(), _alloc(state="asleep"), 600, False, id="already-asleep"),
        pytest.param(_om(), None, 600, False, id="no-allocation"),
        pytest.param(
            _om(deleted_at=T0), _alloc(), 600, False, id="a-deleted-machine-is-released-not-idled"
        ),
    ],
)
def test_the_idle_decision_across_the_window(
    om: OrgMachine, alloc: ComputeAllocation | None, minutes_later: int, idle: bool
) -> None:
    with freeze_time(T0) as frozen:
        assert not is_idle(om, alloc, _now())
        frozen.move_to(T0 + timedelta(minutes=minutes_later))
        assert is_idle(om, alloc, _now()) is idle


def test_a_chat_starting_work_resets_the_window() -> None:
    alloc = _alloc(chats=1, activity=T0)
    om = _om(idle=15)
    with freeze_time(T0 + timedelta(minutes=14)) as frozen:
        assert not is_idle(om, alloc, _now())
        alloc.last_activity_at = _now()
        frozen.move_to(T0 + timedelta(minutes=16))
        assert not is_idle(om, alloc, _now())
        frozen.move_to(T0 + timedelta(minutes=29))
        assert is_idle(om, alloc, _now())


def test_last_work_is_the_later_of_ready_and_reported() -> None:
    assert last_work(_alloc(activity=T0 + timedelta(minutes=5))) == T0 + timedelta(minutes=5)
    assert last_work(_alloc(activity=T0 - timedelta(minutes=5))) == T0
    assert last_work(_alloc(activity=None)) == T0


def _pool(name: str, *, worked_minutes: int, idle: bool, state: str = "ready") -> PoolMachine:
    alloc = _alloc(state=state, activity=T0 + timedelta(minutes=worked_minutes))
    return PoolMachine(org_machine=_om(name=name), allocation=alloc, idle=idle)


def _names(machines: list[OrgMachine]) -> list[str]:
    return [m.name for m in machines]


def test_min_awake_keeps_the_most_recently_active_running() -> None:
    machines = [
        _pool("a", worked_minutes=50, idle=True),
        _pool("b", worked_minutes=10, idle=True),
        _pool("c", worked_minutes=30, idle=True),
    ]
    # Three running, one kept: the two least recently active go.
    assert _names(pool_machines_to_stop(machines, min_awake=1)) == ["b", "c"]


def test_with_no_minimum_every_idle_pool_machine_stops() -> None:
    machines = [_pool("a", worked_minutes=50, idle=True), _pool("b", worked_minutes=10, idle=False)]
    assert _names(pool_machines_to_stop(machines, min_awake=0)) == ["a"]


def test_a_busy_machine_counts_toward_the_ones_kept_awake() -> None:
    machines = [
        _pool("busy", worked_minutes=59, idle=False),
        _pool("old", worked_minutes=1, idle=True),
        _pool("newer", worked_minutes=40, idle=True),
    ]
    # Two must stay up: the busy one and the newer idle one.
    assert _names(pool_machines_to_stop(machines, min_awake=2)) == ["old"]


def test_machines_not_running_do_not_count_toward_the_minimum() -> None:
    machines = [
        _pool("asleep", worked_minutes=59, idle=False, state="asleep"),
        _pool("only", worked_minutes=1, idle=True),
    ]
    assert pool_machines_to_stop(machines, min_awake=1) == []


def test_a_minimum_above_the_running_count_stops_nothing() -> None:
    machines = [_pool("a", worked_minutes=1, idle=True), _pool("b", worked_minutes=2, idle=True)]
    assert pool_machines_to_stop(machines, min_awake=5) == []


@pytest.mark.parametrize(
    ("idle", "use_mode", "audience_empty", "offering_default", "window"),
    [
        pytest.param(30, "assigned", True, 120, 30, id="the-orgs-setting-wins"),
        pytest.param(None, "assigned", False, 120, None, id="never-while-someone-may-use-it"),
        pytest.param(None, "assigned", True, 120, 120, id="unassigned-takes-the-offering-default"),
        pytest.param(None, "assigned", True, None, 60, id="unassigned-no-default-takes-an-hour"),
        pytest.param(None, "pool", True, None, None, id="a-pool-machine-is-the-whole-orgs"),
    ],
)
def test_the_idle_window(
    idle: int | None,
    use_mode: str,
    audience_empty: bool,
    offering_default: int | None,
    window: int | None,
) -> None:
    from alkera_core.compute.idle import UNASSIGNED_IDLE_MINUTES, idle_window_minutes

    assert UNASSIGNED_IDLE_MINUTES == 60
    om = _om(idle=idle, use_mode=use_mode)
    assert (
        idle_window_minutes(om, audience_empty=audience_empty, offering_default=offering_default)
        == window
    )


def test_a_machine_nobody_may_use_stops_after_the_default_window_even_set_to_never() -> None:
    om = _om(idle=None, use_mode="assigned")
    alloc = _alloc(chats=0)
    with freeze_time(T0 + timedelta(minutes=59)) as frozen:
        assert not is_idle(om, alloc, _now(), audience_empty=True)
        frozen.move_to(T0 + timedelta(minutes=60))
        assert is_idle(om, alloc, _now(), audience_empty=True)
        # The same machine with someone still in its audience runs on.
        assert not is_idle(om, alloc, _now(), audience_empty=False)
