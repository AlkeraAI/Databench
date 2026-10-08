"""Team connections' part in exporting and erasing an account.

Registered into :data:`alkera_core.account.contributors.ACCOUNT_CONTRIBUTORS`
by :data:`ACCOUNT_CONNECTIONS`, which the composition roots install (the
backend's and the worker's):

* **export**: the connectors the person's clients reported, in
  ``profile.json``, and per org the person's own connections without any
  credential, as ``connections.json``;
* **closing an org**: every connection the org held goes with it.

What erasure does to each connection column that names a person is registered
beside the tables, in the connection models.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.account.contributors import (
    ACCOUNT_CONTRIBUTORS,
    AccountContributor,
    OrgClosing,
)
from alkera_core.connections.models import ConnectionInventoryEntry, TeamConnection
from alkera_core.extensions import Extension

#: The connection fields an export carries. Never a credential.
_EXPORTED = ("id", "plugin", "handle", "auth_mode", "auth_method", "enabled", "created_at")


async def profile_sections(db: AsyncSession, user_id: uuid.UUID) -> Mapping[str, object]:
    entries = (
        (
            await db.execute(
                select(ConnectionInventoryEntry).where(ConnectionInventoryEntry.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "connection_inventory": [
            {
                "org_team_id": c.org_team_id,
                "workspace_key": c.workspace_key,
                "plugin": c.plugin,
                "status": c.status,
                "reported_at": c.reported_at,
            }
            for c in entries
        ]
    }


async def org_documents(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> Mapping[str, object]:
    owned = (
        (
            await db.execute(
                select(TeamConnection).where(
                    TeamConnection.org_team_id == org_id, TeamConnection.owner_user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {"connections.json": [{name: getattr(c, name) for name in _EXPORTED} for c in owned]}


async def erase_org_connections(closing: OrgClosing) -> Mapping[str, int]:
    result = await closing.db.execute(
        delete(TeamConnection).where(TeamConnection.org_team_id == closing.org_id)
    )
    erased = int(result.rowcount) if isinstance(result, CursorResult) else 0
    return {"team_connections.org_team_id:erase": erased}


CONTRIBUTOR = AccountContributor(
    name="connections",
    profile_sections=profile_sections,
    org_documents=org_documents,
    org_closing=erase_org_connections,
)


def _install() -> None:
    ACCOUNT_CONTRIBUTORS.register(CONTRIBUTOR)


ACCOUNT_CONNECTIONS = Extension(name="alkera.connections.account_lifecycle", install=_install)


__all__ = ["ACCOUNT_CONNECTIONS", "CONTRIBUTOR"]
