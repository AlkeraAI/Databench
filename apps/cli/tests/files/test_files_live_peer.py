"""The box as a text peer of a file people have open live.

The server here is the real merge: the backend sandbox's own document cache
and merge, run in this process, with a person typing into the document as a
real Loro peer. The box's peer reads and writes a real directory through its
chat tree. What is pinned is what ends up in the document and on the disk:
the agent's change and the person's both, once each, line breaks and
characters past the basic plane exactly as they were, and nothing the agent
wrote overwritten by a merge that crossed its next save.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.live_peer import (
    LivePeer,
    PeerRefusedError,
    PeerState,
    PeerStates,
    RestPeerApi,
    Taken,
)
from alkera_cli.files.mount import LeaseSupersededError
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import FILE, DocCache
from loro import ExportMode, LoroDoc

pytestmark = [pytest.mark.spread]

KEY = "org:file:node"
NODE = "node-1"
NAME = "plan.txt"


@dataclass
class SandboxServer:
    """The live routes over the sandbox's own merge: what ``GET``/``POST
    .../live`` answer, and a person typing into the same document."""

    text: str
    cache: DocCache = field(default_factory=DocCache)
    log_seq: int = 0
    live: bool = True
    #: What every answer says of the drive holding the document.
    saved: bool = True
    person: LoroDoc = field(init=False)
    answers: dict[str, PeerState] = field(default_factory=dict)
    sent: list[tuple[str, str | None]] = field(default_factory=list)
    #: The drive etag each send named (``None`` for none), in order.
    named_etags: list[int | None] = field(default_factory=list)
    #: The document's version at each drive etag the session remembers.
    versions: dict[int, bytes] = field(default_factory=dict)
    #: Run once, in the middle of the next submit (after the merge, before
    #: the answer): a save of the agent's landing while the answer is on its way.
    meanwhile: Callable[[], None] | None = None
    #: Lose the next answer after applying it, as a dropped connection does.
    lose_next: bool = False
    #: Answer the next submit 500, before (``"before"``) or after
    #: (``"after"``) taking it: the server went away mid-request.
    fail_next: str | None = None
    _peers: int = 6000

    def __post_init__(self) -> None:
        seeded = core.seed(self.cache, key=KEY, epoch=1, rules=FILE, text=self.text, peer=4000)
        self.person = core.new_doc()
        self.person.peer_id = 5000
        self.person.import_(seeded.snapshot)

    # -- the routes --------------------------------------------------------

    def read(self, node_id: str) -> PeerState | None:
        if not self.live:
            return None
        return PeerState(token=self._token(self._vv()), text=self.content(), saved=self.saved)

    def submit(
        self,
        node_id: str,
        text: str,
        *,
        base_token: str | None,
        submit_id: str,
        base_etag: int | None = None,
    ) -> PeerState | None:
        if not self.live:
            return None
        if submit_id in self.answers:
            return self.answers[submit_id]
        if self.fail_next == "before":
            self.fail_next = None
            raise PeerRefusedError(node_id, 500)
        self.sent.append((text, base_token))
        self.named_etags.append(base_etag)
        base = bytes.fromhex(base_token) if base_token else None
        if base is None and base_etag is not None:
            # The drive version the box agreed, when the session remembers it:
            # merged from exactly there.
            base = self.versions.get(base_etag)
        self._peers += 1
        merged = core.merge(
            self.cache,
            key=KEY,
            epoch=1,
            log_seq=self.log_seq,
            rules=FILE,
            peer=self._peers,
            base_vv=base if base is not None else self._vv(),
            text=text,
            keep=base is None,
        )
        assert merged is not None
        if merged.outcome == "ok":
            self._commit(merged.delta)
        held = None
        if merged.outcome == "ok" and base is None:
            # A merge that kept everything: its state can hold more than was sent.
            data = core.content(
                self.cache, key=KEY, epoch=1, log_seq=self.log_seq, rules=FILE, at=merged.base_vv
            )
            assert data is not None
            held = None if data.decode("utf-8") == text else data.decode("utf-8")
        state = PeerState(
            token=self._token(self._vv()),
            text=self.content(),
            at_token=self._token(merged.base_vv) if merged.outcome == "ok" else base_token,
            at_text=held,
            saved=self.saved,
        )
        self.answers[submit_id] = state
        if self.meanwhile is not None:
            act, self.meanwhile = self.meanwhile, None
            act()
        if self.lose_next:
            self.lose_next = False
            raise httpx.ReadError("the answer was lost")
        if self.fail_next == "after":
            self.fail_next = None
            raise PeerRefusedError(node_id, 500)
        return state

    # -- the person --------------------------------------------------------

    def file(self, etag: int) -> None:
        """The drive holds the document as it is now, at ``etag``."""
        self.versions[etag] = self._vv()

    def type(self, at: int, piece: str) -> None:
        before = self.person.oplog_vv
        self.person.get_text("content").insert(at, piece)
        self.person.commit()
        update = bytes(self.person.export(ExportMode.Updates(before)))
        verdict = core.validate(
            self.cache,
            key=KEY,
            epoch=1,
            log_seq=self.log_seq,
            rules=FILE,
            peers=frozenset({5000}),
            update=update,
        )
        assert verdict is not None and verdict.outcome == "ok"
        self._commit(verdict.delta, person=False)

    def content(self) -> str:
        data = core.content(self.cache, key=KEY, epoch=1, log_seq=self.log_seq, rules=FILE)
        assert data is not None
        return data.decode("utf-8")

    def _commit(self, delta: bytes, *, person: bool = True) -> None:
        assert core.advance(self.cache, key=KEY, epoch=1, log_seq=self.log_seq + 1, delta=delta)
        self.log_seq += 1
        if person:
            self.person.import_(delta)

    def _vv(self) -> bytes:
        entry = self.cache.get(KEY, 1, self.log_seq)
        assert entry is not None
        return core.encode_vv(entry.doc)

    @staticmethod
    def _token(vv: bytes) -> str:
        return vv.hex()


def _peer(
    root: Path,
    server: SandboxServer,
    *,
    agreed: str | None = None,
    states: PeerStates | None = None,
) -> LivePeer:
    peer = LivePeer(api=server, tree=ChatTree(root), states=states or PeerStates())
    peer.relative_of = lambda node: NAME if node == NODE else None
    peer.agreed = lambda _relative: agreed
    return peer


def _disk(root: Path) -> str:
    return (root / NAME).read_bytes().decode("utf-8")


def _save(root: Path, text: str) -> None:
    """The agent's edit tool: the whole file, written in place."""
    (root / NAME).write_bytes(text.encode("utf-8"))


