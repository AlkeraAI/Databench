"""The round-trip corpus tree: one directory containing every POSIX feature.

Both the walk tests and the materialization tests build this tree, so the two
halves are proven against the same corpus rather than against two convenient
subsets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: A name that is not valid UTF-8, the shape an old Latin-1 archive carries.
LATIN1_NAME = b"caf\xe9.txt"

#: A non-ASCII name that IS valid UTF-8. Every filesystem can hold this one, so
#: it carries the "names are bytes, not ASCII" claim on the platforms that
#: refuse :data:`LATIN1_NAME` outright.
UTF8_NAME = "café.txt".encode()

SPARSE_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class Corpus:
    """The built tree plus the features this filesystem actually allowed."""

    root: Path
    latin1_name: bytes | None
    """``None`` where the filesystem refuses a non-UTF-8 name (APFS does)."""

    sparse_allocated: int
    """Blocks the sparse file really got; a filesystem without holes gives it all."""


def build_corpus(root: Path) -> Corpus:
    """Create the corpus under ``root``.

    Contents, by the feature each one pins:

    * ``empty/`` — an empty directory, which browsers drop and a push must keep
    * ``papers/note.txt`` and ``papers/note-link`` — a hard-link pair (one inode)
    * ``sparse.bin`` — a file with a 4 MiB hole; ``dense.bin`` is its twin
    * ``tagged.txt`` — the file the caller hangs a ``user.*`` xattr on
    * ``café.txt`` — a non-ASCII name every platform can hold
    * the Latin-1 name — a non-decodable filename, where the platform allows it
    * ``links/`` — relative, canonical, host, dangling and looping symlinks
    * ``.DS_Store`` and ``._resource`` — folded sidecars
    * ``build/out.o`` — ignored by ``.gitignore`` in a git working tree
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "empty").mkdir()

    docs = root / "papers"
    docs.mkdir()
    (docs / "note.txt").write_bytes(b"note bytes\n")
    os.link(docs / "note.txt", docs / "note-link")

    sparse = root / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(SPARSE_SIZE)
        # The tail lands INSIDE the declared length, not past it, so the holey
        # file and its dense twin are byte-for-byte the same size and the only
        # difference between them is the hole. Writing at SPARSE_SIZE instead
        # grew this file four bytes past the twin, which passed only where the
        # filesystem filled the hole in and the sparse assertions were skipped.
        handle.seek(SPARSE_SIZE - 4)
        handle.write(b"tail")

    (root / "dense.bin").write_bytes(b"x" * SPARSE_SIZE)
    (root / "tagged.txt").write_bytes(b"tagged\n")

    (root / os.fsdecode(UTF8_NAME)).write_bytes(b"utf-8 name\n")

    latin1: bytes | None = None
    try:
        with open(os.fsencode(root) + b"/" + LATIN1_NAME, "wb") as handle:
            handle.write(b"latin-1 name\n")
        latin1 = LATIN1_NAME
    except (OSError, UnicodeError):
        # A Windows filename is a sequence of UTF-16 code units, so a byte
        # string that is not valid UTF-8 cannot name a file there at all:
        # Python raises the decode error off the path converter rather than
        # letting the filesystem refuse it with an OSError the way APFS does.
        latin1 = None

    links = root / "links"
    links.mkdir()
    os.symlink("../papers/note.txt", links / "relative")
    os.symlink(os.fsencode(root) + b"/papers/note.txt", os.fsencode(links / "canonical"))
    os.symlink("/usr/bin/python3", links / "host")
    os.symlink("nowhere.txt", links / "dangling")
    os.symlink("looping", links / "looping")

    (root / ".DS_Store").write_bytes(b"junk")
    (root / "._resource").write_bytes(b"junk")

    build = root / "build"
    build.mkdir()
    (build / "out.o").write_bytes(b"object\n")
    return Corpus(root=root, latin1_name=latin1, sparse_allocated=sparse.stat().st_blocks)


def make_git_repo(root: Path, ignore_patterns: str = "build/\n") -> None:
    """Turn ``root`` into something the walker treats as a working tree."""
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (root / ".gitignore").write_text(ignore_patterns, encoding="utf-8")
