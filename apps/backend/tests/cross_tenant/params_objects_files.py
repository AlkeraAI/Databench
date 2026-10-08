"""Workspace objects' and Files' entries in the cross-tenant parameter table.

Files names a file node ``item_id`` while the knowledge base uses the same name
for its own items, so the Files entry is scoped to its prefix."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from route_matrix import TwoOrgWorld

FILES = "/api/v1/files"
#: A notebook is a file node; its routes name it ``item_id`` under its drive.
NOTEBOOKS = "/api/v1/notebooks"

PARAM_OBJECTS: Mapping[str, Callable[[TwoOrgWorld], str]] = {
    "object_id": lambda w: w.a["object"],
    "drive_id": lambda w: w.a["drive"],
    "node_id": lambda w: w.a["file"],
    "operation_id": lambda w: w.a["operation"],
    "op_id": lambda w: w.a["operation"],
    "conflict_id": lambda w: w.a["conflict"],
    "share_id": lambda w: w.a["share"],
    "version_id": lambda w: w.a["version"],
}

SCOPED_PARAM_OBJECTS: Mapping[tuple[str, str], Callable[[TwoOrgWorld], str]] = {
    (FILES, "item_id"): lambda w: w.a["folder"],
    (FILES, "session_id"): lambda w: w.a["upload_session"],
    (NOTEBOOKS, "item_id"): lambda w: w.a["file"],
}

NOT_TENANT_PARAMS: Mapping[str, str] = {
    "part_no": "a part number inside the upload session the path already names",
    "item_path": "a path below the item the path already names",
    "sha256": "a content hash inside the notebook the path already names",
    "cell_id": "a cell inside the notebook the path already names",
    "env_id": "an environment of the notebook the path already names",
    "action": "the change asked of the notebook's environment (a fixed verb), not an object",
    "frame_id": "an output frame signed for one person and the notebook the path names",
}
