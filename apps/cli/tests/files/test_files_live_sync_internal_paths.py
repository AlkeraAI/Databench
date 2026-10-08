"""Nothing internal reaches the drive through the live sync.

The watched root is the chat's working directory, and the box's own state
sits beside it (the records, ``.runtime`` with the agent's database and the
Python environment, the container's overlay). The classifier refuses a path
under any of those names inside the root, a path that leaves the root (the
runtime state beside it, a link out of it), and the record names at the
root's top level, while the chat's own work still travels.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_cli.files.live_sync import LiveSync
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, make_sync


@pytest.fixture
def chat(tmp_path: Path) -> tuple[Path, LiveSync]:
    chat_dir = tmp_path / ".alkera" / "chats" / "chat-7"
    root = chat_dir / "scratch"
    root.mkdir(parents=True)
    (chat_dir / "manifest.json").write_text("{}")
    (chat_dir / ".runtime" / "envs" / "alkera" / "bin").mkdir(parents=True)
    (chat_dir / ".runtime" / "envs" / "alkera" / "bin" / "pip").write_text("#!python\n")
    (chat_dir / ".runtime" / "agent").mkdir()
    (chat_dir / ".runtime" / "agent" / "agent.db").write_bytes(b"SQLite")
    (chat_dir / ".overlay" / "upper").mkdir(parents=True)
    (chat_dir / ".overlay" / "upper" / "x").write_text("x")
    return root, make_sync(root, FakeLiveApi(root), FakeClock())


def _write(root: Path, relative: str, text: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".runtime/envs/alkera/bin/pip", id="an-environment-grown-in-the-root"),
        pytest.param(".runtime/listen-url", id="a-listen-file-in-the-root"),
        pytest.param(".overlay/upper/x", id="an-overlay-in-the-root"),
        pytest.param(".lock", id="the-write-lock"),
        pytest.param(".cache/pip/http/abc", id="a-tool-cache-grown-in-the-home"),
    ],
)
def test_an_internal_file_under_the_root_never_travels(
    chat: tuple[Path, LiveSync], relative: str
) -> None:
    root, sync = chat
    path = _write(root, relative)
    assert sync.classify(Change.added, str(path)) is None
    assert sync.pending == {}


@pytest.mark.parametrize(
    "beside",
    [
        pytest.param(".runtime/envs/alkera/bin/pip", id="the-environment-beside-the-root"),
        pytest.param(".runtime/agent/agent.db", id="the-database-beside-the-root"),
        pytest.param(".overlay/upper/x", id="the-overlay-beside-the-root"),
        pytest.param("manifest.json", id="the-record-beside-the-root"),
    ],
)
def test_a_file_beside_the_root_is_outside_it(chat: tuple[Path, LiveSync], beside: str) -> None:
    """The box's state sits one level above the watched root; a watcher pointed
    at it by mistake still carries none of it, because containment is decided
    first."""
    root, sync = chat
    assert sync.classify(Change.added, str(root.parent / beside)) is None
    assert sync.pending == {}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_link_from_the_root_to_the_environment_never_travels(
    chat: tuple[Path, LiveSync],
) -> None:
    root, sync = chat
    link = root / "pip-copy"
    link.symlink_to(root.parent / ".runtime" / "envs" / "alkera" / "bin" / "pip")
    assert sync.classify(Change.added, str(link)) is None
    assert sync.pending == {}


def test_the_chats_own_work_still_travels(chat: tuple[Path, LiveSync]) -> None:
    """The control that keeps the refusals above honest: a plain file, a nested
    one, and a file merely named like a record all queue. A record is a record
    at the CHAT FOLDER's top level only; the same name inside the working
    directory is the user's file (a project's own ``manifest.json``), and
    hiding it would lose their work."""
    root, sync = chat
    queued = []
    for relative in (
        "report.md",
        "data/q3/revenue.csv",
        "notes/manifest.json",
        "manifest.json",
        "chat.jsonl",
        "cost_ledger.jsonl",
    ):
        pending = sync.classify(Change.added, str(_write(root, relative)))
        assert pending is not None, relative
        queued.append(pending.relative)
    assert queued == [
        "report.md",
        "data/q3/revenue.csv",
        "notes/manifest.json",
        "manifest.json",
        "chat.jsonl",
        "cost_ledger.jsonl",
    ]
    assert set(sync.pending) == set(queued)
