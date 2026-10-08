"""The chat folder a box holds for as long as the chat is awake.

A web chat's durable state is a folder in the org drive, and a box that is
running the chat is the folder's single writer. That is the same claim a
desktop mount makes, so it is taken the same way and by the same code: the
mount chain in :mod:`alkera_cli.files.mount` — a lease, a pull, a heartbeat,
and a push-then-release on the way out. Nothing here re-implements any of it;
this module is only *which* folder, *where* it lands locally, and *when* the
box takes it and gives it back.

Sleep is the release. A chat that has gone quiet has its mirror closed, and
closing it pushes the folder up and hands the lease back — so the bytes are
safe and the next box (this one or another) can take the folder and carry on.
Resume is the acquire: a re-opened mirror takes the lease again under the same
instance id and pulls the folder down. A lease still held by a box that died is
handed over by the server's own reaper once the TTL lapses, which is what makes
"resume anywhere" true rather than "resume on the box that last had it".

Which folder it is comes off the chat record: the Files node the chat IS, when
the record names one, and the drive that node is on. Only a record that names
no node falls back to the place the Files side conventionally puts it,
``Chats/<chat id>`` unless the record says. The drive is never asked for as
"the caller's": a pool box serves chats from many orgs, each in its own drive,
and a box on its own machine credential is a member of no org and has no drive
of its own — asked for the caller's drive, the server answers that there is
none. Only a record that names no drive is read through the caller's own,
which an org box on its operator's session still has. A box whose backend has
no such folder yet keeps no custody at all — it logs the reason once and serves
the chat exactly as it did before — because a chat that cannot be served is a
worse outcome than a chat whose scratch is not yet durable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Final

import httpx
from alkera_core.authz import agent_headers
from alkera_core.chat_paths import chat_path
from alkera_core.chat_records import TRACE_FILES
from alkera_core.project import prune_stale_locks
from alkera_core.project.chats.trace import pin_trace_if_changed
from alkera_sdk.client import AlkeraHTTPError, files_namespace

from alkera_cli.cloud.box_files import box_files_clients
from alkera_cli.cloud.custody_layout import CHAT_FOLDER_PURPOSE, CustodyLayout
from alkera_cli.cloud.custody_layout import PULL_LOCAL as _PULL_LOCAL
from alkera_cli.cloud.custody_layout import PUSH_LOCAL as _PUSH_LOCAL
from alkera_cli.cloud.faults import passing_fault
from alkera_cli.cloud.folder_paths import (
    CHAT_FOLDER_ROOT,
    CHAT_FOLDER_SUFFIX,
    RECOVERY_NAME_ATTEMPTS,
    RECOVERY_ROOT,
    chat_folder_drive,
    chat_folder_node,
    chat_folder_path,
    home_path,
    occupied,
    recovery_path,
    recovery_stem,
    shown_in_home,
    wire_path,
)
from alkera_cli.cloud.folder_prune import prune_deleted
from alkera_cli.cloud.folder_wipe import wipe_copy
from alkera_cli.cloud.left_behind import LeftBehind
from alkera_cli.cloud.limits import handback_attempts
from alkera_cli.cloud.working_step import working_inside
from alkera_cli.cloud.workspace_seat import is_workspace_key
from alkera_cli.files.journal import LiveJournal, journal_path
from alkera_cli.files.live_sync import (
    LiveCadence,
    LiveSync,
    RestLiveApi,
    TreeWatcher,
)
from alkera_cli.files.mount import (
    LeaseSupersededError,
    MountRecord,
    NotMountedError,
    SelfFence,
    export,
    fenced_client,
    fenced_files,
    heartbeat,
    load_record,
    load_records,
    mount,
    remove_record,
    save_record,
    superseded_from,
    unmount,
)
from alkera_cli.files.mount import release as release_unpushed
from alkera_cli.files.nodemap import (
    NodeMapStore,
    agreed_bases,
    known_for_pull,
    load_node_map,
    remember_pull,
    under_working_dir,
)
from alkera_cli.files.pull import PullSummary
from alkera_cli.files.push import AgreedBase, FilesApi, PushSummary
from alkera_cli.harness.opencode_db import fold_agent_databases
from alkera_cli.plugins.plugin_base.delivery import (
    register_conflict_notices,
    unregister_conflict_notices,
)

logger = logging.getLogger(__name__)
ON_LAPSED_LEASE: Final = "keep_unseen"  # a lapsed re-take keeps the box's unsent work

#: The drive's answer to taking a folder that is in the trash. The folder of a
#: deleted chat is trashed with it, so to a box re-taking a fenced folder this
#: is "the chat is gone" — the one refusal of a re-take it does not wait out.
FOLDER_TRASHED = "files.trashed"
#: The drive's answer to re-taking a lease the SERVER ended: the chat was put
#: to sleep, deleted or moved while this box held it. The chat is over here.
LEASE_ENDED = "files.lease_ended"
#: The re-take refusals that end the chat on this box rather than being waited
#: out: the folder cannot be held again by anyone, or not by this box.
_CHAT_OVER = frozenset({FOLDER_TRASHED, LEASE_ENDED})


#: One log as the last pin saw it on disk: its length and its modified time.
_LogStamp = tuple[str, int, int]


def _log_stamps(root: Path) -> tuple[_LogStamp, ...]:
    """The chat's logs as they stand, cheaply: what a pin would have to hash."""
    stamps: list[_LogStamp] = []
    for name in TRACE_FILES:
        try:
            status = (root / name).stat()
        except OSError:
            continue
        stamps.append((name, status.st_size, status.st_mtime_ns))
    return tuple(stamps)


class _RecordPins:
    """Pin a chat's logs before its folder leaves the box.

    The copy on the drive then always carries a digest covering the logs it
    travelled with, so the next box can check the transcript it reads a gated
    call back from against what this box left — a defence for the day the
    drive's own rule on a chat's records is not enough.

    The push comes round every few seconds, so a chat whose logs have not
    moved since the last pin is not re-hashed: the stamps are compared first,
    and only a changed log pays for the read. A folder holding no log at all
    — a mount that is not a chat, a chat before its first turn — has nothing
    to pin and gets no digest. A pin that cannot be written never holds a
    push back; the logs still travel, unpinned, and the next box's check
    answers "unpinned" rather than refusing.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[_LogStamp, ...]] = {}

    def pin(self, chat_id: str, root: Path) -> None:
        stamps = _log_stamps(root)
        if not stamps or self._seen.get(chat_id) == stamps:
            return
        try:
            pin_trace_if_changed(root, chat_id)
        except OSError:
            logger.warning(
                "chat %s: its trace digest could not be pinned before the push",
                chat_id,
                exc_info=True,
            )
            return
        self._seen[chat_id] = stamps

    def forget(self, chat_id: str) -> None:
        self._seen.pop(chat_id, None)


def _live_keepalive(chat_id: str, sync: LiveSync) -> None:
    """Let the plane say it is alive when nothing else has for a while.

    Never a reason to drop the beat: the beat landed, and a keepalive that
    did not is a line in the log and another try on the next one.
    """
    try:
        sync.keepalive()
    except LeaseSupersededError:
        logger.warning(
            "chat %s: the live plane's keepalive was refused as somebody else's write; "
            "the next beat decides whether the folder is still ours",
            chat_id,
        )
    except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
        logger.info("chat %s: the live plane's keepalive did not land (%s)", chat_id, exc)


__all__ = [
    "CHAT_FOLDER_PURPOSE",
    "CHAT_FOLDER_ROOT",
    "CHAT_FOLDER_SUFFIX",
    "RECOVERY_NAME_ATTEMPTS",
    "RECOVERY_ROOT",
    "ChatFolders",
    "FolderBusyError",
    "FolderHandBackError",
    "HeldFolder",
    "ReleasedFolder",
    "chat_folder_drive",
    "chat_folder_node",
    "chat_folder_path",
    "recovery_path",
    "recovery_stem",
    "shown_in_home",
    "wire_path",
]


def _bases(held: HeldFolder, home: Path | None) -> dict[str, AgreedBase]:
    """The bases a checkpoint push of ``held`` fences on, read once the live sync
    wrote down every save it agreed (its debounced map can trail the drive)."""
    live = held.live
    if live is not None:
        live.remember_nodes(now=True)
    inside = None if live is None else working_inside(held.root, live.root)
    return agreed_bases(load_node_map(held.root, home=home), inside)


class _Gone(Enum):
    """Whether the folder a box leased is still where work can be landed."""

    HERE = "here"
    TRASHED = "trashed"
    PURGED = "purged"


class FolderHandBackError(Exception):
    """A chat's folder could not be handed back on this pass.

    ``transient`` separates "the API was restarting" from "the server refused
    this": the first is waited out for as long as it takes, the second is
    retried a bounded number of times and then said out loud on the chat,
    because a box that retries a 403 forever is a box whose working tree never
    reaches the drive and never tells anybody.
    """

    def __init__(
        self,
        chat_id: str,
        reason: str,
        *,
        transient: bool,
        attempts: int,
        exhausted: bool,
    ) -> None:
        super().__init__(f"chat {chat_id}'s folder was not handed back: {reason}")
        self.chat_id = chat_id
        self.reason = reason
        self.transient = transient
        self.attempts = attempts
        self.exhausted = exhausted


class FolderBusyError(Exception):
    """Another box is already holding this chat's folder.

    Raised rather than swallowed: two boxes writing one chat folder is the one
    outcome the lease exists to rule out, so the caller must decide not to run
    the chat rather than run it over somebody else's writes.
    """

    def __init__(self, chat_id: str, *, holder: str | None = None) -> None:
        super().__init__(
            f"chat {chat_id}'s folder is held by another machine"
            + (f" ({holder})" if holder else "")
        )
        self.chat_id = chat_id
        self.holder = holder


@dataclass(frozen=True, slots=True)
class HeldFolder:
    """One chat folder this box currently owns the writes to."""

    chat_id: str
    org_path: str
    root: Path
    record: MountRecord
    #: What taking the folder brought down, for the take that did the pull;
    #: ``None`` on a handle re-issued by a beat or a rename.
    pull: PullSummary | None = None
    #: This folder's own fenced client: every write for this chat carries this
    #: lease's epoch and instance for as long as the box holds it. Per folder
    #: rather than per box, because a box holds several chats at once and a
    #: fence set on the shared client would sign one chat's writes with
    #: another's lease. ``None`` only on a handle a test built by hand.
    client: httpx.Client | None = None
    #: This folder's live sync, once the chat's working directory is known.
    #: ``None`` before the mirror names one, on a lease the server granted
    #: without the live plane, and on a box whose backend does not serve it —
    #: in every one of those the chat still runs, on the checkpoint push alone.
    live: LiveSync | None = None


class _SystemClock:
    """The clock a live sync measures its own cadence on.

    Monotonic only: a wall clock moved backwards would hand a holder that has
    stopped hearing from the server more time to write, not less.
    """

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


_MONOTONIC = _SystemClock()


@dataclass(frozen=True, slots=True)
class _LiveRun:
    """One folder's live sync and the handle that stops it."""

    sync: LiveSync
    stop: threading.Event
    thread: threading.Thread


