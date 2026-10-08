"""Two-way writes on the holder: the box's bytes are never overwritten.

When the drive sends newer bytes (or a delete) for a file the box has changed
since the two last agreed, the box's bytes move aside under a staging name, the
drive's change is applied, and the staged bytes go to the drive as a conflicted
copy under the name the DRIVE chooses. These drive a real ``LiveSync`` on a real
directory against the fake drive in ``_live_sync_fakes``, whose conflict namer
is the drive's own (``alkera_core.files.conflicts``), so every assertion is
about what ended up on the disk and on the drive.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.live_sync import (
    STAGING_SCAN_EVERY,
    AgreedBase,
    ConflictAnswer,
    ConflictTargetGoneError,
    InboundEntry,
    LiveEntry,
    LiveSync,
    RestLiveApi,
    conflict_staging_name,
    live_watch_filter,
)
from alkera_cli.files.mount import LeaseSupersededError
from alkera_cli.files.tree_watch import Change
from alkera_cli.files.walk import ExportRules
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.delivery import (
    register_conflict_notices,
    unregister_conflict_notices,
    with_conflict_notices,
)
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.files.hashing import hash_bytes
from alkera_core.project.directory import ProjectDirectory
from files._live_sync_fakes import FakeClock, FakeInboundApi, FakeWatcher, RefusedError
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

#: What the drive's namer calls the box's copy of ``report.md`` in these tests.
COPY = "report (conflicted copy from alkera-demo-box, 2026-09-24 03.25 UTC).md"
COPY_2 = "report (conflicted copy from alkera-demo-box, 2026-09-24 03.25 UTC) (1).md"


class Crash(BaseException):
    """The process dying at this exact point: nothing below it runs, and no
    ``except Exception`` in the holder may swallow it."""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def _pushed(sync: LiveSync, tree: Path, relative: str, data: bytes) -> str:
    """Write ``relative`` on the box and let the drive agree on it."""
    _write(tree, relative, data)
    sync.classify(Change.added, str(tree / relative))
    sync.flush()
    api = sync.api
    assert isinstance(api, FakeInboundApi)
    return api.nodes[relative]


def _diverged_report(
    tree: Path, clock: FakeClock, *, web: bytes = b"the web's edit"
) -> tuple[LiveSync, FakeInboundApi, str]:
    """The box and the drive agreed on report.md, then both changed it."""
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "report.md", b"the agreed draft")
    (tree / "report.md").write_bytes(b"the agent's rewrite")
    api.queued = [InboundEntry(node_id=node, state="inbound", seq=2)]
    api.paths = {node: "Home/work/report.md"}
    api.contents = {node: web}
    api.batches.clear()
    return sync, api, node


def _staging(tree: Path) -> list[Path]:
    return sorted(path for path in tree.rglob("*") if path.name.endswith(".alkera-conflict"))


def _restart(tree: Path, api: FakeInboundApi, clock: FakeClock) -> LiveSync:
    """The next start of the plane: a fresh sync on the same folder, run once."""
    sync = _sync(tree, api, clock, root_path="Home/work", watcher=FakeWatcher([]))
    asyncio.run(sync.run())
    return sync


# -- a diverged inbound write --------------------------------------------------


def test_a_diverged_inbound_write_keeps_both_and_the_drive_names_the_copy(tree: Path) -> None:
    """The web's bytes take the name; the agent's go to the drive as a copy of
    that node, land on the box under the name the drive chose, and the entry is
    answered ``applied`` with where they went."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    during: list[tuple[bool, list[bytes]]] = []

    def look(_node_id: str, _into: Path, **_: Any) -> None:
        # Mid-download: the agent's file is still under its own name, and
        # nothing has been staged yet.
        during.append(
            ((tree / "report.md").read_bytes(), [path.read_bytes() for path in _staging(tree)])
        )

    api.on_download = look

    answered = sync.pull_inbound()

    assert answered == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert during == [(b"the agent's rewrite", [])]
    assert (tree / "report.md").read_bytes() == b"the web's edit"
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert _staging(tree) == []
    # On the drive: one copy, beside the node it displaced, holding the agent's bytes.
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, node)]
    assert api.copy_bytes() == {COPY: b"the agent's rewrite"}
    # The settle the drive was sent names the copy on the wire.
    assert [entry.to_wire() for batch in api.batches for entry in batch] == [
        {"nodeId": node, "state": "applied", "displaced": COPY}
    ]


def _names(tree: Path) -> set[str]:
    return {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}


