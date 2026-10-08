"""Cross-platform process liveness, termination, reaping and spawning.

This module owns what a child process is and inherits: whether this process
can read its children's exit status (``SIGCHLD``), how a child is detached
from the terminal's Ctrl-C and bound to its parent's lifetime, and how a pid
is judged alive or ended.

POSIX models "is this pid alive?" with signal 0 and terminates with
``SIGTERM`` / ``SIGKILL``. Windows has neither concept: ``os.kill(pid, sig)``
routes EVERY non-CTRL signal — *including 0* — to ``TerminateProcess``, so the
usual ``os.kill(pid, 0)`` "probe" would KILL the target instead of reporting on
it. We branch explicitly and use the Win32 API directly on Windows.

``process_alive`` answers a question; ``terminate_process`` / ``kill_process``
make a best effort and return without raising for the "already gone / not ours"
cases (mirroring the POSIX ``contextlib.suppress`` idiom callers used before).

A pid is a number the kernel hands out again once its process is gone —
Windows does so within milliseconds on a busy machine — so a pid alone cannot
say WHICH process a caller means. ``process_start_id`` reads the kernel's
creation stamp of the process currently owning the pid; a stamp recorded when
the pid was first observed tells that incarnation from any later one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

__all__ = [
    "PROC_STATUS",
    "ErrorSpec",
    "OutputSpec",
    "SpawnSpec",
    "SpawnSpecError",
    "StdinSpec",
    "bind_to_parent_lifetime",
    "children_reapable",
    "close_job",
    "kernel_ignores_sigchld",
    "kill_process",
    "kill_tree",
    "kill_tree_async",
    "kill_tree_now",
    "launch_argv",
    "pdeathsig_launcher",
    "popen_args",
    "process_alive",
    "process_pool",
    "process_start_id",
    "reclaim_children",
    "release",
    "replace_process",
    "require_program",
    "run",
    "run_async",
    "run_captured",
    "run_text",
    "spawn",
    "spawn_async",
    "spawn_command",
    "spawn_kwargs",
    "terminate_process",
    "unlaunched",
]

logger = logging.getLogger(__name__)


if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102
    _ERROR_ACCESS_DENIED = 5

    # Pin the signatures — a HANDLE is pointer-sized, and the default ctypes
    # restype of c_int truncates it on 64-bit Windows.
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4

    def _win_alive(pid: int) -> bool:
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            # Access-denied means the process exists but isn't ours to query.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            # A still-running process never signals; the wait times out.
            return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            _kernel32.CloseHandle(handle)

    def _win_terminate(pid: int) -> None:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            _kernel32.TerminateProcess(handle, 1)
        finally:
            _kernel32.CloseHandle(handle)

    def _win_start_id(pid: int) -> int | None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            # Creation time in 100-ns units since 1601: fixed at creation and
            # never rewritten, so two processes cannot share it.
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            _kernel32.CloseHandle(handle)


if sys.platform == "darwin":
    import ctypes

    _PROC_PIDTBSDINFO = 3
    # sizeof(struct proc_bsdinfo); ``pbi_start_tvsec`` / ``pbi_start_tvusec``
    # are its last two uint64 fields.
    _PROC_BSDINFO_SIZE = 136
    _PROC_BSDINFO_START_SEC = 120
    _PROC_BSDINFO_START_USEC = 128

    def _load_libproc() -> ctypes.CDLL | None:
        try:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError:  # pragma: no cover - libproc ships with every macOS
            return None
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        return lib

    _libproc = _load_libproc()

    def _darwin_start_id(pid: int) -> int | None:
        if _libproc is None:  # pragma: no cover
            return None
        info = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
        written = _libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, info, _PROC_BSDINFO_SIZE)
        if written != _PROC_BSDINFO_SIZE:
            # ESRCH for a pid nobody owns, EPERM for a process the caller may
            # not inspect: either way nothing to compare against.
            return None
        raw = info.raw
        sec = int.from_bytes(raw[_PROC_BSDINFO_START_SEC:_PROC_BSDINFO_START_USEC], "little")
        usec = int.from_bytes(raw[_PROC_BSDINFO_START_USEC:_PROC_BSDINFO_SIZE], "little")
        return sec * 1_000_000 + usec


def _linux_start_id(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat_file:
            stat = stat_file.read()
    except OSError:
        return None
    # The command name sits in parentheses and may itself contain spaces or a
    # ')': the numeric fields begin after the LAST one. ``starttime`` is field
    # 22 of the line (clock ticks since boot); the remainder starts at field 3.
    _, _, rest = stat.rpartition(b")")
    fields = rest.split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _posix_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # no such process
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True  # unknown error — be conservative and treat as alive
    return True


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process. Never raises; never kills."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_alive(pid)
    return _posix_alive(pid)


def process_start_id(pid: int) -> int | None:
    """The kernel's creation stamp of the process that owns ``pid`` right now,
    or ``None`` when there is none or it cannot be read.

    The unit is whatever the platform keeps (100-ns ticks since 1601 on
    Windows, clock ticks since boot on Linux, microseconds since the epoch on
    macOS). Only equality between two readings on the same host means
    anything: equal stamps are one process, different stamps are two processes
    that were given the same number in turn. A dead process's stamp may still
    be readable while a handle keeps it (Windows); pair this with
    ``process_alive`` when liveness is the question. Never raises.
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _win_start_id(pid)
    if sys.platform == "darwin":
        return _darwin_start_id(pid)
    if sys.platform.startswith("linux"):
        return _linux_start_id(pid)
    return None  # pragma: no cover - no supported platform reaches here


