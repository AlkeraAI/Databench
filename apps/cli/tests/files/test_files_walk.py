"""The push-side walk over the round-trip corpus."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from _tree_fixture import SPARSE_SIZE, UTF8_NAME, Corpus, build_corpus, make_git_repo
from alkera_cli.files.walk import Entry, EntryKind, ExportRules, SkipReason, walk
from alkera_cli.files.xattrs import XATTRS_SUPPORTED, write_xattrs
from alkera_core.files.links import LinkKind

# The package re-exports the function under the module's own name, so the module
# itself is reached by import rather than as an attribute of the package.
walk_module = importlib.import_module("alkera_cli.files.walk")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the walk corpus needs byte-exact names, sparse files and special files the "
        "Windows runner does not offer"
    ),
)


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return build_corpus(tmp_path / "tree")


def index(
    root: Path,
    *,
    local_root: bytes | None = None,
    respect_gitignore: bool = False,
    exclude_presets: tuple[str, ...] = (),
) -> dict[bytes, Entry]:
    walked = walk(
        root,
        local_root=local_root,
        respect_gitignore=respect_gitignore,
        exclude_presets=exclude_presets,
    )
    return {entry.relative: entry for entry in walked}


def test_an_empty_directory_is_an_entry_of_its_own(corpus: Corpus) -> None:
    """Browsers drop empty folders; a push must not."""
    entries = index(corpus.root)
    assert entries[b"empty"].kind is EntryKind.DIRECTORY
    assert not [key for key in entries if key.startswith(b"empty/")]


def test_a_hard_link_pair_shares_one_group_and_a_lone_file_has_none(corpus: Corpus) -> None:
    """Grouping is by (st_dev, st_ino) and only when the link count says so."""
    entries = index(corpus.root)
    note = entries[b"papers/note.txt"]
    twin = entries[b"papers/note-link"]
    assert note.hardlink_group is not None
    assert note.hardlink_group == twin.hardlink_group
    assert entries[b"tagged.txt"].hardlink_group is None


def test_a_holey_file_is_sparse_and_its_dense_twin_is_not(corpus: Corpus) -> None:
    """The same length, one with a hole: only that one is reported sparse."""
    entries = index(corpus.root)
    # The length claim holds on every filesystem — it is the corpus's own
    # construction, not something the storage layer may reinterpret.
    assert entries[b"sparse.bin"].size == SPARSE_SIZE
    assert entries[b"dense.bin"].size == SPARSE_SIZE
    if corpus.sparse_allocated * 512 >= SPARSE_SIZE:
        pytest.skip("this filesystem allocated the hole, so there is no sparseness to detect")
    assert entries[b"sparse.bin"].sparse is True
    assert entries[b"dense.bin"].sparse is False


@pytest.mark.skipif(not XATTRS_SUPPORTED, reason="the platform has no extended attributes")
def test_user_xattrs_travel_and_other_namespaces_do_not(corpus: Corpus) -> None:
    """Only ``user.*`` is portable, so only ``user.*`` is collected."""
    write_xattrs(corpus.root / "tagged.txt", {b"user.alkera.git.commit": b"deadbeef"})
    entries = index(corpus.root)
    assert entries[b"tagged.txt"].xattrs == {b"user.alkera.git.commit": b"deadbeef"}
    assert entries[b"papers/note.txt"].xattrs == {}


def test_a_non_ascii_name_survives_as_bytes(corpus: Corpus) -> None:
    """A UTF-8 name is carried as its bytes, not as a decoded str."""
    entries = index(corpus.root)
    assert UTF8_NAME in entries
    assert entries[UTF8_NAME].kind is EntryKind.FILE


def test_a_non_utf8_name_survives_as_bytes(corpus: Corpus) -> None:
    """A Latin-1 archive name must not be lost or mangled by a decode."""
    if corpus.latin1_name is None:
        pytest.skip("this filesystem refuses a non-UTF-8 filename")
    entries = index(corpus.root)
    assert corpus.latin1_name in entries
    assert entries[corpus.latin1_name].kind is EntryKind.FILE


@pytest.mark.parametrize(
    ("name", "expected_kind"),
    [
        pytest.param(b"links/relative", LinkKind.RELATIVE, id="relative"),
        pytest.param(b"links/canonical", LinkKind.CANONICAL, id="canonical"),
        pytest.param(b"links/host", LinkKind.HOST, id="host"),
        pytest.param(b"links/dangling", LinkKind.RELATIVE, id="dangling-is-still-relative"),
        pytest.param(b"links/looping", LinkKind.RELATIVE, id="looping-is-still-relative"),
    ],
)
def test_links_are_classified_against_the_local_root(
    corpus: Corpus, name: bytes, expected_kind: LinkKind
) -> None:
    entries = index(corpus.root)
    entry = entries[name]
    assert entry.kind is EntryKind.SYMLINK
    assert entry.link_kind is expected_kind


def test_a_canonical_link_is_stored_as_an_org_path_not_a_machine_path(corpus: Corpus) -> None:
    """The local root is stripped, which is what makes the link portable."""
    entry = index(corpus.root)[b"links/canonical"]
    assert entry.link_target == b"/papers/note.txt"


def test_a_host_link_is_stored_verbatim(corpus: Corpus) -> None:
    assert index(corpus.root)[b"links/host"].link_target == b"/usr/bin/python3"


def test_a_looping_link_does_not_hang_the_walk(corpus: Corpus) -> None:
    """follow_symlinks=False: a self-referential link is one entry, not a cycle."""
    entries = index(corpus.root)
    assert entries[b"links/looping"].link_target == b"looping"
    assert not [key for key in entries if key.startswith(b"links/looping/")]


@pytest.mark.parametrize(
    "name",
    [pytest.param(b".DS_Store", id="ds-store"), pytest.param(b"._resource", id="apple-double")],
)
def test_sidecars_are_folded_with_a_typed_reason(corpus: Corpus, name: bytes) -> None:
    """Folded, not deleted from the report: the caller says what it skipped."""
    assert index(corpus.root)[name].skipped is SkipReason.SIDECAR


def test_gitignore_is_honoured_only_inside_a_git_working_tree(corpus: Corpus) -> None:
    """The same .gitignore is inert until the root is actually a repo."""
    (corpus.root / ".gitignore").write_text("build/\n", encoding="utf-8")
    assert index(corpus.root, respect_gitignore=True)[b"build"].skipped is None

    make_git_repo(corpus.root)
    entries = index(corpus.root, respect_gitignore=True)
    assert entries[b"build"].skipped is SkipReason.GITIGNORED
    assert b"build/out.o" not in entries


def test_gitignore_can_be_turned_off(corpus: Corpus) -> None:
    make_git_repo(corpus.root)
    entries = index(corpus.root, respect_gitignore=False)
    assert entries[b"build"].skipped is None
    assert b"build/out.o" in entries


def test_a_negated_gitignore_pattern_wins_over_an_earlier_match(corpus: Corpus) -> None:
    make_git_repo(corpus.root, ignore_patterns="*.o\n!out.o\n")
    (corpus.root / "build" / "other.o").write_bytes(b"other\n")
    entries = index(corpus.root, respect_gitignore=True)
    assert entries[b"build/out.o"].skipped is None
    assert entries[b"build/other.o"].skipped is SkipReason.GITIGNORED


def test_the_caches_preset_folds_every_tool_cache_and_nothing_else(tmp_path: Path) -> None:
    """The preset a held folder applies by default: the directories a working
    directory grows when it is also the agent's home. A sibling that merely
    resembles one (`caches/`, `.venvs`) is the person's and travels."""
    from alkera_cli.files.walk import EXCLUDE_PRESETS, LIVE_SYNC_DEFAULT_PRESETS

    for name in (
        ".uvcache",
        ".uvpython",
        ".local",
        ".cache",
        ".venv",
        "node_modules",
        "__pycache__",
    ):
        (tmp_path / name / "inner").mkdir(parents=True)
        (tmp_path / name / "inner" / "f").write_bytes(b"x")
    (tmp_path / "caches").mkdir()
    (tmp_path / "caches" / "keep.txt").write_bytes(b"x")
    (tmp_path / ".venvs").mkdir()
    (tmp_path / ".venvs" / "note.md").write_bytes(b"x")

    entries = index(tmp_path, exclude_presets=("caches",))

    assert LIVE_SYNC_DEFAULT_PRESETS == ("caches",)
    assert all(name.encode() in EXCLUDE_PRESETS["caches"] for name in (".uvcache", ".local"))
    carried = sorted(e.relative for e in entries.values() if e.skipped is None)
    assert carried == [b".venvs", b".venvs/note.md", b"caches", b"caches/keep.txt"]
    folded = sorted(e.relative for e in entries.values() if e.skipped == SkipReason.EXCLUDE_PRESET)
    assert folded == sorted(
        [b".cache", b".local", b".uvcache", b".uvpython", b".venv", b"__pycache__", b"node_modules"]
    )


