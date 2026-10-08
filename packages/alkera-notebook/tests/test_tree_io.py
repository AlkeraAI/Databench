"""The link-refusing tree: what it refuses and what it leaves alone."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alkera_notebook.envs import generations
from alkera_notebook.tree_io import LinkRefusedError, Tree, TreeModes

needs_posix_links = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symbolic links")
needs_posix_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX fifo")


@pytest.fixture
def victim(tmp_path: Path) -> Path:
    v = tmp_path / "outside"
    v.mkdir()
    (v / "keep.txt").write_text("theirs", encoding="utf-8")
    return v


def _intact(victim: Path) -> bool:
    return (
        sorted(p.name for p in victim.rglob("*")) == ["keep.txt"]
        and (victim / "keep.txt").read_text(encoding="utf-8") == "theirs"
    )


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("../x", id="parent"),
        pytest.param("a/../../x", id="parent-inside"),
        pytest.param("/elsewhere/x", id="absolute-outside"),
        pytest.param("", id="empty"),
    ],
)
def test_a_path_that_leaves_the_tree_is_refused(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ValueError):
        Tree(tmp_path).write_atomic(rel, b"x")


def test_an_absolute_path_under_the_root_is_its_relative_path(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    tree.write_atomic(tmp_path / "a" / "b.txt", b"x")
    assert (tmp_path / "a" / "b.txt").read_bytes() == b"x"


@needs_posix_links
@pytest.mark.parametrize("link", [pytest.param("a", id="first"), pytest.param("a/b", id="middle")])
def test_a_write_through_a_linked_folder_is_refused(
    tmp_path: Path, victim: Path, link: str
) -> None:
    root = tmp_path / "root"
    (root / link).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(victim, root / link)
    with pytest.raises(LinkRefusedError):
        Tree(root).write_atomic("a/b/c.txt", b"x")
    with pytest.raises(LinkRefusedError):
        Tree(root).make_dirs("a/b/c")
    assert _intact(victim)


@needs_posix_links
def test_a_linked_file_is_replaced_never_written_through(tmp_path: Path, victim: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(victim / "keep.txt", root / "f.txt")
    Tree(root).write_atomic("f.txt", b"mine")
    assert not (root / "f.txt").is_symlink() and (root / "f.txt").read_bytes() == b"mine"
    assert _intact(victim)


def test_a_hard_linked_file_is_replaced_never_written_through(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("theirs", encoding="utf-8")
    os.link(other, root / "f.txt")
    Tree(root).write_atomic("f.txt", b"mine")
    assert other.read_text(encoding="utf-8") == "theirs"
    assert (root / "f.txt").read_bytes() == b"mine"


@needs_posix_links
def test_reading_a_link_is_refused(tmp_path: Path, victim: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(victim / "keep.txt", root / "f.txt")
    with pytest.raises(LinkRefusedError):
        Tree(root).read_bytes("f.txt")


@needs_posix_fifo
def test_reading_a_fifo_is_refused_without_waiting(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(LinkRefusedError):
        Tree(tmp_path).read_bytes("pipe")


@needs_posix_links
def test_listing_leaves_out_links_and_other_kinds(tmp_path: Path, victim: Path) -> None:
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "d").mkdir()
    os.symlink(victim / "keep.txt", tmp_path / "link.txt")
    os.symlink(victim, tmp_path / "link-dir")
    tree = Tree(tmp_path)
    assert tree.files() == ["f.txt"]
    assert sorted(tree.dirs()) == ["d", "outside"]
    with pytest.raises(LinkRefusedError):
        tree.files("link-dir")


@needs_posix_links
def test_removing_a_tree_removes_its_links_not_what_they_name(tmp_path: Path, victim: Path) -> None:
    root = tmp_path / "root"
    (root / "t" / "deep").mkdir(parents=True)
    os.symlink(victim, root / "t" / "deep" / "dir-link")
    os.symlink(victim / "keep.txt", root / "t" / "file-link")
    Tree(root).rmtree("t")
    assert not (root / "t").exists()
    assert _intact(victim)


@needs_posix_links
def test_removing_a_link_removes_only_the_link(tmp_path: Path, victim: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(victim, root / "l")
    Tree(root).rmtree("l")
    Tree(root).unlink("gone", missing_ok=True)
    assert not os.path.lexists(root / "l")
    assert _intact(victim)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_tree_modes_are_given_to_what_it_makes(tmp_path: Path) -> None:
    tree = Tree(tmp_path, modes=TreeModes(directory=0o2770, file=0o660))
    tree.write_atomic("a/f.txt", b"x")
    assert (tmp_path / "a").stat().st_mode & 0o7777 == 0o2770
    assert (tmp_path / "a" / "f.txt").stat().st_mode & 0o7777 == 0o660


@needs_posix_links
def test_a_linked_generations_folder_is_not_emptied(tmp_path: Path, victim: Path) -> None:
    """The env root is writable from the kernel sandbox: a link where an
    environment's generations go must not have the old generations' sweep
    remove what the link names."""
    (victim / "g1").mkdir()
    (victim / "g1" / "keep.txt").write_text("theirs", encoding="utf-8")
    env_root = tmp_path / "envs"
    env_root.mkdir()
    prefix = env_root / "default"
    os.symlink(victim, generations.generations_dir(prefix))
    with pytest.raises(LinkRefusedError):
        generations.adopt(Tree(env_root), prefix, generations.new_generation(prefix))
    assert (victim / "g1" / "keep.txt").read_text(encoding="utf-8") == "theirs"