def test_a_watcher_round_during_the_download_keeps_the_node_the_web_replaced(
    tree: Path,
) -> None:
    """The person replaced report.md on the web while the agent was changing
    it on the box. The loop's watcher runs beside the inbound pass, so a round
    may come while the drive's bytes are still downloading: whatever it sees
    goes to the drive. The node the person wrote a version onto must survive
    that round -- the web's bytes stay its head and the agent's become a copy
    beside it -- rather than being trashed for a name that is empty for a
    moment and coming back as a new file with none of its history."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    api.trees.clear()
    before = _names(tree)

    def watcher_round(_node_id: str, _into: Path, **_: Any) -> None:
        # What a watcher reports at this instant: names gone and names come.
        now = _names(tree)
        for name in sorted(before - now):
            sync.classify(Change.deleted, str(tree / name))
        for name in sorted(now - before):
            sync.classify(Change.added, str(tree / name))
        sync.flush()

    api.on_download = watcher_round

    answered = sync.pull_inbound()

    assert [entry for batch in api.trees for entry in batch if entry.op == "delete"] == []
    assert api.trashed == []
    assert api.nodes["report.md"] == node
    assert answered == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert (tree / "report.md").read_bytes() == b"the web's edit"
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, node)]
    assert api.copy_bytes() == {COPY: b"the agent's rewrite"}


def test_a_refused_placement_leaves_the_agent_s_file_alone_and_no_staging_behind(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive's bytes arrived but could not be renamed over the agent's
    file. The agent's file keeps its name and bytes, no second name for them
    is left to be offered as a copy, and the entry stays owed."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)

    def refuse_inbound(self: ChatTree, path: Any, source: Path, **_: Any) -> None:
        raise PermissionError("the name is held open")

    monkeypatch.setattr(ChatTree, "install", refuse_inbound)

    assert sync.pull_inbound() == []
    assert (tree / "report.md").read_bytes() == b"the agent's rewrite"
    assert _names(tree) == {"report.md"}
    assert api.copies == {}


def test_the_holder_puts_the_copy_under_whatever_name_the_drive_answers(tree: Path) -> None:
    """The name is the drive's, never the holder's: a second conflict on the
    same file inside one minute takes the numbered name the drive's namer
    gives it, because the drive says so."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    sync.pull_inbound()
    (tree / "report.md").write_bytes(b"the agent's second rewrite")
    api.contents = {node: b"the web's second edit"}
    api.queued = [InboundEntry(node_id=node, state="inbound", seq=3)]

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied", displaced=COPY_2)]
    assert (tree / COPY_2).read_bytes() == b"the agent's second rewrite"
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert (tree / "report.md").read_bytes() == b"the web's second edit"


def test_a_nested_file_s_copy_lands_beside_it(tree: Path) -> None:
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "out/report.md", b"agreed")
    (tree / "out" / "report.md").write_bytes(b"box")
    api.queued = [InboundEntry(node_id=node, state="inbound", seq=2)]
    api.paths = {node: "Home/work/out/report.md"}
    api.contents = {node: b"web"}

    assert sync.pull_inbound() == [
        LiveEntry(node_id=node, state="applied", displaced=f"out/{COPY}")
    ]
    assert (tree / "out" / COPY).read_bytes() == b"box"
    assert api.copy_bytes() == {f"out/{COPY}": b"box"}


def test_an_undiverged_inbound_write_makes_no_copy(tree: Path) -> None:
    """The asymmetric case: a file still holding the agreed bytes is simply
    replaced — no staging, no copy, no ``displaced``."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "report.md", b"the agreed draft")
    api.queued = [InboundEntry(node_id=node, state="inbound", seq=2)]
    api.paths = {node: "Home/work/report.md"}
    api.contents = {node: b"the web's edit"}

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    assert api.copies == {}
    assert sorted(path.name for path in tree.iterdir()) == ["report.md"]


def test_a_refused_download_puts_the_agent_s_file_back_and_stays_owed(tree: Path) -> None:
    """The drive's bytes never arrived, so nothing replaced the agent's: its
    file is back under its own name, no copy was made, and the entry is not
    settled — the next pass meets the same divergence."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)
    api.refuse, api.refuse_on = "files.not_ready", "download"

    assert sync.pull_inbound() == []
    assert (tree / "report.md").read_bytes() == b"the agent's rewrite"
    assert _staging(tree) == []
    assert api.copies == {}


# -- the holder's own upload over a head the web moved -------------------------


def _held_shared(tree: Path, clock: FakeClock) -> tuple[LiveSync, FakeInboundApi, str]:
    """The agent's loop has been appending to shared.txt and the holder
    uploading each version; the drive holds the last one at etag 6."""
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    body = b""
    for line in range(6):
        body += f"tick {line}\n".encode()
        node = _pushed(sync, tree, "demo/shared.txt", body)
    api.paths = {node: "Home/work/demo/shared.txt"}
    assert api.etags[node] == 6
    return sync, api, node