def test_an_exclude_preset_folds_its_directory_and_its_contents(corpus: Corpus) -> None:
    modules = corpus.root / "node_modules"
    modules.mkdir()
    (modules / "dep.js").write_bytes(b"module\n")
    entries = index(corpus.root, exclude_presets=("node_modules",))
    assert entries[b"node_modules"].skipped is SkipReason.EXCLUDE_PRESET
    assert b"node_modules/dep.js" not in entries
    assert index(corpus.root)[b"node_modules/dep.js"].skipped is None


@pytest.mark.parametrize(
    ("options", "unreached", "reason"),
    [
        pytest.param(
            {"respect_gitignore": True},
            b"build/deeper/out.o",
            SkipReason.GITIGNORED,
            id="under-a-gitignored-folder",
        ),
        pytest.param(
            {"exclude_presets": ("node_modules",)},
            b"node_modules/dep/index.js",
            SkipReason.EXCLUDE_PRESET,
            id="under-a-preset-folder",
        ),
        pytest.param(
            {"respect_gitignore": False}, b"build/deeper/out.o", None, id="gitignore-turned-off"
        ),
    ],
)
def test_one_path_is_answered_exactly_as_the_walk_answers_it(
    corpus: Corpus,
    options: dict[str, Any],
    unreached: bytes,
    reason: SkipReason | None,
) -> None:
    """A watcher meets paths one at a time and has no walk to lean on: asked
    of one path, the rules give the walk's own verdict for every entry the
    walk reaches, and a path under a folder the walk skipped is skipped for
    that folder's reason — the walk never gets there to say so."""
    make_git_repo(corpus.root)
    (corpus.root / "node_modules" / "dep").mkdir(parents=True)
    (corpus.root / "node_modules" / "dep" / "index.js").write_bytes(b"module\n")
    (corpus.root / "build" / "deeper").mkdir()
    (corpus.root / "build" / "deeper" / "out.o").write_bytes(b"obj\n")
    settings: dict[str, Any] = {
        "respect_gitignore": False,
        "exclude_presets": (),
        "skip_local_state": True,
    }
    settings.update(options)
    rules = ExportRules.load(corpus.root, **settings)

    walked = list(walk(corpus.root, **settings))

    assert walked
    for entry in walked:
        is_dir = entry.kind is EntryKind.DIRECTORY
        assert rules.skip_reason(entry.relative, is_dir=is_dir) is entry.skipped, entry.relative
    assert rules.skip_reason(unreached, is_dir=False) is reason


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        pytest.param(".runtime/envs/alkera/bin/python", SkipReason.LOCAL_STATE, id="the-venv"),
        pytest.param(
            ".runtime/envs/alkera/lib/python3.12/site-packages/pip/__init__.py",
            SkipReason.LOCAL_STATE,
            id="a-package-in-the-venv",
        ),
        pytest.param(".overlay/usr/lib/x.so", SkipReason.LOCAL_STATE, id="the-root-overlay"),
        pytest.param(".runtime/agent/agent.db", None, id="the-agents-database-still-travels"),
        pytest.param("scratch/hello.txt", None, id="the-chats-work"),
    ],
)
def test_a_boxs_python_environment_and_overlay_never_ride_the_folder(
    tmp_path: Path, relative: str, reason: SkipReason | None
) -> None:
    """A held folder's push skips local state; the chat's venv (thousands of
    interpreter files a box remakes on every spawn) and the container's root
    overlay are that box's, and the drive must never receive them."""
    rules = ExportRules.load(tmp_path, respect_gitignore=False, skip_local_state=True)
    assert rules.skip_reason(relative.encode(), is_dir=False) is reason


