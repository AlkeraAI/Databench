"""What never travels with a chat folder, and what must.

The predicate is the single spelling both halves of a transfer read, so its
edges are pinned here rather than at each call site.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alkera_core.project import (
    is_local_state,
    local_state,
    local_state_patterns,
    prune_stale_locks,
    register_local_state,
)
from alkera_core.project.locking import FileLock, LockHeldError


@pytest.mark.parametrize(
    ("relative", "local"),
    [
        pytest.param(".lock", True, id="the-write-lock"),
        pytest.param(".lock.stale.1789500172.71e1df99", True, id="a-forensic-rotation"),
        pytest.param(".lock.reclaim", True, id="the-reclaim-guard"),
        pytest.param(".lock.reclaim.stale.1789500172.ab", True, id="a-rotated-guard"),
        pytest.param("blobs.gc.lock", True, id="the-blob-gc-lock"),
        pytest.param(b".lock", True, id="bytes-as-the-walkers-carry-them"),
        pytest.param("/.lock", True, id="a-leading-separator-is-not-a-different-path"),
        pytest.param(".runtime", False, id="the-harness-state-the-manifest-pins"),
        pytest.param(".runtime/agent/agent.db", False, id="inside-the-harness-state"),
        pytest.param(".runtime/envs", True, id="the-chats-python-environments"),
        pytest.param(
            ".runtime/envs/alkera/lib/python3.12/site-packages/pip/__init__.py",
            True,
            id="a-file-of-the-default-environment",
        ),
        pytest.param(
            "scratch/.runtime/envs/alkera/bin/python",
            True,
            id="the-environment-seen-from-the-chat-folder",
        ),
        pytest.param(".runtime/envsx", False, id="a-longer-name-with-the-environments-prefix"),
        pytest.param(".runtime/agent/agent.db-wal", True, id="the-agent-databases-write-ahead-log"),
        pytest.param(".runtime/agent/agent.db-shm", True, id="the-agent-databases-shared-memory"),
        pytest.param(".runtime/agent/agent.db-journal", True, id="a-rollback-journal"),
        pytest.param(
            "scratch/.runtime/agent/agent.db-wal",
            True,
            id="the-write-ahead-log-seen-from-the-chat-folder",
        ),
        pytest.param(".runtime/agent/log", True, id="the-agents-log-directory"),
        pytest.param(
            ".runtime/agent/log/2026-09-28T035937.log", True, id="one-of-the-agents-log-files"
        ),
        pytest.param(
            ".runtime/agent/storage/session_diff/ses_1.json",
            False,
            id="the-agents-session-storage-travels",
        ),
        pytest.param(".runtime/agent/agent.db-walx", False, id="a-longer-name-with-the-log-suffix"),
        pytest.param(".runtime/agent/logbook.md", False, id="a-file-the-agent-named-logbook"),
        pytest.param(".overlay", True, id="the-containers-root-overlay"),
        pytest.param(".overlay/usr/lib/python3/dist-packages/x.py", True, id="inside-the-overlay"),
        pytest.param(".overlays", False, id="a-longer-name-with-the-overlay-prefix"),
        pytest.param("scratch/overlay/notes.md", False, id="a-folder-the-agent-named-overlay"),
        pytest.param("chat.jsonl", False, id="the-transcript"),
        pytest.param("manifest.json", False, id="the-manifest"),
        pytest.param("sandbox/.lock", False, id="a-file-the-agent-wrote-in-its-sandbox"),
        pytest.param("outputs/lock", False, id="a-name-that-merely-contains-lock"),
        pytest.param(".lockfile", False, id="a-longer-name-with-the-same-prefix"),
        pytest.param("", False, id="nothing"),
    ],
)
def test_what_belongs_to_this_machine_and_what_belongs_to_the_chat(
    relative: str | bytes, local: bool
) -> None:
    assert is_local_state(relative) is local


def test_a_name_that_is_not_utf8_still_answers() -> None:
    """A path on disk is bytes, and the walker hands them over undecoded."""
    assert is_local_state(b"\xff\xfe.txt") is False
    assert is_local_state(os.fsencode(".lock")) is True


def test_the_answer_does_not_depend_on_the_hosts_filesystem_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`os.fsdecode` uses `sys.getfilesystemencodeerrors()`, which is
    surrogateescape on POSIX but surrogatepass on Windows — and surrogatepass
    raises on the very bytes a walker has to ask about. The same folder synced
    from either host must get the same answer, so stand in a filesystem codec
    that refuses and nothing moves.
    """

    def refuses(*_args: object, **_kwargs: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(os, "fsdecode", refuses)

    assert is_local_state(b"\xff\xfe.txt") is False
    assert is_local_state(b".lock") is True
    assert is_local_state(b".lock.stale.\xff") is True


def test_new_local_state_arrives_by_registration_not_by_a_second_list() -> None:
    """The seam: a future holder of per-machine state names its own paths and
    both the push and the pull follow, with no third place to keep in step."""
    assert ".runtime/future-breadcrumb" not in local_state_patterns()
    assert is_local_state(".runtime/future-breadcrumb") is False
    register_local_state(".runtime/future-breadcrumb")
    try:
        assert is_local_state(".runtime/future-breadcrumb") is True
        assert ".runtime/future-breadcrumb" in local_state_patterns()
        assert is_local_state(".runtime/agent/agent.db") is False
    finally:
        local_state.unregister_local_state(".runtime/future-breadcrumb")
    assert is_local_state(".runtime/future-breadcrumb") is False


def test_a_pattern_that_names_no_path_is_refused() -> None:
    with pytest.raises(ValueError, match="must name a path"):
        register_local_state("   ")


def test_the_prune_removes_only_a_dead_holders_rotation(tmp_path: Path) -> None:
    """A rotation is by construction the payload of a holder the reclaim had
    already judged dead, so there is no liveness question left. The live lock
    beside it, and anything else that merely looks similar, is untouched."""
    (tmp_path / ".lock.stale.1789500172.71e1df99").write_text("{}", encoding="utf-8")
    (tmp_path / ".lock.stale.1789500243.55f29dcc").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.stale.txt").write_text("mine", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    lock = FileLock(tmp_path / ".lock")
    lock.acquire()
    try:
        removed = prune_stale_locks(tmp_path)

        assert removed == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == [".lock", "notes.stale.txt", "sub"]
        # The lock is still held: pruning is not a reclaim by another name.
        with pytest.raises(LockHeldError):
            FileLock(tmp_path / ".lock").acquire()
    finally:
        lock.release()


def test_pruning_a_directory_that_is_not_there_removes_nothing(tmp_path: Path) -> None:
    assert prune_stale_locks(tmp_path / "gone") == 0
