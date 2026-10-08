"""The box as a text peer of the files people have open live.

While people co-edit a file in a chat's working folder, its live document on
the server is the truth, and the agent edits the same file on this box's disk.
Uploading the agent's edit and leaving the server's session to merge it from
the drive took seconds and sometimes copies. This module makes the box a peer
of the document instead, speaking whole texts and a version token (no Loro on
the box; the server's sandbox does every merge):

* **Joining.** A file becomes the peer's when the server says its live
  document moved (a ``live_text`` notice on the machine channel), when a
  live session's write back of it arrives inbound, or when the agent saves a
  file a session writes. The peer reads the document (``GET .../live``): a
  disk that still holds what the box and the drive last agreed takes the
  document's text at once; a disk the agent changed since sends that change
  first, as made on the drive version the box and the drive last agreed
  (the server merges it from exactly there), and takes the merged text. Only
  when that version no longer describes the disk (the peer wrote or sent the
  file since) or the box agreed none is the change sent as made on a state
  this process cannot name: the server then merges it from the closest state
  it handed this box or wrote back, removing nothing. A peer keeps the last
  states it wrote to each file on disk
  (:class:`~alkera_cli.files.peer_states.PeerStates`), so after a restart it
  names the one its file was made on and merges exactly.
* **The agent's edits.** A change to a joined file on disk is sent as the
  whole text and the token of the state it was made on (``POST .../live``):
  of the states the peer wrote to disk lately, the one the new text differs
  least from. An agent reads, changes and writes a file whole, so one that
  read before the peer's latest write and wrote after it made its change on
  the state before; named as made on the latest, every person's edit in
  between would read as removed. The server diffs it against exactly that
  state, so people's concurrent typing is kept and the agent's change lands
  around it. The merged answer is written back to disk when the disk has not
  moved meanwhile; when it has (the agent saved again), the peer stands on
  the state holding exactly what it sent, and the next save goes from there.
  Each send carries an id reused for its retries, so a send whose answer was
  lost is never applied twice.
* **The document's edits.** When the document moves, the peer reads it and
  writes it to disk, provided the disk still holds what the peer last wrote;
  otherwise the agent's change goes up first and the merged text is written.
* **Saved or not.** Every answer says whether the drive holds the
  document's text (``saved``; a server from before the field means it does).
  A document whose write back could not be made (no person to write it as)
  holds the agent's edit the drive does not: the file is then uploaded the
  ordinary way too, and the drive recognises the same text or merges it.
* **Leaving.** A file nobody touched for :data:`PEER_IDLE_SECONDS`, one whose
  session closed, one that stopped being text or grew past what a session
  opens, and one the live routes refuse, goes back to the live plane's
  ordinary path (upload, merge). Only that file: the round goes on.

The live sync (:mod:`alkera_cli.files.live_sync`) hands a joined file's disk
changes and inbound write backs here, and uploads it only while the drive
does not hold its document (:class:`Taken`). Every touch
of the disk goes through the chat's :class:`~alkera_cli.files.chat_fs.ChatTree`.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx

from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.mount import superseded_from
from alkera_cli.files.peer_states import PeerStates

logger = logging.getLogger(__name__)

__all__ = [
    "PEER_IDLE_SECONDS",
    "PEER_MAX_BYTES",
    "LivePeer",
    "PeerApi",
    "PeerRefusedError",
    "PeerState",
    "PeerStates",
    "RestPeerApi",
    "Taken",
]

#: How long a joined file nobody touched stays the peer's.
PEER_IDLE_SECONDS: Final = 120.0
#: The largest file a live session opens; a larger one is written as a file.
PEER_MAX_BYTES: Final = 1024 * 1024
#: How long the notice thread sleeps between looks when nothing arrives.
NOTICE_IDLE_WAIT: Final = 5.0
#: How many times a file replaced while it is read is read again.
READ_TRIES: Final = 5
#: How many states a joined file remembers having written to disk.
STATES_KEPT: Final = 8

Stamp = tuple[int, int]


@dataclass(frozen=True, slots=True)
class PeerState:
    """The document as the server answered it: its text and token, and for a
    send the token of the state the text sent was merged at."""

    token: str
    text: str
    at_token: str | None = None
    #: What the state ``at_token`` names holds, when it is not the text sent
    #: (a send naming no state is merged adding what it adds and removing
    #: nothing, so that state can hold more).
    at_text: str | None = None
    #: A retry of a send the server had already merged.
    repeat: bool = False
    #: Whether the drive holds the document's text, or will once its write
    #: back runs. ``False``: nothing will write it back, so the file must be
    #: uploaded the ordinary way.
    saved: bool = True


class PeerRefusedError(RuntimeError):
    """The live routes refused one file with an error status: that file
    leaves the peer, and every other file goes on."""

    def __init__(self, node_id: str, status: int) -> None:
        super().__init__(f"the live route answered {status} for {node_id}")
        self.status = status


class Taken(enum.Enum):
    """What became of an agent's save of a file a live session writes."""

    #: The document took it and the drive will hold it: nothing to upload.
    LANDED = "landed"
    #: The document took it but nothing will write it to the drive: the file
    #: is uploaded the ordinary way, and must not count as landed.
    UNSAVED = "unsaved"
    #: Not the peer's (no session, not text, refused): the ordinary way.
    LEFT = "left"


