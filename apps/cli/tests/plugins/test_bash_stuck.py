"""A shell command with no wall clock runs until it finishes, unless it is stuck.

A command the model gave no timeout used to be killed at fifteen minutes, a
build or a training job included. Now nothing ends it for taking long. It ends
only when it shows no sign of life (no output, no CPU) for the stuck bound,
and the result says that is why.

The clock is injected and advanced a minute on every reading, so each test
crosses hours of turn time in a fraction of a real second: a command that is
alive goes far past the old fifteen minutes, one that is stuck ends at the
bound.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import (
    ExecResult,
    build_child_env,
    run_command,
    select_shell,
)
from alkera_cli.plugins.plugin_base.bash_progress import host_tree_cpu, stat_cpu, tree_ticks

needs_posix_shell = pytest.mark.skipif(os.name != "posix", reason="POSIX-only shell executor")

MINUTE = 60.0
OLD_CAP_SECONDS = 15 * MINUTE
STUCK = 30 * MINUTE


class _Clock:
    """Turn time that moves a minute every time anyone looks at it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += MINUTE
        return self.now


def _spill(tmp_path: Path) -> Callable[[], SpillTarget]:
    tree = ChatTree(tmp_path)
    return lambda: SpillTarget(tree, "spill.txt")


async def _run(
    tmp_path: Path,
    command: str,
    *,
    clock: _Clock,
    cpu: Callable[[], float | None],
    timeout_ms: int | None = None,
) -> ExecResult:
    return await run_command(
        command,
        cwd=str(tmp_path),
        env=build_child_env(dict(os.environ), cwd=str(tmp_path)),
        shell=select_shell(),
        timeout_ms=timeout_ms,
        make_spill=_spill(tmp_path),
        stuck_seconds=STUCK,
        cpu_reading=cpu,
        clock=clock,
        check_seconds=0.01,
    )


def _climbing() -> Callable[[], float]:
    used = [0.0]

    def _read() -> float:
        used[0] += 0.5
        return used[0]

    return _read


@needs_posix_shell
async def test_a_silent_command_that_uses_cpu_runs_past_the_old_cap(tmp_path: Path) -> None:
    clock = _Clock()
    result = await _run(tmp_path, "sleep 1; echo done", clock=clock, cpu=_climbing())

    assert clock.now > OLD_CAP_SECONDS + STUCK
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stuck is False
    assert "done" in result.output


@needs_posix_shell
async def test_a_command_that_keeps_printing_runs_past_the_old_cap(tmp_path: Path) -> None:
    clock = _Clock()
    result = await _run(
        tmp_path,
        "for i in 1 2 3 4 5 6 7 8 9 10; do echo tick $i; sleep 0.1; done",
        clock=clock,
        cpu=lambda: 0.0,
    )

    assert clock.now > OLD_CAP_SECONDS
    assert result.exit_code == 0
    assert result.stuck is False
    assert "tick 10" in result.output


@needs_posix_shell
@pytest.mark.parametrize(
    "cpu",
    [
        pytest.param(lambda: 7.0, id="cpu-flat"),
        pytest.param(lambda: None, id="cpu-unreadable"),
    ],
)
async def test_a_command_with_no_output_and_no_cpu_ends_at_the_bound(
    tmp_path: Path, cpu: Callable[[], float | None]
) -> None:
    clock = _Clock()
    result = await _run(tmp_path, "echo started; sleep 30", clock=clock, cpu=cpu)

    assert result.stuck is True
    assert result.timed_out is True
    assert result.exit_code is None
    # Ended at the bound, not long after it: a reading or two past 30 minutes
    # of silence (the first output and the check cadence each cost a minute).
    assert STUCK <= clock.now <= STUCK + 5 * MINUTE
    assert "printed nothing and used no CPU for 30 minutes" in result.output
    assert "looked stuck" in result.output
    assert "started" in result.output


@needs_posix_shell
async def test_a_timeout_the_model_chose_is_still_a_wall_clock(tmp_path: Path) -> None:
    """A model's own timeout is a choice, so it cuts a busy command too."""
    result = await _run(tmp_path, "sleep 5", clock=_Clock(), cpu=_climbing(), timeout_ms=300)

    assert result.timed_out is True
    assert result.stuck is False
    assert "exceeding timeout 300 ms" in result.output


@needs_posix_shell
async def test_a_stuck_bound_of_none_never_ends_a_quiet_command(tmp_path: Path) -> None:
    clock = _Clock()
    result = await run_command(
        "sleep 0.5; echo done",
        cwd=str(tmp_path),
        env=build_child_env(dict(os.environ), cwd=str(tmp_path)),
        shell=select_shell(),
        timeout_ms=None,
        make_spill=_spill(tmp_path),
        stuck_seconds=None,
        cpu_reading=lambda: 0.0,
        clock=clock,
        check_seconds=0.01,
    )
    assert result.exit_code == 0
    assert result.stuck is False


@needs_posix_shell
async def test_the_host_reading_moves_when_the_tree_computes(tmp_path: Path) -> None:
    """The real reading, not a stand-in: a busy child moves it, so a silent
    computation is seen as alive."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time\nend = time.time() + 30\nwhile time.time() < end: pass",
    )
    try:
        first = host_tree_cpu(proc.pid)
        await asyncio.sleep(0.4)
        second = host_tree_cpu(proc.pid)
    finally:
        proc.kill()
        await proc.wait()
    assert first is not None and second is not None
    assert second > first


def test_a_gone_process_has_no_reading() -> None:
    assert host_tree_cpu(2**22 + 12345) is None


_TABLE = "\n".join(
    [
        "42",
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt
        # cmajflt utime stime cutime cstime
        "1 (opencode) S 0 1 1 0 -1 0 0 0 0 0 900 100 0 0",
        "10 (sh) S 1 10 10 0 -1 0 0 0 0 0 1 1 5 5",
        "11 (my (odd) cmd) R 10 10 10 0 -1 0 0 0 0 0 30 10 0 0",
        "12 (other) S 1 12 12 0 -1 0 0 0 0 0 70 0 0 0",
        "not a stat line",
    ]
)


def test_the_container_reading_counts_the_command_tree_only() -> None:
    """The agent server (pid 1) and an unrelated process are left out, the
    command and its children (including a name with parentheses) are in."""
    assert tree_ticks(_TABLE, 10) == (1 + 1 + 5 + 5) + (30 + 10)
    assert tree_ticks(_TABLE, 99) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("11 (a b) R 10 1 1 0 -1 0 0 0 0 0 3 4 5 6", (11, 10, 18), id="plain"),
        pytest.param("11 (a) R 10 1", None, id="short"),
        pytest.param("x (a) R 10 1 1 0 -1 0 0 0 0 0 3 4 5 6", None, id="no-pid"),
        pytest.param("11 (a) R 10 1 1 0 -1 0 0 0 0 0 3 x 5 6", None, id="bad-ticks"),
    ],
)
def test_stat_lines_parse(line: str, expected: tuple[int, int, int] | None) -> None:
    assert stat_cpu(line) == expected
