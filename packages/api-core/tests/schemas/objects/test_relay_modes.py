"""The stance relay carries every stance the HTTP door admits.

A reader switching a chat's stance writes a row AND mints a ``mode`` relay: the
row is what a box reads when it next opens the session, the relay is how a box
running the chat right now hears about it. The relay reads a mode it does not
recognise as ``read_only`` — deliberately, so an unreadable word cannot take a
live socket down and cannot open a session up.

That floor is only safe while the relay recognises exactly what the route
accepts. If the two sets drift, a stance the route takes and records becomes one
the running box silently floors: the chat's row says one thing and the agent
behaves as another, with nothing raised anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.schemas.objects import CHAT_RELAY_ADAPTER, ModeRelay
from alkera_core.schemas.objects.specs import CLOUD_PERMISSION_MODES


def _read_at_the_box(body: dict[str, Any]) -> ModeRelay:
    """Parse a relay body the way the box's socket reader parses one."""
    parsed = CHAT_RELAY_ADAPTER.validate_python(body)
    assert isinstance(parsed, ModeRelay), parsed
    return parsed


@pytest.mark.parametrize("mode", sorted(CLOUD_PERMISSION_MODES))
def test_a_stance_the_route_admits_arrives_at_the_box_as_itself(mode: str) -> None:
    """Both halves of the trip: the route mints the relay from the mode it just
    recorded, and the box reads that body back off the channel."""
    minted = ModeRelay(mode=mode, user_id="u-1").model_dump(mode="json")  # type: ignore[arg-type]

    assert minted["mode"] == mode, "the switch was floored on the way out"
    assert _read_at_the_box(minted).mode == mode, "the switch was floored on the way in"


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("accept_edits", id="a retired word no harness runs"),
        pytest.param("AUTO", id="a stance spelled in another case"),
        pytest.param("sideways", id="not a stance at all"),
        pytest.param("", id="no stance named"),
        pytest.param(3, id="not a word at all"),
    ],
)
def test_a_body_outside_the_set_lands_on_the_floor_rather_than_raising(mode: Any) -> None:
    """What arrives here was written by another process — possibly a newer one.
    The direction of the fallback matters as much as the fallback: an unreadable
    mode becomes the session that asks about everything, never the one that asks
    about nothing."""
    body = {"kind": "mode", "mode": mode, "user_id": "u-1"}

    assert _read_at_the_box(body).mode == "read_only"
