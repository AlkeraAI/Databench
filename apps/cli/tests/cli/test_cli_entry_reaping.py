"""Every alkera process reads its children's exit status from its entry point.

The released Linux binary's startup (Nuitka's anti-debugger plugin) leaves
``SIGCHLD`` ignored, which makes ``subprocess`` report 0 for every child and
asyncio 255 for one whose status is already gone; the root callback restores
the default before any command runs, so a daemon, a chat and a mirror all
reap their own children whatever their startup left behind.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest
from alkera_cli.main import app
from typer.testing import CliRunner


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGCHLD to ignore or restore")
@pytest.mark.usefixtures("sigchld_restored")
def test_the_root_callback_restores_child_reaping_before_any_command_runs() -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    # The blind state the binary starts in: a failing child reads as success.
    assert subprocess.run(["false"], capture_output=True, check=False).returncode == 0
    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0, result.output
    assert signal.getsignal(signal.SIGCHLD) != signal.SIG_IGN
    assert subprocess.run(["false"], capture_output=True, check=False).returncode == 1
