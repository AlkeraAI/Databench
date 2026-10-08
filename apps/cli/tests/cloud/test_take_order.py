"""The order a box takes its listed chats, and which ones it may leave asleep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_cli.cloud.take_order import in_order_of_need, left_asleep

pytestmark = [pytest.mark.spread]

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize(
    ("row", "asleep"),
    [
        pytest.param({"machine_status": "asleep"}, True, id="asleep-and-nobody-came"),
        pytest.param({"machine_status": "ready"}, False, id="awake"),
        pytest.param({}, False, id="no-status"),
        pytest.param(
            {"machine_status": "asleep", "wake_requested_at": _ago(5)},
            False,
            id="a-reader-opened-it",
        ),
        pytest.param(
            {"machine_status": "asleep", "pending_turn": True}, False, id="a-message-waits"
        ),
        pytest.param(
            {"machine_status": "asleep", "wake_requested_at": None, "pending_turn": False},
            True,
            id="cleared-fields-are-nobody",
        ),
    ],
)
def test_a_chat_is_left_asleep_only_when_nothing_came_for_it(
    row: dict[str, Any], asleep: bool
) -> None:
    assert left_asleep(row) is asleep


def test_chats_are_taken_owed_first_then_recent_then_the_rest_in_listing_order() -> None:
    chats: dict[str, dict[str, Any]] = {
        "idle-a": {"last_activity_at": _ago(7200)},
        "recent": {"last_activity_at": _ago(60)},
        "unstamped": {},
        "waiting": {"last_activity_at": _ago(9000), "pending_turn": True},
        "idle-b": {"last_activity_at": "not a time"},
        "opened": {"wake_requested_at": _ago(1)},
    }
    order = [chat_id for chat_id, _row in in_order_of_need(chats, now=NOW)]
    assert order == ["waiting", "opened", "recent", "idle-a", "unstamped", "idle-b"]
