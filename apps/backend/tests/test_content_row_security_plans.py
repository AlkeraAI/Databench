"""Every content-table read a request makes is still served by an index under
the tenant role.

A row-level policy adds its predicate to every statement, and Postgres keeps a
user predicate that is not leakproof (a ``jsonb`` operator, a ``LIKE``) from
running before the policy's, so it can no longer be an index condition. A read
whose only indexable predicate is then the policy's must find an index leading
with the org, or it scans the table; the September 2026 CI stall was exactly a
policy predicate no index served. Each statement here is planned under
``alkera_tenant_app`` with one org bound and sequential scans priced out
(``enable_seqscan = off``): a plan that still holds a ``Seq Scan`` on a content
table is one no index can serve. The data is irrelevant to the question, so
none is seeded.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.db.row_security import CONTENT_TABLES, content_tier_tables
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.tenant_session import apply_now, bind_tenant
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio]

#: Every table the tenant role is policed on: the content tables, and the Files
#: tables (reached under the tenant role by a request bound to several orgs).
POLICED = content_tier_tables()

_ORG = uuid.uuid4()
_ID = uuid.uuid4()
_PARAMS: dict[str, Any] = {
    "org": _ORG,
    "id": _ID,
    "chat": _ID,
    "user": _ID,
    "team": _ID,
    "connection": _ID,
    "doc_id": str(_ID),
    "machine": str(_ID),
    "repo": "acme/data",
    "since": datetime(2026, 1, 1, tzinfo=UTC),
}

#: The hot reads, as the services spell them.
HOT: dict[str, str] = {
    # chat_service: a transcript page, and an ask looked up by its request id.
    "chat_messages.page": (
        "SELECT * FROM chat_messages WHERE chat_id = :chat AND seq > 0 ORDER BY seq LIMIT 200"
    ),
    "chat_messages.find_ask": (
        "SELECT * FROM chat_messages WHERE chat_id = :chat "
        "AND kind IN ('permission', 'question') "
        "AND payload->'payload'->>'request_id' = 'r1' ORDER BY seq"
    ),
    # kb.service.pull: the incremental pull by repo past a watermark.
    "kb_items.pull": (
        "SELECT * FROM kb_items WHERE org_team_id = :org AND repo IN (:repo, '') "
        "AND updated_at >= :since ORDER BY updated_at, id LIMIT 500"
    ),
    "kb_items.by_item": (
        "SELECT * FROM kb_items WHERE org_team_id = :org AND repo = :repo AND item_id = 'x'"
    ),
    # crdt.docs: a document's row and its update log after a position.
    "crdt_docs.by_key": (
        "SELECT * FROM crdt_docs WHERE org_id = :org AND doc_type = 'chat_workspace' "
        "AND doc_id = :doc_id"
    ),
    "crdt_updates.log": (
        "SELECT log_seq, data FROM crdt_updates WHERE org_id = :org "
        "AND doc_type = 'chat_workspace' AND doc_id = :doc_id AND epoch = 1 AND log_seq > 0 "
        "ORDER BY log_seq"
    ),
    # objects: one object, an org's listing, and the chats a box holds (a
    # jsonb predicate, so only the policy's org can lead the index).
    "workspace_objects.by_id": "SELECT * FROM workspace_objects WHERE id = :id",
    "workspace_objects.listing": (
        "SELECT * FROM workspace_objects WHERE org_team_id = :org AND type = 'chat' "
        "AND deleted_at = 0 ORDER BY created_at DESC, id DESC LIMIT 50"
    ),
    "workspace_objects.on_machine": (
        "SELECT * FROM workspace_objects WHERE type = 'chat' AND deleted_at = 0 "
        "AND spec->>'machine_id' = :machine ORDER BY created_at DESC, id DESC LIMIT 50"
    ),
    "object_payload_rows.pages": (
        "SELECT * FROM object_payload_rows WHERE object_id = :id ORDER BY page"
    ),
    "slack_thread_chats.by_chat": "SELECT * FROM slack_thread_chats WHERE chat_id = :chat",
    "team_connections.by_team": "SELECT * FROM team_connections WHERE team_id = :team",
    "user_oauth_tokens.grant": (
        "SELECT * FROM user_oauth_tokens WHERE user_id = :user AND team_connection_id = :connection"
    ),
    "team_memberships.by_user": "SELECT * FROM team_memberships WHERE user_id = :user",
    "team_memberships.by_team": "SELECT * FROM team_memberships WHERE team_id = :team",
}
#: And every content table read with no predicate of its own: only the
#: policy's org can be served by an index then. (Six Files tables have no index
#: leading with their org, under the Files policy as much as this one; nothing
#: reads them without a narrower key, and they are the Files subsystem's.)
HOT.update({f"{table}.unscoped": f"SELECT * FROM {table}" for table in sorted(CONTENT_TABLES)})


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


async def _plan(statement: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        bind_tenant(session, [_ORG])
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        raw = (
            await session.execute(text(f"EXPLAIN (FORMAT JSON) {statement}"), _PARAMS)
        ).scalar_one()
        await session.rollback()
    return dict(raw[0]["Plan"])


@pytest.mark.parametrize("name", sorted(HOT))
async def test_the_read_is_served_by_an_index_under_the_policy(name: str) -> None:
    plan = await _plan(HOT[name])
    scans = [
        node.get("Relation Name")
        for node in _nodes(plan)
        if node["Node Type"] == "Seq Scan" and node.get("Relation Name") in POLICED
    ]
    assert scans == [], f"{name} scans {scans}"


async def test_the_policy_predicate_is_in_the_plan() -> None:
    """The cases above prove nothing if the role did not apply the policy:
    the bound plan of an unscoped read carries the ``alkera.org_ids`` setting
    (``alkera_org_ids()`` is inlined)."""
    plan = await _plan("SELECT * FROM workspace_objects")
    assert any("alkera.org_ids" in str(node) for node in _nodes(plan)), plan


async def test_a_table_with_no_org_index_is_caught() -> None:
    """The guard bites: with the org index it relies on hidden inside the
    planning transaction, the unscoped read of ``team_connections`` plans a
    sequential scan."""
    async with AsyncSessionLocal() as session:
        superuser = (
            await session.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
        if not superuser:
            pytest.skip("dropping an index takes its owner; the non-superuser tier's login is not")
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("DROP INDEX ix_team_connections_org_team_id"))
        bind_tenant(session, [_ORG])

        await apply_now(session)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        raw = (
            await session.execute(text("EXPLAIN (FORMAT JSON) SELECT * FROM team_connections"))
        ).scalar_one()
        await session.rollback()
    types = {node["Node Type"] for node in _nodes(raw[0]["Plan"])}
    assert "Seq Scan" in types
