"""Cloud daemon: the bounds a deployment may tune.

Each bound is read from the environment through the parsers in
:mod:`alkera_cli.host.limits`, at the call site rather than at import, so a
restart picks up a new value and a test can move it.

Unset, blank and unparseable values all read as the built-in default, so a typo
never silently removes a bound. A value at or below zero means "no bound" where
that is meaningful, and the default where it is not (a beat every zero seconds
is a spin, not a cadence).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping

from alkera_cli.cloud.fence import read_root
from alkera_cli.host.backoff import (
    DEFAULT_RECONNECT_BASE_SECONDS,
    DEFAULT_RECONNECT_CAP_SECONDS,
    DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS,
    ENV_RECONNECT_BASE_SECONDS,
    ENV_RECONNECT_CAP_SECONDS,
    ENV_RECONNECT_HEALTHY_PERIOD_SECONDS,
    reconnect_bounds,
)
from alkera_cli.host.limits import env_count, env_seconds

# --- Cloud daemon: bounds a deployment may tune ---

#: Every REST call the box makes to the backend, save the ones below that name
#: their own budget. Thirty seconds is generous for a control call and tight
#: for a transcript carrying a large entry over a slow uplink.
ENV_REST_TIMEOUT_SECONDS = "ALKERA_CLOUD_REST_TIMEOUT_SECONDS"
DEFAULT_REST_TIMEOUT_SECONDS = 30.0

#: One heartbeat's own budget. It has to stay SHORTER than the beat interval:
#: the loop is serial, so a tunnel that accepts the connection and never
#: answers spaces the beats far enough apart to push a healthy box past the
#: cloud's reachability window — the box declares itself dead.
ENV_HEARTBEAT_TIMEOUT_SECONDS = "ALKERA_CLOUD_HEARTBEAT_TIMEOUT_SECONDS"
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 10.0

#: Every Files call from the box, including a whole-folder push or pull. Two
#: minutes is sized for bulk transfer on an ordinary link; a box moving a large
#: chat folder across a slow one needs to say so.
ENV_FILES_TIMEOUT_SECONDS = "ALKERA_CLOUD_FILES_TIMEOUT_SECONDS"
DEFAULT_FILES_TIMEOUT_SECONDS = 120.0

#: How long one Files call may spend CONNECTING, within the budget above. The
#: budget is sized for the bytes of a large file; a host that does not answer
#: the connection at all answers it no better two minutes later, and a folder
#: take that waited the whole budget to learn so held its slot — and every
#: chat queued behind it — for those two minutes.
ENV_FILES_CONNECT_TIMEOUT_SECONDS = "ALKERA_CLOUD_FILES_CONNECT_TIMEOUT_SECONDS"
DEFAULT_FILES_CONNECT_TIMEOUT_SECONDS = 10.0

#: How long a host the box could not connect to is taken at its word. A
#: content origin that is unreachable from this box is unreachable for every
#: chat's folder, so the first connect failure answers the rest at once for
#: this long instead of each paying the connect budget in turn.
ENV_ORIGIN_OUTAGE_SECONDS = "ALKERA_CLOUD_ORIGIN_OUTAGE_SECONDS"
DEFAULT_ORIGIN_OUTAGE_SECONDS = 60.0

#: How often a box beats for the chat folders it holds when the lease's grant
#: named no cadence of its own. A grant that DOES name one is always obeyed
#: instead, so this is only the fallback.
ENV_FOLDER_BEAT_SECONDS = "ALKERA_CLOUD_FOLDER_BEAT_SECONDS"
DEFAULT_FOLDER_BEAT_SECONDS = 15.0

#: The floor under a SERVED cadence. A server that asked for a beat every
#: fraction of a second would otherwise turn the beat loop into a spin, so zero
#: is not "no floor" here — it reads as the default.
ENV_FOLDER_BEAT_FLOOR_SECONDS = "ALKERA_CLOUD_FOLDER_BEAT_FLOOR_SECONDS"
DEFAULT_FOLDER_BEAT_FLOOR_SECONDS = 1.0

#: One folder beat's own budget, which has to be shorter than the cadence: a
#: drive that accepts the connection and never answers held a beat for the
#: whole Files timeout, and the pass keeping every OTHER chat's lease alive
#: waited behind it.
ENV_FOLDER_BEAT_TIMEOUT_SECONDS = "ALKERA_CLOUD_FOLDER_BEAT_TIMEOUT_SECONDS"
DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS = 10.0

#: Non-transient hand-back refusals before the box drops the folder and tells
#: the chat its files could not be saved. Retrying for ever helps nobody, but a
#: deployment whose drive is flaky in a way that clears may want more tries.
ENV_HANDBACK_ATTEMPTS = "ALKERA_CLOUD_HANDBACK_ATTEMPTS"
DEFAULT_HANDBACK_ATTEMPTS = 3

#: How many ids each of the mirror's dedupe sets remembers — relay ops,
#: consumed messages, settled asks, replayed calls. These are correctness
#: guards, not caches: past the bound an answered permission card can be
#: re-offered, so ``0`` here means remember every id for the life of the mirror
#: rather than remember none.
ENV_RELAY_MEMORY = "ALKERA_CLOUD_RELAY_MEMORY"
DEFAULT_RELAY_MEMORY = 512

#: How many of its own event ids a mirror remembers while they travel the bus,
#: so an event it published is not read back as somebody else's. ``0`` means
#: remember every one.
ENV_OWN_EVENT_MEMORY = "ALKERA_CLOUD_OWN_EVENT_MEMORY"
DEFAULT_OWN_EVENT_MEMORY = 1024

#: Pages of the chat list one discovery pass walks. Past this the walk stops
#: with chats unseen, and a served chat outside the window has its mirror torn
#: down. ``0`` means walk to the end however many pages that takes — the cursor
#: not advancing already ends the read.
ENV_CHAT_LIST_PAGES = "ALKERA_CLOUD_CHAT_LIST_PAGES"
DEFAULT_CHAT_LIST_PAGES = 100

#: How long a mirror waits for the chat document's hello before giving up on
#: the chat for this round. A backend under load answers late, and the reader
#: sees a composer that does not answer.
ENV_MIRROR_START_TIMEOUT_SECONDS = "ALKERA_CLOUD_MIRROR_START_TIMEOUT_SECONDS"
DEFAULT_MIRROR_START_TIMEOUT_SECONDS = 30.0

#: Rows a saved query re-run from the browser returns when the saved query
#: names no limit of its own.
ENV_RERUN_LIMIT = "ALKERA_CLOUD_RERUN_LIMIT"
DEFAULT_RERUN_LIMIT = 1000

#: The gap between the streaming publisher's frames. It is a rate, not a
#: ceiling: the byte window beside it is fixed by what a notification may
#: carry, so this is the only half a deployment can trade latency against
#: notification load with.
ENV_PUBLISH_CHUNK_INTERVAL_SECONDS = "ALKERA_CLOUD_PUBLISH_CHUNK_INTERVAL_SECONDS"
DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS = 0.05

#: How long the transcript holds a reply that names files in the chat's folder
#: while their bytes go up to the drive. A chart lands in well under a second;
#: this is the ceiling for a drive that is not answering, after which the reply
#: goes anyway and a reader who opens the file asks the box for it.
ENV_PUBLISH_LANDING_SECONDS = "ALKERA_CLOUD_PUBLISH_LANDING_SECONDS"
DEFAULT_PUBLISH_LANDING_SECONDS = 10.0

#: The socket reconnect bounds (``ENV_RECONNECT_*``, :func:`reconnect_bounds`)
#: live with the backoff that reads them, :mod:`alkera_cli.host.backoff`, and
#: are imported above so this stays the one list of the box's knobs.


# The system locations a cloud chat's SHELL may read beside its own working
# directory: the toolchain and its libraries, the certificate store, the
# system's own identity files. ``os.pathsep``-separated root-anchored paths, in
# either dialect — ``/usr`` and ``C:\tools`` are each a root. ``/`` (or a bare
# ``C:\``) and a relative entry are not roots (an allowlist of ``/`` is no
# fence), and ``ALKERA_HOME``, ``/proc`` and the daemon's ``.alkera`` stay
# refused whatever is listed here.
ENV_SHELL_READ_ROOTS = "ALKERA_CLOUD_SHELL_READ_ROOTS"
DEFAULT_SHELL_READ_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/opt/homebrew",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/os-release",
    "/etc/alternatives",
    "/etc/localtime",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/profile",
    "/etc/bash.bashrc",
    "/etc/bashrc",
    "/etc/zshrc",
    "/etc/zshenv",
    "/etc/inputrc",
    "/etc/terminfo",
    "/etc/mime.types",
    "/nix/store",
    "/System/Library",
    "/Library/Developer",
    "/private/etc/ssl",
)

# --- end of block ---


def _seconds(name: str, default: float, env: Mapping[str, str] | None) -> float:
    """A positive number of seconds. Zero and below are not a duration anyone
    meant for these — a budget of zero is a call that fails before it is sent,
    a cadence of zero is a spin — so they read as the default."""
    source = os.environ if env is None else env
    parsed = env_seconds(source.get(name), default=default)
    return default if parsed is None else parsed


def _unbounded_count(name: str, default: int, env: Mapping[str, str] | None) -> float:
    """A whole count, or ``inf`` when the operator asked for no bound at all.

    ``inf`` rather than ``None`` so a call site keeps the plain ``len(x) >
    limit`` comparison it already had: no bound is a comparison that is never
    true.
    """
    source = os.environ if env is None else env
    parsed = env_count(source.get(name), default=default)
    return math.inf if parsed is None else float(parsed)


def shell_read_roots(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The system locations a cloud chat's shell may read beside its working
    directory. An entry that is anchored at no filesystem root, or that is a
    root itself, is dropped: neither is a bound. An unset or empty variable is
    the default list; a variable set to only such entries is NO system roots.

    Each entry is read through :func:`~alkera_cli.cloud.fence.read_root` — the
    same reading the fence compares a path against, and one that knows both
    dialects, so a Windows box keeps the roots it was given rather than
    silently running with none.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_SHELL_READ_ROOTS)
    if raw is None or not raw.strip():
        return DEFAULT_SHELL_READ_ROOTS
    return tuple(root for entry in raw.split(os.pathsep) if (root := read_root(entry)) is not None)


def rest_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """Every REST call the box makes that does not name its own budget."""
    return _seconds(ENV_REST_TIMEOUT_SECONDS, DEFAULT_REST_TIMEOUT_SECONDS, env)


def heartbeat_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """One heartbeat's own budget."""
    return _seconds(ENV_HEARTBEAT_TIMEOUT_SECONDS, DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, env)


