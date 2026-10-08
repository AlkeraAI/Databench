"""The chat tree seam: what the daemon writes is the sandbox's and editable by
it, and nothing it does follows a link, opens a special file or leaves the
root."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.files import chat_fs
from alkera_cli.files.chat_fs import ChatTree, ChatTreeError, TreeIdentity

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes, links and fifos")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _me() -> TreeIdentity:
    return TreeIdentity(uid=os.getuid(), gid=os.getgid())


def _other_group() -> int | None:
    """A group this user belongs to besides its primary one: a chown to it
    needs no privilege, so a test can watch ownership change."""
    for gid in os.getgroups():
        if gid != os.getgid():
            return gid
    return None


@pytest.fixture(params=[True, False], ids=["descriptors", "paths"])
def walk(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    """Every case runs on the descriptor walk and on the path fallback the
    platforms without ``dir_fd`` use."""
    if request.param and not chat_fs._DIR_FDS:
        pytest.skip("this platform has no dir_fd")
    monkeypatch.setattr(chat_fs, "_DIR_FDS", request.param)
    yield request.param


@pytest.fixture
def tree(tmp_path: Path) -> ChatTree:
    root = tmp_path / "chat"
    root.mkdir()
    return ChatTree(root, _me() if os.name == "posix" else None)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory beside the tree holding a file nothing may touch."""
    there = tmp_path / "outside"
    there.mkdir()
    (there / "secret.txt").write_bytes(b"secret")
    os.chmod(there / "secret.txt", 0o600)
    return there


# -- writes -----------------------------------------------------------------


def test_a_write_over_a_stamp_leaves_a_file_that_moved_alone(walk: bool, tree: ChatTree) -> None:
    """The text peer writes the document over a file only while it still
    wears the stamp the peer read it at: a save of the agent's that landed in
    between is never replaced, and nothing (no temp file) is left behind."""
    tree.write("plan.txt", b"one\n")
    before = tree.stat("plan.txt")
    assert before is not None
    stamp = (before.st_size, before.st_mtime_ns)
    (tree.root / "plan.txt").write_bytes(b"one\nagent\n")
    assert tree.write("plan.txt", b"from the document\n", over=stamp) is False
    assert (tree.root / "plan.txt").read_bytes() == b"one\nagent\n"
    assert sorted(p.name for p in tree.root.iterdir()) == ["plan.txt"]
    now = tree.stat("plan.txt")
    assert now is not None
    assert tree.write("plan.txt", b"merged\n", over=(now.st_size, now.st_mtime_ns)) is True
    assert (tree.root / "plan.txt").read_bytes() == b"merged\n"


def _agent_saves_after_the_look(
    monkeypatch: pytest.MonkeyPatch, where: Path, saves: list[bytes]
) -> None:
    """The agent saves ``where`` right after the stamp is first looked at,
    before the write replaces the file: the window a check and a rename
    leave. A save is written whole and renamed in, as an editor does."""
    real = chat_fs._wears
    looked: list[bool] = []

    def look(info: os.stat_result | None, stamp: tuple[int, int]) -> bool:
        answer = real(info, stamp)
        if not looked:
            looked.append(True)
            for save in saves:
                staged = where.with_name(f".{where.name}.agent")
                staged.write_bytes(save)
                os.replace(staged, where)
        return answer

    monkeypatch.setattr(chat_fs, "_wears", look)


def _write_over_its_stamp(tree: ChatTree, monkeypatch: pytest.MonkeyPatch, *saves: bytes) -> bool:
    tree.write("plan.txt", b"one\n")
    before = tree.stat("plan.txt")
    assert before is not None
    _agent_saves_after_the_look(monkeypatch, tree.root / "plan.txt", list(saves))
    return tree.write("plan.txt", b"from the document\n", over=(before.st_size, before.st_mtime_ns))


native_swap = pytest.mark.skipif(
    not chat_fs._DIR_FDS or chat_fs._EXCHANGE is None, reason="no atomic swap on this platform"
)


