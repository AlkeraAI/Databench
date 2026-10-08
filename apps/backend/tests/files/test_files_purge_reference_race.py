"""A purge that meets a row written under it is refused, never a 500.

Deleting forever clears every row that points at the subtree, then the nodes.
A write that commits between the two (a new version, a history entry) leaves a
row pointing at a node the purge is about to delete, and Postgres refuses that
delete with a foreign-key violation (SQLSTATE 23503). The answer was the opaque
500 every unclassified database error gets; it is the 409 a reference earns.

The race is placed deterministically: a test-only trigger writes the history
row at the instant the purge has just cleared the versions, the moment a
concurrent writer would land. It lives for the one request and is dropped
after.
"""

from __future__ import annotations

from typing import Any

import pytest
from _files_kit import refusal
from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_OWNER
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

_TRIGGER = "lanea_purge_race"


async def _land_a_write_under_the_purge(session: AsyncSession, node: FileNode) -> None:
    await session.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION {_TRIGGER}() RETURNS trigger
            LANGUAGE plpgsql SECURITY DEFINER AS $$
            BEGIN
                INSERT INTO file_history (id, org_team_id, node_id, seq, kind, acting_principal)
                VALUES (gen_random_uuid(), '{node.org_team_id}', '{node.id}', 1, 'attrs',
                        '{node.org_team_id}');
                RETURN NULL;
            END $$
            """
        )
    )
    await session.execute(
        text(
            f"CREATE TRIGGER {_TRIGGER} AFTER DELETE ON file_versions "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {_TRIGGER}()"
        )
    )
    await session.commit()


async def _clear(session: AsyncSession) -> None:
    await session.execute(text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON file_versions"))
    await session.execute(text(f"DROP FUNCTION IF EXISTS {_TRIGGER}()"))
    await session.commit()


async def test_a_write_landing_under_a_purge_is_a_409_and_nothing_is_lost(
    files_client: AsyncClient,
    files_on: None,
    fx: Any,
    files_org: Any,
    idem: Any,
    real_session: AsyncSession,
) -> None:
    folder = await fx.node(b"kept", kind="folder")
    node = await fx.node(b"raced.txt", parent=folder)
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, ctx, folder, Principal(kind="user", id=files_org.org.admin_id), ROLE_OWNER
        )
    await fx.repo.session.commit()
    await fx.repo.session.refresh(node)
    drive = (await files_client.get(f"{BASE}/drives")).json()["id"]

    await _land_a_write_under_the_purge(real_session, node)
    try:
        purged = await files_client.delete(
            f"{BASE}/drives/{drive}/items/{node.id}?permanent=true",
            headers={**idem(), "If-Match": str(node.etag)},
        )
    finally:
        await _clear(real_session)

    assert purged.status_code == 409, purged.text
    assert refusal(purged)["code"] == "conflict.reference"
    # The purge rolled back whole: the node is still there.
    still = await real_session.execute(
        text("SELECT count(*) FROM file_nodes WHERE id = :id"), {"id": node.id}
    )
    assert still.scalar_one() == 1
