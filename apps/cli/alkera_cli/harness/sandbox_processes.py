"""What is still running inside a chat's sandbox besides its agent server.

The harness knows the work it started itself: a turn, a background shell, a SQL
job, a subagent. It does not know what a command left behind. ``nohup ./train.sh
&`` returns at once, the tool call completes, the background-job list is empty,
and the process runs on inside the chat's sandbox for hours. A box that read
that chat as idle would put it to sleep and the process would die with the
container. This module answers the one question the sleep needs: which
processes in the chat's sandbox are not the agent server itself.

The answer is read where the processes actually live:

* under gVisor, from inside the container with ``runsc exec`` (the container's own
  process table: the agent server is its PID 1, everything else was started on
  the chat's behalf);
* on a box with a per-chat cgroup and no gVisor, from the cgroup tree's
  ``cgroup.procs`` (the agent server is the pid the harness spawned, plus the
  launchers that exec'd it);
* on a host with neither (macOS, a local session) it cannot be known, and the
  probe says so with ``None`` rather than a guess.

The parsing and the filtering are pure; the two listers are the I/O shell, each
with its runner or root injectable so a test drives them against a real process
and a fake ``runsc`` or a fake cgroup tree.
"""

from __future__ import annotations

import logging
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import psutil
from alkera_core.process import SpawnSpec, release, spawn
from alkera_core.process import kill_tree as kill_host_tree

from alkera_cli.harness.sandbox import (
    SandboxMode,
    SandboxSpec,
    chat_cgroup_dir,
    chat_container,
    host_path,
)

logger = logging.getLogger(__name__)

#: How long one read of a container's process table may take before the probe gives up and answers
#: "unknown". It is asked only when a chat is about to be put to sleep, so a
#: slow answer delays a sleep, never a turn.
RUNSC_PS_TIMEOUT_SECONDS = 10.0

#: The process state a reaped-in-waiting child reports. A zombie holds no
#: memory and does no work; counting it would keep a chat awake for a process
#: that has already ended.
ZOMBIE_STATES = frozenset({"Z", "zombie"})


@dataclass(frozen=True, slots=True)
class SandboxProcess:
    """One process in a chat's sandbox, as its own process table names it."""

    pid: int
    ppid: int
    command: str
    state: str = ""

    @property
    def ended(self) -> bool:
        return self.state in ZOMBIE_STATES


def work_processes(
    processes: Iterable[SandboxProcess], *, agent_pids: frozenset[int]
) -> tuple[SandboxProcess, ...]:
    """The processes that are work: everything that is neither the agent
    server (nor a launcher that exec'd it) nor already ended."""
    return tuple(p for p in processes if p.pid not in agent_pids and not p.ended)


def parse_proc_stat(line: str) -> SandboxProcess:
    """One ``/proc/<pid>/stat`` line: ``pid (comm) state ppid …``. The command
    name may itself hold spaces and parentheses, so it is cut at the LAST
    closing parenthesis, the way ``ps`` reads it. Raises :class:`ValueError`
    on a line that is not one."""
    head, sep, tail = line.strip().rpartition(")")
    pid_text, open_paren, comm = head.partition(" (")
    fields = tail.split()
    if not sep or not open_paren or len(fields) < 2 or not pid_text.isdigit():
        raise ValueError(f"not a /proc stat line: {line[:80]!r}")
    ppid = fields[1]
    return SandboxProcess(
        pid=int(pid_text),
        ppid=int(ppid) if ppid.lstrip("-").isdigit() else 0,
        command=comm,
        state=fields[0],
    )


