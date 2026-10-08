"""The POSIX round-trip corpus: every feature the fidelity bar names, on disk.

One function builds the tree, and it is deliberately explicit rather than
parametrized: the corpus *is* the contract, so reading it should tell you
exactly what the round trip claims to preserve. Every feature carries a comment
saying which claim it stands for.

Anything the running platform cannot represent is reported in
:attr:`Corpus.unsupported` rather than skipped silently — a corpus that quietly
built fewer features on macOS would turn a red test green by omission.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from alkera_core.files import names

__all__ = [
    "DEEP_SEGMENT",
    "GIT_REPO",
    "PULL_SIDECAR",
    "Corpus",
    "build_corpus",
    "git_available",
]

#: 1999-01-01T00:00:00Z, the old timestamp the mtime claim is made with.
OLD_MTIME_NS: Final = 915_148_800_000_000_000

#: Where the real git repository lives inside the corpus.
GIT_REPO: Final = "repo"

#: How long the deep path aims to be, in bytes of relative path.
DEEP_PATH_BYTES: Final = 2_000

#: What each level of the deep path is named, and the file at the bottom of it.
DEEP_SEGMENT: Final = "segment" * 6
DEEP_LEAF: Final = "leaf.txt"

#: The sidecar a pull streams a file's bytes into before promoting them into
#: place. The longest path a round trip ever creates is a pulled leaf plus
#: this suffix, so the corpus has to budget for it even though nothing in the
#: source tree pays it. Taken from the library so the budget here and the byte
#: ceiling the server enforces cannot be reserving different amounts of room.
PULL_SIDECAR: Final = names.PULL_PART_SUFFIX

#: The relative paths ``--respect-gitignore`` must fold away.
GITIGNORED: Final = frozenset({b"repo/build", b"repo/ignored.log"})


@dataclass
class Corpus:
    """The built tree and what the platform refused to build in it."""

    root: Path
    unsupported: list[str] = field(default_factory=list)
    hardlink_group: tuple[str, str] = ("store/a.bin", "store/b.bin")
    canonical_link: str = "links/canonical"
    deep_path: bytes = b""
    """The relative path of the deep leaf actually built on this platform."""

    case_sensitive: bool = False
    """Whether the repo's two names differing only in case stayed two files."""


def git_available() -> bool:
    return shutil.which("git") is not None


def _write(root: Path, relative: str, data: bytes, *, mode: int | None = None) -> Path:
    where = root / relative
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(data)
    if mode is not None:
        where.chmod(mode)
    return where


def _path_limits(where: Path) -> tuple[int, int]:
    """``(NAME_MAX, PATH_MAX)`` in bytes, as the filesystem holding ``where`` reports them.

    Both are asked rather than assumed: macOS caps a path at 1,024 bytes where
    Linux allows 4,096, and a mount can say something else again. A corpus that
    hard-coded either would build an illegal path on somebody's box — which is
    an OSError out of the fixture, not a verdict about the round trip.

    The fallbacks are the smallest values POSIX guarantees, so a platform that
    declines to answer gets a corpus that is shorter than it could have been
    rather than one that cannot be built. ``unsupported`` records the shortfall.
    """
    name_max, path_max = 255, 1024
    try:
        reported_name = os.pathconf(where, "PC_NAME_MAX")
        reported_path = os.pathconf(where, "PC_PATH_MAX")
    except (OSError, ValueError, AttributeError):
        return name_max, path_max
    return (
        reported_name if reported_name > 0 else name_max,
        reported_path if reported_path > 0 else path_max,
    )


def _set_xattr(where: Path, name: bytes, value: bytes) -> bool:
    """Set one ``user.*`` attribute, reporting whether the platform took it."""
    if not hasattr(os, "setxattr"):
        return False
    try:
        os.setxattr(where, os.fsdecode(name), value, follow_symlinks=False)
    except OSError:
        return False
    return True