@dataclass(frozen=True, slots=True)
class ReleasedFolder:
    """What handing one chat folder back moved."""

    chat_id: str
    org_path: str
    push: PushSummary
    #: Set when the chat's own folder was gone and the work was landed in the
    #: owner's recovery folder instead. ``org_path`` is then that folder.
    recovered: bool = False
    #: ``org_path`` as the owner reads it, for a message said to them.
    shown_path: str = ""
    #: Set when the folder was gone and nothing was landed anywhere, because
    #: the chat it belongs to is gone too. Only the lease went back.
    discarded: bool = False
    #: Set when the drive answered nothing for the folder and the chat could
    #: not be confirmed gone: nothing was landed, the lease record went, and
    #: the working tree was KEPT on this box's disk rather than wiped. A
    #: not-found is what a box that lost its authority over a folder is told
    #: as much as what a purged node is, so it never destroys the only copy.
    kept: bool = False


class _LeaseKeeper:
    """Beats one lease on a thread of its own, the first beat at once, until
    stopped or until a beat says the lease is not ours. :meth:`stop` returns
    only once no beat is in flight, so whatever the caller does next — a
    release — never has a beat land on top of it."""

    def __init__(self, beat: Callable[[], bool], *, every: float, name: str) -> None:
        self._beat = beat
        self._every = max(every, 0.01)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                if not self._beat():
                    return
            except Exception:
                logger.exception("a beat during the hand-back failed")
            if self._stopped.wait(self._every):
                return


