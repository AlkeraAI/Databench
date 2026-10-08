"""The cloud-mirror service: which chats this machine serves, and their mirrors.

On start the service refuses to run without the opencode harness or without the
facts that register the machine (a box that cannot register must not serve chats
it could never publish). It registers BEFORE it opens the socket, so the first
ticket already asserts the machine id the gateway grants writes to. A refused or
unreachable registration is retried by the machine loop while the socket runs as
a reader, and the socket is rebound once an id arrives. Nothing is served until
the box knows that id.

Chats bound to this machine are discovered two ways at once: the org's
invalidation stream (``chat.updated`` frames) and a slow poll of the chat list,
since the stream is best-effort. A newly bound chat gets a :class:`ChatMirror`;
one that is gone or moved is stopped; one the gateway will not let this box
publish is refused visibly.

Folder lease heartbeats run on their own task at each lease's granted cadence.
Slow work (taking folders, pushing a turn's files, retrying owed sleeps) runs on
a separate task so it can never delay a beat past the lease's lapse.

The service also keeps the workspace's source and schema cards current through
the data plane registered on :data:`alkera_cli.cloud.box_data.BOX_DATA`, so each
chat's agent is told which connection to read and answers from the real schema.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import functools
import logging
import math
import os
import platform
import random
import socket
import threading
import time
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from alkera_core.auth.machine_token import FATAL_MACHINE_REFUSALS, looks_like_machine_token
from alkera_core.chat_refusals import ChatRefusalKind
from alkera_core.compute.liveness import (
    DRAIN_CEILING_SECONDS,
    ENV_CHAT_IDLE_MINUTES,
    HEARTBEAT_INTERVAL_SECONDS,
    STOP_EXIT_SECONDS,
    drain_ceiling_seconds,
)
from alkera_core.events.types import CHAT_DELETED_REASON, EventType
from alkera_core.files.history import LEASE_CHANGED_INBOUND, LEASE_CHANGED_REPORT
from alkera_core.host_resources import effective_cpus, effective_memory_bytes
from alkera_core.schemas.realtime import PresenceFrame
from alkera_notebook.actors import ActingFor

from alkera_cli.box_status import STATUS_SCHEMA, BoxStatus, write_status
from alkera_cli.cloud.activity import activity_counts
from alkera_cli.cloud.attachments import (
    MaterializedAttachments,
)
from alkera_cli.cloud.box_data import DEFAULT_SCHEMA_REFRESH_SECONDS, SchemaCards, box_data
from alkera_cli.cloud.budget import TurnBudget
from alkera_cli.cloud.chat_fs import install_sandbox_identity
from alkera_cli.cloud.chat_slice import bound_chat_slice
from alkera_cli.cloud.faults import refusal_kind
from alkera_cli.cloud.folder import (
    ChatFolders,
    chat_folder_drive,
)
from alkera_cli.cloud.folder_returns import FolderReturns
from alkera_cli.cloud.folder_takes import FolderTakes
from alkera_cli.cloud.limits import (
    chat_list_pages,
    folder_beat_floor_seconds,
    folder_beat_seconds,
    folder_beat_timeout_seconds,
    publish_landing_seconds,
)
from alkera_cli.cloud.machine_pulse import MachinePulse
from alkera_cli.cloud.machine_requests import MachineRequests
from alkera_cli.cloud.machine_resources import chats_memory
from alkera_cli.cloud.mirror import (
    CLOUD_PERMISSION_MODES,
    ChatMirror,
    ChatMirrorRefusedError,
    ChatMirrorStoppedError,
    pinned_model,
)
from alkera_cli.cloud.mirror_factory import build_mirror
from alkera_cli.cloud.org_admission import OrgAdmission
from alkera_cli.cloud.pressure_notice import pressure_sleep_sentence
from alkera_cli.cloud.publisher_identity import (
    DEFAULT_PROVIDER,
    MachineIdentity,
    PublishingRefusal,
    require_machine_identity,
)
from alkera_cli.cloud.publisher_report import Reporter
from alkera_cli.cloud.rest import (
    STREAM_OPENED,
    BoxCredential,
    CloudApiError,
    CloudRestClient,
    stored_token_reader,
)
from alkera_cli.cloud.restart_wait import wait_for_in_flight
from alkera_cli.cloud.sleep_policy import (
    DEFAULT_MIRROR_IDLE_MINUTES,
    DEFAULT_PARKED_ASK_HOURS,
    MemorySample,
    SandboxReader,
    SleepPolicy,
    SleepSettings,
    disk_usage_sample,
)
from alkera_cli.cloud.start_failures import (
    REFUSED_SLOT_WAIT,
    StartFailure,
    gateway_refusal,
    slot_admission,
)
from alkera_cli.cloud.start_retry import StartRetry
from alkera_cli.cloud.stop_tasks import settle
from alkera_cli.cloud.take_order import (
    RECENT_ACTIVITY_SECONDS,
    in_order_of_need,
    left_asleep,
    somebody_waits,
    untouched_for,
)
from alkera_cli.cloud.transport import CloudSocket
from alkera_cli.cloud.turn_inputs import TurnInputs
from alkera_cli.cloud.workspace_host import Joined, WorkspaceHost
from alkera_cli.cloud.workspace_seat import is_workspace_key, workspace_of_key
from alkera_cli.files import folder_fence
from alkera_cli.harness import HarnessRuntime, HarnessUnavailableError
from alkera_cli.harness.permission_mode import PermissionMode
from alkera_cli.harness.prewarm import start_prewarm_in_background
from alkera_cli.harness.registry import OPENCODE_HARNESS
from alkera_cli.harness.sandbox_processes import SandboxProbes
from alkera_cli.harness.workspace_sandbox import (
    release_workspace_sandbox,
    workspace_memory_mb,
)
from alkera_cli.host import paths as alkera_paths
from alkera_cli.host import version_info
from alkera_cli.host.backoff import ReconnectBackoff, doubled
from alkera_cli.host.cancellation import raise_if_cancel_requested

if TYPE_CHECKING:
    from alkera_cli.notebooks.box_compose import BoxNotebookSlot

logger = logging.getLogger(__name__)

#: The platform's answer to a beat from a process that is no longer the one
#: registered on the machine row.
STALE_INSTANCE_CODE = "machine_stale_instance"

ENV_API_URL = "ALKERA_API_URL"
ENV_MACHINE_NAME = "ALKERA_MACHINE_NAME"
# The cloud judges this box unreachable after three of these go missing, so the
# beat is read from the one place that states the contract rather than spelled
# again here — a box that beats slower than the window is a healthy machine the
# banner calls dead.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = float(HEARTBEAT_INTERVAL_SECONDS)
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_MISSING_ROUTE_RETRY_SECONDS = 60.0
REFUSAL_BACKOFF_CAP_SECONDS = 300.0
"""Ceiling on the wait between re-asking after a REFUSED registration. A
refusal (no grant, no credit, the ceiling in use) is an answer that only a
human changes, and every re-ask puts a ``refused`` frame in every open browser
and a row on the org's audit trail — so the retry backs off to five minutes
rather than hammering at the heartbeat interval."""
SIGNED_OUT_BACKOFF_CAP_SECONDS = 300.0
"""Ceiling on the wait between beats while the server refuses this box's
session. Only a person replacing the token changes that answer, so the box asks
again on an exponential backoff rather than at the heartbeat interval."""
SIGNED_OUT_GRACE_BEATS = 2
"""How many beats in a row the session must be refused before the box puts the
chats it holds to sleep. More than one, so a single stray 401 does not cost
every reader their open session; few, because every call those chats make is
refused too and their folder leases lapse within a minute."""
SIGNED_OUT_SENTENCE = (
    "this box's session was refused by the server ({cause}): the token in {path} expired or "
    "was revoked. The box takes no new chats, puts the ones it holds to sleep, and asks again "
    "on a backoff of up to five minutes. To fix it, run `alkera login` on the box as the "
    "account it serves (or write a fresh session into {path}); the box picks the new token up "
    "without a restart."
)
#: What renews this box's session when the server refuses it: ``True`` when a
#: new credential is in place and the refused call is worth making again at
#: once. None is wired today; a token the operator replaces on disk is read back
#: by the REST client on its own.
SessionRenewer = Callable[[], Awaitable[bool]]
CHAT_UPDATED = EventType.CHAT_UPDATED.value
TEAM_CONNECTION_UPDATED = EventType.TEAM_CONNECTION_UPDATED.value
MEMBERSHIP_CHANGED = EventType.MEMBERSHIP_CHANGED.value
#: A lease's live plane moved: somebody wrote into a folder this box holds, and
#: what they wrote is waiting for the box to take it. Spelled here rather than
#: read off the event enum because the server half of the live plane and this
#: half land separately, and a box that does not recognise the name would
#: simply wait for its next beat instead of taking the file at once.
FILE_LEASE_CHANGED = "file_lease.changed"

#: How long a checkpoint push that did not land waits before the same state is
#: pushed again, doubling per miss up to :data:`PUSH_RETRY_CAP_SECONDS`. A new
#: turn's end is pushed at once whatever the wait; the hand-back pushes too.
PUSH_RETRY_FIRST_SECONDS = 60.0
PUSH_RETRY_CAP_SECONDS = 1800.0

#: The platform's cap on the channels one socket may hold, reached because this
#: box mirrors that many chats at once. It is pressure on this box's capacity,
#: not a verdict on the chat: the box learns the cap from where it bit, puts
#: the idlest chat it serves to sleep to make the slot, and takes the chat
#: again. Nothing is put on the chat, whose reader keeps waiting for a slot.
CHANNEL_CAP_REFUSAL = "too_many_channels"
#: What the reader is told when this box will not publish their chat. A refusal
#: code is the publishing service's own word for it and reads as machinery, so
#: it never reaches the banner; the code is logged instead.
#: How often a box beats for the chat folders it holds when the lease's grant
#: named no cadence of its own, and the same number the mount record falls back
#: to when a grant carries no ``heartbeatEvery``. Well inside what any
#: deployment serves: the cadence on a grant is ``files_lease_ttl_seconds``
#: over ``HEARTBEATS_PER_TTL``, which on the shipped 600 s TTL and four beats
#: per TTL is 150 s, so a box falling back to this one beats an order of
#: magnitude more often than the lease needs rather than less. A grant that
#: DOES name a cadence is always obeyed instead (see
#: ``_folder_beat_interval``), so a deployment that shortens its TTL is
#: followed down without any of this moving. The three of them — the fallback
#: cadence, the floor under a served one, and one beat's own budget — are read
#: from the cloud settings block.
#: One folder beat's own budget, and it has to be shorter than the cadence.
#:
#: A beat runs on a thread and carries the Files client's request timeout,
#: which is two minutes — sized for pulling and pushing a whole folder, not for
#: a one-request beat. A drive that accepts the connection and never answers
#: held a beat for all of it, and the pass that was keeping every OTHER chat's
#: lease alive waited behind it. Abandoned at this budget instead, well under
#: the shortest cadence a lease is ever granted on, so a hung beat costs its
#: How long ``stop`` gives a cancelled loop task to end before it is named and
#: left behind. A task that has not ended by then is parked on an await that
#: never handed the cancellation back, and waiting longer changes nothing —
#: while a box that cannot stop is a box its supervisor cannot restart: the
#: machine beats go on, the leases stay held, every chat on it is stranded.
STOP_TASK_GRACE_SECONDS = 5.0
#: How often, inside that grace, the cancel is asked again. One cancel can be
#: lost: a library that cancels its own scope on the same tick (anyio's
#: connector does, on every connect that lands) turns both requests into one
#: exception, uncancels its own and swallows it, and the task runs on with the
#: request still counted against it. The next ask lands in a plain await.
STOP_CANCEL_EVERY_SECONDS = 1.0
#: How long ``stop`` gives each chat's hand-back — the session down, the folder
#: pushed and released, the row marked asleep — before it is cut short. Per
#: chat, several at a time: one drive that never answers costs its own chat
#: the clean sleep (the lease lapses on the TTL) and nothing else its stop.
STOP_RELEASE_BUDGET_SECONDS = 30.0

#: Why this box puts a chat to sleep, in the server's chat-end vocabulary
#: (``alkera_core.objects.chat_end.ChatEndReason``): it went quiet, the box
#: needed its slot, or the box is draining or stopping.
IDLE_ENDING = "idle"
EVICTED_ENDING = "evicted"
DRAINED_ENDING = "drained"


def _end_seq_of(chat: Mapping[str, Any]) -> int:
    """The server's count of endings on a chat row; 0 on a server without it."""
    raw = chat.get("end_seq")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


#: How long a box told to stop waits for the turns it still holds before it
#: puts what is left to sleep and exits. A drain has no natural end — a turn
#: may run for hours, and a wedged agent would hold the box for ever — so the
#: ceiling exists for one reason: an operator must be able to finish a deploy.
#: It is not a limit on how long an answer may take; six hours is past any
#: ordinary turn. ``ALKERA_CLOUD_DRAIN_CEILING_SECONDS`` overrides it, and the
#: platform default is stated once in ``settings.compute_drain_ceiling_seconds``.
DEFAULT_DRAIN_CEILING_SECONDS = float(DRAIN_CEILING_SECONDS)
#: How often a drain looks again at what it is still holding. Short enough that
#: the box exits promptly once the last turn ends, long enough to cost nothing.
DRAIN_POLL_SECONDS = 2.0

#: How long the take pass waits for a chat's owed turn to start before it
#: takes the next chat. Bounds what one slow chat costs every chat behind it.
OWED_TURN_START_SECONDS = 30.0
#: A box on its machine credential reads connections per chat it holds, so a
#: chat newly taken here is owed a re-read of them before its first turn rather
#: than at the next schema tick. The re-read runs once no chat has been taken
#: for this long, so a boot that takes sixty chats reads the connections once …
CONNECTIONS_AFTER_TAKE_QUIET_SECONDS = 1.0
#: … and at the latest this long after the first take it owes, so a long run
#: of takes cannot hold back the chats already taken.
CONNECTIONS_AFTER_TAKE_LATEST_SECONDS = 10.0
#: How long a take waits for that re-read before it spawns the chat's session.
#: The agent lists its tools once, as it connects: a read that lands after the
#: spawn leaves the chat's first turn without its owner's warehouse tools. The
#: take itself still returns — a read that has not landed by then costs the
#: first turn those tools and nothing more.
FIRST_SPAWN_CONNECTIONS_WAIT_SECONDS = 8.0
FIRST_SPAWN_CONNECTIONS_POLL_SECONDS = 0.1
#: How long a pass runs on what it listed before it reads the newest page
#: again for a chat bound meanwhile that owes a turn. A start-up pass over
#: hundreds of chats runs for many minutes; a chat created and spoken to in
#: that time is on none of the pages the pass read.
PASS_RELIST_SECONDS = 10.0
#: How often a holder of the take lock that is waiting on a take looks up — at
#: the chats frames have named since, and at whether the newest page is due to
#: be read again. Frames also wake it at once; this bounds the rest.
PASS_WAKE_SECONDS = 1.0
#: How many chats are taken at once BESIDE the pass's own: the ones a frame
#: named and the ones the re-read found owing a turn. They are news about one
#: chat each and are taken at once rather than after whatever the pass is
#: waiting on — a folder take through the box's tunnel can run for minutes. The
#: bound keeps a burst of frames from opening a folder take per chat at once.
TAKES_BESIDE_THE_PASS = 4
#: How many chats a frame names are taken at once, in a lane of their own. A
#: frame is a reader who just wrote to a chat, while the takes beside the pass
#: may all be held by folder pulls stuck on an unreachable origin. The lane
#: keeps a frame from ever queueing behind a take nobody is waiting on.
NAMED_TAKES_AT_ONCE = 2
#: How many chats' turn-end upkeep runs at once. A turn's files leave for the
#: drive the moment the turn ends rather than on the next poll tick; the bound
#: keeps a box whose chats all finish together from pushing every folder at once.
TURN_END_UPKEEPS_AT_ONCE = 4

#: Set to ``1`` by the box's supervisor: a stop signal then means "restart in
#: place", not "hand this box's chats on".
ENV_DAEMON_SUPERVISED = "ALKERA_DAEMON_SUPERVISED"
#: A file the supervisor creates before it stops the daemon for good (the
#: supervisor itself is going away). Present at the signal, the stop is an
#: ordinary drain even under supervision.
ENV_DAEMON_FINAL_STOP_FILE = "ALKERA_DAEMON_FINAL_STOP_FILE"
#: How long past its drain ceiling ANY stop may take to END the process before
#: it is ended hard (a supervised restart past its short ceiling, an ordinary
#: stop past the configured one). The interpreter waits at exit for worker
#: threads that may never return, and a box stuck there is out of placement.
#: Ending hard is safe: the supervisor starts the next process at once, which
#: restarts the turns and reclaims the locks by the dead pid's liveness, and a
#: chat this box could not hand back is re-placed once its lease lapses. Every
#: thread's stack goes to the log first, the one clue such a hang leaves. The
#: node's service unit waits longer than this, so the daemon always ends itself
#: before it is killed.
RESTART_HARD_STOP_SECONDS = float(STOP_EXIT_SECONDS)


def arm_hard_stop(seconds: float, action: Callable[[], None]) -> threading.Timer:
    """Run ``action`` on a daemon thread after ``seconds`` unless the process
    ends first — the production seam behind :class:`CloudMirrorService`'s hard
    stop, which a test replaces with a recorder."""
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


def stop_hard(reason: str) -> None:
    """End the process now: say why, dump every thread's stack to the log, and
    exit without waiting for anything — a thread that never returns is what
    this is for."""
    logger.warning(
        "%s; %d thread(s) alive — every thread's stack follows, then the process ends so "
        "the supervisor restarts it",
        reason,
        threading.active_count(),
    )
    _flush_logs()
    faulthandler.dump_traceback(all_threads=True)
    _flush_logs()
    os._exit(0)


def _flush_logs() -> None:
    """Push what the log handlers hold, as far as they let us.

    A handler whose stream is already gone — closed by whoever captured stderr
    before this ran — raises on flush, and an exception here would leave the
    process alive: the one outcome :func:`stop_hard` exists to rule out.
    """
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except (OSError, ValueError):
            continue


OPENCODE_MISSING_MESSAGE = (
    "The cloud mirror needs the opencode harness and it is not available on this machine: "
    "no binary resolved. Stage it with `make opencode-binary`, point ALKERA_OPENCODE_BIN at "
    "one, or run from a build that bundles it."
)