def terminate_process(pid: int) -> None:
    """Ask ``pid`` to terminate (POSIX ``SIGTERM`` / Windows ``TerminateProcess``).

    Best-effort: a missing or unreachable process is a no-op, not an error."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        _win_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def kill_process(pid: int) -> None:
    """Forcibly end ``pid`` (POSIX ``SIGKILL`` / Windows ``TerminateProcess``).

    Best-effort: a missing or unreachable process is a no-op, not an error."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        # Windows has a single forced-termination primitive; reuse it.
        _win_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# -- reaping: whether this process reads its children's exit status ----------
#
# Whether this process can read its children's exit status — and making sure it can.
#
# ``SIGCHLD``'s disposition is inherited across ``exec`` and can be set by any C
# code in the process. A process with it ignored never sees its children's exit
# status: the kernel discards each child the moment it exits, ``waitpid`` answers
# ``ECHILD``, CPython's ``subprocess`` then reports ``returncode 0`` for every
# child, and asyncio reports ``255`` for a child whose status is already gone. A
# daemon in that state reads a failed ``systemd-run --scope`` as a working
# systemd and a failed ``unshare --mount`` as a private mount namespace,
# composes a launch on those verdicts, and the launch fails with the real error
# in its stderr and ``255`` for an exit code.
#
# The released Linux binary starts that way: Nuitka's anti-debugger startup ends
# with ``signal(SIGCHLD, SIG_IGN)`` after the interpreter is up. That call is
# invisible to ``signal.getsignal``, which reads Python's own handler table —
# the table records only what Python set or found at interpreter start — so the
# disposition has to be read from the kernel (``/proc/self/status``) where the
# kernel offers it, and Python's table is trusted only where it does not.
#
# Every seam that runs a child and reads its verdict — the CLI's entry point,
# the sandbox probe, the sandbox step runner, the agent spawn — calls
# :func:`reclaim_children` first. Restoring the default disposition changes only
# how THIS process's children are reaped; nothing else inherits it.

_WARNED_UNREPAIRABLE = False

#: Where the kernel reports this process's ignored signals as a hex mask, one
#: bit per signal number (bit ``n - 1`` for signal ``n``). The file is Linux's,
#: so the bit is Linux's SIGCHLD (17) whatever the host Python numbers it.
PROC_STATUS = "/proc/self/status"
_SIGCHLD_BIT = 1 << (17 - 1)