@native_swap
def test_an_agent_save_landing_between_the_look_and_the_replace_is_never_lost(
    tree: ChatTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file still wore the stamp when it was looked at, and the agent
    saved before the replace. Replaced by name, the save was gone. The new
    file is swapped in and the one that came out looked at: it is the
    agent's, so it goes back, and the write is refused as for a file that
    moved before the look."""
    written = _write_over_its_stamp(tree, monkeypatch, b"one\nagent\n")

    assert (tree.root / "plan.txt").read_bytes() == b"one\nagent\n"
    assert sorted(p.name for p in tree.root.iterdir()) == ["plan.txt"]
    assert written is False


@native_swap
def test_an_agent_saving_again_after_the_swap_keeps_its_newest_save(
    tree: ChatTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent saved in the window and once more right after the swap: the
    swap back brings out its newest save, which is put back under the name;
    the save it replaced is dropped, as its own write dropped it."""
    real = chat_fs._EXCHANGE
    assert real is not None
    swaps: list[bool] = []

    def exchange(parent: int, first: str, second: str) -> bool:
        done = real(parent, first, second)
        if not swaps:
            swaps.append(True)
            staged = tree.root / ".plan.txt.agent2"
            staged.write_bytes(b"one\nagent\nagent again\n")
            os.replace(staged, tree.root / "plan.txt")
        return done

    monkeypatch.setattr(chat_fs, "_EXCHANGE", exchange)

    written = _write_over_its_stamp(tree, monkeypatch, b"one\nagent\n")

    assert (tree.root / "plan.txt").read_bytes() == b"one\nagent\nagent again\n"
    assert sorted(p.name for p in tree.root.iterdir()) == ["plan.txt"]
    assert written is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="renameat2 is Linux's")
