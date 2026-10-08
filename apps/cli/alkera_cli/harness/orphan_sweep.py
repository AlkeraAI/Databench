"""Agent process registry + breadcrumb + an orphan sweep that reaps ONLY true orphans.

Why this exists: a harness child (opencode / claude) must die when the ``alkera``
that spawned it dies. Windows binds the child to a Job Object and Linux to
``PR_SET_PDEATHSIG`` (see ``harness/spawn.py``), so a hard-killed parent takes the
child with it at the OS level. **macOS has no such primitive** — a ``kill -9`` of
``alkera`` strands the agent. The per-chat reaper only reclaims when that *same*
chat is reopened; this registry + sweep close the gap by reaping any agent whose
spawning ``alkera`` is gone, on the next launch of *any* ``alkera`` command (and
on daemon startup).

**Only TRUE orphans are reaped.** A bare PID is not a safe identity: the OS recycles
PIDs, so a recorded agent PID may now belong to an unrelated process, and a recorded
parent PID may now belong to a *different* live process. We therefore record each
process's creation time and confirm it on reap (``same_process``). We kill an agent
only when its PID is alive AND its creation time matches what we recorded (so it is
provably *our* agent), AND the parent is provably gone (dead, or its PID recycled to
a different process). Anything we can't positively identify is left alone.

The sweep is best-effort and never raises.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
from pathlib import Path

import psutil
from alkera_core.atomic_io import write_json_atomic
from alkera_core.process import kill_process, process_alive, terminate_process

from alkera_cli.host import paths
from alkera_cli.host.limits import env_seconds

logger = logging.getLogger(__name__)

#: Creation-time equality tolerance (seconds). psutil returns a stable float per
#: process, but allow a hair of slack against rounding across reads. Widening it
#: makes the sweep readier to accept a PID as the same agent — on a host whose
#: clock granularity is coarser than psutil assumes, that is the difference
#: between reaping an orphan and leaving it; narrowing it is the safer direction
#: and never kills a stranger. A non-positive value keeps the default: exact
#: float equality across two reads is not a contract psutil offers.
ENV_CREATE_TIME_EPSILON = "ALKERA_AGENT_CREATE_TIME_EPSILON"
_CREATE_TIME_EPSILON_DEFAULT = 1.0
_CREATE_TIME_EPSILON = (
    env_seconds(os.environ.get(ENV_CREATE_TIME_EPSILON), default=_CREATE_TIME_EPSILON_DEFAULT)
    or _CREATE_TIME_EPSILON_DEFAULT
)


def _hostname() -> str:
    return socket.gethostname()


def _entry_path(agent_pid: int) -> Path:
    return paths.agents_dir() / f"{agent_pid}.json"


def process_create_time(pid: int) -> float | None:
    """The process's creation time (epoch seconds), or None if it's gone /
    unreadable. Stable for the life of a process; differs after PID reuse."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def same_process(pid: int, create_time: float | None) -> bool:
    """True iff `pid` is alive AND is the SAME process we recorded.

    Returns False when `create_time` is None (we can't positively identify it,
    so we refuse to act) or when the live PID's creation time differs (PID was
    recycled to a different process)."""
    if create_time is None:
        return False
    current = process_create_time(pid)
    if current is None:
        return False
    return abs(current - create_time) < _CREATE_TIME_EPSILON


# --- per-chat pid breadcrumb (read/written by the opencode adapter) ----------


def write_pid_breadcrumb(path: Path, pid: int) -> None:
    """Write the per-chat ``pid`` breadcrumb as JSON carrying the agent's
    creation time, so the reaper can confirm identity (not just liveness)."""
    payload = {"pid": pid, "create_time": process_create_time(pid)}
    # Atomic (temp+rename): the opencode adapter writes this right after spawn, and a SIGKILL of
    # the spawning ``alkera`` mid-write would otherwise leave a TRUNCATED file → read_pid_breadcrumb
    # returns None → the reaper kills NOTHING and the true orphan opencode survives, holding its
    # port + the chat's locks. A reader now always sees a complete (pid, create_time) or nothing.
    write_json_atomic(path, payload)


def read_pid_breadcrumb(path: Path) -> tuple[int, float | None] | None:
    """Parse the per-chat breadcrumb → (pid, create_time). Tolerates a legacy
    bare-integer file (create_time unknown → None). None if absent/garbage."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # A legacy breadcrumb is a bare integer pid (valid JSON), e.g. "4242".
    if isinstance(data, int) and not isinstance(data, bool):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("pid"), int):
        ct = data.get("create_time")
        return data["pid"], (ct if isinstance(ct, (int, float)) else None)
    return None


# --- central registry + sweep ------------------------------------------------


def register_agent(agent_pid: int, *, pid_file: Path | None = None) -> None:
    """Record a spawned agent (with both its and our creation times) so the
    sweep can reap it if WE die ungracefully. Best-effort: a registry hiccup
    must never stop a chat from starting."""
    try:
        directory = paths.agents_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent_pid": agent_pid,
            "agent_create_time": process_create_time(agent_pid),
            "parent_pid": os.getpid(),
            "parent_create_time": process_create_time(os.getpid()),
            "host": _hostname(),
            "pid_file": str(pid_file) if pid_file is not None else None,
        }
        # Atomic: a torn central-registry entry (parent SIGKILLed mid-write) reads back as None and
        # the sweep drops it, again stranding a true orphan — write whole-or-nothing.
        write_json_atomic(_entry_path(agent_pid), payload)
    except OSError:
        logger.debug("agent registry: failed to register pid=%s", agent_pid, exc_info=True)


def unregister_agent(agent_pid: int) -> None:
    """Drop an agent's breadcrumb (on graceful stop or after a reap). Best-effort."""
    with contextlib.suppress(OSError):
        _entry_path(agent_pid).unlink(missing_ok=True)


