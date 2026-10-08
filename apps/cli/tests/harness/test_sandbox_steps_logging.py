"""How a sandbox step's failure is said.

A cleanup step (taking down a namespace or a veth before setup, or after the
container is gone) usually finds nothing to take down: its failure is the
ordinary case and is said at debug, not as a warning on every sleep. A
checked step's failure refuses the chat and is said as a warning.
"""

from __future__ import annotations

import logging
import sys

import pytest
from alkera_cli.harness.sandbox_steps import SandboxRefusedError, ShellStep, run_steps

LOGGER = "alkera_cli.harness.sandbox_steps"
FAILS = (sys.executable, "-c", "raise SystemExit(1)")
SUCCEEDS = (sys.executable, "-c", "pass")


def _failures(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [r.levelno for r in caplog.records if "sandbox step failed" in r.getMessage()]


def test_a_cleanup_step_that_finds_nothing_is_said_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        run_steps([ShellStep(FAILS, check=False), ShellStep(SUCCEEDS)])
    assert _failures(caplog) == [logging.DEBUG]


def test_a_checked_step_that_fails_is_a_warning_and_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(SandboxRefusedError):
        run_steps([ShellStep(FAILS)])
    assert _failures(caplog) == [logging.WARNING]
