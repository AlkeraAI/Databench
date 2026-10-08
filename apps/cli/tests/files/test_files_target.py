"""The pull-side materialization target, driven with the corpus the walk produced."""

from __future__ import annotations

import os
import stat as stat_module
import sys
from pathlib import Path

import pytest
from _tree_fixture import Corpus, build_corpus
from alkera_cli.files import target as target_module
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.target import ContainmentError, MaterializationTarget
from alkera_cli.files.walk import EntryKind, walk
from alkera_cli.files.xattrs import XATTRS_SUPPORTED, read_xattrs, write_xattrs
from alkera_core.files.links import LinkKind

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the target corpus needs byte-exact names and POSIX modes the Windows runner does not offer"
    ),
)


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return build_corpus(tmp_path / "tree")


def test_a_walked_tree_materializes_with_its_modes_mtimes_and_link_targets(
    corpus: Corpus, tmp_path: Path
) -> None:
    """The round trip: push the corpus, pull it into a fresh root, compare stat."""
    os.chmod(corpus.root / "tagged.txt", 0o640)
    os.utime(corpus.root / "tagged.txt", ns=(1_700_000_000_000_000_111, 1_700_000_000_000_000_111))
    entries = [entry for entry in walk(corpus.root) if entry.skipped is None]

    destination = tmp_path / "pulled"
    with MaterializationTarget(destination) as target:
        for entry in entries:
            if entry.kind is EntryKind.DIRECTORY:
                target.mkdir(entry.relative)
            elif entry.kind is EntryKind.FILE:
                source = os.fsencode(corpus.root) + b"/" + entry.relative
                with open(source, "rb") as handle:
                    target.write_bytes(entry.relative, handle.read())
            elif entry.kind is EntryKind.SYMLINK:
                assert entry.link_kind is not None
                assert entry.link_target is not None
                target.write_symlink(entry.relative, entry.link_kind, entry.link_target)
        for entry in entries:
            if entry.kind in (EntryKind.DIRECTORY, EntryKind.FILE):
                target.set_attrs(
                    entry.relative, mode=entry.mode, mtime_ns=entry.mtime_ns, xattrs=entry.xattrs
                )

    pulled = os.stat(destination / "tagged.txt")
    assert stat_module.S_IMODE(pulled.st_mode) == 0o640
    assert pulled.st_mtime_ns == 1_700_000_000_000_000_111
    assert (destination / "papers" / "note.txt").read_bytes() == b"note bytes\n"
    assert (destination / "empty").is_dir()
    assert os.readlink(destination / "links" / "relative") == "../papers/note.txt"
    assert os.readlink(destination / "links" / "host") == "/usr/bin/python3"


def test_a_canonical_link_is_rewritten_to_the_new_local_root(
    corpus: Corpus, tmp_path: Path
) -> None:
    """The exact inverse of the walk: pulled into root B it points inside root B."""
    entry = next(item for item in walk(corpus.root) if item.relative == b"links/canonical")
    assert entry.link_kind is LinkKind.CANONICAL
    assert entry.link_target is not None

    destination = tmp_path / "root-b"
    with MaterializationTarget(destination) as target:
        target.mkdir(b"papers")
        target.write_bytes(b"papers/note.txt", b"note bytes\n")
        target.write_symlink(entry.relative, entry.link_kind, entry.link_target)

    link = destination / "links" / "canonical"
    assert os.readlink(link) == f"{destination}/papers/note.txt"
    assert link.resolve().read_bytes() == b"note bytes\n"
    # and it no longer names the machine it was pushed from
    assert str(corpus.root) not in os.readlink(link)


def test_a_symlink_is_written_after_the_file_it_points_at(tmp_path: Path) -> None:
    """Queued, not immediate: no window where the link dangles for a reader."""
    destination = tmp_path / "ordered"
    with MaterializationTarget(destination) as target:
        target.write_symlink(b"link", LinkKind.RELATIVE, b"payload.txt")
        target.write_bytes(b"payload.txt", b"payload\n")
        assert (destination / "payload.txt").exists()
        assert not (destination / "link").is_symlink()
    assert (destination / "link").is_symlink()
    assert (destination / "link").read_bytes() == b"payload\n"


def test_a_failed_pull_leaves_no_half_written_links(tmp_path: Path) -> None:
    """The flush is on the clean exit only, so an aborted pull writes no link."""
    destination = tmp_path / "aborted"
    with pytest.raises(RuntimeError):
        with MaterializationTarget(destination) as target:
            target.write_symlink(b"link", LinkKind.RELATIVE, b"payload.txt")
            raise RuntimeError("upload died")
    assert not (destination / "link").exists()


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(b"../escape.txt", id="parent-segment"),
        pytest.param(b"a/../../escape.txt", id="parent-segment-mid-path"),
        pytest.param(b"/etc/passwd", id="absolute"),
        pytest.param(b"a//b", id="empty-segment"),
        pytest.param(b"a\x00b", id="nul"),
        pytest.param(b"", id="empty"),
    ],
)
def test_escaping_paths_are_refused_and_write_nothing(tmp_path: Path, relative: bytes) -> None:
    """Refused lexically: the sibling directory is untouched afterwards."""
    outside = tmp_path / "escape.txt"
    destination = tmp_path / "contained"
    target = MaterializationTarget(destination)
    with pytest.raises(ContainmentError):
        target.write_bytes(relative, b"pwned")
    assert not outside.exists()
    assert list(destination.iterdir()) == []


