"""The supervisor's own command line: ``alkera cloud-mirror supervise``.

The box's root process runs for the box's whole life and holds no tenant
data, so it loads only this package and what it needs: the CLI entry hands
this argv over before the full command tree (Typer, Rich, every command and
everything they import) is ever loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Final

from alkera_core.compute.box_contract import START_COMMAND, START_MODE_ENV, StartMode
from alkera_core.compute.box_logs import PREREQUISITES_MISSING
from alkera_core.process import replace_process

from alkera_cli.host.launcher import running_launcher
from alkera_cli.supervisor import COMMAND, box_log_shipping, org_events, preflight
from alkera_cli.supervisor.service import run_supervisor

#: The exit status of a supervisor the host is not ready for.
EXIT_PREREQUISITES_MISSING: Final = 3


def fall_back_to_the_single_daemon(
    env: MutableMapping[str, str],
    replace: Callable[[Sequence[str]], object] = replace_process,
) -> None:
    """Become the single daemon in place, when the box's start command made
    this the supervisor: it serves one org at a time and needs none of what is
    missing, so the box keeps working, and its heartbeat (no ``org_workers``)
    has the server send it one org only. A supervisor started by name is
    left to fail."""
    if env.get(START_MODE_ENV) != StartMode.SUPERVISE:
        return
    env[START_MODE_ENV] = StartMode.SINGLE
    replace([str(running_launcher()), *START_COMMAND.split()])


def main(
    argv: Sequence[str],
    *,
    missing: Callable[[], list[str]] = preflight.missing_prerequisites,
    fall_back: Callable[[MutableMapping[str, str]], None] = fall_back_to_the_single_daemon,
) -> int:
    """Run the supervisor; the process's exit status."""
    parser = argparse.ArgumentParser(
        prog=" ".join(("alkera", *COMMAND)),
        description="Run the box supervisor: claim the machine, one worker per org.",
    )
    parser.add_argument(
        "--routing-file",
        type=Path,
        default=None,
        help="Read the routing from this file instead of the backend (development).",
    )
    parser.add_argument("--log-level", default="info", help="debug|info|warning|error")
    args = parser.parse_args(list(argv))
    org_events.configure(str(args.log_level))
    shipper = box_log_shipping.install(os.environ)
    faults = missing()
    if faults:
        # Named once, shipped with the box's log, before any worker could die
        # of it; the shipper is flushed first, since what follows may replace
        # this process.
        for fault in faults:
            org_events.emit(PREREQUISITES_MISSING, level="error", error=fault)
        if shipper is not None:
            shipper.close()
        fall_back(os.environ)
        return EXIT_PREREQUISITES_MISSING
    try:
        return asyncio.run(run_supervisor(routing_file=args.routing_file))
    finally:
        if shipper is not None:
            shipper.close()


__all__ = ["EXIT_PREREQUISITES_MISSING", "fall_back_to_the_single_daemon", "main"]
