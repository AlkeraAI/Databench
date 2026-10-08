"""Starting a notebook kernel in its workspace's kernel sandbox.

``ContainerLauncher`` is the engine's ``KernelLauncher`` on a box. A kernel is
started with exactly::

    runsc exec --user=<kernel uid>:<files gid> --cwd=<notebook dir>
        --internal-pid-file=<host file> <sandbox>
        /usr/bin/env -i <the allowlist>
        prlimit --nproc=<n>:<n> --
        /bin/sh -c 'umask 002; exec "$@"' alkera-kernel
        <interpreter> -s -S -X utf8 /opt/alkera/py/boot.py

and the token written as the first line of its stdin. Never ``--cap``: the
kernel runs with no capability. Never the sandbox's own environment: ``env
-i`` starts it from exactly the names the engine allowed. ``prlimit`` bounds
the processes its uid may hold (gVisor enforces ``RLIMIT_NPROC``); the umask
keeps what it writes in the shared tree writable by the workspace's group.

The launcher, not the kernel, knows the kernel's pid and process group: the
pid inside the sandbox comes from ``--internal-pid-file``, and a process
``runsc exec`` starts leads its own session and group (measured), so the group
is the pid. Signals go to the whole group as the kernel's own uid (``runsc
exec ... kill -<SIG> -- -<pgid>``, 20 to 84 ms for ``SIGINT`` in the spike),
memory is summed over the group from inside, and the last resort for every
kernel at once is ``cgroup.kill`` on the sandbox.

``launch`` returns at once and never blocks the engine's event loop: claiming
the kernel's uid (which starts the sandbox for the workspace's first kernel),
handing it its socket and data directory, and spawning the kernel run in a
thread. A start that is refused is reported the way the engine reads a kernel
that exited before connecting: ``wait()`` answers non-zero and ``log_tail()``
says why.

The engine hands host paths (the notebook's folder, the interpreter, the
environment, the socket); the launcher spells each as the sandbox sees it,
and refuses one the sandbox does not hold.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import signal as _signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alkera_core.process import SpawnSpec, run, spawn, spawn_async
from alkera_notebook.envs.models import CommandResult
from alkera_notebook.kernels.launch_local import INTERPRETER_FLAGS, LaunchSpec, build_env
from alkera_notebook.rpc.service import parse_endpoint

from alkera_cli.harness.sandbox_ownership import UMASK_SHELL
from alkera_cli.harness.sandbox_steps import SandboxRefusedError
from alkera_cli.notebooks.kernel_sandbox import (
    KERNEL_MOUNT,
    KernelIdentity,
    KernelSandbox,
    exec_argv,
)
from alkera_cli.notebooks.transport_box import Chown, chown_nofollow, hand_over

logger = logging.getLogger(__name__)

#: Starts the kernel under the tree's shared umask, with nothing of the shell
#: left in its environment (``sh`` exports the ``PWD`` it sets): the kernel
#: begins with exactly the engine's allowlist. Positional, never interpolated.
KERNEL_SHELL: Final = 'umask 002; unset PWD; exec "$@"'
#: The environment names that carry a path the sandbox must see its own way.
PATH_VARIABLES: Final = ("HOME", "VIRTUAL_ENV", "CONDA_PREFIX")
#: How long the launcher waits for runsc to write the kernel's pid.
PID_WAIT_SECONDS: Final = 10.0
#: What ``wait()`` answers for a kernel whose start was refused.
START_REFUSED_STATUS: Final = 1

#: Reads the resident memory of every process in one process group, inside
#: the sandbox, as the kernel's own uid: ``/proc/<pid>/stat`` field 24 (pages)
#: for every process whose group (field 5) is ``argv[1]``. The fields are
#: counted after the command name's closing parenthesis, which a name may
#: itself contain. Run by the rootfs's interpreter, isolated, without ``site``.
GROUP_RSS_SOURCE: Final = (
    "import os, sys\n"
    "group, total = int(sys.argv[1]), 0\n"
    "page = os.sysconf('SC_PAGE_SIZE')\n"
    "for entry in os.listdir('/proc'):\n"
    "    if not entry.isdigit():\n"
    "        continue\n"
    "    try:\n"
    "        with open(f'/proc/{entry}/stat', 'rb') as fh:\n"
    "            stat = fh.read()\n"
    "        rest = stat[stat.rindex(b')') + 2 :].split()\n"
    "        if int(rest[2]) == group:\n"
    "            total += int(rest[21]) * page\n"
    "    except (OSError, ValueError, IndexError):\n"
    "        continue\n"
    "print(total)\n"
)

Run = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]
Spawn = Callable[[Sequence[str]], "subprocess.Popen[bytes]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return run(SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe"), timeout=30)


def _spawn(argv: Sequence[str]) -> subprocess.Popen[bytes]:
    return spawn(
        SpawnSpec(argv=list(argv), env=os.environ, stdin="pipe", stdout="devnull", stderr="devnull")
    )


def signal_name(sig: int) -> str:
    """``sig`` as ``kill`` takes it (``INT``, ``KILL``)."""
    return _signal.Signals(sig).name.removeprefix("SIG")


def exit_status(code: int) -> int:
    """``runsc exec``'s exit status as ``LaunchedKernel.wait`` reports it: the
    process's own code, or minus the signal that ended it (runsc reports a
    signal as ``128 + n``; the client itself killed reports ``-n``)."""
    if 128 < code < 128 + 65:
        return -(code - 128)
    return code


def container_path(sandbox: KernelSandbox, host: Path) -> str:
    """``host`` as the sandbox sees it; refused when the sandbox does not hold
    it (the spelling would be the host's, which means nothing inside)."""
    seen = sandbox.container_path(host)
    if seen is None:
        raise SandboxRefusedError(f"{host} is not in the workspace's kernel sandbox")
    return seen


def _path_entries(sandbox: KernelSandbox, raw: str) -> list[str]:
    """The ``PATH`` entries the sandbox holds, as it sees them, and the
    system's own; an entry it does not hold means nothing inside."""
    kept: list[str] = []
    for entry in raw.split(":"):
        if not entry:
            continue
        seen = sandbox.container_path(Path(entry))
        if seen is not None:
            kept.append(seen)
        elif entry.startswith(("/usr/", "/bin", "/sbin")):
            kept.append(entry)
    return kept


def sandbox_env(sandbox: KernelSandbox, spec: LaunchSpec, cwd: str) -> dict[str, str]:
    """The kernel's environment: the engine's allowlist (anything beyond it is
    refused), each path spelled as the sandbox sees it."""
    env = build_env(spec)
    for name in PATH_VARIABLES:
        if name in env:
            env[name] = container_path(sandbox, Path(env[name]))
    if "PATH" in env:
        env["PATH"] = ":".join(_path_entries(sandbox, env["PATH"]))
    env["ALKERA_NOTEBOOK_DIR"] = cwd
    socket = Path(parse_endpoint(spec.endpoint))
    env["ALKERA_RPC_ENDPOINT"] = f"unix:{container_path(sandbox, socket)}"
    if spec.data_dir is not None:
        # Large SQL results arrive as files the engine wrote there: the
        # kernel opens them at the sandbox's spelling of the runtime bind.
        env["ALKERA_DATA_DIR"] = container_path(sandbox, spec.data_dir)
    return env


def kernel_argv(
    sandbox: KernelSandbox,
    identity: KernelIdentity,
    spec: LaunchSpec,
    *,
    pid_file: Path,
) -> list[str]:
    """The exact command that starts ``spec``'s kernel in ``sandbox`` as
    ``identity`` (see the module docstring)."""
    if spec.mount.resolve() != sandbox.config.platform_mount.resolve():
        raise SandboxRefusedError("the kernel sandbox binds another platform mount")
    cwd = container_path(sandbox, spec.notebook_dir)
    interpreter = container_path(sandbox, Path(spec.interpreter))
    env = sandbox_env(sandbox, spec, cwd)
    nproc = sandbox.config.kernel_nproc
    command = (
        "prlimit",
        f"--nproc={nproc}:{nproc}",
        "--",
        "/bin/sh",
        "-c",
        KERNEL_SHELL,
        "alkera-kernel",
        interpreter,
        *INTERPRETER_FLAGS,
        f"{KERNEL_MOUNT}/boot.py",
    )
    return exec_argv(
        sandbox.target,
        (identity.uid, identity.gid),
        command,
        env=env,
        cwd=cwd,
        pid_file=pid_file,
    )


@dataclass(frozen=True, slots=True)
class _Started:
    identity: KernelIdentity
    proc: subprocess.Popen[bytes]


class ContainerKernel:
    """A kernel in the kernel sandbox, as the launcher sees it, from the
    moment ``launch`` returned (its start may still be under way)."""

    def __init__(
        self,
        sandbox: KernelSandbox,
        kernel_id: str,
        start: concurrent.futures.Future[_Started],
        pid_file: Path,
        *,
        run: Run = _run,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sandbox = sandbox
        self.kernel_id = kernel_id
        self._start = start
        self._pid_file = pid_file
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._pid: int | None = None
        self._released = False
        self._lock = threading.Lock()
        self._killed_early = False
        start.add_done_callback(self._on_started)

    def _on_started(self, start: concurrent.futures.Future[_Started]) -> None:
        # A kill that came before the kernel existed lands as soon as it does.
        if self._killed_early and start.exception() is None:
            self.signal(_signal.SIGKILL)

    def _started(self) -> _Started | None:
        if not self._start.done() or self._start.exception() is not None:
            return None
        return self._start.result()

    @property
    def identity(self) -> KernelIdentity | None:
        started = self._started()
        return started.identity if started is not None else None

    @property
    def pid(self) -> int:
        """The kernel's pid inside the sandbox, as runsc wrote it."""
        if self._pid is None:
            started = self._start.result(timeout=PID_WAIT_SECONDS)
            deadline = self._clock() + PID_WAIT_SECONDS
            while self._pid is None:
                with contextlib.suppress(OSError, ValueError):
                    self._pid = int(self._pid_file.read_text().strip())
                    break
                if started.proc.poll() is not None or self._clock() >= deadline:
                    raise ProcessLookupError(f"kernel {self.kernel_id} has no pid")
                self._sleep(0.01)
        return self._pid

    @property
    def pgid(self) -> int:
        """A process ``runsc exec`` starts leads its own session and group."""
        return self.pid

    def _exec(self, started: _Started, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        user = (started.identity.uid, started.identity.gid)
        return self._run(exec_argv(self._sandbox.target, user, argv))

    def signal(self, sig: int) -> None:
        """``sig`` to the kernel's whole process group, as its own uid. A
        ``SIGKILL`` sent before the kernel exists lands when it does."""
        started = self._started()
        if started is None:
            if sig == _signal.SIGKILL:
                self._killed_early = True
            return
        if started.proc.poll() is not None:
            return
        try:
            group = self.pgid
        except ProcessLookupError:
            return
        self._exec(started, ("kill", f"-{signal_name(sig)}", "--", f"-{group}"))

    def kill(self) -> None:
        self.signal(_signal.SIGKILL)

    def rss_bytes(self) -> int:
        """Resident memory summed over the kernel's process group, read inside
        the sandbox as the kernel's uid. For display and for choosing which
        kernel the guard stops; the guard's own decision reads the host cgroup."""
        started = self._started()
        if started is None or started.proc.poll() is not None:
            return 0
        python = f"{self._sandbox.config.python_home}/bin/python3"
        done = self._exec(started, (python, "-I", "-S", "-c", GROUP_RSS_SOURCE, str(self.pgid)))
        try:
            return max(int(done.stdout.decode().strip()), 0) if done.returncode == 0 else 0
        except ValueError:
            return 0

    async def wait(self) -> int:
        """The kernel's exit status (negative: the signal that ended it);
        :data:`START_REFUSED_STATUS` when its start was refused. Its slot is
        given back once it has exited."""
        try:
            started = await asyncio.wrap_future(self._start)
        except Exception:  # the refusal is the kernel's exit; log_tail says why
            self._release()
            return START_REFUSED_STATUS
        code = started.proc.poll()
        if code is None:
            code = await asyncio.get_running_loop().run_in_executor(None, started.proc.wait)
        self._release()
        return exit_status(code)

    def log_tail(self, limit: int = 8192) -> str:
        """Why the kernel did not start, when it did not."""
        if self._start.done():
            error = self._start.exception()
            if error is not None:
                return str(error)[-limit:]
        return ""

    def _release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._sandbox.release(self.kernel_id)
        with contextlib.suppress(OSError):
            self._pid_file.unlink()


class ContainerLauncher:
    """``KernelLauncher`` for one workspace's kernel sandbox."""

    def __init__(
        self,
        sandbox: KernelSandbox,
        *,
        run: Run = _run,
        spawn: Spawn = _spawn,
        chown: Chown = chown_nofollow,
        executor: concurrent.futures.Executor | None = None,
    ) -> None:
        self.sandbox = sandbox
        self._run = run
        self._spawn = spawn
        self._chown = chown
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=sandbox.config.max_kernels, thread_name_prefix="kernel-launch"
        )

    def launch(self, spec: LaunchSpec) -> ContainerKernel:
        """Start ``spec``'s kernel; returns at once (see the module docstring)."""
        pid_file = self.sandbox.config.state_dir / "pids" / f"{spec.kernel_id}.pid"
        start = self._executor.submit(self._start, spec, pid_file)
        return ContainerKernel(self.sandbox, spec.kernel_id, start, pid_file, run=self._run)

    def _start(self, spec: LaunchSpec, pid_file: Path) -> _Started:
        identity = self.sandbox.claim(spec.kernel_id)
        try:
            argv = kernel_argv(self.sandbox, identity, spec, pid_file=pid_file)
            socket = Path(parse_endpoint(spec.endpoint))
            data_dir = self.sandbox.data_path(spec.kernel_id)
            hand_over(socket, data_dir, identity, chown=self._chown)
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.unlink(missing_ok=True)
            proc = self._spawn(argv)
        except BaseException:
            self.sandbox.release(spec.kernel_id)
            raise
        assert proc.stdin is not None
        try:
            proc.stdin.write(spec.token.encode("ascii") + b"\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
        return _Started(identity, proc)

    def kill_all(self) -> None:
        """Every kernel of the workspace at once, through the sandbox's cgroup."""
        self.sandbox.kill_all()


#: The variables an environment build is given whose values are paths.
BUILD_PATH_VARIABLES: Final = (
    "HOME",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
)


class SandboxCommandRunner:
    """The engine's ``CommandRunner`` on a box: environment builds and
    installs run in the workspace's kernel sandbox, as its build uid, under
    the shared umask (so the environments stay the workspace's group's to
    change), through the same egress the kernels have. Paths in the argv,
    the directory and the environment are spelled as the sandbox sees them;
    an argument that names no path the sandbox holds is passed as it is."""

    def __init__(self, sandbox: KernelSandbox) -> None:
        self.sandbox = sandbox
        self._lock = asyncio.Lock()

    def command(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str] | None) -> list[str]:
        """The ``runsc exec`` that runs ``argv`` in the sandbox."""
        identity = self.sandbox.build_identity()
        inner = [self._spelled(arg) for arg in argv]
        clean: dict[str, str] = {}
        for name, value in (env or {}).items():
            if name == "PATH":
                clean[name] = ":".join(_path_entries(self.sandbox, value))
            elif name in BUILD_PATH_VARIABLES:
                clean[name] = self._spelled(value)
            else:
                clean[name] = value
        return exec_argv(
            self.sandbox.target,
            (identity.uid, identity.gid),
            ("/bin/sh", "-c", UMASK_SHELL, "alkera-build", *inner),
            env=clean,
            cwd=container_path(self.sandbox, Path(cwd)),
        )

    def _spelled(self, arg: str) -> str:
        if not arg.startswith("/"):
            return arg
        seen = self.sandbox.container_path(Path(arg))
        return seen if seen is not None else arg

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("argv is empty")
        async with self._lock:
            await asyncio.to_thread(self.sandbox.ensure_running)
            command = self.command(argv, cwd=cwd, env=env)
            proc = await spawn_async(
                SpawnSpec(argv=list(command), env=os.environ, stdout="pipe", stderr="pipe")
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                identity = self.sandbox.build_identity()
                await asyncio.to_thread(
                    _run,
                    exec_argv(
                        self.sandbox.target,
                        (identity.uid, identity.gid),
                        ("kill", "-KILL", "--", "-1"),
                    ),
                )
                out, err = await proc.communicate()
                return CommandResult(
                    returncode=proc.returncode if proc.returncode is not None else -9,
                    stdout=out.decode("utf-8", "replace"),
                    stderr=err.decode("utf-8", "replace") + f"\ntimed out after {timeout_s} s",
                    timed_out=True,
                )
        return CommandResult(
            returncode=exit_status(proc.returncode if proc.returncode is not None else -1),
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
        )


__all__ = [
    "BUILD_PATH_VARIABLES",
    "GROUP_RSS_SOURCE",
    "KERNEL_SHELL",
    "PATH_VARIABLES",
    "START_REFUSED_STATUS",
    "ContainerKernel",
    "ContainerLauncher",
    "SandboxCommandRunner",
    "container_path",
    "exit_status",
    "kernel_argv",
    "sandbox_env",
    "signal_name",
]