def test_a_symlinked_component_cannot_be_used_as_a_door_out(tmp_path: Path) -> None:
    """A link written earlier in the pull must not redirect a later write."""
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "contained"
    target = MaterializationTarget(destination)
    os.symlink(outside, destination / "door")

    with pytest.raises(ContainmentError):
        target.write_bytes(b"door/loot.txt", b"pwned")
    assert not (outside / "loot.txt").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "setuid is a POSIX bit Windows does not have: os.chmod there honours "
        "only the read-only flag, so neither the stripped nor the trusted mode "
        "can be observed and there is nothing to strip."
    ),
)
def test_setuid_is_stripped_unless_the_target_is_trusted(tmp_path: Path) -> None:
    """A pushed setuid binary must not become one on a shared box."""
    untrusted = MaterializationTarget(tmp_path / "shared")
    untrusted.write_bytes(b"suid", b"#!/bin/sh\n")
    untrusted.set_attrs(b"suid", mode=0o4755)
    assert stat_module.S_IMODE(os.stat(tmp_path / "shared" / "suid").st_mode) == 0o755

    trusted = MaterializationTarget(tmp_path / "own", trusted=True)
    trusted.write_bytes(b"suid", b"#!/bin/sh\n")
    trusted.set_attrs(b"suid", mode=0o4755)
    assert stat_module.S_IMODE(os.stat(tmp_path / "own" / "suid").st_mode) == 0o4755


@pytest.mark.skipif(not XATTRS_SUPPORTED, reason="the platform has no extended attributes")
def test_user_xattrs_are_restored_and_a_privileged_namespace_is_not(tmp_path: Path) -> None:
    target = MaterializationTarget(tmp_path / "attrs")
    target.write_bytes(b"repo.txt", b"repo\n")
    target.set_attrs(
        b"repo.txt",
        xattrs={b"user.alkera.git.branch": b"main", b"trusted.evil": b"no"},
    )
    written = read_xattrs(tmp_path / "attrs" / "repo.txt")
    assert written == {b"user.alkera.git.branch": b"main"}


@pytest.mark.skipif(not XATTRS_SUPPORTED, reason="the platform has no extended attributes")
def test_xattrs_are_read_from_the_link_not_its_target(tmp_path: Path) -> None:
    """follow_symlinks=False on both halves, or a link would leak its target's tags."""
    root = tmp_path / "nofollow"
    root.mkdir()
    (root / "real.txt").write_bytes(b"real\n")
    write_xattrs(root / "real.txt", {b"user.tag": b"target"})
    os.symlink("real.txt", root / "link")
    assert read_xattrs(root / "link") == {}


def test_an_existing_file_is_replaced_atomically_not_edited_in_place(tmp_path: Path) -> None:
    """A re-pull of a changed file leaves a whole file, never a mix."""
    target = MaterializationTarget(tmp_path / "twice")
    first = target.write_bytes(b"doc.txt", b"a" * 64)
    original_inode = first.stat().st_ino
    second = target.write_bytes(b"doc.txt", b"b" * 8)
    assert second.read_bytes() == b"b" * 8
    assert second.stat().st_ino != original_inode
    assert list((tmp_path / "twice").iterdir()) == [second]


def test_a_pointer_file_is_written_as_json(tmp_path: Path) -> None:
    """Row-backed objects materialize as a small pointer, not as bytes."""
    import json

    target = MaterializationTarget(tmp_path / "pointers")
    written = target.write_pointer(
        b"Analysis.alkerachat",
        {"kind": "chat", "node_id": "n1", "schema_version": "1.0.0"},
    )
    assert json.loads(written.read_text(encoding="utf-8"))["node_id"] == "n1"