def _read_status() -> str | None:
    try:
        with open(PROC_STATUS, encoding="ascii") as handle:
            return handle.read()
    except OSError:
        return None


def kernel_ignores_sigchld(read: Callable[[], str | None] = _read_status) -> bool | None:
    """Whether the kernel's disposition for ``SIGCHLD`` is ignore, read from
    ``SigIgn`` in ``/proc/self/status``; ``None`` where there is no such file
    (not Linux) or it cannot be parsed."""
    text = read()
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("SigIgn:"):
            try:
                return bool(int(line.split(":", 1)[1].strip(), 16) & _SIGCHLD_BIT)
            except ValueError:
                return None
    return None


#: ``None`` where the interpreter has no ``SIGCHLD`` at all (Windows): such a
#: process always reads its children, whatever ``sys.platform`` a test claims.
_SIGCHLD: Final[int | None] = getattr(signal, "SIGCHLD", None)


def children_reapable() -> bool:
    """Whether a child's exit status reaches this process: ``SIGCHLD`` is not
    ignored — by the kernel's account where it gives one, else by Python's
    table. Windows has no ``SIGCHLD`` and always reads its children."""
    if _SIGCHLD is None or sys.platform == "win32":
        return True
    ignored = kernel_ignores_sigchld()
    if ignored is not None:
        return not ignored
    return signal.getsignal(_SIGCHLD) != signal.SIG_IGN


def reclaim_children() -> bool:
    """Restore the default ``SIGCHLD`` disposition when it is ignored — left so
    by an ancestor, or set behind Python's back by C code — so every child
    from here on is this process's to reap and its exit status is real.
    Returns whether children are now reapable.

    Only the main thread may change a disposition; from any other thread the
    state is reported as found, and said once."""
    global _WARNED_UNREPAIRABLE
    if children_reapable() or _SIGCHLD is None:
        return True
    try:
        signal.signal(_SIGCHLD, signal.SIG_DFL)
    except ValueError:
        if not _WARNED_UNREPAIRABLE:
            _WARNED_UNREPAIRABLE = True
            logger.warning(
                "this process cannot read its children's exit status (SIGCHLD is ignored) "
                "and only the main thread can restore it: every child exit reads as success"
            )
        return False
    logger.warning(
        "SIGCHLD was ignored (inherited, or set by this binary's startup); "
        "restored the default so children's exit status is read"
    )
    return True


# -- spawning: detached from Ctrl-C, bound to this process's lifetime ---------
#
# Cross-platform spawning that keeps the harness child (opencode / claude):
#
# 1. **detached from the terminal's Ctrl-C** — a console SIGINT must NOT reach the
#    child directly (the ``alkera`` REPL turns Ctrl-C into a graceful turn cancel).
#    POSIX: ``start_new_session=True`` (``setsid``). Windows:
#    ``CREATE_NEW_PROCESS_GROUP`` (the child is excluded from the console's Ctrl-C
#    event group).
#
# 2. **bound to the parent's lifetime — no orphans** — when ``alkera`` dies, even by
#    ``SIGKILL``, the OS takes the child with it. Linux: ``PR_SET_PDEATHSIG``, set by
#    ``setpriv --pdeathsig KILL`` in front of the command when util-linux offers it,
#    else in the child between fork and exec. Windows: a Job Object with
#    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — when the last handle (held by us)
#    closes at parent death, the OS kills the job. macOS has no such primitive, so it
#    relies on graceful teardown + the startup orphan sweep (see ``orphan_sweep``).
#
# Why the launcher rather than ``preexec_fn`` where it exists: a pre-exec hook forces
# CPython to ``fork()`` the daemon — a process with a hundred threads and native
# runtimes (the vector store's, the event loop's) — and every ``pthread_atfork``
# handler runs in it. Without the hook CPython takes the ``vfork`` path, which runs
# none of them. The daemon on the workspace box segfaulted in a native thread twice
# in one evening, each time seconds after such a fork.
#
# Usage::
#
#     argv, kwargs = spawn_command(argv)
#     proc = await asyncio.create_subprocess_exec(*argv, **kwargs, ...)
#     job = bind_to_parent_lifetime(proc.pid)   # Windows: hold this; POSIX: None
#     ...
#     close_job(job)                            # on graceful stop (no-op for None)

