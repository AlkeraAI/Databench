"""How a box takes on the team connections its owners may use.

A member's laptop pulls the team's connections on the cloud-sync connections
lane and the member accepts each one. A box serves an org and has nobody to
click, so it reads the same records per chat it holds, reconciles them into its
workspace and accepts every one it can assemble.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from alkera_core.auth.machine_token import (
    looks_like_machine_token,
    looks_like_machine_worker_token,
)

from alkera_cli.cloud.box_data import SchemaCards
from alkera_cli.cloud_sync import client as connections_client
from alkera_cli.cloud_sync.connections_lane import apply_reconcile
from alkera_cli.cloud_sync.reconcile import plan_reconcile
from alkera_cli.plugins.plugin_base.connections_store import AddedConnectionsStore
from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionRecord,
    TeamConnectionsStore,
    TeamMemberState,
    open_member_groups,
)

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.cloud_sync.client import ChatConnectionsClient, TeamConnectionsClient

logger = logging.getLogger(__name__)


def accept_shared_connections(store: TeamConnectionsStore) -> list[str]:
    """Mark every team connection the box can assemble as added.

    The admin distributed the whole shape of a shared connection; on a member's
    laptop the member still clicks Add, because it is their workspace. A
    provisioned box serves the org, and nothing on it clicks. A per-user one the
    owner the box acts for has signed in to (the server holds that grant) is
    accepted too, with the owner's grant as its credential: the box leases it
    for one chat or workspace at a time. A connection the admin left a field of
    to the member (an open ask group), a per-user one without the owner's
    grant, a disabled one and one this machine dismissed are left alone.
    Returns the record ids accepted on this pass."""
    accepted: list[str] = []
    with store.locked():
        state = store.load()
        for record_id, record in state.records.items():
            member = state.member.setdefault(record_id, TeamMemberState())
            per_user = record.auth_mode == "per_user"
            if (
                member.added
                or member.dismissed
                or not record.enabled
                or (per_user and record.credential_state != "present")
                or open_member_groups(record, member)
            ):
                continue
            if per_user:
                # The owner's grant, relayed through the box's scoped lease.
                member.oauth_custody = "relay"
                member.authorized = True
            member.added = True
            if not member.local_handle:
                member.local_handle = record.handle
            accepted.append(record_id)
        if accepted:
            store.save(state)
    return accepted


class BoxReconcile(NamedTuple):
    """One reconcile pass on a box: whether a row arrived, moved or left, and
    the ids this box accepted on it."""

    moved: bool
    accepted: list[str]

    @property
    def changed(self) -> bool:
        return self.moved or bool(self.accepted)


async def reconcile_box_connections(
    project: ProjectDirectory,
    records: list[TeamConnectionRecord],
    *,
    connections: TeamConnectionsClient,
) -> BoxReconcile:
    """Apply the server's set to the box's workspace, the way the connections
    lane does on a laptop, then accept every connection the box can assemble."""
    store = TeamConnectionsStore(project.team_connections_path)
    local_pairs = {
        (c.plugin, c.handle) for c in AddedConnectionsStore(project.connections_path).load()
    }
    plan = plan_reconcile(store.load(), records, local_pairs=local_pairs)
    moved: list[str] = []

    def _note(record_id: str, _removed: bool) -> None:
        moved.append(record_id)

    await apply_reconcile(
        plan,
        store,
        plugins_root=project.plugins_path,
        fetch_credential=connections.fetch_credential,
        announce=_note,
    )
    return BoxReconcile(moved=bool(moved), accepted=accept_shared_connections(store))


def box_connections_client(
    *,
    api_url: str,
    token: str,
    chats: Callable[[], list[str]],
    workspaces: Callable[[], list[str]] = list,
    token_source: Callable[[], str] | None = None,
    connections: ChatConnectionsClient | None = None,
) -> TeamConnectionsClient:
    """The client a box reads its team connections through.

    The person's door answers about the caller's own memberships; a box on its
    machine credential is a member of no org and reads instead, per chat it
    holds (``chats``) and per workspace it holds (``workspaces``, whose
    notebook kernels run on it), the connections that owner may use. Either
    client is installed as the process's: on a machine credential a plugin
    leasing a credential meets the chat-scoped door rather than a login this
    box must never read, and a box's notebooks read their workspace's
    connections through the same client its sync reads.

    ``connections`` is that chat-scoped client when the caller already holds
    one. An org worker must pass it: its credential is replaced every few
    minutes, so a client built here from ``token`` would keep the first one
    and be refused once it expired. A worker's credential with neither that
    client nor ``token_source`` (where the current bearer is read at each
    request) is refused here rather than served that way.
    """
    if connections is None and token_source is None and looks_like_machine_worker_token(token):
        raise ValueError(
            "an org worker's credential is replaced while it runs; pass the connections "
            "client that follows it instead of the credential itself"
        )
    if connections is None and looks_like_machine_token(token):
        connections = connections_client.ChatConnectionsClient(
            api_url=api_url,
            token=token,
            chats=chats,
            workspaces=workspaces,
            # The box's bearer as it is now: a worker's is replaced while it runs.
            token_source=token_source,
        )
    client: TeamConnectionsClient = connections or connections_client.TeamConnectionsClient(
        api_url=api_url, token=token
    )
    # Installed on every credential: a box run on its operator's login (the
    # self-hosted box) reads its notebooks' connections through it too.
    connections_client.install_connections_client(lambda: client)
    return client


class TeamConnectionsSync:
    """A box's background sync of its team connections: pull, reconcile, accept."""

    def __init__(self, *, project: ProjectDirectory, connections: TeamConnectionsClient) -> None:
        self._project = project
        self._connections = connections
        self._lock = asyncio.Lock()

    async def sync_once(self) -> bool:
        async with self._lock:
            records = await self._connections.fetch_records()
            if records is None:
                logger.info("box connections: team connections unreachable; keeping what is held")
                return False
            done = await reconcile_box_connections(
                self._project, records, connections=self._connections
            )
            for record_id in done.accepted:
                logger.info("box connections: accepted team connection %s", record_id)
            return done.changed

    async def stop(self) -> None:
        return None


class TeamConnectionsData:
    """The open platform's box data plane (:data:`alkera_cli.cloud.box_data.BOX_DATA`):
    the box reconciles its owners' team connections into the workspace and
    accepts them, and writes no schema or source cards."""

    def schema_cards(
        self,
        *,
        api_url: str,
        token: str,
        runtime: Any,
        chats: Callable[[], list[str]],
        refresh_interval: float,
        workspaces: Callable[[], list[str]] = list,
        token_source: Callable[[], str] | None = None,
        connections: Any = None,
    ) -> SchemaCards:
        client = box_connections_client(
            api_url=api_url,
            token=token,
            chats=chats,
            workspaces=workspaces,
            token_source=token_source,
            connections=connections,
        )
        return TeamConnectionsSync(project=runtime.project, connections=client)

    def refresh_sources(self, project: ProjectDirectory) -> Sequence[str]:
        return ()


TEAM_CONNECTIONS_DATA = TeamConnectionsData()


__all__ = [
    "TEAM_CONNECTIONS_DATA",
    "BoxReconcile",
    "TeamConnectionsData",
    "TeamConnectionsSync",
    "accept_shared_connections",
    "box_connections_client",
    "reconcile_box_connections",
]
