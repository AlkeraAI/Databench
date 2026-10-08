"""Who acts in a notebook, and how each one is named.

Pure shapes shared by the engine (``alkera_notebook.engine``) and the agent
tools, which never import the engine. A person is named by their display
name, an agent acting for a person as ``<agent> for <name>``, the system by the
product's name. Nothing here ever names an actor by an id.

What the system and an unnamed agent are called comes from :func:`actor_names`.
Standalone it answers :data:`OPEN_ACTOR_NAMES`; a platform that embeds the
notebooks hands its brand in once through :func:`name_actors_with`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

ActorKind = Literal["person", "agent", "system"]


@dataclass(frozen=True, slots=True)
class ActorNames:
    """What the system and an agent are called wherever an actor is named."""

    system: str
    agent: str


#: The names with nothing handed in.
OPEN_ACTOR_NAMES = ActorNames(system="Databench", agent="Agent")
#: A person whose name is not known. The platform resolves a name for every
#: person it knows (a removed member's last known one), so this is a fallback.
UNKNOWN_PERSON_LABEL = "A former member"


def _open_names() -> ActorNames:
    return OPEN_ACTOR_NAMES


_source: Callable[[], ActorNames] = _open_names


def name_actors_with(source: Callable[[], ActorNames]) -> Callable[[], ActorNames]:
    """Name the system and agents through ``source``, read each time a label is
    built, so a brand registered after this call still applies. Returns the
    source it replaces."""
    global _source
    previous, _source = _source, source
    return previous


def actor_names() -> ActorNames:
    """The names in force."""
    return _source()


class ActingFor(BaseModel):
    """The person an agent acts for."""

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str


def actor_label(kind: str, display_name: str, acting_for: ActingFor | None = None) -> str:
    """How an actor is named to people and models."""
    names = actor_names()
    if kind == "system":
        return names.system
    if kind == "agent":
        person = acting_for.display_name.strip() if acting_for is not None else ""
        if person:
            return f"{names.agent} for {person}"
        return display_name.strip() or names.agent
    return display_name.strip() or UNKNOWN_PERSON_LABEL


__all__ = [
    "OPEN_ACTOR_NAMES",
    "UNKNOWN_PERSON_LABEL",
    "ActingFor",
    "ActorKind",
    "ActorNames",
    "actor_label",
    "actor_names",
    "name_actors_with",
]
