"""A workspace's status reads its move and its file sync before where it was placed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from alkera_core.status import (
    WORKSPACE_STATUS,
    WorkspaceBounds,
    WorkspaceEvidence,
    workspace_status,
)

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
BOUNDS = WorkspaceBounds(turn_silence=timedelta(seconds=120), sync_pause=timedelta(seconds=60))


def ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


#: A workspace a ready box holds, its folder lease beating.
AWAKE = WorkspaceEvidence(
    chat_count=2,
    machine_state="ready",
    machine_name="lab-b",
    awake=True,
    lease_held=True,
    lease_beat_at=ago(5),
    lease_machine_name="lab-b",
)
ASLEEP = WorkspaceEvidence(chat_count=2, machine_state="ready", machine_name="lab-b")


def read(evidence: WorkspaceEvidence) -> tuple[str, str, str] | None:
    fact = workspace_status(evidence, now=NOW, bounds=BOUNDS)
    return None if fact is None else (fact.state, fact.reason_code, fact.sentence)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param(AWAKE, ("awake", "", "Awake on lab-b."), id="awake-and-syncing"),
        pytest.param(ASLEEP, ("asleep", "", "Asleep. A message wakes it."), id="asleep"),
        pytest.param(
            replace(AWAKE, lease_beat_at=ago(59)),
            ("awake", "", "Awake on lab-b."),
            id="lease-beat-just-inside-the-bound",
        ),
        pytest.param(
            replace(AWAKE, lease_beat_at=ago(60)),
            (
                "sync_paused",
                "worker_silent",
                "lab-b has stopped syncing this workspace's files. "
                "The files here are the last copy it saved.",
            ),
            id="box-beats-but-its-worker-stopped-syncing",
        ),
        pytest.param(
            replace(AWAKE, lease_beat_at=ago(60), working_stamps=(ago(5),)),
            (
                "sync_paused",
                "worker_silent",
                "lab-b has stopped syncing this workspace's files. "
                "The files here are the last copy it saved.",
            ),
            id="a-running-turn-does-not-hide-a-paused-sync",
        ),
        pytest.param(
            replace(AWAKE, machine_state="unreachable", lease_beat_at=ago(90)),
            (
                "sync_paused",
                "machine_unreachable",
                "lab-b isn't responding. The files here are the last copy it saved.",
            ),
            id="holder-unreachable",
        ),
        pytest.param(
            replace(ASLEEP, machine_state="unreachable"),
            ("unavailable", "machine_unreachable", "lab-b isn't responding."),
            id="unreachable-with-no-lease",
        ),
        pytest.param(
            replace(AWAKE, working_stamps=(ago(400), ago(10))),
            ("working", "", "An agent is working in this workspace."),
            id="one-turn-still-reporting",
        ),
        pytest.param(
            replace(AWAKE, working_stamps=(ago(400), ago(120))),
            ("stalled", "turn_silent", "An agent here has stopped reporting progress."),
            id="every-running-turn-went-quiet",
        ),
        pytest.param(
            replace(ASLEEP, wake_requested_at=ago(10)),
            ("waking", "", "Waking the workspace."),
            id="wake-asked",
        ),
        pytest.param(
            replace(ASLEEP, machine_state="asleep", wake_requested_at=ago(10)),
            ("waking", "machine_starting", "lab-b is starting."),
            id="wake-on-a-stopped-machine",
        ),
        pytest.param(
            replace(ASLEEP, machine_state="none", wake_requested_at=ago(10)),
            (
                "unavailable",
                "machine_released",
                "The machine this workspace ran on is no longer active. "
                "A new machine will be used.",
            ),
            id="wake-on-a-released-machine",
        ),
        pytest.param(
            replace(AWAKE, move_state="draining", move_target="lab-a"),
            ("moving", "saving_chats", "Moving to lab-a. Its chats are being saved first."),
            id="move-draining-outranks-awake",
        ),
        pytest.param(
            replace(AWAKE, move_state="requested", move_target="lab-a"),
            ("moving", "saving_chats", "Moving to lab-a. Its chats are being saved first."),
            id="move-requested",
        ),
        pytest.param(
            replace(ASLEEP, move_state="switching", move_target="lab-a"),
            ("moving", "starting_target", "Moving to lab-a, which is starting."),
            id="move-switching",
        ),
        pytest.param(
            replace(ASLEEP, move_state="waking", move_target=""),
            (
                "moving",
                "waking_on_target",
                "Moving to another machine. The workspace is waking there.",
            ),
            id="move-waking-to-an-unnamed-target",
        ),
        pytest.param(
            replace(AWAKE, move_state="done", move_target="lab-a"),
            ("awake", "", "Awake on lab-b."),
            id="a-finished-move-says-nothing",
        ),
        pytest.param(WorkspaceEvidence(), None, id="nothing-happened-yet"),
        pytest.param(
            WorkspaceEvidence(chat_count=1),
            ("asleep", "", "Asleep. A message wakes it."),
            id="chats-on-no-one-machine",
        ),
    ],
)
def test_workspace_status(
    evidence: WorkspaceEvidence, expected: tuple[str, str, str] | None
) -> None:
    assert read(evidence) == expected


def test_since_is_the_evidence_that_decided_the_state() -> None:
    beat, moved = ago(300), ago(45)
    paused = workspace_status(replace(AWAKE, lease_beat_at=beat), now=NOW, bounds=BOUNDS)
    moving = workspace_status(
        replace(AWAKE, move_state="waking", move_target="lab-a", move_since=moved),
        now=NOW,
        bounds=BOUNDS,
    )
    stalled = workspace_status(
        replace(AWAKE, working_stamps=(ago(500), ago(200))), now=NOW, bounds=BOUNDS
    )
    assert paused is not None and paused.since == beat
    assert moving is not None and moving.since == moved
    # The latest of the quiet stamps: when the last of them stopped.
    assert stalled is not None and stalled.since == ago(200)


def test_recheck_is_when_time_alone_would_change_the_status() -> None:
    beat = ago(5)
    awake = workspace_status(replace(AWAKE, lease_beat_at=beat), now=NOW, bounds=BOUNDS)
    working = workspace_status(
        replace(AWAKE, working_stamps=(ago(400), ago(10))), now=NOW, bounds=BOUNDS
    )
    asleep = workspace_status(ASLEEP, now=NOW, bounds=BOUNDS)
    assert awake is not None and awake.recheck_at == beat + BOUNDS.sync_pause
    assert working is not None and working.recheck_at == ago(10) + BOUNDS.turn_silence
    assert asleep is not None and asleep.recheck_at is None
    # Reading again at that moment gives the paused sync it promised.
    later = workspace_status(
        replace(AWAKE, lease_beat_at=beat), now=beat + BOUNDS.sync_pause, bounds=BOUNDS
    )
    assert later is not None and later.state == "sync_paused"


def test_every_combination_of_evidence_reads_as_a_registered_state() -> None:
    emitted: set[str] = set()
    for count, machine, awake, wake, stamps, move, held, beat, name in product(
        (0, 3),
        ("", "ready", "starting", "draining", "restarting", "unreachable", "asleep", "none"),
        (False, True),
        (None, ago(5)),
        ((), (ago(5),), (ago(900),), (None,)),
        (None, "requested", "draining", "switching", "waking", "done", "failed", "canceled"),
        (False, True),
        (None, ago(5), ago(900)),
        ("", "lab-b"),
    ):
        fact = workspace_status(
            WorkspaceEvidence(
                chat_count=count,
                machine_state=machine,
                machine_name=name,
                awake=awake,
                wake_requested_at=wake,
                working_stamps=stamps,
                move_state=move,
                move_target=name,
                lease_held=held,
                lease_beat_at=beat,
                lease_machine_name=name,
            ),
            now=NOW,
            bounds=BOUNDS,
        )
        if fact is None:
            assert count == 0 and not held
            continue
        assert fact.state in WORKSPACE_STATUS.states
        emitted.add(fact.state)
        if fact.state in ("awake", "working"):
            # Never while a held folder's box has stopped beating.
            assert not (held and beat is not None and NOW - beat >= BOUNDS.sync_pause)
            assert machine in ("ready", "draining", "restarting")
    assert emitted == set(WORKSPACE_STATUS.states), "every state is reachable"