def _append(sync: LiveSync, tree: Path, line: bytes) -> None:
    """The agent's ``echo … >> demo/shared.txt``, and the flush that sends it."""
    with (tree / "demo" / "shared.txt").open("ab") as handle:
        handle.write(line)
    sync.classify(Change.modified, str(tree / "demo" / "shared.txt"))
    sync.flush()


def test_an_append_racing_a_web_replace_is_fenced_on_the_bytes_the_disk_holds(
    tree: Path,
) -> None:
    """The walkthrough: the web replaces shared.txt (etag 7) and the holder
    reads the drive's word on it -- the node described at 7 -- but the bytes
    never reach the disk; the agent appends and the holder uploads. The upload
    must name etag 6, the version the disk's bytes grew from, so the drive
    sees its head moved and keeps the web's bytes beside the holder's. Naming
    7 (the etag merely seen) or nothing is the drive taking the append as a
    plain new version over the browser's line."""
    clock = FakeClock()
    sync, api, node = _held_shared(tree, clock)
    agreed = (tree / "demo" / "shared.txt").read_bytes()
    api.web_write(node, b"from the browser\n" + agreed, seq=7)
    assert api.etags[node] == 7
    api.refuse, api.refuse_on = "files.not_ready", "download"
    assert sync.pull_inbound() == [], "the web's bytes were seen, not applied"
    api.refuse = None
    api.bases.clear()

    _append(sync, tree, b"tick 6\n")

    assert api.bases == [
        ("demo/shared.txt", AgreedBase(etag="6", content_hash=hash_file_digest(agreed)))
    ]


def test_an_append_after_the_web_s_bytes_landed_is_fenced_on_their_etag(tree: Path) -> None:
    """The asymmetric case: once the web's version is on the disk the append
    grows from it, so the upload names the web's etag and its bytes carry the
    browser's line -- a plain new version, nothing displaced."""
    clock = FakeClock()
    sync, api, node = _held_shared(tree, clock)
    web = b"from the browser\n" + (tree / "demo" / "shared.txt").read_bytes()
    api.web_write(node, web, seq=7)
    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    api.bases.clear()

    _append(sync, tree, b"tick 6\n")

    assert api.bases == [
        ("demo/shared.txt", AgreedBase(etag="7", content_hash=hash_file_digest(web)))
    ]
    assert api.stored["demo/shared.txt"] == web + b"tick 6\n"


def test_the_base_follows_a_rename_and_is_dropped_with_a_delete(tree: Path) -> None:
    """The agreed etag is bookkeeping about a path: a rename carries it to the
    new name, and a file deleted and written afresh has none to name."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    _pushed(sync, tree, "a.txt", b"one")
    os.replace(tree / "a.txt", tree / "b.txt")
    sync.classify(Change.deleted, str(tree / "a.txt"))
    sync.classify(Change.added, str(tree / "b.txt"))
    sync.flush()
    api.bases.clear()
    _append_to(sync, tree, "b.txt", b" two")
    assert api.bases == [("b.txt", AgreedBase(etag="1", content_hash=hash_file_digest(b"one")))]

    (tree / "b.txt").unlink()
    sync.classify(Change.deleted, str(tree / "b.txt"))
    sync.flush()
    api.bases.clear()
    _pushed(sync, tree, "b.txt", b"fresh")
    assert api.bases == [("b.txt", None)]


def _append_to(sync: LiveSync, tree: Path, relative: str, data: bytes) -> None:
    with (tree / relative).open("ab") as handle:
        handle.write(data)
    sync.classify(Change.modified, str(tree / relative))
    sync.flush()


def hash_file_digest(data: bytes) -> str:
    """The digest the holder agrees bytes by: the drive's whole-file hash."""
    return hash_bytes(data).content_hash.hex()


# -- a crash after each step ---------------------------------------------------


def _crash_on_submit(api: FakeInboundApi) -> None:
    def crash(*_args: Any, **_kwargs: Any) -> ConflictAnswer:
        raise Crash

    api.submit_conflict = crash  # type: ignore[method-assign]


def _crash_after_the_drive_made_the_copy(api: FakeInboundApi) -> None:
    def crash(_token: str) -> None:
        raise Crash

    api.after_copy = crash


