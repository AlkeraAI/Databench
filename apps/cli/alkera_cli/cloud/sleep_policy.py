"""Which chats a box may put to sleep, and which it sleeps first.

A box keeps a chat awake until it needs the room: a new chat that finds every
slot taken, or the chats' memory past its pressure line, sleeps the least
recently used idle chat first. Under that sits a backstop for cost hygiene,
twenty-four hours so a chat left at the end of a working day is still warm the
next morning, which only a chat with nothing running and nobody using it ever
reaches.

"Nothing running" is the mirror's whole account (a turn, a background job, a
relay, a question not yet handed over), an ask a person may still answer within
its own window, and, read only when a sleep is being decided, a process a
command left behind in the chat's sandbox, which no job list knows about.
"Used" is the latest of the chat's last output or change in what it owes, the
last reader action (a message, an answer, an open) and the last reader present
on its document.

Two valves sit beside memory: the disk the chats' folders live on, past its own
line, counts as pressure the same way (a box that fills its disk fails every
chat's writes at once), and an operator can turn off sleeping a chat to make a
slot for another (``ALKERA_CLOUD_SLOT_EVICTION=0``), so a new chat waits for a
slot instead.

:class:`SleepPolicy` holds what the box knows about each chat's use and answers
those questions; the service decides when to ask them and does the sleeping.
Nothing here meters or bills anything: a chat's awake time is not billed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from alkera_core.compute.liveness import (
    CHAT_IDLE_MINUTES,
    CHAT_MEMORY_PRESSURE_PERCENT,
    ENV_CHAT_MEMORY_PRESSURE_PERCENT,
)
from alkera_core.schemas.realtime import PresenceFrame

from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.harness.sandbox_processes import SandboxProcess
from alkera_cli.host.backoff import doubled

if TYPE_CHECKING:
    from alkera_cli.cloud.mirror import ChatMirror

logger = logging.getLogger(__name__)

#: The backstop: how long a chat with nothing running and nobody using it keeps
#: its agent server. The platform serves it to its boxes from
#: ``compute_chat_idle_minutes`` as ``ALKERA_CLOUD_CHAT_IDLE_MINUTES``; the older
#: ``ALKERA_CLOUD_MIRROR_IDLE_MINUTES`` is read when that is not set. An idle
#: chat costs a pool box its memory (about 145 MB measured, 256 MB budgeted)
#: and nothing else: no CPU, and no bill.
DEFAULT_MIRROR_IDLE_MINUTES = float(CHAT_IDLE_MINUTES)
#: How full the chats' memory may get, in percent of its limit, before the box
#: sleeps its least recently used idle chats. The chat count is budgeted on
#: what an IDLE agent server holds and a chat running a build grows far past
#: that; with idle chats no longer thinned by a short window, this keeps a burst
#: of busy chats from pushing the kernel into OOM-killing one of them. Served
#: from ``compute_chat_memory_pressure_percent``.
DEFAULT_MEMORY_PRESSURE_PERCENT = float(CHAT_MEMORY_PRESSURE_PERCENT)
#: How long a chat parked on an ask nobody has answered keeps its agent server
#: while no reader comes near it. An ask is durable (it stays in the transcript
#: and the next open re-offers it), so a sleep costs only the live session.
#: Without the window an abandoned permission card pins an agent server, a
#: session and a folder lease for the life of the box. A day: a card raised on
#: a Friday afternoon is still live on Monday morning.
#: ``ALKERA_CLOUD_PARKED_ASK_HOURS`` overrides it; ``0`` turns it off.
DEFAULT_PARKED_ASK_HOURS = 24.0
ENV_PARKED_ASK_HOURS = "ALKERA_CLOUD_PARKED_ASK_HOURS"
#: How long a draining box waits for a chat whose only work is a process left
#: in its sandbox, against the hours it waits for a running turn. A forgotten
#: dev server would otherwise hold a deploy for the whole drain ceiling.
DEFAULT_DRAIN_PROCESS_HOLD_SECONDS = 300.0
ENV_DRAIN_PROCESS_HOLD_SECONDS = "ALKERA_CLOUD_DRAIN_PROCESS_HOLD_SECONDS"
#: How long a chat held only by a process in its sandbox is protected from a
#: sleep for room, counted from the chat's last use (a process can only have
#: been started by a turn, so it is at most this old). Without it a box under
#: pressure slept such a chat half a minute after the agent started a job.
DEFAULT_PROCESS_HOLD_MIN_SECONDS = 300.0
ENV_PROCESS_HOLD_MIN_SECONDS = "ALKERA_CLOUD_PROCESS_HOLD_MIN_SECONDS"
#: How often a draining box re-reads a sandbox that held work: two seconds
#: after the first read, doubling to the cap. A process table is a subprocess,
#: not a flag.
DRAIN_PROBE_BACKOFF_FIRST_SECONDS = 2.0
DRAIN_PROBE_BACKOFF_CAP_SECONDS = 60.0
ENV_MEMORY_PRESSURE_PERCENT = ENV_CHAT_MEMORY_PRESSURE_PERCENT
#: How full the disk the chats' folders live on may get, in percent, before
#: the box sleeps its least recently used idle chats (their folders are handed
#: back, and the workspace sweep puts away what nobody uses). ``0`` turns the
#: valve off.
DEFAULT_DISK_PRESSURE_PERCENT = 90.0
ENV_DISK_PRESSURE_PERCENT = "ALKERA_CLOUD_DISK_PRESSURE_PERCENT"
#: ``0`` / ``false`` / ``off``: never sleep a chat to make a slot for another.
ENV_SLOT_EVICTION = "ALKERA_CLOUD_SLOT_EVICTION"
#: What the service says it needs the room for when it asks for a slot.
SLOT_WHY = "a slot"
_OFF = frozenset({"0", "false", "no", "off"})
MIB = 1024 * 1024


class SandboxReader(Protocol):
    """Reads what runs in a chat's sandbox besides its agent server, by the
    chat's id (its session id): ``None`` when it cannot be read. Blocking; the
    policy calls it off the event loop. ``drop`` forgets a chat that left the
    box. :class:`alkera_cli.harness.sandbox_processes.SandboxProbes` is the
    production one, written by the agent servers' launches."""

    def work(self, session_id: str) -> tuple[SandboxProcess, ...] | None: ...

    def drop(self, session_id: str) -> None: ...


