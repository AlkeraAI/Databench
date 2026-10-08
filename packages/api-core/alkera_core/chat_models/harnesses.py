"""Which model wires each chat agent (harness) can drive mid-session.

A registry, so a new harness registers its row instead of editing the switch
checker, which never names a harness. Shared by the server (cloud chats), the box
and the editor's daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    #: The provider wires the harness can run a model over.
    wires: frozenset[str]
    #: Whether a running session can move onto another model.
    mid_session_switch: bool = True


_REGISTRY: dict[str, HarnessCapabilities] = {}

#: The harness a cloud chat always runs (the chat spec names none).
CLOUD_HARNESS = "opencode"


def register(slug: str, capabilities: HarnessCapabilities) -> None:
    _REGISTRY[slug] = capabilities


def capabilities_of(slug: str | None) -> HarnessCapabilities:
    """The registered row for ``slug``; an unknown harness drives no wire, so a
    chat on it is offered no switch rather than a wrong one."""
    return _REGISTRY.get(slug or CLOUD_HARNESS, HarnessCapabilities(wires=frozenset()))


register("opencode", HarnessCapabilities(wires=frozenset({"anthropic", "openai"})))
# The names the CLI's harness registry files chats under (``harness_type``).
register("agent", HarnessCapabilities(wires=frozenset({"anthropic", "openai"})))
register("claude-agent", HarnessCapabilities(wires=frozenset({"anthropic"})))


__all__ = ["CLOUD_HARNESS", "HarnessCapabilities", "capabilities_of", "register"]
