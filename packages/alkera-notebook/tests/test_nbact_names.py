"""How the notebook package names the system and an agent: its own words when
it runs alone, a platform's when one hands its names in."""

from __future__ import annotations

import pytest
from alkera_notebook import actors
from alkera_notebook.actors import ActingFor, ActorNames, actor_label
from alkera_notebook.engine import system_actor

ADA = ActingFor(id="user:1", display_name="Ada King")


@pytest.mark.parametrize(
    ("kind", "display_name", "acting_for", "expected"),
    [
        pytest.param("system", "", None, "Databench", id="system"),
        pytest.param("agent", "", ADA, "Agent for Ada King", id="agent-for-a-person"),
        pytest.param("agent", "", None, "Agent", id="agent-for-nobody"),
        pytest.param("agent", "Scout", None, "Scout", id="agent-with-its-own-name"),
        pytest.param("person", "", None, "A former member", id="unnamed-person"),
    ],
)
def test_standalone_labels(
    kind: str, display_name: str, acting_for: ActingFor | None, expected: str
) -> None:
    label = actor_label(kind, display_name, acting_for)
    assert label == expected
    assert "Alkera" not in label


def test_the_system_actor_is_named_standalone() -> None:
    actor = system_actor()
    assert (actor.display_name, actor.label()) == ("Databench", "Databench")


def test_handed_in_names_are_read_when_a_label_is_built() -> None:
    current = ActorNames(system="Acme", agent="Acme agent")
    actors.name_actors_with(lambda: current)
    assert actor_label("system", "") == "Acme"
    assert actor_label("agent", "", ADA) == "Acme agent for Ada King"
    assert system_actor().display_name == "Acme"
    # A brand that changes after the hand-in still applies.
    current = ActorNames(system="Other", agent="Helper")
    assert actor_label("agent", "", ADA) == "Helper for Ada King"


def test_handing_in_returns_the_source_it_replaces() -> None:
    first = actors.name_actors_with(lambda: ActorNames(system="A", agent="B"))
    second = actors.name_actors_with(first)
    assert second() == ActorNames(system="A", agent="B")
    assert actors.actor_names() == actors.OPEN_ACTOR_NAMES