#: ``(working set, limit)`` of the memory the chats compete for, or ``None``.
MemorySample = Callable[[], tuple[int, int] | None]
#: ``(used, total)`` bytes of the disk the chats' folders live on, or ``None``.
DiskSample = Callable[[], tuple[int, int] | None]


class Pressure(NamedTuple):
    """A resource past its line: ``(used, limit)`` in bytes, and which one."""

    used: int
    limit: int
    resource: str = "memory"


def disk_usage_sample(path: Path | str) -> DiskSample:
    """A :data:`DiskSample` of the filesystem holding ``path``."""

    def _sample() -> tuple[int, int] | None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return (usage.used, usage.total)

    return _sample


def _non_negative(raw: str | None, default: float, *, top: float = math.inf) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 <= value <= top else default


def parked_ask_hours_from_env(env: Mapping[str, str] | None = None) -> float:
    """How long a chat parked on an unanswered ask stays warm with no reader.
    ``0`` is kept (an ask holds its session for as long as the box lives);
    anything negative or not a number reads as the default."""
    source = os.environ if env is None else env
    return _non_negative(source.get(ENV_PARKED_ASK_HOURS), DEFAULT_PARKED_ASK_HOURS)


def drain_process_hold_seconds_from_env(env: Mapping[str, str] | None = None) -> float:
    """How long a drain waits for a chat held only by its sandbox's processes.
    ``0`` hands such a chat back at once; anything negative or not a number
    reads as the default."""
    source = os.environ if env is None else env
    return _non_negative(
        source.get(ENV_DRAIN_PROCESS_HOLD_SECONDS), DEFAULT_DRAIN_PROCESS_HOLD_SECONDS
    )


def memory_pressure_percent_from_env(env: Mapping[str, str] | None = None) -> float:
    """The share of the chats' memory past which idle chats are slept, in
    percent. Anything that is not a number in ``(0, 100]`` reads as the
    default: ``100`` is how an operator says "only the kernel's own limit",
    and a typo must not set it at zero, which would sleep every idle chat on
    every pass."""
    source = os.environ if env is None else env
    value = _non_negative(
        source.get(ENV_MEMORY_PRESSURE_PERCENT), DEFAULT_MEMORY_PRESSURE_PERCENT, top=100
    )
    return value if value > 0 else DEFAULT_MEMORY_PRESSURE_PERCENT


