"""Saying, once per folder, each file a checkpoint push could not save.

A checkpoint push that leaves a file behind (no room on the drive for it, a
name the drive will not file) still saves the rest and keeps the lease, so it
runs again at the next turn's end and leaves the same file behind again. The
file is said the first time, under the chat it belongs to, and not on every
push after; the hand-back's release names it to the drive as what stayed on
the machine.
"""

from __future__ import annotations

import logging

from alkera_cli.files.push import PushSummary

__all__ = ["LeftBehind"]

#: Said under the custody's name, where the rest of a push is said.
logger = logging.getLogger("alkera_cli.cloud.folder")


class LeftBehind:
    def __init__(self) -> None:
        self._said: dict[str, set[str]] = {}

    def said(self, chat_id: str, summary: PushSummary) -> PushSummary:
        """Say each newly left-behind file of ``summary``; answer ``summary``."""
        said = self._said.setdefault(chat_id, set())
        for path in summary.failed:
            if path in said:
                continue
            said.add(path)
            why = next((w for w in summary.warnings if w.startswith(f"{path}: ")), path)
            logger.warning("chat %s: %s; it stays on this machine", chat_id, why)
        return summary