def _agreed(root: Path) -> str:
    return ChatTree(root).digest(NAME)[0]


def test_joining_a_file_whose_disk_is_the_agreed_version_takes_the_document(
    tmp_path: Path,
) -> None:
    """The disk holds what the box and the drive agreed and a person has
    typed since: the document's text is written over it, nothing is sent."""
    server = SandboxServer("one\ntwo\n")
    _save(tmp_path, "one\ntwo\n")
    agreed = _agreed(tmp_path)
    server.type(0, "# person\n")
    peer = _peer(tmp_path, server, agreed=agreed)
    assert peer.join(NODE, NAME)
    assert _disk(tmp_path) == "# person\none\ntwo\n"
    assert server.sent == []
    assert peer.settled() == [NAME]


def test_joining_with_an_agent_edit_not_sent_sends_it_as_made_on_an_unknown_version(
    tmp_path: Path,
) -> None:
    """The agent changed the file since the version the box agreed: the
    change goes up first, naming no state (this process cannot know which
    one the disk was made on), and the merged text comes down with the
    person's edit and the agent's."""
    server = SandboxServer("one\ntwo\n")
    _save(tmp_path, "one\ntwo\n")
    agreed = _agreed(tmp_path)
    server.type(0, "# person\n")
    _save(tmp_path, "one\ntwo\nagent\n")
    peer = _peer(tmp_path, server, agreed=agreed)
    assert peer.join(NODE, NAME)
    assert [base_token for _text, base_token in server.sent] == [None]
    assert _disk(tmp_path) == server.content()
    assert server.content().count("agent") == 1 and "# person" in server.content()