def disk_pressure_percent_from_env(env: Mapping[str, str] | None = None) -> float:
    """The share of the chats' disk past which idle chats are slept, in
    percent; ``0`` turns the valve off. Anything else outside ``[0, 100]``
    reads as the default."""
    source = os.environ if env is None else env
    return _non_negative(
        source.get(ENV_DISK_PRESSURE_PERCENT), DEFAULT_DISK_PRESSURE_PERCENT, top=100
    )


def slot_eviction_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether the box sleeps an idle chat to make a slot for a new one: on
    unless :data:`ENV_SLOT_EVICTION` says ``0`` / ``false`` / ``no`` / ``off``."""
    source = os.environ if env is None else env
    return source.get(ENV_SLOT_EVICTION, "").strip().lower() not in _OFF


@dataclass(frozen=True, slots=True)
class SleepSettings:
    """The box-side knobs of the sleep policy, read once from the box's
    environment (``from_env``). The idle window and the parked-ask window stay
    on ``MirrorSettings``, where the platform serves them."""

    memory_pressure_percent: float = DEFAULT_MEMORY_PRESSURE_PERCENT
    drain_process_hold_seconds: float = DEFAULT_DRAIN_PROCESS_HOLD_SECONDS
    process_hold_min_seconds: float = DEFAULT_PROCESS_HOLD_MIN_SECONDS
    #: Off unless read from the box's environment, where it defaults to
    #: :data:`DEFAULT_DISK_PRESSURE_PERCENT`: a policy built in code (a test,
    #: an embedding) never sleeps chats on the host's own disk.
    disk_pressure_percent: float = 0.0
    slot_eviction: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SleepSettings:
        source = os.environ if env is None else env
        return cls(
            memory_pressure_percent=memory_pressure_percent_from_env(source),
            drain_process_hold_seconds=drain_process_hold_seconds_from_env(source),
            process_hold_min_seconds=_non_negative(
                source.get(ENV_PROCESS_HOLD_MIN_SECONDS), DEFAULT_PROCESS_HOLD_MIN_SECONDS
            ),
            disk_pressure_percent=disk_pressure_percent_from_env(source),
            slot_eviction=slot_eviction_from_env(source),
        )


def chat_of_channel(channel: str) -> str | None:
    """The chat a document channel (``doc:chat:<id>``) names, else ``None``."""
    kind, _, chat_id = channel.removeprefix("doc:").partition(":")
    return chat_id if channel.startswith("doc:") and kind == "chat" and chat_id else None


class SleepPolicy:
    """What the box knows about each chat's use, and the answers it gives:
    how long a chat has been idle, whether its sandbox still runs work, and
    whether the chats' memory is past its line."""

    def __init__(
        self,
        *,
        idle_minutes: float,
        parked_ask_hours: float,
        clock: Callable[[], float],
        memory: MemorySample,
        probes: SandboxReader,
        settings: SleepSettings | None = None,
        disk: DiskSample | None = None,
    ) -> None:
        settings = settings or SleepSettings()
        self._disk = disk
        self._disk_percent = settings.disk_pressure_percent
        self._slot_eviction = settings.slot_eviction
        #: Whether the disk was past its line on the last read (said once).
        self._disk_full = False
        self.idle_window = idle_minutes * 60.0
        self._parked_window = parked_ask_hours * 3600.0
        self._pressure_percent = settings.memory_pressure_percent
        self._hold_min = settings.process_hold_min_seconds
        self._clock = clock
        self._memory = memory
        self._probes = probes
        #: Per chat: its output and activity when last seen to change, and when.
        self._active_at: dict[str, tuple[tuple[int, int], ChatActivity, float]] = {}
        #: When a person last used a chat from outside it: present on its
        #: document, or an edit of theirs applied to its files.
        self._presence_at: dict[str, float] = {}
        #: The chats last found with work in their sandbox (said once per hold),
        #: and when it was last seen: the backstop window starts again from it.
        self._held: set[str] = set()
        self._work_seen_at: dict[str, float] = {}
        #: What was last read running in each chat's sandbox.
        self._work_of: dict[str, tuple[SandboxProcess, ...]] = {}
        #: The chats picked to sleep with processes still running, and those
        #: processes: what the chat is told the sleep stopped.
        self._stopping: dict[str, tuple[SandboxProcess, ...]] = {}
        #: Draining: per chat held by its sandbox, ``(held since, next read, backoff)``.
        self._drain_hold_seconds = settings.drain_process_hold_seconds
        self._drain: dict[str, tuple[float, float, float]] = {}
        #: Whether the last pass under pressure found nothing it could sleep.
        self._pressure_stuck = False

    # -- what the box has seen ---------------------------------------------------

    def stamp(self, chat_id: str, mirror: ChatMirror) -> None:
        """The chat was used just now (it was just taken or woken)."""
        self._active_at[chat_id] = (_output_of(mirror), mirror.activity, self._clock())

    def note_activity(self, mirrors: Mapping[str, ChatMirror]) -> None:
        """Stamp every chat whose output moved or that stopped owing something
        since the last pass, so a turn that ends in silence starts its window
        where it ended, not at its last word."""
        now = self._clock()
        for chat_id, mirror in mirrors.items():
            output, activity = _output_of(mirror), mirror.activity
            seen = self._active_at.get(chat_id)
            if seen is None or output != seen[0] or activity is not seen[1]:
                self._active_at[chat_id] = (output, activity, now)
        for chat_id in [c for c in self._active_at if c not in mirrors]:
            del self._active_at[chat_id]

    def hear_presence(self, frame: PresenceFrame, own_peer: str | None) -> None:
        """A presence frame from the gateway. A reader joining, beating or
        moving a caret on a chat's document is using the chat; the box's own
        frames and a reader leaving are not."""
        chat_id = chat_of_channel(frame.channel)
        if chat_id is None or frame.event == "leave":
            return
        if any(peer.peer_id != own_peer for peer in frame.peers):
            self._presence_at[chat_id] = self._clock()

    def note_use(self, chat_id: str) -> None:
        """A person used the chat without a word on its document: an edit of
        theirs (a save from an editor's working copy, a file dropped in the
        drive) was applied to its files. The box sees no presence on a file's
        own document, so the applied write is what it can see."""
        self._presence_at[chat_id] = self._clock()

    def forget(self, chat_id: str) -> None:
        """The chat left this box; what was known about it goes with it."""
        self._active_at.pop(chat_id, None)
        self._presence_at.pop(chat_id, None)
        self._held.discard(chat_id)
        self._work_seen_at.pop(chat_id, None)
        self._work_of.pop(chat_id, None)
        self._stopping.pop(chat_id, None)
        self._drain.pop(chat_id, None)
        self._probes.drop(chat_id)

    # -- the answers -------------------------------------------------------------

    def idle_for(
        self, chat_id: str, mirror: ChatMirror | None, *, with_work: bool = True
    ) -> float | None:
        """How long this chat has gone with nothing running and nobody using
        it, or ``None`` while it is not idle at all.

        Silence is no part of it: a turn may legitimately run for hours saying
        nothing, so a chat is a candidate only when the mirror's account says
        nothing it owns is in flight. A chat only awaiting a person goes cold on
        the parked-ask window instead. A mirror still starting is not idle.
        A folder in motion does not make a chat busy: the sleep waits for the
        folder's custody lock and pushes, and the beats take that lock so often
        that reading it as busy kept chats from ever sleeping. ``with_work`` counts the
        last time a process was seen in the sandbox as use (the backstop does;
        ranking which chat the box needs least does not)."""
        if mirror is None or mirror.state == "starting":
            return None
        seen = self._active_at.get(chat_id)
        if seen is None:
            return None
        activity = mirror.activity
        if activity.holds_a_stop:
            return None
        if activity is ChatActivity.AWAITING_USER:
            return self._parked_ask_idle(mirror)
        used = max(seen[2], mirror.reader_seen_at, self._presence_at.get(chat_id, -math.inf))
        if with_work:
            used = max(used, self._work_seen_at.get(chat_id, -math.inf))
        return self._clock() - used

    def _parked_ask_idle(self, mirror: ChatMirror) -> float | None:
        """How long a chat whose ONLY work is an ask has gone with no reader,
        once that is past the parked-ask window; ``None`` before it is. Past it
        the chat sleeps like any idle one, and an answer given later still
        resumes it, on a fresh session."""
        if self._parked_window <= 0:
            return None
        silent = self._clock() - mirror.reader_seen_at
        if silent < self._parked_window:
            return None
        # Already past its own, far longer window: idle NOW, whatever the idle
        # window is.
        return max(silent, self.idle_window)

    async def _read_work(
        self, chat_id: str, *, retry: bool = False
    ) -> tuple[SandboxProcess, ...] | None:
        """What runs in the chat's sandbox, ``()`` when nothing does, ``None``
        when it could not be read. ``retry`` reads once more after a failure."""
        for _ in range(2 if retry else 1):
            try:
                work = await asyncio.to_thread(self._probes.work, chat_id)
            except Exception:
                logger.exception(
                    "chat %s: could not read what runs in its sandbox",
                    chat_id,
                    extra={"chat_id": chat_id},
                )
                work = None
            if work is not None:
                return work
        return None

    async def holds_work(self, chat_id: str, *, retry: bool = False) -> bool:
        """Whether something other than the agent server still runs in the
        chat's sandbox: what a command left behind in the background, which the
        sleep would kill with the container. The backstop window of a chat
        found holding work starts again when the work ends. A sandbox that
        cannot be read holds nothing: a probe that fails must not pin every
        chat on the box awake for ever."""
        work = await self._read_work(chat_id, retry=retry)
        self._work_of[chat_id] = work or ()
        if not work:
            if chat_id in self._held:
                self._held.discard(chat_id)
                logger.info(
                    "chat %s: nothing runs in its sandbox any more",
                    chat_id,
                    extra={"chat_id": chat_id},
                )
            return False
        self._work_seen_at[chat_id] = self._clock()
        if chat_id not in self._held:
            self._held.add(chat_id)
            logger.info(
                "chat %s stays awake: %d process(es) still run in its sandbox (%s)",
                chat_id,
                len(work),
                _named(work),
                extra={"chat_id": chat_id, "processes": _named(work)},
            )
        return True

    async def pick_victim(
        self, chats: Iterable[str], idle_for: Callable[[str, bool], float | None], *, why: str
    ) -> str | None:
        """The chat to sleep when the box needs the room (``why`` says for
        what): the least recently used idle chat with nothing in its sandbox;
        failing every one of those, the least recently used chat whose only
        hold is a process in its sandbox, so such holds can never fill a box,
        stall another org's chat or leave the rest to the OOM killer. ``None``
        when every chat has a turn, an ask or a flush in flight, and for a
        slot (``why`` is :data:`SLOT_WHY`) when the operator turned slot
        eviction off. A failed read of a sandbox is retried once before it
        counts as "nothing runs"."""
        if why == SLOT_WHY and not self._slot_eviction:
            return None
        ranked = [(s, c) for c in chats if (s := idle_for(c, False)) is not None]
        held: list[str] = []
        for _, chat_id in sorted(ranked, reverse=True):
            if not await self.holds_work(chat_id, retry=True):
                if idle_for(chat_id, False) is not None:
                    self._stopping.pop(chat_id, None)
                    return chat_id
            else:
                held.append(chat_id)
        for chat_id in held:
            if (unused := idle_for(chat_id, False)) is not None and unused >= self._hold_min:
                logger.warning(
                    "chat %s is put to sleep although a process still runs in its sandbox: "
                    "the box needs %s and every other chat has a turn, an ask or a flush "
                    "in flight, or is held the same way and used more recently",
                    chat_id,
                    why,
                    extra={"chat_id": chat_id, "why": why},
                )
                self._stopping[chat_id] = self._work_of.get(chat_id, ())
                return chat_id
        return None

    def stopped_by_sleep(self, chat_id: str) -> tuple[SandboxProcess, ...]:
        """The processes still running in ``chat_id``'s sandbox when it was
        picked to sleep for room (``()`` when none were), forgotten once read."""
        return self._stopping.pop(chat_id, ())

    async def drain_releasable(self, quiet: Iterable[str]) -> list[str]:
        """Of the quiet chats a draining box holds, the ones to hand back now:
        those with nothing in their sandbox, and those held only by a process
        there for longer than the drain's process budget. A held chat's sandbox
        is re-read on a doubling interval, not on every drain poll."""
        now = self._clock()
        due: list[str] = []
        out: list[str] = []
        for chat_id in quiet:
            hold = self._drain.get(chat_id)
            if hold is not None and now - hold[0] >= self._drain_hold_seconds:
                logger.warning(
                    "chat %s is handed back with a process still running in its sandbox: "
                    "the box is draining and %.0f s is as long as it waits for one",
                    chat_id,
                    self._drain_hold_seconds,
                    extra={"chat_id": chat_id},
                )
                out.append(chat_id)
            elif hold is None or now >= hold[1]:
                due.append(chat_id)
        holding = await asyncio.gather(*(self.holds_work(c) for c in due))
        for chat_id, holds in zip(due, holding, strict=True):
            if not holds:
                out.append(chat_id)
                continue
            since, _, backoff = self._drain.get(chat_id, (now, now, 0.0))
            backoff = doubled(
                backoff,
                floor=DRAIN_PROBE_BACKOFF_FIRST_SECONDS,
                cap=DRAIN_PROBE_BACKOFF_CAP_SECONDS,
            )
            self._drain[chat_id] = (since, now + backoff, backoff)
        return out

    def pressure(self) -> Pressure | None:
        """The resource past its line: the chats' memory first, then the disk
        their folders live on; ``None`` when neither is (or neither can be
        read)."""
        sample = self._memory()
        if sample is not None and _past(sample, self._pressure_percent):
            return Pressure(sample[0], sample[1], "memory")
        return self._disk_pressure()

    def _disk_pressure(self) -> Pressure | None:
        if self._disk is None or self._disk_percent <= 0:
            return None
        sample = self._disk()
        full = sample is not None and _past(sample, self._disk_percent)
        if full != self._disk_full:
            self._disk_full = full
            if full and sample is not None:
                logger.warning(
                    "the chats' disk is at %d of %d MiB, past its %.0f%% line; idle chats are "
                    "put to sleep to give it back",
                    sample[0] // MIB,
                    sample[1] // MIB,
                    self._disk_percent,
                )
            else:
                logger.info("the chats' disk is back under its line")
        if not full or sample is None:
            return None
        return Pressure(sample[0], sample[1], "disk")

    def clear_stuck(self) -> None:
        """The pressure cleared, or something could be slept again."""
        self._pressure_stuck = False

    def say_stuck(self, pressure: Pressure) -> None:
        """Every chat is busy under pressure: said once until it clears."""
        if not self._pressure_stuck:
            logger.warning(
                "the chats' %s is at %d of %d MiB and every chat this box serves has "
                "something running; none is put to sleep",
                pressure.resource,
                pressure.used // MIB,
                pressure.limit // MIB,
            )
            self._pressure_stuck = True


