"""A move and a restore take *every* folder they rewrite, before they write one.

``rederive_folding_flags`` rewrites the whole sibling set of a folder, so its
caller contract is that the folder row is already held at update strength
(``FOR NO KEY UPDATE``) — taken in the one fixed order, drive → parent → node,
*before* the first row write. A rename earns that gate for its single folder. A
move and a restore each touch **two** folders (the one the node leaves and the
one it lands in), and a gate that took only one of them leaves the other pair
free to be taken in the opposite order by the writer coming the other way.

What this module reads is the wire: every row lock a session takes against
``file_nodes``, in order, up to that session's first ``UPDATE file_nodes``. The
assertion is that both folders are in there, and that they were taken by
ascending id — a total order two writers of the same pair cannot disagree about.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


class LockLedger:
    """The node rows a session takes ``FOR UPDATE`` before its first write.

    Listening on the engine rather than on one connection because the fixture
    hands out a single session: every statement seen here is that session's.
    """

    def __init__(self, session: AsyncSession) -> None:
        bind = session.bind
        assert isinstance(bind, AsyncEngine)
        self._sync = bind.sync_engine
        self.locked: list[uuid.UUID] = []
        self.wrote = False
        event.listen(self._sync, "after_cursor_execute", self._after)

    def close(self) -> None:
        event.remove(self._sync, "after_cursor_execute", self._after)

    @staticmethod
    def _ids(parameters: Any) -> list[uuid.UUID]:
        values = parameters.values() if isinstance(parameters, dict) else (parameters or ())
        return [value for value in values if isinstance(value, uuid.UUID)]

    def _after(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        upper = statement.upper()
        if "FILE_NODES" not in upper:
            return
        if "FOR UPDATE" in upper or "FOR NO KEY UPDATE" in upper:
            if not self.wrote:
                self.locked.extend(self._ids(parameters))
            return
        if upper.lstrip().startswith("UPDATE"):
            self.wrote = True

    def first_index(self, node_id: uuid.UUID) -> int:
        assert node_id in self.locked, f"{node_id} was never locked before the first write"
        return self.locked.index(node_id)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _assert_both_folders_gated(
    ledger: LockLedger, source: uuid.UUID, destination: uuid.UUID
) -> None:
    """Both folders locked before the first write, lower id first."""
    lower, higher = sorted((source, destination))
    assert ledger.wrote, "the operation never wrote a node row"
    assert ledger.first_index(lower) < ledger.first_index(higher), (
        f"folders taken out of order: {ledger.locked}"
    )


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    tmp_path: Path,
) -> tuple[FilesOrg, FilesRepo, Any, DriveId]:
    org = await files_org_factory()
    drive = await files_factory.drive(org=org)
    tree = await files_factory.tree("src/ dst/ src/a.bin", drive=drive)
    repo = FilesRepo(files_session, OrgScope(org_team_id=org.org_team_id))
    return org, repo, tree, DriveId(drive.id)


async def test_move_gates_both_folders(
    rig: tuple[FilesOrg, FilesRepo, Any, DriveId], files_session: AsyncSession
) -> None:
    org, repo, tree, _ = rig
    namespace = Namespace(repo, _ctx(org), SystemClock(), None)
    node, source, destination = tree["src/a.bin"], tree["src"], tree["dst"]
    ledger = LockLedger(files_session)
    try:
        async with repo.transaction():
            await namespace.move(NodeId(node.id), NodeId(destination.id), if_match=int(node.etag))
    finally:
        ledger.close()
    _assert_both_folders_gated(ledger, source.id, destination.id)


async def test_restore_gates_both_folders(
    rig: tuple[FilesOrg, FilesRepo, Any, DriveId], files_session: AsyncSession
) -> None:
    org, repo, tree, _ = rig
    trash = Trash(repo, _ctx(org), SystemClock())
    node, source, destination = tree["src/a.bin"], tree["src"], tree["dst"]
    async with repo.transaction():
        op = await trash.trash(NodeId(node.id), if_match=int(node.etag))
    ledger = LockLedger(files_session)
    try:
        async with repo.transaction():
            await trash.restore(op.id, parent_id=NodeId(destination.id))
    finally:
        ledger.close()
    _assert_both_folders_gated(ledger, source.id, destination.id)
