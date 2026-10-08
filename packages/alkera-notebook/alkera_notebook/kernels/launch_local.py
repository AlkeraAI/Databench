"""Starting a kernel as a local subprocess, and the launcher seam it implements.

``KernelLauncher.launch(spec)`` starts ``<interpreter> -s -S -X utf8
<mount>/boot.py`` in the notebook's folder, in a new session (so the kernel
leads its own process group), with umask 002, exactly the allowlisted environment, and
writes the token as the first line of its stdin. The returned
``LaunchedKernel`` is the only handle that signals, kills or measures the
kernel: the kernel itself never reports its pid or memory for decisions.

Interpreter flags: ``-S`` keeps ``site`` (and every ``.pth`` file) from
running before ``boot.py`` has read the token; ``boot.py`` runs ``site.main()``
itself afterwards. ``-s`` drops the user site directory. ``-I``/``-E`` are not
used because they would also drop ``PYTHONHASHSEED``, which the engine sets so
set iteration order is reproducible; the environment is built from the
allowlist below, so no other ``PYTHON*`` variable reaches the kernel.
"""

from __future__ import annotations

import asyncio
import os
import signal as _signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

#: Variables a kernel may receive. Anything else is refused at launch.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TZ",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "PYTHONHASHSEED",
        "ALKERA_RPC_ENDPOINT",
        "ALKERA_NOTEBOOK_DIR",
        "ALKERA_KERNEL_ID",
        "ALKERA_DATA_DIR",
    }
)

#: Under the engine's data root, each kernel's own data directory (its log,
#: and the files a large SQL result travels in) is ``kernels/<kernel id>``.
#: A launcher that places kernels elsewhere (a sandbox that binds the data
#: root) relies on this layout: it is the one spelling.
KERNELS_SUBDIR = "kernels"


def kernel_data_dir(data_root: str | os.PathLike[str], kernel_id: str) -> Path:
    """``kernel_id``'s data directory under the engine's ``data_root``."""
    return Path(data_root) / KERNELS_SUBDIR / kernel_id


INTERPRETER_FLAGS = ("-s", "-S", "-X", "utf8")


@dataclass(frozen=True)
class LaunchSpec:
    """What a launcher needs to start one kernel."""

    #: The person's interpreter (``<env>/bin/python``).
    interpreter: str
    #: The notebook's folder: the working directory and ``sys.path[0]``.
    notebook_dir: Path
    kernel_id: str
    #: ``ALKERA_RPC_ENDPOINT`` (``unix:<path>``) the kernel connects to.
    endpoint: str
    #: The capability token, written as the first line of stdin.
    token: str
    #: The platform mount holding ``boot.py`` and ``_alkera_kernel/``.
    mount: Path
    #: Allowlisted environment (``PATH``, ``HOME``, ``VIRTUAL_ENV``, ...). The
    #: three ``ALKERA_*`` variables are set from the fields above.
    env: Mapping[str, str] = field(default_factory=dict)
    #: Where the kernel process's own stdout and stderr go before the kernel
    #: captures them (import failures, crashes). A temporary file when None.
    log_path: Path | None = None
    #: The kernel's data directory as the engine spells it. The launcher
    #: passes it as ``ALKERA_DATA_DIR`` spelled as the kernel sees it (a
    #: sandbox sees the engine's paths at its own mount points).
    data_dir: Path | None = None


@runtime_checkable
class LaunchedKernel(Protocol):
    """A started kernel, as its launcher sees it."""

    @property
    def pid(self) -> int: ...

    @property
    def pgid(self) -> int: ...

    def signal(self, sig: int) -> None:
        """Deliver ``sig`` to the kernel's whole process group."""

    def kill(self) -> None:
        """``SIGKILL`` the whole process group."""

    def rss_bytes(self) -> int:
        """Resident memory summed over the process group, measured here."""

    async def wait(self) -> int:
        """The kernel process's exit status (negative: killed by that signal)."""


