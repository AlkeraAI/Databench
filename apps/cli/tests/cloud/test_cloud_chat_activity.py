"""The four activities a held chat can be in, and what each one decides.

The states are wire strings in the daemon status file, which the roll scripts
read from daemons of every age, so their spelling is pinned here.
"""

from __future__ import annotations

import pytest
from alkera_cli.cloud.activity import ChatActivity


@pytest.mark.parametrize(
    ("activity", "value", "holds_a_stop", "owes_anything"),
    [
        pytest.param(ChatActivity.WORKING, "working", True, True, id="working"),
        pytest.param(ChatActivity.RUNNING_JOB, "running_job", True, True, id="running-job"),
        pytest.param(ChatActivity.AWAITING_USER, "awaiting_user", False, True, id="awaiting-user"),
        pytest.param(ChatActivity.IDLE, "idle", False, False, id="idle"),
    ],
)
def test_each_activity_decides_the_stop_and_the_busy_count(
    activity: ChatActivity, value: str, holds_a_stop: bool, owes_anything: bool
) -> None:
    assert activity.value == value
    assert activity.holds_a_stop is holds_a_stop
    assert activity.owes_anything is owes_anything


def test_there_are_exactly_four_activities() -> None:
    """A new state must be a deliberate change here, next to what it decides."""
    assert {a.value for a in ChatActivity} == {"working", "running_job", "awaiting_user", "idle"}


def test_a_chat_on_a_job_is_counted_working_in_the_status_file() -> None:
    """``node-upgrade.sh`` reads how long a drain will wait from
    ``chats_working``: a chat on a background job is among them."""
    from alkera_cli.cloud.activity import activity_counts

    held = [ChatActivity.RUNNING_JOB, ChatActivity.WORKING, ChatActivity.IDLE]
    counts = activity_counts(held)
    assert counts == {
        "chats_working": 2,
        "chats_awaiting_user": 0,
        "chats_idle": 1,
        "chats_busy": 2,
    }