def test_a_send_merged_keeping_everything_is_not_stood_on_as_the_disk_s_state(
    tmp_path: Path,
) -> None:
    """A send naming no state is merged adding what it adds and removing
    nothing, into a state that holds the person's line the box's file never
    had. Stood on as if it held the file's text, the agent's next save (made
    while the answer was on its way) read the person's line as removed and
    deleted it. The box does not stand on such a state: the next save goes
    naming none again, and the person's line stays."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    agreed = _agreed(tmp_path)
    server.type(4, "P\n")
    _save(tmp_path, "one\nagent\n")
    server.meanwhile = lambda: _save(tmp_path, "one\nagent\nagent2\n")
    peer = _peer(tmp_path, server, agreed=agreed)
    peer.join(NODE, NAME)

    assert peer.send(NODE) or peer.join(NODE, NAME)

    lines = server.content().splitlines()
    assert sorted(lines) == ["P", "agent", "agent2", "one"]
    assert [base_token for _text, base_token in server.sent] == [None, None]


@pytest.mark.parametrize(
    ("original", "person", "agent", "expected"),
    [
        pytest.param(
            "one\ntwo\nthree\n",
            (4, "P"),
            "one\nAtwo\nthree\n",
            ("one\nPAtwo\nthree\n", "one\nAPtwo\nthree\n"),
            id="typing-at-the-same-spot",
        ),
        pytest.param(
            "alpha\r\nbeta\r\ngamma\r\n",
            (0, "# person\r\n"),
            "alpha\r\nBETA\r\ngamma\r\n",
            ("# person\r\nalpha\r\nBETA\r\ngamma\r\n",),
            id="crlf",
        ),
        pytest.param(
            "名前 😀 αβγ\nline two\n",
            (4, "!"),
            "名前 😀 αβγ\nline 2\n",
            ("名前 😀! αβγ\nline 2\n",),
            id="multibyte",
        ),
    ],
)
def test_an_agent_save_merges_around_what_a_person_typed_and_comes_back_to_disk(
    tmp_path: Path,
    original: str,
    person: tuple[int, str],
    agent: str,
    expected: tuple[str, ...],
) -> None:
    """The box joined, a person typed, the agent saved: the save is sent as
    made on what the box last wrote, both edits are in the document once,
    and the disk holds the document byte for byte (CRLF and all)."""
    server = SandboxServer(original)
    _save(tmp_path, original)
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    joined_at = server.read(NODE)
    assert joined_at is not None
    server.type(*person)
    _save(tmp_path, agent)
    assert peer.send(NODE)
    assert [base for _text, base in server.sent] == [joined_at.token]
    assert server.content() in expected
    assert (tmp_path / NAME).read_bytes() == server.content().encode("utf-8")
    # The box's own write is no change of the agent's: nothing more is sent.
    assert peer.send(NODE)
    assert len(server.sent) == 1


def test_a_save_landing_while_the_agent_saves_again_is_not_written_over_it(
    tmp_path: Path,
) -> None:
    """The agent saved again while the merged answer was on its way: the
    answer is not written over that save, and the next send goes from the
    state holding exactly what was sent, so the first change is not sent
    twice and the second lands too."""
    server = SandboxServer("a\nb\n")
    _save(tmp_path, "a\nb\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    server.type(0, "# person\n")
    _save(tmp_path, "a\nb\nfirst\n")
    server.meanwhile = lambda: _save(tmp_path, "a\nb\nfirst\nsecond\n")
    assert peer.send(NODE)
    assert _disk(tmp_path) == "a\nb\nfirst\nsecond\n"
    assert peer.send(NODE)
    assert server.content() == "# person\na\nb\nfirst\nsecond\n"
    assert _disk(tmp_path) == server.content()


def test_a_send_whose_answer_was_lost_is_sent_again_under_the_same_id(tmp_path: Path) -> None:
    """The connection dropped after the server merged a save: the send is
    retried with the same id, the server recognises it, and the change is
    in the document once."""
    server = SandboxServer("x\n")
    _save(tmp_path, "x\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    _save(tmp_path, "x\nagent\n")
    server.lose_next = True
    with pytest.raises(httpx.ReadError):
        peer.send(NODE)
    assert peer.send(NODE)
    assert server.content() == "x\nagent\n"
    assert len(server.sent) == 1


def test_an_agent_save_made_before_the_peer_s_last_write_keeps_what_people_typed(
    tmp_path: Path,
) -> None:
    """The agent read the file, the peer then wrote a person's typing to disk,
    and the agent wrote what it had read plus its line over that write. Its
    save is sent as made on the state it read (the closest one the peer
    wrote), not the latest, so the person's typing is kept rather than read
    as removed, and the disk ends with both."""
    server = SandboxServer("one\ntwo\n")
    _save(tmp_path, "one\ntwo\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    read_by_the_agent = _disk(tmp_path)
    server.type(0, "# person\n")
    assert peer.refresh(NODE)
    assert _disk(tmp_path) == "# person\none\ntwo\n"
    _save(tmp_path, read_by_the_agent + "agent\n")
    assert peer.send(NODE)
    assert server.content() == "# person\none\ntwo\nagent\n"
    assert _disk(tmp_path) == server.content()


def test_an_agent_save_made_on_its_last_save_before_the_answer_landed_is_sent_once(
    tmp_path: Path,
) -> None:
    """The agent saved, the peer sent it and wrote the merged answer (with a
    person's line) to disk, but the agent had read its own save before that
    write and saved again over it. The new save is made on exactly what was
    sent, so its earlier line is not sent a second time."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    server.type(0, "# person\n")
    _save(tmp_path, "one\nagent1\n")
    assert peer.send(NODE)
    assert _disk(tmp_path) == "# person\none\nagent1\n"
    _save(tmp_path, "one\nagent1\nagent2\n")
    assert peer.send(NODE)
    assert server.content() == "# person\none\nagent1\nagent2\n"
    assert _disk(tmp_path) == server.content()


def test_a_send_whose_answer_was_lost_is_settled_before_the_next_save_goes(
    tmp_path: Path,
) -> None:
    """The server merged a save and went away before answering; the agent
    then saved again on top of it. The lost send is asked again under its id
    first, so the next save goes as made on the state it made and the first
    save's line is not sent twice."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    server.type(0, "# person\n")
    _save(tmp_path, "one\nagent1\n")
    server.lose_next = True
    with pytest.raises(httpx.ReadError):
        peer.send(NODE)
    _save(tmp_path, "one\nagent1\nagent2\n")
    assert peer.send(NODE)
    assert server.content() == "# person\none\nagent1\nagent2\n"
    assert _disk(tmp_path) == server.content()


def test_the_document_moving_is_written_to_disk_unless_the_agent_wrote_first(
    tmp_path: Path,
) -> None:
    """A person typed: the box writes the document to disk. Then a person
    typed while the agent had saved too: the agent's change is sent first
    and the merged text written, so neither is lost."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    server.type(0, "# person\n")
    assert peer.refresh(NODE)
    assert _disk(tmp_path) == "# person\none\n"
    assert server.sent == []
    server.type(0, "# again\n")
    _save(tmp_path, "# person\none\nagent\n")
    assert peer.refresh(NODE)
    assert server.content() == "# again\n# person\none\nagent\n"
    assert _disk(tmp_path) == server.content()


@pytest.mark.parametrize("kept", [True, False], ids=["states-kept", "nothing-kept"])
def test_a_restarted_peer_names_the_state_its_file_was_made_on(tmp_path: Path, kept: bool) -> None:
    """The box wrote a state to its file, went down, and the agent edited the
    file while a person typed on in the line it had written. The restarted
    peer names the state the file was made on: the agent's removal applies,
    and the person's line is theirs once. Named nothing (no state kept), the
    file is merged keeping everything: the line the person typed on comes
    back beside its older copy, and the agent's removal does not apply."""
    states = PeerStates.beside(type("Journal", (), {"path": tmp_path / "folder.journal"}))
    server = SandboxServer("one\ntwo\n")
    box = tmp_path / "box"
    box.mkdir()
    _save(box, "one\ntwo\n")
    first = _peer(box, server, agreed=_agreed(box), states=states)
    assert first.join(NODE, NAME)
    server.type(3, " P")
    assert first.refresh(NODE)
    assert _disk(box) == "one P\ntwo\n"
    first.close()
    server.type(5, "Q")
    _save(box, "one P\nagent\n")

    again = _peer(box, server, states=states if kept else PeerStates())
    assert again.join(NODE, NAME)

    if kept:
        assert server.content() == "one PQ\nagent\n"
        assert server.sent[-1][1] is not None
    else:
        assert server.content().count("one P") == 2 and "two" in server.content()
    assert _disk(box) == server.content()


def test_a_notice_is_taken_on_the_peer_s_own_thread(tmp_path: Path) -> None:
    """A notice from the machine channel never blocks its caller: the peer's
    thread joins a file it did not hold, then follows the document."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    joined = threading.Event()
    peer.on_join = lambda _node, _relative: joined.set()
    server.type(0, "# person\n")
    try:
        peer.notify(NODE, "token-from-the-server")
        assert joined.wait(5)
        assert _disk(tmp_path) == "# person\none\n"
    finally:
        peer.close()


def _closed_then_saved(server: SandboxServer, root: Path) -> None:
    server.live = False
    _save(root, "one\ntwo\n")


@pytest.mark.parametrize(
    "leaves",
    [
        pytest.param(_closed_then_saved, id="session-closed"),
        pytest.param(
            lambda server, root: (root / NAME).write_bytes(b"\xff\xfe binary"), id="not-text"
        ),
        pytest.param(lambda server, root: (root / NAME).unlink(), id="gone"),
    ],
)
def test_a_file_that_stops_being_live_text_goes_back_to_the_ordinary_path(
    tmp_path: Path, leaves: Callable[[SandboxServer, Path], object]
) -> None:
    """Its session closed, it is no longer text, or it is gone: the peer lets
    it go and the live sync's ordinary path takes it (``send`` says False)."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    leaves(server, tmp_path)
    assert peer.send(NODE) is False
    assert not peer.owns(NODE)


def test_a_peer_whose_folder_was_let_go_sends_and_writes_nothing(tmp_path: Path) -> None:
    """The folder was let go (a lost lease, a process going away): a run left
    behind must not race the next holder's peer, so a save is taken and not
    sent (the next holder sends it), and the document is not written to disk."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME)
    peer.close()
    _save(tmp_path, "one\nagent\n")
    server.type(0, "# person\n")
    assert peer.send(NODE) is True
    assert peer.refresh(NODE) is False
    assert peer.join(NODE, NAME) is False
    assert server.sent == []
    assert _disk(tmp_path) == "one\nagent\n"


def test_a_file_nobody_touched_for_a_while_is_let_go(tmp_path: Path) -> None:
    now = [100.0]
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    peer.clock = lambda: now[0]
    assert peer.join(NODE, NAME)
    now[0] += peer.idle_seconds - 1
    assert peer.expire() == []
    now[0] += 2
    assert peer.expire() == [NODE]
    assert not peer.owns(NODE)


def test_a_file_with_no_live_session_is_never_joined(tmp_path: Path) -> None:
    server = SandboxServer("one\n")
    server.live = False
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.join(NODE, NAME) is False
    assert not peer.owns(NODE)


def test_the_checkpoint_push_leaves_alone_what_the_live_plane_sends_itself(
    tmp_path: Path,
) -> None:
    """One sender per file. While the plane runs: the whole watched directory
    until its first sweep has queued what is on disk, then what it has
    queued and what its text peer holds. Once stopped: the peer's files whose
    disk holds the document's text (it has every byte; uploading them would
    merge a stale copy), and nothing the plane merely had queued."""
    import asyncio

    from alkera_cli.files.tree_watch import Change
    from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher, make_sync

    working = tmp_path / "scratch"
    working.mkdir()
    server = SandboxServer("one\n")
    _save(working, "one\n")
    sync = make_sync(working, FakeLiveApi(root=working), FakeClock(), watcher=FakeWatcher([set()]))
    sync.peer = _peer(working, server, agreed=_agreed(working))
    assert sync.sent_live("scratch", running=True) == ["scratch"]
    asyncio.run(sync.run())
    # A run that ends closes its peer; the plane's next run has a fresh one.
    assert sync.peer.join(NODE, NAME) is False
    sync.peer = _peer(working, server, agreed=_agreed(working))
    assert sync.peer.join(NODE, NAME)
    (working / "draft.md").write_text("queued")
    sync.classify(Change.added, str(working / "draft.md"))
    assert sync.sent_live("scratch", running=True) == ["scratch/draft.md", "scratch/plan.txt"]
    assert sync.sent_live("scratch", running=False) == ["scratch/plan.txt"]
    _save(working, "one\nunsent\n")
    assert sync.sent_live("scratch", running=False) == []
    assert sync.sent_live("", running=True) == []


def _restarted(tmp_path: Path, server: SandboxServer, *, settle_ms: int = 0) -> tuple[Any, Any]:
    """A box restarted with ``plan.txt`` known (as a pull proved it) and the
    agent's edit made to it while the box was down, half a second ago."""
    from alkera_cli.files.live_sync import LiveCadence
    from alkera_cli.files.pull import PulledFile
    from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher, make_sync

    working = tmp_path / "scratch"
    working.mkdir()
    _save(working, "one\n")
    api = FakeLiveApi(root=working)
    sync = make_sync(
        working,
        api,
        FakeClock(),
        watcher=FakeWatcher([set()]),
        cadence=LiveCadence(settle_ms=settle_ms),
    )
    digest, size = ChatTree(working).digest(NAME)
    stat = (working / NAME).stat()
    pulled = PulledFile(
        node_id=NODE,
        etag="1",
        content_hash=digest,
        size=size,
        stamp=(stat.st_size, stat.st_mtime_ns),
    )
    assert sync.adopt({NAME: pulled}) == 1
    sync.peer = _peer(working, server, agreed=digest)
    _save(working, "one\nagent\n")
    written = (working / NAME).stat().st_mtime_ns - 500_000_000
    os.utime(working / NAME, ns=(written, written))
    return sync, api


