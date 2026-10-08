"""Which live document type a file on the drive is co-edited as.

A file has exactly one live document: a ``.alknb.py`` notebook is a
``notebook`` document (a tree of cells), anything else a ``file`` document
(one text). Two documents writing one file back would each merge the other's
write backs as outside changes, so each type refuses the other's files.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from alkera_core.files.ids import NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from sqlalchemy.ext.asyncio import AsyncSession

#: The file name every notebook carries.
NOTEBOOK_SUFFIX: Final = ".alknb.py"


def is_notebook_name(name: str) -> bool:
    return name.endswith(NOTEBOOK_SUFFIX) and len(name) > len(NOTEBOOK_SUFFIX)


def name_text(raw: object) -> str:
    """A drive name (kept as bytes: a filename need not be UTF-8) as text."""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def live_type_of(name: str) -> str:
    """The live document type a file of this name is co-edited as."""
    return "notebook" if is_notebook_name(name) else "file"


async def file_name(db: AsyncSession, org_id: UUID, doc_id: str) -> str | None:
    """The name of the live file ``doc_id`` names in ``org_id``, or ``None``
    when it names no file there."""
    try:
        node_id = UUID(doc_id)
    except ValueError:
        return None
    repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
    if node is None or node.kind != "file":
        return None
    return name_text(node.name)


__all__ = ["NOTEBOOK_SUFFIX", "file_name", "is_notebook_name", "live_type_of", "name_text"]
