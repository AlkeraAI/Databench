"""The ``notebook`` CRDT document type: ``doc:notebook:<node_id>``.

A notebook is co-edited live exactly as a text file is (``ephemeral_session``,
its source the ``.alknb.py`` node on the drive, read and written back through
:class:`~backend.services.crdt.registry.FileSource`), but its live document is
a tree of cells, not one text: the sandbox's ``notebook`` strategy reads the
file into it and renders it back (``backend.services.crdt.sandbox.notebook``).
Everything that decides who may do what is the file's: anyone the Files
policy lets read the node reads the document, and a person the policy lets
write it writes it while no lease fences the write. Agents never write over
the socket: they edit through the notebook operations route, which the Files
policy and the lease fence admit per request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from alkera_core.models import User
from alkera_core.notebooks.edits import record_edits
from alkera_core.notebooks.models import NotebookEpochTail
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.errors import CrdtError
from backend.services.crdt.file_kinds import (
    NOTEBOOK_SUFFIX,
    file_name,
    is_notebook_name,
    live_type_of,
)
from backend.services.crdt.registry import (
    DENIED,
    Access,
    CrdtLimits,
    DocRef,
    EntitlementSnapshot,
    FileDocType,
    Seed,
    SessionPolicy,
    register_type,
)
from backend.services.infra import now as _now
from backend.services.sharing import SharedRungCache

#: The agent id a normalization pass is written as.
NORMALIZER_AGENT_ID: Final = "server:normalize"


class NotebookDocType(FileDocType):
    """See the module docstring. ``FileDocType`` supplies the source, the
    seed from the head version, the recovery and the Files-policy access; this
    type narrows them to nodes that are notebooks and gives the document the
    notebook's own rules and limits."""

    name = "notebook"
    rules = "notebook"
    #: Its dependency graph is analysed after commits (debounced), for the
    #: diagnostics an operations result carries.
    diagnoses_graph = True
    doc_schema = 1
    session_policy: SessionPolicy = "ephemeral_session"
    limits = CrdtLimits(
        max_update_bytes=1024 * 1024,
        # The lane sends a whole document in one transfer with room for a
        # second (16 MiB), so a document's history is capped at 8 MiB.
        max_doc_bytes=8 * 1024 * 1024,
        # The largest notebook file a session opens (and the soft cap a
        # client holds the rendered file to); a cell's source is capped at
        # 1 MiB by the sandbox.
        max_text_bytes=4 * 1024 * 1024,
        compact_log_rows=500,
        compact_log_bytes=1024 * 1024,
        rotate_snapshot_bytes=4 * 1024 * 1024,
    )

    async def _is_notebook(self, db: AsyncSession, ref: DocRef) -> bool:
        name = await file_name(db, ref.org_id, ref.doc_id)
        return name is not None and live_type_of(name) == "notebook"

    async def authorize(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        rungs: SharedRungCache | None = None,
    ) -> Access:
        access = await super().authorize(
            db,
            ref=ref,
            user=user,
            ent=ent,
            agent_id=agent_id,
            machine_id=machine_id,
            rungs=rungs,
        )
        if not access.can_read or not await self._is_notebook(db, ref):
            return DENIED
        return access

    async def seed(self, db: AsyncSession, *, ref: DocRef) -> Seed:
        if not await self._is_notebook(db, ref):
            raise CrdtError("not_editable", "this file is not a notebook", reason="not_notebook")
        return await super().seed(db, ref=ref)

    async def keep_tail(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        epoch: int,
        snapshot: bytes,
        vv: bytes,
        next_epoch: int,
        next_base_vv: bytes,
    ) -> None:
        """Keep the state the document's history restarted from (in the
        restart's transaction), and only the latest few."""
        item_id = UUID(ref.doc_id)
        await db.execute(
            pg_insert(NotebookEpochTail)
            .values(
                org_id=ref.org_id,
                item_id=item_id,
                epoch=epoch,
                snapshot=snapshot,
                vv=vv,
                next_epoch=next_epoch,
                next_base_vv=next_base_vv,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "item_id", "epoch"],
                set_={
                    "snapshot": snapshot,
                    "vv": vv,
                    "next_epoch": next_epoch,
                    "next_base_vv": next_base_vv,
                },
            )
        )
        await db.execute(
            delete(NotebookEpochTail).where(
                NotebookEpochTail.org_id == ref.org_id,
                NotebookEpochTail.item_id == item_id,
                NotebookEpochTail.epoch <= epoch - TAILS_KEPT,
            )
        )

    async def on_write(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        author: User | None,
        agent_id: str | None,
        notes: Mapping[str, Any],
    ) -> None:
        """A person's update committed through the socket: record which cells
        they touched, for the edit notices and the activity digest."""
        touched = notes.get("touched")
        if author is None or not isinstance(touched, list) or not touched:
            return
        await record_edits(
            db,
            org_id=ref.org_id,
            item_id=UUID(ref.doc_id),
            cell_ids=[str(cell) for cell in touched],
            actor_key=f"user:{author.id}",
            actor_kind="person",
            user_id=author.id,
            agent_id=None,
            actor_display=author_display(author),
            submit_id=None,
            at=_now(),
        )


#: How many of a notebook's latest history restarts keep their old state.
TAILS_KEPT: Final = 2


register_type(NotebookDocType)


def author_display(user: User) -> str:
    """How a person is named to the others in a notebook."""
    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    named = f"{first} {last}".strip()
    return named or str(user.email)


__all__ = [
    "NORMALIZER_AGENT_ID",
    "NOTEBOOK_SUFFIX",
    "NotebookDocType",
    "author_display",
    "is_notebook_name",
]
