"""What a held chat folder's push carries to the drive.

The drive is the only way a chat's state reaches the next box that serves it,
so the chat's own records travel: the manifest pins the agent session, the
agent database holds it, and the transcript, the decision log and the digest
over both are what the box reads back on resume. What describes one process on
one machine does not: the write lock, the agent's pid breadcrumb and loopback
URL, the database's side files, the agent's logs and the chat's Python
environment. The chat's working directory travels whole.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.files.walk import EntryKind, walk

#: The chat's records: what the next box needs to pick the chat up.
RECORDS = (
    "manifest.json",
    "trace.digest.json",
    "chat.jsonl",
    "decisions.jsonl",
    ".runtime/agent/agent.db",
)

#: The working directory: the chat's work, which the drive shows a person.
WORK = ("scratch/report.csv", "scratch/sub/notes.md", "scratch/.hidden-by-choice")

#: One process's state on one machine.
PROCESS_STATE = (
    ".lock",
    ".runtime/pid",
    ".runtime/listen-url",
    ".runtime/agent/agent.db-wal",
    ".runtime/agent/agent.db-shm",
    ".runtime/agent/log/2026-09-28.log",
    ".runtime/envs/alkera/bin/python",
)


def _chat_folder(root: Path) -> Path:
    for relative in (*RECORDS, *WORK, *PROCESS_STATE):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    return root


def _pushed(root: Path) -> set[str]:
    return {
        entry.relative.decode()
        for entry in walk(root, respect_gitignore=False, skip_local_state=True)
        if entry.kind is EntryKind.FILE and entry.skipped is None
    }


def test_the_push_carries_the_records_and_the_work_and_nothing_else(tmp_path: Path) -> None:
    assert _pushed(_chat_folder(tmp_path)) == {*RECORDS, *WORK}


@pytest.mark.parametrize("relative", [pytest.param(p, id=p) for p in PROCESS_STATE])
def test_process_state_never_rides_the_folder(tmp_path: Path, relative: str) -> None:
    assert relative not in _pushed(_chat_folder(tmp_path))


def test_a_person_pushing_their_own_tree_gets_every_byte(tmp_path: Path) -> None:
    """The rule is a held folder's; a plain push of the same bytes keeps them."""
    root = _chat_folder(tmp_path)
    pushed = {
        entry.relative.decode()
        for entry in walk(root, respect_gitignore=False)
        if entry.kind is EntryKind.FILE and entry.skipped is None
    }
    assert pushed == {*RECORDS, *WORK, *PROCESS_STATE}