def files_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """Every Files call from the box, bulk transfers included."""
    return _seconds(ENV_FILES_TIMEOUT_SECONDS, DEFAULT_FILES_TIMEOUT_SECONDS, env)


def files_connect_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long a Files call may spend connecting, never more than its budget."""
    return min(
        files_timeout_seconds(env),
        _seconds(ENV_FILES_CONNECT_TIMEOUT_SECONDS, DEFAULT_FILES_CONNECT_TIMEOUT_SECONDS, env),
    )


def origin_outage_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long a host that could not be connected to is not asked again."""
    return _seconds(ENV_ORIGIN_OUTAGE_SECONDS, DEFAULT_ORIGIN_OUTAGE_SECONDS, env)


def folder_beat_seconds(env: Mapping[str, str] | None = None) -> float:
    """The fallback folder-beat cadence, used only when no grant names one."""
    return _seconds(ENV_FOLDER_BEAT_SECONDS, DEFAULT_FOLDER_BEAT_SECONDS, env)


def folder_beat_floor_seconds(env: Mapping[str, str] | None = None) -> float:
    """The floor under a served folder-beat cadence."""
    return _seconds(ENV_FOLDER_BEAT_FLOOR_SECONDS, DEFAULT_FOLDER_BEAT_FLOOR_SECONDS, env)


