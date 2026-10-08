"""The notebook policy's rungs are the Files ladder's, and loading the
authz package first (as a process that starts with the event outbox does)
never runs into the Files stack's own imports."""

from __future__ import annotations

import subprocess
import sys

import pytest
from alkera_core.authz.policies import notebook
from alkera_core.files.authz.ladder import DEFAULT_LADDER, ROLE_WRITER


def test_the_policys_rungs_are_the_files_ladders_in_order() -> None:
    ranked = sorted(notebook.RUNGS, key=DEFAULT_LADDER.rank)
    assert list(notebook.RUNGS) == ranked
    assert all(DEFAULT_LADDER.rank(rung) >= 0 for rung in notebook.RUNGS)
    assert notebook.RUNS_FROM == ROLE_WRITER


@pytest.mark.parametrize(
    "first",
    [
        pytest.param("alkera_core.events", id="the-event-outbox-first"),
        pytest.param("alkera_core.authz", id="the-authz-package-first"),
    ],
)
def test_a_process_may_import_the_authz_stack_first(first: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
