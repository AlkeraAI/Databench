"""A box's refusal on a chat reads as ``refused`` only while it is the box's
word now: a refusal said before a wake, or before the machine life the chat
waits on, reads as the machine's own state instead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alkera_core.models.compute import ComputeAllocation
from alkera_core.schemas.objects.specs import ChatSpec, MirrorState
from backend.services.compute.placement import chat_machine_status

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
REASON = "this chat's workspace is open on another machine"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _box(
    *, ready_since: datetime, beat: datetime | None, state: str = "ready"
) -> ComputeAllocation:
    return ComputeAllocation(state=state, last_heartbeat_at=beat, state_changed_at=ready_since)


def _chat(
    *,
    refused_at: datetime | None,
    wake_at: datetime | None = None,
    mirror: MirrorState | None = None,
) -> ChatSpec:
    return ChatSpec(
        machine_id=str(uuid4()),
        machine_status="ready",
        publisher_refusal=REASON,
        publisher_refusal_at=_iso(refused_at) if refused_at else None,
        wake_requested_at=_iso(wake_at) if wake_at else None,
        mirror_state=mirror,
    )


def _status(spec: ChatSpec, box: ComputeAllocation) -> str:
    return chat_machine_status(spec, box, now=T0 + timedelta(seconds=5))


def test_a_refusal_from_before_the_machine_was_started_again_reads_starting() -> None:
    """The wake: the machine was started after the refusal and has not looked
    at the chat yet."""
    starting = _box(ready_since=T0 - timedelta(minutes=2), beat=None)
    assert _status(_chat(refused_at=T0 - timedelta(hours=1)), starting) == "starting"


def test_a_refusal_on_a_machine_that_slept_since_reads_asleep_so_a_message_can_wake_it() -> None:
    asleep = _box(ready_since=T0 - timedelta(minutes=30), beat=None, state="asleep")
    assert _status(_chat(refused_at=T0 - timedelta(hours=1)), asleep) == "asleep"


@pytest.mark.parametrize(
    ("box", "word"),
    [
        pytest.param(
            _box(ready_since=T0 - timedelta(minutes=2), beat=None), "starting", id="starting"
        ),
        pytest.param(
            _box(ready_since=T0 - timedelta(minutes=30), beat=None, state="asleep"),
            "asleep",
            id="asleep",
        ),
    ],
)
def test_an_unstamped_refusal_on_a_machine_coming_up_or_asleep_reads_as_the_machine(
    box: ComputeAllocation, word: str
) -> None:
    assert _status(_chat(refused_at=None), box) == word


def test_a_wake_asked_for_after_the_refusal_reads_as_the_wake() -> None:
    box = _box(ready_since=T0 - timedelta(hours=2), beat=T0)
    spec = _chat(
        refused_at=T0 - timedelta(minutes=5), wake_at=T0 - timedelta(minutes=1), mirror="asleep"
    )
    assert _status(spec, box) == "asleep"


def test_a_refusal_from_the_machines_previous_life_reads_as_the_new_one() -> None:
    """The machine became ready after the refusal: a new process that has not
    said anything about the chat yet."""
    box = _box(ready_since=T0 - timedelta(minutes=1), beat=T0)
    assert _status(_chat(refused_at=T0 - timedelta(minutes=5)), box) == "ready"


def test_a_refusal_the_box_said_after_the_wake_and_in_this_life_stands() -> None:
    box = _box(ready_since=T0 - timedelta(hours=1), beat=T0)
    spec = _chat(
        refused_at=T0 - timedelta(minutes=1), wake_at=T0 - timedelta(minutes=5), mirror="asleep"
    )
    assert _status(spec, box) == "refused"


def test_an_unstamped_refusal_on_an_up_machine_stands() -> None:
    box = _box(ready_since=T0 - timedelta(minutes=1), beat=T0)
    assert _status(_chat(refused_at=None), box) == "refused"


@pytest.mark.parametrize(
    "box",
    [
        pytest.param(
            _box(ready_since=T0 - timedelta(hours=1), beat=None), id="registered-not-beating"
        ),
        pytest.param(
            _box(ready_since=T0 - timedelta(hours=1), beat=T0, state="draining"), id="draining"
        ),
        pytest.param(
            _box(ready_since=T0 - timedelta(hours=1), beat=T0 - timedelta(hours=1)),
            id="unreachable",
        ),
    ],
)
def test_a_refusal_said_in_the_machines_current_state_stands(box: ComputeAllocation) -> None:
    assert _status(_chat(refused_at=T0 - timedelta(minutes=5)), box) == "refused"
