"""The extensions an open install's backend runs with.

The open platform serves team and personal connections, so a team that still
holds one must not be deleted: its stored credentials sit on an ``ON DELETE
CASCADE`` key and would go with it. That refusal is registered rather than
imported by the team service, which the connections service itself imports.

An open entry installs :data:`OPEN_EXTENSIONS` before it builds the app
(``backend.app_factory``); the product's composition lists them ahead of
its own.
"""

from __future__ import annotations

from alkera_core.extensions import Extension, install_extensions

from backend.services.connections import team_delete_refusal
from backend.services.org import TEAM_DELETE_BLOCKERS, TeamDeleteBlocker


def _install_team_connections() -> None:
    TEAM_DELETE_BLOCKERS.register(TeamDeleteBlocker("connections", team_delete_refusal))


#: Team connections' part in the open backend: the team-delete refusal.
TEAM_CONNECTIONS = Extension(name="alkera.team-connections", install=_install_team_connections)

#: Every extension the open backend installs, in installation order.
OPEN_EXTENSIONS: tuple[Extension, ...] = (TEAM_CONNECTIONS,)


def install() -> None:
    """Install the open extensions. Idempotent."""
    install_extensions(OPEN_EXTENSIONS)


__all__ = ["OPEN_EXTENSIONS", "TEAM_CONNECTIONS", "install"]