def folder_beat_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """One folder beat's own budget."""
    return _seconds(ENV_FOLDER_BEAT_TIMEOUT_SECONDS, DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS, env)


def handback_attempts(env: Mapping[str, str] | None = None) -> int:
    """Non-transient hand-back refusals before the folder is dropped. At least
    one: a hand-back nobody attempts is a chat's files never saved."""
    source = os.environ if env is None else env
    parsed = env_count(source.get(ENV_HANDBACK_ATTEMPTS), default=DEFAULT_HANDBACK_ATTEMPTS)
    return DEFAULT_HANDBACK_ATTEMPTS if parsed is None else parsed


def relay_memory(env: Mapping[str, str] | None = None) -> float:
    """How many ids each mirror dedupe set remembers; ``inf`` for all of them."""
    return _unbounded_count(ENV_RELAY_MEMORY, DEFAULT_RELAY_MEMORY, env)


def own_event_memory(env: Mapping[str, str] | None = None) -> float:
    """How many of its own event ids a mirror remembers; ``inf`` for all."""
    return _unbounded_count(ENV_OWN_EVENT_MEMORY, DEFAULT_OWN_EVENT_MEMORY, env)


def chat_list_pages(env: Mapping[str, str] | None = None) -> float:
    """Pages of the chat list one discovery pass walks; ``inf`` to walk to the
    end."""
    return _unbounded_count(ENV_CHAT_LIST_PAGES, DEFAULT_CHAT_LIST_PAGES, env)