#: The launcher that sets the parent-death signal without a pre-exec hook, once
#: probed: the path to a ``setpriv`` that knows ``--pdeathsig``, ``""`` when there
#: is none, ``None`` before the probe. Probed once per process.
_PDEATHSIG_LAUNCHER: str | None = None


def pdeathsig_launcher() -> str:
    """The ``setpriv`` that can set ``PR_SET_PDEATHSIG`` for a command, or ``""``.

    ``setpriv --pdeathsig`` arrived in util-linux 2.33; an older one, or a
    busybox applet without the flag, answers with a usage error and is not used.
    """
    global _PDEATHSIG_LAUNCHER
    if _PDEATHSIG_LAUNCHER is not None:
        return _PDEATHSIG_LAUNCHER
    found = shutil.which("setpriv") or ""
    if found:
        try:
            probe = subprocess.run(  # noqa: S603 -- a fixed argv, no shell
                [found, "--help"],
                capture_output=True,
                timeout=5,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            found = ""
        else:
            if b"--pdeathsig" not in probe.stdout + probe.stderr:
                found = ""
    _PDEATHSIG_LAUNCHER = found
    return found


def _set_pdeathsig() -> None:  # pragma: no cover - runs in the forked child (Linux)
    """Child-side (between fork and exec, Linux only): ask the kernel to send us
    SIGKILL the instant our parent dies, then guard the race where the parent
    already died before we got here."""
    import ctypes
    import signal

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, signal.SIGKILL)  # 1 == PR_SET_PDEATHSIG
    if os.getppid() == 1:
        # Parent already gone (reparented to init) — don't linger as an orphan.
        os._exit(1)


def spawn_kwargs() -> dict[str, Any]:
    """Keyword args for ``create_subprocess_exec`` / ``Popen`` that detach the
    child from the terminal's Ctrl-C and (Linux) bind it to our lifetime."""
    if sys.platform == "win32":
        # New process group → the console Ctrl-C event isn't delivered to the
        # child (parity with POSIX setsid detaching from the controlling tty).
        # getattr fallback to the stable Win32 value so the branch is also
        # exercisable from a non-Windows host with a monkeypatched platform.
        flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flag}

    kwargs: dict[str, Any] = {"start_new_session": True}
    if sys.platform == "linux":
        # PR_SET_PDEATHSIG must be set in the child; macOS has no equivalent.
        kwargs["preexec_fn"] = _set_pdeathsig
    return kwargs


def spawn_command(
    argv: Sequence[str], *, cwd: Path | None = None, umask: int | None = None
) -> tuple[list[str], dict[str, Any]]:
    """The command to run and the kwargs to run it with, for a child bound to
    this process.

    On Linux with a capable ``setpriv`` the parent-death signal is set by the
    launcher in front of the command and no pre-exec hook is passed, so CPython
    spawns without forking this process's threads and atfork handlers.
    Everywhere else this is ``spawn_kwargs()`` with the command as given.
    ``cwd`` and ``umask`` are the child's own.

    The child's exit status must reach us (a process that inherited an
    ignored ``SIGCHLD`` would read every exit as 255), so the default
    disposition is restored here if an ancestor left it ignored.
    """
    reclaim_children()
    kwargs = spawn_kwargs()
    command = list(argv)
    if cwd is not None:
        kwargs["cwd"] = cwd.as_posix()
    if umask is not None:
        kwargs["umask"] = umask
    if sys.platform != "linux":
        return command, kwargs
    launcher = pdeathsig_launcher()
    if not launcher:
        return command, kwargs
    kwargs.pop("preexec_fn", None)
    return [launcher, "--pdeathsig", "KILL", "--", *command], kwargs


