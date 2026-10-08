"""A box's custody of the workspaces its chats are members of.

A chat in a native workspace (:func:`~alkera_cli.cloud.workspace_seat.seat_of`)
is served as a member: before its own records folder is taken, the box takes
ONE lease on the workspace's folder (the first member to wake takes it, the
rest find it held) and streams the workspace's shared ``files/`` tree, which
every member runs in. Its own chat lease then nests under the workspace's.

Sleep has two tiers. A member whose chat goes quiet is put to sleep the way
every chat is (its agent server stops, its records go back), and leaves the
workspace. The workspace itself is put away only once no member is awake and
nothing holds it: a process a member left running in the sandbox holds it for
as long as it runs (as a process in a chat's sandbox holds that chat), and a
person writing into the shared tree through the live editor holds it for
:data:`LIVE_HOLD_SECONDS` after their last write landed. Neither holds it
under memory pressure: a chat that is answering outranks a process nobody is
watching. Putting it away pushes the shared tree, releases the lease and
takes its sandbox down; a live edit made while it is away lands on the drive
directly. Waking a member wakes the workspace if it is asleep.

Everything here is keyed by the workspace's custody key; the service keeps
keying chats by chat id and asks this module the few questions where the two
differ: which custody key's live sync covers a chat (:meth:`WorkspaceHost.
live_key`), which keys the beat must keep (:meth:`WorkspaceHost.keys`), and
what to say about the workspace when it reports on a chat
(:meth:`WorkspaceHost.report_facets`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from alkera_core.chat_refusals import ChatRefusalKind

from alkera_cli.cloud.faults import says_refused
from alkera_cli.cloud.folder import FolderBusyError
from alkera_cli.cloud.workspace_seat import (
    FILES_DIR,
    SeatRefused,
    WorkspaceSeat,
    seat_of,
)
from alkera_cli.harness.sandbox_processes import SandboxProcess
from alkera_cli.harness.sandbox_scope import (
    SCOPE_KEY,
    SandboxScope,
    forget_scope,
    register_scope,
    scope_for,
)

logger = logging.getLogger(__name__)

SandboxState = Literal["waking", "awake", "asleep"]

#: How long a write from the live editor holds a workspace no chat is awake
#: in, after it landed: long enough for someone typing to keep the shared tree
#: on the box, short enough that a tab left open does not hold it all day.
LIVE_HOLD_SECONDS = 15 * 60.0

#: What a member is told while another box holds its workspace.
HELD_ELSEWHERE = "this chat's workspace is open on another machine"


class Custody(Protocol):
    """The part of the folder custody this module drives (``ChatFolders``)."""

    @property
    def enabled(self) -> bool: ...

    def take(self, chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any: ...

    def held(self, chat_id: str) -> Any: ...

    def local_root(self, chat_id: str) -> Path: ...

    def live(self, chat_id: str, working_dir: Path) -> Any: ...

    def push(self, chat_id: str) -> Any: ...

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any: ...


class Joined(Enum):
    """What :meth:`WorkspaceHost.join` decided for a chat."""

    #: Not a member of a native workspace: served exactly as a chat always was.
    SOLO = "solo"
    #: A member, and the workspace is held here: serve the chat in it.
    MEMBER = "member"
    #: Not served here: the workspace is held by another box, could not be
    #: taken, or the chat's folder is not where its workspace is.
    NOT_HERE = "not_here"


@dataclass(slots=True)
class _Held:
    """One workspace this box holds."""

    seat: WorkspaceSeat
    members: set[str] = field(default_factory=set)
    #: When a write from the live editor last landed in the shared tree.
    edited_at: float | None = None
    #: Every member that left while the box held it was deleted, as the
    #: backend said: a shared tree the drive no longer answers for is then the
    #: workspace's deletion. One member that only slept is not; its work is
    #: in the tree.
    gone: bool = False
    #: A member left without being deleted while the box held it.
    slept: bool = False
    #: Its hand-back did not land: the custody still holds the folder and
    #: owes the hand-back, which is tried again (with ``ending``) until it
    #: lands or a member wakes, which takes the debt back.
    owed: bool = False
    ending: str | None = None


@dataclass(slots=True)
class _KeyLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


#: What a sandbox probe answers: the processes running in it beside the agent
#: servers, or ``None`` when it could not be read.
SandboxWork = Callable[[str], tuple[SandboxProcess, ...] | None]
#: What the memory reader answers for one workspace: MiB in use, or ``None``.
MemoryOf = Callable[[str], int | None]


class WorkspaceHost:
    """See the module docstring."""

    def __init__(
        self,
        *,
        folders: Custody,
        instance_of: Callable[[str], str],
        refuse: Callable[[str, str, ChatRefusalKind], Awaitable[None]],
        clock: Callable[[], float],
        live_hold_seconds: float = LIVE_HOLD_SECONDS,
        sandbox_work: SandboxWork | None = None,
        memory_of: MemoryOf | None = None,
        release_sandbox: Callable[[str], Awaitable[None]] | None = None,
        in_thread: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._folders = folders
        self._instance_of = instance_of
        self._refuse = refuse
        self._clock = clock
        self._live_hold_seconds = live_hold_seconds
        self._sandbox_work = sandbox_work
        self._memory_of = memory_of
        self._release_sandbox = release_sandbox
        self._in_thread = in_thread or asyncio.to_thread
        self._held: dict[str, _Held] = {}
        self._seats: dict[str, WorkspaceSeat] = {}
        self._locks: dict[str, _KeyLock] = {}
        #: The holder each chat was last refused under because another box
        #: holds its workspace, so the refusal is said once per holder.
        self._refused_busy: dict[str, str | None] = {}
        #: Joins in a row each chat found its workspace held by another box.
        #: A wake or a move passes through this while the previous holder
        #: hands the workspace back, so it is said only once it has outlasted
        #: its tries (``faults.says_refused``), like any passing fault.
        self._busy_tries: dict[str, int] = {}

    # -- what the service asks ------------------------------------------------

    def seat(self, chat_id: str) -> WorkspaceSeat | None:
        """The workspace ``chat_id`` is served in on this box, if any."""
        return self._seats.get(chat_id)

    def live_key(self, chat_id: str) -> str:
        """The custody key whose live sync covers ``chat_id``'s working
        directory: its workspace's for a member, its own otherwise."""
        seat = self._seats.get(chat_id)
        return seat.key if seat is not None else chat_id

    def keys(self) -> list[str]:
        """Every workspace custody key this box holds, for the beat."""
        return list(self._held)

    def custody_keys(self, chats: Mapping[str, Any]) -> list[str]:
        """Every folder the box keeps a lease on: each served chat's own (the
        keys of ``chats``) and each workspace its chats are members of."""
        return [*chats, *self._held]

    async def dropped(self, chats: Mapping[str, Any]) -> list[str]:
        """The chats to stop because the custody let go of the folder they are
        served from (a push or a beat found the lease gone).

        From then on nothing beats that folder and nothing saves what is
        written into it, and the beats skip it for being unheld: a chat (or a
        workspace's members) left running there wrote work no push carried
        while the box looked to everyone like it held the chat. Stopping it
        means the next message takes the folder again or is told who holds it.
        """
        dropped = getattr(self._folders, "dropped", None)
        if dropped is None:
            return []
        stopping: list[str] = []
        for key in self.custody_keys(chats):
            if self._folders.held(key) is not None or not dropped(key):
                continue
            these = await self.to_stop(key, chats)
            logger.warning(
                "%s: its folder is no longer held by this box; %s stopped so nothing is "
                "written where nothing is saved",
                key,
                ", ".join(these) if these else "nothing served from it is",
            )
            getattr(self._folders, "settle_drop", lambda _key: None)(key)
            stopping.extend(these)
        return stopping

    async def to_stop(self, key: str, chats: Mapping[str, Any]) -> list[str]:
        """The chats to stop because the lease on ``key`` is somebody else's
        now: the chat itself, or every member of the workspace."""
        if key in chats:
            return [key]
        return [chat_id for chat_id in sorted(await self.lost(key)) if chat_id in chats]

    def members(self, key: str) -> frozenset[str]:
        held = self._held.get(key)
        return frozenset(held.members) if held is not None else frozenset()

    def working_dir(self, chat_id: str) -> Path | None:
        """The shared tree a member runs in, on this box."""
        seat = self._seats.get(chat_id)
        if seat is None:
            return None
        return self._folders.local_root(seat.key) / FILES_DIR

    def sandbox_bag(self, chat_id: str) -> dict[str, str]:
        """What a member's harness bag carries so its sandbox is the
        workspace's: the workspace's custody key. Empty for a chat on its own,
        whose manifest is left exactly as it was."""
        seat = self._seats.get(chat_id)
        return {SCOPE_KEY: seat.key} if seat is not None else {}

    def state(self, key: str) -> SandboxState:
        held = self._held.get(key)
        return "awake" if held is not None and not held.owed else "asleep"

    def report_facets(self, chat_id: str) -> dict[str, Any]:
        """The workspace half of a report about ``chat_id``: nothing for a
        chat on its own (the report is sent exactly as it always was), else
        the workspace's sandbox state and what it holds in memory."""
        seat = self._seats.get(chat_id)
        if seat is None:
            return {}
        key = seat.key
        facets: dict[str, Any] = {"workspace_sandbox": self.state(key)}
        memory = self._memory(key)
        if memory is not None:
            facets["workspace_memory_mb"] = memory
        return facets

    def forget_report(self, chat_id: str) -> None:
        """Drop ``chat_id``'s seat once its last report went out."""
        seat = self._seats.get(chat_id)
        if seat is None:
            return
        held = self._held.get(seat.key)
        if held is None or chat_id not in held.members:
            self._seats.pop(chat_id, None)
            forget_scope(chat_id)

    # -- waking ---------------------------------------------------------------

    async def join(self, chat_id: str, chat: Mapping[str, Any]) -> Joined:
        """Seat ``chat_id`` in its workspace, taking the workspace's folder
        first when this box does not hold it yet."""
        seat = seat_of(chat)
        if seat is None:
            return Joined.SOLO
        if isinstance(seat, SeatRefused):
            await self._refuse(chat_id, seat.reason, "workspace_unservable")
            return Joined.NOT_HERE
        if not _is_uuid(seat.workspace_id):
            # The id names a directory on this box; only an id can be one.
            await self._refuse(
                chat_id,
                "this chat's workspace id is not one this box can hold",
                "workspace_unservable",
            )
            return Joined.NOT_HERE
        if not self._folders.enabled:
            # A box with no folder custody has no shared tree to give the
            # chat; serving it in a scratch of its own would split it from its
            # siblings without a word.
            await self._refuse(
                chat_id, "this box cannot hold a workspace's shared files", "workspace_unservable"
            )
            return Joined.NOT_HERE
        key = seat.key
        async with self._locked(key):
            held = self._held.get(key)
            # Taken afresh, or taken back from a hand-back that did not land:
            # the take clears the custody's debt and streams the tree again.
            busy: FolderBusyError | None = None
            if held is None or held.owed:
                taken = await self._take(chat_id, seat)
                if taken is True:
                    held = held or _Held(seat=seat)
                    held.owed = False
                    self._held[key] = held
                elif isinstance(taken, FolderBusyError):
                    busy = taken
            if held is not None and not held.owed:
                held.members.add(chat_id)
        if held is None or held.owed:
            if busy is not None:
                await self._held_elsewhere(chat_id, seat, busy.holder)
            return Joined.NOT_HERE
        self._refused_busy.pop(chat_id, None)
        self._busy_tries.pop(chat_id, None)
        self._seats[chat_id] = seat
        register_scope(self._scope(chat_id, key))
        return Joined.MEMBER

    async def _held_elsewhere(self, chat_id: str, seat: WorkspaceSeat, holder: str | None) -> None:
        """Another box holds the workspace. A wake or a switch passes through
        this while the previous holder hands the workspace back, so it is a
        passing fault: nothing is said until it has outlasted its tries, and
        the chat reads as the machine's own state (starting, waking) in the
        meantime. Past that it is said on the chat, as a refused seat is, so
        the reader is not left at a composer nobody answers. Once per chat and
        holder."""
        tries = self._busy_tries[chat_id] = self._busy_tries.get(chat_id, 0) + 1
        if not says_refused("transient", tries):
            logger.info(
                "chat %s waits for its workspace %s: another machine still holds it%s",
                chat_id,
                seat.workspace_id,
                f" ({holder})" if holder else "",
            )
            return
        if chat_id in self._refused_busy and self._refused_busy[chat_id] == holder:
            return
        self._refused_busy[chat_id] = holder
        logger.warning(
            "chat %s is not served here: its workspace %s is held by another machine%s",
            chat_id,
            seat.workspace_id,
            f" ({holder})" if holder else "",
        )
        await self._refuse(
            chat_id, HELD_ELSEWHERE + (f" ({holder})" if holder else ""), "workspace_elsewhere"
        )

    async def _take(self, chat_id: str, seat: WorkspaceSeat) -> bool | FolderBusyError:
        """Take the workspace's folder and start streaming its shared tree.
        The refusal when another box holds it; False when it could not be
        taken for any other reason."""
        key = seat.key
        try:
            taken = await self._in_thread(
                self._folders.take, key, seat.custody_row(), instance=self._instance_of(key)
            )
        except FolderBusyError as busy:
            return busy
        except Exception as exc:  # an unreachable drive
            logger.info(
                "chat %s is not served here: its workspace %s could not be taken (%s)",
                chat_id,
                seat.workspace_id,
                exc,
            )
            return False
        if taken is None:
            logger.info(
                "chat %s is not served here: its workspace %s has no folder this box can hold",
                chat_id,
                seat.workspace_id,
            )
            return False
        files = self._folders.local_root(key) / FILES_DIR
        try:
            await self._in_thread(files.mkdir, parents=True, exist_ok=True)
            await self._in_thread(self._folders.live, key, files)
        except Exception as exc:
            # The shared tree is still saved at each member's checkpoint.
            logger.info("workspace %s: its shared files are not streamed (%s)", key, exc)
        logger.info("workspace %s: taken for chat %s", seat.workspace_id, chat_id)
        return True

    # -- sleeping -------------------------------------------------------------

    async def leave(
        self,
        chat_id: str,
        *,
        ending: str | None = None,
        gone: bool = False,
        moved: bool = False,
    ) -> SandboxState | None:
        """``chat_id``'s agent server is down and its records are back: it is
        no longer a member here. The workspace is put away at once when no
        member is left and nothing it started still runs; held up otherwise.
        ``gone``: the backend said the chat is deleted. ``moved``: the chat is
        bound to another machine now, so its workspace goes there with it and
        nothing it left running here holds it back (the next box cannot take
        the shared tree while this one keeps it). ``None`` for a chat that was
        not a member."""
        seat = self._seats.get(chat_id)
        if seat is None:
            return None
        key = seat.key
        async with self._locked(key):
            held = self._held.get(key)
            if held is None or held.owed:
                return "asleep"
            held.members.discard(chat_id)
            held.slept = held.slept or not gone
            if held.members:
                return "awake"
            held.gone = gone and not held.slept
            if not moved and await self._held_up(key, held):
                logger.info(
                    "workspace %s: its last chat slept; something still holds it, so its "
                    "sandbox stays up",
                    seat.workspace_id,
                )
                return "awake"
            return "asleep" if await self._put_away(key, held, ending=ending) else "awake"

    async def sweep(
        self, *, pressure: bool = False, served: Collection[str] | None = None
    ) -> list[str]:
        """Put away every workspace no member is in once nothing holds it.
        Under memory pressure, one held up goes anyway (one per sweep): a busy
        chat outranks a process nobody is watching. ``served``: the chats the
        box still serves; a member outside it left without saying so (its
        start failed, its stop was cut short) and holds nothing. A workspace
        whose hand-back is owed is left to :meth:`retry_owed`. Returns what
        was put away."""
        put_away: list[str] = []
        for key in list(self._held):
            async with self._locked(key):
                held = self._held.get(key)
                if held is None or held.owed or self._members(held, served):
                    continue
                if pressure or not await self._held_up(key, held):
                    if await self._put_away(key, held, ending="evicted" if pressure else "idle"):
                        put_away.append(key)
                    if pressure:
                        break
        return put_away

    async def put_away(
        self, key: str, *, ending: str, served: Collection[str] | None = None
    ) -> bool:
        """Put ``key`` away now if no member is in it, held up or owed or not.
        True when it was handed back."""
        async with self._locked(key):
            held = self._held.get(key)
            if held is None or self._members(held, served):
                return False
            return await self._put_away(key, held, ending=ending)

    async def put_away_every(
        self, *, ending: str, served: Collection[str] | None = None
    ) -> list[str]:
        """The box is going away: put away every workspace no member is in,
        held up or not, all at once. Nothing a hold protects outlives the
        process (the sandboxes go with it), and a lease left behind would keep
        every other box off the shared tree until it lapsed."""
        keys = list(self._held)
        done = await asyncio.gather(
            *(self.put_away(key, ending=ending, served=served) for key in keys)
        )
        return [key for key, ok in zip(keys, done, strict=True) if ok]

    async def retry_owed(self) -> list[str]:
        """Hand back again every workspace whose hand-back did not land and
        that no member has woken since. Returns what was handed back."""
        handed: list[str] = []
        for key in [key for key, held in self._held.items() if held.owed]:
            async with self._locked(key):
                held = self._held.get(key)
                if held is not None and held.owed and not held.members:
                    if await self._put_away(key, held, ending=held.ending):
                        handed.append(key)
        return handed

    def note_used(self, key_or_chat: str) -> None:
        """A write from the live editor landed in the workspace's shared tree:
        it holds the workspace for :data:`LIVE_HOLD_SECONDS` from now."""
        key = self.live_key(key_or_chat)
        held = self._held.get(key)
        if held is not None:
            held.edited_at = self._clock()

    async def lost(self, key: str) -> frozenset[str]:
        """The workspace's lease is no longer this box's: forget it and answer
        the members whose sessions must stop, since none may write into it.

        The sandbox goes and the custody forgets the folder under the
        workspace's lock: a member waking in between would otherwise take the
        folder afresh and have that new grant handed back under it. Nothing is
        pushed under a lease that is not this box's, and the tree stays unless
        the drive says why."""
        async with self._locked(key):
            held = self._held.pop(key, None)
            if held is None:
                return frozenset()
            logger.warning("workspace %s: its folder was taken from this box", key)
            if self._release_sandbox is not None:
                try:
                    await self._release_sandbox(key)
                except Exception:
                    logger.exception("workspace %s: its sandbox was not taken down", key)
            try:
                await self._in_thread(self._folders.hand_back, key, recover=False)
            except Exception as exc:
                logger.warning("workspace %s: its custody did not let go (%s)", key, exc)
        return frozenset(held.members)

    async def turn_ended(self, chat_id: str) -> None:
        """A member's turn is over: save the shared tree now, as a chat's
        own folder is saved at the end of its turn."""
        seat = self._seats.get(chat_id)
        if seat is None or seat.key not in self._held:
            return
        try:
            await self._in_thread(self._folders.push, seat.key)
        except Exception as exc:
            logger.info("workspace %s: its shared files were not pushed (%s)", seat.key, exc)

    async def _held_up(self, key: str, held: _Held) -> bool:
        """Whether a workspace no member is in must stay up: a live edit
        landed in the hold window, or something it started still runs in its
        sandbox. A sandbox that cannot be read holds nothing, as a chat's
        does not: a reader that never answers must not pin a box's memory."""
        if held.edited_at is not None:
            if self._clock() - held.edited_at < self._live_hold_seconds:
                return True
        if self._sandbox_work is None:
            return False
        try:
            found = await self._in_thread(self._sandbox_work, key)
        except Exception:
            logger.debug("workspace %s: its sandbox could not be read", key, exc_info=True)
            return False
        return bool(found)

    async def _put_away(self, key: str, held: _Held, *, ending: str | None) -> bool:
        """Push the shared tree, release the lease, take the sandbox down.

        Off the held table while the hand-back runs, so no beat asserts a
        lease being given back. One that did not land, on a folder the
        custody still holds, is back on it as owed: beaten, tried again by
        :meth:`retry_owed`, and taken back by a member that wakes. True when
        the workspace was handed back."""
        self._held.pop(key, None)
        if self._release_sandbox is not None:
            try:
                await self._release_sandbox(key)
            except Exception:
                logger.exception("workspace %s: its sandbox was not taken down", key)
        try:
            await self._in_thread(self._folders.hand_back, key, ending=ending, gone=held.gone)
        except Exception as exc:
            owed = self._folders.held(key) is not None
            logger.warning(
                "workspace %s: its shared files were not handed back (%s); %s",
                key,
                exc,
                "the hand-back is owed and tried again"
                if owed
                else "this box has stopped trying and the lease lapses on its own",
            )
            if owed:
                held.owed, held.ending = True, ending
                self._held[key] = held
            return False
        logger.info("workspace %s: put away (%s)", held.seat.workspace_id, ending or "asleep")
        return True

    def _members(self, held: _Held, served: Collection[str] | None) -> set[str]:
        """``held``'s members, less any the box no longer serves."""
        if served is not None:
            for chat_id in held.members - set(served):
                held.members.discard(chat_id)
                held.slept = True
                self._seats.pop(chat_id, None)
                forget_scope(chat_id)
        return held.members

    # -- internals ------------------------------------------------------------

    def _memory(self, key: str) -> int | None:
        if self._memory_of is None:
            return None
        try:
            return self._memory_of(key)
        except Exception:
            logger.debug("workspace %s: its memory could not be read", key, exc_info=True)
            return None

    @contextlib.asynccontextmanager
    async def _locked(self, key: str) -> AsyncIterator[None]:
        """Hold ``key``'s lock; the entry goes once nobody holds or waits on it."""
        entry = self._locks.setdefault(key, _KeyLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(key) is entry:
                del self._locks[key]

    @staticmethod
    def _scope(chat_id: str, key: str) -> SandboxScope:
        return scope_for(chat_id, {SCOPE_KEY: key})


__all__ = [
    "HELD_ELSEWHERE",
    "LIVE_HOLD_SECONDS",
    "Joined",
    "MemoryOf",
    "SandboxState",
    "SandboxWork",
    "WorkspaceHost",
]
