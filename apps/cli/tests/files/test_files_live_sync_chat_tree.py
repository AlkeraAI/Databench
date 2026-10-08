"""The live sync touches a chat's tree only through the chat tree seam: what
the drive sends lands owned by the sandbox and editable by the agent, and no
inbound write, delete or read follows a link or opens a fifo the agent left."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.chat_fs import ChatTree, ChatTreeError, TreeIdentity
from alkera_cli.files.live_sync import InboundEntry, LiveEntry, LiveSync
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeInboundApi
from files._live_sync_fakes import make_sync as _sync

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes, links and fifos")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    there = tmp_path / "outside"
    there.mkdir()
    (there / "secret.txt").write_bytes(b"secret")
    os.chmod(there / "secret.txt", 0o600)
    return there


def _owed(tree: Path, relative: str, payload: bytes) -> tuple[LiveSync, FakeInboundApi]:
    """A sync the drive owes ``relative`` to, with ``payload`` as its bytes."""
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-1", state="inbound", seq=1)]
    api.paths = {"n-1": f"Home/work/{relative}"}
    api.contents = {"n-1": payload}
    return _sync(tree, api, FakeClock(), root_path="Home/work"), api


def _agreed(tree: Path, relative: str, data: bytes) -> tuple[LiveSync, FakeInboundApi, str]:
    """``relative`` written on the box and agreed with the drive."""
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, FakeClock(), root_path="Home/work")
    where = tree / relative
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(data)
    sync.classify(Change.added, str(where))
    sync.flush()
    node = api.nodes[relative]
    api.paths = {node: f"Home/work/{relative}"}
    api.batches.clear()
    return sync, api, node


@posix_only
def test_an_inbound_file_lands_owned_by_the_sandbox_and_editable(tree: Path) -> None:
    gid = next((g for g in os.getgroups() if g != os.getgid()), None)
    if gid is None:
        pytest.skip("this user is in no second group")
    sync, _api = _owed(tree, "notes/plan.md", b"from the web")
    sync.tree = ChatTree(tree, TreeIdentity(uid=os.getuid(), gid=gid))

    assert sync.pull_inbound() == [LiveEntry(node_id="n-1", state="applied")]

    landed = tree / "notes" / "plan.md"
    assert landed.read_bytes() == b"from the web"
    info = os.stat(landed)
    assert (info.st_uid, info.st_gid) == (os.getuid(), gid)
    assert stat.S_IMODE(info.st_mode) == 0o660
    assert os.stat(tree / "notes").st_gid == gid


@posix_only
def test_a_web_write_over_a_read_only_file_leaves_it_editable(tree: Path) -> None:
    sync, api, node = _agreed(tree, "report.md", b"agreed")
    os.chmod(tree / "report.md", 0o444)
    api.web_write(node, b"the web's edit", seq=2)

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]

    assert stat.S_IMODE(os.stat(tree / "report.md").st_mode) == 0o660
    with (tree / "report.md").open("r+b") as handle:
        handle.write(b"T")
    assert (tree / "report.md").read_bytes() == b"The web's edit"


@posix_only
def test_a_link_at_the_name_is_not_written_through(tree: Path, outside: Path) -> None:
    sync, _api = _owed(tree, "plan.md", b"from the web")
    (tree / "plan.md").symlink_to(outside / "secret.txt")

    assert sync.pull_inbound() == []

    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert stat.S_IMODE(os.stat(outside / "secret.txt").st_mode) == 0o600
    assert (tree / "plan.md").is_symlink()


@posix_only
def test_a_link_above_the_name_is_never_crossed(tree: Path, outside: Path) -> None:
    sync, _api = _owed(tree, "notes/plan.md", b"from the web")
    (tree / "notes").symlink_to(outside, target_is_directory=True)

    assert sync.pull_inbound() == []

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


@posix_only
def test_a_fifo_at_the_name_is_refused_without_blocking(tree: Path) -> None:
    sync, _api = _owed(tree, "plan.md", b"from the web")
    os.mkfifo(tree / "plan.md")

    assert sync.pull_inbound() == []

    assert stat.S_ISFIFO(os.lstat(tree / "plan.md").st_mode)


@posix_only
def test_a_fifo_swapped_in_for_an_agreed_file_is_never_read(tree: Path) -> None:
    """The divergence check hashes the box's file before an inbound write; a
    fifo under the name must not park the daemon on an open for a writer."""
    sync, api, node = _agreed(tree, "report.md", b"agreed")
    (tree / "report.md").unlink()
    os.mkfifo(tree / "report.md")
    api.web_write(node, b"the web's edit", seq=2)

    assert sync.pull_inbound() == []
    assert stat.S_ISFIFO(os.lstat(tree / "report.md").st_mode)


@posix_only
def test_an_inbound_delete_never_removes_what_a_link_points_at(tree: Path, outside: Path) -> None:
    sync, api, node = _agreed(tree, "report.md", b"agreed")
    (tree / "report.md").unlink()
    (tree / "report.md").symlink_to(outside / "secret.txt")
    api.queued = [InboundEntry(node_id=node, state="inbound_delete", seq=2)]

    sync.pull_inbound()

    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert stat.S_IMODE(os.stat(outside / "secret.txt").st_mode) == 0o600


def test_a_download_in_flight_is_counted_from_the_spool(tree: Path) -> None:
    """The bytes stream outside the tree, and how many have arrived is still
    readable: that is what keeps a turn waiting on a slow hand-over."""
    payload = b"x" * 10_000
    sync, api = _owed(tree, "big.bin", payload)
    spool = sync.spool
    assert spool is not None
    seen: list[int] = []
    api.on_download = lambda _node, _into: seen.append(spool.landing_bytes("big.bin"))

    sync.pull_inbound()

    assert seen == [len(payload) // 2]
    assert spool.landing_bytes("big.bin") == 0
    assert spool.landing_bytes("other.bin") == 0
    assert (tree / "big.bin").read_bytes() == payload


@posix_only
def test_the_sync_hashes_through_the_tree(tree: Path, outside: Path) -> None:
    """Every digest the sync takes of a box file is read through the tree, so
    a link swapped in under a name between a check and the read is refused
    rather than read (and its target's bytes sent up as the file's)."""
    sync, _api = _owed(tree, "plan.md", b"")
    (tree / "plan.md").write_bytes(b"mine")
    (tree / "link.md").symlink_to(outside / "secret.txt")

    assert sync.hasher(tree / "plan.md")[1] == 4
    with pytest.raises(ChatTreeError):
        sync.hasher(tree / "link.md")


def test_a_session_write_back_never_sets_the_agents_newer_bytes_aside(tree: Path) -> None:
    """A live session wrote the file back while the agent's edit on this box
    was not sent yet. The box keeps its bytes under the name and sends them up
    first (the session merges them, and its next write back carries both),
    rather than setting them aside as a conflicted copy nobody reconciles. A
    write the web made directly still sets them aside as before."""

    class _SessionHeads(FakeInboundApi):
        session = True

        def item(self, node_id: str) -> dict[str, Any]:
            described = super().item(node_id)
            if self.session:
                described["file"] = {"metadata": {"head_source": "document_snapshot"}}
            return described

    api = _SessionHeads(root=tree)
    sync = _sync(tree, api, FakeClock(), root_path="Home/work")
    (tree / "plan.txt").write_bytes(b"agreed\n")
    sync.classify(Change.added, str(tree / "plan.txt"))
    sync.flush()
    node = api.nodes["plan.txt"]
    api.paths = {node: "Home/work/plan.txt"}
    (tree / "plan.txt").write_bytes(b"agreed\nagent\n")
    api.web_write(node, b"person\nagreed\n", seq=2)

    assert sync.pull_inbound() == []

    assert (tree / "plan.txt").read_bytes() == b"agreed\nagent\n"
    assert sorted(p.name for p in tree.iterdir()) == ["plan.txt"]
    assert "plan.txt" in sync.promotions
    assert api.downloads == []

    api.session = False
    assert sync.pull_inbound()[0].displaced is not None
    assert (tree / "plan.txt").read_bytes() == b"person\nagreed\n"
