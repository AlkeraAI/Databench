"""Factories for the CRDT lane's tests: a real chat with real shares, a real
sandbox pool, and Loro peers playing clients.

A Loro peer here is the test's stand-in for a browser tab: it holds its own
document, types into it, and exports exactly what a client would send. The
backend under test never imports Loro (``test_sandbox_isolation``); the test
process may.
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from alkera_core.events import actor_for_user
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_COMMENTER, ROLE_READER, ROLE_WRITER
from alkera_core.models import User, WorkspaceObject
from backend.services.chats import chat_service
from backend.services.crdt.docs import CrdtDocs
from backend.services.crdt.registry import CrdtRegistry, DocRef
from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool, worker_command
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from loro import ExportMode, LoroDoc, VersionVector
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import share_chat_with
from tests.conftest import OrgWithAdmin, make_member


def new_pool(workers: int = 1) -> SandboxPool:
    return SandboxPool(
        PoolConfig(
            workers=workers,
            command=worker_command(memory_mb=1024, cache_docs=64, cache_bytes=32 * 1024 * 1024),
        )
    )


@asynccontextmanager
async def crdt_docs(
    *, registry: CrdtRegistry | None = None, workers: int = 1
) -> AsyncIterator[CrdtDocs]:
    """A document store on a fresh pool of real workers, closed on the way out."""
    pool = new_pool(workers)
    docs = CrdtDocs(
        pool=pool,
        registry=registry or CrdtRegistry(),
        maintenance_delay=0.0,
        validate_budget=10.0,
        load_budget=20.0,
    )
    try:
        yield docs
    finally:
        await docs.aclose()
        await pool.close()


@dataclass
class Person:
    user: User
    ent: EntitlementSnapshot
    #: What they log in with, for a test that opens a socket as them.
    password: str = ""

    @property
    def actor(self) -> dict[str, Any]:
        return actor_for_user(self.user, org_id=self.user.home_org_team_id)


@dataclass
class World:
    """One chat in one org: its owner, a member it is shared with
    at each rung, a member it is not shared with, and the document's ref."""

    org_id: UUID
    ref: DocRef
    owner: Person
    writer: Person
    commenter: Person
    reader: Person
    stranger: Person


async def _person(db: AsyncSession, user: User, password: str | None) -> Person:
    """A person as a socket holds them: a user row loaded once and detached,
    so the test's own commits never expire it under the code being tested."""
    await db.refresh(user)
    ent = await load_entitlements(db, user, org_id=user.home_org_team_id)
    db.expunge(user)
    return Person(user=user, ent=ent, password=password or "")


async def make_world(
    db: AsyncSession, org: OrgWithAdmin, *, workspace: WorkspaceObject | None = None
) -> World:
    """Needs the ``files_on`` fixture: the share hangs off the chat's node.
    The chat is the owner's alone unless ``workspace`` names the workspace
    it is started in."""
    owner = await db.get(User, org.admin_id)
    assert owner is not None
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        title="Draft",
        client_id=None,
        machine_id=None,
        machine_status="none",
        org_id=owner.home_org_team_id,
        workspace=workspace,
    )
    await db.commit()
    members: dict[str, User] = {}
    passwords: dict[str, str | None] = {}
    for name in ("writer", "commenter", "reader", "stranger"):
        member, password = await make_member(db, org_id=org.org_id, verified=True)
        members[name] = member
        passwords[name] = password
    for name, role in (
        ("writer", ROLE_WRITER),
        ("commenter", ROLE_COMMENTER),
        ("reader", ROLE_READER),
    ):
        await share_chat_with(
            db,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=members[name].id),
            role=role,
        )
    await db.commit()
    return World(
        org_id=org.org_id,
        ref=DocRef(org_id=org.org_id, doc_type="chat_draft", doc_id=str(chat.id)),
        owner=await _person(db, owner, org.admin_password),
        writer=await _person(db, members["writer"], passwords["writer"]),
        commenter=await _person(db, members["commenter"], passwords["commenter"]),
        reader=await _person(db, members["reader"], passwords["reader"]),
        stranger=await _person(db, members["stranger"], passwords["stranger"]),
    )


@dataclass
class Peer:
    """A client's copy of the document: what a browser tab holds."""

    peer: int
    #: The root text the document keeps its content in (``content`` for a file).
    container: str = "draft"
    doc: LoroDoc = field(init=False)
    sent: VersionVector | None = None

    def __post_init__(self) -> None:
        self.doc = LoroDoc()  # type: ignore[no-untyped-call]
        self.doc.peer_id = self.peer

    def receive(self, data: bytes) -> None:
        self.doc.import_(data)

    @property
    def vv(self) -> bytes:
        return bytes(self.doc.oplog_vv.encode())

    @property
    def text(self) -> str:
        return str(self.doc.get_text(self.container).to_string())

    def type(self, at: int, text: str) -> bytes:
        """Type ``text`` at ``at`` and return the update a client would send."""
        before = self.doc.oplog_vv
        self.doc.get_text(self.container).insert(at, text)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    def erase(self, at: int, length: int) -> bytes:
        before = self.doc.oplog_vv
        self.doc.get_text(self.container).delete(at, length)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    def since(self, vv: bytes) -> bytes:
        """Everything this copy holds that a holder of ``vv`` lacks."""
        return bytes(self.doc.export(ExportMode.Updates(VersionVector.decode(vv))))


def same_vv(a: bytes, b: bytes) -> bool:
    """Whether two encoded version vectors are the same vector. Never compare
    their bytes: the encoding follows a hash map's order, so one vector
    encodes differently from one document to another."""
    return bool(VersionVector.decode(a) == VersionVector.decode(b))


__all__ = ["Peer", "Person", "World", "crdt_docs", "make_world", "new_pool", "same_vv"]


def kill_hard(pid: int) -> None:
    """End ``pid`` at once, as a crash would: SIGKILL where there is one,
    TerminateProcess on Windows. Gone already is fine."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def alive(pid: int) -> bool:
    """Whether ``pid`` still runs. Never ``os.kill(pid, 0)``: on Windows that
    terminates the process it was meant to probe."""
    from alkera_core.process import process_alive

    return process_alive(pid)
