"""A box that answers but cannot serve: how its beats are kept, which minutes
go unbilled, and what each reader is told. Also the shared vocabularies the
box and the backend read the same way: the isolation profile and the fault
codes, and the shipped events that carry them."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import (
    ORG_NAMESPACES_NEEDS,
    IsolationMechanism,
    IsolationProfile,
    known_mechanisms,
    profile_for,
    profile_from_capabilities,
)
from alkera_core.compute.box_logs import sanitize_fields
from alkera_core.compute.unservable import (
    fault_read,
    is_unhealthy,
    isolation_read,
    record_fault,
    record_isolation,
    record_silence,
    settle,
    unbilled_span,
    unservable_since,
)
from alkera_core.compute.worker_faults import UNHEALTHY_MESSAGE, WorkerFault, fault_code
from alkera_core.config import settings
from alkera_core.models.compute import ComputeAllocation

T0 = datetime(2026, 10, 6, 6, 50, tzinfo=UTC)
ORG = "8650355e-ce0b-4325-97fe-30bf4516f873"


def _alloc(**fields: Any) -> ComputeAllocation:
    values: dict[str, Any] = {"id": uuid.uuid4(), "fault_summary": "", "capabilities_json": None}
    values.update(fields)
    return ComputeAllocation(**values)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# -- the profile ----------------------------------------------------------------


def test_only_every_needed_mechanism_together_isolates_orgs() -> None:
    assert profile_for(ORG_NAMESPACES_NEEDS) == IsolationProfile.ORG_NAMESPACES
    assert profile_for(set(IsolationMechanism)) == IsolationProfile.ORG_NAMESPACES
    for missing in ORG_NAMESPACES_NEEDS:
        assert profile_for(ORG_NAMESPACES_NEEDS - {missing}) == IsolationProfile.SINGLE_ORG
    # Systemd alone is a way to run units, not a boundary.
    assert profile_for({IsolationMechanism.SYSTEMD}) == IsolationProfile.SINGLE_ORG


@pytest.mark.parametrize(
    ("capabilities", "profile"),
    [
        pytest.param(None, IsolationProfile.SINGLE_ORG, id="said-nothing"),
        pytest.param([], IsolationProfile.SINGLE_ORG, id="said-no-capability"),
        pytest.param(["org_workers"], IsolationProfile.SINGLE_ORG, id="workers-without-isolation"),
        pytest.param([BoxCapability.ORG_ISOLATION], IsolationProfile.ORG_NAMESPACES, id="isolates"),
    ],
)
def test_a_box_is_single_org_unless_it_said_otherwise(
    capabilities: list[str] | None, profile: IsolationProfile
) -> None:
    assert profile_from_capabilities(capabilities) == profile


def test_a_newer_box_s_unknown_mechanisms_are_left_out() -> None:
    assert known_mechanisms(["systemd", "landlock_v9", "systemd"]) == [IsolationMechanism.SYSTEMD]


def test_the_console_reads_the_profile_placement_holds_the_box_to() -> None:
    """A box whose report says ``org_namespaces`` but whose capabilities do
    not is shown as single-org: the console never disagrees with placement."""
    alloc = _alloc(capabilities_json=["org_workers"])
    record_isolation(alloc, {"profile": "org_namespaces", "mechanisms": ["systemd", "bogus"]})
    read = isolation_read(alloc)
    assert read is not None
    assert (read.profile, read.mechanisms) == ("single_org", ["systemd"])
    record_isolation(alloc, None)
    assert isolation_read(alloc) is None


# -- the fault codes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("cgroup_refused", WorkerFault.CGROUP_REFUSED, id="known"),
        pytest.param("new_in_a_later_build", WorkerFault.OTHER, id="unknown"),
        pytest.param(None, WorkerFault.OTHER, id="absent"),
        pytest.param(3, WorkerFault.OTHER, id="not-text"),
    ],
)
def test_a_fault_code_this_side_does_not_know_reads_as_other(
    raw: object, code: WorkerFault
) -> None:
    assert fault_code(raw) == code


@pytest.mark.parametrize("code", [fault.value for fault in WorkerFault])
def test_every_fault_code_leaves_the_box_on_its_events(code: str) -> None:
    for event in ("supervisor.worker.start_failed", "supervisor.worker.crash_loop"):
        out = sanitize_fields(event, {"org_id": ORG, "reason": code})
        assert out == {"org_id": ORG, "reason": code}


def test_a_start_failure_ships_its_reason_and_scrubbed_words_but_never_prose_as_a_code() -> None:
    out = sanitize_fields(
        "supervisor.worker.start_failed",
        {
            "org_id": ORG,
            "reason": "unshare failed",
            "error": "token=" + "abc123def456 unshare failed",
        },
    )
    assert out == {"org_id": ORG, "error": "[redacted] unshare failed"}


@pytest.mark.parametrize(
    ("profile", "kept"),
    [("single_org", True), ("org_namespaces", True), ("anything_else", False)],
)
def test_the_probed_profile_ships_only_as_one_of_its_names(profile: str, kept: bool) -> None:
    out = sanitize_fields("supervisor.isolation.probed", {"profile": profile})
    assert (out == {"profile": profile}) is kept


# -- the span its running time is not billed for --------------------------------


def test_a_beat_with_a_fault_opens_a_span_that_later_beats_continue() -> None:
    alloc = _alloc()
    record_fault(alloc, {"code": "cgroup_refused", "summary": "I/O error"}, now=_at(2))
    record_fault(alloc, {"code": "exited", "summary": "again"}, now=_at(5))
    assert (alloc.fault_since, alloc.fault_until) == (_at(2), None)
    assert (alloc.fault_code, alloc.fault_summary) == ("exited", "again")
    assert is_unhealthy(alloc)


def test_a_beat_without_a_fault_ends_the_span_and_the_machine_reads_healthy() -> None:
    alloc = _alloc()
    record_fault(alloc, {"code": "cgroup_refused"}, now=_at(2))
    record_fault(alloc, None, now=_at(9))
    assert (alloc.fault_since, alloc.fault_until) == (_at(2), _at(9))
    assert not is_unhealthy(alloc)
    assert fault_read(alloc, staff=True) is None
    # A later healthy beat leaves the ended span as it was.
    record_fault(alloc, None, now=_at(12))
    assert alloc.fault_until == _at(9)


def test_a_fault_back_before_the_meter_ran_is_one_span() -> None:
    alloc = _alloc()
    record_fault(alloc, {"code": "exited"}, now=_at(2))
    record_fault(alloc, None, now=_at(3))
    record_fault(alloc, {"code": "exited"}, now=_at(4))
    assert (alloc.fault_since, alloc.fault_until) == (_at(2), None)


def test_a_healthy_box_never_opens_a_span() -> None:
    alloc = _alloc()
    record_fault(alloc, None, now=_at(1))
    assert (alloc.fault_since, alloc.fault_until, alloc.fault_code) == (None, None, None)


def test_the_box_s_words_are_scrubbed_and_cut() -> None:
    alloc = _alloc()
    record_fault(
        alloc, {"code": "other", "summary": "Bearer eyJabc.defgh.ijklm " + "x" * 400}, now=T0
    )
    assert "eyJ" not in alloc.fault_summary
    assert len(alloc.fault_summary) <= 300


@pytest.mark.parametrize(
    ("since", "until", "anchor", "now", "span"),
    [
        pytest.param(None, None, 0, 10, None, id="no-fault"),
        pytest.param(2, None, 0, 10, (2, 10), id="open-span-after-the-anchor"),
        pytest.param(2, None, 5, 10, (5, 10), id="open-span-already-partly-metered"),
        pytest.param(2, 6, 0, 10, (2, 6), id="ended-span"),
        pytest.param(2, 6, 7, 10, None, id="ended-span-already-metered"),
        pytest.param(12, None, 0, 10, None, id="span-after-now"),
    ],
)
def test_the_unbilled_part_of_a_metering_window(
    since: float | None,
    until: float | None,
    anchor: float,
    now: float,
    span: tuple[float, float] | None,
) -> None:
    alloc = _alloc(
        fault_since=None if since is None else _at(since),
        fault_until=None if until is None else _at(until),
    )
    got = unbilled_span(alloc, anchor=_at(anchor), now=_at(now))
    assert got == (None if span is None else (_at(span[0]), _at(span[1])))


def test_an_ended_span_is_forgotten_only_once_metered_past() -> None:
    alloc = _alloc(fault_since=_at(2), fault_until=_at(6), last_metered_at=_at(5))
    settle(alloc)
    assert alloc.fault_since == _at(2)
    alloc.last_metered_at = _at(6)
    settle(alloc)
    assert (alloc.fault_since, alloc.fault_until) == (None, None)


def test_an_open_span_is_never_forgotten() -> None:
    alloc = _alloc(fault_since=_at(2), fault_code="exited", last_metered_at=_at(60))
    settle(alloc)
    assert alloc.fault_since == _at(2)


def test_staff_see_the_box_s_words_and_since_when_the_org_sees_one_sentence() -> None:
    alloc = _alloc(fault_code="cgroup_refused", fault_summary="I/O error", fault_since=_at(2))
    staff = fault_read(alloc, staff=True)
    org = fault_read(alloc, staff=False)
    assert staff is not None and org is not None
    assert (staff.code, staff.message, staff.summary, staff.since) == (
        "cgroup_refused",
        UNHEALTHY_MESSAGE,
        "I/O error",
        _at(2),
    )
    assert (org.summary, org.since) == ("", None)


# -- a box that has gone silent -----------------------------------------------------


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 45)


@pytest.mark.parametrize(
    ("lifecycle", "last_beat", "span"),
    [
        pytest.param("workspace", 3, (3, 10), id="silent-past-the-window"),
        pytest.param("workspace", 9.5, None, id="heard-within-the-window"),
        pytest.param("session", 3, None, id="a-session-pod-has-no-daemon-to-hear"),
    ],
)
def test_a_silent_workspace_box_s_minutes_from_its_last_beat_are_unbilled(
    window: None, lifecycle: str, last_beat: float, span: tuple[float, float] | None
) -> None:
    alloc = _alloc(lifecycle=lifecycle, last_heartbeat_at=_at(last_beat), ready_at=_at(0))
    got = unbilled_span(alloc, anchor=_at(0), now=_at(10))
    assert got == (None if span is None else (_at(span[0]), _at(span[1])))


def test_the_earliest_of_a_fault_and_a_silence_comes_first(window: None) -> None:
    alloc = _alloc(
        lifecycle="workspace",
        fault_since=_at(2),
        fault_until=_at(4),
        last_heartbeat_at=_at(6),
    )
    assert unbilled_span(alloc, anchor=_at(0), now=_at(10)) == (_at(2), _at(4))
    assert unbilled_span(alloc, anchor=_at(4), now=_at(10)) == (_at(6), _at(10))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param((None, None), (3, 14), id="opens-an-ended-span-for-the-gap"),
        pytest.param((1, 2), (1, 14), id="merges-into-an-unmetered-span"),
    ],
)
def test_the_beat_that_ends_a_silence_records_the_gap(
    window: None,
    before: tuple[float | None, float | None],
    after: tuple[float, float],
) -> None:
    since, until = before
    alloc = _alloc(
        last_heartbeat_at=_at(3),
        fault_since=None if since is None else _at(since),
        fault_until=None if until is None else _at(until),
    )
    record_silence(alloc, now=_at(14))
    assert (alloc.fault_since, alloc.fault_until) == (_at(after[0]), _at(after[1]))


def test_a_beat_inside_the_window_records_nothing(window: None) -> None:
    alloc = _alloc(last_heartbeat_at=_at(3))
    record_silence(alloc, now=_at(3.5))
    assert (alloc.fault_since, alloc.fault_until) == (None, None)


def test_an_open_fault_already_covers_a_silence(window: None) -> None:
    alloc = _alloc(last_heartbeat_at=_at(3), fault_since=_at(1), fault_code="exited")
    record_silence(alloc, now=_at(14))
    assert (alloc.fault_since, alloc.fault_until) == (_at(1), None)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"last_heartbeat_at": _at(60)}, None, id="beating"),
        pytest.param({"last_heartbeat_at": _at(10)}, _at(10), id="silent-since-its-last-beat"),
        pytest.param(
            {"last_heartbeat_at": _at(60), "fault_code": "exited", "fault_since": _at(20)},
            _at(20),
            id="faulted-while-beating",
        ),
        pytest.param(
            {"last_heartbeat_at": _at(30), "fault_code": "exited", "fault_since": _at(20)},
            _at(20),
            id="the-earlier-of-the-two",
        ),
        pytest.param(
            {
                "last_heartbeat_at": _at(60),
                "fault_code": "exited",
                "fault_since": _at(20),
                "fault_until": _at(40),
            },
            None,
            id="a-fault-that-ended",
        ),
    ],
)
def test_a_box_is_unservable_since_the_first_of_its_open_fault_and_its_silence(
    fields: dict[str, Any], expected: datetime | None
) -> None:
    alloc = _alloc(lifecycle="workspace", **fields)
    assert unservable_since(alloc, now=_at(60)) == expected
