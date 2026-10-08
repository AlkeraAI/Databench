"""The open CLI's composition: the extensions an open install runs with.

The open platform ships one data connector, generic SQL. A team's connections
reach a workspace on the cloud-sync connections lane, a box serves them to the
chats it holds, and a box notebook's SQL resolves a workspace's connections from
the server record. An open entry point calls
:func:`install` before it hands off to the CLI (``alkera_cli.main``), the way a
distribution's entry calls its own installer. The two are not alternatives: a
distribution installs :data:`OPEN_PLATFORM` first, then its own data plugins,
tools and box data plane.
"""

from __future__ import annotations

from alkera_core.extensions import Extension, install_extensions

from alkera_cli.cloud_sync import extension as cloud_sync
from alkera_cli.notebooks import actor_names, connections_extension
from alkera_cli.plugins.generic_sql import extension as generic_sql
from alkera_cli.plugins.plugin_base import harness_records

#: The open platform the product installs too, first: the notebook actors named
#: after the brand, a box notebook's SQL over the workspace's connections, the
#: connection record and spend the harness reads, and the connections lane.
OPEN_PLATFORM: tuple[Extension, ...] = (
    actor_names.EXTENSION,
    connections_extension.EXTENSION,
    harness_records.EXTENSION,
    cloud_sync.CONNECTIONS_SYNC,
)

#: Every extension the open CLI installs, in installation order: the platform,
#: plus the generic SQL connector and the box's data plane, which the product
#: replaces with its own plugins and schema cards.
OPEN_EXTENSIONS: tuple[Extension, ...] = (
    generic_sql.EXTENSION,
    *OPEN_PLATFORM,
    cloud_sync.BOX_TEAM_CONNECTIONS,
)


def install() -> None:
    """Install the open extensions. Idempotent."""
    install_extensions(OPEN_EXTENSIONS)


__all__ = ["OPEN_EXTENSIONS", "OPEN_PLATFORM", "install"]
