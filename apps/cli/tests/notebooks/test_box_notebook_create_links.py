"""Creating a notebook file never follows a link out of the held folder.

The worker that writes it can reach every member's tree, so a folder a member
pointed at another member's tree must refuse the create instead of landing
the file there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_cli.notebooks.box import _write_new

needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")


@needs_symlinks
def test_a_create_through_a_linked_folder_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "held"
    victim = tmp_path / "victim"
    root.mkdir()
    victim.mkdir()
    (root / "x").symlink_to(victim)
    with pytest.raises(OSError):
        _write_new(root, "x/evil.alknb.py", "print('hi')\n")
    assert list(victim.iterdir()) == []


@needs_symlinks
def test_a_create_over_a_link_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "held"
    root.mkdir()
    outside = tmp_path / "outside.alknb.py"
    (root / "n.alknb.py").symlink_to(outside)
    with pytest.raises(OSError):
        _write_new(root, "n.alknb.py", "x")
    assert not outside.exists()


def test_a_create_makes_the_folders_on_its_way(tmp_path: Path) -> None:
    _write_new(tmp_path, "a/b/n.alknb.py", "print(1)\n")
    assert (tmp_path / "a/b/n.alknb.py").read_text(encoding="utf-8") == "print(1)\n"


def test_a_create_never_replaces_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / "n.alknb.py").write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _write_new(tmp_path, "n.alknb.py", "new")
    assert (tmp_path / "n.alknb.py").read_text(encoding="utf-8") == "old"