def parse_container_table(text: str) -> tuple[SandboxProcess, ...]:
    """What :data:`CONTAINER_TABLE_SCRIPT` printed inside the container: the
    reader's own pid on the first line, then one stat line per process. The
    reader is left out (it is this probe, not the chat's work). Raises
    :class:`ValueError` on output the script never prints, so the caller
    answers "unknown" rather than "nothing runs"."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].strip().isdigit():
        raise ValueError("the container table did not start with the reader's pid")
    reader = int(lines[0].strip())
    found = [parse_proc_stat(line) for line in lines[1:]]
    return tuple(p for p in found if p.pid != reader)


#: Read inside the chat's container: its own pid, then every process's stat
#: line. ``exec`` makes ``cat`` the shell's own pid, so the reader can name
#: itself and be left out. gVisor's ``runsc ps`` gives pids and commands but no
#: state, and the agent server (the container's PID 1) does not reap the
#: orphans it inherits, so an ended background process stays in the table as a
#: zombie. Counting those would keep a chat awake for work that has finished.
#: Every binary is named by its absolute path, and the exec carries its own
#: ``PATH`` (:data:`PROBE_PATH`): the container belongs to the chat, and a
#: ``cat`` it put earlier on a path the probe searched would answer for it.
CONTAINER_TABLE_SCRIPT = "echo $$; exec /bin/cat /proc/[0-9]*/stat 2>/dev/null"
PROBE_PATH = "/usr/bin:/bin"
#: The most a process table may print before the read is cut and counted as a
#: failure. A stat line is a few hundred bytes, so this is thousands of
#: processes; past it the output is not a table but something filling a pipe.
MAX_TABLE_BYTES = 4 * 1024 * 1024


class ProbeOutputTooLargeError(subprocess.SubprocessError):
    """The probe printed more than :data:`MAX_TABLE_BYTES`; it was killed."""


#: Runs an argv and returns ``(exit status, stdout)``; the seam a test replaces.
Runner = Callable[[Sequence[str]], tuple[int, str]]


def run_probe(argv: Sequence[str]) -> tuple[int, str]:
    """Run ``argv`` and return ``(exit status, stdout)``, reading at most
    :data:`MAX_TABLE_BYTES` within :data:`RUNSC_PS_TIMEOUT_SECONDS`. Past
    either the child is killed and the read raises, so a probe can neither
    hang the box nor fill its memory."""
    proc = spawn(SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="devnull"))
    assert proc.stdout is not None
    deadline = time.monotonic() + RUNSC_PS_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(list(argv), RUNSC_PS_TIMEOUT_SECONDS)
                if not selector.select(left):
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_TABLE_BYTES:
                    raise ProbeOutputTooLargeError(f"more than {MAX_TABLE_BYTES} bytes")
                chunks.append(chunk)
        status = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            kill_host_tree(proc, grace=0)
        release(proc.pid)
        proc.stdout.close()
    return status, b"".join(chunks).decode("utf-8", errors="replace")


@runtime_checkable
class ProcessLister(Protocol):
    """Lists every process in one chat's sandbox, or ``None`` when it cannot."""

    def list_processes(self) -> tuple[SandboxProcess, ...] | None: ...

    @property
    def agent_pids(self) -> frozenset[int]: ...


@dataclass(frozen=True, slots=True)
class RunscLister:
    """The container's own process table, read from inside it with ``runsc
    exec`` as the chat's uid. The agent server is the container's PID 1:
    ``runsc run`` starts it as the container's init, and an orphaned background
    process is reparented to it."""

    runsc: str
    root: str
    container: str
    uid: int
    runner: Runner = run_probe

    @property
    def agent_pids(self) -> frozenset[int]:
        return frozenset({1})

    def list_processes(self) -> tuple[SandboxProcess, ...] | None:
        argv = (
            self.runsc,
            f"--root={self.root}",
            "exec",
            f"--user={self.uid}:{self.uid}",
            "--env",
            f"PATH={PROBE_PATH}",
            self.container,
            "/bin/sh",
            "-c",
            CONTAINER_TABLE_SCRIPT,
        )
        try:
            status, out = self.runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("could not list the processes in %s: %s", self.container, exc)
            return None
        if status != 0:
            logger.warning("listing the processes in %s exited %d", self.container, status)
            return None
        try:
            return parse_container_table(out)
        except ValueError as exc:
            logger.warning("the process table of %s did not parse: %s", self.container, exc)
            return None