def test_a_crash_mid_download_leaves_the_agent_s_file_where_it_was(tree: Path) -> None:
    """The process died while the drive's bytes were still arriving. Nothing
    had touched the agent's file yet: it is under its own name with its own
    bytes, no staging file and no half-downloaded file are left, and the
    entry is still owed to the next pass."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)

    def die_half_way(_node_id: str, into: Path, **_: Any) -> None:
        assert into.exists(), "the fake writes half the payload before this"
        raise Crash

    api.on_download = die_half_way

    with pytest.raises(Crash):
        sync.pull_inbound()

    assert _names(tree) == {"report.md"}
    assert (tree / "report.md").read_bytes() == b"the agent's rewrite"
    assert api.copies == {}


def test_a_crash_before_the_drive_s_bytes_replace_the_file_makes_no_copy_on_restart(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process died after the agent's bytes were kept aside and before the
    drive's replaced them. Nothing was displaced: the next start files no
    copy, leaves no staging file, and the agent's file keeps its name."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)
    real_install = ChatTree.install

    def die_at_placement(self: ChatTree, path: Any, source: Path, **_: Any) -> None:
        raise Crash

    monkeypatch.setattr(ChatTree, "install", die_at_placement)
    with pytest.raises(Crash):
        sync.pull_inbound()
    monkeypatch.setattr(ChatTree, "install", real_install)

    fresh = FakeInboundApi(root=tree)
    fresh.nodes = api.nodes
    _restart(tree, fresh, clock)

    assert fresh.copies == {}
    assert _names(tree) == {"report.md"}
    assert (tree / "report.md").read_bytes() == b"the agent's rewrite"


@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(_crash_on_submit, id="after-applying-the-drive-s-bytes"),
        pytest.param(_crash_after_the_drive_made_the_copy, id="after-the-drive-answered"),
    ],
)
def test_a_crash_after_each_step_is_replayed_from_the_staging_file_on_the_next_start(
    tree: Path, crash: Callable[[FakeInboundApi], None]
) -> None:
    """Whatever point the process died at, the agent's bytes are in a staging
    file, and the next start sends them to the drive as a copy of that node —
    exactly one copy, even when the drive had already made one before the
    answer was lost."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    crash(api)

    with pytest.raises(Crash):
        sync.pull_inbound()

    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"
    assert (tree / "report.md").read_bytes() == b"the web's edit"

    # The next process: a plain drive again.
    fresh = FakeInboundApi(root=tree)
    fresh.nodes, fresh.copies, fresh._answers = api.nodes, api.copies, api._answers
    _restart(tree, fresh, clock)

    assert _staging(tree) == []
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert [(copy, of) for copy, of, _data in fresh.copies.values()] == [(COPY, node)]


def test_a_copy_that_could_not_be_sent_is_sent_by_the_next_reconciliation_pass(
    tree: Path,
) -> None:
    """A refusal is not a crash: the drive's bytes are applied and settled, the
    agent's wait staged, and the next pass (no restart) sends them."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    api.refuse, api.refuse_on = "files.unavailable", "conflict"

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    assert (tree / "report.md").read_bytes() == b"the web's edit"
    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"

    api.refuse = None
    api.queued = []
    assert sync.pull_inbound() == []
    assert _staging(tree) == []
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, node)]


def test_a_staging_file_whose_node_nobody_knows_is_filed_as_a_new_file(tree: Path) -> None:
    """The node the bytes displaced cannot be found — renamed or trashed since
    — so they go with no ``conflict_of`` and the drive files them beside the
    name they were staged from."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    _write(tree, conflict_staging_name("lost.md", "0badc0de"), b"orphaned work")

    _restart(tree, api, clock)

    lost_copy = "lost (conflicted copy from alkera-demo-box, 2026-09-24 03.25 UTC).md"
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(lost_copy, None)]
    assert (tree / lost_copy).read_bytes() == b"orphaned work"


def test_a_copy_whose_node_was_trashed_mid_upload_is_filed_as_a_new_file(tree: Path) -> None:
    """The web trashed the node while the copy's session was open, so its
    commit is refused and every replay of that session would be too. The
    bytes go again as a plain new file beside the name, once."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    api.gone_at_complete = {node}

    answered = sync.pull_inbound()

    assert answered == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert _staging(tree) == []
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, None)]
    assert len(api.gone_refusals) == 1
    # Nothing is left to send: another pass and another start make no copy.
    api.queued = []
    assert sync.pull_inbound() == []
    _restart(tree, api, clock)
    assert len(api.copies) == 1
    assert len(api.gone_refusals) == 1


def test_a_crash_after_the_unbound_copy_is_made_never_makes_a_second(tree: Path) -> None:
    """The drive made the new-file copy and the answer was lost with the
    process. The next start replays the refused session, then the new-file
    submission under the same key as before: the drive answers the copy it
    already made."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    api.gone_at_complete = {node}

    def die(_token: str) -> None:
        raise Crash

    api.after_copy = die
    with pytest.raises(Crash):
        sync.pull_inbound()
    assert len(api.copies) == 1
    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"

    fresh = FakeInboundApi(root=tree)
    fresh.nodes, fresh.copies, fresh._answers = api.nodes, api.copies, api._answers
    fresh.gone_at_complete = {node}
    _restart(tree, fresh, clock)

    assert _staging(tree) == []
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert [(copy, of) for copy, of, _data in fresh.copies.values()] == [(COPY, None)]
    assert fresh.gone_refusals == api.gone_refusals, "the replay met the same refused key"