@pytest.mark.parametrize(
    "settle_ms",
    [
        pytest.param(0, id="settled"),
        # Offered to the peer, it settles as a live file does: an agent saving
        # every second would otherwise hold it the served two seconds and more.
        pytest.param(2000, id="on-the-live-settle-time"),
    ],
)
def test_a_restarted_box_offers_a_file_changed_while_it_was_down_to_its_peer_first(
    tmp_path: Path, settle_ms: int
) -> None:
    """A restarted box knows which of its files people have open only once a
    write back names one. The agent's edits made while it was down waited for
    that, or went up the ordinary way and waited for the session to merge
    the upload; the first sweep now offers each file it finds changed to the
    text peer once, and a file whose document is open goes as the peer's."""
    import asyncio

    server = SandboxServer("one\n")
    sync, api = _restarted(tmp_path, server, settle_ms=settle_ms)

    asyncio.run(sync.run())

    assert server.content() == "one\nagent\n"
    assert [text for text, _base in server.sent] == ["one\nagent\n"]
    assert api.uploads == []


def test_a_restarted_box_s_send_that_landed_with_no_state_to_stand_on_uploads_nothing(
    tmp_path: Path,
) -> None:
    """The restarted peer's first send was merged keeping everything (it could
    name no state) into a state holding a person's line its file lacked, and
    the agent saved again while the answer was on its way: there is no state
    to stand on. The file used to go up the ordinary way then, and the
    session merged the upload from the closest version it had, which could
    already hold the agent's next line, read as removed and lost. Nothing is
    uploaded: the next save joins afresh."""
    import asyncio

    server = SandboxServer("one\n")
    server.type(4, "P\n")
    sync, api = _restarted(tmp_path, server)
    server.meanwhile = lambda: _save(tmp_path / "scratch", "one\nagent\nagent2\n")

    asyncio.run(sync.run())

    assert api.uploads == []
    assert len(server.sent) == 1 and "agent" in server.content().splitlines()