def test_linux_swaps_with_renameat2(tree: ChatTree, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``renameat2(RENAME_EXCHANGE)`` path: found, and it keeps the
    agent's save landing in the window."""
    assert chat_fs._DIR_FDS and chat_fs._EXCHANGE is not None
    written = _write_over_its_stamp(tree, monkeypatch, b"one\nagent\n")
    assert (tree.root / "plan.txt").read_bytes() == b"one\nagent\n"
    assert written is False


def test_a_platform_that_cannot_swap_still_writes_over_an_unmoved_file(
    walk: bool, tree: ChatTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no swap (another platform, an old kernel, a filesystem that
    cannot), a file still wearing its stamp is replaced as before."""
    monkeypatch.setattr(chat_fs, "_EXCHANGE", None)
    tree.write("plan.txt", b"one\n")
    before = tree.stat("plan.txt")
    assert before is not None
    assert tree.write("plan.txt", b"two\n", over=(before.st_size, before.st_mtime_ns)) is True
    assert (tree.root / "plan.txt").read_bytes() == b"two\n"
    assert sorted(p.name for p in tree.root.iterdir()) == ["plan.txt"]


def test_a_filesystem_that_refuses_to_swap_falls_back_to_replacing(
    tree: ChatTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not chat_fs._DIR_FDS:
        pytest.skip("this platform has no dir_fd")
    monkeypatch.setattr(chat_fs, "_EXCHANGE", lambda _parent, _first, _second: False)
    tree.write("plan.txt", b"one\n")
    before = tree.stat("plan.txt")
    assert before is not None
    assert tree.write("plan.txt", b"two\n", over=(before.st_size, before.st_mtime_ns)) is True
    assert (tree.root / "plan.txt").read_bytes() == b"two\n"
    assert sorted(p.name for p in tree.root.iterdir()) == ["plan.txt"]


@posix_only
@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(None, 0o660, id="new-file"),
        pytest.param(0o444, 0o660, id="read-only-upload-becomes-editable"),
        pytest.param(0o600, 0o660, id="private-opens-to-the-group"),
        pytest.param(0o755, 0o770, id="executable-keeps-x"),
        pytest.param(0o700, 0o770, id="owner-only-executable"),
    ],
)
def test_a_write_leaves_a_file_the_sandbox_can_edit(
    tree: ChatTree, walk: bool, before: int | None, after: int
) -> None:
    where = tree.root / "notes" / "a.txt"
    if before is not None:
        where.parent.mkdir()
        where.write_bytes(b"old")
        os.chmod(where, before)
    tree.write("notes/a.txt", b"new")
    assert where.read_bytes() == b"new"
    if walk:
        assert _mode(where) == after
        info = os.stat(where)
        assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid())
    # The agent (here, the same uid) opens it for writing in place.
    with where.open("r+b") as handle:
        handle.write(b"N")
    assert where.read_bytes() == b"New"


@posix_only
def test_an_explicit_executable_flag_wins_over_the_old_mode(tree: ChatTree) -> None:
    where = tree.root / "run.sh"
    where.write_bytes(b"")
    os.chmod(where, 0o755)
    tree.write("run.sh", b"echo", executable=False)
    assert _mode(where) == 0o660
    tree.write("run.sh", b"echo", executable=True)
    assert _mode(where) == 0o770


@posix_only
def test_directories_a_write_creates_are_the_sandboxs(tree: ChatTree) -> None:
    tree.write("a/b/c.txt", b"x")
    for directory in (tree.root / "a", tree.root / "a" / "b"):
        assert stat.S_IMODE(os.stat(directory).st_mode) & 0o770 == 0o770


@posix_only
def test_a_write_to_another_group_owns_the_file_by_it(tmp_path: Path) -> None:
    gid = _other_group()
    if gid is None:
        pytest.skip("this user is in no second group")
    root = tmp_path / "chat"
    root.mkdir()
    tree = ChatTree(root, TreeIdentity(uid=os.getuid(), gid=gid))
    tree.write("deep/f.txt", b"x")
    assert os.stat(root / "deep" / "f.txt").st_gid == gid
    assert os.stat(root / "deep").st_gid == gid


def test_a_write_streams_from_a_file_and_leaves_no_temporary(
    tree: ChatTree, walk: bool, tmp_path: Path
) -> None:
    source = tmp_path / "download"
    source.write_bytes(b"z" * (3 << 20))
    tree.install("big.bin", source)
    assert (tree.root / "big.bin").read_bytes() == source.read_bytes()
    assert sorted(p.name for p in tree.root.iterdir()) == ["big.bin"]


@posix_only
def test_a_link_at_the_leaf_is_refused_and_its_target_untouched(
    tree: ChatTree, walk: bool, outside: Path
) -> None:
    (tree.root / "a.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(ChatTreeError):
        tree.write("a.txt", b"pwned")
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert _mode(outside / "secret.txt") == 0o600
    assert (tree.root / "a.txt").is_symlink()


@posix_only
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda t: t.write("door/a.txt", b"pwned"), id="write"),
        pytest.param(lambda t: t.mkdirs("door/new"), id="mkdirs"),
        pytest.param(lambda t: t.read_bytes("door/secret.txt"), id="read"),
        pytest.param(lambda t: t.unlink("door/secret.txt"), id="unlink"),
        pytest.param(lambda t: t.rename("door/secret.txt", "taken.txt"), id="rename-from"),
        pytest.param(lambda t: t.link("door/secret.txt", "taken.txt"), id="link-from"),
        pytest.param(lambda t: t.open_write("door/part").__enter__(), id="open-write"),
    ],
)
def test_a_link_above_the_leaf_is_never_crossed(
    tree: ChatTree, walk: bool, outside: Path, operation: object
) -> None:
    (tree.root / "door").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ChatTreeError, FileNotFoundError)):
        operation(tree)  # type: ignore[operator]
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert not (tree.root / "taken.txt").exists()


@posix_only
def test_a_fifo_is_refused_without_blocking(tree: ChatTree, walk: bool) -> None:
    os.mkfifo(tree.root / "pipe")
    with pytest.raises(ChatTreeError):
        tree.read_bytes("pipe")
    with pytest.raises(ChatTreeError):
        tree.write("pipe", b"x")
    with pytest.raises(ChatTreeError), tree.open_write("pipe"):
        pass
    assert stat.S_ISFIFO(os.lstat(tree.root / "pipe").st_mode)


@posix_only
def test_a_link_is_never_read_through(tree: ChatTree, walk: bool, outside: Path) -> None:
    (tree.root / "a.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(ChatTreeError):
        tree.read_bytes("a.txt")
    assert not tree.is_file("a.txt")


def test_a_directory_is_not_written_over_or_unlinked(tree: ChatTree, walk: bool) -> None:
    (tree.root / "d").mkdir()
    (tree.root / "d" / "keep").write_bytes(b"k")
    with pytest.raises(ChatTreeError):
        tree.write("d", b"x")
    with pytest.raises(ChatTreeError):
        tree.unlink("d")
    tree.write("f", b"f")
    with pytest.raises(ChatTreeError):
        tree.rename("f", "d")
    assert (tree.root / "d" / "keep").read_bytes() == b"k"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../escape", id="dotdot"),
        pytest.param("a/../../escape", id="inner-dotdot"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("", id="empty"),
        pytest.param("a//b", id="empty-segment"),
        pytest.param("a/./b", id="dot"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_a_path_that_is_not_inside_the_tree_is_refused(tree: ChatTree, path: str) -> None:
    with pytest.raises(ChatTreeError):
        tree.write(path, b"x")


def test_an_absolute_path_is_measured_against_the_root(tree: ChatTree, tmp_path: Path) -> None:
    tree.write(tree.root / "in.txt", b"x")
    assert (tree.root / "in.txt").read_bytes() == b"x"
    with pytest.raises(ChatTreeError):
        tree.write(tmp_path / "beside.txt", b"x")
    assert not (tmp_path / "beside.txt").exists()


# -- moves ------------------------------------------------------------------


def test_rename_replaces_and_creates_directories(tree: ChatTree, walk: bool) -> None:
    tree.write("a.txt", b"a")
    tree.write("b/b.txt", b"b")
    tree.rename("a.txt", "b/b.txt")
    assert (tree.root / "b" / "b.txt").read_bytes() == b"a"
    assert not (tree.root / "a.txt").exists()
    tree.rename("b/b.txt", "c/d/e.txt")
    assert (tree.root / "c" / "d" / "e.txt").read_bytes() == b"a"
    with pytest.raises(FileNotFoundError):
        tree.rename("gone", "x")


def test_link_names_one_file_twice(tree: ChatTree, walk: bool) -> None:
    tree.write("a.txt", b"a")
    tree.link("a.txt", "kept.txt")
    assert tree.samefile("a.txt", "kept.txt")
    tree.write("a.txt", b"new")
    assert not tree.samefile("a.txt", "kept.txt")
    assert (tree.root / "kept.txt").read_bytes() == b"a"


@posix_only
def test_moving_a_link_is_refused(tree: ChatTree, walk: bool, outside: Path) -> None:
    (tree.root / "l").symlink_to(outside / "secret.txt")
    with pytest.raises(ChatTreeError):
        tree.rename("l", "moved")
    with pytest.raises(ChatTreeError):
        tree.link("l", "second")
    assert not (tree.root / "second").exists()


@posix_only
def test_unlink_removes_a_link_and_never_its_target(
    tree: ChatTree, walk: bool, outside: Path
) -> None:
    (tree.root / "l").symlink_to(outside / "secret.txt")
    tree.unlink("l")
    assert not (tree.root / "l").is_symlink()
    assert (outside / "secret.txt").read_bytes() == b"secret"
    tree.unlink("never-there")
    with pytest.raises(FileNotFoundError):
        tree.unlink("never-there", missing_ok=False)


def test_open_write_appends_a_partial_in_place(tree: ChatTree, walk: bool) -> None:
    with tree.open_write("dl.part") as handle:
        handle.write(b"abc")
    with tree.open_write("dl.part", append=True) as handle:
        handle.write(b"def")
    assert tree.read_bytes("dl.part") == b"abcdef"
    with tree.open_write("dl.part") as handle:
        handle.write(b"x")
    assert tree.read_bytes("dl.part") == b"x"
    if walk and os.name == "posix":
        assert _mode(tree.root / "dl.part") == 0o660


# -- repair -----------------------------------------------------------------


@posix_only
def test_repair_normalizes_modes_and_never_follows_a_link(tree: ChatTree, outside: Path) -> None:
    root = tree.root
    (root / "sub").mkdir(mode=0o700)
    (root / "sub" / "ro.txt").write_bytes(b"r")
    os.chmod(root / "sub" / "ro.txt", 0o444)
    (root / "tool").write_bytes(b"#!")
    os.chmod(root / "tool", 0o700)
    (root / "sub" / "out").symlink_to(outside / "secret.txt")
    (root / "door").symlink_to(outside, target_is_directory=True)
    os.mkfifo(root / "pipe")
    report = tree.repair()
    assert _mode(root / "sub") == chat_fs.DIR_MODE
    assert _mode(root / "sub" / "ro.txt") == 0o660
    assert _mode(root / "tool") == 0o770
    assert _mode(outside / "secret.txt") == 0o600
    assert report.modes >= 3
    assert report.skipped >= 1  # the fifo
    again = tree.repair()
    assert (again.modes, again.owners) == (0, 0)


@posix_only
def test_repair_re_owns_mixed_owners(tmp_path: Path, outside: Path) -> None:
    gid = _other_group()
    if gid is None:
        pytest.skip("this user is in no second group")
    root = tmp_path / "chat"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f").write_bytes(b"f")
    (root / "g").write_bytes(b"g")
    (root / "out").symlink_to(outside / "secret.txt")
    before = os.stat(outside / "secret.txt").st_gid
    tree = ChatTree(root, TreeIdentity(uid=os.getuid(), gid=gid))
    report = tree.repair()
    for path in (root, root / "a", root / "a" / "f", root / "g"):
        assert os.stat(path).st_gid == gid
    assert os.stat(outside / "secret.txt").st_gid == before
    assert report.owners >= 4


# -- identity ---------------------------------------------------------------


def test_the_resolver_can_be_repointed_and_restored(tmp_path: Path) -> None:
    chosen = TreeIdentity(uid=4242, gid=4343)
    previous = chat_fs.set_identity_resolver(lambda chat: chosen if chat == "c1" else None)
    try:
        assert chat_fs.identity_for("c1") == chosen
        assert ChatTree.for_chat(tmp_path, "c1").identity == chosen
        assert ChatTree.for_chat(tmp_path, "c2").identity is None
        assert ChatTree.for_chat(tmp_path, None).identity is None
    finally:
        chat_fs.set_identity_resolver(previous)
    chat_fs.set_identity_resolver(None)
    assert not chat_fs.has_identity_resolver()
    assert chat_fs.identity_for("c1") is None


# -- hard links ---------------------------------------------------------------


@posix_only
def test_a_hard_link_at_the_leaf_is_not_written_in_place(
    tree: ChatTree, walk: bool, outside: Path
) -> None:
    """A second name for a host file's bytes: an append or an in-place open
    would write the host's file and a hand-over would re-own it, so the open
    refuses it. A whole write lands a fresh file under the name, and the host
    file keeps its bytes, its mode and its one remaining name."""
    try:
        os.link(outside / "secret.txt", tree.root / "twin.txt")
    except PermissionError:
        pytest.skip("this host forbids hard links to files one does not own")
    with pytest.raises(ChatTreeError, match="hard link"), tree.open_write("twin.txt"):
        pass
    with (
        pytest.raises(ChatTreeError, match="hard link"),
        tree.open_write("twin.txt", append=True),
    ):
        pass
    assert (outside / "secret.txt").read_bytes() == b"secret", "the refused open truncated nothing"
    tree.write("twin.txt", b"mine")
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert os.stat(outside / "secret.txt").st_nlink == 1
    assert _mode(outside / "secret.txt") == 0o600
    assert tree.read_bytes("twin.txt") == b"mine"


@posix_only
def test_repair_leaves_a_hard_linked_file_as_it_is(tree: ChatTree, outside: Path) -> None:
    """The repair re-owns and re-modes the chat's files; a file that is also a
    host file's second name is skipped, since the chown and chmod would reach
    the host's."""
    try:
        os.link(outside / "secret.txt", tree.root / "twin.txt")
    except PermissionError:
        pytest.skip("this host forbids hard links to files one does not own")
    (tree.root / "own.txt").write_bytes(b"o")
    os.chmod(tree.root / "own.txt", 0o600)
    report = tree.repair()
    assert _mode(tree.root / "own.txt") == 0o660
    assert _mode(outside / "secret.txt") == 0o600
    assert report.skipped >= 1 and report.modes >= 1


# -- links the tree makes ------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "target", "inside"),
    [
        pytest.param(["latest"], b"alpha.txt", True, id="a-sibling"),
        pytest.param(["d", "l"], b"../alpha.txt", True, id="up-to-the-root"),
        pytest.param(["d", "l"], b"sub/../x", True, id="a-dot-dot-that-stays"),
        pytest.param(["l"], b"..", False, id="the-roots-parent"),
        pytest.param(["l"], b"../x", False, id="climbing-out-from-the-top"),
        pytest.param(["d", "l"], b"../../x", False, id="climbing-out-from-below"),
        pytest.param(["l"], b"/opt/alkera-home/auth.yml", False, id="a-host-path"),
        pytest.param(["l"], b"/", False, id="the-host-root"),
        pytest.param(["l"], b"C:\\x", False, id="a-drive-path"),
        pytest.param(["l"], b"", False, id="empty"),
    ],
)
def test_a_links_target_is_inside_only_when_it_never_leaves_the_root(
    link: list[str], target: bytes, inside: bool
) -> None:
    assert chat_fs.link_stays_inside(link, target) is inside


@posix_only
def test_a_link_to_a_host_path_is_refused_and_nothing_is_made(
    tree: ChatTree, walk: bool, outside: Path
) -> None:
    """The report that started this: a drive held ``dl-host-auth ->
    /opt/alkera-home/auth.yml`` and the box, pulling as root, made the link in
    the chat's tree. The sandbox could not open it, but a root daemon planting
    a door to a host path is refused at the seam, with its reason."""
    for target in (os.fsencode(outside / "secret.txt"), b"../outside/secret.txt", b"/etc"):
        with pytest.raises(ChatTreeError, match="outside the tree") as caught:
            tree.symlink("dl-host-auth", target)
        assert caught.value.errno == errno.EXDEV
        assert not (tree.root / "dl-host-auth").is_symlink()
    assert not (tree.root / "dl-host-auth").exists()


@posix_only
def test_an_absolute_target_under_the_root_is_written_relative(tree: ChatTree, walk: bool) -> None:
    """A canonical link is re-anchored at the root that pulled it as an
    absolute host path; a sandbox sees the root at its own path, so the tree
    writes the relative spelling, which resolves wherever the tree is
    mounted, and says so before writing."""
    (tree.root / "sub").mkdir()
    (tree.root / "sub" / "alpha.txt").write_bytes(b"alpha")
    (tree.root / "d").mkdir()
    assert (
        tree.link_text("latest", os.fsencode(tree.root / "sub" / "alpha.txt")) == b"sub/alpha.txt"
    )
    assert (
        tree.link_text("d/l", os.fsencode(tree.root / "sub" / "alpha.txt")) == b"../sub/alpha.txt"
    )
    assert tree.link_text("d/root", os.fsencode(tree.root)) == b".."
    tree.symlink("d/l", os.fsencode(tree.root / "sub" / "alpha.txt"))
    assert os.readlink(tree.root / "d" / "l") == "../sub/alpha.txt"
    assert (tree.root / "d" / "l").read_bytes() == b"alpha"
    # A relative target inside stays as it was given; a second write replaces the link.
    tree.symlink("d/l", b"../sub/alpha.txt")
    tree.symlink("latest", b"sub/alpha.txt")
    assert os.readlink(tree.root / "latest") == "sub/alpha.txt"
    assert tree.readlink("d/l") == b"../sub/alpha.txt"
