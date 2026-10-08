"""What a chat's machine reporting on it does to the chat's spec."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.objects.publisher_report import apply_publisher_report, clear_refusal

NOW = "2026-10-04T12:00:00+00:00"
EARLIER = "2026-10-04T11:00:00+00:00"
WAKE = "2026-10-04T10:00:00+00:00"


def _after(spec: dict[str, Any], state: str, reason: str = "") -> dict[str, Any]:
    return apply_publisher_report(spec, state=state, reason=reason, now=NOW)


def test_a_refusal_carries_its_reason_or_its_state() -> None:
    assert _after({}, "refused", "no credit")["publisher_refusal"] == "no credit"
    assert _after({}, "refused")["publisher_refusal"] == "refused"


@pytest.mark.parametrize("state", ["publishing", "waiting", "asleep"])
def test_every_other_report_clears_a_refusal(state: str) -> None:
    assert "publisher_refusal" not in _after({"publisher_refusal": "no credit"}, state)


def test_publishing_holds_the_session_and_answers_a_wake() -> None:
    after = _after({"mirror_state": "asleep", "wake_requested_at": WAKE}, "publishing")
    assert after["mirror_state"] == "awake"
    assert "wake_requested_at" not in after


@pytest.mark.parametrize("state", ["waiting", "refused", "asleep"])
def test_no_other_report_says_the_box_holds_the_session(state: str) -> None:
    after = _after({"mirror_state": "asleep", "wake_requested_at": WAKE}, state)
    assert after["mirror_state"] == "asleep"
    assert after["wake_requested_at"] == WAKE, "a wake stands until a box opens the chat"


def test_waiting_stamps_when_the_wait_began_and_keeps_it() -> None:
    assert _after({}, "waiting")["slot_wait_at"] == NOW
    assert _after({"slot_wait_at": EARLIER}, "waiting")["slot_wait_at"] == EARLIER


@pytest.mark.parametrize("state", ["publishing", "refused", "asleep"])
def test_every_other_report_ends_the_wait(state: str) -> None:
    assert "slot_wait_at" not in _after({"slot_wait_at": EARLIER}, state)


def test_the_report_leaves_the_rest_of_the_spec_and_its_input_alone() -> None:
    spec = {"machine_id": "m-1", "last_seq": 7, "slot_wait_at": EARLIER}
    after = _after(spec, "publishing")
    assert after["machine_id"] == "m-1" and after["last_seq"] == 7
    assert spec == {"machine_id": "m-1", "last_seq": 7, "slot_wait_at": EARLIER}


def test_a_refusal_is_stamped_each_time_it_is_said() -> None:
    """A box saying the same refusal again after a wake moves its stamp past
    the wake, so the refusal reads as current again."""
    assert _after({}, "refused", "no credit")["publisher_refusal_at"] == NOW
    again = {"publisher_refusal": "no credit", "publisher_refusal_at": EARLIER}
    assert _after(again, "refused", "no credit")["publisher_refusal_at"] == NOW


@pytest.mark.parametrize("state", ["publishing", "waiting", "asleep"])
def test_every_other_report_clears_the_refusals_stamp(state: str) -> None:
    after = _after({"publisher_refusal": "no credit", "publisher_refusal_at": EARLIER}, state)
    assert "publisher_refusal_at" not in after


def test_clearing_a_refusal_drops_its_stamp_and_kind_too() -> None:
    spec: dict[str, Any] = {
        "publisher_refusal": "x",
        "publisher_refusal_at": EARLIER,
        "publisher_refusal_kind": "moving",
        "last_seq": 3,
    }
    clear_refusal(spec)
    assert spec == {"last_seq": 3}


def test_a_refusal_keeps_its_kind_and_a_kindless_one_drops_a_stale_kind() -> None:
    assert (
        apply_publisher_report({}, state="refused", reason="r", now=NOW, kind="moving")[
            "publisher_refusal_kind"
        ]
        == "moving"
    )
    stale = {"publisher_refusal": "r", "publisher_refusal_kind": "moving"}
    assert "publisher_refusal_kind" not in _after(stale, "refused", "other")


@pytest.mark.parametrize("state", ["publishing", "waiting", "asleep"])
def test_every_other_report_clears_the_refusals_kind(state: str) -> None:
    spec = {"publisher_refusal": "r", "publisher_refusal_kind": "moving"}
    assert "publisher_refusal_kind" not in _after(spec, state)