def test_a_staging_file_left_by_another_run_is_found_by_the_periodic_walk(tree: Path) -> None:
    """A running sync walks its root again once the scan interval has passed,
    so a staging file it did not make itself is not left behind for good."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    sync.pull_inbound()
    _write(tree, conflict_staging_name("late.md", "feedf00d"), b"late work")

    sync.pull_inbound()
    assert api.copies == {}, "inside the interval the walk is not repeated"

    clock.advance(STAGING_SCAN_EVERY)
    sync.fence.beat()
    sync.pull_inbound()
    assert api.copy_bytes() == {
        "late (conflicted copy from alkera-demo-box, 2026-09-24 03.25 UTC).md": b"late work"
    }


def test_a_fenced_submission_ends_the_pass_and_keeps_the_staged_bytes(tree: Path) -> None:
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)
    api.refuse, api.refuse_on = "files.lease_fenced", "conflict"

    with pytest.raises(LeaseSupersededError):
        sync.pull_inbound()
    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"


# -- where the drive's answer cannot go ----------------------------------------


@pytest.mark.parametrize(
    "answered",
    [
        pytest.param("../escape.md", id="a-path-out-of-the-folder"),
        pytest.param("sub/report.md", id="a-path-with-a-separator"),
        pytest.param(".report.md.cafe.alkera-conflict", id="another-staging-name"),
        pytest.param("", id="no-name"),
    ],
)
def test_a_name_the_holder_will_not_write_leaves_the_bytes_staged_and_asks_once(
    tree: Path, answered: str
) -> None:
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    asked: list[str] = []

    def answer(staged: Path, relative: str, *, conflict_of: str | None, token: str) -> Any:
        asked.append(token)
        return ConflictAnswer(node_id="node-odd", name=answered)

    api.submit_conflict = answer  # type: ignore[method-assign]

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    api.queued = []
    sync.pull_inbound()

    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"
    assert len(asked) == 1, "a copy the drive already made is not asked for again"
    assert not (tree.parent / "escape.md").exists()


def test_the_drive_s_name_already_taken_on_the_box_is_not_overwritten(tree: Path) -> None:
    """A file the agent wrote under the very name the drive chose, not yet
    pushed, is the agent's work too: it is kept, and the copy waits staged."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)
    (tree / COPY).write_bytes(b"a file the agent named just so")

    sync.pull_inbound()

    assert (tree / COPY).read_bytes() == b"a file the agent named just so"
    [staged] = _staging(tree)
    assert staged.read_bytes() == b"the agent's rewrite"
    # The copy itself is safe on the drive; only its place on the box waits.
    assert api.copy_bytes() == {COPY: b"the agent's rewrite"}


def test_a_displacement_past_the_ceiling_updates_the_newest_copy_on_the_box(tree: Path) -> None:
    """Past ``files_conflict_copies_max`` the drive files a displacement as a
    new version of the newest copy and answers that copy's own name. The box
    already holds that copy, unchanged since it placed it, so the newer bytes
    take its place there too; they never wait staged under a hidden name while
    the box and the drive disagree about what the copy holds."""
    clock = FakeClock()
    sync, api, node = _diverged_report(tree, clock)
    api.copies_max = 1
    sync.pull_inbound()
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    (tree / "report.md").write_bytes(b"the agent's second rewrite")
    api.contents = {node: b"the web's second edit"}
    api.queued = [InboundEntry(node_id=node, state="inbound", seq=3)]

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert _staging(tree) == []
    assert (tree / COPY).read_bytes() == b"the agent's second rewrite"
    assert api.copy_bytes() == {COPY: b"the agent's second rewrite"}
    assert (tree / "report.md").read_bytes() == b"the web's second edit"


# -- a diverged inbound delete -------------------------------------------------


