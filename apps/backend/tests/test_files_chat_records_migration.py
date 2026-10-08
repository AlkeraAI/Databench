"""The chat-records back-fill against the real schema.

Revision 0133 marked three of a chat's records with the RECORD bit and only
for the chats that existed on the day it ran; nothing marked the logs, and
nothing marked a record created since. Revision 0168 closes both. ``alembic
check`` sees none of it — a back-fill changes no column — so this module
drives the revision down and back up against a seeded tree and pins exactly
which rows carry the mark afterwards: the six names directly under a chat
folder and everything the runtime directory holds, never the working
directory, never a working file wearing a record's name, never the same
names outside a chat.

The downgrade is asymmetric on purpose and pinned as such: it takes the mark
off the three names this revision introduced and leaves 0133's three alone,
because on any given row 0133 may have been what set them.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from alkera_core.chat_records import CHAT_RECORD_NAMES, TRACE_FILES
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.files.authz.decider import RECORD_BIT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0168_chat_records_from_birth.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0167"

_SANDBOX = b"scratch"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _seed_drive(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"records-seed-{store_id.hex}"},
    )
    domain_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dedup_domains (id, org_team_id, store_id, chunker_seed) "
            "VALUES (:id, :org, :store, '\\x00'::bytea)"
        ),
        {"id": domain_id, "org": org_id, "store": store_id},
    )
    drive_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_drives "
            "(id, org_team_id, store_id, dedup_domain_id, quota_bytes, quota_nodes, next_ino) "
            "VALUES (:id, :org, :store, :domain, 0, 0, 1)"
        ),
        {"id": drive_id, "org": org_id, "store": store_id, "domain": domain_id},
    )
    return drive_id


_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata) VALUES (:id, :ino, :drive, :org, :parent, :kind, :subtype, :object, :name, "
    "'{}'::jsonb, CAST(:path AS ltree), :depth, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, :flags, false, '{}'::jsonb)"
)


class _Seeder:
    """Inserts nodes with running inos and remembers each by a readable key."""

    def __init__(self, session: AsyncSession, drive_id: uuid.UUID, org_id: uuid.UUID) -> None:
        self._session = session
        self._drive = drive_id
        self._org = org_id
        self._ino = 1
        self.ids: dict[str, uuid.UUID] = {}

    async def node(
        self,
        key: str,
        name: bytes,
        *,
        kind: str = "folder",
        parent: tuple[uuid.UUID, str, int] | None = None,
        subtype: str | None = None,
        target_object_id: uuid.UUID | None = None,
        flags: int = 0,
    ) -> tuple[uuid.UUID, str, int]:
        node_id = uuid.uuid4()
        label = str(node_id).replace("-", "_")
        path = label if parent is None else f"{parent[1]}.{label}"
        depth = 0 if parent is None else parent[2] + 1
        await self._session.execute(
            text(_INSERT_NODE),
            {
                "id": node_id,
                "ino": self._ino,
                "drive": self._drive,
                "org": self._org,
                "parent": None if parent is None else parent[0],
                "kind": kind,
                "subtype": subtype,
                "object": target_object_id,
                "name": name,
                "path": path,
                "depth": depth,
                "flags": flags,
            },
        )
        self._ino += 1
        self.ids[key] = node_id
        return node_id, path, depth


def _kind_of(name: str) -> str:
    return "folder" if name == ".runtime" else "file"


async def _seed(session: AsyncSession, org_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Two chats and a lookalike.

    ``old`` is a chat 0133 marked: its manifest, digest and runtime folder
    already carry the bit, its logs do not. ``new`` is a chat made after 0133,
    nothing marked. Both hold every record name, a working directory with a
    working file under a record's name, and the runtime folder holds a file.
    ``notes`` is an ordinary folder holding the same six names.
    """
    drive_id = await _seed_drive(session, org_id)
    seed = _Seeder(session, drive_id, org_id)
    root = await seed.node("root", b"root")
    for chat_key, premarked in (("old", True), ("new", False)):
        chat = await seed.node(
            chat_key,
            f"{chat_key}.chat".encode(),
            parent=root,
            subtype="chat",
            target_object_id=uuid.uuid4(),
        )
        for name in sorted(CHAT_RECORD_NAMES):
            marked = premarked and name not in TRACE_FILES
            made = await seed.node(
                f"{chat_key}/{name}",
                name.encode(),
                kind=_kind_of(name),
                parent=chat,
                flags=RECORD_BIT if marked else 0,
            )
            if name == ".runtime":
                agent = await seed.node(
                    f"{chat_key}/.runtime/agent",
                    b"agent",
                    parent=made,
                    flags=RECORD_BIT if marked else 0,
                )
                await seed.node(
                    f"{chat_key}/.runtime/agent/agent.db",
                    b"agent.db",
                    kind="file",
                    parent=agent,
                    flags=RECORD_BIT if marked else 0,
                )
        sandbox = await seed.node(f"{chat_key}/scratch", _SANDBOX, parent=chat)
        await seed.node(
            f"{chat_key}/scratch/report.html", b"report.html", kind="file", parent=sandbox
        )
        await seed.node(
            f"{chat_key}/scratch/chat.jsonl", b"chat.jsonl", kind="file", parent=sandbox
        )
    notes = await seed.node("notes", b"notes", parent=root)
    for name in sorted(CHAT_RECORD_NAMES):
        await seed.node(f"notes/{name}", name.encode(), kind=_kind_of(name), parent=notes)
    await session.commit()
    return seed.ids


