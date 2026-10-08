"""Where a chat's agent session stands, as a reader is told it."""

from __future__ import annotations

import pytest
from alkera_core.objects.chat_session_state import chat_session_state
from alkera_core.schemas.objects.specs import ChatSpec, MachineStatus, MirrorState, SessionState


def _spec(mirror: MirrorState | None, *, wake: bool = False) -> ChatSpec:
    return ChatSpec(
        machine_id="00000000-0000-0000-0000-000000000001",
        mirror_state=mirror,
        wake_requested_at="2026-10-04T10:00:00+00:00" if wake else None,
    )


@pytest.mark.parametrize(
    ("mirror", "machine", "wake", "pending", "expected"),
    [
        pytest.param("awake", "ready", False, False, "awake", id="a-box-holds-it"),
        pytest.param("awake", "ready", False, True, "working", id="a-box-holds-it-mid-turn"),
        pytest.param("asleep", "asleep", False, False, "asleep", id="the-box-slept-it"),
        pytest.param(None, "ready", False, False, "asleep", id="no-box-ever-opened-it"),
        pytest.param("asleep", "asleep", True, False, "waking", id="a-wake-stands"),
        pytest.param("asleep", "asleep", False, True, "waking", id="a-message-waits"),
        pytest.param("awake", "unreachable", False, False, "asleep", id="its-box-went-quiet"),
        pytest.param("awake", "draining", False, True, "waking", id="its-box-drains-mid-turn"),
        # Nothing will open a chat whose box is gone, that its box refused, or
        # that no machine serves: reading "Waking" promised what would not
        # happen. It rests, and its status says why it can't run.
        pytest.param("awake", "stranded", True, False, "asleep", id="its-box-is-gone-a-wake-waits"),
        pytest.param("asleep", "refused", False, True, "asleep", id="its-box-refused-it"),
        pytest.param("asleep", "refused", True, False, "asleep", id="refused-a-wake-waits"),
        pytest.param(None, "none", False, True, "asleep", id="no-machine-a-message-waits"),
        # No box has ever held its session: there is nothing to wake.
        pytest.param(None, "ready", False, True, "starting", id="a-new-chats-first-message"),
        pytest.param(None, "starting", False, True, "starting", id="a-new-chat-on-a-booting-box"),
        pytest.param(None, "ready", True, False, "starting", id="a-new-chat-opened"),
    ],
)
def test_a_chats_session_state_follows_the_box_the_wake_and_the_turn(
    mirror: MirrorState | None,
    machine: MachineStatus,
    wake: bool,
    pending: bool,
    expected: SessionState,
) -> None:
    """``awake`` and ``working`` need the bound box ready AND holding the
    session; a box that said ``awake`` and then went quiet or drained no longer
    holds it. A message or a wake waiting for a box reads ``waking``, or
    ``starting`` when no box has ever held the chat's session."""
    assert chat_session_state(_spec(mirror, wake=wake), machine, pending) == expected


QUEUED_AT = "2026-10-04T10:05:00+00:00"


@pytest.mark.parametrize(
    ("mirror", "machine", "wake", "pending", "expected"),
    [
        pytest.param("asleep", "ready", False, True, "queued", id="a-message-waits-for-a-slot"),
        pytest.param("asleep", "ready", True, False, "queued", id="a-wake-waits-for-a-slot"),
        pytest.param(None, "ready", False, True, "queued", id="never-opened-and-waiting"),
        pytest.param("asleep", "asleep", False, True, "queued", id="slept-on-a-live-box"),
        # A slot wait is the box's word, and a box that is not ready says nothing.
        pytest.param("asleep", "unreachable", False, True, "waking", id="its-box-went-quiet"),
        pytest.param("asleep", "draining", False, True, "waking", id="its-box-drains"),
        # Nothing to open: a stale stamp on an idle chat is not a queue.
        pytest.param("asleep", "ready", False, False, "asleep", id="nothing-waits"),
        # Served since: the box holds it, whatever the stamp said.
        pytest.param("awake", "ready", False, True, "working", id="served-mid-turn"),
    ],
)
def test_a_chat_its_box_has_no_slot_for_reads_queued(
    mirror: MirrorState | None,
    machine: MachineStatus,
    wake: bool,
    pending: bool,
    expected: SessionState,
) -> None:
    """Seen under load: chats with a message waiting read ``ready`` for four
    minutes while their box served others. Once the box says it has the
    message but no free slot, the chat reads ``queued``."""
    spec = _spec(mirror, wake=wake).model_copy(update={"slot_wait_at": QUEUED_AT})
    assert chat_session_state(spec, machine, pending) == expected