def test_a_diverged_inbound_delete_keeps_the_agent_s_bytes_as_a_copy(tree: Path) -> None:
    """The web trashed a file the agent had since rewritten. The trash keeps
    the record of the delete; the agent's bytes survive it as a copy on the
    drive and on the box, and the node itself is not put back."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "report.md", b"the agreed draft")
    (tree / "report.md").write_bytes(b"the agent's rewrite")
    # The web trashed the node: the drive no longer lists it, and refuses a
    # submission that names it as the node displaced.
    api.nodes.pop("report.md")
    api.queued = [InboundEntry(node_id=node, state="inbound_delete", seq=2)]
    api.paths = {node: "Home/work/report.md"}
    api.batches.clear()

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert not (tree / "report.md").exists()
    assert (tree / COPY).read_bytes() == b"the agent's rewrite"
    assert _staging(tree) == []
    # Filed as a new file under the drive's copy name: the node it displaced is
    # in the trash, which is the record of the delete.
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, None)]
    assert "superseded" not in api.states().values()


def test_an_undiverged_inbound_delete_removes_the_file_and_makes_no_copy(tree: Path) -> None:
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "report.md", b"the agreed draft")
    api.queued = [InboundEntry(node_id=node, state="inbound_delete", seq=2)]
    api.paths = {node: "Home/work/report.md"}

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    assert list(tree.iterdir()) == []
    assert api.copies == {}


def test_a_rename_over_a_trashed_name_then_its_delete_keeps_the_moved_file(tree: Path) -> None:
    """The web trashed b.md and renamed a.md onto the freed name. The delete was
    left owed by a pass that could not take it, so the box meets the rename
    first. The delete then names the trashed node, whose path is the name a.md
    now holds on the box — and the file there is a.md's, a node the box knows,
    which the delete must not touch."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    moved = _pushed(sync, tree, "a.md", b"a's bytes")
    replaced = _pushed(sync, tree, "b.md", b"b's bytes")
    api.queued = [
        InboundEntry(node_id=moved, state="inbound_rename", seq=2),
        InboundEntry(node_id=replaced, state="inbound_delete", seq=3),
    ]
    api.paths = {moved: "Home/work/b.md", replaced: "Home/work/b.md"}

    sync.pull_inbound()

    assert not (tree / "a.md").exists()
    assert (tree / "b.md").read_bytes() == b"a's bytes"


# -- a rename onto the agent's own file ----------------------------------------


def test_a_rename_onto_a_newer_file_on_the_box_stages_it_first(tree: Path) -> None:
    """The web renamed draft.md to report.md while the agent had written a
    report.md of its own. The move does not overwrite it: the agent's file goes
    to the drive as a copy of the renamed node, and the move keeps what the
    holder knew about the moved bytes."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "draft.md", b"the draft")
    _write(tree, "report.md", b"the agent's own report")
    api.queued = [InboundEntry(node_id=node, state="inbound_rename", seq=2)]
    api.paths = {node: "Home/work/report.md"}

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied", displaced=COPY)]
    assert not (tree / "draft.md").exists()
    assert (tree / "report.md").read_bytes() == b"the draft"
    assert (tree / COPY).read_bytes() == b"the agent's own report"
    assert [(copy, of) for copy, of, _data in api.copies.values()] == [(COPY, node)]

    # The moved bytes are still the agreed ones: the watcher's echo of the
    # move sends nothing back up.
    api.uploads.clear()
    api.trees.clear()
    for relative in ("report.md", COPY):
        sync.classify(Change.added, str(tree / relative))
    sync.flush()
    assert api.uploads == []


# -- staging names stay on the box ---------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(conflict_staging_name("report.md", "0a1b2c3d"), id="conflict-at-root"),
        pytest.param(f"out/{conflict_staging_name('r.md', '0a1b2c3d')}", id="conflict-nested"),
        pytest.param(".report.md.0a1b2c3d.alkera-inbound", id="inbound-at-root"),
        pytest.param("out/.r.md.0a1b2c3d.alkera-inbound", id="inbound-nested"),
        # A write a crash left half done in the chat's tree (ChatTree's temp).
        pytest.param(".plan.md.0a1b2c3d.alkera-tmp", id="tree-temp-at-root"),
        pytest.param("out/.plan.md.0a1b2c3d.alkera-tmp", id="tree-temp-nested"),
    ],
)
def test_a_staging_name_never_reaches_the_drive(tree: Path, relative: str) -> None:
    """Not the watcher, not the metadata queue, not the content queue, not the
    checkpoint export: bytes waiting for the drive's answer are nobody's file."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    path = _write(tree, relative, b"staged")

    assert live_watch_filter(Change.added, str(path)) is False
    assert sync.classify(Change.added, str(path)) is None
    sync.classify(Change.modified, str(path.parent))
    # A nested staging file's folder is a real folder and is listed; the
    # staging file inside it is not.
    folder = {"out"} if "/" in relative else set()
    assert set(sync.metadata) == folder
    assert sync.pending == {}
    sync.flush()
    assert {entry.path for batch in api.trees for entry in batch} == folder
    assert api.uploads == []
    rules = ExportRules.load(tree, respect_gitignore=True, skip_local_state=True)
    assert rules.skip_reason(relative.encode(), is_dir=False) is not None


