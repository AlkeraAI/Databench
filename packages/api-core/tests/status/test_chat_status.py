"""A chat's status says work is happening only on evidence the worker reports."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from alkera_core.chat_refusals import CHAT_REFUSAL_KINDS, refusal_is_final
from alkera_core.status import (
    CHAT_STATUS,
    ChatBounds,
    ChatEvidence,
    chat_status,
    placement_status,
    refusal_read,
)

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
BOUNDS = ChatBounds(turn_silence=timedelta(seconds=120), turn_start=timedelta(seconds=180))


def ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


#: A chat a ready box holds, that has run before and owes nothing.
HELD = ChatEvidence(
    machine_status="ready",
    machine_answers=True,
    machine_name="lab-b",
    session_held=True,
    has_run=True,
)
#: The same chat after its box closed its session.
SLEPT = replace(HELD, machine_status="asleep", session_held=False)


def read(evidence: ChatEvidence) -> tuple[str, str, str] | None:
    fact = chat_status(evidence, now=NOW, bounds=BOUNDS)
    return None if fact is None else (fact.state, fact.reason_code, fact.sentence)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param(HELD, ("awake", "", "Ready for a message."), id="held-idle"),
        pytest.param(SLEPT, ("asleep", "", "Asleep. A message wakes it."), id="slept"),
        pytest.param(
            replace(HELD, turn_working=True, turn_stamped_at=ago(10)),
            ("working", "", "The agent is working."),
            id="turn-stamped-ten-seconds-ago",
        ),
        pytest.param(
            replace(HELD, turn_working=True, turn_stamped_at=ago(119)),
            ("working", "", "The agent is working."),
            id="turn-stamp-just-inside-the-bound",
        ),
        pytest.param(
            replace(HELD, turn_working=True, turn_stamped_at=ago(120)),
            ("stalled", "turn_silent", "The agent has stopped reporting progress."),
            id="turn-stamp-at-the-bound",
        ),
        pytest.param(
            replace(HELD, turn_working=True, turn_stamped_at=None),
            ("working", "", "The agent is working."),
            id="turn-with-no-stamp-is-an-older-box",
        ),
        pytest.param(
            replace(HELD, prompt_owed=True, prompt_at=ago(30)),
            ("waking", "", "Waking the chat."),
            id="message-sent-thirty-seconds-ago",
        ),
        pytest.param(
            replace(HELD, prompt_owed=True, prompt_at=ago(13 * 60)),
            ("stalled", "turn_not_started", "lab-b has the message and hasn't started on it."),
            id="ready-machine-sat-on-a-message-for-thirteen-minutes",
        ),
        pytest.param(
            replace(SLEPT, wake_requested_at=ago(20)),
            ("waking", "", "Waking the chat."),
            id="wake-asked",
        ),
        pytest.param(
            replace(SLEPT, wake_requested_at=ago(181)),
            ("stalled", "wake_overdue", "lab-b hasn't picked this chat up."),
            id="wake-nobody-took",
        ),
        pytest.param(
            replace(SLEPT, prompt_owed=True, prompt_at=ago(400), slot_wait_at=ago(390)),
            ("queued", "", "Queued until a slot on its machine frees up."),
            id="queued-for-a-slot-is-not-stalled",
        ),
        pytest.param(
            replace(SLEPT, machine_answers=False, prompt_owed=True, prompt_at=ago(400)),
            (
                "waking",
                "machine_starting",
                "lab-b is starting. Your message is sent as soon as it is ready.",
            ),
            id="stopped-machine-being-started-is-not-stalled",
        ),
        pytest.param(
            replace(HELD, machine_status="starting", machine_answers=False, session_held=False),
            (
                "waking",
                "machine_starting",
                "lab-b is starting. Your message is sent as soon as it is ready.",
            ),
            id="machine-starting",
        ),
        pytest.param(
            replace(HELD, machine_status="draining"),
            (
                "awake",
                "machine_draining",
                "lab-b is being replaced. This chat continues on the next machine.",
            ),
            id="machine-draining",
        ),
        pytest.param(
            replace(HELD, machine_status="draining", machine_restarting=True),
            (
                "awake",
                "machine_restarting",
                "lab-b is restarting and picks this chat up again when it is back.",
            ),
            id="machine-restarting-in-place",
        ),
        pytest.param(
            replace(HELD, machine_status="unreachable", machine_answers=False, turn_working=True),
            ("unavailable", "machine_unreachable", "lab-b isn't responding."),
            id="unreachable-machine-is-not-working",
        ),
        pytest.param(
            replace(HELD, machine_status="unreachable", machine_answers=False, machine_name=""),
            ("unavailable", "machine_unreachable", "The machine isn't responding."),
            id="unreachable-unnamed",
        ),
        pytest.param(
            replace(SLEPT, machine_status="none", machine_answers=False),
            ("asleep", "", "Asleep. A message wakes it."),
            id="not-placed-is-not-a-missing-machine",
        ),
        pytest.param(
            replace(
                SLEPT,
                machine_status="none",
                machine_answers=False,
                prompt_owed=True,
                prompt_at=ago(900),
            ),
            ("waking", "", "Waking the chat."),
            id="not-placed-with-a-message-waits-on-placement",
        ),
        pytest.param(
            replace(HELD, machine_status="stranded", machine_answers=False),
            (
                "unavailable",
                "machine_released",
                "The machine this chat ran on is no longer active. A new machine will be used.",
            ),
            id="stranded",
        ),
        pytest.param(
            replace(
                HELD,
                machine_status="refused",
                refusal_kind="files_unreachable",
            ),
            (
                "waiting",
                "files_unreachable",
                "The machine serving it can't reach the chat's files right now.",
            ),
            id="refusal-never-shows-the-boxes-words",
        ),
        pytest.param(
            replace(
                SLEPT,
                machine_answers=False,
                turn_end_reason="credits_exhausted",
                turn_end_at=ago(600),
                prompt_at=ago(700),
                prompt_owed=True,
            ),
            ("stopped", "credits_exhausted", "Stopped because the organization ran out of credit."),
            id="turn-killed-by-a-credit-drain",
        ),
        pytest.param(
            replace(
                SLEPT,
                machine_answers=False,
                turn_end_reason="machine_stopped",
                turn_end_at=ago(600),
                prompt_at=ago(700),
            ),
            ("stopped", "machine_stopped", "Stopped because lab-b was stopped."),
            id="turn-killed-by-a-stop",
        ),
        pytest.param(
            replace(SLEPT, turn_end_reason="some_newer_reason", turn_end_at=ago(60)),
            ("stopped", "", "The last turn was stopped before it finished."),
            id="an-ending-this-build-has-no-words-for",
        ),
        pytest.param(
            replace(
                SLEPT,
                turn_end_reason="credits_exhausted",
                turn_end_at=ago(600),
                prompt_at=ago(20),
                prompt_owed=True,
            ),
            ("waking", "", "Waking the chat."),
            id="a-message-sent-after-the-ending-is-owed-again",
        ),
        pytest.param(
            replace(HELD, has_run=False),
            ("awake", "", "Ready for a message."),
            id="a-new-chat-a-box-holds",
        ),
        pytest.param(replace(SLEPT, has_run=False), None, id="a-chat-that-never-ran"),
    ],
)
def test_chat_status(evidence: ChatEvidence, expected: tuple[str, str, str] | None) -> None:
    assert read(evidence) == expected


def test_since_is_the_evidence_that_decided_the_state() -> None:
    stamp, sent, slot, ended = ago(300), ago(900), ago(50), ago(40)
    cases = {
        "stalled-turn": (replace(HELD, turn_working=True, turn_stamped_at=stamp), stamp),
        "stalled-start": (replace(HELD, prompt_owed=True, prompt_at=sent), sent),
        "queued": (
            replace(SLEPT, prompt_owed=True, prompt_at=sent, slot_wait_at=slot),
            slot,
        ),
        "stopped": (replace(SLEPT, turn_end_reason="box_lost", turn_end_at=ended), ended),
        "awake": (HELD, None),
    }
    got = {
        name: chat_status(evidence, now=NOW, bounds=BOUNDS).since  # type: ignore[union-attr]
        for name, (evidence, _since) in cases.items()
    }
    assert got == {name: since for name, (_evidence, since) in cases.items()}


def test_recheck_is_when_time_alone_would_change_the_status() -> None:
    """A client reads again at ``recheck_at`` and needs to know no bound: a
    working turn is due when its stamp would go quiet, a wait when it would
    read stalled, and a status only an event can change names no time."""
    stamp, sent, wake = ago(10), ago(30), ago(20)
    cases = {
        "working": (
            replace(HELD, turn_working=True, turn_stamped_at=stamp),
            stamp + BOUNDS.turn_silence,
        ),
        "working-unstamped": (replace(HELD, turn_working=True), None),
        "message-waiting": (
            replace(HELD, prompt_owed=True, prompt_at=sent),
            sent + BOUNDS.turn_start,
        ),
        "wake-waiting": (replace(SLEPT, wake_requested_at=wake), wake + BOUNDS.turn_start),
        "stalled": (replace(HELD, turn_working=True, turn_stamped_at=ago(500)), None),
        "awake": (HELD, None),
        "asleep": (SLEPT, None),
    }
    got = {
        name: chat_status(evidence, now=NOW, bounds=BOUNDS).recheck_at  # type: ignore[union-attr]
        for name, (evidence, _due) in cases.items()
    }
    assert got == {name: due for name, (_evidence, due) in cases.items()}
    # Reading again at that moment gives the stalled status it promised.
    due = cases["working"][1]
    assert due is not None
    later = chat_status(cases["working"][0], now=due, bounds=BOUNDS)
    assert later is not None and later.state == "stalled"


@pytest.mark.parametrize(
    ("kind", "read"),
    [
        pytest.param(None, ("waiting", "not_yet"), id="none-is-a-wait"),
        pytest.param("", ("waiting", "not_yet"), id="empty-is-a-wait"),
        pytest.param("from_a_newer_box", ("waiting", "not_yet"), id="unknown-is-a-wait"),
        pytest.param("files_unreachable", ("waiting", "files_unreachable"), id="files"),
        pytest.param("workspace_elsewhere", ("waiting", "workspace_elsewhere"), id="held"),
        pytest.param("transcript_unopened", ("waiting", "not_opened"), id="not-opened"),
        pytest.param("start_failed", ("waiting", "not_started"), id="not-started"),
        pytest.param("resume_failed", ("waiting", "not_resumed"), id="not-resumed"),
        pytest.param("moving", ("unavailable", "refused_moving"), id="moving"),
        pytest.param("not_allowed", ("unavailable", "refused"), id="not-allowed"),
        pytest.param("sandbox_refused", ("unavailable", "refused"), id="sandbox"),
        pytest.param("workspace_unservable", ("unavailable", "refused"), id="unservable"),
        pytest.param("folder_gone", ("unavailable", "refused_folder_gone"), id="folder-gone"),
        pytest.param("files_unsaved", ("unavailable", "refused_save"), id="save-failed"),
        # The box's sentence is never read for a kind, however it is worded.
        pytest.param(
            "this chat's folder could not be taken", ("waiting", "not_yet"), id="a-sentence"
        ),
    ],
)
def test_a_refusal_is_read_for_its_kind(kind: str | None, read: tuple[str, str]) -> None:
    assert refusal_read(kind) == read


@pytest.mark.parametrize("kind", sorted(CHAT_REFUSAL_KINDS))
def test_every_refusal_kind_reads_as_a_reason_its_state_defines(kind: str) -> None:
    state, reason = refusal_read(kind)
    assert reason in CHAT_STATUS.states[state].reasons
    # Only a verdict reads as one: a wait never says the chat can't run.
    assert (state == "unavailable") is refusal_is_final(kind)


@pytest.mark.parametrize(
    ("machine_status", "name", "expected"),
    [
        pytest.param("ready", "lab-b", None, id="ready"),
        pytest.param("pool", "", None, id="a-shared-machine-will-take-it"),
        pytest.param("draining", "lab-b", None, id="draining-places-elsewhere"),
        pytest.param(
            "none",
            "",
            ("unavailable", "no_machine", "No machine can serve your organization right now."),
            id="nothing-would-take-it",
        ),
        pytest.param(
            "unreachable",
            "lab-b",
            ("unavailable", "machine_unreachable", "lab-b isn't responding."),
            id="unreachable",
        ),
        pytest.param(
            "starting",
            "",
            (
                "waking",
                "machine_starting",
                "The machine is starting. Your message is sent as soon as it is ready.",
            ),
            id="starting-unnamed",
        ),
        pytest.param(
            "asleep",
            "lab-b",
            (
                "waking",
                "machine_starting",
                "lab-b is starting. Your message is sent as soon as it is ready.",
            ),
            id="a-stopped-machine-the-message-starts",
        ),
    ],
)
def test_placement_status(
    machine_status: str, name: str, expected: tuple[str, str, str] | None
) -> None:
    fact = placement_status(machine_status, machine_name=name)
    assert (None if fact is None else (fact.state, fact.reason_code, fact.sentence)) == expected


def test_every_combination_of_evidence_reads_as_a_registered_state() -> None:
    """The builder is total: whatever the rows hold, it answers with a state
    of the vocabulary (or nothing, for a chat that never ran) and never
    raises on a sentence it cannot fill."""
    statuses = (
        "starting",
        "ready",
        "draining",
        "unreachable",
        "none",
        "refused",
        "asleep",
        "stranded",
    )
    moments = (None, ago(5), ago(3600))
    emitted: set[str] = set()
    for (
        status,
        answers,
        held,
        working,
        stamp,
        owed,
        prompt,
        wake,
        slot,
        ended,
        ran,
        name,
    ) in product(
        statuses,
        (False, True),
        (False, True),
        (False, True),
        moments,
        (False, True),
        moments,
        moments,
        (None, ago(5)),
        (None, "credits_exhausted", "unheard_of"),
        (False, True),
        ("", "lab-b"),
    ):
        fact = chat_status(
            ChatEvidence(
                machine_status=status,
                machine_answers=answers,
                machine_name=name,
                refusal_kind=("not_allowed" if held else "files_unreachable")
                if status == "refused"
                else None,
                session_held=held,
                wake_requested_at=wake,
                slot_wait_at=slot,
                turn_working=working,
                turn_stamped_at=stamp,
                prompt_at=prompt,
                prompt_owed=owed,
                turn_end_reason=ended,
                turn_end_at=ago(600) if ended else None,
                has_run=ran,
                # A chat that ran was held by a box; one that never ran may not have been.
                ever_held=ran,
            ),
            now=NOW,
            bounds=BOUNDS,
        )
        if fact is None:
            assert not ran
            continue
        assert fact.state in CHAT_STATUS.states
        emitted.add(fact.state)
        if fact.state == "working":
            # Never on placement alone: a turn is running, a box that answers
            # holds the chat, and its stamp is fresh (or it sends none).
            assert working and held and status in ("ready", "draining")
            assert stamp is None or NOW - stamp < BOUNDS.turn_silence
    assert emitted == set(CHAT_STATUS.states), "every state is reachable"


#: A new chat: a message sent, nothing ever written back, and no box has
#: ever held its session.
NEW = ChatEvidence(
    machine_status="ready",
    machine_answers=True,
    machine_name="lab-b",
    prompt_at=ago(5),
    prompt_owed=True,
    ever_held=False,
)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param(NEW, ("starting", "", "Starting the chat."), id="new-chat-on-a-ready-box"),
        pytest.param(
            replace(NEW, machine_status="starting", machine_answers=False),
            (
                "starting",
                "machine_starting",
                "lab-b is starting. Your message is sent as soon as it is ready.",
            ),
            id="new-chat-while-its-box-starts",
        ),
        pytest.param(
            replace(NEW, machine_status="none", machine_answers=False),
            ("starting", "", "Starting the chat."),
            id="new-chat-not-placed-yet",
        ),
        pytest.param(
            replace(NEW, ever_held=True),
            ("waking", "", "Waking the chat."),
            id="a-chat-a-box-has-held-is-woken-not-started",
        ),
        pytest.param(
            replace(NEW, prompt_at=ago(180)),
            ("stalled", "turn_not_started", "lab-b has the message and hasn't started on it."),
            id="a-new-chat-a-ready-box-sits-on-still-stalls",
        ),
    ],
)
def test_a_chat_no_box_has_held_reads_starting(
    evidence: ChatEvidence, expected: tuple[str, str, str]
) -> None:
    assert read(evidence) == expected