def build_corpus(
    root: Path, *, local_root: bytes | None = None, pulled_under: Path | None = None
) -> Corpus:
    """Build the corpus under ``root`` and describe what it holds.

    ``local_root`` is the org tree's mount point the canonical link is written
    against; it defaults to ``root`` itself, which is what a push from this
    directory classifies against.

    ``pulled_under`` is the root the same tree will be materialized under
    again — the pull's destination. The deep path is budgeted against *that*
    root rather than this one, because a chain that fits only where it was
    built is a chain the round trip cannot write back.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    corpus = Corpus(root=root)
    anchor = local_root if local_root is not None else os.fsencode(root)

    # Empty directories: a browser drop omits them, so the CLI must not.
    (root / "empty").mkdir(exist_ok=True)
    (root / "nested" / "deep" / "empty").mkdir(parents=True, exist_ok=True)

    # Ordinary bytes, and an executable whose mode bits must survive.
    _write(root, "data/notes.txt", b"plain bytes\n")
    _write(root, "data/run.sh", b"#!/bin/sh\necho hi\n", mode=0o755)

    # A file with a 1999 mtime: the claim that mtime is restored, not reset.
    old = _write(root, "data/old.txt", b"old\n")
    os.utime(old, ns=(OLD_MTIME_NS, OLD_MTIME_NS))

    # Platform sidecars: stored as junk by nobody, folded by the naming rules.
    _write(root, "data/.DS_Store", b"\x00finder\x00")
    _write(root, "data/._notes.txt", b"\x00resource fork\x00")

    # A hard-link group: one inode, two names, pushed once.
    first = _write(root, corpus.hardlink_group[0], b"shared payload\n")
    second = root / corpus.hardlink_group[1]
    second.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(first, second)
    except OSError:
        corpus.unsupported.append("hard links")
        second.write_bytes(first.read_bytes())

    # A sparse file: a hole then bytes. Allocation is not preserved; content is.
    sparse = root / "data" / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(1 << 20)
        handle.seek(1 << 20)
        handle.write(b"tail\n")

    # ``user.*`` xattrs on a real file.
    tagged = _write(root, "data/tagged.txt", b"tagged\n")
    if not _set_xattr(tagged, b"user.alkera.test", b"value"):
        corpus.unsupported.append("xattrs")

    # A non-ASCII name that IS valid UTF-8: every filesystem can hold this one,
    # so it carries the "names are bytes, not ASCII" claim where the Latin-1
    # name below cannot even be created.
    _write(root, "data/café.txt", "café bytes\n".encode())

    # A name that is not valid UTF-8: a Latin-1 archive name. A Windows filename
    # is a sequence of UTF-16 code units, so an undecodable byte string cannot
    # name a file there at all — Python raises the decode error off the path
    # converter rather than letting the filesystem refuse it with an OSError.
    latin1 = os.fsencode(root) + b"/data/" + b"caf\xe9.txt"
    try:
        with open(latin1, "wb") as handle:
            handle.write(b"latin-1 name\n")
    except (OSError, UnicodeError):
        corpus.unsupported.append("non-UTF-8 names")

    # A path far longer than Windows' 260 characters, grown until either the
    # 2,000-byte target or this filesystem's own PATH_MAX stops it. macOS caps
    # a path at 1,024 bytes, so the full 2,000 is reported unsupported rather
    # than silently skipped — a corpus that quietly built a shorter path would
    # make the deep-path claim vacuous on that platform.
    #
    # The budget reserves the leaf's own name up front, because the deepest
    # *directory* that fits under PATH_MAX says nothing about the file inside
    # it. Growing until mkdir refuses leaves a directory that can be within a
    # few bytes of the cap, and writing the leaf into it then fails with
    # ENAMETOOLONG — on exactly those root lengths where the last surviving
    # directory lands in the final ``len("/leaf.txt")`` bytes of the limit.
    #
    # It reserves what the *destination* costs too. The tree is built once and
    # written again by every pull, under a root that need not be as short as
    # this one and through a ``.alkera-part`` sidecar beside each file. A
    # chain grown to the cap here is therefore over it there, and the round
    # trip dies with ENAMETOOLONG on a path the corpus itself chose.
    name_max, path_max = _path_limits(root)
    segment = os.fsdecode(os.fsencode(DEEP_SEGMENT)[:name_max])
    leaf = os.fsencode(DEEP_LEAF)
    destination = os.fsencode(pulled_under if pulled_under is not None else root)
    displacement = max(0, len(destination) - len(os.fsencode(root)))
    # PATH_MAX counts the terminating NUL, so the longest usable path is one
    # byte shorter than it; off that come the leaf's separator and name, the
    # sidecar, and however much longer the destination root is than this one.
    deepest_directory = path_max - 1 - (1 + len(leaf)) - len(PULL_SIDECAR) - displacement
    deep = root / "long"
    deep.mkdir(parents=True, exist_ok=True)
    while len(os.fsencode(deep)) - len(os.fsencode(root)) < DEEP_PATH_BYTES:
        candidate = deep / segment
        if len(os.fsencode(candidate)) > deepest_directory:
            corpus.unsupported.append("a 2,000-byte path")
            break
        try:
            candidate.mkdir()
        except OSError:
            corpus.unsupported.append("a 2,000-byte path")
            break
        deep = candidate
    (deep / DEEP_LEAF).write_bytes(b"deep\n")
    corpus.deep_path = os.fsencode(deep)[len(os.fsencode(root)) + 1 :] + b"/" + leaf

    _links(root, anchor, corpus)
    _git_repo(root, corpus)
    return corpus


def _links(root: Path, anchor: bytes, corpus: Corpus) -> None:
    """Every symlink class the classification distinguishes."""
    links = root / "links"
    links.mkdir(exist_ok=True)

    # Relative: stored and written back verbatim.
    os.symlink("../data/notes.txt", links / "relative")

    # Canonical: absolute *under* the local root, rewritten to the pull's root.
    os.symlink(os.fsdecode(anchor + b"/data/notes.txt"), links / "canonical")

    # Host: absolute outside the root, verbatim on every machine.
    os.symlink("/usr/bin/env", links / "host")

    # Dangling: a target that does not exist, and must still round-trip.
    os.symlink("../data/gone.txt", links / "dangling")

    # Looping: two links pointing at each other; neither may be followed.
    os.symlink("loop-b", links / "loop-a")
    os.symlink("loop-a", links / "loop-b")

    # A fifo: recorded as a special, recreated where the platform allows it.
    try:
        os.mkfifo(root / "data" / "pipe")
    except (OSError, AttributeError):
        corpus.unsupported.append("fifos")


def _git_repo(root: Path, corpus: Corpus) -> None:
    """A real git repository — init, a commit, a hook, a link and a packfile.

    Real rather than synthesized: the claim is that ``git status`` is clean and
    ``git fsck`` passes after the round trip, and only git's own output can
    make that claim.
    """
    if not git_available():
        corpus.unsupported.append("git")
        return
    repo = root / GIT_REPO
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Corpus",
        "GIT_AUTHOR_EMAIL": "corpus@alkera.test",
        "GIT_COMMITTER_NAME": "Corpus",
        "GIT_COMMITTER_EMAIL": "corpus@alkera.test",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_bytes(b"# corpus repo\n")
    (repo / ".gitignore").write_bytes(b"build/\nignored.log\n")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "main.py").write_bytes(b"print('hello')\n")

    # An in-tree symlink git tracks as a link, not as its target's bytes.
    os.symlink("src/main.py", repo / "entrypoint")

    # A case-colliding pair: two names that differ only in case. Both must be in
    # the COMMIT — written after it they are untracked, and `git status` on the
    # pulled clone is then dirty on any filesystem that really keeps both names.
    # Whether this filesystem keeps both is a property of the mount, not of the
    # platform (macOS can be case-sensitive, Linux can be mounted otherwise), so
    # it is probed rather than assumed.
    (repo / "src" / "Case.txt").write_bytes(b"upper\n")
    (repo / "src" / "case.txt").write_bytes(b"lower\n")
    if (repo / "src" / "Case.txt").read_bytes() == b"upper\n":
        corpus.case_sensitive = True
    else:
        # The two names collapsed onto one inode, so there is no pair to push.
        corpus.unsupported.append("case-colliding pair")

    # A hook the *user* wrote: an ordinary executable file in their own clone.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(b"#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)

    git("add", "-A")
    git("commit", "-q", "-m", "the corpus commit")
    # ``gc`` turns the loose objects into a packfile: a large immutable blob.
    # Plain rather than ``--aggressive``: the claim is that a packfile survives
    # the round trip, and a repack of a handful of objects finds the same pack
    # either way — the aggressive delta search only spends the time.
    git("gc", "-q", "--prune=now")

    # Paths ``--respect-gitignore`` must fold away.
    (repo / "build").mkdir(exist_ok=True)
    (repo / "build" / "artifact.o").write_bytes(b"object\n")
    (repo / "ignored.log").write_bytes(b"noise\n")


def executable_bits(where: Path) -> int:
    """The execute bits on ``where`` — the corpus's mode claim, spelled once."""
    return stat_module.S_IMODE(where.lstat().st_mode) & 0o111
