"""A workspace's listing row is read in one grouped statement, however many
chats it holds, and says exactly what reading every chat would say.

The reference is ``effective_binding`` over every chat's own spec, the
derivation the row used to be computed with: the summary must agree with it
on every fact, on a fixture with many chats spread over a few distinct values,
while the rows it reads stay bounded by the distinct values, not the chats.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.objects.workspaces import effective_binding
from alkera_core.schemas.objects import ChatSpec
from backend.services.compute import chat_is_writable
from backend.services.workspaces.summary import summarize
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio]

_MACHINE_A, _MACHINE_B = str(uuid.uuid4()), str(uuid.uuid4())

#: ``(spec, how many chats carry it)``: a busy workspace, a few distinct values.
_BUSY: list[tuple[dict[str, Any], int]] = [
    (
        {
            "machine_id": _MACHINE_A,
            "machine_status": "ready",
            "mirror_state": "asleep",
            "permission_mode": "read_only",
        },
        120,
    ),
    (
        {
            "machine_id": _MACHINE_A,
            "machine_status": "ready",
            "mirror_state": "awake",
            "permission_mode": "read_only",
            "wake_requested_at": "2026-10-05T10:00:00+02:00",
        },
        60,
    ),
    (
        {
            "machine_id": _MACHINE_A,
            "machine_status": "ready",
            "mirror_state": "awake",
            "permission_mode": "read_only",
            # Later as text, earlier as an instant: compared as instants.
            "wake_requested_at": "2026-10-05T09:30:00+00:00",
        },
        3,
    ),
    ({"wake_requested_at": "not a time"}, 2),
]


async def _seed(
    db: AsyncSession, org: OrgWithAdmin, workspace_id: UUID, specs: list[tuple[dict[str, Any], int]]
) -> list[ChatSpec]:
    made: list[ChatSpec] = []
    for spec, copies in specs:
        body = {"schema_version": "1.10.0", **spec, "workspace_id": str(workspace_id)}
        for n in range(copies):
            await db.execute(
                text(
                    "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, type, "
                    "title, version, status, spec, owner_user_id, visibility_scope) "
                    "VALUES (gen_random_uuid(), :org, :logical, 'workspace', 'chat', :title, 1, "
                    "'ready', CAST(:spec AS jsonb), :owner, 'private')"
                ),
                {
                    "org": org.org_id,
                    "logical": uuid.uuid4().hex,
                    "title": f"chat {n}",
                    "spec": json.dumps(body),
                    "owner": org.admin_id,
                },
            )
            made.append(ChatSpec.model_validate(body))
    await db.commit()
    return made


@contextmanager
def _statements(db: AsyncSession) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = db.get_bind()

    def _count(*args: Any) -> None:
        seen.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", _count)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _count)


def _facts(specs: list[ChatSpec]) -> Any:
    return effective_binding(specs, is_writable=chat_is_writable)


async def test_a_busy_workspace_is_summarized_in_one_bounded_read_that_agrees_with_every_chat(
    org_admin: OrgWithAdmin,
) -> None:
    busy, single, spares, empty = (uuid.uuid4() for _ in range(4))
    async with AsyncSessionLocal() as db:
        every = await _seed(db, org_admin, busy, _BUSY)
        one = await _seed(
            db, org_admin, single, [({"machine_id": _MACHINE_B, "permission_mode": "auto"}, 1)]
        )
        await _seed(db, org_admin, spares, [({"spare": True}, 1)])

    async with AsyncSessionLocal() as db:
        with _statements(db) as seen:
            summaries = await summarize(db, [busy, single, spares, empty])

    assert len(seen) == 1, "one statement for the whole page"
    assert summaries[busy].count == len(every) == 185
    assert len(summaries[busy].specs) <= len(_BUSY), "rows are bounded by distinct values"
    assert _facts(summaries[busy].specs) == _facts(every)
    assert _facts(summaries[busy].specs).wake_requested_at is not None
    assert summaries[busy].all_spare is False
    assert summaries[busy].only_title is None
    assert summaries[single].count == 1
    assert _facts(summaries[single].specs) == _facts(one)
    assert summaries[single].only_title == "chat 0"
    assert summaries[spares].all_spare is True
    assert (summaries[empty].count, summaries[empty].all_spare) == (0, False)
    assert _facts(summaries[empty].specs) == _facts([])