def bind_to_parent_lifetime(pid: int) -> Any:
    """Bind ``pid``'s lifetime to ours so it dies when we die.

    Returns an opaque handle to hold for the child's lifetime (Windows Job
    Object), or ``None`` where the binding is already done at spawn time (POSIX:
    setsid + PR_SET_PDEATHSIG). Pass whatever is returned to ``close_job`` on
    graceful teardown. Best-effort: never raises."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return _create_and_assign_job(pid)
    return None


def close_job(job: Any) -> None:
    """Release a handle from ``bind_to_parent_lifetime``. No-op for ``None``.

    On Windows, closing our last handle to a KILL_ON_JOB_CLOSE job kills any
    process still in it — which is what we want on graceful stop too (the
    adapter has already terminated the child by then). Best-effort."""
    if job is None:
        return
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        with _suppress_all():
            ctypes.windll.kernel32.CloseHandle(job)


if sys.platform == "win32":  # pragma: no cover - Windows only
    import contextlib
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _suppress_all() -> contextlib.AbstractContextManager[None]:
        return contextlib.suppress(Exception)

    def _create_and_assign_job(pid: int) -> Any:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.debug("spawn: CreateJobObject failed; child won't be parent-bound")
            return None

        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None

        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(handle)
        return job

else:

    def _suppress_all() -> Any:  # pragma: no cover - non-Windows shim
        import contextlib

        return contextlib.suppress(Exception)


# -- the spawn seam: every child this codebase starts -----------------------------
#
# A child inherits whatever its spawner leaves it: every open descriptor, fd 0,
# the controlling terminal's process group, and no tie to the spawner's
# lifetime. Those defaults fail far from the call site: a child that reads its
# inherited stdin consumed the org worker's control socket on fd 0 and the
# worker crash-looped; a child left in the terminal's group takes the Ctrl-C
# meant for the REPL; a child nobody binds outlives a killed daemon. So every
# child goes through :func:`spawn`, :func:`spawn_async`, :func:`run` or
# :func:`run_async` with a :class:`SpawnSpec` whose defaults are the safe ones,
# and a spec that wants something else says so (and, for an inherited
# descriptor, why).

StdinSpec = Literal["devnull", "pipe"] | int
OutputSpec = Literal["inherit", "pipe", "devnull"] | int
ErrorSpec = Literal["inherit", "pipe", "devnull", "stdout"] | int

_STREAMS: Final[dict[str, int | None]] = {
    "inherit": None,
    "pipe": subprocess.PIPE,
    "devnull": subprocess.DEVNULL,
    "stdout": subprocess.STDOUT,
}


class SpawnSpecError(ValueError):
    """A spawn spec that asks for something the seam does not do."""


@dataclass(frozen=True)
class SpawnSpec:
    """One child to start, and everything it inherits.

    ``env`` is the child's whole environment; there is no implicit inheritance,
    so a caller that means "mine" passes ``os.environ``. ``stdin`` is
    ``/dev/null`` unless the spec asks for a pipe or hands over a descriptor;
    every other descriptor is closed in the child except ``pass_fds``. A
    descriptor handed over (``stdin`` as an fd, or ``pass_fds``) names the
    reason in ``pass_fds_reason``.

    ``new_session`` puts the child in a session (POSIX) or process group
    (Windows) of its own, out of the terminal's Ctrl-C and killable as a tree.
    ``die_with_parent`` binds it to this process's lifetime: a parent-death
    signal on Linux (which follows the spawning THREAD, so a long-lived child
    must be spawned from a thread that outlives it), a kill-on-close Job Object
    on Windows, and the startup orphan sweep on macOS, which has no such
    primitive.
    """

    argv: Sequence[str]
    env: Mapping[str, str]
    cwd: Path | str | None = None
    stdin: StdinSpec = "devnull"
    stdout: OutputSpec = "inherit"
    stderr: ErrorSpec = "inherit"
    new_session: bool = True
    die_with_parent: bool = True
    pass_fds: tuple[int, ...] = ()
    pass_fds_reason: str | None = None
    umask: int | None = None
    #: The most :func:`spawn_async`'s stream readers buffer per line
    #: (asyncio's own default when ``None``).
    read_limit: int | None = None
    #: Windows: start the child with no console window of its own.
    no_window: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.argv, str) or not self.argv:
            raise SpawnSpecError("a spawn spec needs an argument list, never a shell string")
        hands_over = isinstance(self.stdin, int) or bool(self.pass_fds)
        if hands_over and not (self.pass_fds_reason or "").strip():
            raise SpawnSpecError("a descriptor handed to a child must say why (pass_fds_reason)")


def _stream(value: str | int) -> int | None:
    if isinstance(value, int):
        return value
    return _STREAMS[value]


def popen_args(spec: SpawnSpec) -> tuple[list[str], dict[str, Any]]:
    """The argument list the OS runs and the keyword arguments for
    ``subprocess.Popen`` / ``asyncio.create_subprocess_exec`` that give the
    child exactly what ``spec`` says. The argument list carries the Linux
    death-signal launcher in front when the spec asks for it."""
    reclaim_children()
    kwargs: dict[str, Any] = {
        "env": dict(spec.env),
        "close_fds": True,
        "stdin": _stream(spec.stdin),
        "stdout": _stream(spec.stdout),
        "stderr": _stream(spec.stderr),
    }
    if spec.pass_fds:
        kwargs["pass_fds"] = spec.pass_fds
    if spec.cwd is not None:
        kwargs["cwd"] = Path(spec.cwd).as_posix() if isinstance(spec.cwd, Path) else spec.cwd
    if spec.umask is not None:
        kwargs["umask"] = spec.umask
    if sys.platform == "win32":
        flags = 0
        if spec.new_session:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        if spec.no_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if flags:
            kwargs["creationflags"] = flags
        return list(spec.argv), kwargs
    if spec.new_session:
        kwargs["start_new_session"] = True
    if spec.die_with_parent and sys.platform == "linux" and not pdeathsig_launcher():
        kwargs["preexec_fn"] = _set_pdeathsig
    return launch_argv(spec.argv, die_with_parent=spec.die_with_parent), kwargs


def launch_argv(argv: Sequence[str], *, die_with_parent: bool = True) -> list[str]:
    """The argument list the OS runs for ``argv``: on Linux, a child bound to
    this process's lifetime starts behind ``setpriv --pdeathsig KILL`` when a
    capable one exists (no pre-exec hook, so CPython need not fork this
    process's threads); everywhere else ``argv`` as given."""
    command = list(argv)
    if not (die_with_parent and sys.platform == "linux"):
        return command
    launcher = pdeathsig_launcher()
    if not launcher:
        return command
    return [launcher, *_LAUNCHER_FLAGS, *command]


#: What the death-signal launcher is told before the command it runs.
_LAUNCHER_FLAGS: Final[tuple[str, ...]] = ("--pdeathsig", "KILL", "--")


def unlaunched(argv: Sequence[str]) -> list[str]:
    """The command ``argv`` runs, without the death-signal launcher
    :func:`launch_argv` may have put in front of it: what a caller asked to
    run, the same on every platform."""
    command = list(argv)
    head = len(_LAUNCHER_FLAGS) + 1
    if len(command) <= head or tuple(command[1:head]) != _LAUNCHER_FLAGS:
        return command
    if Path(command[0]).name != "setpriv":
        return command
    return command[head:]


def require_program(spec: SpawnSpec, command: Sequence[str]) -> None:
    """Raise what the OS raises for a program that is not there when the
    launcher stands in front of ``spec``'s: the launcher itself starts, and a
    missing program would otherwise surface as its own exit 127 and its own
    message ("setpriv: failed to execute ..."), naming neither the caller's
    command nor the error a caller catches. The program is looked up the way
    the OS would, on the child's ``PATH`` and relative to its directory."""
    if len(command) == len(spec.argv):
        return
    program = spec.argv[0]
    if os.sep in program:
        path = Path(program)
        if not path.is_absolute() and spec.cwd is not None:
            path = Path(spec.cwd) / path
        found = path.exists()
    else:
        found = shutil.which(program, path=spec.env.get("PATH", os.defpath)) is not None
    if not found:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)