def _past(sample: tuple[int, int], percent: float) -> bool:
    used, limit = sample
    return limit > 0 and used * 100 >= limit * percent


def _named(work: tuple[SandboxProcess, ...]) -> str:
    return ", ".join(f"{p.pid} {p.command}".strip() for p in work[:5])


def _output_of(mirror: ChatMirror) -> tuple[int, int]:
    """What a chat has put on the wire: durable events and streamed chunks."""
    return (mirror.published_count, mirror.chunk_count)


__all__ = [
    "DEFAULT_DISK_PRESSURE_PERCENT",
    "DEFAULT_DRAIN_PROCESS_HOLD_SECONDS",
    "DEFAULT_MEMORY_PRESSURE_PERCENT",
    "DEFAULT_MIRROR_IDLE_MINUTES",
    "DEFAULT_PARKED_ASK_HOURS",
    "DEFAULT_PROCESS_HOLD_MIN_SECONDS",
    "ENV_DISK_PRESSURE_PERCENT",
    "ENV_MEMORY_PRESSURE_PERCENT",
    "ENV_PARKED_ASK_HOURS",
    "ENV_SLOT_EVICTION",
    "SLOT_WHY",
    "DiskSample",
    "MemorySample",
    "Pressure",
    "SandboxReader",
    "SleepPolicy",
    "SleepSettings",
    "chat_of_channel",
    "disk_pressure_percent_from_env",
    "disk_usage_sample",
    "drain_process_hold_seconds_from_env",
    "memory_pressure_percent_from_env",
    "parked_ask_hours_from_env",
    "slot_eviction_from_env",
]