def sweep_orphaned_agents() -> int:
    """Reap agents whose spawning ``alkera`` is gone. Returns the count killed.

    For each same-host registry entry:
    - agent NOT positively identified (dead, or PID recycled) → drop the entry,
      kill nothing.
    - agent confirmed ours, parent confirmed alive → leave it (still owned).
    - agent confirmed ours, parent provably gone → TRUE orphan: terminate→kill,
      then drop the breadcrumbs.
    Cross-host entries are skipped (we can't probe another machine's PIDs).
    Best-effort throughout — never raises.
    """
    directory = paths.agents_dir()
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return 0

    killed = 0
    this_host = _hostname()
    for entry in entries:
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)  # corrupt breadcrumb — sweep aside
            continue

        if not isinstance(data, dict) or data.get("host") != this_host:
            continue  # another machine's agent — not ours to judge

        agent_pid = data.get("agent_pid")
        parent_pid = data.get("parent_pid")
        if not isinstance(agent_pid, int) or not isinstance(parent_pid, int):
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)
            continue

        agent_ct = data.get("agent_create_time")
        agent_ct = agent_ct if isinstance(agent_ct, (int, float)) else None
        parent_ct = data.get("parent_create_time")
        parent_ct = parent_ct if isinstance(parent_ct, (int, float)) else None

        if not same_process(agent_pid, agent_ct):
            # Agent is gone, or its PID was recycled to an unrelated process —
            # NOT a reapable orphan. Drop the stale breadcrumb, kill nothing.
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)
            continue

        # The parent is "provably gone" ONLY when its PID is dead, or alive but RECYCLED to a
        # different process (create_time known and differs). A None parent_ct means we can't
        # identify it — NOT that it's gone — so an alive PID we can't positively confirm is left
        # ALONE (else a psutil hiccup at register time, or a legacy entry, would make us reap a
        # LIVE parent's agent). Mirrors the docstring: anything we can't identify is left alone.
        if process_alive(parent_pid) and (parent_ct is None or same_process(parent_pid, parent_ct)):
            continue

        # Agent confirmed ours + parent provably gone → a TRUE orphan.
        logger.info(
            "sweeping orphaned alkera-agent pid=%d (spawning alkera pid=%d is gone)",
            agent_pid,
            parent_pid,
        )
        terminate_process(agent_pid)
        if process_alive(agent_pid):
            kill_process(agent_pid)
        killed += 1

        pid_file = data.get("pid_file")
        if isinstance(pid_file, str):
            with contextlib.suppress(OSError):
                Path(pid_file).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            entry.unlink(missing_ok=True)

    return killed


def _is_seed_worker_cmdline(cmdline: list[str]) -> bool:
    """True iff ``cmdline`` is an ``alkera lineage seed`` / ``alkera context seed`` worker
    (positional ``<verb> seed`` tokens + an ``alkera`` argv element). Only our seed
    workers ever carry this shape, so matching it can't misfire onto an unrelated process."""
    if not any("alkera" in part for part in cmdline):
        return False
    for verb in ("lineage", "context"):
        if verb in cmdline:
            i = cmdline.index(verb)
            if i + 1 < len(cmdline) and cmdline[i + 1] == "seed":
                return True
    return False


def sweep_orphaned_seed_workers() -> int:
    """Reap ``alkera lineage|context seed`` workers ORPHANED by a dead daemon — a
    DevWatcher restart / crash reparents them to init (ppid 1) but they keep grinding
    (and, before the embed-thread cap, pinned the machine). Run at daemon/CLI startup.

    Identity is cmdline + ``ppid == 1``: a LIVE worker is parented to its daemon and a
    user-run ``alkera … seed`` to its shell, so neither is touched — only true orphans.
    Returns the count killed; best-effort, never raises."""
    killed = 0
    self_pid = os.getpid()
    try:
        procs = list(psutil.process_iter(["pid", "ppid", "cmdline"]))
    except (psutil.Error, OSError):
        return 0
    for proc in procs:
        try:
            info = proc.info
            if info["pid"] == self_pid or info["ppid"] != 1:
                continue
            if not _is_seed_worker_cmdline(info["cmdline"] or []):
                continue
            logger.info("sweeping orphaned alkera seed worker pid=%d", info["pid"])
            proc.kill()
            killed += 1
        except (psutil.Error, OSError):
            continue
    return killed


__all__ = [
    "process_create_time",
    "read_pid_breadcrumb",
    "register_agent",
    "same_process",
    "sweep_orphaned_agents",
    "sweep_orphaned_seed_workers",
    "unregister_agent",
    "write_pid_breadcrumb",
]
