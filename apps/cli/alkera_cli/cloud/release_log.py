"""What the box's log says when a chat's folder (or a workspace's shared
tree) has been given back."""

from __future__ import annotations

import logging

from alkera_cli.cloud.folder import ReleasedFolder

logger = logging.getLogger("alkera_cli.cloud.service")


def log_released(chat_id: str, released: ReleasedFolder) -> None:
    """One line for a hand-back that landed. A tree the folder layer kept is
    said there, with its path, and nothing is added here."""
    if released.discarded:
        logger.info(
            "chat %s: it is gone from the workspace, so its folder %r was left in the trash "
            "and only its lease was released",
            chat_id,
            released.org_path,
        )
    elif not released.kept:
        pushed = released.push
        logger.info(
            "chat %s slept: its folder %r was pushed (%d file(s) uploaded, %d unchanged, "
            "%d folder(s), %d byte(s)) and released",
            chat_id,
            released.org_path,
            pushed.uploaded,
            pushed.unchanged,
            pushed.folders,
            pushed.bytes_uploaded,
        )


__all__ = ["log_released"]
