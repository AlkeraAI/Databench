"""How a settled ask and a mode change read, held to the vectors the web's
twin (``packages/chat-model/src/permissionOutcome.test.ts``) is held to."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.permission_presentation.outcome import (
    OUTCOME_VECTORS,
    mode_change,
    resolution_line,
)


@pytest.mark.parametrize(
    "vector",
    [pytest.param(v, id=v["line"]) for v in OUTCOME_VECTORS["resolutions"]],
)
def test_a_settled_ask_reads_the_same_on_every_surface(vector: dict[str, Any]) -> None:
    assert (
        resolution_line(
            vector["optionId"],
            decided_by=vector["decidedBy"],
            decider_name=vector["deciderName"],
            surface=vector["surface"],
        )
        == vector["line"]
    )


@pytest.mark.parametrize(
    "vector",
    [pytest.param(v, id=f"{v['mode']}-{v['surface']}") for v in OUTCOME_VECTORS["modeChanges"]],
)
def test_a_mode_change_reads_the_same_on_every_surface(vector: dict[str, Any]) -> None:
    shown = mode_change(
        vector["mode"],
        previous_mode=vector["previousMode"],
        changer_name=vector["changerName"],
        surface=vector["surface"],
    )
    assert shown.to_json() == vector["presentation"]


@pytest.mark.parametrize(
    ("decided_by", "surface", "name", "wanted"),
    [
        pytest.param("user", "web", "A", "Allowed on web by A", id="person-on-web"),
        pytest.param("user", "slack", "A", "Allowed in Slack by A", id="person-in-slack"),
        # A policy is nobody: a name or a surface on the row must not make it
        # read as a person's decision.
        pytest.param("policy", "web", "A", "Decided by the new mode", id="policy-names-nobody"),
        pytest.param("timeout", "slack", "A", "No answer in time", id="timeout-names-nobody"),
        pytest.param("user", "fax", None, "Allowed", id="unknown-surface-left-out"),
    ],
)
def test_who_and_where_are_said_only_for_a_person(
    decided_by: str, surface: str, name: str | None, wanted: str
) -> None:
    assert (
        resolution_line("allow_once", decided_by=decided_by, decider_name=name, surface=surface)
        == wanted
    )


def test_a_mode_set_to_itself_shows_no_arrow() -> None:
    assert mode_change("auto", previous_mode="auto").change == "Auto"
