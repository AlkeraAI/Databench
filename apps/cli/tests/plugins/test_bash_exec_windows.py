"""The shell executor on Windows: a command that times out ends with
everything it started.

Windows has no process groups to signal and no ``os.killpg``; the command's
tree is ended through ``alkera_core.process``. The POSIX twin is
``test_bash_exec.py::test_timeout_kills_the_whole_process_group``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import build_child_env, run_command, select_shell
from alkera_core.process import kill_process, process_alive

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("bash") is None,
    reason="the Windows tree kill, under a bash (Git Bash) on Windows",
)


async def test_a_timed_out_command_on_windows_takes_its_child_with_it(tmp_path: Path) -> None:
    python = Path(sys.executable).as_posix()
    child = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
    tree = ChatTree(tmp_path)
    res = await run_command(
        f'"{python}" -c "{child}" & sleep 60',
        cwd=str(tmp_path),
        env=build_child_env(dict(os.environ), cwd=str(tmp_path)),
        shell=select_shell(),
        make_spill=lambda: SpillTarget(tree, "spill.txt"),
        timeout_ms=3000,
    )
    assert res.timed_out is True
    pid = int(res.output.strip().splitlines()[0])
    try:
        for _ in range(500):
            if not process_alive(pid):
                break
            await asyncio.sleep(0.02)
        assert not process_alive(pid), "the command's child outlived its timeout"
    finally:
        kill_process(pid)
