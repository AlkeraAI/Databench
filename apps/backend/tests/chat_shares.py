"""Sharing a chat's node at a rung, the way the share dialog does.

Driving a chat's agent takes the ladder's WRITE rung on the chat's node, so
every test about who may send needs to be able to make a real grant. Nothing
here is hand-rolled: ``files_on`` makes ``create_chat`` mint the chat's node in
the same transaction production does, and :func:`share_chat` writes the grant
through ``files.acl.grant``, which interns the ACL exactly as a share does. A
change to how a chat's node or its ACL is built therefore surfaces in these
tests instead of passing against a fixture that quietly stopped matching.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.files import acl as files_acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from backend.services.files.store import set_store_factory
from backend.services.org import teams as team_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def files_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Files enabled with a filesystem store under ``tmp_path``, so a chat
    created in this test gets its node — and any bytes it does write land here.

    Settings alone are not enough. The store factory is process-wide and built
    once, on whichever call reaches it first, so a worker that opened a store
    before this test runs keeps serving that one however the settings read now:
    a test that writes a byte would go to the developer's dev object store, and
    on a machine that runs none it gets a connection refused. Installing the
    factory through the seam production fills points the whole backend here, and
    dropping it on teardown stops the next test inheriting a root that is gone.
    """
    root = tmp_path / "files-store"
    root.mkdir()
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    try:
        yield
    finally:
        set_store_factory(None)


async def share_chat_with(
    db: AsyncSession, *, chat: WorkspaceObject, owner: User, principal: Principal, role: str
) -> None:
    """Share ``chat``'s node with ``principal`` at ``role``: the ``file_shares``
    row plus the interned ACL below it.

    The principal is whatever the registry knows — a person, or a team, which
    is how a chat reaches a group without naming its members one by one."""
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        node = (
            await repo.session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == chat.id, FileNode.trashed_at.is_(None)
                )
            )
        ).scalar_one()
        await files_acl.grant(repo, ctx, node, principal, role)
    await db.commit()


async def share_chat(
    db: AsyncSession, *, chat: WorkspaceObject, owner: User, user: User, role: str
) -> None:
    """Share ``chat``'s node with ``user`` at ``role``."""
    await share_chat_with(
        db, chat=chat, owner=owner, principal=Principal(kind="user", id=user.id), role=role
    )


__all__ = ["files_on", "share_chat", "share_chat_with"]