@runtime_checkable
class KernelLauncher(Protocol):
    def launch(self, spec: LaunchSpec) -> LaunchedKernel: ...

    def kill_all(self) -> None:
        """``SIGKILL`` every kernel this launcher started that may still run:
        the memory guard's second stage (in a container, the whole kernel
        sandbox)."""


def build_env(spec: LaunchSpec) -> dict[str, str]:
    """The kernel's environment: the allowlisted variables of ``spec.env``
    plus the Alkera variables the launcher sets. Raises ``ValueError`` for any other
    variable, so a caller cannot leak the workspace's environment by mistake."""
    extra = sorted(set(spec.env) - ENV_ALLOWLIST)
    if extra:
        raise ValueError(f"variables outside the kernel allowlist: {', '.join(extra)}")
    env = {k: str(v) for k, v in spec.env.items()}
    env.setdefault("PYTHONHASHSEED", "0")
    env["ALKERA_RPC_ENDPOINT"] = spec.endpoint
    env["ALKERA_NOTEBOOK_DIR"] = str(spec.notebook_dir)
    env["ALKERA_KERNEL_ID"] = spec.kernel_id
    if spec.data_dir is not None:
        env["ALKERA_DATA_DIR"] = str(spec.data_dir)
    return env


def kernel_command(spec: LaunchSpec) -> list[str]:
    return [spec.interpreter, *INTERPRETER_FLAGS, str(spec.mount / "boot.py")]


class KernelsFencedError(RuntimeError):
    """No kernel starts while the folder it would write is fenced."""

    def __init__(self) -> None:
        super().__init__(
            "This workspace lost contact with the server. Runs resume when it is back."
        )


