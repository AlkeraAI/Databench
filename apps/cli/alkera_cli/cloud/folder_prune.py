"""Trashing on the drive what a box deleted from a chat folder it holds."""

from __future__ import annotations

import logging
import os
from collections.abc import Container
from pathlib import Path
from typing import Any

from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.files.mount import MountRecord, superseded_from
from alkera_cli.files.nodemap import NodeMap, load_node_map, save_node_map

__all__ = ["prune_deleted"]

#: Said under the custody's name, where the rest of a push is said.
logger = logging.getLogger("alkera_cli.cloud.folder")


def prune_deleted(
    *,
    chat_id: str,
    root: Path,
    record: MountRecord,
    api: Any,
    skip: Container[str],
    home: Path | None,
) -> int:
    """Trash on the drive every file this box agreed with it and has since
    deleted, so the folder the next box takes is the one the chat left.

    The push is a union: it uploads what is on disk and never deletes, so
    a file the agent removed while no live sync was watching (a restart, a
    watcher that missed the event) would come back on the next wake. The
    node map is the manifest of what the drive and this box last agreed on,
    file by file; an entry whose path is gone from this disk is a delete.

    Each trash is conditional on the etag both sides agreed, so a file a
    person changed on the web since is left alone (the drive answers 412),
    and one already gone (404) is simply forgotten. Only a file with a
    known node and an agreed etag is ever pruned: absence on its own proves
    nothing about a file this box never saw. A folder whose root is missing
    altogether is not pruned at all — that is a disk problem, not a
    deletion. A fenced refusal is the lease changing hands and is raised
    as the push would raise it; a network failure is raised for the retry.
    Returns how many nodes were trashed.
    """
    node_map = load_node_map(root, home=home)
    if node_map is None or not node_map.files or not root.is_dir():
        return 0
    forgotten: set[str] = set()
    trashed = 0
    for relative, known in sorted(node_map.files.items()):
        if not known.node_id or not known.etag or relative in skip:
            continue
        if os.path.lexists(root / relative):
            continue
        try:
            api.trash(record.drive_id, known.node_id, if_match=known.etag)
        except AlkeraHTTPError as refused:
            fenced = superseded_from(refused, record=record)
            if fenced is not None:
                raise fenced from refused
            if refused.status == 404:
                forgotten.add(relative)
                continue
            # 412: changed on the drive since this box agreed it — somebody
            # else's edit, which a delete here must not throw away. Anything
            # else is a refusal this box cannot argue with; either way the
            # entry stays so a later push can try again.
            logger.info(
                "chat %s: %s was deleted here but not on the drive (%s)",
                chat_id,
                relative,
                refused.status,
            )
            continue
        forgotten.add(relative)
        trashed += 1
    if forgotten:
        kept = {p: k for p, k in node_map.files.items() if p not in forgotten}
        save_node_map(root, NodeMap(files=kept), home=home)
    if trashed:
        logger.info("chat %s: trashed %d file(s) deleted on this box", chat_id, trashed)
    return trashed
