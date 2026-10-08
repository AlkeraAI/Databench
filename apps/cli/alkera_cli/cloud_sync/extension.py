"""The open platform's team connection sync, installed as extensions.

:data:`CONNECTIONS_SYNC` puts the connections lane on the cloud-sync job: pull
the authoritative set of team connections and reconcile it into the workspace,
due on every open so a membership change lands within minutes. Both composition
roots install it.

:data:`BOX_TEAM_CONNECTIONS` makes a box serve those connections to the chats it
holds, through the box's data plane. The open composition installs it; the
product installs its own data plane, which also writes schema cards.
"""

from __future__ import annotations

from alkera_core.extensions import Extension

from alkera_cli.cloud.box_data import BOX_DATA
from alkera_cli.cloud_sync import connections_lane
from alkera_cli.cloud_sync.box_sync import TEAM_CONNECTIONS_DATA
from alkera_cli.cloud_sync.job import CLOUD_SYNC_LANES, LaneRun, SyncLane

CONNECTIONS_LANE = "connections"


async def _connections(run: LaneRun) -> None:
    await connections_lane.run_connections_lane(run.project, announce=run.announce)


CONNECTIONS = SyncLane(
    name=CONNECTIONS_LANE,
    cadence_seconds=900.0,
    due_on_open=True,
    run=_connections,
    after_sign_in=True,
)


def _install_lane() -> None:
    CLOUD_SYNC_LANES.register(CONNECTIONS)


def _install_box() -> None:
    BOX_DATA.register(TEAM_CONNECTIONS_DATA)


CONNECTIONS_SYNC = Extension(name="alkera.team-connections-sync", install=_install_lane)
BOX_TEAM_CONNECTIONS = Extension(name="alkera.box-team-connections", install=_install_box)

__all__ = ["BOX_TEAM_CONNECTIONS", "CONNECTIONS", "CONNECTIONS_LANE", "CONNECTIONS_SYNC"]
