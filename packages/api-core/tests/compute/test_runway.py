"""Runway and credit-state tables, with every threshold hit exactly."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from alkera_core.compute.runway import (
    CREDIT_STATES,
    WARNING_STATES,
    CreditState,
    credit_state,
    drain_due,
    runway_minutes,
)


@dataclass(frozen=True)
class _Thresholds:
    machine_credit_low_hours: int = 24
    machine_credit_urgent_minutes: int = 60
    machine_credit_drain_minutes: int = 5


T = _Thresholds()


@pytest.mark.parametrize(
    ("available", "burn", "expected"),
    [
        pytest.param(1_000, 0, None, id="zero-burn-has-no-runway"),
        pytest.param(1_000, -5, None, id="negative-burn-treated-as-none"),
        pytest.param(0, 0, None, id="nothing-and-no-burn"),
        pytest.param(0, 10, 0, id="empty-account"),
        pytest.param(-500, 10, 0, id="overdrawn-account-has-no-runway"),
        pytest.param(9, 10, 0, id="less-than-one-minute-rounds-down"),
        pytest.param(10, 10, 1, id="exactly-one-minute"),
        pytest.param(19, 10, 1, id="partial-minute-not-counted"),
        pytest.param(600, 10, 60, id="an-hour"),
        pytest.param(10**15, 7, 10**15 // 7, id="large-balance-integer-exact"),
    ],
)
def test_runway_minutes(available: int, burn: int, expected: int | None) -> None:
    assert runway_minutes(available, burn) == expected


@pytest.mark.parametrize(
    ("runway", "expected"),
    [
        pytest.param(None, "ok", id="no-burn-is-ok"),
        pytest.param(24 * 60 + 1, "ok", id="one-minute-past-low-is-ok"),
        pytest.param(24 * 60, "low", id="exactly-low-threshold-is-low"),
        pytest.param(61, "low", id="one-minute-past-urgent-is-low"),
        pytest.param(60, "urgent", id="exactly-urgent-threshold-is-urgent"),
        pytest.param(5, "urgent", id="drain-threshold-still-reads-urgent"),
        pytest.param(0, "urgent", id="empty-reads-urgent-until-the-meter-acts"),
        pytest.param(10**9, "ok", id="huge-runway"),
    ],
)
def test_credit_state_reads_the_runway(runway: int | None, expected: CreditState) -> None:
    assert credit_state(runway, T) == expected


@pytest.mark.parametrize(
    ("runway", "draining", "stopped", "expected"),
    [
        pytest.param(10**9, True, False, "draining", id="draining-outranks-ok"),
        pytest.param(None, True, False, "draining", id="draining-with-no-burn"),
        pytest.param(None, False, True, "stopped", id="stopped-burns-nothing-yet-reads-stopped"),
        pytest.param(30, True, True, "stopped", id="stopped-outranks-draining"),
        pytest.param(0, False, True, "stopped", id="stopped-outranks-urgent"),
    ],
)
def test_meter_actions_outrank_the_runway(
    runway: int | None, draining: bool, stopped: bool, expected: CreditState
) -> None:
    assert credit_state(runway, T, draining=draining, stopped=stopped) == expected


@pytest.mark.parametrize(
    ("runway", "due"),
    [
        pytest.param(None, False, id="no-burn-never-drains"),
        pytest.param(6, False, id="one-past-window"),
        pytest.param(5, True, id="exactly-the-window"),
        pytest.param(0, True, id="empty"),
    ],
)
def test_drain_due(runway: int | None, due: bool) -> None:
    assert drain_due(runway, T) is due


def test_thresholds_come_from_settings_not_constants() -> None:
    tight = _Thresholds(
        machine_credit_low_hours=1, machine_credit_urgent_minutes=10, machine_credit_drain_minutes=2
    )
    assert credit_state(61, tight) == "ok"
    assert credit_state(60, tight) == "low"
    assert credit_state(10, tight) == "urgent"
    assert drain_due(2, tight) and not drain_due(3, tight)


def test_every_state_but_ok_is_a_warning() -> None:
    assert set(CREDIT_STATES) - WARNING_STATES == {"ok"}
