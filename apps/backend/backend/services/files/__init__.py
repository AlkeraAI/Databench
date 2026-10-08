"""Backend glue for Files: wiring only, never a decision.

Everything here binds a request to the library — the session as a
``FilesRepo``, the principal, the org's ``DomainStore`` and a clock — and turns
what the library returns into the wire payload. Business logic that finds its
way into this package belongs in ``alkera_core.files``.
"""

from __future__ import annotations

from backend.services.files.context import FilesContext, build_files_context, node_scope
from backend.services.files.decided import authorized_node
from backend.services.files.directory import member_names
from backend.services.files.items import to_item
from backend.services.files.lease_hold import beat_extends
from backend.services.files.live_documents import (
    NO_ACCESS,
    DocumentAccess,
    FolderHolder,
    HeadText,
    NotEditableError,
    NotLandedError,
    WriteBackRefusedError,
    Written,
    document_access,
    folder_holder,
    head_etag,
    holder_peer,
    keep_aside,
    read_head,
    write_back,
    write_back_as_holder,
    written_positions,
)
from backend.services.files.store import (
    admin_store,
    build_store_factory,
    set_store_factory,
    store_factory,
)

__all__ = [
    "NO_ACCESS",
    "DocumentAccess",
    "FilesContext",
    "FolderHolder",
    "HeadText",
    "NotEditableError",
    "NotLandedError",
    "WriteBackRefusedError",
    "Written",
    "admin_store",
    "authorized_node",
    "beat_extends",
    "build_files_context",
    "build_store_factory",
    "document_access",
    "folder_holder",
    "head_etag",
    "holder_peer",
    "keep_aside",
    "member_names",
    "node_scope",
    "read_head",
    "set_store_factory",
    "store_factory",
    "to_item",
    "write_back",
    "write_back_as_holder",
    "written_positions",
]
