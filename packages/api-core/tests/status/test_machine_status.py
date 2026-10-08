"""A machine's status holds its estimate against the clock and names every state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.status import MACHINE_STATUS, MachineEvidence, fleet_status, machine_status

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


def ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


def read(evidence: MachineEvidence) -> tuple[str, str, str, str]:
    fact = machine_status(evidence, now=NOW)
    return (fact.state, fact.reason_code, fact.tone, fact.sentence)


def installing(started: int, expected: int = 240) -> MachineEvidence:
    return MachineEvidence(
        state="starting",
        name="lab-b",
        step="installing",
        step_started_at=ago(started),
        step_expected_seconds=expected,
    )


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param(
            installing(60),
            ("starting", "installing", "info", "Installing Alkera, about 3 minutes."),
            id="estimate-still-holds",
        ),
        pytest.param(
            installing(210),
            ("starting", "installing", "info", "Installing Alkera, about 1 minute."),
            id="last-minute",
        ),
        pytest.param(
            installing(240),
            (
                "starting",
                "installing_overdue",
                "info",
                "Installing Alkera is taking longer than usual.",
            ),
            id="at-the-estimate",
        ),
        pytest.param(
            installing(14 * 60),
            (
                "starting",
                "installing_overdue",
                "info",
                "Installing Alkera is taking longer than usual.",
            ),
            id="fourteen-minutes-on-a-one-minute-step",
        ),
        pytest.param(
            MachineEvidence(state="starting", name="lab-b", step="connecting"),
            ("starting", "", "info", "lab-b is starting."),
            id="a-step-with-no-estimate",
        ),
        pytest.param(
            MachineEvidence(state="waiting_for_hardware", name="lab-b"),
            (
                "no_capacity",
                "",
                "warning",
                "No hardware is free for lab-b right now. We keep trying.",
            ),
            id="no-capacity",
        ),
        pytest.param(
            MachineEvidence(state="waiting_for_hardware", name="lab-b", gpu_name="A40"),
            ("no_capacity", "gpu", "warning", "No A40 is free right now. We keep trying."),
            id="no-gpu",
        ),
        pytest.param(
            MachineEvidence(
                state="waiting_for_hardware",
                name="lab-b",
                gpu_name="A40",
                wait_reason="RunPod has no 8 vCPU hosts right now.",
            ),
            (
                "no_capacity",
                "provider",
                "warning",
                "RunPod has no 8 vCPU hosts right now. We keep trying.",
            ),
            id="the-providers-refusal-wins-over-the-generic-words",
        ),
        pytest.param(
            MachineEvidence(state="stopped", name="lab-b", stop_reason="credits"),
            ("stopped", "credits", "warning", "lab-b is stopped. It ran out of credits."),
            id="stopped-for-credit",
        ),
        pytest.param(
            MachineEvidence(state="stopped", name="lab-b", stop_reason="user"),
            ("stopped", "", "muted", "lab-b is stopped. Sending a message starts it."),
            id="stopped-by-a-person",
        ),
        pytest.param(
            MachineEvidence(state="stopping", name="lab-b", stop_reason="cap"),
            (
                "stopping",
                "cap",
                "neutral",
                "lab-b reached its monthly cap and stops soon. Your files are saved.",
            ),
            id="draining-for-its-cap",
        ),
        pytest.param(
            MachineEvidence(state="running", name="lab-b", unhealthy=True),
            ("unhealthy", "", "danger", "lab-b can't run chats."),
            id="up-but-no-worker-serves",
        ),
        pytest.param(
            MachineEvidence(state="unreachable", name=""),
            ("unreachable", "", "danger", "The machine isn't responding."),
            id="unnamed",
        ),
    ],
)
def test_machine_status(evidence: MachineEvidence, expected: tuple[str, str, str, str]) -> None:
    assert read(evidence) == expected


def test_a_step_is_read_again_when_its_estimate_runs_out() -> None:
    fact = machine_status(installing(60), now=NOW)
    assert fact.recheck_at == ago(60) + timedelta(seconds=240)
    assert fact.since == ago(60)
    assert fact.recheck_at is not None
    later = machine_status(installing(60), now=fact.recheck_at + timedelta(seconds=60))
    assert later.reason_code == "installing_overdue"


def test_a_wait_for_hardware_is_read_again_when_the_provider_is_asked_again() -> None:
    retry = NOW + timedelta(minutes=5)
    evidence = MachineEvidence(
        state="waiting_for_hardware",
        name="lab-b",
        wait_reason="RunPod has no 8 vCPU hosts right now.",
        wait_retry_at=retry,
    )
    assert machine_status(evidence, now=NOW).recheck_at == retry


@pytest.mark.parametrize(
    "state",
    [
        "starting",
        "running",
        "unreachable",
        "stopping",
        "stopped",
        "waiting_for_hardware",
        "failed",
        "deleted",
        "shared",
    ],
)
def test_every_card_state_has_its_words(state: str) -> None:
    fact = machine_status(MachineEvidence(state=state, name="lab-b"), now=NOW)
    assert fact.state in MACHINE_STATUS.states


@pytest.mark.parametrize(
    ("allocation", "liveness", "revoked", "wake", "expected"),
    [
        pytest.param(None, "none", True, False, ("revoked", ""), id="revoked-and-never-claimed"),
        pytest.param(None, "none", False, False, ("starting", ""), id="minted-not-claimed-yet"),
        pytest.param("pending", "none", False, False, ("starting", ""), id="pending"),
        pytest.param("ready", "starting", False, False, ("starting", ""), id="ready-never-beat"),
        pytest.param("ready", "ready", False, False, ("ready", ""), id="ready"),
        pytest.param("ready", "unreachable", False, False, ("unreachable", ""), id="silent"),
        pytest.param("draining", "ready", False, False, ("draining", ""), id="draining"),
        pytest.param("asleep", "asleep", False, False, ("asleep", ""), id="asleep"),
        pytest.param(
            "asleep", "asleep", False, True, ("starting", "after_stop"), id="wake-pending"
        ),
        pytest.param("lost", "none", True, False, ("lost", ""), id="lost"),
        pytest.param("released", "none", True, False, ("released", ""), id="released"),
        pytest.param("from_a_newer_server", "none", False, False, ("failed", ""), id="unknown"),
    ],
)
def test_fleet_status(
    allocation: str | None, liveness: str, revoked: bool, wake: bool, expected: tuple[str, str]
) -> None:
    fact = fleet_status(
        allocation_state=allocation,
        liveness=liveness,
        name="box-1",
        revoked=revoked,
        wake_pending=wake,
    )
    assert (fact.state, fact.reason_code) == expected


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param(
            MachineEvidence(
                state="failed",
                name="lab-b",
                failure="never_registered",
                failure_detail="ApiError: unreachable: Connection refused",
            ),
            (
                "failed",
                "never_registered_error",
                "danger",
                "lab-b couldn't start because its node never registered with this server. "
                'Its last error was "ApiError: unreachable: Connection refused".',
            ),
            id="with-the-nodes-error",
        ),
        pytest.param(
            MachineEvidence(state="failed", name="lab-b", failure="never_registered"),
            (
                "failed",
                "never_registered",
                "danger",
                "lab-b couldn't start because its node never registered with this server.",
            ),
            id="without-an-error",
        ),
        pytest.param(
            MachineEvidence(state="failed", name="lab-b"),
            ("failed", "", "danger", "lab-b couldn't start."),
            id="another-failure",
        ),
        pytest.param(
            MachineEvidence(state="stopped", name="lab-b", failure="never_registered"),
            ("stopped", "", "muted", "lab-b is stopped. Sending a message starts it."),
            id="only-a-failed-machine-says-it",
        ),
    ],
)
def test_a_boot_that_never_registered_says_why(
    evidence: MachineEvidence, expected: tuple[str, str, str, str]
) -> None:
    assert read(evidence) == expected
