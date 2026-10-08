"""The walk never leaves its root, even while the tree changes under it.

On a shared box the daemon walks each chat's folder as root, and the chat's
agent owns that folder. Between the moment the walk classifies a name and the
moment it opens it, the agent can put something else at that name: a symlink
to ``/`` where a directory was, a fifo where a regular file was. These tests
make that swap at a fixed point instead of racing for it, and pin that the
walk then neither enumerates anything outside the root nor blocks.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.files.walk import Entry, EntryKind, ExportRules, SkipReason, walk

walk_module = importlib.import_module("alkera_cli.files.walk")

needs_posix_links_and_fifos = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "mkfifo"),
    reason="needs POSIX symlinks and fifos",
)
needs_seek_hole = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "SEEK_HOLE") or not hasattr(os, "mkfifo"),
    reason="only a platform with SEEK_HOLE opens a file to ask whether it is sparse",
)

#: Long enough for a loaded runner, short enough that a hang fails the test.
_HANG_LIMIT_S = 10.0


def _outside(tmp_path: Path) -> Path:
    """A directory beside the root holding names the walk must never report."""
    outside = tmp_path / "other-tenant"
    (outside / "private").mkdir(parents=True)
    (outside / "private" / "secret.txt").write_bytes(b"not yours\n")
    (outside / "payroll.csv").write_bytes(b"x\n")
    return outside


def _swap_for_link(target: Path, outside: Path) -> None:
    """What the agent does: move the directory aside and link its name out."""
    target.rename(target.with_name(target.name + ".moved"))
    os.symlink(outside, target)


def _escaped(entries: list[Entry]) -> list[bytes]:
    outside_names = {b"private", b"secret.txt", b"payroll.csv"}
    return [e.relative for e in entries if set(e.relative.split(b"/")) & outside_names]


def _walk_in_thread(run: Callable[[], list[Entry]], unblock: Callable[[], None]) -> list[Entry]:
    """Run a walk with a deadline; on a hang, unblock it so the thread can end."""
    result: list[list[Entry]] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(run())
        except BaseException as exc:  # surfaced to the test below
            errors.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(_HANG_LIMIT_S)
    hung = worker.is_alive()
    if hung:
        unblock()
        worker.join(_HANG_LIMIT_S)
    assert not hung, "the walk blocked on a name an agent swapped for a fifo"
    if errors:
        raise errors[0]
    return result[0]


def _release_fifo(path: Path) -> Callable[[], None]:
    """Open the fifo's write end so a reader blocked in ``open`` returns."""

    def unblock() -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.close(fd)

    return unblock


@needs_posix_links_and_fifos
def test_a_directory_swapped_for_a_link_after_it_is_yielded_is_not_followed(
    tmp_path: Path,
) -> None:
    """The consumer runs between the directory's entry and its descent, so it
    is the agent's window in the plain case: the walk must keep listing the
    directory it classified, never whatever the name points at now."""
    outside = _outside(tmp_path)
    root = tmp_path / "chat"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "inner.txt").write_bytes(b"mine\n")

    entries: list[Entry] = []
    for entry in walk(root, respect_gitignore=False):
        entries.append(entry)
        if entry.relative == b"sub":
            _swap_for_link(root / "sub", outside)

    assert _escaped(entries) == []
    assert b"sub/inner.txt" in {e.relative for e in entries}


@needs_posix_links_and_fifos
def test_a_directory_swapped_for_a_link_between_its_stat_and_its_open_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow window: the name is stat'ed as a directory, then replaced by
    a link to another tenant's folder before the walk opens it. The local-state
    check runs inside that window, so it is where the swap is made."""
    outside = _outside(tmp_path)
    root = tmp_path / "chat"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "inner.txt").write_bytes(b"mine\n")
    real_is_local_state = walk_module.is_local_state

    def swap_then_answer(relative: bytes) -> bool:
        if relative == b"sub" and not (root / "sub").is_symlink():
            _swap_for_link(root / "sub", outside)
        return bool(real_is_local_state(relative))

    monkeypatch.setattr(walk_module, "is_local_state", swap_then_answer)
    entries = list(walk(root, respect_gitignore=False, skip_local_state=True))

    assert _escaped(entries) == []
    assert all(not e.relative.startswith(b"sub/") for e in entries)


@needs_seek_hole
def test_a_file_swapped_for_a_fifo_before_it_is_opened_does_not_hang_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name stat'ed as a regular file and then replaced by a fifo is not
    opened in a way that waits for a writer, and is not reported as the file
    it no longer is: the next walk sees it as what it is."""
    root = tmp_path / "chat"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    data = root / "data.bin"
    data.write_bytes(b"payload")
    real_is_local_state = walk_module.is_local_state

    def swap_then_answer(relative: bytes) -> bool:
        if relative == b"data.bin" and data.is_file():
            data.unlink()
            os.mkfifo(data)
        return bool(real_is_local_state(relative))

    monkeypatch.setattr(walk_module, "is_local_state", swap_then_answer)
    entries = _walk_in_thread(
        lambda: list(walk(root, respect_gitignore=False, skip_local_state=True)),
        _release_fifo(data),
    )

    by_name = {e.relative: e for e in entries}
    assert set(by_name) == {b"a.txt"}
    again = {e.relative: e.kind for e in walk(root, respect_gitignore=False)}
    assert again[b"data.bin"] is EntryKind.SPECIAL


