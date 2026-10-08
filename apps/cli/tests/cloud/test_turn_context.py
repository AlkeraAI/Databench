"""The hidden context a turn carries beside the person's words."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_cli.cloud.turn_context import hidden_turn


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        pytest.param(("machine", "brief"), {"context": "machine\n\nbrief"}, id="joined-in-order"),
        pytest.param(
            (None, "brief", "  \n", "", "missing"),
            {"context": "brief\n\nmissing"},
            id="blank-blocks-dropped",
        ),
        pytest.param((None, " ", ""), {}, id="all-blank-sends-no-context"),
        pytest.param((), {}, id="nothing"),
    ],
)
def test_hidden_turn(blocks: tuple[str | None, ...], expected: dict[str, Any]) -> None:
    assert hidden_turn(*blocks) == expected