def _process_state(pid: int) -> str | None:
    """A live pid's state, or ``None`` once it is gone."""
    try:
        return str(psutil.Process(pid).status())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _process_command(pid: int) -> str:
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return ""


def _process_ppid(pid: int) -> int:
    try:
        return int(psutil.Process(pid).ppid())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return 0


#: How many launchers deep a spawn is followed up its parents: the cgroup
#: placement, the uid drop and the mount alias exec in place, so a real chain
#: is one or two; the bound only keeps a cycle in a broken table finite.
MAX_LAUNCH_CHAIN = 16


def _launch_chain(pid: int, members: frozenset[int]) -> frozenset[int]:
    """``pid`` and every ancestor of it that is in the same cgroup: the
    launchers (the cgroup placement, the uid drop) a spawn passed through."""
    chain = {pid}
    current = pid
    for _ in range(MAX_LAUNCH_CHAIN):
        parent = _process_ppid(current)
        if parent <= 1 or parent not in members:
            break
        chain.add(parent)
        current = parent
    return frozenset(chain)


@dataclass(frozen=True, slots=True)
class CgroupLister:
    """Every process in the chat's cgroup tree, read from ``cgroup.procs``.
    The agent server is the pid the harness spawned, together with any
    launcher in the same cgroup it was exec'd through."""

    cgroup: Path
    agent_pid: int

    @property
    def agent_pids(self) -> frozenset[int]:
        return _launch_chain(self.agent_pid, frozenset(self._pids() or ()))

    def _pids(self) -> list[int] | None:
        if not self.cgroup.is_dir():
            return None
        pids: list[int] = []
        try:
            files = sorted(self.cgroup.rglob("cgroup.procs"))
            for path in files:
                for line in path.read_text(encoding="ascii").split():
                    if line.isdigit():
                        pids.append(int(line))
        except OSError as exc:
            logger.warning("could not read the processes in %s: %s", self.cgroup, exc)
            return None
        return pids

    def list_processes(self) -> tuple[SandboxProcess, ...] | None:
        pids = self._pids()
        if pids is None:
            return None
        found: list[SandboxProcess] = []
        for pid in dict.fromkeys(pids):
            state = _process_state(pid)
            if state is None:
                continue
            found.append(
                SandboxProcess(
                    pid=pid,
                    ppid=_process_ppid(pid),
                    command=_process_command(pid),
                    state=state,
                )
            )
        return tuple(found)


@dataclass(frozen=True, slots=True)
class SandboxProcessProbe:
    """Answers whether anything other than the agent server runs in one
    chat's sandbox."""

    lister: ProcessLister

    def work(self) -> tuple[SandboxProcess, ...] | None:
        """The processes doing work in the sandbox, ``()`` when there are
        none, ``None`` when the sandbox could not be read."""
        listed = self.lister.list_processes()
        if listed is None:
            return None
        return work_processes(listed, agent_pids=self.lister.agent_pids)


def probe_for(mode: SandboxMode, spec: SandboxSpec, agent_pid: int) -> SandboxProcessProbe | None:
    """The probe for a chat whose agent server was launched under ``spec`` as
    ``agent_pid``: the container's own table under gVisor, the chat's cgroup
    tree on a box that has one, ``None`` where the chat has neither."""
    if mode == "gvisor":
        return SandboxProcessProbe(
            RunscLister(
                runsc=spec.runsc,
                root=host_path(spec.runsc_root),
                container=chat_container(spec.chat_id),
                uid=spec.uid,
            )
        )
    cgroup = chat_cgroup_dir(spec.cgroup, spec.chat_id, spec.cgroup_root)
    if cgroup is None:
        return None
    return SandboxProcessProbe(CgroupLister(cgroup, agent_pid))


def descendants(processes: Iterable[SandboxProcess], root: int) -> tuple[int, ...]:
    """``root`` and every process below it in ``processes``, parents first."""
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process.ppid, []).append(process.pid)
    found: list[int] = []
    queue = [root]
    while queue:
        pid = queue.pop(0)
        if pid in found:
            continue
        found.append(pid)
        queue.extend(children.get(pid, []))
    return tuple(found)