def _git_tree(tmp_path: Path) -> Path:
    root = tmp_path / "chat"
    (root / ".git").mkdir(parents=True)
    (root / "secret.txt").write_bytes(b"s\n")
    (root / "kept.txt").write_bytes(b"k\n")
    return root


@needs_posix_links_and_fifos
def test_a_gitignore_that_is_a_link_out_of_the_root_is_not_read(tmp_path: Path) -> None:
    """Following it would let an agent learn whether a root-only file exists
    and what glob-like lines it holds, one pushed name at a time."""
    root = _git_tree(tmp_path)
    elsewhere = tmp_path / "root-only"
    elsewhere.write_bytes(b"secret*\n")
    os.symlink(elsewhere, root / ".gitignore")

    entries = {e.relative: e for e in walk(root)}
    rules = ExportRules.load(root)

    assert entries[b"secret.txt"].skipped is None
    assert rules.skip_reason(b"secret.txt", is_dir=False) is None


@needs_posix_links_and_fifos
def test_a_regular_gitignore_still_folds_what_it_names(tmp_path: Path) -> None:
    """The negative twin: the refusal is about links, not about .gitignore."""
    root = _git_tree(tmp_path)
    (root / ".gitignore").write_bytes(b"secret*\n")

    entries = {e.relative: e for e in walk(root)}

    assert entries[b"secret.txt"].skipped is SkipReason.GITIGNORED
    assert entries[b"kept.txt"].skipped is None


@needs_posix_links_and_fifos
def test_a_gitignore_that_is_a_fifo_does_not_hang_the_rules(tmp_path: Path) -> None:
    root = _git_tree(tmp_path)
    fifo = root / ".gitignore"
    os.mkfifo(fifo)

    entries = _walk_in_thread(lambda: list(walk(root)), _release_fifo(fifo))

    by_name = {e.relative: e for e in entries}
    assert by_name[b"secret.txt"].skipped is None
    assert by_name[b".gitignore"].kind is EntryKind.SPECIAL


def test_the_plain_walk_still_reports_every_name_in_order(tmp_path: Path) -> None:
    """Cross-platform: the hardened walk is the same walk on an ordinary tree."""
    root = tmp_path / "chat"
    (root / "b" / "c").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"a")
    (root / "b" / "c" / "d.txt").write_bytes(b"d")

    assert [e.relative for e in walk(root, respect_gitignore=False)] == [
        b"a.txt",
        b"b",
        b"b/c",
        b"b/c/d.txt",
    ]


def test_the_by_path_walk_windows_uses_reports_the_same_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no directory descriptors to walk from, so it keeps the walk
    by path. Driven here on whatever platform runs the suite, it must report
    the same entries in the same order as the walk by descriptor."""
    root = tmp_path / "chat"
    (root / "b" / "c").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"a")
    (root / "b" / "c" / "d.txt").write_bytes(b"dd")
    (root / ".DS_Store").write_bytes(b"")

    def described(entries: list[Entry]) -> list[tuple[bytes, EntryKind, int, SkipReason | None]]:
        return [(e.relative, e.kind, e.size, e.skipped) for e in entries]

    by_descriptor = described(list(walk(root, respect_gitignore=False)))
    monkeypatch.setattr(walk_module, "_FD_WALK", False)
    by_path = described(list(walk(root, respect_gitignore=False)))

    assert (
        by_path
        == by_descriptor
        == [
            (b".DS_Store", EntryKind.FILE, 0, SkipReason.SIDECAR),
            (b"a.txt", EntryKind.FILE, 1, None),
            (b"b", EntryKind.DIRECTORY, by_path[2][2], None),
            (b"b/c", EntryKind.DIRECTORY, by_path[3][2], None),
            (b"b/c/d.txt", EntryKind.FILE, 2, None),
        ]
    )