class PeerApi(Protocol):
    def read(self, node_id: str) -> PeerState | None: ...

    def submit(
        self,
        node_id: str,
        text: str,
        *,
        base_token: str | None,
        submit_id: str,
        base_etag: int | None = None,
    ) -> PeerState | None: ...


@dataclass
class RestPeerApi:
    """The live text routes over the held folder's own fenced client: only
    this lease's holder is answered, and every call names its epoch."""

    http: httpx.Client
    drive_id: str
    base: str = "/api/v1/files"

    def read(self, node_id: str) -> PeerState | None:
        response = self.http.get(self._url(node_id))
        return self._state(node_id, response)

    def submit(
        self,
        node_id: str,
        text: str,
        *,
        base_token: str | None,
        submit_id: str,
        base_etag: int | None = None,
    ) -> PeerState | None:
        body: dict[str, Any] = {"text": text, "submitId": submit_id}
        if base_token is not None:
            body["baseToken"] = base_token
        if base_etag is not None:
            body["baseEtag"] = base_etag
        response = self.http.post(self._url(node_id), json=body)
        return self._state(node_id, response)

    def _url(self, node_id: str) -> str:
        return f"{self.base}/drives/{self.drive_id}/items/{node_id}/live"

    @staticmethod
    def _state(node_id: str, response: httpx.Response) -> PeerState | None:
        if response.status_code in (404, 405):
            # A file the drive no longer has, or a server from before the
            # routes: either way, the ordinary path.
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as failure:
            # The fence's refusal is the folder's, never one file's: the
            # live plane stops. Any other is this file's alone.
            superseded = superseded_from(failure)
            if superseded is not None:
                raise superseded from failure
            raise PeerRefusedError(node_id, response.status_code) from failure
        body: Any = response.json()
        if not isinstance(body, Mapping) or body.get("live") is not True:
            return None
        token, text = body.get("token"), body.get("text")
        if not isinstance(token, str) or not isinstance(text, str):
            return None
        at_token = body.get("atToken")
        at_text = body.get("atText")
        return PeerState(
            token=token,
            text=text,
            at_token=at_token if isinstance(at_token, str) else None,
            at_text=at_text if isinstance(at_text, str) else None,
            repeat=body.get("repeat") is True,
            saved=body.get("saved") is not False,
        )


@dataclass(frozen=True, slots=True)
class _Pending:
    """A send not yet answered, reused on a retry so the server recognises it."""

    submit_id: str
    base_token: str | None
    base_etag: int | None
    text: str

    def same(self, base_token: str | None, base_etag: int | None, text: str) -> bool:
        return (self.base_token, self.base_etag, self.text) == (base_token, base_etag, text)