class ChatFolders:
    """Custody of the chat folders one box holds.

    One instance per box. Every method is synchronous because the mount chain
    is: the service calls them off the event loop.
    """

    def __init__(
        self,
        *,
        chats_root: Path,
        files: FilesApi | None = None,
        http: httpx.Client | None = None,
        beat_http: httpx.Client | None = None,
        machine_id: str | None = None,
        home: Path | None = None,
        watcher_factory: Callable[[Path, LiveCadence, threading.Event], Any] | None = None,
    ) -> None:
        self._layout = CustodyLayout.beside(chats_root)
        self._watcher_factory = watcher_factory or self._default_watcher
        #: The live syncs running right now, one per held folder.
        self._live: dict[str, _LiveRun] = {}
        #: The live syncs being stopped right now, draining what they hold. The
        #: hand-back's own beats still open their fence: the drain sends only
        #: while the fence is open, and a drain no beat could reach waited out
        #: its whole window with every change withheld.
        self._stopping_live: dict[str, _LiveRun] = {}
        self._files = files
        self._http = http
        #: The lease beats' own client (``None``: they speak on ``http``).
        self._beat_http = beat_http
        self._machine_id = machine_id
        self._home = home
        self._held: dict[str, HeldFolder] = {}
        #: Chats whose hand-back was refused and whose folder this box therefore
        #: still holds. The lease was never given back, so the entry stays in
        #: `_held` and a later pass tries again.
        self._owed: set[str] = set()
        #: How many times a hand-back has been refused for a reason that will
        #: not change on its own, per chat.
        self._refusals: dict[str, int] = {}
        #: Chats whose folder this box could not take for a reason that will not
        #: change on the next poll — said once, per chat, rather than per tick.
        self._unavailable: dict[str, str] = {}
        #: Chats whose lease lapsed with nobody else holding it. The folder is
        #: still this box's to re-take, so it stays in ``_held`` and every beat
        #: tries again; the set is only so the lapse and the recovery are each
        #: said once instead of on every beat for as long as it lasts.
        self._lapsed: set[str] = set()
        #: The server answered the batched beat 404: it predates the route, so
        #: every later pass beats each folder on its own.
        self._batch_beat_refused = False
        #: Chats whose folder is no longer in the drive to push into, by what
        #: became of it. The checkpoint push runs every few seconds and the
        #: verdict cannot change until the folder is restored or the chat is
        #: handed back, so it is said once instead of on every beat.
        self._gone: dict[str, _Gone] = {}
        #: Folders this box stopped holding on its own while they were served
        #: (:meth:`dropped`); a take, a hand-back or :meth:`settle_drop` clears.
        self._dropped: set[str] = set()
        self._left_behind = LeftBehind()
        self._pins = _RecordPins()
        #: One folder-moving call at a time PER CHAT. Each held folder carries
        #: its own fenced client, so two chats can be on the wire at once
        #: without signing each other's writes; what still has to be serialized
        #: is one chat against itself — a push that overlapped its own hand-back
        #: would race the release. A box holds several chats, and a shared lock
        #: made one chat's stalled push everybody's stall: the beat behind it
        #: could not land and every other chat lost the folder it was running.
        self._locks: dict[str, threading.Lock] = {}
        #: Guards the lock table itself, not any I/O: held only long enough to
        #: hand out a chat's lock, so it is never behind a request.
        self._lock_table = threading.Lock()
        if machine_id:
            self.bind_machine(machine_id)

    @classmethod
    def for_box(
        cls,
        *,
        api_url: str,
        auth: httpx.Auth,
        chats_root: Path,
        home: Path | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> ChatFolders:
        """The custody a provisioned box runs with, every request signed by the
        box's credential through ``auth`` (:mod:`alkera_cli.cloud.box_files`).
        Nothing connects until the first chat is taken, and every call speaks
        as the machine from :meth:`bind_machine` on."""
        clients = box_files_clients(api_url=api_url, auth=auth, timeout=timeout)
        return cls(
            chats_root=chats_root,
            files=clients.files,
            http=clients.http,
            beat_http=clients.beats,
            home=home,
        )

    def bind_machine(self, machine_id: str) -> None:
        """The box registered: every Files call from here on speaks as that
        machine — the identity the chat's routes admit a box under, and the
        badge the lease shows whoever is refused."""
        self._machine_id = machine_id
        for client in (self._http, self._beat_http):
            if client is not None:
                client.headers.update(agent_headers(machine_id))

    @property
    def enabled(self) -> bool:
        """Whether this box can hold chat folders at all.

        A box built without a Files client is one running against a backend
        where the folder does not exist yet: every method is a no-op and the
        chat is served exactly as it was before.
        """
        return self._files is not None and self._http is not None

    def local_root(self, chat_id: str) -> Path:
        """Where the chat's folder lands on this box.

        The chat's own directory under ``.alkera/chats``: the sandbox the fence
        already carves out is inside it, so pulling the folder here is what
        makes that scratch durable instead of local to one box.
        """
        return self._layout.root(chat_id)

    def held(self, chat_id: str) -> HeldFolder | None:
        return self._held.get(chat_id)

    def held_by_lease_node(self, lease_node_id: str) -> HeldFolder | None:
        """The folder this box holds under the lease on ``lease_node_id``.

        The drive names a folder by the node its lease is on, never by the
        chat: a request from the drive for a file under a lease is answered
        through here, and a lease this box does not hold is ``None``.
        """
        with self._lock_table:
            for held in self._held.values():
                if held.record.node_id == lease_node_id:
                    return held
        return None

    def machine(self, chat_id: str) -> str:
        """The machine id this box leases under, or the chat id before it has
        registered — the same stand-in a receipt uses."""
        return self._machine_id or chat_id

    # -- the three verbs -------------------------------------------------------

    def take(self, chat_id: str, chat: Mapping[str, Any], *, instance: str) -> HeldFolder | None:
        """Take the chat's folder and pull it down, or answer ``None``.

        ``None`` means "this box keeps no custody of this chat" — the folder is
        not there, or the backend does not serve it — which is a chat served
        without durable scratch, not a chat refused. A folder somebody else is
        holding is different and raises :class:`FolderBusyError`: running the chat
        anyway would write over another box's turn. A drive that is only
        briefly unavailable (a wire error, a 5xx, a 429) raises too: custody
        is never given up on an answer the next attempt may not repeat.

        Which folder is decided by the node id the chat record carries, and only
        by its path when the record carries no id.
        """
        if not self.enabled:
            return None
        with self._lock(chat_id):
            # Already held: a second caller that queued behind the take, or a
            # folder whose hand-back did not land. Served again, the hand-back
            # is no longer owed: a retry would push, release and wipe the tree
            # the chat now runs in. Under the lock, so a hand-back in flight
            # finishes first and this take then starts afresh.
            already = self._held.get(chat_id)
            if already is not None:
                self._owed.discard(chat_id)
                self._refusals.pop(chat_id, None)
                return already
            return self._take_locked(chat_id, chat, instance=instance)

    def _take_locked(
        self, chat_id: str, chat: Mapping[str, Any], *, instance: str
    ) -> HeldFolder | None:
        org_path = chat_folder_path(chat, chat_id)
        # The record names the node the chat IS; the path is only what that node
        # is called. A box that leased by name would take whatever folder is
        # filed under `Chats/<chat id>` at this instant — which after a rename,
        # a restore, or a second chat filed there is somebody else's bytes.
        node_id = chat_folder_node(chat)
        # The record names the drive too. Read off it rather than asked for as
        # the caller's own: this box may be a member of no org at all.
        drive_id = chat_folder_drive(chat)
        root = self.local_root(chat_id)
        root.mkdir(parents=True, exist_ok=True)
        try:
            if node_id is not None:
                # The push and the release address the folder by its path, so
                # the path recorded has to be where the node really is.
                org_path = self._path_of(node_id, drive_id=drive_id) or org_path
            # The take IS the pull: before the first turn runs here, every file
            # and folder the chat's Files node holds is on this box's disk,
            # byte for byte, under the fence the lease just granted (the box may
            # read its own chat folder; nobody may download a chat). What is
            # already here with the server's bytes is left alone unfetched.
            #
            # Conflicts, and what the pull never does. A FRESH box (no record on disk) takes Files
            # as the truth. A box that already holds the folder AND the lease (a record at the
            # granted epoch) keeps its local edits — it was the only writer, so a divergent byte
            # here is a turn the cloud has not seen yet — and pushes them on the next push. A box
            # whose lease lapsed keeps a local edit where the drive still has the agreed bytes (its
            # own unsent work); where both moved, Files wins. And the pull REMOVES NOTHING: a local
            # file Files lacks is either one this box wrote and has not pushed (deleting it would
            # drop the only copy) or one pushed and later removed in Files, which the union push
            # restores; absence on the server is never a deletion.
            #
            # A file is recognised by the node it IS before any of that: what an earlier process on
            # this box knew about each file's node (the node map beside the record) lets the pull
            # move a file the drive renamed meanwhile to its new name, and remove one the drive
            # trashed, instead of fetching the new name and pushing the old as a second file. Only
            # a file no holder knew a node for is left to the union push.
            remembered = load_node_map(root, home=self._home)
            # Said before the pull, not only after it: a take walks every
            # folder under the chat on the drive, and one that fails part-way
            # has paid for that walk with nothing else in the log to show it.
            logger.info("chat %s: taking its folder", chat_id)
            record, pulled = mount(
                files=self._require_files(),
                http=self._require_http(),
                source=org_path,
                node_id=node_id,
                drive_id=drive_id,
                root=root,
                machine=self.machine(chat_id),
                home=self._home,
                purpose=self._layout.purpose(chat_id),
                instance=instance,
                inbound=True,
                live=True,
                on_lapsed=ON_LAPSED_LEASE,
                pull_tree=self._layout.pull(chat_id, _PULL_LOCAL),
                known=known_for_pull(remembered),
            )
            remember_pull(root, remembered, pulled, home=self._home)
        except LeaseSupersededError as busy:
            raise FolderBusyError(chat_id, holder=busy.holder) from busy
        except AlkeraHTTPError as refused:
            if passing_fault(refused):
                # A 5xx or a 429 is the drive being briefly unavailable (a
                # Postgres restart answers 503), not an answer about the
                # folder. Read as "not served" it let the chat run with no
                # lease, no stream and no push, so everything its agent wrote
                # stayed on this box with nothing to say so. Raised instead:
                # the caller does not serve the chat and takes it again.
                raise
            self._note_unavailable(chat_id, org_path, refused)
            return None
        # The lock this chat's last life left behind was rotated aside for
        # forensics by whichever process reclaimed it (it names a dead pid by
        # then). Nothing else ever removes one, and the folder is taken again
        # every time the chat wakes, so without this the directory grows one
        # per take for as long as the chat exists.
        pruned = prune_stale_locks(root)
        logger.info(
            "chat %s: pulled %d file(s), %d unchanged, %d byte(s), %d folder(s) listed%s",
            chat_id,
            pulled.files,
            pulled.unchanged,
            pulled.bytes_downloaded,
            pulled.folders + 1,
            f"; pruned {pruned} stale lock file(s)" if pruned else "",
        )
        for warning in pulled.warnings:
            # Something under the folder stayed on the server: the box was
            # refused it. Said per take, because the chat then runs without it.
            logger.warning("chat %s: %s", chat_id, warning)
        held = HeldFolder(
            chat_id=chat_id,
            org_path=record.org_path,
            root=root,
            record=record,
            pull=pulled,
            client=fenced_client(self._require_http, record),
        )
        self._held[chat_id] = held
        self._dropped.discard(chat_id)
        self._unavailable.pop(chat_id, None)
        self._owed.discard(chat_id)
        self._refusals.pop(chat_id, None)
        return held

    def left_behind(self) -> dict[str, MountRecord]:
        """The folders an earlier life of this box left held and this one does
        not hold, by custody key: a restart in place keeps its leases for the
        next process, which takes back only the folders of the chats it serves."""
        found: dict[str, MountRecord] = {}
        for record in load_records(home=self._home):
            key = self._layout.key_of(Path(record.local_root))
            if key is not None and key not in self._held:
                found[key] = record
        return found

    def settle(self, key: str, record: MountRecord) -> ReleasedFolder | None:
        """Take a folder an earlier life of this box left held, then hand it
        back: what is on this disk is pushed and the lease goes. ``None`` when a
        chat took the folder in between, which then serves it as its own."""
        with self._lock(key):
            if key in self._held:
                return None
            row = {"files_node_id": record.node_id, "files_drive_id": record.drive_id}
            if self._take_locked(key, row, instance=record.instance_id) is None:
                return None
            self._owed.add(key)
        return self.hand_back(key, if_owed=True)

    def beat(self, chat_id: str) -> bool:
        """Keep the folder while the chat is awake. False once it is not ours.

        A lost lease is not raised at the caller: the box has already lost the
        folder, and the useful thing is that it stops believing it holds one.

        "Not ours" means somebody else holds it — never merely that a beat did
        not land in time. A beat that never reached the server is not a lease
        that is gone: the beats go on, the live plane's own fence closes on its
        own while none of them lands (so nothing is written under a lease that
        may have moved) and re-opens on the first one that does. A turn is
        answering into that folder, and sixty seconds of an unreachable API is
        not a reason to throw its answer away.

        A beat the server REFUSES is a different thing, and the refusal cannot
        be read on its face: the heartbeat route has exactly one refusal and it
        names nobody, so "fenced with no holder" covers both the lease that
        merely lapsed and the one another box acquired, the reaper reaped, or a
        force-release completed. The folder is asked for again instead — the
        acquire is granted when it is genuinely unclaimed and refused naming
        the holder when it is not, which is the only answer that tells those
        apart.
        """
        held = self._held.get(chat_id)
        if held is None or not self.enabled:
            return False
        return self._settle_beat(chat_id, held, self._ask_beat(held))

    def _ask_beat(self, held: HeldFolder) -> MountRecord | Exception:
        """One heartbeat, answered or refused. No lock and the beat client on
        purpose: it is the one call that must not wait — queued behind a push
        or a transfer, it lands after the lease it was keeping has lapsed."""
        try:
            return heartbeat(http=self._beats(), record=held.record, home=self._home)
        except (LeaseSupersededError, httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
            return exc

    def beat_all(self, chat_ids: list[str]) -> dict[str, bool] | None:
        """Keep every named folder in one call per drive. ``None`` when the
        server does not serve the batched beat, so the caller beats each folder
        on its own the way an older server expects.

        A box holds one lease per chat, and a beat per lease was most of what an
        idle box asked the API — growing with every chat it ran. Each verdict is
        answered exactly as the per-lease beat's answer would have been:
        ``renewed`` is the grant that beat returns, ``superseded`` its fence
        refusal, and ``gone`` its 404, which that beat never read as the lease
        being lost either. A batch that did not land is a beat that did not
        land for every folder in it.
        """
        if not self.enabled or self._batch_beat_refused:
            return None
        by_drive: dict[str, list[tuple[str, HeldFolder]]] = {}
        for chat_id in chat_ids:
            held = self._held.get(chat_id)
            if held is not None:
                by_drive.setdefault(held.record.drive_id, []).append((chat_id, held))
        kept: dict[str, bool] = {chat_id: False for chat_id in chat_ids}
        for drive_id, folders in by_drive.items():
            outcomes: dict[str, MountRecord | Exception] = {}
            try:
                verdicts = files_namespace(self._beats()).heartbeat_leases(
                    drive_id,
                    [
                        {
                            "nodeId": held.record.node_id,
                            "epoch": held.record.epoch,
                            "instanceId": held.record.instance_id,
                            # What the plane's empty keepalive batch said, on
                            # the beat instead of in a request per folder.
                            "synced": self._plane_running(chat_id),
                        }
                        for chat_id, held in folders
                    ],
                )
            except AlkeraHTTPError as exc:
                if exc.status == 404:
                    self._batch_beat_refused = True
                    return None
                outcomes = {chat_id: exc for chat_id, _ in folders}
            except (httpx.HTTPError, OSError) as exc:
                outcomes = {chat_id: exc for chat_id, _ in folders}
            else:
                answered = {str(v.get("nodeId")): v for v in verdicts}
                for chat_id, held in folders:
                    outcomes[chat_id] = self._verdict_outcome(
                        held, answered.get(held.record.node_id)
                    )
            for chat_id, held in folders:
                if self._held.get(chat_id) is not held:
                    # Handed back or re-taken while the batch was on the wire:
                    # its verdict is about a lease this box no longer writes under.
                    kept[chat_id] = self._held.get(chat_id) is not None
                    continue
                kept[chat_id] = self._settle_beat(chat_id, held, outcomes[chat_id], keepalive=False)
        return kept

    def fenced(self, key: str) -> bool:
        """Whether nothing here may write ``key``'s folder: its live sync's own
        fence closed, so its lease is in doubt (``files.folder_fence``)."""
        run = self._live.get(key)
        return run is not None and run.sync.fence.expired()

    def _plane_running(self, chat_id: str) -> bool:
        """Whether this chat's live sync runs behind an open fence (its keepalive's condition)."""
        return chat_id in self._live and not self.fenced(chat_id)

    def _verdict_outcome(
        self, held: HeldFolder, verdict: Mapping[str, Any] | None
    ) -> MountRecord | Exception:
        """One batched verdict as the per-lease beat would have answered it."""
        said = None if verdict is None else verdict.get("verdict")
        grant = None if verdict is None else verdict.get("grant")
        if said == "renewed" and isinstance(grant, Mapping):
            beaten = held.record.model_copy(
                update={
                    "epoch": int(grant.get("epoch", held.record.epoch)),
                    "expires_at": str(grant.get("expiresAt", held.record.expires_at)),
                }
            )
            try:
                save_record(beaten, home=self._home)
            except OSError as exc:
                return exc
            return beaten
        if said == "superseded":
            return LeaseSupersededError("a newer epoch owns this subtree")
        return AlkeraHTTPError(
            label="files/leases-heartbeat",
            status=404,
            code="not_found",
            message=f"the batched beat answered {said!r} for this folder",
            trace_id=None,
        )

    def _settle_beat(
        self,
        chat_id: str,
        held: HeldFolder,
        outcome: MountRecord | Exception,
        *,
        keepalive: bool = True,
    ) -> bool:
        """Act on what one beat came to: the renewed record, or what refused it.

        ``keepalive`` False is a beat that already told the drive the plane is
        running, so the plane does not say it again in a request of its own."""
        if isinstance(outcome, LeaseSupersededError):
            holder = outcome.holder
            regranted: MountRecord | None = None
            if holder is None:
                try:
                    regranted = self._retake(held)
                except LeaseSupersededError as taken:
                    holder = taken.holder or "another holder"
                except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
                    if isinstance(exc, AlkeraHTTPError) and exc.code in _CHAT_OVER:
                        # The folder is in the trash, or the server ended the
                        # chat's service (asleep, deleted, moved) under this
                        # box. That is an answer, not a wire error to wait out:
                        # the chat is let go, and nothing is pushed into a
                        # folder that is no longer this box's to write.
                        logger.warning(
                            "chat %s: its folder is not this box's any more (%s); the chat is "
                            "not served here",
                            chat_id,
                            exc.code,
                        )
                        self._stop_holding(chat_id)
                        return False
                    # The question could not be put. The folder is not KNOWN to
                    # be gone, so it is not given up on a wire error; the next
                    # beat asks again, and the live plane's fence keeps every
                    # write off it meanwhile.
                    logger.info(
                        "chat %s: a fenced beat could not be answered with a re-take (%s)",
                        chat_id,
                        exc,
                    )
                    return True
            if regranted is None:
                logger.warning(
                    "chat %s: its folder is held by %s now; it is not written again here",
                    chat_id,
                    holder,
                )
                self._stop_holding(chat_id)
                return False
            if chat_id not in self._lapsed:
                self._lapsed.add(chat_id)
                logger.warning(
                    "chat %s: its folder lease was fenced and nobody else had taken it; "
                    "the box holds it again at epoch %d",
                    chat_id,
                    regranted.epoch,
                )
            beaten = regranted
        elif isinstance(outcome, MountRecord):
            beaten = outcome
        else:
            # A beat that did not land is not a lease that is gone: the TTL
            # holds for several more, and the next tick tries again.
            logger.info("chat %s: a folder heartbeat did not land (%s)", chat_id, outcome)
            return True
        if chat_id in self._lapsed:
            self._lapsed.discard(chat_id)
            logger.info("chat %s: its folder lease is this box's again", chat_id)
        # The epoch a beat answers with is normally the one we already write
        # under, but a lease re-granted under this holder moves it — and the
        # fence rides on this folder's client, so a client left on the old
        # epoch would sign every later write with a number the server has
        # retired. Re-minted only when it actually moved.
        moved = beaten.epoch != held.record.epoch
        client = fenced_client(self._require_http, beaten) if moved else None
        amended = self._amend(
            chat_id,
            lambda now: replace(
                now,
                record=now.record.model_copy(
                    update={"epoch": beaten.epoch, "expires_at": beaten.expires_at}
                ),
                client=client if client is not None else now.client,
            ),
        )
        if amended is None:
            # Handed back or taken away mid-beat: not this box's to keep.
            return False
        run = self._live.get(chat_id)
        if run is not None:
            # A beat landed, so the live sync may write on: only it can know the
            # silence ended. Beaten off the run table, not the handle: a writer
            # racing this beat once re-issued the handle without the sync, and
            # the fence closed for good with nothing in the log.
            run.sync.fence.beat()
            if moved:
                self._rebind(run.sync, amended)
            if keepalive:
                _live_keepalive(chat_id, run.sync)
        return True

    def let_go(self, chat_id: str) -> None:
        """Stop holding the chat's folder without pushing or releasing: the
        server ended the chat's service, and its lease with it."""
        self._stop_holding(chat_id)

    def _stop_holding(self, chat_id: str) -> None:
        """Stop believing this box holds the chat's folder.

        Nothing is released: the lease is already somebody else's, or gone
        with the folder. The sync was streaming under it, and left running it
        would watch the directory for the life of the box behind a fence that
        never opens again — and the next take would start a second one beside
        it.
        """
        if self._held.pop(chat_id, None) is not None:
            self._dropped.add(chat_id)
        self._lapsed.discard(chat_id)
        self.stop_live(chat_id, deadline=0.0)

    def dropped(self, chat_id: str) -> bool:
        """Whether the box let go of a served folder: stop serving from it."""
        return chat_id in self._dropped

    def settle_drop(self, chat_id: str) -> None:
        self._dropped.discard(chat_id)

    def _retake(self, held: HeldFolder) -> MountRecord:
        """Ask for the folder again, the way a take asks for it.

        The same acquire the mount chain makes, under the same instance — so a
        lease the server still considers this box's is handed straight back at
        a fresh epoch, and one that has moved on is refused with the holder
        named. Raises :class:`LeaseSupersededError` for that refusal; the
        record it answers with is the folder's new epoch.

        No pull: the box already has the bytes, its own writes since the last
        push among them, and re-pulling here would take the server's copy as
        the truth over a turn's answer that has not been pushed yet.
        """
        record = held.record
        # The acquire answers 428 without the folder's etag, so a re-take that
        # sent none was refused every time and the lapsed lease never came back.
        item = self._item_of(record.node_id, drive_id=record.drive_id or None)
        try:
            grant = files_namespace(self._require_http()).acquire_lease(
                record.drive_id,
                record.node_id,
                instance_id=record.instance_id,
                machine_id=record.machine,
                if_match=str(item.get("etag", "")),
                purpose=record.purpose,
                inbound=True,
                live=True,
                retake=True,
            )
        except AlkeraHTTPError as exc:
            refusal = superseded_from(exc)
            if refusal is None:
                raise
            raise refusal from exc
        regranted = record.model_copy(
            update={
                "epoch": int(grant["epoch"]),
                "expires_at": str(grant.get("expiresAt", record.expires_at)),
            }
        )
        save_record(regranted, home=self._home)
        return regranted

    def _amend(self, chat_id: str, change: Callable[[HeldFolder], HeldFolder]) -> HeldFolder | None:
        """Change the handle as it is NOW, under the table lock.

        Every writer of a handle goes through here because two of them run on
        different threads at once — the beat that moves the record and the
        start that attaches the live sync — and a writer that put back the
        handle it had read a moment earlier dropped whatever the other had
        attached since. ``None`` means the folder is no longer held: the
        change is not applied, and the caller must not resurrect it.
        """
        with self._lock_table:
            held = self._held.get(chat_id)
            if held is None:
                return None
            amended = change(held)
            self._held[chat_id] = amended
            return amended

    def _refile(self, chat_id: str, relocated: HeldFolder) -> HeldFolder:
        """Record where the folder is filed now on the live handle, keeping
        whatever a beat put there in the meantime."""
        amended = self._amend(
            chat_id,
            lambda now: replace(
                now,
                org_path=relocated.org_path,
                record=now.record.model_copy(update={"org_path": relocated.org_path}),
            ),
        )
        return amended if amended is not None else relocated

    def _rebind(self, sync: LiveSync, held: HeldFolder) -> None:
        """Put the sync on the client the lease now signs with.

        The epoch moved under this holder, so the client the sync was built on
        signs every later call with a number the server has retired, and each
        one would be refused as somebody else's write.
        """
        sync.record = held.record
        api = sync.api
        if isinstance(api, RestLiveApi):
            api.http = self._fenced(held)
            api.files = self._fenced_files(held)

    def live(self, chat_id: str, working_dir: Path) -> LiveSync | None:
        """Stream this chat's working directory to the drive as it is written.

        Started once the mirror names its working directory, because that — not
        the folder — is what the agent writes: the records beside it are the
        box's own and are outside the watch by construction. ``None`` when the
        server granted the lease without the live plane, which leaves the chat
        exactly as it was: saved at the checkpoint push.

        Starting twice is the same sync: a chat re-taken after a beat lost its
        lease gets a new one, but a second call for a folder already streaming
        must not put two watchers on one directory.
        """
        held = self._held.get(chat_id)
        if held is None or not self.enabled:
            return None
        if held.live is not None and chat_id in self._live:
            # Running. A handle whose sync a failed hand-back stopped starts anew.
            return held.live
        cadence = LiveCadence.from_grant(held.record.live)
        if not held.record.live:
            return None
        # A run a lost lease left behind — the beat that lost it pops the
        # handle, and the take that follows mints a new one with no sync on
        # it — must not keep a second watcher on the directory.
        self.stop_live(chat_id, deadline=0.0)
        stop = threading.Event()
        # Where on the drive the watched directory is. The lease is on the chat
        # folder and the stream is of the working directory inside it, so an
        # inbound item's path has to be measured against the directory being
        # watched — measured against the folder, every drop would be filed one
        # level deep, under a second `scratch` nobody made.
        inside = working_inside(held.root, working_dir)
        dest = self._working_dest(held, working_dir)
        # The take IS a beat: the lease was granted a moment ago, and a fence
        # that had never been beaten would read as silence and refuse the
        # folder's very first flush.
        fence = SelfFence.for_record(held.record)
        fence.beat()
        sync = LiveSync(
            root=working_dir,
            record=held.record,
            cadence=cadence,
            api=RestLiveApi(
                # Through the folder's OWN client, namespace included: the
                # fence is a header on a client, so a namespace left on the
                # box's shared one opens every upload session unfenced. The
                # server then reads the holder's own writes as somebody else's
                # — it admits them as inbound drops for this box to download
                # back instead of settling them as saved, and the lease's
                # `last_sync_at` never moves, so a member's row says the file
                # is still on its way to a workspace that wrote it.
                files=self._fenced_files(held),
                http=self._fenced(held),
                root=working_dir,
                drive_id=held.record.drive_id,
                lease_node_id=held.record.node_id,
                dest=dest,
                inside=inside,
                home=self._home,
            ),
            watcher=self._watcher(working_dir, cadence, stop),
            fence=fence,
            clock=_MONOTONIC,
            root_path=dest,
            chat_id=chat_id,
            # Beside the mount record: what this sync promised the drive
            # survives the box restarting between a write and its flush.
            journal=LiveJournal(journal_path(held.root, home=self._home)),
            # Beside it, which node each file is: the next process on this
            # folder recognises a file the drive renamed meanwhile by it.
            node_map=NodeMapStore(held.root, inside=inside, home=self._home),
        )
        # What the take's pull just proved is the drive's needs no second
        # proof: without this the first sweep finds every file new and sends
        # each one back through the checkpoint push, several requests apiece,
        # for a folder nobody has touched.
        if held.pull is not None:
            sync.adopt(under_working_dir(held.pull.agreed, inside))
        # And what the pull could not prove but an earlier process knew: the
        # node of every file still here, so a rename the drive queued for one
        # while the box was down moves it rather than filling the new name.
        sync.seed()
        amended = self._amend(chat_id, lambda now: replace(now, live=sync))
        if amended is None:
            # The folder went away between the read above and now. There is
            # nothing to stream, and a watcher started on it would be a thread
            # nothing stops.
            if sync.journal is not None:
                sync.journal.close()
            return None
        self._live[chat_id] = _LiveRun(sync=sync, stop=stop, thread=self._run_live(chat_id, sync))
        # The agent works in this directory, so its tool results are where a
        # conflict the sync settled here is told — read through the held
        # handle, so a sync a re-take replaces is the one that answers.
        register_conflict_notices(working_dir, partial(self._live_of, chat_id))
        return sync

    def _live_of(self, chat_id: str) -> LiveSync | None:
        held = self._held.get(chat_id)
        return held.live if held is not None else None

    def land(self, chat_id: str, targets: Sequence[str], *, timeout: float) -> frozenset[str]:
        """Put the bytes of the files a reply names on the drive, then answer.

        ``targets`` are the references as the agent wrote them — relative to
        its working directory, or the box's absolute path into this chat's
        folder. Each one that names a file inside the watched directory is
        handed to the live sync's :meth:`~LiveSync.land`; anything else (a URL,
        another chat's path, a file beside the working directory the live
        plane does not watch) is not waited for. A folder with no live plane
        answers at once with nothing: its bytes go at the checkpoint, and a
        reader who opens the file meets the on-demand path. Blocks; call it
        off the event loop. Answers the relative paths that landed.
        """
        held = self._held.get(chat_id)
        if held is None or held.live is None:
            return frozenset()
        sync = held.live
        inside = working_inside(held.root, sync.root)
        relatives: list[str] = []
        for target in targets:
            path = chat_path(target, chat_id=chat_id)
            if path is None:
                continue
            if path.anchor == "working" or not inside:
                relatives.append(path.path)
            elif path.path.startswith(f"{inside}/"):
                relatives.append(path.path[len(inside) + 1 :])
        if not relatives:
            return frozenset()
        return sync.land(relatives, timeout=timeout)

    def stop_live(
        self, chat_id: str, deadline: float = 5.0, *, drain: float | None = None
    ) -> list[str] | None:
        """Stop streaming this chat's folder, flushing what is still queued.

        ``drain`` gives the content queue that many seconds to empty before
        the sync ends (on top of ``deadline``), and the answer is what it
        could not send — the files still on this machine only, by their path
        under the leased folder. None when there was no sync to stop.

        Called before the push and the release, never after: a batch still on
        the wire when the lease went back would be refused by the fence, and
        its bytes are the only ones the checkpoint push has not already sent.
        A run that has not finished within ``deadline`` is left behind rather
        than held on to — the hand-back has to happen, and the fence stops a
        stray flush from writing under a lease this box no longer owns. A
        ``deadline`` of zero does not wait at all (the lease is lost): the fence
        refuses what the run sends last, and its text peer sends nothing more."""
        running = self._live.pop(chat_id, None)
        if running is None:
            return None
        sync = running.sync
        if drain is not None:
            sync.request_drain(drain)
        unregister_conflict_notices(running.sync.root)
        running.stop.set()
        if deadline <= 0:
            if sync.peer is not None:
                sync.peer.close()
            return self._lease_relative(chat_id, sync, sync.unsynced())
        self._stopping_live[chat_id] = running
        try:
            running.thread.join(deadline + (drain or 0.0))
        finally:
            if self._stopping_live.get(chat_id) is running:
                del self._stopping_live[chat_id]
        if running.thread.is_alive():
            logger.info(
                "chat %s: its live sync was still running after %.0fs; the folder is handed "
                "back anyway and the fence refuses whatever it sends next",
                chat_id,
                deadline,
            )
            return self._lease_relative(chat_id, sync, sync.unsynced())
        if sync.journal is not None:
            sync.journal.close()
        left = sync.remainder if sync.remainder is not None else sync.unsynced()
        return self._lease_relative(chat_id, sync, left)

    def _lease_relative(self, chat_id: str, sync: LiveSync, paths: list[str]) -> list[str]:
        """``paths`` under the watched directory, spelled from the leased folder."""
        held = self._held.get(chat_id)
        step = "" if held is None else working_inside(held.root, sync.root)
        return [f"{step}/{path}" if step else path for path in paths]

    @staticmethod
    def _drain_seconds(held: HeldFolder) -> float:
        """How long a release waits for the content queue, as the grant served."""
        return LiveCadence.from_grant(held.record.live).release_drain_ms / 1000.0

    def _drop_journal(self, held: HeldFolder) -> None:
        """The lease went back: nothing the journal promised is owed any more."""
        path = journal_path(held.root, home=self._home)
        for suffix in ("", "-wal", "-shm", "-peer"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

    def tombstones(self, chat_id: str) -> tuple[str, ...]:
        """Paths the live sync has already trashed on the drive for this chat.

        The checkpoint push walks what is on disk, and a file the box deleted
        is gone from disk *and* gone from the drive — but a name the trash has
        not caught up with, or a path still on disk under a lease that trashed
        it, would be uploaded back and undo the delete. Named here so the push
        skips them, and with them what the live plane sends itself (one sender
        per file)."""
        held = self._held.get(chat_id)
        if held is None or held.live is None:
            return ()
        sent = held.live.sent_live(
            working_inside(held.root, held.live.root), running=chat_id in self._live
        )
        return tuple(sorted({*held.live.tombstones, *sent}))

    def _prune_deleted(self, chat_id: str, held: HeldFolder) -> int:
        """Trash on the drive what this box agreed and has since deleted
        (:func:`alkera_cli.cloud.folder_prune.prune_deleted`)."""
        return prune_deleted(
            chat_id=chat_id,
            root=held.root,
            record=held.record,
            api=files_namespace(self._fenced(held)),
            skip=set(self.tombstones(chat_id)),
            home=self._home,
        )

    def _wipe(self, held: HeldFolder) -> None:
        """Leave nothing of the chat on this box once its folder is back
        (:func:`~alkera_cli.cloud.folder_wipe.wipe_copy`)."""
        wipe_copy(held.root, self._layout.bound(held.chat_id), held.chat_id, home=self._home)

    def owed(self) -> list[str]:
        """Chats whose hand-back was refused and is still owed a retry."""
        return [chat_id for chat_id in self._owed if chat_id in self._held]

    def hand_back(
        self,
        chat_id: str,
        *,
        recover: bool = True,
        ending: str | None = None,
        gone: bool = False,
        if_owed: bool = False,
    ) -> ReleasedFolder | None:
        """Sleep: push everything this box wrote, then release the lease, in
        that order (``unmount``, which refuses to release a fenced push): a
        release first would hand the folder on before this box's bytes left.

        ``recover`` is what happens when the folder itself has gone from the
        drive: landed somewhere findable while the chat is still there, never
        once the caller knows the chat is gone (its deletion trashed the folder,
        and a copy pushed back would undo the delete). A workspace's shared
        tree is never landed for a person: no one person owns it. ``gone`` says
        the backend itself said the chat (or workspace) is deleted, so a folder
        the drive answers nothing for is that delete, not a box that lost its
        sight of it: the tree is discarded, not kept.

        Raises :class:`FolderHandBackError` when the folder could not be given
        back. It stays held while there is any chance of giving it back, and is
        dropped only once the box has run out of tries and said why.

        ``if_owed`` is a retry's: ``None`` without a word once the folder is no
        longer owed, decided under the chat's lock, since a take that served
        the chat again in the meantime cleared the debt.
        """
        held = self._held.get(chat_id)
        if held is None or not self.enabled:
            self._forget(chat_id)
            return None
        try:
            with self._lock(chat_id):
                if if_owed and chat_id not in self._owed:
                    return None
                held = self._relocated(self._held.get(chat_id) or held)
                on_gone = "discard" if gone else "trashed"
                if recover and not is_workspace_key(chat_id):
                    on_gone = "recover"
                return self._hand_back_locked(chat_id, held, on_gone=on_gone, ending=ending)
        except NotMountedError:
            # Nothing on disk claims the folder — there is no lease of ours to
            # hand back and nothing that could be pushed under one.
            self._forget(chat_id)
            return None
        except LeaseSupersededError:
            # Already taken from us. The record stays where `unmount` left it so
            # a person can see what happened; there is nothing to release.
            logger.warning(
                "chat %s: its folder had already been taken from this box; nothing was released",
                chat_id,
            )
            self._forget(chat_id)
            return None
        except (httpx.HTTPError, AlkeraHTTPError, OSError, RuntimeError) as exc:
            raise self._refused(chat_id, exc) from exc

    def _hand_back_locked(
        self, chat_id: str, held: HeldFolder, *, on_gone: str, ending: str | None = None
    ) -> ReleasedFolder:
        # The live sync stops FIRST, and stopping it flushes: a batch still on
        # the wire when the lease went back is refused by the fence, and those
        # bytes are the only ones the push below has not already carried. Its
        # tombstones are read after it has stopped, so a path the last flush
        # trashed is one the push below leaves alone too. It drains first, on
        # the grant's clock, so what the live plane can still land goes the
        # way it was queued.
        # The lease is kept on its own thread from here until the push has
        # ended, and never past it: the chat has left the box's mirrors, so the
        # beat pass no longer keeps it, and without a beat the live sync's
        # fence closes — its drain then waited out the whole window with every
        # change withheld, and the push ran on under a lease that had lapsed.
        keeper = _LeaseKeeper(
            partial(self._hand_back_beat, chat_id, held),
            every=held.record.heartbeat_every,
            name=f"alkera-hand-back-beat:{chat_id}",
        )
        keeper.start()
        try:
            return self._hand_back_kept(chat_id, held, keeper, on_gone=on_gone, ending=ending)
        finally:
            keeper.stop()

    def _hand_back_beat(self, chat_id: str, held: HeldFolder) -> bool:
        """One beat of a lease being handed back; False once it is not ours.

        Only the heartbeat and the fence it opens: a refusal here is not
        answered with a re-take, which would ask for the folder again in the
        middle of giving it back."""
        record = (self._held.get(chat_id) or held).record
        try:
            heartbeat(http=self._beats(), record=record, home=self._home, persist=False)
        except LeaseSupersededError:
            return False
        except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
            logger.info("chat %s: a beat during the hand-back did not land (%s)", chat_id, exc)
            return True
        for table in (self._stopping_live, self._live):
            run = table.get(chat_id)
            if run is not None:
                run.sync.fence.beat()
        return True

    def _hand_back_kept(
        self,
        chat_id: str,
        held: HeldFolder,
        keeper: _LeaseKeeper,
        *,
        on_gone: str,
        ending: str | None = None,
    ) -> ReleasedFolder:
        self.stop_live(chat_id, drain=self._drain_seconds(held))
        # The agent's database travels as one file: what its write-ahead log
        # still holds is folded in here, once nothing on this box writes it,
        # so the next box opens the session's whole record from the file alone.
        fold_agent_databases(held.root)
        self._pins.pin(chat_id, held.root)
        push_local = self._push_local(chat_id, held)
        held, where = self._locate(held)
        held = self._refile(chat_id, held)
        if where is _Gone.HERE:
            self._prune_deleted(chat_id, held)
            summary = unmount(
                files=self._fenced_files(held),
                http=self._fenced(held),
                root=held.root,
                home=self._home,
                push_tree=push_local,
                # The keeper beats through the push and is stopped — its beat
                # in flight finished — by ``unmount`` itself, immediately before
                # the release goes out. Tied to the release rather than to the
                # push, so no push that ends early or is swapped for another can
                # leave a beat to land on a lease already given back.
                before_release=keeper.stop,
                # The checkpoint push above has just carried every file on
                # this disk, what the drain left included, so nothing is left
                # on the machine: the count the release records is zero.
                unsynced=[],
                # A sleep is the chat-end transition: the release carries its
                # reason, and the server records the chat asleep with it.
                ending=ending,
            )
            self._drop_journal(held)
            self._forget(chat_id)
            if not summary.push.failed:  # a file the drive refused is kept on this disk
                self._wipe(held)
            return ReleasedFolder(chat_id=chat_id, org_path=held.org_path, push=summary.push)
        # The folder is not where the lease is: what follows lands the work
        # elsewhere and lets the lease go, and no beat belongs on top of that.
        keeper.stop()
        if on_gone == "recover":
            released = self._recover(chat_id, held, where)
        elif on_gone == "discard" or (where is _Gone.TRASHED and not is_workspace_key(chat_id)):
            # A chat's folder in the trash is its deletion, or the backend said so.
            # A workspace's tree is kept, for a restore and the next take to send.
            released = self._discard(chat_id, held, where)
        else:
            released = self._keep(chat_id, held)
        self._forget(chat_id)
        if not released.kept:
            self._wipe(held)
        return released

    def _refused(self, chat_id: str, exc: BaseException) -> FolderHandBackError:
        """Count one refused hand-back and say what happens next.

        A transient failure is never counted: the API coming back up is not
        something the box can run out of patience with. A refusal the server
        spelled out is counted, and once the count is spent the box stops —
        it keeps neither the folder nor the pretence that it will retry, and
        the caller says the reason on the chat.
        """
        transient = passing_fault(exc)
        attempts = self._refusals.get(chat_id, 0)
        if not transient:
            attempts += 1
            self._refusals[chat_id] = attempts
        exhausted = not transient and attempts >= handback_attempts()
        if exhausted:
            self._forget(chat_id)
        else:
            self._owed.add(chat_id)
        return FolderHandBackError(
            chat_id,
            str(exc) or exc.__class__.__name__,
            transient=transient,
            attempts=attempts,
            exhausted=exhausted,
        )

    def _forget(self, chat_id: str) -> None:
        self._held.pop(chat_id, None)
        self._dropped.discard(chat_id)
        self._owed.discard(chat_id)
        self._refusals.pop(chat_id, None)
        self._lapsed.discard(chat_id)
        self._gone.pop(chat_id, None)
        self._pins.forget(chat_id)

    def release(self, chat_id: str) -> bool:
        """Give the lease back with NOTHING pushed: the chat was taken and
        never ran here.

        A mirror that failed to start wrote nothing; the take pulled the
        folder down, and pushing this disk back up would only upload whatever
        an earlier life left on it as if it were this box's work. The lease
        goes back at once rather than lapsing on its own TTL, so the chat is
        takeable again — by this box on a later pass, or by any other. True
        when the server took the release; False when there was nothing of
        ours to give back (not held, or already taken from us).
        """
        waiting = self._held.get(chat_id)
        left = self.stop_live(
            chat_id, drain=None if waiting is None else self._drain_seconds(waiting)
        )
        held = self._held.pop(chat_id, None)
        if held is None or not self.enabled:
            return False
        try:
            with self._lock(chat_id):
                held = self._relocated(held)
                release_unpushed(
                    files=self._fenced_files(held),
                    http=self._fenced(held),
                    root=held.root,
                    home=self._home,
                    # Nothing is pushed here, so what the drain could not land
                    # stays on this machine, and the release says so by path.
                    unsynced=left,
                )
                self._drop_journal(held)
        except NotMountedError:
            return False
        except LeaseSupersededError:
            logger.warning(
                "chat %s: its folder had already been taken from this box; nothing was released",
                chat_id,
            )
            return False
        # No wipe here, unlike a sleep: nothing was pushed, and what is on
        # this disk may be an earlier life's work that never reached the drive.
        # The next hand-back pushes it and then leaves nothing behind.
        return True

    def push(self, chat_id: str) -> PushSummary | None:
        """Save what the chat has written so far, keeping the lease.

        The sleep is where the folder is handed back; this is what makes the
        cloud copy trail a live chat by a turn rather than by a whole sleep, so
        a box that dies mid-life has lost at most the turn it was in. ``None``
        when this box holds no folder for the chat, or when the push could not
        land — the beat still keeps the lease, and the next pass tries again.

        A folder somebody trashed (or purged) while this box held it is NOT
        pushed into. A create whose parent is in the trash cannot be reached by
        path afterwards, so such a push lands new versions on the files that
        still have a node and then refuses on the first folder it has to make —
        a half-saved chat, reported every beat as an error nobody can act on.
        The work is not lost by skipping it: the hand-back lands the whole tree
        in the owner's recovery folder, which is the one outcome that is honest
        about where it went.
        """
        return self._push(chat_id, verified=False)

    def _push(self, chat_id: str, *, verified: bool) -> PushSummary | None:
        held = self._held.get(chat_id)
        if held is None or not self.enabled:
            return None
        try:
            with self._lock(chat_id):
                held, where = self._locate(held)
                if where is not _Gone.HERE:
                    self._folder_gone(chat_id, where)
                    return None
                held = self._refile(chat_id, held)
                self._prune_deleted(chat_id, held)
                self._pins.pin(chat_id, held.root)
                summary = export(
                    files=self._fenced_files(held),
                    http=self._fenced(held),
                    record=held.record,
                    push_tree=self._push_local(chat_id, held),
                    home=self._home,
                    also="\0".join(self.tombstones(chat_id)),
                )
                return self._left_behind.said(chat_id, summary)
        except LeaseSupersededError as refused:
            # Not taken at its word: the beat renews the lease (re-minting the
            # client if the epoch moved) or settles who holds it now.
            ours = None if verified else self._still_ours(chat_id)
            if ours:
                return self._push(chat_id, verified=True)
            if ours is None and not verified:
                logger.info("chat %s: a fenced push was not verified (%s)", chat_id, refused)
                return None
            logger.warning(
                "chat %s: this box no longer holds its folder; its work was not pushed and "
                "stays on this disk (%s)",
                chat_id,
                refused,
            )
            # No beat keeps the lease from here, and the service stops serving it.
            self._stop_holding(chat_id)
            return None
        except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
            logger.info("chat %s: a folder push did not land (%s)", chat_id, exc)
            return None

    def _still_ours(self, chat_id: str) -> bool | None:
        """Whether the beat says the folder is still ours; ``None``: unknown."""
        held = self._held.get(chat_id)
        if held is None:
            return False
        outcome = self._ask_beat(held)
        if not isinstance(outcome, (MountRecord, LeaseSupersededError)):
            return None
        return self._settle_beat(chat_id, held, outcome, keepalive=False)

    def _folder_gone(self, chat_id: str, where: _Gone) -> None:
        """Say once that the checkpoint push has nowhere to land.

        Once per chat and verdict, because the beat comes round every few
        seconds and the state it is reporting cannot change until somebody
        restores the folder or the chat is handed back.
        """
        if self._gone.get(chat_id) is where:
            return
        self._gone[chat_id] = where
        logger.warning(
            "chat %s: its folder is %s in the drive, so this turn's files were not pushed into "
            "it; the hand-back lands them in the owner's recovery folder",
            chat_id,
            where.value,
        )

    def resumable(self, chat_id: str) -> bool:
        """Whether a record from a previous life of this box is still on disk.

        The record is what makes a resume the *same* holder rather than a second
        one, so its presence is the difference between waking a chat up and
        racing the box that was running it.
        """
        return load_record(self.local_root(chat_id), home=self._home) is not None

    # -- internals -------------------------------------------------------------

    def _lock(self, chat_id: str) -> threading.Lock:
        """This chat's own lock, minted on first use."""
        with self._lock_table:
            return self._locks.setdefault(chat_id, threading.Lock())

    def _watcher(self, working_dir: Path, cadence: LiveCadence, stop: threading.Event) -> Any:
        """The source of changes for one folder's live sync.

        A seam rather than a direct construction so a test can drive the sync
        with a scripted stream: the real one puts an OS watch on a directory,
        which no unit test should have to wait for.
        """
        return self._watcher_factory(working_dir, cadence, stop)

    @staticmethod
    def _default_watcher(working_dir: Path, cadence: LiveCadence, stop: threading.Event) -> Any:
        return TreeWatcher(working_dir, cadence=cadence, stop=stop)

    def _run_live(self, chat_id: str, sync: LiveSync) -> threading.Thread:
        """Run one folder's live sync on a thread of its own.

        Its own loop rather than the service's, because the sync's only await
        is an OS watch that never ends on its own — parked on the service's
        loop it would be a task nothing can cancel without cancelling the beat
        with it. A refusal ends the thread: the fence has already decided this
        box may not write, and the checkpoint push is what still runs.
        """

        def pump() -> None:
            try:
                asyncio.run(sync.run())
            except LeaseSupersededError:
                logger.info(
                    "chat %s: its folder moved on while the live sync was writing; "
                    "nothing more is streamed from here",
                    chat_id,
                )
            except Exception:
                logger.exception("chat %s: its live sync stopped", chat_id)

        thread = threading.Thread(target=pump, name=f"live-sync-{chat_id}", daemon=True)
        thread.start()
        return thread

    @classmethod
    def _working_dest(cls, held: HeldFolder, working_dir: Path) -> str:
        """The drive path of the chat's working directory.

        The lease is on the folder and the stream is of the directory inside
        it, so the wire path the live push addresses is the folder's plus
        whatever the working directory is called locally.
        """
        inside = working_inside(held.root, working_dir)
        folder = held.org_path.strip("/")
        return f"{folder}/{inside}".strip("/") if inside else folder

    def _push_local(self, chat_id: str, held: HeldFolder) -> Callable[..., PushSummary]:
        """The checkpoint push for this chat, blind to what it already trashed."""
        skip = self.tombstones(chat_id)  # before the bases: a round may land between
        return self._layout.push(chat_id, skip, _PUSH_LOCAL, bases=_bases(held, self._home))

    def _fenced(self, held: HeldFolder) -> httpx.Client:
        """The client this folder's writes go out on.

        A handle built without one (a test, a handle from before the folder was
        taken) falls back to the shared client: the write still carries the
        fence, because the mount chain sets it for the length of the call.
        """
        return held.client if held.client is not None else self._require_http()

    def _fenced_files(self, held: HeldFolder) -> FilesApi:
        """The Files namespace this folder's writes go out through.

        It has to be the namespace bound to the same client :meth:`_fenced`
        answers: a push opens its upload session through the namespace and
        writes the tree through the client, and the fence is a header on a
        client — so a namespace left on the box's shared one opens every
        session unfenced and the server refuses it as somebody else's write.
        """
        files = self._require_files()
        return fenced_files(files, held.client) if held.client is not None else files

    def _require_files(self) -> FilesApi:
        files = self._files
        if files is None:  # pragma: no cover - guarded by `enabled`
            raise RuntimeError("this box holds no chat folders")
        return files

    def _require_http(self) -> httpx.Client:
        http = self._http
        if http is None:  # pragma: no cover - guarded by `enabled`
            raise RuntimeError("this box holds no chat folders")
        return http

    def _beats(self) -> httpx.Client:
        """The client a lease beat speaks on: its own, never the transfers'."""
        return self._beat_http if self._beat_http is not None else self._require_http()

    def _path_of(self, node_id: str, *, drive_id: str | None = None) -> str | None:
        """Where the node is filed right now, as the wire spells it."""
        return wire_path(self._item_of(node_id, drive_id=drive_id))

    def _item_of(self, node_id: str, *, drive_id: str | None = None) -> Mapping[str, Any]:
        """The node, on ``drive_id`` when the caller knows it — the chat's own
        record does — and on the caller's own drive only when it does not."""
        files = self._require_files()
        if drive_id is None:
            drive_id = str(files.drive()["id"])
        return files.item(drive_id, node_id)

    def _relocated(self, held: HeldFolder) -> HeldFolder:
        """The folder as it is filed NOW, for a caller that only needs the path."""
        return self._locate(held)[0]

    def _locate(self, held: HeldFolder) -> tuple[HeldFolder, _Gone]:
        """The folder as it is filed NOW, asked for by the id the lease is on.

        A chat renamed while its box held it moved the node, and a push aimed at
        the old name would raise a new folder there instead of landing in the
        chat — or, when the name holds a `?`, address something else entirely.
        The id cannot be renamed, so it is what the question is asked with, and
        the answer is where the work goes.

        Best-effort about the path: a lookup that fails on the wire keeps the
        path the lease was taken under, because a push that can still find its
        folder is better than one that refuses to run. A node the server says is
        not there at all is different — that is the caller's cue to recover.
        """
        if not held.record.node_id:
            return held, _Gone.HERE
        try:
            # Read under this folder's own fence, like every write for it: the
            # lease is what admits a box to a chat's folder once the chat has
            # been bound to another machine, and the server reads the lease
            # off the fence headers. Asked unfenced, a holder handing back a
            # chat that moved off it is told the folder does not exist, and
            # would treat the only copy of its last turn as a purged chat's.
            # The lease names the drive the folder is on, so the drive is not
            # read again on every push just to learn its id.
            files = self._fenced_files(held)
            item = files.item(held.record.drive_id or str(files.drive()["id"]), held.record.node_id)
        except AlkeraHTTPError as refused:
            if refused.status == 404:
                return held, _Gone.PURGED
            raise
        except (httpx.HTTPError, OSError):
            return held, _Gone.HERE
        where = _Gone.TRASHED if bool(item.get("trashed")) else _Gone.HERE
        current = wire_path(item)
        if current is None or current == held.record.org_path:
            return held, where
        record = held.record.model_copy(update={"org_path": current})
        save_record(record, home=self._home)
        return replace(held, org_path=current, record=record), where

    def _recover(self, chat_id: str, held: HeldFolder, where: _Gone) -> ReleasedFolder:
        """Land the work of a chat whose folder is gone somewhere a person can
        find it, then let the lease go.

        Both a trashed folder and a purged one end up here. Handing back INTO a
        trashed folder would be the nicer story — a restore would bring the
        agent's work with it — but a create whose parent is trashed is refused,
        so a push aimed there would land the files that already have a node and
        refuse the rest, which is a half-saved chat presented as a saved one.
        A whole tree in the owner's recovery folder is the honest outcome.
        """
        name = held.org_path.rpartition("/")[2] or chat_id
        home = home_path(self._require_files())
        dest = recovery_path(home, name, taken=partial(occupied, self._require_files()))
        summary = _PUSH_LOCAL(
            files=self._require_files(),
            http=self._require_http(),
            root=held.root,
            dest=dest,
            drive_id=held.record.drive_id or None,
        )
        logger.warning(
            "chat %s: its folder is %s in the drive; its work was landed in %r instead",
            chat_id,
            where.value,
            dest,
        )
        self._let_go(held)
        return ReleasedFolder(
            chat_id=chat_id,
            org_path=dest,
            push=summary,
            recovered=True,
            shown_path=shown_in_home(dest, home),
        )

    def _discard(self, chat_id: str, held: HeldFolder, where: _Gone) -> ReleasedFolder:
        """Give the lease back with nothing landed: the chat is gone too.

        A chat's folder is trashed BY the chat's deletion, so the tree this box
        is holding is a copy of something a person has just thrown away. Landing
        it — anywhere — would put the deleted chat's files back in the drive
        under a name the delete never touched, which is the one outcome a delete
        must not have. The trashed folder is already recoverable for as long as
        the trash keeps it; this box adds nothing to that and only lets go.

        Reached for a TRASHED folder only. The trash is the one thing that
        confirms the deletion: a folder the drive answers nothing for is as
        likely a folder this box may no longer see as a purged one, and that
        is :meth:`_keep`.
        """
        logger.info(
            "chat %s: the chat is gone and its folder is %s in the drive; nothing was landed "
            "and only its lease went back",
            chat_id,
            where.value,
        )
        self._let_go(held)
        return ReleasedFolder(
            chat_id=chat_id, org_path=held.org_path, push=PushSummary(), discarded=True
        )

    def _keep(self, chat_id: str, held: HeldFolder) -> ReleasedFolder:
        """Let the lease go and keep the working tree on this disk: the drive
        answered nothing for the folder and nothing confirms the chat is gone.

        A not-found on the folder is what a purged node answers — and what a
        box answers whose chat has since been bound to another machine and
        whose lease has lapsed or been ended for it, which is a chat that is
        very much alive. The two are the same from here, and only one of them
        may cost the tree: what is on this disk is the only copy of the last
        turn's writes, and the next take of the chat on this box pushes it
        (the pull removes nothing a box wrote and never pushed). What a
        chat's deletion would have shown is the folder in the trash, and that
        path discards; this one never does.
        """
        logger.warning(
            "chat %s: its folder is gone from where the box can write (trashed or not "
            "found) and nothing confirms a delete; the lease went back and the tree is kept at %s",
            chat_id,
            held.root,
        )
        self._let_go(held)
        return ReleasedFolder(
            chat_id=chat_id, org_path=held.org_path, push=PushSummary(), kept=True
        )

    def _let_go(self, held: HeldFolder) -> None:
        """Release the lease on a folder that is gone, and forget the mount.

        A lease on a purged node is already gone with it, so a release the
        server answers 404 is the expected outcome rather than a failure — but
        the record on this disk has to go either way, or the next take would
        read it as a mount of a node that no longer exists.
        """
        try:
            release_unpushed(
                files=self._require_files(),
                http=self._require_http(),
                root=held.root,
                home=self._home,
            )
        except (NotMountedError, LeaseSupersededError):
            remove_record(held.root, home=self._home)
        except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
            logger.info(
                "chat %s: the lease on its gone folder was not released (%s); it lapses on its own",
                held.chat_id,
                exc,
            )
            remove_record(held.root, home=self._home)

    def _note_unavailable(self, chat_id: str, org_path: str, refused: AlkeraHTTPError) -> None:
        reason = f"{refused.status}"
        if self._unavailable.get(chat_id) == reason:
            return
        self._unavailable[chat_id] = reason
        logger.info(
            "chat %s: its folder %r is not served by this backend (%s); the chat runs without "
            "durable scratch until it is",
            chat_id,
            org_path,
            reason,
        )
