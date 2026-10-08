"""A command's exit code is the command's all the way through the bash tool's
``runsc exec``-shaped wrapper — also from the state the compiled daemon starts
in.

The shipped ``alkera`` runs with ``SIGCHLD`` ignored: the anti-debugger it is
built with sets it in C at the start of the main module, after the interpreter
has already read every disposition into :func:`signal.getsignal`'s table. In
that state every command a box ran came back 255. :func:`alkera_core.process.reclaim_children`
undoes it at the CLI's entry; these pin that the undo reaches the exec path
whichever way the ignore was set — through Python, where the table sees it, and
in C past the table, as the binary has it — and that the code the reader is
shown is then 0, 1 or 127 as the command said.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness import sandbox as sb
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import run_command
from alkera_core.process import children_reapable, kernel_ignores_sigchld, reclaim_children

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX child signals")

#: Read leniently so the module collects on Windows, where the marker skips it.
SIGCHLD = getattr(signal, "SIGCHLD", 0)

#: The wrapper the bash tool spawns under gVisor, with ``runsc exec``'s job done
#: by a shell: export each ``--env`` pair, then become the command. The same
#: shape, so the exit code crosses the same hops.
EXEC_SHIM = 'while [ "$1" = "--env" ]; do export "$2"; shift 2; done; exec "$@"'

EXITS = [
    pytest.param("echo ok", 0, "ok\n", id="exit-0"),
    pytest.param("echo no >&2; exit 1", 1, "no\n", id="exit-1"),
    pytest.param("no-such-command-here 2>/dev/null", 127, "(no output)", id="exit-127"),
]


def ignore_in_python() -> None:
    signal.signal(SIGCHLD, signal.SIG_IGN)


def ignore_in_c() -> None:
    """Ignore ``SIGCHLD`` the way the compiled binary does: in C, without
    Python's signal table hearing of it. Only where the kernel's own account
    of the disposition can be read back — the reclaim reads it there, and the
    binary that starts this way is a Linux one."""
    if kernel_ignores_sigchld() is None:
        pytest.skip("the kernel's account of SIGCHLD is not readable on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.signal.restype = ctypes.c_void_p
    libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    libc.signal(int(SIGCHLD), ctypes.c_void_p(1))  # SIG_IGN is (void (*)(int))1


IGNORES = [
    pytest.param(ignore_in_python, id="ignored-through-python"),
    pytest.param(ignore_in_c, id="ignored-in-c-past-the-table"),
]


def _spill(tmp_path: Path) -> Callable[[], SpillTarget]:
    return lambda: SpillTarget(ChatTree(tmp_path), "spill.txt")


def _through_the_wrapper(tmp_path: Path, command: str) -> tuple[int | None, str]:
    launch = sb.SandboxLaunch(
        mode="gvisor",
        prefix=("/bin/sh", "-c", EXEC_SHIM, "runsc-exec-shim"),
        exec_env=True,
        env={"HOME": str(tmp_path)},
    )
    result = asyncio.run(
        run_command(
            command,
            cwd=str(tmp_path),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
            shell="/bin/sh",
            make_spill=_spill(tmp_path),
            sandbox=launch,
        )
    )
    return result.exit_code, result.output


@pytest.mark.usefixtures("sigchld_restored")
@pytest.mark.parametrize("ignore", IGNORES)
@pytest.mark.parametrize(("command", "code", "output"), EXITS)
def test_the_exec_wrapper_reports_the_commands_own_code_once_reaping_is_reclaimed(
    tmp_path: Path, ignore: Callable[[], None], command: str, code: int, output: str
) -> None:
    """The box's own sequence: the ignore the binary starts with, the entry's
    reclaim, a command through the wrapper — and the code is the command's."""
    ignore()
    assert children_reapable() is False, "the ignore must be seen, however it was set"
    assert reclaim_children() is True
    assert _through_the_wrapper(tmp_path, command) == (code, output)


@pytest.mark.usefixtures("sigchld_restored")
def test_an_ignore_set_in_c_is_invisible_to_pythons_table_and_still_undone() -> None:
    """The binary's exact shape, pinned on its own: the table says default,
    the kernel discards every child, and only a read of the real disposition
    tells — a guard that trusted the table would leave every code a lie."""
    ignore_in_c()
    assert signal.getsignal(SIGCHLD) is signal.SIG_DFL, "the table cannot see a C-level ignore"
    assert subprocess.run(["/bin/sh", "-c", "exit 3"], capture_output=True).returncode == 0
    assert children_reapable() is False
    assert reclaim_children() is True
    assert children_reapable() is True
    assert subprocess.run(["/bin/sh", "-c", "exit 3"], capture_output=True).returncode == 3


@pytest.mark.usefixtures("sigchld_restored")
def test_the_cli_entry_undoes_an_ignore_set_in_c_before_any_command_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every command goes through the root callback, so the daemon (``serve``,
    ``cloud-mirror run``) and the chat client alike start reading their
    children — from the binary's own shape, where Python's table sees nothing
    to undo. ``--help`` on a sub-app is the cheapest command that still runs
    the callback: the group's callback runs before the sub-app parses its own
    arguments. Asserted on behavior, not on the table, which the C-level
    ignore leaves stale on purpose."""
    from alkera_cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("CI", "1")  # keeps the launch's update check off the network
    ignore_in_c()
    result = CliRunner().invoke(app, ["files", "--help"])
    assert result.exit_code == 0, result.output
    assert children_reapable() is True
    assert subprocess.run(["/bin/sh", "-c", "exit 3"], capture_output=True).returncode == 3