@dataclass
class _Joined:
    """One file the peer holds: the document state it last wrote to disk (or
    found there), and the disk's stamp when it did."""

    relative: str
    token: str
    text: str
    stamp: Stamp | None
    used: float
    #: The states the peer wrote to disk (or found there) lately, oldest
    #: first. An agent edit is read, changed and written whole: one that read
    #: the file before the peer's latest write and wrote after it was made on
    #: an earlier state, and sent as made on the latest it would read every
    #: person's edit in between as removed.
    seen: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=STATES_KEPT))
    #: Where the states written are kept across a restart (see
    #: :meth:`LivePeer.join`), called with them oldest first.
    keep: Callable[[list[tuple[str, str]]], None] = lambda _states: None
    #: Called whenever the disk holds, or is about to hold, a document state:
    #: from then on the drive version the box agreed no longer describes it.
    descend: Callable[[], None] = lambda: None

    def stand(self, token: str, text: str) -> None:
        """The disk holds the document's state ``token``, whose text is ``text``."""
        self.descend()
        self.token, self.text = token, text
        if not self.seen or self.seen[-1] != (token, text):
            self.seen.append((token, text))
            self.keep(list(self.seen))

    def ahead(self, token: str, text: str) -> None:
        """The disk is about to hold ``token``: kept before it does, so a
        process that dies between the two names it or the state before."""
        self.descend()
        self.keep([*self.seen, (token, text)])

    def base_for(self, disk: str) -> str:
        """The token of the state ``disk`` was most likely made from: of the
        states the peer wrote lately, the one it differs least from (the
        latest on a tie)."""
        best: tuple[int, str] | None = None
        for token, text in reversed(self.seen):
            changed = changed_between(text, disk)
            if best is None or changed < best[0]:
                best = (changed, token)
        return self.token if best is None else best[1]


def changed_between(before: str, after: str) -> int:
    """How much of two texts differs: their lengths past a shared start and
    end. Small when one is the other plus an edit."""
    limit = min(len(before), len(after))
    start = 0
    while start < limit and before[start] == after[start]:
        start += 1
    end = 0
    while end < limit - start and before[-1 - end] == after[-1 - end]:
        end += 1
    return (len(before) - start - end) + (len(after) - start - end)