def test_a_restarted_box_s_changed_file_with_no_live_document_goes_the_ordinary_way(
    tmp_path: Path,
) -> None:
    import asyncio

    server = SandboxServer("one\n")
    server.live = False
    sync, api = _restarted(tmp_path, server)

    asyncio.run(sync.run())

    assert server.sent == []
    assert api.uploads == [NAME]


# -- the drive holding the document, or not -----------------------------------


def test_a_document_the_drive_does_not_hold_is_never_counted_as_landed(tmp_path: Path) -> None:
    """Nothing will write the document back (no person to write it as): the
    agent's save is the document's, and still the file's own upload's. It
    is not among the files nothing need upload until an answer says the
    drive holds the document again."""
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    server.saved = False
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.take(NODE, NAME) is Taken.UNSAVED
    assert peer.owns(NODE)
    _save(tmp_path, "one\nagent\n")
    assert peer.take(NODE, NAME) is Taken.UNSAVED
    assert server.content() == "one\nagent\n"
    assert peer.settled() == []
    assert peer.unsaved() == [NAME]

    server.saved = True
    _save(tmp_path, "one\nagent\nagain\n")
    assert peer.take(NODE, NAME) is Taken.LANDED
    assert peer.settled() == [NAME]
    assert peer.unsaved() == []


