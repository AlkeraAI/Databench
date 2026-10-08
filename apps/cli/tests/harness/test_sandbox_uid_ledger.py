"""An org worker's chat uids: recorded in its own root, never handed twice."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox_uid
from alkera_cli.harness.sandbox_ownership import ENV_TREES_FLOOR, _ancestors
from alkera_cli.harness.sandbox_steps import SandboxRefusedError
from alkera_cli.harness.sandbox_uid_ledger import ENV_UID_LEDGER, ledger_path, ledger_uid
from freezegun import freeze_time

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="an org worker is Linux")


def test_a_chat_keeps_its_uid_and_two_chats_never_share_one(tmp_path: Path) -> None:
    ledger = tmp_path / "home" / "sandbox-uids.json"
    a = ledger_uid(ledger, "alkera-chat-a", low=20000, high=59999)
    b = ledger_uid(ledger, "alkera-chat-b", low=20000, high=59999)
    assert (a, b) == (20000, 20001)
    assert ledger_uid(ledger, "alkera-chat-a", low=20000, high=59999) == a
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_concurrent_starts_take_distinct_uids(tmp_path: Path) -> None:
    ledger = tmp_path / "uids.json"
    got: list[int] = []
    lock = threading.Lock()

    def take(i: int) -> None:
        uid = ledger_uid(ledger, f"alkera-chat-{i}", low=20000, high=59999)
        with lock:
            got.append(uid)

    threads = [threading.Thread(target=take, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 16


def test_a_full_range_and_an_unreadable_ledger_refuse(tmp_path: Path) -> None:
    ledger = tmp_path / "uids.json"
    ledger_uid(ledger, "one", low=20000, high=20000)
    with pytest.raises(SandboxRefusedError):
        ledger_uid(ledger, "two", low=20000, high=20000)
    ledger.write_text("{not json")
    with pytest.raises(SandboxRefusedError):
        ledger_uid(ledger, "one", low=20000, high=20000)


def test_with_a_ledger_no_host_user_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An org worker cannot write the host's user table and must not: its
    uids are its org's alone."""
    monkeypatch.setenv(ENV_UID_LEDGER, str(tmp_path / "uids.json"))
    ran: list[object] = []
    uid = sandbox_uid.ensure_chat_uid(
        "chat-1", run=lambda argv: ran.append(argv) or 0, lookup=lambda n: None
    )
    assert uid == sandbox_uid.UID_MIN and ran == []


def test_only_an_absolute_ledger_path_is_honoured() -> None:
    assert ledger_path({ENV_UID_LEDGER: "relative/uids.json"}) is None
    assert ledger_path({}) is None
    assert ledger_path({ENV_UID_LEDGER: "/opt/x/uids.json"}) == Path("/opt/x/uids.json")


def test_ancestor_grants_stop_at_the_org_root() -> None:
    """Above an org worker's root nothing is its to change; the way through is
    the directories' own modes, so no ACL is asked for there (and the step
    would fail on a directory the worker does not own)."""
    trees = (Path("/opt/alkera-work/orgs/2/work/.alkera/chats/c1/scratch"),)
    floor = Path("/opt/alkera-work/orgs/2")
    assert _ancestors(trees, floor) == [
        "/opt/alkera-work/orgs/2/work/.alkera/chats/c1",
        "/opt/alkera-work/orgs/2/work/.alkera/chats",
        "/opt/alkera-work/orgs/2/work/.alkera",
        "/opt/alkera-work/orgs/2/work",
        "/opt/alkera-work/orgs/2",
    ]
    assert "/opt/alkera-work" in _ancestors(trees)
    assert ENV_TREES_FLOOR == "ALKERA_SANDBOX_TREES_FLOOR"


# ---------------------------------------------------------------------------
# A ledger the disk cannot vouch for, and uids taken back
# ---------------------------------------------------------------------------

ME = os.getuid()


def _chat_tree(floor: Path, chat: str) -> Path:
    """A chat's folder under the org's root, owned (as everything this test
    makes) by this process's uid, which the tests make the range's first."""
    tree = floor / "work" / ".alkera" / "chats" / chat / "sandbox"
    tree.mkdir(parents=True)
    (tree / "notes.md").write_text("A1's private notes\n")
    return tree