def test_an_ordinary_file_with_a_similar_name_still_travels(tree: Path) -> None:
    """The asymmetric case: only the staging shape is withheld."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    path = _write(tree, "notes.alkera-conflict.md", b"mine")

    assert sync.classify(Change.added, str(path)) is not None


def test_the_watcher_s_echo_of_a_conflict_sends_nothing_back(tree: Path) -> None:
    """The conflict moved report.md away and put the web's bytes under it, made
    and removed a staging file, and placed the copy. A watcher reports all of
    it — the removal of report.md last, in the order a batch is handed over —
    and none of it is news to the drive: no delete, no upload."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)
    sync.pull_inbound()
    api.trees.clear()
    api.uploads.clear()
    staged = tree / conflict_staging_name("report.md", "0a1b2c3d")
    batch = {
        (Change.added, str(staged)),
        (Change.deleted, str(staged)),
        (Change.added, str(tree / "report.md")),
        (Change.deleted, str(tree / "report.md")),
        (Change.added, str(tree / COPY)),
    }
    for change, path in sorted(batch, key=lambda item: (item[1], item[0].value)):
        sync.classify(change, path)
    sync.flush()

    assert [entry for tree_batch in api.trees for entry in tree_batch if entry.op == "delete"] == []
    assert api.uploads == []


# -- the agent is told ---------------------------------------------------------


def test_the_notice_is_said_once_to_a_call_that_names_the_file(tree: Path) -> None:
    clock = FakeClock()
    sync, _api, _node = _diverged_report(tree, clock)
    sync.pull_inbound()
    sentence = f"A newer copy of report.md arrived from the web; your version is at {COPY}."

    assert sync.take_conflict_notices(["cat old-report.md", "report.mdx"]) == []
    assert sync.take_conflict_notices([f"cat {tree / 'report.md'}"]) == [sentence]
    assert sync.take_conflict_notices(["report.md"]) == []


def test_a_delete_s_notice_says_it_was_deleted(tree: Path) -> None:
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "report.md", b"agreed")
    (tree / "report.md").write_bytes(b"box")
    api.queued = [InboundEntry(node_id=node, state="inbound_delete", seq=2)]
    api.paths = {node: "Home/work/report.md"}
    sync.pull_inbound()

    assert sync.take_conflict_notices(["report.md"]) == [
        f"report.md was deleted on the web; your version is at {COPY}."
    ]


def test_the_holder_remembers_only_the_last_twenty_conflicts(tree: Path) -> None:
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    for index in range(22):
        name = f"f{index:02d}.md"
        node = _pushed(sync, tree, name, b"agreed")
        (tree / name).write_bytes(b"box")
        api.queued = [InboundEntry(node_id=node, state="inbound", seq=index)]
        api.paths = {node: f"Home/work/{name}"}
        api.contents = {node: b"web"}
        sync.pull_inbound()

    assert [notice.path for notice in sync.conflicts] == [
        f"f{index:02d}.md" for index in range(2, 22)
    ]


def test_the_notice_reaches_the_next_tool_result_through_the_delivery_seam(
    tree: Path, tmp_path: Path
) -> None:
    """A real dispatch of the bash tool in the folder's working directory: the
    call that reads report.md carries the line in its ``note``, and the call
    after it does not."""
    clock = FakeClock()
    sync, _api, _node = _diverged_report(tree, clock)
    sync.pull_inbound()
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    register_conflict_notices(tree, lambda: sync)

    async def call(command: str) -> dict[str, Any]:
        return await registry.dispatch(
            "bash",
            {"command": command, "description": "read"},
            alkera_dir=str(tmp_path / ".alkera"),
            sandbox_dir=str(tree),
        )

    try:
        unrelated = asyncio.run(call("echo hi"))
        first = asyncio.run(call(f"cat '{tree / 'report.md'}'"))
        second = asyncio.run(call(f"cat '{tree / 'report.md'}'"))
    finally:
        unregister_conflict_notices(tree)

    sentence = f"A newer copy of report.md arrived from the web; your version is at {COPY}."
    assert sentence not in json.dumps(unrelated)
    assert sentence in first["note"]
    assert "the web's edit" in json.dumps(first)
    assert sentence not in json.dumps(second)


def test_a_result_is_unchanged_where_nobody_holds_the_folder(tree: Path) -> None:
    result = {"output": "x", "note": "kept"}
    assert with_conflict_notices(result, {"path": "report.md"}, sandbox_dir=str(tree)) == result
    assert with_conflict_notices(result, {"path": "report.md"}, sandbox_dir=None) == result
    # A handle that is not a path at all (a caller's sentinel) is no folder.
    assert with_conflict_notices(result, {"path": "report.md"}, sandbox_dir=object()) == result


# -- the submission over the real wire -----------------------------------------


class _Files:
    """The Files slice a conflict submission reads through."""

    def __init__(self) -> None:
        self.awaited: list[str] = []

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        folders = {"Chats/c": "folder-root", "Chats/c/out": "folder-out"}
        if item_path not in folders:
            raise RuntimeError(f"GET {item_path} returned 404 — {{}}")
        return {"id": folders[item_path]}

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        assert item_id == "lease-9", item_id
        return self.item_by_path(drive_id, f"Chats/c/{item_path}".rstrip("/"))

    def await_operation(self, drive_id: str, operation_id: str, **_: Any) -> dict[str, Any]:
        self.awaited.append(operation_id)
        return {"id": operation_id, "state": "succeeded", "resultNodeId": "node-copy"}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return {"id": item_id, "name": COPY}