async def _flags(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(await _scalar(session, "SELECT flags FROM file_nodes WHERE id = :id", id=node_id))


async def _marked(session: AsyncSession, ids: dict[str, uuid.UUID]) -> set[str]:
    return {key for key, node_id in ids.items() if await _flags(session, node_id) & RECORD_BIT}


def _records_of(chat_key: str) -> set[str]:
    return {f"{chat_key}/{name}" for name in CHAT_RECORD_NAMES} | {
        f"{chat_key}/.runtime/agent",
        f"{chat_key}/.runtime/agent/agent.db",
    }


def test_the_revision_is_part_of_the_schema_the_code_expects() -> None:
    assert _REVISION == "0168"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def test_the_back_fill_marks_every_chats_records_and_nothing_else() -> None:
    """Down to the parent and back up against the seeded tree.

    Afterwards every record of both chats carries the mark — the logs 0133
    never named, the records of the chat made after 0133, the file inside the
    runtime folder — and nothing else moved: not the working directory, not
    the working file wearing a record's name, not the lookalike outside any
    chat, and no other bit on any row.
    """
    org_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            ids = await _seed(session, org_id)
            assert await _marked(session, ids) == _records_of("old") - {
                f"old/{name}" for name in TRACE_FILES
            }

        await db.downgrade(_PARENT)
        await db.upgrade()
        async with db.session() as session:
            assert await _marked(session, ids) == _records_of("old") | _records_of("new")
            for key in ("old/scratch/report.html", "new/scratch/chat.jsonl", "notes/chat.jsonl"):
                assert await _flags(session, ids[key]) == 0, key


async def test_the_downgrade_unmarks_the_logs_and_leaves_the_older_marks_standing() -> None:
    """The inverse of what this revision introduced, and no more: the three
    log names lose the mark under every chat; the manifest, the digest and the
    runtime folder keep whatever they carry, because 0133 may have set it and
    0133's own downgrade is where that comes off. Running the upgrade again
    puts the logs back — the statements are an OR of one bit and idempotent."""
    org_id = uuid.uuid4()
    logs = {f"{chat}/{name}" for chat in ("old", "new") for name in TRACE_FILES}
    async with migration_scratch() as db:
        async with db.session() as session:
            ids = await _seed(session, org_id)
        await db.downgrade(_PARENT)
        await db.upgrade()
        async with db.session() as session:
            everything = await _marked(session, ids)

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _marked(session, ids) == everything - logs

        await db.upgrade()
        async with db.session() as session:
            assert await _marked(session, ids) == everything
