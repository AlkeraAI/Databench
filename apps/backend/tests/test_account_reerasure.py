"""A database restored from before an erasure ends clean again.

The person is erased; then the rows a pre-erasure backup holds for them are put
back, as a restore would (their identity row, memberships, chats and
transcripts, preferences, linked sign-in, the closed org's name). The knowledge
base's rows are restored and erased again in its own lifecycle test.
The erasure ledger lives in its own store, which the restore does not touch.
The re-erasure the restore runbook and the worker's boot run finds the person
back and erases them again; afterwards nothing in the database carries their
address or surname, and a second run finds nobody.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.account import ledger
from alkera_core.account.dispositions import tombstone_email
from alkera_core.db.base import Base
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models import AccountDeletionRequest, User, WorkspaceObject
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from tests.chat_shares import files_on  # noqa: F401 - fixture
from tests.conftest import OrgWithAdmin
from tests.test_account_erasure import (
    World,
    _rows_mentioning,
    _run_due,
    _schedule,
    _seed,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on", "multi_org")]

#: What a pre-erasure backup holds for the person, table by table, in the order
#: a restore can put it back (parents first).
_SNAPSHOT: tuple[tuple[str, str], ...] = (
    ("org_memberships", "user_id = :u"),
    ("team_memberships", "user_id = :u"),
    ("user_preferences", "user_id = :u"),
    ("oauth_identities", "user_id = :u"),
    ("workspace_objects", "owner_user_id = :u"),
    ("chat_messages", "chat_id IN (SELECT id FROM workspace_objects WHERE owner_user_id = :u)"),
)


async def _backup(w: World) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        user = (
            (await session.execute(select(User.__table__).where(User.id == w.u_id)))
            .mappings()
            .one()
        )
        solo_name = await session.scalar(
            text("SELECT name FROM teams WHERE id = :o"), {"o": w.org_s}
        )
        tables: dict[str, list[dict[str, Any]]] = {}
        for name, where in _SNAPSHOT:
            table = Base.metadata.tables[name]
            rows = await session.execute(select(table).where(text(where)), {"u": w.u_id})
            tables[name] = [dict(r) for r in rows.mappings()]
    return {"user": dict(user), "solo_name": solo_name, "tables": tables}


async def _restore(backup: dict[str, Any], w: World) -> None:
    """Put the person's rows back as the backup held them."""
    async with AsyncSessionLocal() as session:
        user = dict(backup["user"])
        user.pop("id")
        await session.execute(update(User.__table__).where(User.id == w.u_id).values(**user))
        await session.execute(
            text("UPDATE teams SET name = :n WHERE id = :o"),
            {"n": backup["solo_name"], "o": w.org_s},
        )
        for name, _where in _SNAPSHOT:
            rows = backup["tables"][name]
            if rows:
                table = Base.metadata.tables[name]
                await session.execute(insert(table).values(rows).on_conflict_do_nothing())
        await session.commit()


async def test_a_restore_from_before_the_erasure_is_erased_again(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    ledger_store = FilesystemStore(tmp_path / "ledger", clock=lambda: datetime.now(UTC))
    w = await _seed(org_admin, client)
    backup = await _backup(w)
    assert backup["tables"]["workspace_objects"], "the backup holds the person's chats"

    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        status = await _run_due(w.u_id, account_archives, ledger_store=ledger_store)
    assert status == "completed"
    assert await ledger.erased_ids(ledger_store) == [w.u_id]
    assert await _rows_mentioning(w.u_email) == {}

    # The restore: the database goes back, the ledger does not.
    await _restore(backup, w)
    assert await _rows_mentioning(w.u_email) != {}
    assert await ledger.resurrected([w.u_id]) == [w.u_id]
    # In the restored rows the person reads as the shared org's last admin; the
    # re-erasure is not held by it: that erasure was already decided.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE team_memberships SET role = 'admin' WHERE user_id = :u AND team_id = :t"),
            {"u": w.u_id, "t": w.org_a},
        )
        await session.execute(
            text("UPDATE team_memberships SET role = 'member' WHERE user_id = :u AND team_id = :t"),
            {"u": w.b_id, "t": w.org_a},
        )
        await session.commit()
    mails_before = len(account_mail)

    rerun = await ledger.reerase(store=ledger_store)
    assert (rerun.ledgered, rerun.resurrected, rerun.erased, rerun.failed) == (1, 1, [w.u_id], [])

    assert await _rows_mentioning(w.u_email) == {}
    assert await _rows_mentioning(w.surname) == {}
    async with AsyncSessionLocal() as session:
        user = await session.get(User, w.u_id)
        assert user is not None and user.email == tombstone_email(w.u_id)
        assert user.deleted_at is not None
        shared = await session.get(WorkspaceObject, w.shared_chat)
        # Moved to the org at the first erasure; the restore left it there.
        assert shared is not None and shared.owner_user_id == w.b_id
        assert await session.get(WorkspaceObject, w.private_chat) is None
        sources = (
            await session.execute(
                select(AccountDeletionRequest.source, AccountDeletionRequest.status).where(
                    AccountDeletionRequest.user_id == w.u_id
                )
            )
        ).all()
        assert ("restore", "completed") in [tuple(r) for r in sources]
    assert len(account_mail) == mails_before, "the person is not emailed a second time"

    again = await ledger.reerase(store=ledger_store)
    assert (again.ledgered, again.resurrected, again.erased) == (1, 0, [])


async def test_the_ledger_is_append_only_and_holds_nothing_personal(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    ledger_store = FilesystemStore(tmp_path / "ledger", clock=lambda: datetime.now(UTC))
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        await _run_due(w.u_id, account_archives, ledger_store=ledger_store)
    key = ledger.ledger_key(w.u_id)
    body = b"".join([chunk async for chunk in await ledger_store.get(key)])
    assert w.u_email.encode() not in body and w.surname.encode() not in body
    assert set(__import__("json").loads(body)) == {"schema", "user_id", "request_id", "erased_at"}
    # A second write for the same identity leaves the first one exactly as it was.
    wrote = await ledger.record(
        ledger_store,
        user_id=w.u_id,
        request_id=w.u_id,
        erased_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert wrote is False
    assert b"".join([chunk async for chunk in await ledger_store.get(key)]) == body