class LocalKernel:
    """A kernel running as a local subprocess that leads its process group."""

    def __init__(self, proc: subprocess.Popen[bytes], log_path: Path) -> None:
        self._proc = proc
        self._pgid = proc.pid  # start_new_session: the kernel leads its group
        self.log_path = log_path

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def pgid(self) -> int:
        return self._pgid

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def signal(self, sig: int) -> None:
        try:
            os.killpg(self._pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS answers EPERM for a group whose only member is the
            # exited, unreaped leader; anything else is a real refusal.
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                raise PermissionError(f"cannot signal kernel process group {self._pgid}") from None

    def kill(self) -> None:
        self.signal(_signal.SIGKILL)

    def rss_bytes(self) -> int:
        return group_rss_bytes(self._pgid)

    def group_pids(self) -> list[int]:
        """Every live process still in the kernel's process group."""
        return group_pids(self._pgid)

    async def wait(self) -> int:
        code = self._proc.poll()
        if code is not None:
            return code
        return await asyncio.get_running_loop().run_in_executor(None, self._proc.wait)

    def log_tail(self, limit: int = 8192) -> str:
        """The last ``limit`` bytes the process wrote before its streams were
        captured (an import error, a crash message)."""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""


class LocalSubprocessLauncher:
    """``KernelLauncher`` for kernels on this machine. Each kernel runs with
    umask 002 so files it writes stay writable for the workspace's group."""

    def __init__(self) -> None:
        self._kernels: list[LocalKernel] = []
        self._fenced = False

    @property
    def fenced(self) -> bool:
        return self._fenced

    def fence(self) -> None:
        """Stop every kernel's process group (``SIGSTOP``) and launch none:
        the folder they write is not this machine's to write until
        :meth:`unfence`. Where no cgroup freezer is at hand, this is the
        freeze."""
        self._fenced = True
        for kernel in self._live():
            kernel.signal(_signal.SIGSTOP)

    def unfence(self) -> None:
        """Let every kernel go on where it stopped (``SIGCONT``)."""
        self._fenced = False
        for kernel in self._live():
            kernel.signal(_signal.SIGCONT)

    def _live(self) -> list[LocalKernel]:
        self._kernels = [k for k in self._kernels if k.returncode is None]
        return list(self._kernels)

    def kill_all(self) -> None:
        for kernel in self._kernels:
            kernel.kill()
        self._kernels = [k for k in self._kernels if k.returncode is None]

    def launch(self, spec: LaunchSpec) -> LocalKernel:
        if self._fenced:
            raise KernelsFencedError()
        env = build_env(spec)
        if not (spec.mount / "boot.py").is_file():
            raise FileNotFoundError(f"no boot.py in the kernel mount {spec.mount}")
        log_path = spec.log_path
        if log_path is None:
            log_path = Path(tempfile.mkdtemp(prefix="alknb-log-")) / "kernel.log"
        log = open(log_path, "ab")
        try:
            proc = subprocess.Popen(  # noqa: S603 - the interpreter the engine resolved, no shell
                kernel_command(spec),
                cwd=str(spec.notebook_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                umask=0o002,
            )
        finally:
            log.close()
        assert proc.stdin is not None
        try:
            proc.stdin.write(spec.token.encode("ascii") + b"\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
        kernel = LocalKernel(proc, log_path)
        self._kernels = [k for k in self._kernels if k.returncode is None]
        self._kernels.append(kernel)
        return kernel


# --------------------------------------------------------------------------- process groups


def group_pids(pgid: int) -> list[int]:
    if sys.platform.startswith("linux"):
        return [pid for pid, _ in _linux_group(pgid)]
    out = _ps(["-o", "pid=", "-g", str(pgid)])
    return [int(tok) for tok in out.split()]


def group_rss_bytes(pgid: int) -> int:
    """Resident set size summed over every process in group ``pgid``: from
    ``/proc`` on Linux, from ``ps -o rss= -g`` elsewhere (macOS)."""
    if sys.platform.startswith("linux"):
        return sum(rss for _, rss in _linux_group(pgid))
    out = _ps(["-o", "rss=", "-g", str(pgid)])
    return sum(int(tok) for tok in out.split()) * 1024


def _ps(args: list[str]) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def _linux_group(pgid: int) -> list[tuple[int, int]]:
    page = os.sysconf("SC_PAGE_SIZE")
    found: list[tuple[int, int]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read()
            # Fields after the command name (which may contain spaces and
            # parentheses): state ppid pgrp ...; rss is field 24 overall.
            rest = stat[stat.rindex(b")") + 2 :].split()
            if int(rest[2]) != pgid:
                continue
            found.append((int(entry), int(rest[21]) * page))
        except (OSError, ValueError, IndexError):
            continue
    return found


# --------------------------------------------------------------------------- mounts


class KernelFiles(NamedTuple):
    """The kernel's platform files: the ``boot.py`` a mount runs and the
    ``_alkera_kernel`` package directory it loads."""

    boot: Path
    package: Path


def kernel_files() -> KernelFiles:
    """The kernel's platform files as ``alkera-kernel`` provides them. ``boot.py``
    ships inside the ``_alkera_kernel`` package, so a source checkout and an
    installed wheel resolve the same way; a mount copies it to its own root."""
    import importlib.util

    spec = importlib.util.find_spec("_alkera_kernel")
    if spec is None or spec.origin is None:
        raise FileNotFoundError("the alkera-kernel distribution is not installed")
    package = Path(spec.origin).resolve().parent
    boot = package / "boot.py"
    if not boot.is_file():
        raise FileNotFoundError(f"no boot.py in {package}")
    return KernelFiles(boot=boot, package=package)


def prepare_mount(directory: Path) -> Path:
    """Assemble a kernel mount in ``directory`` from the installed sources:
    ``boot.py``, ``_alkera_kernel/`` and ``public/alkera/`` (the public API a
    notebook imports when its environment does not install ``alkera``).
    Symlinks, so it is cheap and always current."""
    import importlib.util

    files = kernel_files()
    directory.mkdir(parents=True, exist_ok=True)
    links = {
        directory / "boot.py": files.boot,
        directory / "_alkera_kernel": files.package,
    }
    alkera = importlib.util.find_spec("alkera")
    if alkera is not None and alkera.origin is not None:
        (directory / "public").mkdir(exist_ok=True)
        links[directory / "public" / "alkera"] = Path(alkera.origin).resolve().parent
    for link, target in links.items():
        if link.is_symlink() or link.exists():
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            link.unlink()
        link.symlink_to(target)
    return directory