def runsc_kill_argv(lister: RunscLister, pid: int, signal: str) -> tuple[str, ...]:
    """``runsc kill`` for one process inside the lister's container."""
    return (lister.runsc, f"--root={lister.root}", "kill", f"--pid={pid}", lister.container, signal)


def _survivors(table: Iterable[SandboxProcess], tree: Iterable[int]) -> tuple[int, ...]:
    """The pids of ``tree`` still running in ``table``, each with whatever now
    runs below it (a child forked after the tree was read). A parent that died
    first leaves its children reparented to the agent server, so they are
    found by their own pids, not by walking down from the root again. A
    zombie is a process that already ended and that the agent server (its
    new parent, which reaps nothing) has not collected: dead, not a survivor."""
    listed = tuple(table)
    alive = {p.pid for p in listed if p.state != "Z"}
    found: list[int] = []
    for pid in tree:
        if pid not in alive:
            continue
        found.extend(p for p in descendants(listed, pid) if p in alive and p not in found)
    return tuple(found)


def kill_tree(
    lister: RunscLister,
    pid: int,
    *,
    grace: float | None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, ...]:
    """End the command rooted at ``pid`` inside the container: ``TERM`` to it and
    everything below it, up to ``grace`` seconds for them to go, then ``KILL`` to
    whatever is left (a ``grace`` of ``None`` sends the ``TERM`` alone and
    forces nothing, like the host-side kill). Answers the pids still in the
    table afterwards (none when it worked, none too when the table cannot be
    read, since nothing is then known). The host-side ``runsc exec`` client
    dying ends nothing inside: a ``nohup``'d child, or the shell itself, would
    live on under the agent server and hold the chat awake as work. A ``pid``
    the table does not list is already gone, and nothing is signalled."""
    table = lister.list_processes()
    if table is None or not any(p.pid == pid for p in table):
        return ()
    tree = descendants(table, pid)
    for victim in tree:
        lister.runner(runsc_kill_argv(lister, victim, "SIGTERM"))
    if grace is None:
        return ()
    sleep(grace)
    remaining = lister.list_processes()
    if remaining is None:
        return ()
    left = _survivors(remaining, tree)
    for victim in left:
        lister.runner(runsc_kill_argv(lister, victim, "SIGKILL"))
    final = lister.list_processes()
    if final is None:
        return ()
    return _survivors(final, tree)


class SandboxProbes:
    """How to read each sandbox's processes, by session id (for a cloud chat,
    its chat id). The launch that spawns an agent server is the only place
    that knows its sandbox and its pid, so it records the probe here; whoever
    composed the runtime holds this registry and reads it by the same id."""

    def __init__(self) -> None:
        self._probes: dict[str, SandboxProcessProbe] = {}
        self._lock = threading.Lock()

    def register(self, session_id: str, probe: SandboxProcessProbe | None) -> None:
        """Record (or, with ``None``, drop) the probe for ``session_id``."""
        with self._lock:
            if probe is None:
                self._probes.pop(session_id, None)
            else:
                self._probes[session_id] = probe

    def drop(self, session_id: str) -> None:
        self.register(session_id, None)

    def work(self, session_id: str) -> tuple[SandboxProcess, ...] | None:
        """What runs in the session's sandbox besides its agent server; ``None``
        when it has no sandbox here or it could not be read. Blocking."""
        with self._lock:
            probe = self._probes.get(session_id)
        return None if probe is None else probe.work()


__all__ = [
    "CONTAINER_TABLE_SCRIPT",
    "MAX_TABLE_BYTES",
    "PROBE_PATH",
    "RUNSC_PS_TIMEOUT_SECONDS",
    "CgroupLister",
    "ProbeOutputTooLargeError",
    "ProcessLister",
    "Runner",
    "RunscLister",
    "SandboxProbes",
    "SandboxProcess",
    "SandboxProcessProbe",
    "descendants",
    "kill_tree",
    "parse_container_table",
    "parse_proc_stat",
    "probe_for",
    "run_probe",
    "runsc_kill_argv",
    "work_processes",
]
