"""The agent assertion headers, built and parsed in exactly one place.

An agent running inside a user's chat session calls the backend with the
user's own JWT plus two headers naming itself::

    X-Alkera-Actor: agent
    X-Alkera-Agent-Id: <chat session id>

The backend records the call as ``[user, agent]``. The id is client-asserted
(the audit record says so); the user behind it is always server-authenticated.
Nothing else in the codebase spells these header names: the CLI builds them
with :func:`agent_headers`, the backend reads them with
:func:`parse_agent_assertion`. Every malformed combination is an error rather
than a silent downgrade to "just the user" — a half-present assertion is a
client bug or a forgery attempt, and neither should pass as a plain session.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ACTOR_HEADER = "X-Alkera-Actor"
AGENT_ID_HEADER = "X-Alkera-Agent-Id"
ACTOR_AGENT = "agent"

#: A session id: starts alphanumeric, then up to 127 of ``[A-Za-z0-9._:-]``.
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentHeaderError(ValueError):
    """The agent headers are present but not a well-formed assertion."""


@dataclass(frozen=True, slots=True)
class AgentAssertion:
    session_id: str


def agent_headers(session_id: str) -> dict[str, str]:
    """The two headers an agent-originated request carries.

    Raises :class:`AgentHeaderError` when ``session_id`` would not parse on the
    other side, so a bad id fails at the client instead of as a 400.
    """
    if not AGENT_ID_PATTERN.fullmatch(session_id):
        raise AgentHeaderError(f"agent id {session_id!r} does not match {AGENT_ID_PATTERN.pattern}")
    return {ACTOR_HEADER: ACTOR_AGENT, AGENT_ID_HEADER: session_id}


def _single(headers: Mapping[str, str], name: str) -> str | None:
    """The one value of header ``name`` (case-insensitive), ``None`` when the
    header is absent. Two occurrences with different values are refused: a
    request that says two things about who is acting says nothing trustworthy."""
    wanted = name.lower()
    values = {value for key, value in headers.items() if key.lower() == wanted}
    if not values:
        return None
    if len(values) > 1:
        raise AgentHeaderError(f"{name} appears more than once with different values")
    return values.pop()


def parse_agent_assertion(headers: Mapping[str, str]) -> AgentAssertion | None:
    """Read the agent assertion out of request headers.

    ``None`` when neither header is present. :class:`AgentHeaderError` when the
    actor header is anything but ``agent``, when either header appears without
    the other, or when the id fails :data:`AGENT_ID_PATTERN`.
    """
    actor = _single(headers, ACTOR_HEADER)
    agent_id = _single(headers, AGENT_ID_HEADER)
    if actor is None and agent_id is None:
        return None
    if actor is None:
        raise AgentHeaderError(f"{AGENT_ID_HEADER} requires {ACTOR_HEADER}: {ACTOR_AGENT}")
    if actor != ACTOR_AGENT:
        raise AgentHeaderError(f"{ACTOR_HEADER} must be {ACTOR_AGENT!r}, got {actor!r}")
    if agent_id is None:
        raise AgentHeaderError(f"{ACTOR_HEADER}: {ACTOR_AGENT} requires {AGENT_ID_HEADER}")
    if not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise AgentHeaderError(f"{AGENT_ID_HEADER} {agent_id!r} is not a valid agent id")
    return AgentAssertion(session_id=agent_id)


__all__ = [
    "ACTOR_AGENT",
    "ACTOR_HEADER",
    "AGENT_ID_HEADER",
    "AGENT_ID_PATTERN",
    "AgentAssertion",
    "AgentHeaderError",
    "agent_headers",
    "parse_agent_assertion",
]