@pytest.fixture
def floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "org"
    root.mkdir()
    monkeypatch.setenv(ENV_TREES_FLOOR, str(root))
    return root


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param('{"uids": {"alkera-chat-a1": ', id="truncated"),
        pytest.param('["not", "a", "ledger"]', id="wrong-shape"),
    ],
)
def test_an_unreadable_ledger_never_reissues_a_uid_that_owns_a_chats_tree(
    floor: Path, content: str
) -> None:
    """A1's tree is owned by the range's first uid; a crash left the ledger
    unreadable. A2 must be refused, never handed A1's uid (and with it A1's
    files)."""
    ledger = floor / "home" / "sandbox-uids.json"
    ledger.parent.mkdir()
    _chat_tree(floor, "a1")
    ledger.write_text(content)
    with pytest.raises(SandboxRefusedError, match="cannot be read"):
        ledger_uid(ledger, "alkera-chat-a2", low=ME, high=ME + 5)


def test_an_unreadable_ledger_with_no_chat_tree_starts_again(floor: Path) -> None:
    ledger = floor / "home" / "sandbox-uids.json"
    ledger.parent.mkdir()
    ledger.write_text("")
    # Nothing of the range owns anything below the root (its own directories
    # are this process's, outside the range).
    assert ledger_uid(ledger, "alkera-chat-a2", low=ME + 1, high=ME + 5) == ME + 1
    assert json.loads(ledger.read_text())["uids"] == {"alkera-chat-a2": ME + 1}


def test_a_fresh_uid_is_never_one_that_owns_a_tree(floor: Path) -> None:
    """The ledger was lost (or never knew): the range's first uid still owns
    A1's tree, so a new chat gets the next one."""
    ledger = floor / "home" / "sandbox-uids.json"
    _chat_tree(floor, "a1")
    assert ledger_uid(ledger, "alkera-chat-a2", low=ME, high=ME + 5) == ME + 1


_KILL_CHILD = """
import json, os, signal, sys
from pathlib import Path
from alkera_cli.harness import sandbox_uid_ledger as m

def die(*_a, **_k):
    os.kill(os.getpid(), signal.SIGKILL)

target = sys.argv[2]
if target == "dump":
    m.json.dump = die
else:
    m.os.replace = die
m.ledger_uid(Path(sys.argv[1]), "alkera-chat-new", low=20000, high=20009)
"""


@pytest.mark.parametrize("point", ["dump", "replace"], ids=["mid-write", "before-rename"])
def test_a_writer_killed_mid_write_leaves_the_old_ledger_whole(floor: Path, point: str) -> None:
    ledger = floor / "home" / "sandbox-uids.json"
    first = ledger_uid(ledger, "alkera-chat-old", low=20000, high=20009)
    before = ledger.read_text()
    child = subprocess.run(
        [sys.executable, "-c", _KILL_CHILD, str(ledger), point],
        capture_output=True,
        check=False,
        env={**os.environ, ENV_TREES_FLOOR: str(floor)},
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    assert ledger.read_text() == before
    assert ledger_uid(ledger, "alkera-chat-old", low=20000, high=20009) == first


def test_a_uid_whose_chat_left_no_file_is_taken_back(floor: Path) -> None:
    """Allocate, release, re-allocate: the range holds one uid; the chat that
    had it leaves, its tree is removed, and once the grace has passed the
    next chat gets it. While the tree stands, or within the grace, it does
    not."""
    # Beside the root here: every directory this test makes is the range's uid.
    ledger = floor.parent / "sandbox-uids.json"
    with freeze_time("2026-10-01 12:00:00") as frozen:
        assert ledger_uid(ledger, "a", low=ME, high=ME, running=set) == ME
        tree = _chat_tree(floor, "a")
        frozen.move_to("2026-10-03 12:00:00")
        with pytest.raises(SandboxRefusedError, match="every chat uid"):
            ledger_uid(ledger, "b", low=ME, high=ME, running=set)
        shutil.rmtree(floor / "work")
        assert tree.parent.exists() is False
        with pytest.raises(SandboxRefusedError, match="every chat uid"):
            # Its files are gone, but a process still runs as it.
            ledger_uid(ledger, "b", low=ME, high=ME, running=lambda: {ME})
        assert ledger_uid(ledger, "b", low=ME, high=ME, running=set) == ME
        assert json.loads(ledger.read_text())["uids"] == {"b": ME}


def test_a_uid_just_handed_out_is_not_taken_back_before_its_trees_are_its(
    floor: Path,
) -> None:
    ledger = floor.parent / "sandbox-uids.json"
    with freeze_time("2026-10-01 12:00:00") as frozen:
        ledger_uid(ledger, "a", low=ME, high=ME, running=set)
        frozen.move_to("2026-10-01 13:00:00")
        with pytest.raises(SandboxRefusedError, match="every chat uid"):
            ledger_uid(ledger, "b", low=ME, high=ME, running=set)


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_the_running_uids_include_this_process() -> None:
    from alkera_cli.harness.sandbox_uid_ledger import running_uids

    assert os.getuid() in running_uids()
