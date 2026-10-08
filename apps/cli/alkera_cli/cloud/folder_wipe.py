"""Removing a box's copy of a folder once it is back on the drive."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from alkera_cli.files.nodemap import node_map_path

logger = logging.getLogger(__name__)


def wipe_copy(root: Path, bound: Path, key: str, *, home: Path | None) -> None:
    """Leave nothing of the folder held under ``key`` on this box.

    Called only after the hand-back landed whole (or the chat is gone): the
    drive holds the work, and a box that kept the tree would carry one org's
    files into whatever it serves next. The next wake pulls the folder again
    as a fresh box would; the node map goes too. Refused unless ``root`` is
    strictly inside ``bound``, whatever the record says, so a bad path can
    never widen the delete. A wipe that fails is logged; the lease is already
    back and nothing about the chat depends on it.
    """
    try:
        inside = root.resolve().is_relative_to(bound.resolve())
    except OSError:
        inside = False
    if not inside or root.resolve() == bound.resolve():
        logger.error("chat %s: refused to wipe %s outside the chats root", key, root)
        return
    node_map_path(root, home=home).unlink(missing_ok=True)
    if not root.exists():
        return
    try:
        shutil.rmtree(root)
    except OSError as failed:
        logger.warning(
            "chat %s: its local copy was not removed after the hand-back (%s)", key, failed
        )


__all__ = ["wipe_copy"]
