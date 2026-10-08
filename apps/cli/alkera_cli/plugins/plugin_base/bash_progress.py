"""Whether a running shell command is making progress, for the stuck check.

A command nobody gave a wall clock runs until it finishes, or until it shows no
sign of life for :data:`~alkera_cli.plugins.plugin_base.bash_ids.STUCK_SECONDS`.
Output is one sign and the executor watches it itself. The other is CPU time:
a command that prints nothing but computes (a quiet compile, a training loop,
``pip install -q``) is working. This module reads that CPU time.

A reading is an opaque number that changes when the command's processes used
CPU since the last one. Only a change counts, so clock ticks and seconds are
both fine, and a reading that drops (a child exited and was not reaped into its
parent's totals) is a change too, never mistaken for stillness. ``None`` means
nothing could be read this time; the check then goes by output alone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import psutil

from alkera_cli.harness.sandbox_processes import CONTAINER_TABLE_SCRIPT, PROBE_PATH, Runner

logger = logging.getLogger(__name__)

#: A command's CPU reading right now, or ``None`` when it cannot be read.
CpuReading = Callable[[], float | None]


def host_tree_cpu(pid: int) -> float | None:
    """CPU seconds used by host process ``pid`` and everything under it,
    including the children it has already reaped."""
    try:
        root = psutil.Process(pid)
        members = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    total = 0.0
    for member in members:
        try:
            times = member.cpu_times()
        except psutil.Error:
            continue
        total += times.user + times.system + times.children_user + times.children_system
    return total


def stat_cpu(line: str) -> tuple[int, int, int] | None:
    """``(pid, ppid, ticks)`` from one ``/proc/<pid>/stat`` line, where ticks
    is the process's own CPU plus its reaped children's; ``None`` for a line
    that is not one. The command name may hold spaces and parentheses, so the
    line is cut at its LAST closing parenthesis."""
    head, sep, tail = line.strip().rpartition(")")
    pid_text, open_paren, _comm = head.partition(" (")
    fields = tail.split()
    # After the name: state, ppid, ... utime is field 14 of the line, so the
    # 12th after the name; then stime, cutime, cstime.
    if not sep or not open_paren or len(fields) < 15 or not pid_text.isdigit():
        return None
    try:
        ppid = int(fields[1])
        ticks = sum(int(value) for value in fields[11:15])
    except ValueError:
        return None
    return int(pid_text), ppid, ticks


def tree_ticks(table: str, root_pid: int) -> int | None:
    """CPU ticks of ``root_pid`` and its descendants in a process table of
    stat lines; ``None`` when the root is not in it."""
    rows = [row for row in (stat_cpu(line) for line in table.splitlines()) if row is not None]
    children: dict[int, list[int]] = {}
    ticks: dict[int, int] = {}
    for pid, ppid, used in rows:
        children.setdefault(ppid, []).append(pid)
        ticks[pid] = used
    if root_pid not in ticks:
        return None
    total = 0
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += ticks.get(pid, 0)
        pending.extend(children.get(pid, ()))
    return total


def container_tree_cpu(
    *, runner: Runner, runsc: str, root: str, container: str, uid: int, pid_file: Path
) -> float | None:
    """CPU ticks of the command running inside a gVisor container: its pid is
    the one ``runsc exec`` wrote to ``pid_file``, and its tree is read from the
    container's own process table. The host sees only the ``runsc exec``
    wrapper, which does no work, so its CPU says nothing about the command."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    argv = (
        runsc,
        f"--root={root}",
        "exec",
        f"--user={uid}:{uid}",
        "--env",
        f"PATH={PROBE_PATH}",
        container,
        "/bin/sh",
        "-c",
        CONTAINER_TABLE_SCRIPT,
    )
    try:
        status, out = runner(argv)
    except Exception as exc:  # a failed read is "unknown", never a crash
        logger.debug("could not read the command's CPU in %s: %s", container, exc)
        return None
    if status != 0:
        return None
    used = tree_ticks(out, pid)
    return None if used is None else float(used)


__all__ = ["CpuReading", "container_tree_cpu", "host_tree_cpu", "stat_cpu", "tree_ticks"]
