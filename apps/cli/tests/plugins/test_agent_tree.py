"""The tools' writes below the agent's root follow nothing the agent planted,
leave what they make the chat's, and make nothing before something is written.

On a box the daemon is root and the root folder is the agent's to write, so
every file a tool makes there (a spilled result, a payload, a materialized
blob) goes through the chat tree (``files/chat_fs.py``) by way of
``agent_tree``: each component below the root with no link followed, the leaf
without blocking, only a plain file written, and the result the chat's with
the tree's modes. Each case plants what an agent could leave at the name and
pins that the write is refused and the target untouched; the plain cases pin
that nothing honest is refused, that what is made is the chat's, and that the
tool-output directory exists only once something spilled into it. The owner
cases pin whose identity a tool's files are handed to, for a session and for
a subagent child that shares its parent's tree.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files import chat_fs
from alkera_cli.files.chat_fs import DIR_MODE, FILE_MODE, ChatTree, ChatTreeError, TreeIdentity
from alkera_cli.plugins.plugin_base.agent_tree import (
    SpillTarget,
    ToolOutput,
    tool_output,
    tool_owner,
    tool_tree,
)
from alkera_cli.plugins.plugin_base.tool import ToolContext

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="links and FIFOs are POSIX")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _ctx(**fields: Any) -> ToolContext:
    """A tool context with the handles ``agent_tree`` reads: a local session
    unless a fence is given, with no registry or blob store behind it."""
    return ToolContext(registry=None, blobs=None, **fields)  # type: ignore[arg-type]


@pytest.fixture
def closed_umask() -> object:
    """The daemon's umask may close everything to group and other; what it
    makes for the agent must still be the sandbox's to read and write."""
    before = os.umask(0o077)
    yield None
    os.umask(before)


@pytest.fixture
def restored_resolver() -> object:
    previous = chat_fs.set_identity_resolver(None)
    yield None
    chat_fs.set_identity_resolver(previous)


# -- what is made, and when ------------------------------------------------------


def test_a_spill_makes_the_directory_and_the_file_with_the_trees_modes_whatever_the_umask(
    tmp_path: Path, closed_umask: object
) -> None:
    output = ToolOutput(ChatTree(tmp_path))
    target = output.spill("bash")
    assert target.path.parent == tmp_path / "tool-output"
    assert not output.path.exists(), "naming a spill makes nothing"
    with target.open_append() as f:
        f.write(b"hello\n")
    assert target.path.read_text() == "hello\n"
    assert _mode(target.path) == FILE_MODE and _mode(output.path) == DIR_MODE


def test_two_spills_never_share_a_name_and_a_payload_is_named_as_asked(tmp_path: Path) -> None:
    output = ToolOutput(ChatTree(tmp_path))
    first, second = output.spill("graph-out"), output.spill("graph-out")
    assert first.path != second.path
    assert first.path.name.startswith("graph-out-") and first.path.suffix == ".txt"
    made = output.write_text("graph-call-1.json", "{}")
    assert made == tmp_path / "tool-output" / "graph-call-1.json" and made.read_text() == "{}"
    output.unlink("graph-call-1.json")
    assert not made.exists()


def test_a_spill_file_is_appended_to_and_replaced_as_asked(tmp_path: Path) -> None:
    target = SpillTarget(ChatTree(tmp_path), "d/f")
    with target.open_append() as f:
        f.write(b"one\n")
    with target.open_append() as f:
        f.write(b"two\n")
    assert (tmp_path / "d" / "f").read_text() == "one\ntwo\n"
    target.write_text("three\n")
    assert (tmp_path / "d" / "f").read_text() == "three\n"