def _stamped(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mtime_ns


def _as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``os.utime`` behave the way it does on Windows.

    ``os.utime`` is not in ``os.supports_follow_symlinks`` there, and passing
    the flag anyway raises ``NotImplementedError`` -- the two halves have to be
    faked together, or a POSIX host keeps accepting the call that is the bug.
    """
    real = os.utime

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        if "follow_symlinks" in kwargs:
            raise NotImplementedError("utime: follow_symlinks unavailable on this platform")
        real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "supports_follow_symlinks", frozenset())
    monkeypatch.setattr(os, "utime", refuse)


def test_a_pull_finishes_on_a_platform_that_cannot_stamp_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' ``os.utime`` refuses ``follow_symlinks=False``.

    It is not in ``os.supports_follow_symlinks`` there, so the flag raises
    ``NotImplementedError`` -- which is not an ``OSError``, so it walked past
    the pull's attribute-restore guard and took the whole materialisation down
    on every tree, link or no link. Stamping must go through on the entries the
    platform can stamp, and the file a link points at must keep the mtime it
    had: a link entry does not own its target's timestamp.
    """
    target = MaterializationTarget(tmp_path / "tree")
    target.mkdir(b"papers")
    target.write_bytes(b"papers/real.txt", b"real\n")
    real = tmp_path / "tree" / "papers" / "real.txt"
    os.utime(real, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    before = _stamped(real)
    target.write_symlink(b"papers/link", LinkKind.RELATIVE, b"real.txt")
    target.flush_links()

    _as_windows(monkeypatch)
    target.set_attrs(b"papers", mtime_ns=1_500_000_000_000_000_000)

    assert _stamped(tmp_path / "tree" / "papers") == 1_500_000_000_000_000_000
    assert _stamped(real) == before, "a link's stamp was written through to its target"
    assert (tmp_path / "tree" / "papers" / "link").is_symlink()


def test_a_regular_file_is_still_stamped_where_links_cannot_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform guard is about links only.

    Dropping the mtime for every path would make git call the whole pulled
    tree racy, so a file and a directory keep their restored timestamp on a
    platform that cannot stamp a link.
    """
    target = MaterializationTarget(tmp_path / "tree")
    target.write_bytes(b"repo.txt", b"repo\n")
    _as_windows(monkeypatch)

    target.set_attrs(b"repo.txt", mtime_ns=1_234_567_891_000_000_000)

    assert _stamped(tmp_path / "tree" / "repo.txt") == 1_234_567_891_000_000_000


def test_a_canonical_link_is_re_anchored_the_way_windows_spells_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull half of the same trip, on the platform that prefixes its roots.

    An org path joined onto the root with `/` is not a path a Windows kernel
    resolves the way the pusher meant, and a root still carrying its
    extended-length prefix puts bytes in the link text that nothing else on
    the machine spells — so both are the link pointing somewhere else.
    """
    monkeypatch.setattr(target_module, "_WINDOWS", True)
    destination = tmp_path / "root-b"

    with MaterializationTarget(destination, local_root=rb"\\?\C:\Users\runner\root-b") as target:
        target.write_symlink(b"links/canonical", LinkKind.CANONICAL, b"/data/notes.txt")

    link = destination / "links" / "canonical"
    assert os.readlink(link) == r"C:\Users\runner\root-b\data\notes.txt"


def test_a_relative_link_is_still_written_verbatim_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative twin: only a canonical target is ever rewritten."""
    monkeypatch.setattr(target_module, "_WINDOWS", True)
    destination = tmp_path / "root-c"

    with MaterializationTarget(destination, local_root=rb"C:\Users\runner\root-c") as target:
        target.write_symlink(b"links/relative", LinkKind.RELATIVE, rb"..\data\notes.txt")

    assert os.readlink(destination / "links" / "relative") == r"..\data\notes.txt"


@pytest.mark.parametrize(
    ("relative", "kind", "stored", "written"),
    [
        pytest.param(
            b"files/dl-host-auth", LinkKind.HOST, b"/opt/alkera-home/auth.yml", False, id="host"
        ),
        pytest.param(b"files/up", LinkKind.RELATIVE, b"../../outside", False, id="relative-escape"),
        pytest.param(
            b"files/sibling", LinkKind.RELATIVE, b"../other.txt", True, id="relative-inside"
        ),
        pytest.param(
            b"files/canon", LinkKind.CANONICAL, b"/files/other.txt", True, id="canonical-inside"
        ),
        pytest.param(
            b"files/canon-up", LinkKind.CANONICAL, b"/../etc/passwd", False, id="canonical-escape"
        ),
    ],
)
def test_a_box_never_writes_a_link_that_escapes_its_tree(
    tmp_path: Path, relative: bytes, kind: LinkKind, stored: bytes, written: bool
) -> None:
    """A box materialized a workspace link pointing at its own credential
    (``files/dl-host-auth -> /opt/alkera-home/auth.yml``) inside a tree it reads
    and uploads. A box writes through the chat tree, which keeps every link
    inside it: an escaping one is not written and is named in ``skipped_links``."""
    root = tmp_path / "box"
    root.mkdir()
    with MaterializationTarget(root, tree=ChatTree(root)) as target:
        target.mkdir(b"files")
        target.write_symlink(relative, kind, stored)
    leaf = root / os.fsdecode(relative)
    assert leaf.is_symlink() is written
    assert (relative in [name for name, _reason in target.skipped_links]) is not written


def test_a_person_pulling_their_own_drive_keeps_every_link(tmp_path: Path) -> None:
    """The negative twin: a person's own pull has no chat tree, and a host link
    is written as it is."""
    with MaterializationTarget(tmp_path / "mine") as target:
        target.write_symlink(b"python", LinkKind.HOST, b"/usr/bin/python3")
    assert os.readlink(tmp_path / "mine" / "python") == "/usr/bin/python3"
