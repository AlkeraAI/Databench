"""The note a chat gets when a sleep for memory or disk stops what ran in it.

A box short on memory or disk may have to sleep a chat whose sandbox still runs
something a command left behind (a dev server, a long build). Sleeping it stops
those processes, and the reader is told so in the chat, never left to find the
work gone: the same principle as a background job a restart cut off.

The note is posted to the chat's transcript before the chat is released. It is
also kept in the chat's own records (beside its working directory, which the
agent cannot write) until it is known to be published, so a note whose post
did not land before the release is posted when the chat next wakes on this box.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Sequence
from pathlib import Path

from alkera_core.project import write_text_atomic

from alkera_cli.harness.sandbox_processes import SandboxProcess

#: The chat's record of notes still owed to its transcript.
PENDING_FILENAME = "pressure-sleep-notes.json"
#: How many process names the note spells out before it counts the rest.
_NAMED = 5


def pressure_sleep_sentence(resource: str, processes: Sequence[SandboxProcess]) -> str:
    """One sentence naming what the box was short of and what was stopped."""
    names = [
        os.path.basename(p.command.split()[0]) if p.command.strip() else f"pid {p.pid}"
        for p in processes
    ]
    count = len(names)
    shown = ", ".join(names[:_NAMED])
    if count > _NAMED:
        shown += f" and {count - _NAMED} more"
    noun = "process" if count == 1 else "processes"
    return (
        f"This chat was put to sleep because its box was short on {resource}, "
        f"which stopped {count} {noun} still running in it: {shown}."
    )


def _path(chat_folder: Path) -> Path:
    return Path(chat_folder) / PENDING_FILENAME


def pending_notes(chat_folder: Path) -> list[str]:
    """The notes still owed to the chat's transcript, oldest first."""
    try:
        data = json.loads(_path(chat_folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    return [n for n in notes if isinstance(n, str) and n] if isinstance(notes, list) else []


def owe_note(chat_folder: Path, note: str) -> None:
    """Record ``note`` as owed to the chat's transcript."""
    notes = [*pending_notes(chat_folder), note]
    path = _path(chat_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"notes": notes}) + "\n")


def settle_notes(chat_folder: Path) -> None:
    """Every owed note reached the transcript."""
    with contextlib.suppress(FileNotFoundError):
        _path(chat_folder).unlink()


__all__ = [
    "PENDING_FILENAME",
    "owe_note",
    "pending_notes",
    "pressure_sleep_sentence",
    "settle_notes",
]
