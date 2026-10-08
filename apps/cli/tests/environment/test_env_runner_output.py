"""The local runner keeps the tail of a command's output and never more than
that: an installer that prints far past the limit costs the daemon the limit,
not everything it printed."""

from __future__ import annotations

import os
import sys
import tracemalloc

import pytest
from alkera_cli.environment import runner as runner_module
from alkera_cli.environment.runner import Command, LocalRunner

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")

MIB = 1024 * 1024


async def test_a_flood_of_output_keeps_the_tail_and_only_the_tail_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "MAX_OUTPUT_CHARS", 1 * MIB)
    printed = 64 * MIB
    script = (
        "import sys\n"
        f"block = b'x' * {MIB}\n"
        f"for _ in range({printed // MIB}):\n"
        "    sys.stdout.buffer.write(block)\n"
        "sys.stdout.buffer.write(b'THE-END')\n"
    )
    tracemalloc.start()
    try:
        result = await LocalRunner().run(
            Command(argv=(sys.executable, "-c", script), timeout_s=120)
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.exit_code == 0
    assert result.output.endswith("THE-END")
    assert len(result.output) == 1 * MIB
    assert peak < 16 * MIB, f"held {peak // MIB} MiB of a {printed // MIB} MiB flood"


async def test_short_output_comes_back_whole_with_the_exit_code() -> None:
    result = await LocalRunner().run(
        Command(argv=(sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"))
    )
    assert result.exit_code == 3
    assert result.output == "a\nb\n"


async def test_a_command_past_its_limit_is_killed_and_said() -> None:
    result = await LocalRunner().run(
        Command(argv=(sys.executable, "-c", "import time; time.sleep(30)"), timeout_s=0.5)
    )
    assert result.exit_code is None
    assert "timed out" in result.output
