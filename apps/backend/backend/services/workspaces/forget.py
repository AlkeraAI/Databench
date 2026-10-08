"""Take every workspace away while the schema stays at head: the data half of
rolling the workspaces release back.

A build from before workspaces lists objects by type and has no ``workspace``
type, so its unfiltered objects listing fails as soon as one workspace row
exists. Rolling the services back without downgrading the schema therefore
needs the rows gone, and this does exactly what the workspaces revision's
downgrade does to the data, leaving the schema alone:

1. every chat's ``workspace_id`` is cleared, so no chat names a row about to go;
2. every folder a workspace made as one owns is untagged into an ordinary
   folder, left in place with its contents;
3. every workspace row is deleted.

Each step walks its table a committed batch at a time. Run it while the new
build serves with ``WORKSPACES_ADOPTION_ENABLED`` off on every task (so nothing
re-adopts behind it), and only then roll the services back: the previous build
then never meets a workspace row, and the new build with adoption off serves a
chat with no workspace as it is. Re-running is safe, and rolling forward again
needs nothing else, since the reconcile pass adopts every chat once adoption is
back on.

Run from the backend image::

    python -m backend.services.workspaces.forget            # count only
    python -m backend.services.workspaces.forget --apply    # do it
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

#: Rows one committed statement touches.
BATCH: Final = 1000

_COUNT_CHATS = text(
    "SELECT count(*) FROM workspace_objects WHERE type = 'chat' AND spec ? 'workspace_id'"
)
_COUNT_FOLDERS = text("SELECT count(*) FROM file_nodes WHERE subtype = 'workspace'")
_COUNT_WORKSPACES = text("SELECT count(*) FROM workspace_objects WHERE type = 'workspace'")

_FORGET_CHATS = text(
    """
    UPDATE workspace_objects SET spec = spec - 'workspace_id'
     WHERE id IN (
        SELECT id FROM workspace_objects
         WHERE type = 'chat' AND spec ? 'workspace_id'
         ORDER BY id LIMIT :batch
     )
    """
)
_UNTAG_FOLDERS = text(
    """
    UPDATE file_nodes SET subtype = NULL, target_object_id = NULL
     WHERE id IN (
        SELECT id FROM file_nodes WHERE subtype = 'workspace' ORDER BY id LIMIT :batch
     )
    """
)
_DELETE_WORKSPACES = text(
    """
    DELETE FROM workspace_objects
     WHERE id IN (
        SELECT id FROM workspace_objects WHERE type = 'workspace' ORDER BY id LIMIT :batch
     )
    """
)


@dataclass(frozen=True, slots=True)
class Forgotten:
    """What a run found (``apply=False``) or changed."""

    chats: int
    folders: int
    workspaces: int


async def _unfiltered(session: AsyncSession) -> None:
    """Read and write every org's rows. A login that row security would still
    filter raises here rather than matching nothing and reporting success."""
    await session.execute(text("SET LOCAL row_security = off"))


async def _count(sessions: Callable[[], AsyncSession]) -> Forgotten:
    async with sessions() as session:
        await _unfiltered(session)
        chats = await session.scalar(_COUNT_CHATS)
        folders = await session.scalar(_COUNT_FOLDERS)
        workspaces = await session.scalar(_COUNT_WORKSPACES)
    return Forgotten(
        chats=int(chats or 0), folders=int(folders or 0), workspaces=int(workspaces or 0)
    )


async def _drain(sessions: Callable[[], AsyncSession], statement: TextClause, batch: int) -> int:
    touched = 0
    while True:
        async with sessions() as session:
            await _unfiltered(session)
            result = await session.execute(statement, {"batch": batch})
            await session.commit()
        done = int(getattr(result, "rowcount", 0) or 0)
        touched += done
        if done < batch:
            return touched


async def forget_workspaces(
    sessions: Callable[[], AsyncSession] = AsyncSessionLocal,
    *,
    apply: bool,
    batch: int = BATCH,
) -> Forgotten:
    """Count, or with ``apply`` take away, every workspace (see the module
    docstring for the order and why)."""
    if not apply:
        return await _count(sessions)
    chats = await _drain(sessions, _FORGET_CHATS, batch)
    folders = await _drain(sessions, _UNTAG_FOLDERS, batch)
    workspaces = await _drain(sessions, _DELETE_WORKSPACES, batch)
    return Forgotten(chats=chats, folders=folders, workspaces=workspaces)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m backend.services.workspaces.forget",
        description="Take every workspace away, leaving the schema at head.",
    )
    parser.add_argument("--apply", action="store_true", help="change the rows (default: count)")
    args = parser.parse_args(argv)
    done = asyncio.run(forget_workspaces(apply=args.apply))
    verb = "cleared" if args.apply else "would clear"
    sys.stdout.write(
        f"{verb}: {done.chats} chat references, {done.folders} workspace folders, "
        f"{done.workspaces} workspace rows\n"
    )


if __name__ == "__main__":
    main()