def test_the_root_itself_may_be_reached_through_a_link(tmp_path: Path) -> None:
    """A temp directory on macOS is one; only what lies below the root is
    the agent's to redirect."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    ToolOutput(ChatTree(link)).write_text("f", "x")
    assert (real / "tool-output" / "f").read_text() == "x"


# -- what the agent could plant at the name --------------------------------------


def test_a_link_at_the_name_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    victim = tmp_path / "host" / "auth.yml"
    victim.parent.mkdir()
    victim.write_text("token: ORIGINAL\n")
    root = tmp_path / "root"
    (root / "tool-output").mkdir(parents=True)
    (root / "tool-output" / "f").symlink_to(victim)
    output = ToolOutput(ChatTree(root))
    with pytest.raises(ChatTreeError):
        output.write_text("f", "token: STOLEN\n")
    with pytest.raises(ChatTreeError), output.target("f").open_append():
        pass
    assert victim.read_text() == "token: ORIGINAL\n"
    (root / "tool-output" / "g").symlink_to(tmp_path / "host" / "never-made")
    with pytest.raises(ChatTreeError):
        output.write_text("g", "x")
    assert not (tmp_path / "host" / "never-made").exists()


def test_a_hard_link_at_the_name_is_not_written_through(tmp_path: Path) -> None:
    """A second name for a host file's bytes (where the kernel allows the link)
    would be written in place by an append; the open refuses it, and a whole
    write lands a new file under the name, leaving the host's bytes as they
    were."""
    victim = tmp_path / "host" / "secret"
    victim.parent.mkdir()
    victim.write_text("ORIGINAL")
    root = tmp_path / "root"
    (root / "tool-output").mkdir(parents=True)
    os.link(victim, root / "tool-output" / "f")
    output = ToolOutput(ChatTree(root))
    with pytest.raises(ChatTreeError, match="hard link"), output.target("f").open_append():
        pass
    assert victim.read_text() == "ORIGINAL"
    output.write_text("f", "replaced")
    assert victim.read_text() == "ORIGINAL"
    assert (root / "tool-output" / "f").read_text() == "replaced"
    assert (root / "tool-output" / "f").stat().st_nlink == 1


def test_a_link_in_the_directorys_place_is_refused_and_nothing_lands_behind_it(
    tmp_path: Path,
) -> None:
    """The directory swapped for a link to a host directory: the write would
    land there under an honest name. Refused at the directory, so neither
    the open nor the write reaches the target."""
    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "tool-output").symlink_to(elsewhere)
    output = ToolOutput(ChatTree(root))
    with pytest.raises(ChatTreeError):
        output.write_text("cron", "* * * * * root sh\n")
    with pytest.raises(ChatTreeError), output.spill("bash").open_append():
        pass
    assert list(elsewhere.iterdir()) == []


def test_a_fifo_at_the_name_is_refused_without_waiting(tmp_path: Path) -> None:
    """A FIFO holds a writer until a reader comes; with nothing ever reading
    it, a plain open would hold the daemon for good."""
    (tmp_path / "tool-output").mkdir()
    os.mkfifo(tmp_path / "tool-output" / "f")
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            with ToolOutput(ChatTree(tmp_path)).target("f").open_append():
                pass
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "the open waited on the FIFO"
    assert isinstance(outcome[0], ChatTreeError)


def test_a_directory_or_a_device_at_the_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tool-output" / "f").mkdir(parents=True)
    output = ToolOutput(ChatTree(tmp_path))
    with pytest.raises(ChatTreeError):
        output.write_text("f", "x")
    (tmp_path / "tool-output" / "null").symlink_to("/dev/null")
    with pytest.raises(ChatTreeError):
        output.write_text("null", "x")


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("../x", id="dot-dot"),
        pytest.param("d/../x", id="dot-dot-inside"),
        pytest.param("", id="empty"),
    ],
)
def test_a_path_that_is_not_plainly_below_the_root_is_refused(
    tmp_path: Path, relative: str
) -> None:
    with pytest.raises(ChatTreeError):
        SpillTarget(ChatTree(tmp_path), relative).write_text("x")
    with pytest.raises(ChatTreeError), SpillTarget(ChatTree(tmp_path), relative).open_append():
        pass


def test_a_platform_that_walks_by_path_still_writes_and_refuses_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows runs no box, but a local session there still spills and
    materializes through the same calls, and the chat tree walks by path
    there: a plain write lands, a link at the name is still refused."""
    monkeypatch.setattr(chat_fs, "_DIR_FDS", False)
    output = ToolOutput(ChatTree(tmp_path))
    output.write_text("f", "plain\n")
    assert (tmp_path / "tool-output" / "f").read_text() == "plain\n"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "tool-output" / "link").symlink_to(victim)
    with pytest.raises(ChatTreeError):
        output.write_text("link", "stolen")
    assert victim.read_text() == "keep"