#: Windows Job Objects of children spawned with ``die_with_parent``, by pid,
#: closed by :func:`release` (or when the child's handle object is collected).
_JOBS: dict[int, Any] = {}


def _bind(pid: int, spec: SpawnSpec, owner: object) -> None:
    if not (spec.die_with_parent and sys.platform == "win32"):
        return
    job = bind_to_parent_lifetime(pid)
    if job is None:
        return
    _JOBS[pid] = job
    weakref.finalize(owner, release, pid)


def release(pid: int) -> None:
    """Let go of the Job Object binding child ``pid`` to this process
    (Windows; a no-op elsewhere and for a child with none). Closing it ends
    whatever of the child's tree still runs."""
    close_job(_JOBS.pop(pid, None))


def spawn(spec: SpawnSpec) -> subprocess.Popen[bytes]:
    """Start ``spec``'s child."""
    command, kwargs = popen_args(spec)
    require_program(spec, command)
    proc: subprocess.Popen[bytes] = subprocess.Popen(command, **kwargs)  # noqa: S603
    _bind(proc.pid, spec, proc)
    return proc


async def spawn_async(spec: SpawnSpec) -> asyncio.subprocess.Process:
    """Start ``spec``'s child on the running event loop."""
    command, kwargs = popen_args(spec)
    require_program(spec, command)
    if spec.read_limit is not None:
        kwargs["limit"] = spec.read_limit
    proc = await asyncio.create_subprocess_exec(*command, **kwargs)
    _bind(proc.pid, spec, proc)
    return proc