def mirror_start_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long a mirror waits for the chat document's hello."""
    return _seconds(ENV_MIRROR_START_TIMEOUT_SECONDS, DEFAULT_MIRROR_START_TIMEOUT_SECONDS, env)


def rerun_limit(env: Mapping[str, str] | None = None) -> int:
    """Rows a re-run saved query returns when it names no limit of its own."""
    source = os.environ if env is None else env
    parsed = env_count(source.get(ENV_RERUN_LIMIT), default=DEFAULT_RERUN_LIMIT)
    return DEFAULT_RERUN_LIMIT if parsed is None else parsed


def publish_chunk_interval_seconds(env: Mapping[str, str] | None = None) -> float:
    """The gap between the streaming publisher's frames."""
    return _seconds(ENV_PUBLISH_CHUNK_INTERVAL_SECONDS, DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS, env)


def publish_landing_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long a reply naming chat files waits for their bytes to land."""
    return _seconds(ENV_PUBLISH_LANDING_SECONDS, DEFAULT_PUBLISH_LANDING_SECONDS, env)


__all__ = [
    "DEFAULT_CHAT_LIST_PAGES",
    "DEFAULT_FILES_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_FILES_TIMEOUT_SECONDS",
    "DEFAULT_FOLDER_BEAT_FLOOR_SECONDS",
    "DEFAULT_FOLDER_BEAT_SECONDS",
    "DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS",
    "DEFAULT_HANDBACK_ATTEMPTS",
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_MIRROR_START_TIMEOUT_SECONDS",
    "DEFAULT_ORIGIN_OUTAGE_SECONDS",
    "DEFAULT_OWN_EVENT_MEMORY",
    "DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS",
    "DEFAULT_PUBLISH_LANDING_SECONDS",
    "DEFAULT_RECONNECT_BASE_SECONDS",
    "DEFAULT_RECONNECT_CAP_SECONDS",
    "DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS",
    "DEFAULT_RELAY_MEMORY",
    "DEFAULT_RERUN_LIMIT",
    "DEFAULT_REST_TIMEOUT_SECONDS",
    "DEFAULT_SHELL_READ_ROOTS",
    "ENV_CHAT_LIST_PAGES",
    "ENV_FILES_CONNECT_TIMEOUT_SECONDS",
    "ENV_FILES_TIMEOUT_SECONDS",
    "ENV_FOLDER_BEAT_FLOOR_SECONDS",
    "ENV_FOLDER_BEAT_SECONDS",
    "ENV_FOLDER_BEAT_TIMEOUT_SECONDS",
    "ENV_HANDBACK_ATTEMPTS",
    "ENV_HEARTBEAT_TIMEOUT_SECONDS",
    "ENV_MIRROR_START_TIMEOUT_SECONDS",
    "ENV_ORIGIN_OUTAGE_SECONDS",
    "ENV_OWN_EVENT_MEMORY",
    "ENV_PUBLISH_CHUNK_INTERVAL_SECONDS",
    "ENV_PUBLISH_LANDING_SECONDS",
    "ENV_RECONNECT_BASE_SECONDS",
    "ENV_RECONNECT_CAP_SECONDS",
    "ENV_RECONNECT_HEALTHY_PERIOD_SECONDS",
    "ENV_RELAY_MEMORY",
    "ENV_RERUN_LIMIT",
    "ENV_REST_TIMEOUT_SECONDS",
    "ENV_SHELL_READ_ROOTS",
    "chat_list_pages",
    "files_connect_timeout_seconds",
    "files_timeout_seconds",
    "folder_beat_floor_seconds",
    "folder_beat_seconds",
    "folder_beat_timeout_seconds",
    "handback_attempts",
    "heartbeat_timeout_seconds",
    "mirror_start_timeout_seconds",
    "origin_outage_seconds",
    "own_event_memory",
    "publish_chunk_interval_seconds",
    "publish_landing_seconds",
    "reconnect_bounds",
    "relay_memory",
    "rerun_limit",
    "rest_timeout_seconds",
    "shell_read_roots",
]