#: How many chats one box serves at once when nothing else says: no cap. A chat
#: the box holds back is a chat whose reader waits with no way to tell it apart
#: from one nobody is serving, and the idle close already hands back every
#: server nothing is using. ``ALKERA_CLOUD_MAX_MIRRORS`` sets a cap when an
#: operator wants one; only then is any chat ever held back.
DEFAULT_MAX_MIRRORS: int | None = None
#: The narrowest and widest a box's own hardware is allowed to argue for. Below
#: two a box could serve one chat at a time, which is not a box; above twelve a
#: big instance would claim a capacity its memory cannot hold, and capacity is
#: what placement spreads a shared pool by.
MIN_MAX_MIRRORS = 2
MAX_MAX_MIRRORS = 12
#: What a box reports as its capacity when no cap is configured — placement
#: spreads a shared pool by a number, and "no local cap" is not one.
DEFAULT_REPORTED_CAPACITY = 6
#: Refusals of a claim or a beat that no retry can change: the credential is
#: gone (revoked, rotated away, never minted) or was never presented. The box
#: stops rather than re-asking for ever - and stopping is what hands its chats
#: back. The codes are the backend's own (``alkera_core.auth.machine_token``):
#: a set spelled here would drift from what the server actually answers, and a
#: box whose refusal no longer matched would retry a dead credential for ever.
FATAL_CLAIM_REFUSALS = FATAL_MACHINE_REFUSALS
#: What is logged, and reported as the reason the box gave up, when the
#: platform takes its credential away. Says what an operator has to do.
CREDENTIAL_REFUSED_SENTENCE = (
    "This box's machine credential was refused ({code}), so it is handing its chats back and "
    "stopping. Mint a new credential in the admin console and put it in the box's secret."
)

#: The idle window's older name, still read when the platform's own
#: (``ALKERA_CLOUD_CHAT_IDLE_MINUTES``) is not set: an operator who set it on a
#: box by hand keeps that window.
ENV_MIRROR_IDLE_MINUTES = "ALKERA_CLOUD_MIRROR_IDLE_MINUTES"
ENV_MAX_MIRRORS = "ALKERA_CLOUD_MAX_MIRRORS"
#: What one agent server is budgeted while its chat sits idle, and what the
#: daemon keeps for itself, in MiB. An idle opencode server holds about 145 MB
#: of its own and a daemon serving forty chats about 460 MB; a turn grows the
#: server and spawns tools beside it, which is what the headroom is for. A box
#: past its memory limit thrashes and crashes, and each restart re-takes every
#: chat, so it never reaches the idle window that would thin them. A box takes
#: no more servers than its memory holds; past that the least recently active
#: idle chat sleeps first, exactly as under a configured cap.
#: ``ALKERA_CLOUD_AGENT_MEMORY_MB`` re-budgets a server; ``ALKERA_CLOUD_MEMORY_LIMIT_MB``
#: states the memory outright, for a container whose limit sits on a cgroup it
#: cannot read.
DEFAULT_AGENT_MEMORY_MB = 256
DAEMON_MEMORY_RESERVE_MB = 1024
ENV_AGENT_MEMORY_MB = "ALKERA_CLOUD_AGENT_MEMORY_MB"
ENV_MEMORY_LIMIT_MB = "ALKERA_CLOUD_MEMORY_LIMIT_MB"
MIB = 1024 * 1024


def _positive(raw: str | None, default: float) -> float:
    """A positive, finite number, or the default. ``0`` never means "unlimited"
    — a box told to keep zero mirrors would serve nothing at all — and neither
    does ``inf``, which parses as a float and is not a window anyone meant."""
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 and math.isfinite(value) else default


def default_max_mirrors(cpu_count: int | None = None) -> int:
    """How many chats a box of this size serves at once when nothing sets it.

    One agent server per vCPU this process may use (its cgroup's CPU quota,
    else the cores it may run on), held between two and twelve: an agent server
    is CPU-bound while a turn runs, so a box that admits more chats than it has
    cores makes every reader's turn slower rather than serving more of them. A
    host that cannot say how many cores it has keeps the small default.
    """
    cores = int(effective_cpus()) if cpu_count is None else cpu_count
    if not cores or cores < 1:
        return DEFAULT_REPORTED_CAPACITY
    return max(MIN_MAX_MIRRORS, min(MAX_MAX_MIRRORS, cores))


def _max_mirrors(raw: str | None) -> int | None:
    """A configured cap on the chats one box serves at once, or ``None`` for no
    cap. Anything that is not a positive whole number — unset, blank, junk, zero,
    negative — is not a cap anyone meant, and reads as no cap rather than as
    "serve nothing"."""
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        # `inf` is not a whole number of chats, and `int()` refuses it with an
        # OverflowError that would take the box down before it served anything.
        return None
    return int(value) if value >= 1 else None