def test_mode_uid_gid_and_mtime_are_captured_verbatim(corpus: Corpus) -> None:
    """git decides its index is racy if the mtime moves, so it is nanoseconds."""
    script = corpus.root / "hook"
    script.write_bytes(b"#!/bin/sh\n")
    os.chmod(script, 0o755)
    os.utime(script, ns=(1_000_000_123, 923_400_000_567))
    entry = index(corpus.root)[b"hook"]
    assert entry.mode == 0o755
    assert entry.mtime_ns == 923_400_000_567
    assert entry.uid == os.stat(script).st_uid
    assert entry.gid == os.stat(script).st_gid


def test_parents_are_yielded_before_their_children_in_byte_order(corpus: Corpus) -> None:
    order = [entry.relative for entry in walk(corpus.root)]
    assert order.index(b"papers") < order.index(b"papers/note.txt")
    assert order.index(b"papers/note-link") < order.index(b"papers/note.txt")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no FIFOs (no os.mkfifo)")
def test_a_special_file_is_recorded_not_read(tmp_path: Path) -> None:
    """A fifo must be classified without opening it, which would block."""
    root = tmp_path / "specials"
    root.mkdir()
    os.mkfifo(root / "pipe")
    entry = next(item for item in walk(root) if item.relative == b"pipe")
    assert entry.kind is EntryKind.SPECIAL
    assert entry.hardlink_group is None


