"""The `alkera` entry: hand-overs that run before the command tree is imported,
then the open CLI (``alkera_cli.main``).

Shared by the open entry points (``python -m alkera_cli``, the open console
script) and the product's own entry point. Each
installs a composition before the command tree loads: the product passes its
own ``install``, and with none the open platform's
(:data:`OPEN_COMPOSITION`) is installed, so an open install never runs with no
extensions. The box supervisor's
command is handed over before anything else is imported: the box's root process
loads its own small package and nothing of the command tree
(:mod:`alkera_cli.supervisor.cli`).
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from typing import Any

from alkera_cli.org_worker_channel import take_control_channel
from alkera_cli.supervisor import supervisor_args

#: The open platform's composition, named rather than imported: this module
#: loads before the supervisor hand-over, which must import nothing of the
#: command tree.
OPEN_COMPOSITION = "alkera_cli.app.open_product:install"


def _open_install() -> None:
    module_name, _, function_name = OPEN_COMPOSITION.partition(":")
    getattr(importlib.import_module(module_name), function_name)()


def _hand_over_to_the_supervisor() -> None:
    """Run the box supervisor and exit, when this process is it."""
    args = supervisor_args(sys.argv, os.environ)
    if args is not None:
        from alkera_cli.supervisor.cli import main as supervise

        raise SystemExit(supervise(args))


#: The flag the supervisor names an org worker's control channel with.
_CONTROL_FD_FLAG = "--control-fd"


def _guard_the_worker_channel(argv: list[str]) -> None:
    """An org worker's supervisor channel moves off its standard stream first.

    The supervisor hands the worker its socketpair on stdin, and anything the
    process starts before the worker command runs (the CLI's own housekeeping
    on launch) would inherit it there; a program that shuts down a socket it
    gets as stdin (``runsc exec``) ends the channel and the worker with it.
    So the channel is moved before the command tree is even imported, and the
    command is told the fd it now lives on."""
    if tuple(argv[1:3]) != ("cloud-mirror", "worker") or _CONTROL_FD_FLAG not in argv:
        return
    at = argv.index(_CONTROL_FD_FLAG) + 1
    if at >= len(argv) or not argv[at].isdigit():
        return
    argv[at] = str(take_control_channel(int(argv[at])))


def _open_cli(install: Callable[[], None] | None) -> Any:
    """The open CLI module, imported only after ``install`` has run.

    An extension point freezes the first time it is read, so the order is the
    contract: were the open CLI imported first, any import-time read of a point
    (``CLI_PLUGINS``) would freeze it empty, and installing would then raise."""
    (install if install is not None else _open_install)()
    from alkera_cli import main as open_cli

    return open_cli


def main(install: Callable[[], None] | None = None) -> None:
    """The compiled binary and `python -m`: the Typer app itself."""
    _hand_over_to_the_supervisor()
    _guard_the_worker_channel(sys.argv)
    _open_cli(install).app()


def console_main(install: Callable[[], None] | None = None) -> None:
    """The `[project.scripts]` console script, which owns the crash path."""
    _hand_over_to_the_supervisor()
    _guard_the_worker_channel(sys.argv)
    _open_cli(install).main()