def run(
    spec: SpawnSpec, *, timeout: float | None, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run ``spec``'s child to completion and collect what it wrote. A child
    still running at ``timeout`` seconds has its whole tree ended and
    ``subprocess.TimeoutExpired`` is raised; ``None`` waits as long as it
    takes, which only a caller that is itself bounded should ask for.
    ``input`` needs ``stdin="pipe"``."""
    if input is not None and spec.stdin != "pipe":
        raise SpawnSpecError("input for a child needs stdin='pipe'")
    proc = spawn(spec)
    try:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc, grace=0)
            proc.communicate()
            raise
    finally:
        release(proc.pid)
    return subprocess.CompletedProcess(list(spec.argv), proc.returncode, out, err)


def run_text(spec: SpawnSpec, *, timeout: float | None) -> subprocess.CompletedProcess[str]:
    """:func:`run` with what the child wrote decoded as UTF-8 (undecodable
    bytes replaced), the shape ``subprocess.run(..., text=True)`` gives."""
    done = run(spec, timeout=timeout)
    return subprocess.CompletedProcess(
        done.args,
        done.returncode,
        done.stdout.decode("utf-8", errors="replace") if done.stdout is not None else "",
        done.stderr.decode("utf-8", errors="replace") if done.stderr is not None else "",
    )


def run_captured(
    argv: Sequence[str], *, timeout: float | None, **_unused: object
) -> subprocess.CompletedProcess[str]:
    """``argv`` with this process's environment, its output captured and
    decoded: the shape of ``subprocess.run(argv, capture_output=True,
    text=True)``, for a module that takes a runner of that shape so a test can
    hand it a fake. The extra keywords such a caller passes are the defaults
    here."""
    spec = SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe")
    return run_text(spec, timeout=timeout)


async def run_async(
    spec: SpawnSpec, *, time_limit: float, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """:func:`run` on the event loop, ending the child's tree at ``time_limit``
    seconds (a caller's own cancellation leaves the child to it)."""
    if input is not None and spec.stdin != "pipe":
        raise SpawnSpecError("input for a child needs stdin='pipe'")
    proc = await spawn_async(spec)
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), time_limit)
        except TimeoutError:
            await kill_tree_async(proc, grace=0)
            raise subprocess.TimeoutExpired(list(spec.argv), time_limit) from None
        code = await proc.wait()
    finally:
        release(proc.pid)
    return subprocess.CompletedProcess(list(spec.argv), code, out, err)