# -- whose the files are -----------------------------------------------------------


def _another_group() -> int:
    """A group this user belongs to that is not the one files get by default,
    so a hand-over to it is visible; the test skips where there is none."""
    others = [gid for gid in os.getgroups() if gid != os.getgid()]
    if not others:
        pytest.skip("this user belongs to one group only")
    return others[0]


def test_what_is_made_is_handed_to_the_owner_named(tmp_path: Path, closed_umask: object) -> None:
    """On a box the daemon is root and the chat's commands are the chat's uid:
    a file or directory made for the chat is handed to it, so the chat can
    write it afterwards. The hand-over is a real ``chown``; as an ordinary
    user the test can give away only a group it is in."""
    owner = TreeIdentity(uid=os.getuid(), gid=_another_group())
    output = ToolOutput(ChatTree(tmp_path, owner))
    output.write_text("out.txt", "x")
    made = output.path
    assert made.lstat().st_gid == owner.gid and _mode(made) == DIR_MODE
    assert (made / "out.txt").lstat().st_gid == owner.gid
    assert _mode(made / "out.txt") == FILE_MODE
    with output.target("log.txt").open_append() as f:
        f.write(b"x")
    assert (made / "log.txt").lstat().st_gid == owner.gid


def test_a_local_sessions_files_are_nobodys_to_hand_over(restored_resolver: object) -> None:
    """A local session's files are the person's own already: no resolver is
    asked, whatever one is installed."""
    asked: list[str] = []

    def resolver(chat_id: str) -> TreeIdentity | None:
        asked.append(chat_id)
        return TreeIdentity(uid=20031, gid=20031)

    chat_fs.set_identity_resolver(resolver)
    assert tool_owner(_ctx(session_id="chat-a")) is None
    assert tool_owner(_ctx(session_id="chat-a", identity_resolver=resolver)) is None
    assert asked == []


def test_a_bounded_sessions_files_are_handed_to_the_identity_its_tree_resolves_to(
    restored_resolver: object, tmp_path: Path
) -> None:
    """A bounded session asks the one resolver every daemon write asks (the
    box installs it at start); a host with no per-chat uid answers ``None``
    and the files stay the daemon's. An injected resolver stands in for it."""
    assert tool_owner(_ctx(session_id="chat-a", fence=object())) is None, "no box: nobody"
    installed: list[str] = []
    chat_fs.set_identity_resolver(
        lambda chat_id: (installed.append(chat_id), TreeIdentity(uid=20031, gid=20031))[1]
    )
    assert tool_owner(_ctx(session_id="chat-a", fence=object())) == TreeIdentity(20031, 20031)
    assert installed == ["chat-a"]
    injected = _ctx(
        session_id="chat-a",
        fence=object(),
        identity_resolver=lambda chat_id: TreeIdentity(uid=20099, gid=20099),
    )
    assert tool_owner(injected) == TreeIdentity(20099, 20099)
    assert installed == ["chat-a"], "an injected resolver is asked instead, not as well"
    assert tool_owner(_ctx(fence=object())) is None, "no session: nobody to hand to"
    tree = tool_tree(injected, tmp_path)
    assert tree.root == tmp_path and tree.identity == TreeIdentity(20099, 20099)


def test_a_subagent_childs_files_are_handed_to_its_parents_identity() -> None:
    """A child runs in its parent's tree as its parent's uid; what a tool makes
    there is the parent's, never a second identity the parent's commands
    could not write. The owner session rides the context, resolved once."""
    asked: list[str] = []

    def resolver(chat_id: str) -> TreeIdentity | None:
        asked.append(chat_id)
        return TreeIdentity(uid=20000 + len(chat_id), gid=20000 + len(chat_id))

    child = _ctx(
        session_id="child-1", owner_session_id="parent", fence=object(), identity_resolver=resolver
    )
    assert tool_owner(child) == resolver("parent")
    assert asked[0] == "parent" and "child-1" not in asked
    output = tool_output(child, fallback="x")
    assert output.tree.identity == TreeIdentity(20006, 20006)
