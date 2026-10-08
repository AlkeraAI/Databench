"""A member's actions on a team's preconfigured connections: a port.

The runtime offers a member five actions on a team connection row (accept,
complete with their own sign-in, mute, remove, dismiss), and every surface
(the daemon, the TUI, a chat tool) reaches them through the runtime. What a row
is and how each action is carried out belongs to the distribution that syncs
team connections, so the runtime asks :func:`team_connection_actions` for the
one implementation registered into :data:`TEAM_CONNECTION_ACTIONS`. With none
registered there is no team row to act on: an accept or sign-in is refused
with a message, and a mute, remove or dismiss changes nothing.

The registry and the connection an action returns are the runtime's own
objects; this port passes them through without looking at them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory


class TeamConnectionActionError(Exception):
    """A member action that can't proceed; the message is user-facing."""


#: The refusal of an accept or sign-in when this build syncs no team connections.
NO_TEAM_CONNECTIONS = "This build has no team connections."


class TeamConnectionActions(Protocol):
    """How a member acts on a team connection row.

    ``registry`` is the runtime's plugin registry; the two async actions return
    the connection that went live. Each raises :class:`TeamConnectionActionError`
    with the user-facing reason when it cannot proceed. The three sync actions
    answer whether anything changed."""

    async def accept(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        member_values: dict[str, str] | None = None,
    ) -> Any: ...

    async def authorize(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        open_browser: Callable[[str], None] | None = None,
    ) -> Any: ...

    def set_muted(self, project: ProjectDirectory, record_id: str, muted: bool) -> bool: ...

    def remove(self, project: ProjectDirectory, record_id: str) -> bool: ...

    def dismiss(self, project: ProjectDirectory, record_id: str) -> bool: ...


class NoTeamConnections:
    """The open platform's answer: there is no team row to act on."""

    async def accept(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        member_values: dict[str, str] | None = None,
    ) -> Any:
        raise TeamConnectionActionError(NO_TEAM_CONNECTIONS)

    async def authorize(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        open_browser: Callable[[str], None] | None = None,
    ) -> Any:
        raise TeamConnectionActionError(NO_TEAM_CONNECTIONS)

    def set_muted(self, project: ProjectDirectory, record_id: str, muted: bool) -> bool:
        return False

    def remove(self, project: ProjectDirectory, record_id: str) -> bool:
        return False

    def dismiss(self, project: ProjectDirectory, record_id: str) -> bool:
        return False


NO_TEAM_CONNECTION_ACTIONS = NoTeamConnections()

#: The one implementation of a member's team connection actions, if any.
TEAM_CONNECTION_ACTIONS: ExtensionPoint[TeamConnectionActions] = ExtensionPoint(
    "team_connection_actions"
)


def team_connection_actions(
    point: ExtensionPoint[TeamConnectionActions] = TEAM_CONNECTION_ACTIONS,
) -> TeamConnectionActions:
    """The registered implementation, else :data:`NO_TEAM_CONNECTION_ACTIONS`.
    Two implementations would each own half of a row's life, so that is refused."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError("more than one implementation of the team connection actions")
    return registered[0] if registered else NO_TEAM_CONNECTION_ACTIONS


__all__ = [
    "NO_TEAM_CONNECTIONS",
    "NO_TEAM_CONNECTION_ACTIONS",
    "TEAM_CONNECTION_ACTIONS",
    "NoTeamConnections",
    "TeamConnectionActionError",
    "TeamConnectionActions",
    "team_connection_actions",
]