def memory_max_mirrors(
    limit_bytes: int | None,
    *,
    agent_bytes: int = DEFAULT_AGENT_MEMORY_MB * MIB,
    reserve_bytes: int = DAEMON_MEMORY_RESERVE_MB * MIB,
) -> int | None:
    """How many agent servers ``limit_bytes`` of memory holds beside the daemon:
    ``None`` when the memory is unknown, and never fewer than two — a box that
    cannot hold two is a slow box, not an absent one."""
    if limit_bytes is None or agent_bytes <= 0:
        return None
    return max(MIN_MAX_MIRRORS, (limit_bytes - reserve_bytes) // agent_bytes)


def memory_cap_from_env(
    env: Mapping[str, str] | None = None, *, limit_bytes: int | None = None
) -> int | None:
    """The most agent servers this host's memory holds, or ``None`` when it
    cannot be known. ``ALKERA_CLOUD_MEMORY_LIMIT_MB`` states the memory outright
    and wins over what the host reports; ``ALKERA_CLOUD_AGENT_MEMORY_MB`` is what
    one server is budgeted. ``limit_bytes`` stands in for the host's word."""
    source = os.environ if env is None else env
    stated = _max_mirrors(source.get(ENV_MEMORY_LIMIT_MB))
    if stated is not None:
        limit: int | None = stated * MIB
    elif limit_bytes is not None:
        limit = limit_bytes
    else:
        limit = effective_memory_bytes()
    agent_mb = _positive(source.get(ENV_AGENT_MEMORY_MB), DEFAULT_AGENT_MEMORY_MB)
    return memory_max_mirrors(limit, agent_bytes=int(agent_mb * MIB))


def memory_limit_from_env(
    env: Mapping[str, str] | None = None, *, limit_bytes: int | None = None
) -> int | None:
    """This box's memory in bytes, read as :func:`memory_cap_from_env` reads
    it: ``ALKERA_CLOUD_MEMORY_LIMIT_MB`` when stated, else ``limit_bytes``, else
    what the host reports."""
    source = os.environ if env is None else env
    stated = _max_mirrors(source.get(ENV_MEMORY_LIMIT_MB))
    if stated is not None:
        return stated * MIB
    return limit_bytes if limit_bytes is not None else effective_memory_bytes()


def bound_chats_memory(env: Mapping[str, str] | None = None) -> int | None:
    """Cap the sandboxed chats together at this box's memory less the daemon's
    reserve, so the kernel's OOM kills a chat and never the daemon. Returns the
    bytes set, or ``None`` when this box has no chat slice to bound."""
    return bound_chat_slice(
        memory_limit_from_env(env), reserve_bytes=DAEMON_MEMORY_RESERVE_MB * MIB
    )


def drain_ceiling_from_env(env: Mapping[str, str] | None = None) -> float:
    """How long a drain waits for the turns the box still holds.

    ``0`` is meaningful and is kept: an operator who wants a box to hand its
    chats back at once — a deploy that cannot wait — says so with a zero.
    Anything that is not a positive finite number reads as the default, so a
    typo in a deploy's environment cannot silently turn a drain into a cut.
    """
    return drain_ceiling_seconds(os.environ if env is None else env)


def supervision_from_env(env: Mapping[str, str] | None = None) -> tuple[bool, Path | None]:
    """``(supervised, final stop file)``: whether a supervisor restarts this
    daemon when it exits, and the file that says a stop is final anyway."""
    source = os.environ if env is None else env
    supervised = (source.get(ENV_DAEMON_SUPERVISED) or "").strip() == "1"
    raw = (source.get(ENV_DAEMON_FINAL_STOP_FILE) or "").strip()
    return supervised, (Path(raw) if raw else None)


def mirror_limits_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[float, int | None]:
    """``(idle minutes, max mirrors)`` — how long an idle chat keeps its agent
    server and how many chats one box serves at once. Both are settings so a
    bigger box can be told that it is bigger; unset, the second is no cap at
    all. The idle window is read from the platform's name first and the older
    one after it, each falling through when it is unusable."""
    source = os.environ if env is None else env
    legacy = _positive(source.get(ENV_MIRROR_IDLE_MINUTES), DEFAULT_MIRROR_IDLE_MINUTES)
    return (
        _positive(source.get(ENV_CHAT_IDLE_MINUTES), legacy),
        _max_mirrors(source.get(ENV_MAX_MIRRORS)),
    )


async def _first_of(*events: asyncio.Event) -> None:
    """Return as soon as any of them is set."""
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def machine_agent_id(name: str) -> str:
    """A machine name as an agent id (``[A-Za-z0-9][A-Za-z0-9._:-]{0,127}``)."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in name.strip())
    cleaned = cleaned.lstrip("._:-") or "machine"
    return f"machine:{cleaned}"[:128]


@dataclass(frozen=True, slots=True)
class MirrorSettings:
    """Everything the service needs, passed in — never read from a global."""

    api_url: str
    token: str
    project_dir: Path
    machine_name: str
    budget: TurnBudget = field(default_factory=TurnBudget)
    user_id: str = ""
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS
    missing_route_retry: float = DEFAULT_MISSING_ROUTE_RETRY_SECONDS
    schema_refresh_interval: float = DEFAULT_SCHEMA_REFRESH_SECONDS
    #: See ``mirror_limits_from_env`` — the two limits that keep a box's agent
    #: servers bounded.
    mirror_idle_minutes: float = DEFAULT_MIRROR_IDLE_MINUTES
    max_mirrors: int | None = DEFAULT_MAX_MIRRORS
    #: See ``memory_cap_from_env`` — the most agent servers this box's memory
    #: holds, bounding the chats served at once beside any configured cap.
    #: ``None`` when the memory could not be read.
    memory_max_mirrors: int | None = None
    #: See ``parked_ask_hours_from_env`` — how long an ask nobody answers keeps
    #: the chat's agent server. ``0`` means it keeps it for ever.
    parked_ask_hours: float = DEFAULT_PARKED_ASK_HOURS
    #: See ``drain_ceiling_from_env`` — how long a box told to stop waits for
    #: the turns it holds before it hands the rest back and exits.
    drain_ceiling_seconds: float = DEFAULT_DRAIN_CEILING_SECONDS
    #: The box-side knobs of the sleep policy (``cloud/sleep_policy.py``).
    sleep: SleepSettings = field(default_factory=SleepSettings)
    #: See ``supervision_from_env`` — a supervised daemon's stop is a restart
    #: in place: every lease is kept and nothing is handed back.
    supervised: bool = False
    final_stop_file: Path | None = None
    #: See ``box_status.status_file_from_env`` — where the daemon writes what it reported
    #: on its last beat the server took. ``None`` writes nothing.
    status_file: Path | None = None
    #: What registers the box: the provider, the pod it runs on and the
    #: catalog code of its flavor. Provisioning knows them; nothing here
    #: guesses them, and a box missing one refuses to start.
    provider: str = DEFAULT_PROVIDER
    provider_pod_id: str = ""
    machine_type_code: str = ""
    #: The platform machine credential, when the platform runs this box. With
    #: one the box CLAIMS the machine the credential names instead of
    #: registering under its org's grant, and serves whatever orgs the backend
    #: then binds to it. Out of the repr: these settings are logged whole.
    machine_credential: str = field(default="", repr=False)
    #: The daemon build this box runs, reported on the claim and every beat so
    #: an admin can see which boxes are behind a release without opening one.
    #: What the box reports as its daemon: the package version plus the build
    #: or release it runs when the environment names one, so the console can
    #: tell two nodes on the same version but different builds apart.
    daemon_version: str = field(default_factory=version_info.daemon_version)
    #: An org worker's org and the machine its supervisor claimed: the worker
    #: claims nothing and beats nothing, and serves only rows naming its org.
    org_id: str | None = None
    machine_id: str | None = None

    @staticmethod
    def machine_name_from_env(env: dict[str, str] | None = None) -> str:
        """The box's name: what provisioning set, else this host's own name.

        ``platform.node()`` rather than ``os.uname().nodename`` — the daemon is
        packaged for Windows too, where ``os.uname`` does not exist at all, and
        a host that cannot name itself still gets a name from the socket layer.
        """
        source = os.environ if env is None else env
        named = source.get(ENV_MACHINE_NAME, "").strip()
        return named or platform.node() or socket.gethostname()

    @property
    def machine_identity(self) -> MachineIdentity:
        return MachineIdentity(
            name=self.machine_name,
            provider=self.provider,
            provider_pod_id=self.provider_pod_id,
            machine_type_code=self.machine_type_code,
            credential=self.machine_credential,
        )


MirrorFactory = Callable[[str, dict[str, Any]], ChatMirror]


@dataclass(slots=True)
class _TakeRound:
    """The takes one holder of the sync lock started beside its own.

    A pass (or a frame's re-read) takes its chats one at a time and in order
    of need; a chat a frame names meanwhile, or one the re-read of the newest
    page finds owing a turn, is taken at once in a task of its own rather than
    queued behind the take in flight. The round is what the holder waits on
    before it lets go of the lock, so a caller of ``sync_once`` or
    ``reconcile_chat`` still gets control back once everything it started is
    settled.
    """

    #: Chats taken from a read newer than the pass's listing, by id, with that
    #: read; ``None`` for a round with no listing to protect (a frame's own).
    fresher: dict[str, dict[str, Any]] | None
    #: When the newest page was last read; ``None`` for a round that does not
    #: read it again.
    relisted_at: float | None = None
    beside: set[asyncio.Task[None]] = field(default_factory=set)


@dataclass(slots=True)
class _ChatTakeLock:
    """One chat's lock and how many callers hold or wait on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@contextlib.asynccontextmanager
async def _per_chat(table: dict[str, _ChatTakeLock], chat_id: str) -> AsyncIterator[None]:
    """Hold ``chat_id``'s lock in ``table``; the entry goes once nobody uses it."""
    entry = table.get(chat_id)
    if entry is None:
        entry = table[chat_id] = _ChatTakeLock()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and table.get(chat_id) is entry:
            del table[chat_id]


def box_rest_client(
    settings: MirrorSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> CloudRestClient:
    """The client this box speaks with, from its settings alone.

    The bearer is what the box booted with, and where a fresher one comes from:
    this process outlives its own session — a turn may run for days, and the
    session is re-issued while it does — so a 401 sends an org box back to its
    own profile in the credential file (same person, same org) rather than
    leaving every request refused. A box on its machine credential has no such
    file: re-reading a login off the disk would put the box on a person's
    session, which is the one thing it must never do.

    Which agent the client asserts follows the bearer too. An org box asserts
    the machine name it will register as, a chain the backend records under the
    operator who signed it in. A box on its machine credential asserts nothing
    until the claim answers: the credential is the machine's own identity, and
    the backend admits an assertion on it only when it names that machine or a
    chat bound to it — neither of which the box knows before it has claimed.
    ``transport`` puts the client on a wire a test can watch.
    """
    on_credential = looks_like_machine_token(settings.token)
    reread = None if on_credential else stored_token_reader(settings.token)
    return CloudRestClient(
        api_url=settings.api_url,
        token=BoxCredential(settings.token, read=reread),
        agent_id=None if on_credential else machine_agent_id(settings.machine_name),
        transport=transport,
    )


class CloudMirrorService(StartRetry):
    """See the module docstring."""

    def __init__(
        self,
        settings: MirrorSettings,
        runtime: HarnessRuntime,
        *,
        rest: CloudRestClient | None = None,
        socket: CloudSocket | None = None,
        mirror_factory: MirrorFactory | None = None,
        schema_loader: SchemaCards | None = None,
        folders: ChatFolders | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        hard_stop: Callable[[float, Callable[[], None]], Any] | None = None,
        renew_session: SessionRenewer | None = None,
        wall_clock: Callable[[], float] = time.time,
        memory: MemorySample | None = None,
        sandbox_probes: SandboxReader | None = None,
        notebooks: BoxNotebookSlot | None = None,
    ) -> None:
        self._settings: MirrorSettings = settings
        #: The notebooks this box serves to its chats' agents and, through the
        #: machine channel, to people; composed once the machine is adopted.
        self._notebooks = notebooks
        install_sandbox_identity()
        #: Which chats may sleep and which sleeps first (``cloud/sleep_policy.py``).
        self._policy = SleepPolicy(
            idle_minutes=settings.mirror_idle_minutes,
            parked_ask_hours=settings.parked_ask_hours,
            clock=clock,
            memory=memory or chats_memory,
            probes=(probes := sandbox_probes or SandboxProbes()),
            settings=settings.sleep,
            disk=disk_usage_sample(settings.project_dir),
        )
        self._wall_clock = wall_clock
        #: Why the server refuses this box's session, while it does: the box
        #: takes no chats, sleeps the ones it held, and backs its beats off
        #: until a beat lands again. Not an outage — the server answered.
        self._signed_out: str | None = None
        self._signed_out_beats = 0
        self._signed_out_backoff: ReconnectBackoff | None = None
        self._renew_session = renew_session
        self._runtime = runtime
        self._schema = schema_loader
        self._rng = rng
        #: How many heartbeats in a row the server has not taken, and when the
        #: first of them failed. The loop backs off on these with jitter, says
        #: the outage once, and re-reads the org's chats on the beat that lands.
        self._missed_beats = 0
        self._server_lost_at: float | None = None
        #: Work the loops started and did not wait for — the re-read after an
        #: outage — so a stop (or a test) can settle it.
        self._background: set[asyncio.Task[None]] = set()
        #: How many chats have been newly taken here, and the one re-read of
        #: the connections that a run of takes shares.
        self._chats_taken = 0
        self._connections_after_take: asyncio.Task[None] | None = None
        # Custody of the chat folders this box holds while their chats are
        # awake. A box built without one serves chats exactly as before — the
        # scratch is local to this machine and does not survive the sleep.
        self._folders = folders or ChatFolders(chats_root=runtime.project.chats().path)
        #: What answers the drive when it asks this machine for a file's bytes
        #: on the machine channel: a promotion onto the held folder's queue.
        self._machine_requests = MachineRequests(
            self._folders, notebooks=None if notebooks is None else notebooks.dispatch
        )
        self._rest = rest or box_rest_client(settings)
        self._socket = socket or CloudSocket(self._rest, clock=clock, sleep=sleep)
        self._mirror_factory = mirror_factory or self._default_mirror
        self._sleep = sleep
        self._clock = clock
        self._workspaces = WorkspaceHost(  # the workspaces its chats are members of
            folders=self._folders,
            instance_of=self._folder_instance,
            refuse=lambda chat_id, reason, kind: self.report_publisher_state(
                chat_id, "refused", reason, kind=kind
            ),
            clock=clock,
            sandbox_work=probes.work,
            memory_of=workspace_memory_mb,
            release_sandbox=release_workspace_sandbox,
        )
        self._takes = FolderTakes(
            folders=self._folders,
            instance_of=self._folder_instance,
            report=self.report_publisher_state,
            clock=clock,
            poll_interval=lambda: self._settings.poll_interval,
        )
        self._returns = FolderReturns(
            folders=self._folders,
            lock=self._folder_lock,
            report=self.report_publisher_state,
            serving=lambda: self._workspaces.custody_keys(self._mirrors),
            workspaces=self._workspaces,
        )
        #: What ends the process when a stop has not by its bound. ``None`` is
        #: the production timer, looked up when it is armed rather than bound
        #: here, so a test process can neutralise it once for every service.
        self._hard_stop = hard_stop
        self._mirrors: dict[str, ChatMirror] = {}
        #: Per chat: the stale refusal this box last said ``publishing`` over.
        self._refusals_cleared: dict[str, str] = {}
        #: Per chat: its person (the row's owner, id and name), whom its
        #: agent acts for.
        self._owners: dict[str, tuple[str, str]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        #: Why this box stopped serving, when it was the service (not the
        #: socket) that decided: today, a refused machine credential.
        self._fatal_reason: str | None = None
        self._machine_id: str | None = None
        #: This process's own identity on the machine row: fresh per process,
        #: sent with the registration and every beat. The platform takes a
        #: beat only from the process that registered last, so this process's
        #: late ``draining`` beat cannot drain the row its successor serves.
        self._instance_id = uuid.uuid4().hex
        #: The box's own readings on every beat, and the machine card it hears back.
        self._pulse = MachinePulse()
        #: Set once the platform answered that another process registered
        #: after this one: this process stops beating for good.
        self._superseded = False
        self._machine_routes_missing = False
        self.org = OrgAdmission(settings.org_id) if settings.org_id is not None else None
        self._machine_route_missing_logged = False
        #: The last registration refusal, as ``"<status>:<code>"`` — what the
        #: machine loop backs off on, and resets when the reason changes.
        self._registration_refusal: str | None = None
        self._registration_backoff: ReconnectBackoff | None = None
        #: Chats bound here that the gateway would not let this box publish,
        #: by the refusal code — logged once per (chat, code), retried each poll.
        self._refused: dict[str, str] = {}
        #: Chats whose last start the gateway refused, by chat id: how many
        #: refusals in a row, when to try again, and the row facts that were
        #: true. The same shape as a failed start, kept apart because it is
        #: an answer rather than a fault — and because a retry of it must not
        #: put a served chat to sleep to make room for another refusal.
        self._refusal_waits: dict[str, StartFailure] = {}
        #: Chats bound here whose mirror would not start, by chat id: how many
        #: times in a row, when to try again, and the row facts that were true
        #: — news on the row (a reader spoke, opened the chat, the folder moved)
        #: ends the wait early. Gone with the process: a restarted box tries
        #: everything once.
        self._start_failures: dict[str, StartFailure] = {}
        #: One lock per chat over custody of its folder. The beats run on their
        #: own schedule now, so "beat it" and "hand it back" are two tasks: this
        #: is what keeps a beat off a lease the box is in the middle of giving
        #: up, while a slow take or push for ANOTHER chat waits on nothing.
        self._folder_locks: dict[str, asyncio.Lock] = {}
        #: The threads the beats run on, and only the beats. A beat is the one
        #: folder call that must not wait (``ChatFolders.beat`` takes no lock of
        #: its own for the same reason), and the shared thread pool is a queue
        #: like any other: a box pushing a folder per chat would hold every
        #: worker in it while the leases those pushes belong to lapsed. Built on
        #: first use so a service that never beats never starts a thread.
        self._beat_pool: ThreadPoolExecutor | None = None
        #: How wide the pool above is. A pool cannot be resized, so widening it
        #: means building the next one; this is what says whether the one in
        #: hand is still wide enough for the folders this box now holds.
        self._beat_pool_width = 0
        #: The wake stamp each served chat's row carried last pass — what tells
        #: a reader opening the chat from the same stamp read again.
        self._woke_at: dict[str, str] = {}
        #: Chats the cap had no room for, so the "no room" line is said (and
        #: the chat reported ``waiting``) once per wait, not on every tick.
        self._no_room: set[str] = set()
        self._reporter = Reporter(lambda: self._rest, sleep=self._sleep, later=self._in_background)
        #: Chats the running discovery pass opened for a reader's wake, ``None``
        #: between passes. The reader is looking at the chat, so the pass that
        #: woke it never closes it again to seat a chat nobody is reading.
        self._woken_this_pass: set[str] | None = None
        #: How many chats this box's socket was shown it may hold at once, once
        #: the platform refused one more; ``None`` until then. A cap this box
        #: learned rather than one it was configured with, and as binding.
        self._channel_cap: int | None = None
        #: The chat row's ``last_seq`` as it stood when this box last served the
        #: chat, by chat id. Kept so a mirror let go can be told apart from one
        #: with something unanswered.
        self._seen_seq: dict[str, int | None] = {}
        #: Chats whose mirror was handed back with nothing outstanding, and the
        #: ``last_seq`` that was true then. While the chat's counter has not
        #: moved past it there is nothing to answer, so the cap does not evict
        #: someone else to re-open it — with more bound chats than slots, doing
        #: so would close and re-spawn agent servers on every poll tick forever.
        self._released: dict[str, int | None] = {}
        #: Chats the event stream said were deleted, until their mirror stops.
        self._deleted: set[str] = set()
        #: The server's ``end_seq`` each served chat was taken at. A read that
        #: shows a higher one means the server ended the chat under this box
        #: (asleep, deleted, moved — ``alkera_core.objects.chat_end``), and the
        #: box drops it without pushing: its lease is already gone.
        self._end_seq: dict[str, int] = {}
        #: The published count each served chat's folder was last pushed at,
        #: by chat id — the sweep pushes again only once a turn has moved it.
        self._pushed_at: dict[str, int] = {}
        #: A push that did not land, by chat: the published count it was for,
        #: when it may be tried again, and the wait that was served.
        self._push_retry: dict[str, tuple[int, float, float]] = {}
        #: The last source list announced, so the poll tick's rewrite is silent
        #: until it says something new. ``None`` = nothing said yet.
        self._sources_said: str | None = None
        self._sync_lock = asyncio.Lock()
        #: Chats a ``chat.updated`` frame named while a pass held the lock, in
        #: arrival order, each with the callers waiting for it to be settled.
        #: The holder starts a take for each at once, beside its own, and
        #: empties this before letting go.
        self._named: dict[str, list[asyncio.Future[None]]] = {}
        #: Set when ``_named`` gains a chat, so the lock holder waiting on a
        #: take wakes for it at once instead of on its next look.
        self._named_waiting = asyncio.Event()
        #: One lock per chat over its take: the pass, a frame and the re-read
        #: may each reach the same chat, and only one may be opening it. A
        #: chat with an entry here has a take in flight or waiting.
        self._take_locks: dict[str, _ChatTakeLock] = {}
        #: The slots the takes beside the pass run in.
        self._beside_slots = asyncio.Semaphore(TAKES_BESIDE_THE_PASS)
        #: The lane the chats frames name are taken in, apart from those.
        self._named_slots = asyncio.Semaphore(NAMED_TAKES_AT_ONCE)
        #: Chats bound here that a pass left untaken because nothing waits in
        #: them; the row of one that claimed to be live was told it is asleep,
        #: once. Cleared when the chat is served.
        self._deferred: set[str] = set()
        self._deferred_said = 0
        #: Chats past ``_make_room_for`` whose mirror is not in ``_mirrors``
        #: yet — their folder is still being taken. They hold a slot of a
        #: configured cap exactly as a mirror does, or two takes running at
        #: once would each find the same slot free.
        self._claimed: set[str] = set()
        #: One lock per chat over its folder upkeep: a turn-end push and the
        #: tick's may both reach a chat, and only one moves its folder at once.
        self._upkeep_locks: dict[str, _ChatTakeLock] = {}
        #: Chats whose turn-end upkeep is scheduled and has not begun: a second
        #: turn end meanwhile is already covered by it.
        self._upkeep_queued: set[str] = set()
        self._upkeep_slots = asyncio.Semaphore(TURN_END_UPKEEPS_AT_ONCE)
        #: Set once the box has been told to stop and is finishing what it
        #: holds: it takes no new chat, says so on every beat so the allocator
        #: places nothing here, and releases each chat as its turn ends.
        self._draining = False
        #: Set with ``_draining`` when the stop is the supervisor restarting
        #: this daemon in place: the leases stay held, nothing is handed back,
        #: and the next process on this box takes the chats straight back.
        self._restarting = False
        self._stopped = False
        #: The shutdown in progress, once ``stop`` was called: every caller
        #: awaits the same one, and a caller that is cancelled meanwhile does
        #: not cut it short.
        self._stopping: asyncio.Task[None] | None = None
        #: What the last stop gave up on, by task name: a loop that never
        #: handed its cancellation back, a hand-back the drive never answered.
        self._abandoned: list[str] = []
        self._sse_backoff = ReconnectBackoff(clock=clock)
        self._sse_cursor: int | None = None
        #: Whether the event stream is up right now. While it is, a chat.updated
        #: frame is what tells a chat something was said, and the discovery pass
        #: reads no transcript on its own; while it is down, the pass is the
        #: only way to find out and reads as before.
        self._stream_live = False
        self._gave_up = asyncio.Event()
        #: How many times the socket has come up, and the re-read of the chat
        #: list the latest reconnect started.
        self._socket_ups = 0
        self._reconnect_poll: asyncio.Task[None] | None = None
        #: The idle sweep, which runs beside the poll and never in it, and the
        #: chats it is putting to sleep right now.
        self._sweep_task: asyncio.Task[None] | None = None
        self._sleeping: set[str] = set()
        self._sweep_overdue_said = False
        #: What a turn needs on disk before it starts: the drive's changes to
        #: the chat's folder, the files its message links, its attachments.
        self._inputs = TurnInputs(
            folders=self._folders,
            live_key=self._workspaces.live_key,
            drive_named=lambda chat_id: getattr(self._mirrors.get(chat_id), "files_drive_id", None),
            drain=self._drain_inbound,
            reader_for=lambda drive: self._rest.attachment_fetcher(drive_id=drive),
            project_path=runtime.project.path,
            clock=clock,
            sleep=sleep,
        )

    # -- observability ----------------------------------------------------------

    @property
    def machine_id(self) -> str | None:
        return self._machine_id

    @property
    def mirrors(self) -> dict[str, ChatMirror]:
        return dict(self._mirrors)

    @property
    def refused(self) -> dict[str, str]:
        """Chats bound here that this box may not publish, and why."""
        return dict(self._refused)

    @property
    def start_failures(self) -> dict[str, str]:
        """Chats bound here whose mirror would not start, by the sentence the
        reader was given."""
        return {chat_id: failure.reason for chat_id, failure in self._start_failures.items()}

    @property
    def socket(self) -> CloudSocket:
        return self._socket

    @property
    def rest(self) -> CloudRestClient:
        return self._rest

    @property
    def abandoned(self) -> tuple[str, ...]:
        """The tasks the last ``stop`` left behind, by name — a loop parked on
        an await that never returned its cancellation, a chat whose hand-back
        the drive never answered. Empty after a clean stop."""
        return tuple(self._abandoned)

    @property
    def schema_cards(self) -> SchemaCards:
        """The workspace's schema cards (built on first use from the settings)."""
        if self._schema is None:
            self._schema = box_data().schema_cards(
                api_url=self._settings.api_url,
                token=self._settings.token,
                runtime=self._runtime,
                chats=lambda: list(self._mirrors),
                refresh_interval=self._settings.schema_refresh_interval,
                workspaces=self.held_workspace_ids,
                token_source=lambda: self._rest.credential.token,
            )
        return self._schema

    def held_workspace_ids(self) -> list[str]:
        """The workspaces this box holds now: their notebook kernels run here,
        so their owners' connections are read and leased for them even when
        no chat of theirs is awake on the box."""
        return [w for key in self._workspaces.keys() if (w := workspace_of_key(key)) is not None]

    # -- lifecycle --------------------------------------------------------------

    def require_harness(self) -> None:
        """Refuse to start without the opencode harness — a plain message."""
        if not self._runtime.is_harness_available(OPENCODE_HARNESS):
            raise HarnessUnavailableError(OPENCODE_MISSING_MESSAGE)

    def require_machine_identity(self) -> MachineIdentity:
        """Refuse to start a box that cannot register — a plain message."""
        return require_machine_identity(self._settings.machine_identity)

    def refresh_sources(self) -> None:
        """Rewrite the workspace's source cards from the connections this box
        holds, so an agent started here is told what it can read from and how to
        route between two engines. Best-effort by design: a box that cannot
        describe its sources still serves chats — it just routes on the schema
        cards alone, which is where it was before."""
        described = box_data().refresh_sources(self._runtime.project)
        # The cards are rewritten every tick (an admin's new connection has to
        # reach the agent without a restart) but the LINE is news only when the
        # list changes: announced every 15 s it was thousands of identical lines
        # a day, with the box's real events buried between them.
        named = ", ".join(described) or "none"
        if named != self._sources_said:
            logger.info("workspace sources: %s", named)
            self._sources_said = named

    async def start(self) -> None:
        self.require_harness()
        self.require_machine_identity()
        # A box exists to answer questions with the agent, so the agent's
        # one-time costs — staging the binary, opencode's first-run database
        # migration — are paid HERE, at boot, in the background. Left to the
        # first chat they land inside a reader's first question, where the
        # budget is thirty seconds and the reader is watching. Best-effort and
        # never awaited: registration and the socket come up alongside it.
        start_prewarm_in_background(force=True)
        bound_chats_memory()
        memory = self._settings.memory_max_mirrors
        if memory is not None:
            logger.info(
                "this box's memory holds %d agent servers at once; past that the least "
                "recently used idle chat sleeps first",
                memory,
            )
        logger.info(
            "an idle chat sleeps after %.0f min with nobody using it, or sooner when this box "
            "needs its room (a full box, or the chats' memory past %.0f%%); a chat with "
            "anything running never does",
            self._settings.mirror_idle_minutes,
            self._settings.sleep.memory_pressure_percent,
        )
        self.refresh_sources()
        self._stopped = False
        self._stopping = None
        self._abandoned = []
        # Register first: the socket's first ticket then asserts the machine
        # id the gateway grants the write to. A refusal or an unreachable
        # backend is logged and retried by the machine loop; the socket still
        # comes up (as a reader) and is rebound when the id arrives.
        # An org worker's machine is its supervisor's claim, never its own.
        await (
            self._adopt_machine(self._settings.machine_id or "") if self.org else self._register()
        )
        # A socket that gives up (a revoked device token, a gateway that will
        # not have this box) never comes back on its own, and the process would
        # sit there looking alive while no chat is ever answered.
        # The service says it is finished; the supervisor owns the retry.
        self._socket.on_state(self._on_socket_state)
        self._socket.on_presence(self.hear_presence)
        await self._socket.start()
        loop = asyncio.get_running_loop()
        # An org worker neither claims nor beats (its supervisor does, and its
        # org credential is refused on the machine's routes), so the loop is
        # never made: a task dropped from the list still runs, untracked, and
        # took the worker's machine id away on its first refused beat.
        machine = (
            []
            if self.org is not None
            else [loop.create_task(self._machine_loop(), name="cloud-mirror-machine")]
        )
        self._tasks = [
            *machine,
            loop.create_task(self._stream_loop(), name="cloud-mirror-stream"),
            loop.create_task(self._poll_loop(), name="cloud-mirror-poll"),
            # Its OWN task: a lease is kept by a beat landing inside its cadence,
            # and a beat behind the discovery pass would wait for every slow folder.
            loop.create_task(self._beat_loop(), name="cloud-mirror-folder-beat"),
            # What a folder needs that is not keeping it: taking in what the
            # drive holds and pushing out what the last turn wrote. Its own
            # task too, because a push uploads one file at a time and a folder
            # of runtime files takes minutes — minutes the beats above must not
            # be waiting through.
            loop.create_task(self._folder_upkeep_loop(), name="cloud-mirror-folder-upkeep"),
            loop.create_task(self._schema_loop(), name="cloud-mirror-schema"),
            loop.create_task(folder_fence.run(self._folders.fenced), name="cloud-mirror-fence"),
        ]

    @property
    def draining(self) -> bool:
        """Whether this box has been told to stop and is finishing its turns."""
        return self._draining

    def begin_drain(self, *, restart: bool | None = None) -> None:
        """Take no new chat, and tell the platform so on the next beat.

        Separate from :meth:`drain` so the flip is immediate: the beat that
        carries it is what makes the allocator stop placing chats here, and a
        flip that waited for the first pass of the wait below would let a chat
        land on a box that is already leaving. ``restart``: an org worker's
        supervisor's stop kind; ``None`` reads this process's own."""
        if self._draining:
            return
        self._draining = True
        self._restarting = self._restart_in_place() if restart is None else restart
        if self._restarting:
            logger.info(
                "this box's daemon is being restarted by its supervisor: it is taking no new "
                "chats, keeps the %d it holds and hands none back",
                len(self._mirrors),
            )
            within = self._settings.drain_ceiling_seconds + RESTART_HARD_STOP_SECONDS
            self._arm_hard_stop(
                within,
                lambda: stop_hard(
                    f"the supervised restart did not end the process within {within:.0f} s"
                ),
            )
            return
        ceiling = self._settings.drain_ceiling_seconds
        within = ceiling + RESTART_HARD_STOP_SECONDS
        logger.info(
            "this box was told to stop: it is taking no new chats and finishing the %d it holds "
            "(at most %.0f s; the process ends within %.0f s)",
            len(self._mirrors),
            ceiling,
            within,
        )
        # The drain and the stop after it are each bounded, but the PROCESS is
        # not: a worker thread parked on a drive that never answers holds the
        # interpreter's exit, and a supervisor with no stop timeout waits on it
        # for ever — every chat on the box served by nobody and handed to no
        # other box. Armed from the configured ceiling: an explicit, longer
        # ``drain(ceiling)`` is for a caller that owns the process itself.
        self._arm_hard_stop(
            within,
            lambda: stop_hard(f"the stop did not end the process within {within:.0f} s"),
        )

    def _arm_hard_stop(self, seconds: float, action: Callable[[], None]) -> None:
        (self._hard_stop or arm_hard_stop)(seconds, action)

    @property
    def restarting(self) -> bool:
        """Whether the stop in progress is a supervised restart in place."""
        return self._restarting

    def _restart_in_place(self) -> bool:
        """A supervised daemon restarts in place unless its supervisor said the
        stop is final."""
        if not self._settings.supervised:
            return False
        final = self._settings.final_stop_file
        return not (final is not None and final.exists())

    async def _wait_for_in_flight(self) -> None:
        """A supervised restart: let in-flight work settle (``restart_wait``)."""
        await wait_for_in_flight(
            lambda: [m.activity for m in self._mirrors.values()],
            clock=self._clock,
            sleep=self._sleep,
            drain_ceiling=self._settings.drain_ceiling_seconds,
            poll=DRAIN_POLL_SECONDS,
        )
        # The wait may have been the drain ceiling's; the exit after it is not.
        self._arm_hard_stop(RESTART_HARD_STOP_SECONDS, lambda: stop_hard("the restart hung"))

    async def drain(self, ceiling: float | None = None) -> None:
        """Finish what this box holds, then return so the stop can run.

        Every chat that owes nothing is handed back at once — its folder
        pushed, its lease released, the row marked asleep — which is what lets
        another box pick it up while this one is still finishing the rest. So
        is every chat whose only work is an ask parked on a person: the ask is
        durable and the next open re-offers it, and waiting on a person who may
        not come back held a node's stop for its whole ceiling. The chats with
        a turn, a tool or a background job running are left alone and released
        as each one ends.

        ``ceiling`` bounds the wait. It is not a limit on how long an answer
        may take — a turn may legitimately run for hours — it is what makes a
        deploy finishable: without it one wedged chat holds the box, and the
        operator's only remaining move is the kill this whole path exists to
        avoid. What is still running when the ceiling is reached is handed back
        the same way, so the next box resumes it.

        A caller that gives up on the drain (a supervisor out of patience) ends
        the WAIT, never a hand-back that is half done: a release takes the chat
        off this box before it pushes its folder, so one cut in the middle
        would leave the chat with neither a box nor its last turn's files. The
        release in flight is finished and the cancellation re-raised after, so
        the stop runs next and puts the rest to sleep.
        """
        self.begin_drain()
        if self._restarting:
            await self._wait_for_in_flight()
            return
        budget = self._settings.drain_ceiling_seconds if ceiling is None else ceiling
        deadline = self._clock() + budget
        interrupted = False
        while True:
            # Every idle chat's hand-back runs at once, each on its own budget:
            # one folder the drive will not take must not hold the others.
            quiet = [cid for cid, m in self._mirrors.items() if not m.activity.holds_a_stop]
            idle = await self._policy.drain_releasable(quiet)
            releases = {
                asyncio.get_running_loop().create_task(
                    self._finish_or_abandon(
                        self._release_mirror(chat_id, ending=DRAINED_ENDING),
                        f"cloud-mirror-drain:{chat_id}",
                        STOP_RELEASE_BUDGET_SECONDS,
                    ),
                    name=f"cloud-mirror-drain-release:{chat_id}",
                )
                for chat_id in idle
            }
            while releases and not all(task.done() for task in releases):
                try:
                    await asyncio.wait(releases)
                except asyncio.CancelledError:
                    interrupted = True
            for release in releases:
                release.result()
            if interrupted:
                raise asyncio.CancelledError
            if not self._mirrors:
                logger.info("every chat this box held has been handed back; stopping")
                return
            left = deadline - self._clock()
            if left <= 0:
                logger.warning(
                    "%d chat(s) were still running after %.0f s of draining; they are handed "
                    "back where they stand and resume on the next box",
                    len(self._mirrors),
                    budget,
                )
                return
            await self._sleep(min(DRAIN_POLL_SECONDS, left))

    async def stop(self) -> None:
        """Stop serving, within a bound, whatever the loops are parked on.

        The shutdown runs as its own task. A caller cancelled while it waits
        (a supervisor that gave up on ``run_until``) is honoured by finishing
        the shutdown — it is bounded — and re-raising the cancellation after,
        never by leaving the box half-stopped; a second caller joins the first.
        """
        self._stopped = True
        if self._stopping is None or self._stopping.done():
            self._stopping = asyncio.get_running_loop().create_task(
                self._stop_now(), name="cloud-mirror-stop"
            )
        stopping = self._stopping
        interrupted = False
        while not stopping.done():
            try:
                await asyncio.wait({stopping})
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError
        stopping.result()

    async def _stop_now(self) -> None:
        tasks, self._tasks = self._tasks, []
        self._abandoned = []
        # Never ``await task``: a task whose cancellation was absorbed by the
        # library it was inside never ends, and a stop waiting on it is the
        # zombie box — heartbeats going, leases held, the operator's restart
        # blocked — that this service exists to rule out. The cancel is asked
        # again through the grace, and what has still not ended is named and
        # left behind.
        await self._settle(tasks, STOP_TASK_GRACE_SECONDS)
        await self._settle(list(self._background), STOP_TASK_GRACE_SECONDS)
        if self._notebooks is not None:
            # Kernels stop before the folders they run in are handed back.
            await self._finish_or_abandon(
                self._notebooks.close(), "cloud-mirror-notebooks", STOP_TASK_GRACE_SECONDS
            )
        # A box going away puts every chat it serves to sleep — the folder
        # pushed and released, the chat marked asleep — so the next box (or
        # this one, restarted) resumes them rather than finding them stranded
        # on a lease nobody beats for.
        if self._restarting:
            # The chats come back to this box and keep their leases; a
            # workspace none of them is in (its chats moved on, and something
            # it started held it here) comes back to nobody, and its lease
            # would keep the box its chats moved to off the tree until it
            # lapsed.
            served = self._served_now()
            await self._park_every_mirror()
            await self._workspaces.put_away_every(ending=DRAINED_ENDING, served=served)
        else:
            await self._release_every_mirror()
            served = self._served_now()
            await asyncio.gather(
                *(
                    self._finish_or_abandon(
                        self._workspaces.put_away(key, ending=DRAINED_ENDING, served=served),
                        f"cloud-workspace-release:{key}",
                        STOP_RELEASE_BUDGET_SECONDS,
                    )
                    for key in self._workspaces.keys()
                )
            )
        if self._schema is not None:
            await self._finish_or_abandon(
                self._schema.stop(), "cloud-mirror-schema-cards", STOP_TASK_GRACE_SECONDS
            )
        await self._finish_or_abandon(
            self._socket.stop(), "cloud-mirror-socket", STOP_TASK_GRACE_SECONDS
        )
        if self._beat_pool is not None:
            # Nothing is left to keep alive, and a beat still on the wire is
            # asserting a lease every chat has just given back. Not waited for:
            # a beat parked on a drive that does not answer would hold the
            # stop for as long as the drive does.
            self._beat_pool.shutdown(wait=False, cancel_futures=True)
            self._beat_pool = None
            self._beat_pool_width = 0

    async def _settle(self, tasks: Sequence[asyncio.Task[Any]], grace: float) -> None:
        """Cancel ``tasks`` within ``grace``; what will not end is left behind
        (:func:`~alkera_cli.cloud.stop_tasks.settle`)."""
        self._abandoned += await settle(tasks, grace, every=STOP_CANCEL_EVERY_SECONDS, log=logger)

    async def _finish_or_abandon(
        self, work: Coroutine[Any, Any, Any], name: str, budget: float
    ) -> bool:
        """Run ``work`` as a task named ``name`` and give it ``budget`` to
        finish. One that has not is cancelled and given the stop's grace to
        end; either way it is named as abandoned. ``True`` when it finished.

        Its failure is logged, never raised: a stop is a sequence of things
        that must each be attempted, and none of them may end the sequence.
        """
        task = asyncio.get_running_loop().create_task(work, name=name)
        done, _pending = await asyncio.wait({task}, timeout=budget)
        if done:
            if not task.cancelled() and task.exception() is not None:
                logger.warning("%s failed during the stop: %r", name, task.exception())
            return True
        logger.warning(
            "%s did not finish within %.1fs; it is cut short and its work is left to the next box",
            name,
            budget,
        )
        self._abandoned.append(name)
        await self._settle([task], STOP_TASK_GRACE_SECONDS)
        # ``_settle`` names it again only when it also ignored the cancel;
        # one entry per abandoned task is what the caller reads.
        while self._abandoned.count(name) > 1:
            self._abandoned.remove(name)
        return False

    def _on_socket_state(self, state: str) -> None:
        if state == "fatal":
            self._gave_up.set()
        elif state == "connected":
            self._socket_ups += 1
            if self._socket_ups > 1:
                self._repoll_after_reconnect()

    def _repoll_after_reconnect(self) -> None:
        """The socket came back: read the chat list now, not on the next tick.

        Whatever was bound here while the socket was down — or while it sat
        open delivering nothing, before its silence was noticed — has no frame
        on the way, so the listing is the only word on it. One read at a time;
        a reconnect while one is pending is covered by it."""
        if self._stopped or self._signed_out is not None:
            return
        pending = self._reconnect_poll
        if pending is not None and not pending.done():
            return
        self._reconnect_poll = self._in_background(
            self._resume_after_outage(), "cloud-mirror-reconnect-poll"
        )

    def _give_up(self, reason: str) -> None:
        """The box can no longer serve anything and only an operator changes
        that. Said once, then the run loop unwinds: ``stop`` puts every chat
        this box holds to sleep — folder pushed, lease released — so the chats
        land on another box rather than on a lease nobody beats for."""
        if self._fatal_reason is None:
            self._fatal_reason = reason
            logger.error("%s", reason)
        self._machine_id = None
        self._gave_up.set()

    @property
    def gave_up(self) -> asyncio.Event:
        """Set once the box can no longer serve anything and only a restart
        will change that. `run_until` returns on it; the caller exits non-zero
        so its supervisor starts a fresh one."""
        return self._gave_up

    @property
    def signed_out(self) -> str | None:
        """Why the server refuses this box's session (``"401 token_expired"``),
        or ``None`` while it accepts it."""
        return self._signed_out

    @property
    def failure(self) -> str | None:
        """Why the box gave up — the service's own reason (a credential the
        platform refused) first, then the socket's."""
        return self._fatal_reason or self._socket.fatal_reason

    async def run_until(self, stop: asyncio.Event) -> None:
        """Start, block until ``stop`` is set (or the task is cancelled), drain,
        stop.

        The drain runs only when the box was ASKED to stop — a deploy, an
        operator. A box that gave up (its credential refused, its socket dead)
        has nothing to drain into: it cannot publish another token, so waiting
        on its turns would only delay handing the chats to a box that can.
        """
        await self.start()
        try:
            await _first_of(stop, self._gave_up)
            if not self._gave_up.is_set():
                await self.drain()
        finally:
            await self.stop()

    # -- machine registration -------------------------------------------------------

    async def _register(self) -> bool:
        """One registration attempt. ``True`` when the box now knows the
        machine id it publishes as; a refusal, a 404 (no machine routes) or an
        unreachable backend is logged and answered ``False``.

        A box carrying a platform machine credential CLAIMS the machine the
        credential was minted for; one without registers as its org's own. A
        box holding both takes the credential path: what the platform says a
        box is outranks the grant its operator's account happens to hold.
        """
        identity = self._settings.machine_identity
        try:
            body = (
                await self._rest.claim_machine(
                    credential=identity.credential,
                    name=identity.name,
                    provider_pod_id=identity.provider_pod_id,
                    capacity=self._capacity(),
                    daemon_version=self._settings.daemon_version,
                    daemon_instance_id=self._instance_id,
                )
                if identity.is_platform
                else await self._rest.register_machine(
                    name=identity.name,
                    provider=identity.provider,
                    provider_pod_id=identity.provider_pod_id,
                    machine_type_code=identity.machine_type_code,
                    daemon_instance_id=self._instance_id,
                )
            )
        except CloudApiError as exc:
            if identity.is_platform and exc.code in FATAL_CLAIM_REFUSALS:
                # The platform has taken this box's standing away. Nothing the
                # box can do changes that, and every chat it holds belongs to
                # somebody who is waiting: give up, which releases the folders
                # and puts the chats back where another box can take them.
                self._give_up(CREDENTIAL_REFUSED_SENTENCE.format(code=exc.code))
                return False
            if exc.session_refused:
                # Said once as what it is, and backed off like any refusal:
                # asking again cannot help until the token is replaced.
                self._note_signed_out(exc)
                self._registration_refusal = f"{exc.status}:{exc.code or ''}"
                return False
            if exc.status == 404:
                self._machine_routes_missing = True
                if not self._machine_route_missing_logged:
                    logger.warning(
                        "the machine routes are not available on this backend (404); "
                        "this box serves no chat until it can register"
                    )
                    self._machine_route_missing_logged = True
            else:
                # Remember WHAT was refused: the loop backs off a refusal that
                # keeps coming back, and starts over the moment it changes.
                self._registration_refusal = f"{exc.status}:{exc.code or ''}"
                logger.warning(
                    "machine registration refused (%s %s): %s; this box publishes nothing "
                    "until it registers",
                    exc.status,
                    exc.code or "",
                    exc.message or exc,
                )
            return False
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("machine registration unreachable: %s", exc)
            return False
        self._machine_routes_missing = False
        self._pulse.heard(body, carries_card=identity.is_platform)
        machine_id = body.get("id") or body.get("machine_id")
        if not (isinstance(machine_id, str) and machine_id):
            logger.warning("machine registration answered without an id: %r", body)
            return False
        await self._adopt_machine(machine_id)
        return True

    async def _adopt_machine(self, machine_id: str) -> None:
        """The box now publishes as ``machine_id``: every REST call and — from
        the next ticket on — the socket assert it as the agent."""
        self._machine_id = machine_id
        logger.info(
            "registered machine %s as %s; publishing as it",
            self._settings.machine_name,
            machine_id,
        )
        # The chat folders are leased as the machine too: the lease's badge
        # names the box a reader is refused by, and the Files routes admit a
        # box under the same verified assertion the chat's routes do.
        self._folders.bind_machine(machine_id)
        if self._notebooks is not None:
            # Read at each request: the credential and the asserted machine
            # are whatever this service speaks with at that moment.
            self._notebooks.start(
                self._folders,
                lambda: self._rest.headers(),
                self._settings.api_url,
                person_of=self.chat_person,
            )
        # Bound before the rebind: the reconnect it causes is the first
        # connection that speaks as the machine, and so the first on which
        # the machine's own channel is subscribed.
        self._socket.bind_machine(machine_id, self._machine_requests.handle)
        if self._rest.agent_id != machine_id:
            self._rest = self._rest.for_agent(machine_id)
            await self._socket.rebind(self._rest)

    async def _machine_loop(self) -> None:
        while self.org is None:  # an org worker's supervisor claims and beats for it
            if self._gave_up.is_set():
                # Nothing this loop can do next changes the answer, and the run
                # loop is already unwinding. Asking again would only put another
                # refusal on the org's audit trail on the way out.
                return
            wait = self._settings.heartbeat_interval
            if self._machine_id is None:
                refused_before = self._registration_refusal
                if await self._register():
                    self._registration_refusal = None
                    self._registration_backoff = None
                    # Nothing else in this branch: the beat below runs on the
                    # same tick. A registration restarts the silence the
                    # platform reaps a box on, and a discovery pass here would
                    # open the org's chats before the first beat lands, long
                    # enough to be reaped for silence. The poll loop discovers
                    # the chats within its own interval regardless.
                elif self._machine_routes_missing:
                    self._registration_refusal = None
                    self._registration_backoff = None
                    wait = self._settings.missing_route_retry
                elif self._registration_refusal is not None:
                    # A refusal is an ANSWER, not an outage: no credit, no
                    # grant, the ceiling in use. Re-asking every 20 s cannot
                    # change it, and each ask costs the org a refusal frame in
                    # every open browser and an audit row. Back off to a
                    # five-minute floor — and start over the moment the reason
                    # changes, so a box refused for a NEW reason is not stuck
                    # behind the old one's delay.
                    if (
                        self._registration_backoff is None
                        or refused_before != self._registration_refusal
                    ):
                        self._registration_backoff = ReconnectBackoff(
                            base=self._settings.heartbeat_interval,
                            cap=REFUSAL_BACKOFF_CAP_SECONDS,
                            clock=self._clock,
                        )
                    wait = self._registration_backoff.next_delay()
            if self._machine_id is not None and not self._superseded:
                try:
                    pulse = await self._pulse.fields(
                        lambda: [m.activity for m in self._mirrors.values()]
                    )
                    beat = await self._rest.heartbeat_machine(
                        self._machine_id,
                        capacity=self._capacity(),
                        chats_served=len(self._mirrors),
                        daemon_version=self._settings.daemon_version,
                        credential=self._settings.machine_credential,
                        # A restart says so in its own word: the platform keeps
                        # the box's chats on it rather than moving them off.
                        draining=self._draining and not self._restarting,
                        restarting=self._restarting or None,
                        daemon_instance_id=self._instance_id,
                        **pulse,
                    )
                    self._pulse.heard(beat, carries_card=bool(self._settings.machine_credential))
                except CloudApiError as exc:
                    if exc.code == STALE_INSTANCE_CODE:
                        # Another process on this box registered after this
                        # one: it owns the row now. Beating on would only be
                        # refused, and must not re-register (that would take
                        # the row back from the process that is serving).
                        logger.warning(
                            "machine %s was registered by a newer daemon process; "
                            "this one stops beating",
                            self._machine_id,
                        )
                        self._superseded = True
                    elif self._settings.machine_credential and exc.code in FATAL_CLAIM_REFUSALS:
                        # A beat refused on the credential is the platform
                        # taking the box away mid-service, not an outage.
                        self._give_up(CREDENTIAL_REFUSED_SENTENCE.format(code=exc.code))
                    elif exc.session_refused:
                        wait = await self._session_refused(exc)
                    elif exc.status == 404:
                        # The row is gone (released, or reaped while the
                        # server could not hear this box): register again
                        # rather than heartbeat a ghost. The server revives the
                        # row by pod id, so the id comes back the same and the
                        # chats bound to it stay served.
                        logger.warning("machine %s is no longer registered", self._machine_id)
                        self._machine_id = None
                    else:
                        wait = self._heartbeat_failed(str(exc))
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    wait = self._heartbeat_failed(str(exc))
                else:
                    self._heartbeat_landed()
                    self._record_status()
            await self._sleep(wait)

    def _heartbeat_failed(self, reason: str) -> float:
        """A beat the server did not take. Returns the wait before the next.

        An outage is not a refusal: the box keeps every chat and lease it
        holds and keeps asking — but with a JITTERED wait no shorter than the
        interval and always inside the ready window, so a fleet returning from
        one outage arrives spread out rather than as a wave (a fleet that
        reconnects in lockstep is what turns a recovering server into a
        struggling one), while no box backs off far enough to be judged silent
        once the server is listening again. Said once, at WARNING: a warning
        per beat for twenty-five minutes is a log nobody reads.
        """
        self._missed_beats += 1
        if self._missed_beats == 1:
            self._server_lost_at = self._clock()
            logger.warning(
                "the server has stopped answering (%s); the beats continue with a jittered "
                "wait and every chat and folder held here is kept",
                reason,
            )
        else:
            logger.debug("machine heartbeat still not taken (%s)", reason)
        interval = self._settings.heartbeat_interval
        return interval * (1.0 + self._rng())

    def _note_signed_out(self, exc: CloudApiError) -> None:
        """The server refused this box's session. Said once, at ERROR, with
        what a person has to do: nothing the box does on its own changes it."""
        if self._signed_out is not None:
            return
        self._signed_out = f"{exc.status} {exc.code or 'unauthorized'}"
        logger.error(
            "%s",
            SIGNED_OUT_SENTENCE.format(cause=self._signed_out, path=alkera_paths.AUTH_FILE_PATH),
        )

    async def _session_refused(self, exc: CloudApiError) -> float:
        """A beat refused on the session itself. Returns the wait before the
        next beat.

        Not an outage: the server answered, and it will answer the same until
        the token is replaced. So the box stops taking chats, puts the ones it
        holds to sleep once the refusal has repeated (their turns could not be
        published, and their leases lapse on refused beats anyway), and backs
        off exponentially to a five-minute ceiling instead of asking at the
        heartbeat interval for ever."""
        self._signed_out_beats += 1
        self._note_signed_out(exc)
        if self._signed_out_beats >= SIGNED_OUT_GRACE_BEATS and self._mirrors:
            logger.warning(
                "putting the %d chat(s) this box holds to sleep: it cannot publish them while "
                "its session is refused",
                len(self._mirrors),
            )
            await self._release_every_mirror()
        if self._renew_session is not None:
            try:
                renewed = await self._renew_session()
            except Exception:
                logger.exception("renewing this box's session failed")
                renewed = False
            if renewed:
                return 0.0
        if self._signed_out_backoff is None:
            self._signed_out_backoff = ReconnectBackoff(
                base=self._settings.heartbeat_interval,
                cap=SIGNED_OUT_BACKOFF_CAP_SECONDS,
                rng=self._rng,
                clock=self._clock,
            )
        return self._signed_out_backoff.next_delay()

    def _record_status(self) -> None:
        """Write what this process reported on the beat the server just took
        (the roll reads it; ``alkera_cli.box_status``)."""
        counts = self.activity_counts()
        report: BoxStatus = {
            "schema": STATUS_SCHEMA,
            "pid": os.getpid(),
            "daemon_instance_id": self._instance_id,
            "daemon_version": self._settings.daemon_version,
            "build": version_info.build_id(),
            "machine_id": self._machine_id,
            "beat_at": self._wall_clock(),
            "chats_held": len(self._mirrors),
            "chats_busy": counts["chats_busy"],
            "chats_working": counts["chats_working"],
            "chats_awaiting_user": counts["chats_awaiting_user"],
            "chats_idle": counts["chats_idle"],
            "draining": self._draining,
            "restarting": self._restarting,
        }
        write_status(self._settings.status_file, report)

    def _heartbeat_landed(self) -> None:
        """A beat the server took. After an outage or a refused session: say
        so once and re-read the org's chats NOW — a message the reader sent
        while the box was away is answered on the beat that found the server,
        not a poll tick later."""
        if self._missed_beats == 0 and self._signed_out is None:
            return
        if self._signed_out is not None:
            logger.info(
                "the server accepts this box's session again (it had refused it: %s); taking chats",
                self._signed_out,
            )
            self._signed_out = None
            self._signed_out_beats = 0
            self._signed_out_backoff = None
        if self._missed_beats:
            lost_at = self._server_lost_at
            away = (self._clock() - lost_at) if lost_at is not None else 0.0
            logger.info(
                "the server answered again after %.0f s and %d missed beat(s); resuming",
                away,
                self._missed_beats,
            )
            self._missed_beats = 0
            self._server_lost_at = None
        task = asyncio.get_running_loop().create_task(
            self._resume_after_outage(), name="cloud-mirror-resume"
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _resume_after_outage(self) -> None:
        """What the box owes the moment the server is back: the chat list
        (a chat bound here meanwhile, a message relayed into the gap — the
        running mirrors catch up from the transcript on the same pass). The
        socket and the event stream reconnect on their own backoffs."""
        try:
            await self.sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("the re-read after the outage failed; the poll loop retries it")

    async def settle_background(self) -> None:
        """Wait for the work the loops started and did not wait for."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # -- discovery ----------------------------------------------------------------------

    async def _stream_loop(self) -> None:
        while True:
            try:
                async for frame in self._rest.events(after=self._sse_cursor):
                    if frame.get("type") == STREAM_OPENED:
                        self._stream_opened()
                        continue
                    self._sse_backoff.connected()
                    self._sse_backoff.decay()
                    if frame.get("id"):
                        with contextlib.suppress(ValueError, TypeError):
                            self._sse_cursor = int(frame["id"])
                    data = frame.get("data")
                    if frame.get("type") == "reset":
                        await self.sync_once()
                        continue
                    if not isinstance(data, dict):
                        continue
                    # A membership change moves what this box's user may USE
                    # just as a connection change does (the owner who joins the
                    # team gains its rows), so both re-pull the team set — and
                    # the chats already open serve it on their next turn.
                    if data.get("type") in (TEAM_CONNECTION_UPDATED, MEMBERSHIP_CHANGED):
                        await self.reconcile_connections()
                        continue
                    if data.get("type") == FILE_LEASE_CHANGED:
                        # Somebody wrote into a folder this box holds. Taken now
                        # rather than on the next beat: the reader who dropped
                        # the file expects the agent to see it, and a poll
                        # interval of latency on a drop is what makes a live
                        # folder feel like a batch job. A frame that says it is
                        # the holder's own report owes the holder nothing, and
                        # draining on it asked the drive once per report. Only
                        # a write the drive queued (a person's) holds a workspace.
                        if (reason := data.get("reason")) != LEASE_CHANGED_REPORT:
                            await self._pull_inbound_for(
                                data.get("entity_id"), person=reason == LEASE_CHANGED_INBOUND
                            )
                        continue
                    if data.get("type") != CHAT_UPDATED:
                        continue
                    chat_id = data.get("entity_id")
                    if isinstance(chat_id, str) and chat_id:
                        if data.get("reason") == CHAT_DELETED_REASON:
                            self._deleted.add(chat_id)
                        await self._chat_named(chat_id)
            except asyncio.CancelledError:
                self._stream_live = False
                raise
            except CloudApiError as exc:
                logger.warning("event stream refused: %s", exc)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.info("event stream dropped: %s", exc)
            except Exception:
                logger.exception("event stream failed")
            self._stream_live = False
            raise_if_cancel_requested()  # a cancel httpx turned into a read timeout
            await self._sleep(self._sse_backoff.next_delay())

    def _stream_opened(self) -> None:
        """The event stream is (back) up. Every chat this box serves reads its
        transcript once, for whatever was said while nothing was listening;
        from here on a chat.updated frame is what wakes a chat's read."""
        self._stream_live = True
        for mirror in list(self._mirrors.values()):
            if mirror.state in ("running", "starting"):
                mirror.request_catch_up()
        if self._folders.enabled:
            for chat_id in list(self._mirrors):
                held = self._folders.held(chat_id)
                if held is not None:
                    self._in_background(
                        self._pull_inbound_for(held.record.node_id),
                        f"cloud-mirror-reopened-drain:{chat_id}",
                    )

    async def _poll_loop(self) -> None:
        while True:
            try:
                # A connection the admin adds after this box booted has to reach
                # the agent without a restart, and the cards are cheap to rebuild
                # (an unchanged document is not rewritten).
                self.refresh_sources()
                # A refused session refuses the listing too: the beat is what
                # asks, on its own backoff, and the beat that lands re-reads.
                if self._signed_out is None:
                    await self.sync_once()
                    self._sweep_beside()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("chat poll failed")
            await self._sleep(self._settings.poll_interval)

    def _sweep_beside(self) -> None:
        """Start the idle sweep on a task of its own, unless one still runs.

        Never awaited here. Putting a chat to sleep pushes its whole folder,
        and a push is as slow as the drive: awaited, one sleeping chat held
        this loop — and with it every chat bound to the box since — for as
        long as its push took. A sweep still running when the next tick comes
        is said once and left to finish; the poll goes on either way."""
        running = self._sweep_task
        if running is not None and not running.done():
            if not self._sweep_overdue_said:
                self._sweep_overdue_said = True
                logger.warning(
                    "the idle sweep is still putting a chat to sleep; the chat list is read "
                    "without waiting for it"
                )
            return
        self._sweep_overdue_said = False
        self._sweep_task = self._in_background(self._sweep_quietly(), "cloud-mirror-idle-sweep")

    async def _sweep_quietly(self) -> None:
        try:
            await self.sweep_idle_mirrors()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("idle sweep failed")

    async def _beat_loop(self) -> None:
        """Keep the leases on the folders this box holds, on the lease's cadence.

        Nothing else runs in here. The discovery pass takes folders one at a
        time and a take that cannot reach the drive costs a connect timeout
        apiece, so a beat sequenced behind the pass arrives when the pass is
        done rather than when the lease needs it — which is how six chats that
        were being written perfectly well lost their folders to a lapse and
        their readers were told the workspace had restarted.
        """
        while True:
            try:
                await self._beat_folders()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("folder heartbeat pass failed")
            await self._sleep(self._folder_beat_interval())

    async def _folder_upkeep_loop(self) -> None:
        """Everything a held folder needs that is not keeping the lease.

        Split off the beat because it is SLOW and the beat is not allowed to
        be: a push uploads a file at a time (four requests apiece), so a chat
        whose folder holds a runtime's worth of files pushed for minutes while
        the folders after it in the pass waited for their beat and lost their
        leases to the sixty-second TTL.
        """
        while True:
            try:
                await self._upkeep_folders()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("folder upkeep pass failed")
            await self._sleep(self._settings.poll_interval)

    async def _schema_loop(self) -> None:
        while True:
            await self.reconcile_connections()
            await self._sleep(self._settings.poll_interval)

    async def reconcile_connections(self) -> None:
        """The team connections may have changed: re-pull them and (re)load the
        schema cards of whatever is live here. When the pass moved a row, the
        agent's tool surface is rebuilt and every open chat rebound to it — the
        box holds the credential and cards the warehouse off this same store,
        and a chat that opened before the row landed would otherwise go on
        answering "no added connection" until the box restarted. Never raises —
        a failed pass logs and the next tick retries."""
        try:
            if await self.schema_cards.sync_once():
                await self._runtime.team_connections_changed()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("schema-card sync failed")

    def _chat_taken(self) -> None:
        """A chat is newly held here. On a machine credential the connections
        are read per held chat, so this chat's owner's set is owed a read before
        the next tick; one background read serves every take that lands while
        it is pending. A box on a person's login reads that person's set, which
        no take changes."""
        if not looks_like_machine_token(self._settings.token):
            return
        self._chats_taken += 1
        if self._connections_after_take is None or self._connections_after_take.done():
            self._connections_after_take = self._in_background(
                self._reconcile_after_takes(), "cloud-mirror-connections-after-take"
            )

    async def _connections_before_first_spawn(self) -> None:
        """On a machine credential, start the read a newly held chat is owed
        and wait for it, bounded, before the chat's session spawns. The
        session's agent lists its tools once, as it connects; a read that
        landed after the spawn left the chat's first turn answering that no
        SQL tool was registered while the box had already carded its owner's
        warehouse. A person's login owes no read, so nothing waits. The wait
        is on the injected clock and sleep, and ends at the bound whatever
        the read is doing: the spawn is never held on a backend that does
        not answer."""
        if not looks_like_machine_token(self._settings.token):
            return
        self._chat_taken()
        read = self._connections_after_take
        if read is None:
            return
        deadline = self._clock() + FIRST_SPAWN_CONNECTIONS_WAIT_SECONDS
        while not read.done():
            remaining = deadline - self._clock()
            if remaining <= 0:
                logger.info(
                    "the connections read is still pending after %.0fs; spawning without it",
                    FIRST_SPAWN_CONNECTIONS_WAIT_SECONDS,
                )
                return
            await self._sleep(min(FIRST_SPAWN_CONNECTIONS_POLL_SECONDS, remaining))

    async def _reconcile_after_takes(self) -> None:
        """Re-read the connections once the run of takes has gone quiet (or
        has run long enough), and again when a take landed during the read:
        every chat taken before this returns has had its owner's set read."""
        while True:
            waited = 0.0
            while True:
                seen = self._chats_taken
                await self._sleep(CONNECTIONS_AFTER_TAKE_QUIET_SECONDS)
                waited += CONNECTIONS_AFTER_TAKE_QUIET_SECONDS
                if self._chats_taken == seen or waited >= CONNECTIONS_AFTER_TAKE_LATEST_SECONDS:
                    break
            read_for = self._chats_taken
            await self.reconcile_connections()
            if self._chats_taken == read_for:
                return

    async def sync_once(self) -> None:
        """Reconcile mirrors with the chat list: start what is bound here, stop
        what is gone."""
        async with self._sync_lock:
            round_ = _TakeRound(fresher={})
            # Whatever ended the pass — a listing that failed, a backend
            # without the routes — a chat named during it is still owed its
            # re-read, and its callers their answer: the round is finished
            # either way.
            await self._in_round(round_, self._pass(round_))

    async def _pass(self, round_: _TakeRound) -> None:
        """One walk of the listing, under the lock."""
        chats: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        # A bounded walk: a runaway cursor must not loop for ever. The
        # bound is the deployment's, because an org with more chats than it
        # carries has the ones past it torn down as if they were deleted.
        pages = chat_list_pages()
        walked = 0
        while walked < pages:
            walked += 1
            try:
                page = await self._rest.list_chats(cursor=cursor)
            except CloudApiError as exc:
                if exc.status == 404:
                    logger.info("the chat routes are not available on this backend yet")
                    return
                logger.warning("chat list failed: %s", exc)
                return
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.info("chat list unreachable: %s", exc)
                return
            for item in page.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    chats[item["id"]] = item
            cursor = page.get("next_cursor")
            if not cursor:
                break
        round_.relisted_at = self._clock()
        # Chats this pass dealt with from a read NEWER than its listing (a
        # frame, a re-read of the newest page), by id, with that read.
        # Only this pass's, and only those: the sweep below spares a chat
        # here that the listing lacks, and the loop does not act on the
        # listing's older row for one.
        fresher = round_.fresher
        assert fresher is not None
        self._woken_this_pass = set()
        now = datetime.now(UTC)
        left: list[dict[str, Any]] = []
        try:
            for chat_id, chat in in_order_of_need(chats, now=now):
                # Somebody may be waiting on a chat this pass never listed: one
                # a frame named, or one bound since the listing was read. Those
                # are started before the long tail, and never wait on it.
                await self._look_up(round_)
                if chat_id in fresher:
                    continue
                if chat_id in self._sleeping:
                    # Its folder is on its way back to the drive; the first pass
                    # after the sleep takes it, and this one does not wait on it.
                    continue
                if self._left_for_later(chat_id, chat, now=now):
                    left.append(chat)
                    continue
                await self._while_taking(
                    self._take(chat_id, chat, start_owed_turn=True, unless_in=fresher, polled=True),
                    round_,
                )
        finally:
            self._woken_this_pass = None
        await self._leave_for_later(left)
        await self._finish_round(round_)
        for chat_id in list(self._mirrors):
            if chat_id not in chats and chat_id not in fresher:
                # A chat the list no longer carries is one the backend will
                # not serve to this box again: a tombstone is hidden from
                # every read, so there is no chat left to open whatever this
                # box would push.
                await self._stop_mirror(chat_id, chat_gone=True)

    async def _take(
        self,
        chat_id: str,
        chat: dict[str, Any],
        *,
        start_owed_turn: bool,
        unless_in: Mapping[str, Any] | None = None,
        beside: bool = False,
        named: bool = False,
        polled: bool = False,
    ) -> None:
        """Serve or let go of one chat as its row says; with
        ``start_owed_turn``, a chat that owes a turn has it handed to the
        harness (bounded) before this returns.

        One take per chat at a time. ``unless_in`` is the pass's record of the
        chats read after its listing: a chat that turns up there while this
        take waited for the chat's lock has been taken from the newer row, and
        the listing's older one is not acted on after it. A take ``beside`` the
        pass runs in one of the bounded slots, taken only once the chat's own
        lock is: a chat whose take is already in flight must not hold a slot
        another chat could be taken in. A take a frame ``named`` runs in the
        frames' own lane, so a reader's message never waits behind the
        re-read's takes.
        """
        slot: contextlib.AbstractAsyncContextManager[Any]
        if named:
            slot = self._named_slots
        elif beside:
            slot = self._beside_slots
        else:
            slot = contextlib.nullcontext()
        async with self._chat_take(chat_id), slot:
            if unless_in is not None and chat_id in unless_in:
                return
            if self._ended_under_me(chat_id, chat):
                await self._drop_ended(chat_id)
                return
            if self._serves(chat):
                await self._ensure_mirror(chat_id, chat, polled=polled)
                if start_owed_turn and chat.get("pending_turn"):
                    await self._start_owed_turn(chat_id)
            elif chat_id in self._mirrors:
                await self._stop_mirror(
                    chat_id,
                    chat_gone=bool(chat.get("deleted_at")),
                    moved=self._bound_elsewhere(chat),
                )

    def _bound_elsewhere(self, chat: Mapping[str, Any]) -> bool:
        """Whether the row binds ``chat`` to a machine that is not this one."""
        bound = chat.get("machine_id")
        return bool(bound) and self._machine_id is not None and bound != self._machine_id

    def _chat_take(self, chat_id: str) -> contextlib.AbstractAsyncContextManager[None]:
        """Hold ``chat_id``'s take lock."""
        return _per_chat(self._take_locks, chat_id)

    # -- the takes beside the pass ----------------------------------------------

    async def _in_round(self, round_: _TakeRound, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` under the lock, then settle every take it started beside
        itself. Cancelled, the takes beside are cancelled with it and whoever
        waits on a named chat is told."""
        try:
            try:
                await work
            except asyncio.CancelledError:
                raise
            except BaseException:
                await self._finish_round(round_)
                raise
            await self._finish_round(round_)
        except asyncio.CancelledError:
            self._cut_round(round_)
            raise

    async def _while_taking(self, take: Coroutine[Any, Any, None], round_: _TakeRound) -> None:
        """Await one take of the lock holder's own, starting meanwhile every
        chat a frame names and every chat the re-read finds owing a turn.

        The take runs as a task so that waiting on it is not the only thing
        the holder does: a folder take through the box's tunnel can run for
        minutes, and a reader who sends a chat its first message in those
        minutes must not wait for all of them.
        """
        task = self._in_background(take, "cloud-mirror-take")
        try:
            while not task.done():
                await self._look_up(round_)
                if task.done():
                    break
                await self._wake_for(round_, {task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        task.result()

    async def _finish_round(self, round_: _TakeRound) -> None:
        """Wait for every take started beside the holder's own, starting the
        chats frames name meanwhile, until there is nothing left to start."""
        while True:
            await self._look_up(round_)
            running = {task for task in round_.beside if not task.done()}
            if not running and not self._named:
                break
            await self._wake_for(round_, running)
        round_.beside.clear()

    def _cut_round(self, round_: _TakeRound) -> None:
        """The holder was cancelled: so are its takes beside, and whoever is
        waiting on a chat nobody will now take is told."""
        for task in round_.beside:
            task.cancel()
        for waiting in self._named.values():
            for future in waiting:
                if not future.done():
                    future.cancel()
        self._named.clear()

    async def _wake_for(self, round_: _TakeRound, tasks: set[asyncio.Task[None]]) -> None:
        """Wait until one of ``tasks`` ends, a frame names a chat, or it is
        time to look again."""
        if self._named:
            return
        named = asyncio.get_running_loop().create_task(self._named_waiting.wait())
        try:
            await asyncio.wait(
                {*tasks, named}, timeout=PASS_WAKE_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            named.cancel()

    async def _look_up(self, round_: _TakeRound) -> None:
        """Start every chat named since the last look, and read the newest page
        again when it is due."""
        self._start_named(round_)
        if (
            round_.relisted_at is not None
            and round_.fresher is not None
            and self._clock() - round_.relisted_at >= PASS_RELIST_SECONDS
        ):
            await self._take_newly_owed(round_)
            round_.relisted_at = self._clock()

    def _start_named(self, round_: _TakeRound) -> None:
        """Start a take for every chat a frame named while the lock was held,
        each in a task of its own beside the holder's."""
        self._named_waiting.clear()
        while self._named:
            chat_id = next(iter(self._named))
            waiting = self._named.pop(chat_id)
            if chat_id in self._sleeping:
                # Taken on the first pass after its sleep, which reads it anew;
                # waiting on the sleep here would hold the whole round on it.
                for future in waiting:
                    if not future.done():
                        future.set_result(None)
                continue
            self._beside(round_, self._take_named(chat_id, waiting, round_.fresher), chat_id)

    def _beside(self, round_: _TakeRound, work: Coroutine[Any, Any, None], chat_id: str) -> None:
        round_.beside.add(self._in_background(work, f"cloud-mirror-take:{chat_id}"))

    def _in_background(self, work: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        """A task the stop settles with the rest of the loops' work."""
        task = asyncio.get_running_loop().create_task(work, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _take_named(
        self,
        chat_id: str,
        waiting: list[asyncio.Future[None]],
        fresher: dict[str, dict[str, Any]] | None,
    ) -> None:
        """Re-read one chat a frame named, take it, and answer whoever is
        waiting on it. A chat read here that is still there joins ``fresher``
        (the running pass's record of what it read after its listing)."""
        try:
            chat = await self._reread(chat_id)
            if chat is not None:
                if fresher is not None:
                    fresher[chat_id] = chat
                await self._take(chat_id, chat, start_owed_turn=True, beside=True, named=True)
        except BaseException as exc:
            for future in waiting:
                if not future.done():
                    if isinstance(exc, asyncio.CancelledError):
                        future.cancel()
                    else:
                        future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
            logger.exception("chat %s could not be reconciled", chat_id)
            return
        for future in waiting:
            if not future.done():
                future.set_result(None)

    async def _take_newly_owed(self, round_: _TakeRound) -> None:
        """Read the newest page again and start, now, a take for each chat on
        it that owes a turn and is not served here yet. The frame that names a
        new chat is best-effort; this is what bounds the wait when it is lost."""
        fresher = round_.fresher
        assert fresher is not None
        try:
            page = await self._rest.list_chats()
        except (CloudApiError, httpx.HTTPError, OSError, ValueError) as exc:
            logger.info("the mid-pass re-read of the chat list failed: %s", exc)
            return
        for item in page.get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            chat_id = item["id"]
            if chat_id in self._mirrors or not item.get("pending_turn"):
                continue
            if chat_id in self._take_locks or not self._serves(item):
                # Already being taken — by the pass, or beside it.
                continue
            fresher[chat_id] = item
            self._beside(round_, self._take_quietly(chat_id, item), chat_id)

    async def _take_quietly(self, chat_id: str, chat: dict[str, Any]) -> None:
        """A take beside the pass: its failure is this chat's, not the pass's."""
        try:
            await self._take(chat_id, chat, start_owed_turn=True, beside=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat %s could not be reconciled", chat_id)

    async def _reread(self, chat_id: str) -> dict[str, Any] | None:
        """One chat's row, or ``None`` when there is nothing to act on (a
        chat that is gone has its mirror stopped here)."""
        try:
            return await self._rest.get_chat(chat_id)
        except CloudApiError as exc:
            if exc.status == 404:
                await self._stop_mirror(chat_id, chat_gone=True)
            else:
                logger.warning("chat %s could not be read: %s", chat_id, exc)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.info("chat %s unreachable: %s", chat_id, exc)
        return None

    async def reconcile_chat(self, chat_id: str) -> None:
        """One chat changed: re-read it and start/stop its mirror.

        Returns once the chat has been dealt with. While another caller holds
        the lock the chat is handed to it, which starts its take at once — it
        does not wait behind whatever that caller is taking: a start-up pass
        over hundreds of chats runs for minutes, and a single folder take
        through a slow link can too."""
        if self._sync_lock.locked():
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._named.setdefault(chat_id, []).append(future)
            self._named_waiting.set()
            await future
            return
        async with self._sync_lock:
            round_ = _TakeRound(fresher=None)
            await self._in_round(round_, self._reconcile_one(chat_id, round_))

    async def _reconcile_one(self, chat_id: str, round_: _TakeRound) -> None:
        if chat_id in self._sleeping:
            # The first pass after its sleep reads it anew; holding the lock
            # through the sleep would hold that pass too.
            return
        chat = await self._reread(chat_id)
        if chat is not None:
            await self._while_taking(self._take(chat_id, chat, start_owed_turn=False), round_)

    async def _chat_named(self, chat_id: str) -> None:
        """A ``chat.updated`` frame on the event stream. The stream moves on
        to its next frame at once, whatever this chat's take costs: it carries
        every chat's frames and every lease change, and none of them waits for
        this one. While a take holds the lock the chat is handed to its holder;
        otherwise its own reconcile starts in the background."""
        if self._sync_lock.locked():
            self._named.setdefault(chat_id, [])
            self._named_waiting.set()
            return
        self._in_background(self._reconcile_quietly(chat_id), f"cloud-mirror-named:{chat_id}")

    async def _reconcile_quietly(self, chat_id: str) -> None:
        try:
            await self.reconcile_chat(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat %s could not be reconciled", chat_id)

    @staticmethod
    async def _follow_record(mirror: ChatMirror, chat: dict[str, Any]) -> None:
        """The row is the durable word on a chat's stance and model, read on every
        poll tick and ``chat.updated``: a switch whose relay reached nobody (the
        socket down for that moment) is applied here. Only what it states moves."""
        if "permission_mode" in chat:
            mirror.adopt_mode(_stored_mode(chat.get("permission_mode")))
        if chat.get("model") is not None:
            await mirror.adopt_model(chat.get("model"))

    @staticmethod
    def _catch_up(mirror: ChatMirror, chat: dict[str, Any]) -> None:
        """In the background, and never blocking the reconcile: a pass waits for
        each turn it starts, and the chat row's own counter makes it free when
        nothing has been said since the last one."""
        last_seq = chat.get("last_seq")
        mirror.request_catch_up(last_seq=last_seq if isinstance(last_seq, int) else None)

    def _left_for_later(self, chat_id: str, chat: dict[str, Any], *, now: datetime) -> bool:
        """Whether the pass leaves this bound chat untaken: nobody has touched
        it for an hour and nothing waits in it.

        A folder is taken when the chat is used, not when the box claims it:
        taking every claimed chat at once (a lease, a full walk of the drive and
        an agent server apiece) would leave the chat a reader just wrote to
        waiting behind all of them. A chat with a turn owed or a reader
        looking (the wake stamp) is taken now, and so is one written to within
        the hour: a reader mid-session on a box that restarted finds the chat
        live again without asking. One this box already serves is kept. A row
        that carries no activity stamp is taken as it always was. The rest wait
        for the frame, the message or the wake that comes for them, exactly as
        a chat a box put to sleep does.
        """
        if not self._serves(chat) or chat_id in self._mirrors or chat_id in self._claimed:
            return False
        if chat_id in self._take_locks or somebody_waits(chat):
            return False
        idle = untouched_for(chat, now=now)
        return idle is not None and idle > RECENT_ACTIVITY_SECONDS

    async def _leave_for_later(self, left: list[dict[str, Any]]) -> None:
        """Say once how many chats the pass left, and tell the row of one that
        does not already read asleep that it is — so a reader's banner does
        not promise a machine that is not serving it, and the wake they ask
        for is the ordinary one. Never one a frame took while the pass ran."""
        if len(left) != self._deferred_said:
            self._deferred_said = len(left)
            if left:
                logger.info(
                    "%d bound chat(s) untouched for over an hour with nothing owed are left "
                    "until a reader or a message comes for them",
                    len(left),
                )
        for chat in left:
            chat_id = str(chat.get("id"))
            if chat_id in self._deferred or chat_id in self._mirrors or chat_id in self._take_locks:
                continue
            self._deferred.add(chat_id)
            if chat.get("machine_status") != "asleep" and self._machine_id is not None:
                await self.report_publisher_state(chat_id, "asleep")

    def _serves(self, chat: dict[str, Any]) -> bool:
        if chat.get("deleted_at"):
            return False
        if self.org is not None and self.org.refusal(chat) is not None:
            return False
        if self._draining and chat.get("id") not in self._mirrors:
            # A box on its way out takes nothing it is not already holding —
            # including a chat the backend has not yet moved off it. Opening
            # one here would be a session spawned to be torn down a moment
            # later, on a lease the next box then has to wait out.
            return False
        if self._signed_out is not None and chat.get("id") not in self._mirrors:
            # Every call a new chat needs would be refused, and a chat taken
            # here would only hold a lease another box could serve it under.
            return False
        bound = chat.get("machine_id")
        if self._machine_id is None:
            # No machine identity yet, so nothing is served: a chat picked up
            # before the box knows the id it publishes as could not be
            # published, and a box that cannot tell which chats are its own
            # (a 404 on the machine routes included) must not take them all.
            return False
        return bound == self._machine_id

    # -- how many agent servers this box keeps ---------------------------------

    def activity_counts(self) -> dict[str, int]:
        return activity_counts(m.activity for m in self._mirrors.values())

    def _capacity(self) -> int:
        """What this box tells the platform it can hold. The configured cap when
        there is one; otherwise what its hardware argues for — its cores, and
        no more than its memory holds — since placement spreads a shared pool
        by this number, and a box with no local cap still has a size worth
        spreading by. A configured cap above what the memory holds reports
        what the memory holds: the platform is told what the box can serve,
        not what somebody hoped. Not what the beats are pooled by: those go by
        how many folders the box is actually holding."""
        cap = self._configured_cap()
        argued = cap if cap is not None else default_max_mirrors()
        memory = self._settings.memory_max_mirrors
        return argued if memory is None else min(argued, memory)

    def _configured_cap(self) -> int | None:
        """The cap somebody set: the configured one, the one this box's socket
        was shown to have, the smaller where both are known, or ``None``."""
        caps = [c for c in (self._settings.max_mirrors, self._channel_cap) if c is not None]
        return min(caps) if caps else None

    def _effective_cap(self) -> int | None:
        """The most chats this box serves at once: the configured cap, and in
        any case no more agent servers than its memory holds — a cap nobody
        set, but one the box cannot argue with. ``None`` when neither is known,
        and then the box serves every chat bound to it."""
        caps = [
            c for c in (self._configured_cap(), self._settings.memory_max_mirrors) if c is not None
        ]
        return min(caps) if caps else None

    def hear_presence(self, frame: PresenceFrame) -> None:
        """A reader's presence on a chat's document counts as using the chat."""
        self._policy.hear_presence(frame, self._socket.peer_id)

    def _idle_for(self, chat_id: str, with_work: bool = True) -> float | None:
        """How long this chat has had nothing running and nobody using it, or
        ``None`` while it is not idle (``SleepPolicy.idle_for``)."""
        mirror = self._mirrors.get(chat_id)
        return self._policy.idle_for(chat_id, mirror, with_work=with_work)

    def _note_reader_open(self, chat_id: str, mirror: ChatMirror, chat: Mapping[str, Any]) -> None:
        """Tell the mirror a reader opened the chat.

        A reader who opens a chat and only reads it puts nothing on the
        document — the box takes no presence there — so the wake the backend
        stamps on the row is the only place that open is visible. It is a
        reader in the chat, and so it re-arms the window a parked ask goes
        cold on, exactly as a message would.
        """
        woke = chat.get("wake_requested_at")
        woke = woke if isinstance(woke, str) and woke else None
        if woke is not None and self._woke_at.get(chat_id) != woke:
            self._woke_at[chat_id] = woke
            mirror.note_reader()

    async def sweep_idle_mirrors(self) -> list[str]:
        """Close the mirror of every chat that has owed nothing, and that
        nobody has used, past the backstop window; then, if the chats' memory
        is past its pressure line, sleep the least recently used idle chats
        until it is not. Returns what was closed."""
        self._policy.note_activity(self._mirrors)
        window = self._policy.idle_window
        closed: list[str] = []
        for chat_id in list(self._mirrors):
            idle = self._idle_for(chat_id)
            if idle is None or idle < window:
                continue
            if chat_id in self._take_locks:
                # Somebody is taking it right now: it is not idle to them.
                continue
            async with self._chat_take(chat_id):
                # Under the chat's take lock, so a take of the same chat (the
                # poll's, or a frame's) waits for the sleep to finish rather
                # than serving it while its folder is being handed back.
                idle = self._idle_for(chat_id) if chat_id in self._mirrors else None
                if idle is None or idle < window:
                    continue
                if await self._policy.holds_work(chat_id):
                    continue
                if (idle := self._idle_for(chat_id)) is None or idle < window:
                    continue  # it was used while its sandbox was being read
                logger.info(
                    "chat %s has had nothing running for %.0f min; closing its mirror "
                    "(the agent server goes with it, the next question re-opens it)",
                    chat_id,
                    idle / 60.0,
                )
                self._sleeping.add(chat_id)
                try:
                    await self._release_mirror(chat_id, ending=IDLE_ENDING)
                finally:
                    self._sleeping.discard(chat_id)
                closed.append(chat_id)
        closed += await self._relieve_memory_pressure()
        await self._workspaces.sweep(
            pressure=bool(self._policy.pressure()) and not closed, served=self._served_now()
        )
        return closed

    async def _least_recently_used_idle(
        self, *, why: str, exclude: frozenset[str] = frozenset()
    ) -> str | None:
        """The chat this box sleeps first to make room (``SleepPolicy.pick_victim``).
        A chat woken this pass is never the one chosen."""
        skip = exclude | (self._woken_this_pass or set())
        chats = [c for c in self._mirrors if c not in skip]
        return await self._policy.pick_victim(chats, self._idle_for, why=why)

    async def _relieve_memory_pressure(self) -> list[str]:
        """Past the memory line, sleep the least recently used idle chat: one
        per sweep, so a cgroup reading that lags the sleep cannot take a second
        chat for memory the first already gave back. When every chat has a
        turn, an ask or a flush in flight the box says so once."""
        if (pressure := self._policy.pressure()) is None:
            self._policy.clear_stuck()
            return []
        victim = await self._least_recently_used_idle(
            why="memory", exclude=frozenset(self._take_locks)
        )
        if victim is None:
            self._policy.say_stuck(pressure)
            return []
        stopped = self._policy.stopped_by_sleep(victim)
        async with self._chat_take(victim):
            if victim not in self._mirrors or self._idle_for(victim) is None:
                return []
            logger.info(
                "the chats' %s is at %d of %d MiB; putting %s to sleep, the least "
                "recently used of the idle chats",
                pressure.resource,
                pressure[0] // MIB,
                pressure[1] // MIB,
                victim,
            )
            if stopped:
                # Never a silent loss: the chat is told what the sleep stops.
                await self._mirrors[victim].note_pressure_sleep(
                    pressure_sleep_sentence(pressure.resource, stopped)
                )
            await self._release_mirror(victim, ending=EVICTED_ENDING)
        return [victim]

    async def _make_room_for(
        self, chat_id: str, *, evict: bool = True, held_back: str = REFUSED_SLOT_WAIT
    ) -> bool:
        """Free a slot for a chat this box is not serving yet, by closing the
        least recently used IDLE mirror (``_least_recently_used_idle``). True
        at once when nothing bounds the box (no configured cap, and a memory it
        could not read). False when every slot is held by a chat with something
        in flight: nothing is taken from a reader who is waiting, so the new
        chat is served on a later pass instead."""
        cap = self._effective_cap()
        if cap is None:
            self._no_room.discard(chat_id)
            return True
        self._policy.note_activity(self._mirrors)
        while len(self._mirrors.keys() | (self._claimed - {chat_id})) >= cap:
            woken = self._woken_this_pass or set()
            busy = frozenset(self._take_locks)  # being taken: not idle to whoever takes it
            candidates = [
                c
                for c in self._mirrors
                if c not in woken and c not in busy and self._idle_for(c) is not None
            ]
            if candidates and not evict:
                self._wait_for_slot(chat_id, held_back)
                return False
            victim = (
                await self._least_recently_used_idle(why="a slot", exclude=busy)
                if candidates
                else None
            )
            if victim is not None and victim in self._take_locks:
                continue  # a take began while the victim was picked; pick again
            if victim is None:
                self._wait_for_slot(
                    chat_id,
                    "chat %s waits: this box is capped at %d chats and every one it holds "
                    "has something running or a reader waiting; it is served on a later pass",
                    cap,
                )
                return False
            # Under the victim's take lock, free at this instant (nothing awaited
            # since the check): a frame for it waits for the sleep to finish
            # rather than serving it while its folder is being handed back.
            async with self._chat_take(victim):
                if victim not in self._mirrors or self._idle_for(victim) is None:
                    continue
                logger.info(
                    "chat %s needs a slot; closing the mirror of %s, the least recently used "
                    "of the %d this box serves",
                    chat_id,
                    victim,
                    len(self._mirrors),
                )
                await self._release_mirror(victim, ending=EVICTED_ENDING)
        self._no_room.discard(chat_id)
        return True

    def _served_now(self) -> set[str]:
        """The chats this box serves or is taking at this moment."""
        return {*self._mirrors, *self._claimed, *self._take_locks}

    def _wait_for_slot(self, chat_id: str, why: str, *args: object) -> None:
        """Said once per wait, and reported ``waiting`` off the take (``Reporter``)."""
        if chat_id not in self._no_room:
            logger.info(why, chat_id, *args)
            self._no_room.add(chat_id)
            waiting = self.report_publisher_state(chat_id, "waiting")
            self._in_background(waiting, f"cloud-mirror-waiting:{chat_id}")

    async def _start_owed_turn(self, chat_id: str) -> None:
        """A chat the listing says owes a turn: wait (bounded) until that turn
        is handed to its harness before the pass takes the next chat."""
        mirror = self._mirrors.get(chat_id)
        if mirror is None:
            return
        if not await mirror.wait_for_owed_turn(OWED_TURN_START_SECONDS):
            logger.info(
                "chat %s: its owed turn had not started after %.0f s; the pass moves on",
                chat_id,
                OWED_TURN_START_SECONDS,
            )

    async def _ensure_mirror(
        self, chat_id: str, chat: dict[str, Any], *, polled: bool = False
    ) -> None:
        """Serve ``chat_id`` as its row says. ``polled`` is the discovery pass's
        row rather than one a frame named: while the event stream is up it
        reads no transcript, since a chat that was said something in has a
        frame of its own on the way."""
        seq = chat.get("last_seq")
        seq = seq if isinstance(seq, int) else None
        self._seen_seq[chat_id] = seq
        self._note_owner(chat_id, chat)
        existing = self._mirrors.get(chat_id)
        if existing is not None:
            if existing.state in ("running", "starting"):
                # Already serving it — but a message relayed into a moment when
                # the socket was down reached nobody, and only the transcript
                # remembers it. The chat row's own counter makes this free when
                # nothing has been said since the last pass.
                await self._follow_record(existing, chat)
                self._note_reader_open(chat_id, existing, chat)
                await self._clear_stale_refusal(chat_id, existing, chat)
                if not (polled and self._stream_live):
                    self._catch_up(existing, chat)
                return
            await self._stop_mirror(chat_id)
        if chat_id in self._released:
            released = self._released[chat_id]
            if (
                released is not None
                and (seq is None or seq <= released)
                and not chat.get("wake_requested_at")
            ):
                # Nothing has been said since this box handed the chat's mirror
                # back, and nobody has opened it. Taking a slot from another
                # chat to re-open it would only make it the next thing evicted.
                # A reader opening the chat is the other thing that wakes it:
                # the backend stamps the wake on the row, and the row is what
                # this box reads.
                return
            del self._released[chat_id]
        elif left_asleep(chat):
            # A box put this chat to sleep — this one, before it restarted, or
            # the one before it — and nothing has come for it since: nobody
            # opened it (the backend stamps that wake on the row) and nothing
            # waits in it. A restart that re-opened every chat it had ever
            # slept spawned a server per chat the org has, which is the very
            # count the sleep exists to bound — and took every one of their
            # folders again on the way.
            return
        if not self._may_retry_start(chat_id, chat):
            return
        evict, held_back = slot_admission(chat_id, chat, self._refusal_waits, self._start_failures)
        if not await self._make_room_for(chat_id, evict=evict, held_back=held_back):
            return
        # The slot is this chat's from here, while its folder is taken: another
        # take running beside this one must not find it free.
        self._claimed.add(chat_id)
        try:
            # Waking the chat starts with taking its folder: the box that holds
            # the lease is the box that may write the chat, so a folder somebody
            # else still has means this chat is not served here at all — never
            # served over another box's turn. A member's workspace is taken first.
            if await self._workspaces.join(chat_id, chat) is Joined.NOT_HERE:
                return
            if not await self._takes.take(chat_id, chat):
                await self._workspaces.leave(chat_id)
                return
            mirror = self._mirror_factory(chat_id, chat)
            self._mirrors[chat_id] = mirror
            self._end_seq[chat_id] = _end_seq_of(chat)
            if self._woken_this_pass is not None and chat.get("wake_requested_at"):
                self._woken_this_pass.add(chat_id)
        finally:
            self._claimed.discard(chat_id)
        # The chat is held here from this point (the connections client reads
        # the held set), so the read its owner's connections are owed can
        # start — and the first spawn waits, bounded, for it.
        await self._connections_before_first_spawn()
        # The mirror is what knows where the agent runs, and that directory —
        # not the folder around it — is what streams: the chat's own records
        # sit beside it and stay this box's. Started before the first turn, so
        # a reader watching the chat sees the first file as it is written.
        await self._start_live(chat_id, mirror)
        self._policy.stamp(chat_id, mirror)
        try:
            await mirror.start()
        except ChatMirrorStoppedError:
            # A drain (or a sleep for the slot) took this chat back while its
            # mirror was still coming up. The stop did the tearing down and
            # took the chat off this box's table; a start that carried on from
            # there would have raised the folder and the session it had just
            # given back. Not a failure to count, and nothing to say on the
            # chat: the box is on its way out and another one takes it.
            logger.info("chat %s was handed back while its mirror was starting", chat_id)
            return
        except ChatMirrorRefusedError as refused:
            # The gateway would not let this box publish the chat: no session
            # was opened and nothing was spent. Said once per chat and reason
            # (the poll retries it, so the cause clearing heals it), kept on
            # the service for whoever asks, and put on the chat itself so the
            # reader's banner says it rather than a composer that never answers.
            if refused.code == CHANNEL_CAP_REFUSAL:
                await self._at_channel_cap(chat_id, mirror)
                return
            await self._start_refused(chat_id, chat, mirror, refused.code)
            return
        except Exception as exc:
            await self._start_failed(chat_id, chat, mirror, exc)
            return
        self._pushed_at[chat_id] = mirror.published_count
        self._refused.pop(chat_id, None)
        self._start_failures.pop(chat_id, None)
        self._refusal_waits.pop(chat_id, None)
        self._deferred.discard(chat_id)
        if self._machine_id is not None:
            # The chat is awake here: the row says so (it read `asleep` since
            # the last sleep, or `refused` from a past life of this box), and
            # the wake a reader requested is answered by this very open.
            await self.report_publisher_state(chat_id, "publishing")

    def _note_owner(self, chat_id: str, chat: Mapping[str, Any]) -> None:
        """Remember the chat's person from its row: their id and their name."""
        if not chat.get("owner_user_id"):
            return
        owner = str(chat["owner_user_id"])
        name = str(chat.get("owner_display_name") or "").strip()
        known = self._owners.get(chat_id)
        if not name and known is not None and known[0] == owner:
            # A read that did not carry the name keeps the one known.
            name = known[1]
        self._owners[chat_id] = (owner, name[:255])

    def chat_person(self, chat_id: str) -> ActingFor | None:
        """The person a chat's agent acts for: the chat's owner, named as the
        chat's row names them, so what the agent does reads "Alkera agent for
        <name>" live as well as in what the platform answers later."""
        owner = self._owners.get(chat_id)
        if owner is None:
            return None
        return ActingFor(id=f"user:{owner[0]}", display_name=owner[1])

    async def _clear_stale_refusal(
        self, chat_id: str, mirror: ChatMirror, chat: Mapping[str, Any]
    ) -> None:
        """The row still says the chat cannot run while this box serves it:
        a refusal said while the mirror kept running (a take or hand-back that
        failed beside it), or one whose clearing report never landed. The
        composer would stay disabled over a chat that answers, so the box says
        ``publishing`` again, once per refusal it sees."""
        reason = chat.get("machine_refusal_reason")
        if not reason or mirror.state != "running" or self._machine_id is None:
            self._refusals_cleared.pop(chat_id, None)
            return
        if self._refusals_cleared.get(chat_id) == reason:
            return
        self._refusals_cleared[chat_id] = str(reason)
        logger.info("chat %s: its row still read refused while it is served here", chat_id)
        await self.report_publisher_state(chat_id, "publishing")

    async def report_publisher_state(
        self,
        chat_id: str,
        state: str,
        reason: str = "",
        *,
        ending: str | None = None,
        kind: ChatRefusalKind | None = None,
    ) -> None:
        """Put the box's verdict on the chat, best-effort (``Reporter``). A
        refusal names its ``kind``, which is what a reader is shown. A
        member's also says how its workspace is."""
        await self._reporter(
            chat_id,
            state,
            reason,
            ending=ending,
            kind=kind,
            fields=self._workspaces.report_facets(chat_id),
        )

    async def _at_channel_cap(self, chat_id: str, mirror: ChatMirror) -> None:
        """The socket may hold no more chat channels. Learn the cap from the
        number held when it bit, take the refused mirror down (no session was
        opened), make a slot by sleeping the idlest chat, and take the chat
        again — in the background, once this take has let go of the chat's
        lock. With every held chat busy there is no slot to make: the chat
        waits for one, and nothing is put on it either way."""
        held = len([c for c in self._mirrors if c != chat_id])
        if held >= 1 and (self._channel_cap is None or held < self._channel_cap):
            self._channel_cap = held
            logger.warning(
                "this box's socket holds %d chat channels and the platform allows no more; "
                "it serves at most that many chats at once from here on, sleeping the "
                "idlest to make room",
                held,
            )
        self._mirrors.pop(chat_id, None)
        with contextlib.suppress(Exception):
            await mirror.stop()
        await self._workspaces.leave(chat_id)  # the retake seats it again
        self._refused.pop(chat_id, None)
        if await self._make_room_for(chat_id):
            self._in_background(
                self._reconcile_quietly(chat_id), name=f"cloud-mirror-retake-{chat_id}"
            )
        else:
            logger.info(
                "chat %s waits for a slot: every chat this box serves has something running",
                chat_id,
            )

    async def _on_mirror_refused(self, chat_id: str, refusal: PublishingRefusal) -> None:
        """A running mirror was refused mid-turn: it has stopped the turn;
        this puts the reason on the chat and remembers it for the next poll.
        The channel cap is not a reason: it is this box's own capacity, met by
        sleeping the idlest chat, and the turn is taken up again on the next
        look at the chat."""
        if refusal.code == CHANNEL_CAP_REFUSAL:
            held = len([c for c in self._mirrors if c != chat_id])
            if held >= 1 and (self._channel_cap is None or held < self._channel_cap):
                self._channel_cap = held
            logger.warning(
                "chat %s: publishing was refused mid-turn by the channel cap (%d held); "
                "making room and taking it up again",
                chat_id,
                held,
            )
            self._refused.pop(chat_id, None)
            await self._make_room_for(chat_id)
            return
        if refusal_kind(refusal.code) == "transient":
            # Nothing is said on the chat: the next poll takes it again, and a
            # start refused the same way waits on its own backoff.
            logger.info("chat %s: publishing paused mid-turn: %s", chat_id, refusal.reason)
            self._refused.pop(chat_id, None)
            return
        self._refused[chat_id] = refusal.code
        logger.warning("chat %s: publishing was refused mid-turn: %s", chat_id, refusal.reason)
        sentence, kind = gateway_refusal(refusal.code)
        await self.report_publisher_state(chat_id, "refused", sentence, kind=kind)

    async def _park_every_mirror(self) -> None:
        """A supervised restart: take every chat's session down and keep its
        folder. The lease stays held under this box's holder identity, which
        the next process on the box asserts again, so it takes the folder
        straight back without a push, a pull or an ``asleep`` in between.

        Every chat at once, each on its own budget: one after the other, a
        session that would not close cost every chat behind it its whole budget
        too, and a box holding a dozen chats spent minutes of its restart on
        them — past the point where the process is ended hard, with the parks
        after it never attempted."""
        mirrors = [(chat_id, self._mirrors.pop(chat_id, None)) for chat_id in list(self._mirrors)]
        await asyncio.gather(
            *(
                self._finish_or_abandon(
                    mirror.stop(), f"cloud-mirror-park:{chat_id}", STOP_RELEASE_BUDGET_SECONDS
                )
                for chat_id, mirror in mirrors
                if mirror is not None
            ),
            return_exceptions=True,
        )

    async def _release_every_mirror(self) -> None:
        """Put every chat this box serves to sleep, several at a time.

        One at a time, a chat whose tree is large or whose drive has gone
        unroutable holds every chat behind it: their sessions stay up and their
        leases are handed back only when the TTL runs out, which is minutes in
        which no other box may serve them. One slow folder must cost its own
        chat and nothing else. Each release already swallows its own failures,
        so the fan-out changes only who waits for whom — never what a failure
        means. Bounded by what the box says it can hold, so a full box does not
        open one tree push per chat at once; the beats have their own pool,
        sized by the folders actually held rather than by this number.
        """
        chat_ids = list(self._mirrors)
        if not chat_ids:
            return
        at_once = asyncio.Semaphore(max(MIN_MAX_MIRRORS, self._capacity()))

        async def release(chat_id: str) -> None:
            async with at_once:
                # Budgeted per chat, from the moment its turn comes: a drive
                # that never answers the hand-back costs this chat its clean
                # sleep, and the chats after it still get theirs.
                await self._finish_or_abandon(
                    self._release_mirror(chat_id, ending=DRAINED_ENDING),
                    f"cloud-mirror-release:{chat_id}",
                    STOP_RELEASE_BUDGET_SECONDS,
                )

        await asyncio.gather(*(release(chat_id) for chat_id in chat_ids), return_exceptions=True)

    async def _release_mirror(self, chat_id: str, *, ending: str) -> None:
        """Put a chat to sleep: the cap needed the slot, or it went quiet.

        This is what sleep IS — the session goes, the chat's folder is pushed up
        and the lease handed back, and the counter that was true at the time is
        recorded so the next pass can tell "already answered" from "waiting".
        The chat is then resumable anywhere: the next box to serve it takes the
        lease and pulls the folder it just gave back.

        ``ending`` is why (``idle``, ``evicted``, ``drained``). The hand-back's
        release carries it, so the release IS the server's chat-end transition
        — one path for every way a chat stops, never a second beside it.
        """
        seq = self._seen_seq.get(chat_id)
        was_served = chat_id in self._mirrors
        await self._stop_mirror(chat_id, ending=ending)
        self._released[chat_id] = seq
        if was_served and self._machine_id is not None and self._signed_out is None:
            # Said AFTER the folder is back: a reader who sees `asleep` may
            # open the chat at once, and the box that answers must find the
            # lease free and the bytes up.
            await self.report_publisher_state(chat_id, "asleep", ending=ending)
        self._workspaces.forget_report(chat_id)

    async def _drop_ended(self, chat_id: str) -> None:
        """The server ended this chat under the box: let it go without a push.

        Its lease is already released — the server's transition did that — so
        a hand-back would only have its push refused. The folder is forgotten
        first, then the session goes, and the chat is recorded as answered at
        the counter it had, so the next pass does not simply take it back: a
        new message or a reader opening it is what wakes it again.
        """
        logger.info("chat %s was ended by the server; this box lets it go", chat_id)
        seq = self._seen_seq.get(chat_id)
        let_go = getattr(self._folders, "let_go", None)
        if self._folders.enabled and let_go is not None:
            async with self._folder_lock(chat_id):
                let_go(chat_id)
        await self._stop_mirror(chat_id)
        self._released[chat_id] = seq

    def _ended_under_me(self, chat_id: str, chat: Mapping[str, Any]) -> bool:
        """Whether the row says the server ended ``chat_id`` after this box
        took it."""
        taken = self._end_seq.get(chat_id)
        return chat_id in self._mirrors and taken is not None and _end_seq_of(chat) > taken

    async def _stop_mirror(
        self,
        chat_id: str,
        *,
        chat_gone: bool = False,
        ending: str | None = None,
        moved: bool = False,
    ) -> None:
        """Take the chat's session down and give its folder back.

        ``chat_gone`` says the backend no longer has the chat at all. It changes
        one thing: what happens if the folder has gone from the drive too. That
        is the ordinary shape of a delete — deleting a chat trashes the folder
        it IS — and a box that landed its working copy somewhere else would put
        the deleted chat's files back in the drive. A folder that is still there
        is pushed to either way; the work is only ever left behind when there is
        no live folder AND no chat to open it with. A chat the stream said was
        deleted leaves nothing on this disk, its workspace's shared tree neither.
        ``moved``: the chat is bound to another machine, and its workspace is
        handed back with its last member whatever it left running here.
        """
        gone = chat_id in self._deleted
        self._deleted.discard(chat_id)
        mirror = self._mirrors.pop(chat_id, None)
        self._policy.forget(chat_id)
        self._woke_at.pop(chat_id, None)
        self._no_room.discard(chat_id)
        self._released.pop(chat_id, None)
        self._start_failures.pop(chat_id, None)
        self._refusal_waits.pop(chat_id, None)
        self._takes.forget(chat_id)
        self._seen_seq.pop(chat_id, None)
        self._end_seq.pop(chat_id, None)
        self._pushed_at.pop(chat_id, None)
        self._push_retry.pop(chat_id, None)
        if mirror is not None:
            with contextlib.suppress(Exception):
                await mirror.stop()
        # After the session is down, never before: the push must carry what the
        # last turn wrote, and a session still running could write again behind
        # a lease this box has already handed back.
        await self._returns.hand_back(chat_id, recover=not chat_gone, ending=ending, gone=gone)
        await self._workspaces.leave(chat_id, ending=ending, gone=gone, moved=moved)
        # The lock is gone with the custody it guarded — unless somebody is in
        # it, which is the one case where dropping it would let a second caller
        # mint a lock nobody else is holding. A box serves chats for as long as
        # it runs, so the table has to shrink somewhere.
        lock = self._folder_locks.get(chat_id)
        if lock is not None and not lock.locked():
            self._folder_locks.pop(chat_id, None)

    # -- the chat folder this box holds while the chat is awake ----------------

    async def _start_live(self, chat_id: str, mirror: Any) -> None:
        """Stream this chat's working directory while the chat is awake.

        Never raises: a chat whose folder cannot be streamed is a chat saved at
        the checkpoint push, exactly as it was before there was a live plane.
        """
        working_dir = getattr(mirror, "working_dir", None)
        member = self._workspaces.seat(chat_id) is not None  # its workspace's streams
        if not self._folders.enabled or not isinstance(working_dir, Path) or member:
            return
        try:
            await asyncio.to_thread(self._folders.live, chat_id, working_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "chat %s: its folder is not streamed (%s); it is saved at each checkpoint",
                chat_id,
                exc,
            )

    def _folder_instance(self, chat_id: str) -> str:
        """The lease holder id for this chat on this box.

        Stable across a sleep and a resume on the same box, so a lease the
        server still holds is handed straight back to the same holder instead of
        racing a second one.
        """
        return f"{self._machine_id or machine_agent_id(self._settings.machine_name)}:{chat_id}"

    def _folder_beat_interval(self) -> float:
        """How long the beat loop waits between passes.

        The shortest cadence any lease this box holds was GRANTED — the server
        decides how often it wants to hear from a holder, and a box that beats
        slower than the fastest lease it holds loses that one. Nothing held (or
        a grant that named no cadence) falls back to a quarter of the lease TTL.
        """
        served: list[float] = []
        for chat_id in self._workspaces.custody_keys(self._mirrors):
            held = self._folders.held(chat_id)
            cadence = getattr(getattr(held, "record", None), "heartbeat_every", 0.0)
            if isinstance(cadence, int | float) and cadence > 0:
                served.append(float(cadence))
        return max(
            folder_beat_floor_seconds(),
            min(served) if served else folder_beat_seconds(),
        )

    def _folder_lock(self, chat_id: str) -> asyncio.Lock:
        """The lock over custody of one chat's folder.

        Per chat rather than one for the box: a beat exists to land inside its
        cadence, and a single lock would put it back behind whatever slow thing
        another chat is doing — which is the sequencing this loop was split out
        of in the first place.
        """
        lock = self._folder_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._folder_locks[chat_id] = lock
        return lock

    async def _beat_folders(self) -> None:
        """One heartbeat per folder this box holds, all of them at once, and
        nothing else at all.

        Concurrent rather than in turn, because a lease is kept by a beat
        landing inside its cadence and every folder's cadence runs at the same
        time: one beat that hangs on an unreachable drive must not be what
        makes the NEXT chat's lease lapse. Nothing slow shares this pass —
        taking in what the drive holds, pushing out what a turn wrote and
        retrying an owed hand-back all live in ``_upkeep_folders``.
        """
        if not self._folders.enabled:
            return
        for chat_id in await self._workspaces.dropped(self._mirrors):
            await self._stop_mirror(chat_id)
        if await self._beat_folders_at_once():
            return
        await asyncio.gather(
            *(
                self._beat_folder(chat_id)
                for chat_id in self._workspaces.custody_keys(self._mirrors)
            )
        )

    async def _beat_folders_at_once(self) -> bool:
        """Every held folder in one batched beat. False when the server (or the
        custody) does not serve the batch, so the pass beats each on its own.

        A box holds one lease per chat, so a beat per lease made an idle box's
        traffic grow with every chat it ran; one call per pass does not.

        Each folder's custody lock is held across the call, as the single beat
        holds it — but only a lock nobody holds is taken: a chat whose lock is
        held is being handed back, and waiting for that here would make every
        other chat's beat queue behind one hand-back. It sits this pass out and
        needs no beat for a folder it is returning.
        """
        beat_all = getattr(self._folders, "beat_all", None)
        if beat_all is None:
            return False
        taken: list[str] = []
        try:
            for chat_id in self._workspaces.custody_keys(self._mirrors):
                lock = self._folder_lock(chat_id)
                if lock.locked() or self._folders.held(chat_id) is None:
                    continue
                await lock.acquire()
                taken.append(chat_id)
            if not taken:
                return True
            loop = asyncio.get_running_loop()
            try:
                kept: dict[str, bool] | None = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._beat_executor(), functools.partial(beat_all, list(taken))
                    ),
                    folder_beat_timeout_seconds(),
                )
            except TimeoutError:
                logger.warning(
                    "the batched folder heartbeat did not answer inside %.0fs; the next pass "
                    "asks again",
                    folder_beat_timeout_seconds(),
                )
                return True
            except Exception:
                logger.exception("the batched folder heartbeat did not complete")
                return True
        finally:
            for chat_id in taken:
                self._folder_lock(chat_id).release()
        if kept is None:
            return False
        for key, still in kept.items():
            # A lease somebody else holds now: a chat's ends its mirror, a
            # workspace's every member's, since none may write into it.
            for chat_id in [] if still else await self._workspaces.to_stop(key, self._mirrors):
                await self._stop_mirror(chat_id)
        return True

    async def _beat_folder(self, chat_id: str) -> None:
        """Keep one chat's folder. A chat whose lease was taken away loses its
        mirror with it — it is no longer this box's to write.

        Under the chat's custody lock, and re-reading what is held INSIDE it: a
        chat swept between the pass starting and this beat has already handed
        its folder back, and beating for it afterwards would be a beat on a
        lease this box no longer holds. Only the hand-back holds that lock
        against us — never a push, which is exactly the thing a beat may not
        queue behind.
        """
        kept = True
        try:
            async with self._folder_lock(chat_id):
                if (
                    chat_id not in self._workspaces.custody_keys(self._mirrors)
                    or self._folders.held(chat_id) is None
                ):
                    return
                kept = await asyncio.wait_for(
                    self._beat_in_thread(chat_id), folder_beat_timeout_seconds()
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # Abandoned, not failed: the lease is not known to be gone, the
            # next pass asks again, and the live plane's own fence keeps every
            # write off the folder while none of the beats lands. What must not
            # happen is this chat's unreachable drive holding the pass open
            # while the leases beside it lapse.
            logger.warning(
                "chat %s: its folder heartbeat did not answer inside %.0fs; the pass carries on "
                "without it",
                chat_id,
                folder_beat_timeout_seconds(),
            )
            return
        except Exception:
            # One folder's beat is not the pass: a chat whose beat threw must
            # not cost the other chats on this box their leases.
            logger.exception("chat %s: its folder heartbeat did not complete", chat_id)
            return
        if not kept:
            # Outside the lock, never in it: the stop hands the folder back,
            # which takes the same lock. A workspace's lease stops its members.
            for stopping in await self._workspaces.to_stop(chat_id, self._mirrors):
                await self._stop_mirror(stopping)

    async def _beat_in_thread(self, chat_id: str) -> bool:
        """Run one beat on the pool that carries only beats."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._beat_executor(), functools.partial(self._folders.beat, chat_id)
        )

    def _beat_executor(self) -> ThreadPoolExecutor:
        """The threads the beats run on, wide enough for every folder held.

        Sized by the chats this box is actually serving, never by the capacity
        it advertises: how many chats a box holds has no cap — a chat held back
        is a reader waiting with nothing to see — so a pool cut to the
        advertised capacity is two workers on a two-vCPU box no matter how many
        folders are leased, and two beats stuck on an unreachable drive hold
        both of them while every other chat's lease lapses behind them.

        A pool cannot be resized, so growing means building the next one. The
        retired pool is not waited on: the beats already running on it finish
        on their own threads, and a pass is always complete before the next one
        is sized, so nothing is left awaiting the pool being replaced.
        """
        wanted = max(MIN_MAX_MIRRORS, len(self._mirrors))
        pool = self._beat_pool
        if pool is not None and self._beat_pool_width >= wanted:
            return pool
        if pool is not None:
            pool.shutdown(wait=False)
        pool = ThreadPoolExecutor(max_workers=wanted, thread_name_prefix="alkera-folder-beat")
        self._beat_pool = pool
        self._beat_pool_width = wanted
        return pool

    async def _upkeep_folders(self) -> None:
        """Bring every held folder up to date, on its own schedule.

        What the drive holds for the chat comes in (what the web wrote while
        nothing streamed, a frame lost to a dropped socket), what the last turn
        wrote goes out, a folder a failed sleep left owed is tried again, and
        one an earlier life of this box left held is settled. All of it slow,
        none of it what keeps a lease alive.
        """
        if not self._folders.enabled:
            return
        await asyncio.gather(
            self._returns.retry_owed(),
            self._returns.settle_left(),
            *(self._upkeep_folder(chat_id) for chat_id in list(self._mirrors)),
        )
        # A workspace's lock outlives its custody unless dropped here: a chat's
        # goes with its stop, and a workspace has no stop of its own.
        for key, lock in list(self._folder_locks.items()):
            if is_workspace_key(key) and not lock.locked() and self._folders.held(key) is None:
                del self._folder_locks[key]

    async def _upkeep_folder(self, chat_id: str, *, turn_end: bool = False) -> None:
        """Take in what the drive holds for one chat and push what its last
        turn wrote. One upkeep per chat at a time; a turn-end one also waits
        for one of the bounded slots, once the chat's own lock is held."""
        slot: contextlib.AbstractAsyncContextManager[Any] = (
            self._upkeep_slots if turn_end else contextlib.nullcontext()
        )
        begun = False
        try:
            async with _per_chat(self._upkeep_locks, chat_id), slot:
                begun = True
                if turn_end:
                    # From here a turn that ends is one this upkeep may have
                    # missed, so it schedules one of its own.
                    self._upkeep_queued.discard(chat_id)
                if chat_id not in self._mirrors or self._folders.held(chat_id) is None:
                    return
                if not self._stream_live:
                    # While the stream is up the drive rings it for every drop
                    # it queues, and that frame drains the folder; asking on
                    # every pass besides was one request per folder per pass.
                    await self._drain_inbound(chat_id)
                await self._push_folder_if_due(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat %s: its folder was not brought up to date", chat_id)
        finally:
            if turn_end and not begun:
                # Cut short before it began: nothing covers the chat now.
                self._upkeep_queued.discard(chat_id)

    def _turn_ended(self, chat_id: str) -> None:
        """A served chat's turn is over: bring its folder up to date now.

        The poll tick would push it too, but anywhere up to a whole interval
        later and after every other folder's upkeep — and until then a reader
        looking at the chat's files is told they are newer on this box. The
        push is still a checkpoint: it runs because the turn ENDED, never while
        one is running. The tick stays as the net for an end this missed.
        """
        self._pulse.turn_ended()
        if self._stopped or not self._folders.enabled or chat_id in self._upkeep_queued:
            return
        if self._workspaces.seat(chat_id) is not None:
            self._in_background(self._workspaces.turn_ended(chat_id), f"ws-turn-end:{chat_id}")
        self._upkeep_queued.add(chat_id)
        self._in_background(
            self._upkeep_folder(chat_id, turn_end=True), f"cloud-mirror-turn-end:{chat_id}"
        )

    async def _land_files(self, chat_id: str, targets: list[str]) -> frozenset[str]:
        """Put the bytes of the files a reply names on the drive before the
        reply is published, within the deployment's landing wait.

        Off the loop: the live sync is on a thread of its own and a landing
        blocks until that thread has sent the bytes. A chat whose folder is
        not held, or held without the live plane, lands nothing and waits for
        nothing.
        """
        if self._stopped or not self._folders.enabled:
            return frozenset()
        return await asyncio.to_thread(
            self._folders.land,
            self._workspaces.live_key(chat_id),
            targets,
            timeout=publish_landing_seconds(),
        )

    async def _pull_inbound_for(self, lease_node_id: Any, *, person: bool = False) -> None:
        """Take what is waiting for the folder the event names, if we hold it;
        ``person``: a write the drive queued for it, which holds a workspace."""
        if not isinstance(lease_node_id, str) or not lease_node_id:
            return
        for chat_id in self._workspaces.custody_keys(self._mirrors):
            held = self._folders.held(chat_id)
            if held is not None and held.record.node_id == lease_node_id:
                if person:
                    self._workspaces.note_used(chat_id)
                await self._drain_inbound(chat_id)
                return

    async def _drain_inbound(self, chat_id: str) -> int:
        """Take onto the box whatever the drive is holding for this chat.

        Never raises — a folder whose inbound cannot be read is a folder the box
        goes on running without the file somebody dropped, which is a visible
        gap rather than a chat that stops — but it is never quiet about it
        either. A failed drain is the reason a reader's file is not on disk, so
        it is logged at WARNING with the chat and the reason: at INFO it was the
        one fact missing from the record when an agent reported a chat whose
        links pointed at nothing.

        Returns how many entries were actually applied, so a caller can tell
        "the drive had nothing" from "the drive could not be asked".
        """
        held = self._folders.held(self._workspaces.live_key(chat_id))
        if held is None or held.live is None:
            return 0
        try:
            taken = await asyncio.to_thread(held.live.pull_inbound)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "chat %s: what the drive holds for it was not taken (%s: %s)",
                chat_id,
                type(exc).__name__,
                exc,
                extra={"chat_id": chat_id},
            )
            return 0
        for entry in taken:
            if getattr(entry, "state", "") != "applied":
                logger.warning(
                    "chat %s: the drive's %s for node %s was not applied (%s)",
                    chat_id,
                    "change",
                    getattr(entry, "node_id", "?"),
                    getattr(entry, "state", "?"),
                    extra={"chat_id": chat_id, "node_id": getattr(entry, "node_id", "")},
                )
        if any(getattr(entry, "state", "") == "applied" for entry in taken):
            self._policy.note_use(chat_id)  # a person's edit reached the chat's files
        return len(taken)

    async def _push_folder_if_due(self, chat_id: str) -> None:
        """Push the chat's folder once a turn has ended since the last push.

        The published counter is the turn's footprint: it moves while the
        agent speaks and is still when it is done, so "moved since the last
        push, and no turn running" is exactly "a turn ended and its files are
        not up yet". Never mid-turn — a half-written file is not a save.
        """
        mirror = self._mirrors.get(chat_id)
        if mirror is None or mirror.turn_running:
            return
        published = mirror.published_count
        if self._pushed_at.get(chat_id) == published:
            return
        retry = self._push_retry.get(chat_id)
        if retry is not None and retry[0] == published and self._clock() < retry[1]:
            # The last push of this very state did not land. Tried again on
            # the pass after it was it re-read the folder, re-listed the drive
            # and uploaded nothing new, every pass, for as long as the folder
            # stayed unreachable; a turn that ends moves ``published`` and is
            # pushed at once.
            return
        summary = await asyncio.to_thread(self._folders.push, chat_id)
        if summary is not None:
            self._pushed_at[chat_id] = published
            self._push_retry.pop(chat_id, None)
            return
        waited = retry[2] if retry is not None and retry[0] == published else 0.0
        wait = doubled(waited, floor=PUSH_RETRY_FIRST_SECONDS, cap=PUSH_RETRY_CAP_SECONDS)
        self._push_retry[chat_id] = (published, self._clock() + wait, wait)

    async def prepare_attachments(
        self, chat_id: str, message: Mapping[str, Any]
    ) -> MaterializedAttachments:
        """Put this message's attachments on disk before its turn starts
        (:meth:`TurnInputs.prepare`)."""
        return await self._inputs.prepare(chat_id, message)

    def _default_mirror(self, chat_id: str, chat: dict[str, Any]) -> ChatMirror:
        return build_mirror(
            chat_id,
            chat,
            shared_tree=self._workspaces.working_dir(chat_id),
            sandbox_bag=self._workspaces.sandbox_bag(chat_id),
            prepare_attachments=self.prepare_attachments,
            on_refused=self._on_mirror_refused,
            on_turn_end=self._turn_ended,
            land_files=functools.partial(self._land_files, chat_id),
            runtime=self._runtime,
            socket=self._socket,
            rest=self._rest.for_agent(chat_id),
            budget=self._settings.budget,
            user_id=self._settings.user_id,
            machine_id=self._machine_id,
            model=pinned_model(chat.get("model")),
            permission_mode=_stored_mode(chat.get("permission_mode")),
            files_drive_id=chat_folder_drive(chat),
            machine_card=self._pulse.machine_card,
            clock=self._clock,
            sleep=self._sleep,
        )


def _stored_mode(stored: Any) -> PermissionMode:
    """The permission stance a chat's own record says it runs in.

    A chat carries its stance on its record, so a reader who moved it out of
    read-only keeps that stance across a sleep and a resume — and across the
    box it resumes on, which may not be the one that heard the relay. Anything
    the box does not run cloud chats in (no stance at all, a chat from before
    the stance existed, or a word no harness knows) reads as the floor rather
    than as permission nobody granted.
    """
    if isinstance(stored, str) and stored in CLOUD_PERMISSION_MODES:
        return cast(PermissionMode, stored)
    return "read_only"


__all__ = [
    "CHAT_UPDATED",
    "ENV_API_URL",
    "ENV_MACHINE_NAME",
    "OPENCODE_MISSING_MESSAGE",
    "TEAM_CONNECTION_UPDATED",
    "CloudMirrorService",
    "MirrorFactory",
    "MirrorSettings",
    "box_rest_client",
    "machine_agent_id",
]