@dataclass
class LivePeer:
    """One held folder's text peer. Thread-safe: the live sync's thread sends
    the agent's edits, the drain thread hands write backs over, and the
    machine channel's notices are taken on a thread of the peer's own."""

    api: PeerApi
    tree: ChatTree
    clock: Callable[[], float] = time.monotonic
    idle_seconds: float = PEER_IDLE_SECONDS
    max_bytes: int = PEER_MAX_BYTES
    #: Where the live sync is told a node's path, for a notice naming a node
    #: the peer has not joined yet (``None`` when the box does not know it).
    relative_of: Callable[[str], str | None] = lambda _node: None
    #: The digest of what the box and the drive last agreed a path holds, or
    #: ``None``.
    agreed: Callable[[str], str | None] = lambda _relative: None
    #: The drive etag of the version a path's disk was made on: the bytes the
    #: box and the drive last agreed, while this peer has not written or sent
    #: the file since (see :attr:`descend`). ``None`` when there is none.
    agreed_etag: Callable[[str], int | None] = lambda _relative: None
    #: Called with a path before this peer writes or sends its file: the disk
    #: descends from a document state from then on, and the drive version
    #: agreed before names it no longer, in this process or the next.
    descend: Callable[[str], None] = lambda _relative: None
    #: Called with ``(node_id, relative)`` when a node joins, so the live sync
    #: treats it as a live file (short settle, ahead of the batch window).
    on_join: Callable[[str, str], None] = lambda _node, _relative: None
    #: Where the states written to each file are kept across a restart.
    states: PeerStates = field(default_factory=PeerStates)
    _joined: dict[str, _Joined] = field(default_factory=dict, init=False)
    #: The nodes whose latest answer said the drive does not hold the text.
    _unsaved: set[str] = field(default_factory=set, init=False)
    #: Each node's send whose answer never came (a refusal, a server gone
    #: mid-request, a lost connection), kept through the file leaving the
    #: peer: it may have landed, and is asked again under its id before the
    #: next send, so the state it made is known.
    _unanswered: dict[str, _Pending] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _inbox: dict[str, str] = field(default_factory=dict, init=False)
    _inbox_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False)

    # -- what the live sync asks -----------------------------------------

    # The reads below never wait for the folder's lock, which is held
    # across a request to the server: a caller on an event loop (the folder's
    # beat, a checkpoint push) must not stall behind one.

    def owns(self, node_id: str) -> bool:
        return node_id in self._joined

    def owned(self) -> dict[str, str]:
        """The joined files, ``relative -> node``."""
        return {joined.relative: node for node, joined in list(self._joined.items())}

    def settled(self) -> list[str]:
        """The joined files whose disk holds exactly what the document held
        when the peer last wrote or read it, and whose document the drive
        holds too: nothing need upload them."""
        return [
            joined.relative
            for node, joined in list(self._joined.items())
            if node not in self._unsaved
            and joined.stamp is not None
            and self._stamp(joined.relative) == joined.stamp
        ]

    def unsaved(self) -> list[str]:
        """The joined files whose document the drive does not hold: the
        ordinary uploads, the checkpoint push's among them, still send them."""
        return [
            joined.relative for node, joined in list(self._joined.items()) if node in self._unsaved
        ]

    def take(self, node_id: str, relative: str) -> Taken:
        """The agent saved ``node_id`` (at ``relative``), a file a live
        session writes: send it if held, join it if not, and say whether the
        drive will hold it. A closed peer takes everything: the next holder
        sends the file."""
        if self._closed:
            return Taken.LANDED
        taken = self.send(node_id) if self.owns(node_id) else self.join(node_id, relative)
        if not taken:
            self._unsaved.discard(node_id)
            return Taken.LEFT
        return Taken.UNSAVED if node_id in self._unsaved else Taken.LANDED

    def join(self, node_id: str, relative: str) -> bool:
        """Become ``node_id``'s peer when its live document is open (see the
        module docstring). ``False`` leaves the file to the ordinary path;
        ``True`` with the file not held means its send landed and the next
        save joins afresh."""
        if self._closed:
            return False
        with self._lock:
            if node_id in self._joined:
                self._joined[node_id].relative = relative
                return self.refresh(node_id)
            try:
                return self._join(node_id, relative)
            except PeerRefusedError as refused:
                self._leave(node_id, str(refused))
                return False

    def _join(self, node_id: str, relative: str) -> bool:
        with self._lock:
            state = self._heard(node_id, self.api.read(node_id))
            if state is None:
                return False
            read = self._read(relative)
            if read is None:
                return False
            text, stamp = read
            # Not held until it is settled: a half-joined file must never be
            # sent as made on the document's current state, which the disk
            # was not made on.
            joined = _Joined(
                relative=relative,
                token=state.token,
                text=text,
                stamp=None,
                used=0,
                keep=lambda states: self.states.save(node_id, states),
            )
            joined.descend = lambda: self.descend(joined.relative)
            # What a peer before a restart wrote to this file: the disk is one
            # of these, or one with the agent's edit on it.
            joined.seen.extend(self.states.load(node_id))
            self._touch(joined)
            agreed = self.agreed(relative)
            if text == state.text:
                joined.stand(state.token, state.text)
                joined.stamp = stamp
            elif joined.seen:
                if not (
                    any(kept == text for _token, kept in joined.seen)
                    and self._take(joined, state, stamp)
                ):
                    # The agent changed the file since a state this peer (or
                    # the one before a restart) wrote: sent as made on it.
                    read = self._read(relative)
                    if read is None:
                        return False
                    text, stamp = read
                    joined.text = text
                    sent = self._send(
                        node_id, joined, text, stamp, base_token=joined.base_for(text)
                    )
                    if sent is not True:
                        return sent is None
            elif not (
                agreed is not None
                and self._digest(relative) == agreed
                and self._take(joined, state, stamp)
            ):
                # The disk is not what the box and the drive agreed: the agent
                # changed it since. Made on the agreed version (the peer has
                # not written the file since), it goes up as made on exactly
                # that version. Otherwise it was made on a state this process
                # cannot name (one a peer before a restart wrote, or a write
                # back) and goes up as made on an unknown version: the server
                # merges it from the closest state it handed this box or wrote
                # back, adding what it adds and removing nothing. Searched
                # for, a disk whose agreed lines a person had typed into was
                # merged from a state holding the typing: the file's own lines
                # came back beside the typed ones, and lines a lost answer had
                # already merged landed twice.
                read = self._read(relative)
                if read is None:
                    return False
                text, stamp = read
                joined.text = text
                sent = self._send(
                    node_id,
                    joined,
                    text,
                    stamp,
                    base_token=None,
                    base_etag=self.agreed_etag(relative),
                )
                if sent is not True:
                    # Landed with no state to stand on (``None``): the file is
                    # not held, and nothing is uploaded either. Uploaded, it was
                    # merged from the closest version the session had, which
                    # could already hold the agent's next line: read as
                    # removed, the line was lost.
                    return sent is None
            self._joined[node_id] = joined
            logger.info("live peer: joined %s (%s)", relative, node_id)
        self.on_join(node_id, relative)
        return True

    def send(self, node_id: str) -> bool:
        """Send the agent's change to ``node_id`` on disk. ``True`` when the
        peer took it (nothing to upload); ``False`` when the file left the
        peer and goes the ordinary way. A closed peer (its folder was let go)
        takes everything and sends nothing: the next holder sends the file."""
        if self._closed:
            return True
        with self._lock:
            try:
                return self._send_disk(node_id)
            except PeerRefusedError as refused:
                self._leave(node_id, str(refused))
                return False

    def _send_disk(self, node_id: str) -> bool:
        with self._lock:
            joined = self._joined.get(node_id)
            if joined is None:
                return False
            read = self._read(joined.relative)
            if read is None:
                self._leave(node_id, "not text any more", forget=True)
                return False
            text, stamp = read
            self._touch(joined)
            if text == joined.text:
                joined.stamp = stamp
                return True
            sent = self._send(node_id, joined, text, stamp, base_token=joined.base_for(text))
            return sent is not False

    def refresh(self, node_id: str, token: str | None = None) -> bool:
        """The document moved (a notice naming ``token``, or a write back):
        read it and write it to disk. ``False`` when the file left the peer."""
        if self._closed:
            return False
        with self._lock:
            try:
                return self._refresh(node_id, token)
            except PeerRefusedError as refused:
                self._leave(node_id, str(refused))
                return False

    def _refresh(self, node_id: str, token: str | None) -> bool:
        with self._lock:
            joined = self._joined.get(node_id)
            if joined is None:
                return False
            self._touch(joined)
            if token is not None and token == joined.token:
                return True
            state = self._heard(node_id, self.api.read(node_id))
            if state is None:
                self._leave(node_id, "its live session closed", forget=True)
                return False
            if state.token == joined.token:
                return True
            read = self._read(joined.relative)
            if read is None:
                self._leave(node_id, "not text any more", forget=True)
                return False
            text, stamp = read
            if text != joined.text:
                # The agent wrote since the peer last did: its change first.
                sent = self._send(node_id, joined, text, stamp, base_token=joined.base_for(text))
                return sent is not False
            self._take(joined, state, stamp)
            return True

    def leave(self, node_id: str) -> None:
        """Let go of ``node_id``; what was kept for it stays for its next join."""
        with self._lock:
            self._joined.pop(node_id, None)

    def expire(self) -> list[str]:
        """Let go of the files nobody touched for :attr:`idle_seconds`."""
        now = self.clock()
        with self._lock:
            idle = [n for n, j in self._joined.items() if now - j.used >= self.idle_seconds]
            for node_id in idle:
                self._leave(node_id, "idle")
        return idle

    # -- notices from the machine channel ----------------------------------

    def notify(self, node_id: str, token: str) -> None:
        """The server says ``node_id``'s document is at ``token``. Safe from
        any thread and never blocks: taken on the peer's own thread."""
        if self._closed:
            return
        with self._inbox_lock:
            self._inbox[node_id] = token
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._take_notices, name="live-peer", daemon=True
                )
                self._thread.start()
        self._wake.set()

    def take_notices(self) -> int:
        """Act on every notice waiting; answers how many."""
        with self._inbox_lock:
            taken, self._inbox = self._inbox, {}
        for node_id, token in taken.items():
            try:
                if self.owns(node_id):
                    self.refresh(node_id, token)
                    continue
                relative = self.relative_of(node_id)
                if relative is not None:
                    self.join(node_id, relative)
            except Exception as failure:  # the next notice or write back tries again
                logger.warning(
                    "live peer: the notice for %s was not taken (%s: %s)",
                    node_id,
                    type(failure).__name__,
                    failure,
                )
        return len(taken)

    def close(self) -> None:
        """Stop: nothing more is read, written or sent as the folder's peer."""
        self._closed = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _take_notices(self) -> None:
        while not self._closed:
            self._wake.wait(NOTICE_IDLE_WAIT)
            self._wake.clear()
            if self._closed:
                return
            self.take_notices()
            self.expire()

    # -- the disk and the server ---------------------------------------------

    def _send(
        self,
        node_id: str,
        joined: _Joined,
        text: str,
        stamp: Stamp,
        *,
        base_token: str | None,
        base_etag: int | None = None,
    ) -> bool | None:
        """Send ``text`` (read at ``stamp``) as made on ``base_token`` (or, with
        none, on the drive version ``base_etag``). ``True``
        when ``joined`` now stands on the answer, ``False`` when the session is
        gone (the file left the peer), ``None`` when the send landed but the
        state it made is unknown (the file left the peer, nothing to upload)."""
        pending = self._unanswered.get(node_id)
        if pending is not None and not pending.same(base_token, base_etag, text):
            # A send whose answer never came (the server or the connection went
            # away) may have landed. It is asked again under its id first, so
            # the state it made is known: the agent's next save was made on
            # it, and sent as made on the state before, the earlier save's
            # lines landed twice.
            earlier = self._heard(
                node_id,
                self.api.submit(
                    node_id,
                    pending.text,
                    base_token=pending.base_token,
                    submit_id=pending.submit_id,
                    base_etag=pending.base_etag,
                ),
            )
            self._unanswered.pop(node_id, None)
            if earlier is None:
                self._leave(node_id, "its live session closed", forget=True)
                return False
            if earlier.at_token is None or earlier.at_text is not None:
                self._leave(node_id, "its last send's state is unknown")
                return None
            joined.stand(earlier.at_token, pending.text)
            # The disk was made on that state or one the peer wrote before
            # it: it is named now, never the drive version it moved past.
            base_token, base_etag = joined.base_for(text), None
        pending = self._unanswered.get(node_id)
        if pending is not None and pending.same(base_token, base_etag, text):
            submit_id = pending.submit_id
        else:
            submit_id = uuid.uuid4().hex
            self._unanswered[node_id] = _Pending(submit_id, base_token, base_etag, text)
        # Once sent, the drive version the disk was made on is merged into
        # the document: a later send naming it again would add its lines
        # twice, whether or not the answer arrives.
        self.descend(joined.relative)
        state = self._heard(
            node_id,
            self.api.submit(
                node_id, text, base_token=base_token, submit_id=submit_id, base_etag=base_etag
            ),
        )
        self._unanswered.pop(node_id, None)
        if state is None:
            self._leave(node_id, "its live session closed", forget=True)
            return False
        # What the disk held when it was read, which the answer is written
        # over, and the state holding exactly it: an agent that read the file
        # before the answer was written and saves after made its edit on it.
        joined.text = text
        exact = state.at_token is not None and state.at_text is None
        if exact and state.at_token is not None:
            joined.stand(state.at_token, text)
        if self._stamp(joined.relative) == stamp:
            if self._take(joined, state, stamp):
                return True
        # The agent saved again while the answer was on its way: the peer now
        # stands on the state holding exactly what it sent, and the next save
        # goes from there.
        if not exact or state.at_token is None:
            # A retry the server had merged once already (the state it made
            # unknown), or a send merged keeping everything into a state that
            # holds more than the disk did: no state is the disk's, and the
            # next save goes as made on an unknown version, which adds what
            # it adds and removes nothing.
            self._leave(node_id, "its last send's state is unknown")
            return None
        joined.stand(state.at_token, text)
        joined.stamp = None
        return True

    def _take(self, joined: _Joined, state: PeerState, stamp: Stamp) -> bool:
        """Write ``state`` to disk over a file still stamped ``stamp``.
        ``False`` (nothing written) when the disk moved since."""
        if state.text == joined.text and self._stamp(joined.relative) == stamp:
            joined.stand(state.token, state.text)
            joined.stamp = stamp
            return True
        joined.ahead(state.token, state.text)
        try:
            if not self.tree.write(joined.relative, state.text.encode("utf-8"), over=stamp):
                return False
        except OSError as failure:
            logger.warning(
                "live peer: %s could not be written (%s: %s)",
                joined.relative,
                type(failure).__name__,
                failure,
            )
            return False
        joined.stand(state.token, state.text)
        joined.stamp = self._stamp(joined.relative)
        return True

    def _read(self, relative: str) -> tuple[str, Stamp] | None:
        """The file's text and the stamp it was read at, or ``None`` when it
        is gone, not a regular file, not UTF-8 text, or too large. A file
        replaced while it was read (the agent saving) is read again: taken
        for not being text, it left the peer and its next save went the
        ordinary way."""
        for _ in range(READ_TRIES):
            before = self._stamp(relative)
            if before is None or before[0] > self.max_bytes:
                return None
            try:
                data = self.tree.read_bytes(relative)
            except OSError:
                return None
            if self._stamp(relative) == before:
                break
        else:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if "\x00" in text:
            return None
        return text, before

    def _stamp(self, relative: str) -> Stamp | None:
        info = self.tree.stat(relative)
        if info is None:
            return None
        return info.st_size, info.st_mtime_ns

    def _digest(self, relative: str) -> str | None:
        try:
            return self.tree.digest(relative)[0]
        except OSError:
            return None

    def _touch(self, joined: _Joined) -> None:
        joined.used = self.clock()

    def _heard(self, node_id: str, state: PeerState | None) -> PeerState | None:
        """Note whether ``state`` (an answer for ``node_id``) is saved."""
        if state is not None and state.saved:
            self._unsaved.discard(node_id)
        elif state is not None:
            self._unsaved.add(node_id)
            logger.info("live peer: the drive does not hold %s's document", node_id)
        return state

    def _leave(self, node_id: str, why: str, *, forget: bool = False) -> None:
        """Let go of ``node_id``. What was kept for it stays (an idle file
        rejoined with the agent's edit on it names the state it was made on)
        unless ``forget``: its session closed, whose states no later session
        knows, or it stopped being text."""
        joined = self._joined.pop(node_id, None)
        if forget:
            # A session that closed answers nothing more about its sends.
            self._unanswered.pop(node_id, None)
        if joined is not None:
            if forget:
                self.states.drop(node_id)
            logger.info("live peer: left %s (%s)", joined.relative, why)