def test_the_submission_is_one_fenced_session_that_names_the_node_it_displaces(
    tree: Path,
) -> None:
    """The session is opened beside the file with ``conflictOf``, the bytes go
    in parts with their checksums, the commit asks the drive to name the node,
    and every call carries a key made of the staging token — so the same
    submission after a crash is the same submission."""
    seen: list[tuple[str, str, dict[str, str], bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.headers), request.content))
        if request.url.path.endswith("/uploads"):
            return httpx.Response(201, json={"uploadId": "up-1", "partSize": 4})
        if request.url.path.endswith("/complete"):
            return httpx.Response(202, json={"id": "op-1", "kind": "upload", "state": "queued"})
        return httpx.Response(200, json={"partNo": 1, "size": 4, "duplicate": False})

    files = _Files()
    api = RestLiveApi(
        files=files,  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c",
    )
    staged = _write(tree, f"out/{conflict_staging_name('report.md', 'c0ffee00')}", b"abcdefghij")

    answer = api.submit_conflict(staged, "out/report.md", conflict_of="node-7", token="c0ffee00")

    assert answer == ConflictAnswer(node_id="node-copy", name=COPY, conflict_id=None)
    methods = [(method, path) for method, path, _h, _c in seen]
    assert methods == [
        ("POST", "/api/v1/files/uploads"),
        ("PUT", "/api/v1/files/uploads/up-1/parts/1"),
        ("PUT", "/api/v1/files/uploads/up-1/parts/2"),
        ("PUT", "/api/v1/files/uploads/up-1/parts/3"),
        ("POST", "/api/v1/files/uploads/up-1/complete"),
    ]
    assert json.loads(seen[0][3]) == {
        "parentId": "folder-out",
        "name": "report.md",
        "declaredSize": 10,
        "conflictOf": "node-7",
    }
    assert [content for _m, _p, _h, content in seen[1:4]] == [b"abcd", b"efgh", b"ij"]
    keys = [headers["idempotency-key"] for _m, _p, headers, _c in seen]
    assert all("c0ffee00" in key for key in keys)
    assert len(set(keys)) == len(keys)
    assert files.awaited == ["op-1"]


def test_a_submission_the_drive_answers_directly_needs_no_wait(tree: Path) -> None:
    """The contract's answer — ``{node_id, name, conflict_id}`` on the commit —
    is read as it is, with no operation to poll and no second look-up."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(201, json={"uploadId": "up-1", "partSize": 64})
        if request.url.path.endswith("/complete"):
            return httpx.Response(
                202, json={"id": "op-1", "nodeId": "n-c", "name": COPY, "conflictId": "cf-1"}
            )
        return httpx.Response(200, json={})

    files = _Files()
    api = RestLiveApi(
        files=files,  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c",
    )
    staged = _write(tree, conflict_staging_name("report.md", "c0ffee00"), b"")

    answer = api.submit_conflict(staged, "report.md", conflict_of=None, token="c0ffee00")

    assert answer == ConflictAnswer(node_id="n-c", name=COPY, conflict_id="cf-1")
    assert files.awaited == []


def test_a_conflict_submission_refused_by_the_fence_is_the_fence_s_refusal(tree: Path) -> None:
    """Through the sync: the fence's code on the session is the lease moving on."""
    clock = FakeClock()
    sync, api, _node = _diverged_report(tree, clock)

    def fenced(*_args: Any, **_kwargs: Any) -> ConflictAnswer:
        raise RefusedError("files.lease_fenced")

    api.submit_conflict = fenced  # type: ignore[method-assign]
    with pytest.raises(LeaseSupersededError):
        sync.pull_inbound()


@pytest.mark.parametrize(
    ("conflict_of", "raised"),
    [
        pytest.param("node-7", ConflictTargetGoneError, id="a-copy-of-a-node-is-told-it-is-gone"),
        pytest.param(None, httpx.HTTPStatusError, id="a-new-file-keeps-the-plain-refusal"),
    ],
)
def test_a_404_at_the_commit_says_whether_the_displaced_node_is_gone(
    tree: Path, conflict_of: str | None, raised: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(201, json={"uploadId": "up-1", "partSize": 64})
        if request.url.path.endswith("/complete"):
            return httpx.Response(404, json={"code": "files.not_found"})
        return httpx.Response(200, json={})

    api = RestLiveApi(
        files=_Files(),  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c",
    )
    staged = _write(tree, conflict_staging_name("report.md", "c0ffee00"), b"abc")

    with pytest.raises(raised):
        api.submit_conflict(staged, "report.md", conflict_of=conflict_of, token="c0ffee00")