@pytest.mark.parametrize(
    ("patterns", "expected"),
    [
        pytest.param("logs/\n", b".git/logs/HEAD", id="a-directory-pattern"),
        pytest.param("*.pack\n", b".git/objects/pack/pack-abc.pack", id="a-glob-pattern"),
        pytest.param("info\n", b".git/info/exclude", id="a-bare-name"),
    ],
)
def test_a_gitignore_pattern_never_prunes_the_repositorys_own_metadata(
    tmp_path: Path, patterns: str, expected: bytes
) -> None:
    """git itself never consults .gitignore inside `.git/`, and neither may a
    push: `logs/`, `*.pack` and `info` are ordinary root-level entries that
    also name real metadata, and folding them ships a repository the far side
    cannot `git fsck`."""
    root = tmp_path / "repo"
    root.mkdir()
    make_git_repo(root, ignore_patterns=patterns)
    for relative in (".git/logs/HEAD", ".git/objects/pack/pack-abc.pack", ".git/info/exclude"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")

    entries = index(root, respect_gitignore=True)

    assert expected in entries, sorted(entries)
    assert entries[expected].skipped is None
    assert [key for key, entry in entries.items() if entry.skipped is SkipReason.GITIGNORED] == []


def test_a_gitignore_pattern_still_prunes_a_working_tree_path_of_the_same_name(
    tmp_path: Path,
) -> None:
    """The exemption is scoped to the metadata directory, not to the pattern."""
    root = tmp_path / "repo"
    root.mkdir()
    make_git_repo(root, ignore_patterns="logs/\n")
    (root / "logs").mkdir()
    (root / "logs" / "app.log").write_bytes(b"x")
    (root / ".git" / "logs").mkdir()
    (root / ".git" / "logs" / "HEAD").write_bytes(b"x")

    entries = index(root, respect_gitignore=True)

    assert entries[b"logs"].skipped is SkipReason.GITIGNORED
    assert b"logs/app.log" not in entries
    assert entries[b".git/logs"].skipped is None
    assert entries[b".git/logs/HEAD"].skipped is None


def _windows_readlink(link: Path, substitute: bytes):
    """An ``os.readlink`` that answers for one link the way Windows would.

    Windows hands back the symlink's substitute name — extended-length
    prefixed, spelled with its own separators — where POSIX hands back the
    text the link was created with. Only the one link is answered for, so
    every other path the walk reads is still the real filesystem's.
    """
    real = os.readlink
    wanted = os.lstat(link)

    def readlink(path, *, dir_fd=None):
        # The walk reads a link by name relative to its directory's descriptor,
        # so the one link is recognised by identity rather than by spelling.
        found = os.lstat(path, dir_fd=dir_fd)
        if (found.st_dev, found.st_ino) == (wanted.st_dev, wanted.st_ino):
            return substitute
        return real(path, dir_fd=dir_fd)

    return readlink


def _windows_link_tree(tmp_path: Path) -> Path:
    root = tmp_path / "root-a"
    (root / "data").mkdir(parents=True)
    (root / "data" / "notes.txt").write_bytes(b"plain bytes\n")
    (root / "links").mkdir()
    os.symlink("../data/notes.txt", root / "links" / "canonical")
    return root


def test_a_windows_substitute_name_is_classified_the_way_windows_spells_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute link pushed from Windows travels as an org path.

    The failure this pins is silent rather than loud: read by the POSIX rules
    a drive-absolute target is not absolute at all, so it is stored verbatim
    as a relative link and the pusher's own drive is what the machine that
    pulls it ends up pointing at.
    """
    root = _windows_link_tree(tmp_path)
    monkeypatch.setattr(walk_module, "_WINDOWS", True)
    monkeypatch.setattr(
        os,
        "readlink",
        _windows_readlink(
            root / "links" / "canonical", rb"\\?\C:\Users\runner\root-a\data\notes.txt"
        ),
    )

    entries = index(root, local_root=rb"C:\Users\runner\root-a")

    entry = entries[b"links/canonical"]
    assert (entry.link_kind, entry.link_target) == (LinkKind.CANONICAL, b"/data/notes.txt")


def test_a_windows_substitute_name_outside_the_root_is_a_host_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute, but under no root this walk knows: stored verbatim.

    The kind is the whole assertion. ``host`` and ``relative`` are both written
    back byte for byte, so mislabelling one as the other is invisible on the
    machine that pushed it — until a target under a *differently* mounted root
    on the far side is re-anchored, or refused, on the strength of that label.
    """
    root = _windows_link_tree(tmp_path)
    substitute = rb"\\?\C:\Users\runner\outside\env"
    monkeypatch.setattr(walk_module, "_WINDOWS", True)
    monkeypatch.setattr(os, "readlink", _windows_readlink(root / "links" / "canonical", substitute))

    entry = index(root, local_root=rb"C:\Users\runner\root-a")[b"links/canonical"]

    assert (entry.link_kind, entry.link_target) == (
        LinkKind.HOST,
        rb"C:\Users\runner\outside\env",
    )


def test_a_posix_absolute_target_is_not_absolute_to_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/usr/bin/env`` is rooted without a drive, so Windows reads it relative.

    It names nothing fixed on the machine that pulls it — the current drive
    decides — so it travels verbatim as a ``relative`` link rather than as the
    ``host`` link the same text is on POSIX. Pinned because it is the reason a
    corpus cannot use a POSIX absolute path as its host-link fixture.
    """
    root = _windows_link_tree(tmp_path)
    monkeypatch.setattr(walk_module, "_WINDOWS", True)
    monkeypatch.setattr(
        os, "readlink", _windows_readlink(root / "links" / "canonical", b"/usr/bin/env")
    )

    entry = index(root, local_root=rb"C:\Users\runner\root-a")[b"links/canonical"]

    assert (entry.link_kind, entry.link_target) == (LinkKind.RELATIVE, b"/usr/bin/env")


def test_the_same_target_on_a_posix_machine_is_an_ordinary_relative_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative twin: those bytes are a legal POSIX name, not a drive.

    A machine that does not spell paths that way must not reinterpret one —
    the name is stored byte for byte and written back byte for byte.
    """
    root = _windows_link_tree(tmp_path)
    substitute = rb"\\?\C:\Users\runner\root-a\data\notes.txt"
    monkeypatch.setattr(os, "readlink", _windows_readlink(root / "links" / "canonical", substitute))

    entry = index(root, local_root=rb"C:\Users\runner\root-a")[b"links/canonical"]

    assert (entry.link_kind, entry.link_target) == (LinkKind.RELATIVE, substitute)


def test_an_entry_removed_between_the_listing_and_its_stat_leaves_the_walk_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walked tree is live — a person deletes a folder while an export runs,
    a mount rewrites its own temp files under the root. An entry that is gone
    by the time the walker stats it is left out of this walk instead of ending
    it with FileNotFoundError; an export over a 10k-file folder must not die on
    the one file that moved."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "gone").mkdir()
    (root / "gone" / "inner.txt").write_text("x")
    (root / "z.txt").write_text("z")
    real_scandir = os.scandir

    class _ListedThenRemoved:
        """The real listing of the root; the moment it closes — after the walker
        has its entries and before it stats them — one child is removed."""

        def __init__(self, path: Any) -> None:
            self._scan = real_scandir(path)
            # The walk lists a directory through its descriptor where it can.
            listed = os.fstat(path) if isinstance(path, int) else os.stat(path)
            self._is_root = os.path.samestat(listed, os.stat(root))

        def __enter__(self) -> Any:
            return self._scan.__enter__()

        def __exit__(self, *exc: object) -> None:
            self._scan.__exit__(*exc)
            if self._is_root and (root / "gone").exists():
                (root / "gone" / "inner.txt").unlink()
                (root / "gone").rmdir()

    monkeypatch.setattr(os, "scandir", _ListedThenRemoved)
    names = [entry.relative for entry in walk(root)]
    assert names == [b"a.txt", b"z.txt"]


def test_an_entry_removed_after_its_stat_but_before_its_xattrs_leaves_the_walk_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stat is not the last read that describes an entry: the link target
    and the xattrs follow it, and each is a moment for the entry to go. The
    gate saw exactly this — the stat succeeded, ``os.listxattr`` on the same
    path did not. An entry that vanishes anywhere in that sequence is left out
    of this walk, never a crash of the export."""
    # `alkera_cli.files` re-exports the walk *function* under the module's
    # name, so the module itself is reached through the import system.
    walk_module = importlib.import_module("alkera_cli.files.walk")

    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "fleeting.txt").write_text("f")
    (root / "z.txt").write_text("z")
    real_read_xattrs = walk_module.read_xattrs

    def _gone_before_its_xattrs(path: Path) -> dict[str, bytes]:
        if path.name == "fleeting.txt":
            path.unlink()
            # What os.listxattr answers for a path that is no longer there.
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_read_xattrs(path)

    monkeypatch.setattr(walk_module, "read_xattrs", _gone_before_its_xattrs)
    names = [entry.relative for entry in walk(root)]
    assert names == [b"a.txt", b"z.txt"]


def test_a_preset_scoped_to_a_directory_folds_only_beneath_it(tmp_path: Path) -> None:
    """The hand-back scopes the tool-cache preset to the working directory: the
    same names at any depth under it fold, and beside it they travel."""
    for relative in (
        "scratch/.venv/bin/python",
        "scratch/app/node_modules/dep/index.js",
        "scratch/src/__pycache__/m.pyc",
        "scratch/src/m.py",
        "node_modules/dep/index.js",
        "scratchpad/.venv/bin/python",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(b"x")

    within = {"scratch": ("caches",)}
    carried = sorted(
        e.relative
        for e in walk(tmp_path, respect_gitignore=False, exclude_presets_within=within)
        if e.skipped is None and e.kind is EntryKind.FILE
    )

    assert carried == [
        b"node_modules/dep/index.js",
        b"scratch/src/m.py",
        b"scratchpad/.venv/bin/python",
    ]
    rules = ExportRules.load(tmp_path, respect_gitignore=False, exclude_presets_within=within)
    assert (
        rules.skip_reason(b"scratch/app/node_modules/x.js", is_dir=False)
        is SkipReason.EXCLUDE_PRESET
    )
    assert rules.skip_reason(b"node_modules/x.js", is_dir=False) is None