def _signal_tree(pid: int, sig: int) -> None:
    """Send ``sig`` to ``pid``'s process group when ``pid`` leads one (it was
    spawned in a session of its own), else to ``pid`` alone."""
    try:
        group = os.getpgid(pid)
    except OSError:
        return
    try:
        if group == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except OSError:
        pass


def _windows_kill_tree(pid: int) -> None:  # pragma: no cover - Windows only
    if pid in _JOBS:
        release(pid)
        return
    taskkill = shutil.which("taskkill") or "taskkill"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 -- a fixed argv, no shell
            [taskkill, "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    terminate_process(pid)


def kill_tree_now(pid: int) -> None:
    """Kill child ``pid`` and everything it started at once, without waiting
    for any of it to go: the caller awaits the child's own exit. Best-effort."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        _windows_kill_tree(pid)
        return
    _signal_tree(pid, signal.SIGKILL)


def replace_process(argv: Sequence[str]) -> NoReturn:
    """Replace this process with ``argv`` (``execv``): the same pid, the same
    environment and descriptors marked inheritable. POSIX only in practice:
    Windows detaches the new program and hands the console back mid-run."""
    os.execv(argv[0], list(argv))  # noqa: S606 -- an argument list, no shell


def process_pool(
    *,
    max_workers: int,
    initializer: Callable[..., object] | None = None,
    initargs: tuple[Any, ...] = (),
) -> concurrent.futures.ProcessPoolExecutor:
    """A pool of worker processes for CPU-bound work. Its workers are
    multiprocessing children: they get no standard input, and they end with
    the pool."""
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers, initializer=initializer, initargs=initargs
    )


def kill_tree(proc: subprocess.Popen[bytes] | int, *, grace: float) -> None:
    """End a child and everything it started: ask the tree to stop, wait up to
    ``grace`` seconds, then kill what is left. A child spawned with
    ``new_session`` leads its own process group, which is the tree on POSIX;
    on Windows it is the child's Job Object, else ``taskkill /T``. Best-effort:
    a child already gone is not an error."""
    pid = proc if isinstance(proc, int) else proc.pid
    if pid <= 0:
        return
    if sys.platform == "win32":
        _windows_kill_tree(pid)
        return
    _signal_tree(pid, signal.SIGTERM)
    if _wait_gone(proc, grace):
        return
    _signal_tree(pid, signal.SIGKILL)
    _wait_gone(proc, 5.0)


def _wait_gone(proc: subprocess.Popen[bytes] | int, seconds: float) -> bool:
    if isinstance(proc, int):
        deadline = time.monotonic() + seconds
        while process_alive(proc):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


async def kill_tree_async(proc: asyncio.subprocess.Process, *, grace: float | None) -> None:
    """:func:`kill_tree` for a child of :func:`spawn_async`, waiting on the
    event loop. A ``grace`` of ``None`` asks the tree to stop and waits for it
    as long as it takes, forcing nothing."""
    if sys.platform == "win32":
        _windows_kill_tree(proc.pid)
        await proc.wait()
        return
    if proc.returncode is not None:
        return
    _signal_tree(proc.pid, signal.SIGTERM)
    if grace is None:
        await proc.wait()
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
        return
    except TimeoutError:
        pass
    _signal_tree(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 5.0)