def test_a_file_with_no_live_document_is_taken_by_nobody(tmp_path: Path) -> None:
    server = SandboxServer("one\n")
    server.live = False
    server.saved = False
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path))
    assert peer.take(NODE, NAME) is Taken.LEFT
    assert peer.unsaved() == []


def _answering(response: httpx.Response) -> RestPeerApi:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    http = httpx.Client(base_url="http://drive.test", transport=httpx.MockTransport(handler))
    return RestPeerApi(http=http, drive_id="drive-1")


@pytest.mark.parametrize(
    ("body", "saved"),
    [
        pytest.param({"saved": True}, True, id="saved"),
        pytest.param({"saved": False}, False, id="not-saved"),
        # A server from before the field writes every document back.
        pytest.param({}, True, id="absent"),
        pytest.param({"saved": None}, True, id="null"),
    ],
)
def test_the_live_routes_answer_says_whether_the_drive_holds_the_document(
    body: dict[str, Any], saved: bool
) -> None:
    api = _answering(httpx.Response(200, json={"live": True, "token": "t", "text": "x"} | body))
    read = api.read(NODE)
    sent = api.submit(NODE, "x", base_token=None, submit_id="s")
    assert read is not None and read.saved is saved
    assert sent is not None and sent.saved is saved


@pytest.mark.parametrize(
    ("status", "code", "raised"),
    [
        pytest.param(500, "internal", PeerRefusedError, id="server-error"),
        pytest.param(422, "files.crdt.refused", PeerRefusedError, id="unprocessable"),
        pytest.param(409, "files.exists", PeerRefusedError, id="conflict-about-the-file"),
        pytest.param(409, "files.lease_fenced", LeaseSupersededError, id="fenced"),
        pytest.param(409, "files.leased", LeaseSupersededError, id="leased"),
    ],
)
def test_an_error_from_the_live_routes_is_the_file_s_unless_it_is_the_fence_s(
    status: int, code: str, raised: type[Exception]
) -> None:
    api = _answering(httpx.Response(status, json={"error": {"code": code, "message": code}}))
    with pytest.raises(raised):
        api.read(NODE)
    with pytest.raises(raised):
        api.submit(NODE, "x", base_token=None, submit_id="s")


@pytest.mark.parametrize("status", [404, 405])
def test_a_file_the_drive_lacks_or_a_server_without_the_routes_is_not_live(status: int) -> None:
    assert _answering(httpx.Response(status)).read(NODE) is None


@dataclass
class _Refusing:
    """A live route that answers reads and refuses sends (or both)."""

    server: SandboxServer
    refuse_reads: bool = False

    def read(self, node_id: str) -> PeerState | None:
        if self.refuse_reads:
            raise PeerRefusedError(node_id, 500)
        return self.server.read(node_id)

    def submit(
        self,
        node_id: str,
        text: str,
        *,
        base_token: str | None,
        submit_id: str,
        base_etag: int | None = None,
    ) -> PeerState | None:
        raise PeerRefusedError(node_id, 500)


def _kept(tmp_path: Path) -> PeerStates:
    return PeerStates.beside(type("Journal", (), {"path": tmp_path / "folder.journal"}))


def test_a_refused_send_lets_the_file_go_and_keeps_what_was_kept_for_it(
    tmp_path: Path,
) -> None:
    """A refusal is no news about the session: the states the box wrote stay
    for the next join to name."""
    states = _kept(tmp_path)
    server = SandboxServer("one\n")
    box = tmp_path / "box"
    box.mkdir()
    _save(box, "one\n")
    route = _Refusing(server)
    peer = LivePeer(api=route, tree=ChatTree(box), states=states)
    peer.agreed = lambda _relative: _agreed(box)
    assert peer.join(NODE, NAME)
    kept = states.load(NODE)
    assert kept != []

    _save(box, "one\nagent\n")
    assert peer.take(NODE, NAME) is Taken.LEFT
    assert not peer.owns(NODE)
    assert states.load(NODE) == kept

    route.refuse_reads = True
    assert peer.join(NODE, NAME) is False
    assert peer.refresh(NODE) is False


def test_an_idle_file_rejoined_with_an_agent_edit_names_the_state_it_was_made_on(
    tmp_path: Path,
) -> None:
    """Let go for idleness, the file keeps the states the box wrote to it. The
    agent then edits the line a person typed on, and the person types on in
    it: rejoined, the edit is sent as made on the state the box wrote, so
    the agent's removal applies and the person's line is theirs once.
    Named nothing, the edit would be merged keeping everything and the line
    would come back beside its older copy."""
    states = _kept(tmp_path)
    now = [100.0]
    server = SandboxServer("one\ntwo\n")
    _save(tmp_path, "one\ntwo\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path), states=states)
    peer.clock = lambda: now[0]
    assert peer.join(NODE, NAME)
    server.type(3, " P")
    assert peer.refresh(NODE)
    assert _disk(tmp_path) == "one P\ntwo\n"
    now[0] += peer.idle_seconds
    assert peer.expire() == [NODE]
    assert states.load(NODE) != []

    server.type(5, "Q")
    _save(tmp_path, "one P\nagent\n")
    assert peer.join(NODE, NAME)

    assert server.sent[-1][1] is not None
    assert server.content() == "one PQ\nagent\n"
    assert _disk(tmp_path) == server.content()


def _session_closes(server: SandboxServer, root: Path) -> None:
    server.live = False


@pytest.mark.parametrize(
    "ends",
    [
        pytest.param(_session_closes, id="session-closed"),
        pytest.param(
            lambda server, root: (root / NAME).write_bytes(b"\xff\xfe binary"), id="not-text"
        ),
    ],
)
def test_a_file_whose_session_closed_or_that_stopped_being_text_forgets_its_states(
    tmp_path: Path, ends: Callable[[SandboxServer, Path], object]
) -> None:
    states = _kept(tmp_path)
    server = SandboxServer("one\n")
    _save(tmp_path, "one\n")
    peer = _peer(tmp_path, server, agreed=_agreed(tmp_path), states=states)
    assert peer.join(NODE, NAME)
    server.type(0, "# person\n")
    assert peer.refresh(NODE)
    assert states.load(NODE) != []
    ends(server, tmp_path)
    if server.live:
        server.type(0, "more ")  # the document moves, and the peer reads the disk
    assert peer.refresh(NODE) is False
    assert not peer.owns(NODE)
    assert states.load(NODE) == []


# -- the drive version a joining disk was made on ------------------------------

PLAN = "1. one\n2. two\n3. three\n"


@dataclass
class HeldBox:
    """A held folder's live sync with its text peer, wired as the box wires
    them, its node map kept where a restarted process reads it back."""

    working: Path
    api: Any
    sync: Any
    peer: LivePeer

    @property
    def node(self) -> str:
        return str(self.api.nodes[NAME])

    def restarted(self, server: SandboxServer) -> HeldBox:
        """The box process dies and a new one takes the same folder: nothing
        in memory survives, the node map does."""
        return _held(self.working, server, api=self.api)


def _held(working: Path, server: SandboxServer, *, api: Any = None) -> HeldBox:
    from alkera_cli.files.nodemap import NodeMapStore
    from files._live_sync_fakes import FakeClock, FakeInboundApi, make_sync

    working.mkdir(exist_ok=True)
    api = api or FakeInboundApi(root=working)
    peer = LivePeer(api=server, tree=ChatTree(working))
    sync = make_sync(working, api, FakeClock(), peer=peer)
    sync.node_map = NodeMapStore(mount_root=working.parent, home=working.parent / "home")
    sync.seed()
    return HeldBox(working=working, api=api, sync=sync, peer=peer)


def _uploaded(tmp_path: Path, server: SandboxServer) -> HeldBox:
    """The agent wrote ``PLAN`` and the box uploaded it: the drive holds it at
    etag 1, which the live document was opened from."""
    from alkera_cli.files.tree_watch import Change

    box = _held(tmp_path / "scratch", server)
    _save(box.working, PLAN)
    box.sync.classify(Change.added, str(box.working / NAME))
    box.sync.flush()
    assert box.api.etags[box.node] == 1
    server.file(1)
    return box


def test_joining_with_an_agent_edit_names_the_drive_version_the_box_agreed(
    tmp_path: Path,
) -> None:
    """A person typed into a line of the file before the box joined, and the
    agent's edit is on the version the box uploaded. Sent naming no state,
    it was merged from a state holding the typing, keeping everything: the
    line came back untyped beside the typed one. Named as made on the drive
    version the box agreed, it is merged from exactly there."""
    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    server.type(len("1. one"), " typed")
    _save(box.working, "1. one\n2. two\nagent0\n3. three\n")

    assert box.peer.join(box.node, NAME)

    assert server.content() == "1. one typed\n2. two\nagent0\n3. three\n"
    assert _disk(box.working) == server.content()
    assert server.named_etags == [1]


def test_a_send_that_may_have_landed_never_names_the_agreed_version_again(
    tmp_path: Path,
) -> None:
    """The first send's answer was lost after the document took it. The
    agent's next save carries that send's line too: named as made on the
    agreed version again, the line would land a second time."""
    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    server.type(0, "P\n")
    _save(box.working, "1. one\n2. two\nagent0\n3. three\n")
    server.lose_next = True
    with pytest.raises(httpx.ReadError):
        box.peer.join(box.node, NAME)
    assert server.content().count("agent0") == 1

    _save(box.working, "1. one\n2. two\nagent1\nagent0\n3. three\n")
    assert box.peer.join(box.node, NAME)

    assert sorted(server.content().splitlines()) == sorted(
        ["P", "1. one", "2. two", "agent1", "agent0", "3. three"]
    )
    assert server.named_etags == [1, None]


@pytest.mark.parametrize(
    ("sent_before", "named"),
    [
        # The old process never wrote or sent the file: the agent's edits made
        # while the box was down are on the version it uploaded.
        pytest.param(False, [1], id="untouched-since-the-upload"),
        # The old process sent an edit the document took: its disk descends
        # from that, and the version it uploaded names it no longer.
        pytest.param(True, [1, None], id="sent-since-the-upload"),
    ],
)
def test_a_restarted_box_names_the_agreed_version_only_while_its_disk_descends_from_it(
    tmp_path: Path, sent_before: bool, named: list[int | None]
) -> None:
    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    server.type(0, "P\n")
    edit = "1. one\n2. two\nagent0\n3. three\n"
    _save(box.working, edit)
    if sent_before:
        assert box.peer.join(box.node, NAME)
        # The answer reached the disk; the process dies before anything else.
        edit = _disk(box.working)
    edit = edit.replace("3. three\n", "agent1\n3. three\n")

    restarted = box.restarted(server)
    _save(restarted.working, edit)
    assert restarted.peer.join(restarted.node, NAME)

    lines = server.content().splitlines()
    assert sorted(lines) == sorted(["P", "1. one", "2. two", "agent0", "agent1", "3. three"])
    assert _disk(restarted.working) == server.content()
    assert server.named_etags == named


def test_an_upload_the_drive_did_not_confirm_leaves_no_version_to_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box uploaded the agent's line but never heard the etag the drive
    filed it at, and the session merged the upload. The disk is made on
    those bytes, not on the version the box last heard of: named as made on
    that one, the uploaded line would land twice."""
    from alkera_cli.files.tree_watch import Change

    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    upload = box.api.upload

    def unanswered(*args: Any, **kwargs: Any) -> str | None:
        upload(*args, **kwargs)
        return None

    monkeypatch.setattr(box.api, "upload", unanswered)
    _save(box.working, "1. one\n2. two\nagent0\n3. three\n")
    box.sync.classify(Change.modified, str(box.working / NAME))
    box.sync.flush()
    assert box.api.uploads[-1] == NAME
    server.type(len("1. one\n2. two\n"), "agent0\n")  # the session merged the upload
    _save(box.working, "1. one\n2. two\nagent1\nagent0\n3. three\n")

    assert box.peer.join(box.node, NAME)

    assert server.content().count("agent0") == 1
    assert "agent1" in server.content()
    assert server.named_etags == [None]


def _written_back(box: HeldBox, server: SandboxServer, text: str) -> None:
    """The live session writes its document back to the drive as version 2,
    and the box takes that write back onto its disk."""
    box.api.paths = {box.node: NAME}
    box.api.web_write(box.node, text.encode("utf-8"), seq=2)
    server.file(2)
    box.sync.pull_inbound()


def _pulled_at_take(box: HeldBox, server: SandboxServer, text: str) -> None:
    """The take's pull found the drive moved to version 2 and wrote it."""
    from alkera_cli.files.pull import PulledFile

    _save(box.working, text)
    digest, size = ChatTree(box.working).digest(NAME)
    stat = (box.working / NAME).stat()
    server.file(2)
    pulled = PulledFile(
        node_id=box.node,
        etag="2",
        content_hash=digest,
        size=size,
        stamp=(stat.st_size, stat.st_mtime_ns),
    )
    assert box.sync.adopt({NAME: pulled}) == 1


@pytest.mark.parametrize(
    "arrives",
    [
        pytest.param(_written_back, id="inbound-write-back"),
        pytest.param(_pulled_at_take, id="take"),
    ],
)
def test_bytes_the_box_was_handed_are_never_named_as_what_the_agent_s_save_was_made_on(
    tmp_path: Path, arrives: Callable[[HeldBox, SandboxServer, str], None]
) -> None:
    """The drive's version 2 (a person's line in it) was written over the
    box's file while the agent had the file open from before: its save is of
    the version before, without the person's line. Named as made on version
    2, the line would read as removed by the agent; it stays."""
    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    server.type(0, "P\n")
    arrives(box, server, "P\n" + PLAN)
    assert _disk(box.working) == "P\n" + PLAN
    _save(box.working, "1. one\n2. two\nagent0\n3. three\n")  # read before

    assert box.peer.join(box.node, NAME)

    assert sorted(server.content().splitlines()) == sorted(
        ["P", "1. one", "2. two", "agent0", "3. three"]
    )
    assert server.named_etags == [None]


@pytest.mark.parametrize("fails", [pytest.param("before"), pytest.param("after")])
def test_a_send_the_server_failed_is_asked_again_under_its_id_before_the_next(
    tmp_path: Path, fails: str
) -> None:
    """The server went away in the middle of the first send, having taken
    it or not; the file left the peer. The agent saved again, and the box
    joins afresh: the send that failed goes again under its id first (taken
    once, whichever it was), and the new save goes as made on the state it
    made. Sent naming nothing instead, it was merged from a state holding a
    person's typing and the typed line came back beside its older copy."""
    server = SandboxServer(PLAN)
    box = _uploaded(tmp_path, server)
    server.type(len("1. one"), " typed")
    _save(box.working, "1. one\n2. two\nagent0\n3. three\n")
    server.fail_next = fails
    assert box.peer.join(box.node, NAME) is False
    _save(box.working, "1. one\n2. two\nagent1\nagent0\n3. three\n")

    assert box.peer.join(box.node, NAME)

    assert server.content() == "1. one typed\n2. two\nagent1\nagent0\n3. three\n"
    assert _disk(box.working) == server.content()
