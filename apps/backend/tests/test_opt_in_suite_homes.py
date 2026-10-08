"""The opt-in suites are collected from their homes, and every marked test lives there.

`make e2e`, `make live` and the gate jobs that call pytest themselves name the
directories and files the `opencode_e2e` / `claude_e2e` and `live` / `slow_live`
tests live in (`E2E_TEST_PATHS` / `LIVE_TEST_PATHS` in the Makefile) instead of
letting `-m` walk the whole tree: the walk imports every test module to deselect
all but a couple of hundred items, and on a Windows runner that is most of an
e2e job's pytest time. The price is a list that can go stale — a marked test
written outside its home would silently never run in CI — so this pins the list
to the tree: every test carrying one of those markers is under a listed path,
every listed path exists, and every runner of the suite uses the list.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_pr_gate_shards import _markers_on_every_test_function, _testpaths, _under

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-gate.yml"

#: Each suite: the Makefile variable naming its homes, the markers that put a
#: test in it, and the Makefile targets that run it.
SUITES: dict[str, tuple[str, frozenset[str], tuple[str, ...]]] = {
    "e2e": ("E2E_TEST_PATHS", frozenset({"opencode_e2e", "claude_e2e"}), ("e2e",)),
    "live": ("LIVE_TEST_PATHS", frozenset({"live", "slow_live"}), ("live", "live-slow")),
}

#: The gate jobs that call pytest for a suite themselves rather than through
#: `make`, and the suite each must ask the Makefile for.
WORKFLOW_RUNNERS: dict[str, str] = {"e2e-windows": "e2e", "live-local": "live"}


def _variable(name: str) -> list[str]:
    found = re.search(rf"^{name} := (.+)$", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None, f"{name} is not defined in the Makefile"
    return found.group(1).split()


def _target_body(name: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    found = re.search(
        rf"^{re.escape(name)}:.*?(?=^\.PHONY:|^[a-z][\w-]*:)", text, re.MULTILINE | re.DOTALL
    )
    assert found is not None, f"no target {name!r} in the Makefile"
    return found.group(0)


def _marked_test_files(markers: frozenset[str]) -> dict[str, set[str]]:
    """Every test file with a function carrying one of `markers`, with the marks."""
    hits: dict[str, set[str]] = {}
    for testpath in _testpaths():
        for file in sorted((REPO_ROOT / testpath).rglob("test_*.py")):
            carried: set[str] = set()
            for marks in _markers_on_every_test_function(file).values():
                carried |= marks & markers
            if carried:
                hits[file.relative_to(REPO_ROOT).as_posix()] = carried
    return hits


def _job_run_text(job_id: str) -> str:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_id]
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_every_listed_home_exists(suite: str) -> None:
    variable, _markers, _targets = SUITES[suite]
    missing = [path for path in _variable(variable) if not (REPO_ROOT / path).exists()]
    assert not missing, (
        f"{variable} names paths that are not in the tree: {missing}. A renamed or "
        f"deleted home makes pytest refuse the whole run."
    )


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_every_marked_test_lives_in_a_listed_home(suite: str) -> None:
    variable, markers, _targets = SUITES[suite]
    homes = _variable(variable)
    marked = _marked_test_files(markers)
    strays = {
        file: sorted(marks)
        for file, marks in marked.items()
        if not any(_under(file, home) for home in homes)
    }
    assert not strays, (
        f"these files carry {sorted(markers)} tests but are outside {variable}, so "
        f"`make {suite}` and the gate would never collect them: {strays}. Add each "
        f"file (or its directory) to {variable} in the Makefile."
    )
    # If the scan stopped finding a marker at all, it is reading the tree wrongly
    # and the check above proves nothing.
    seen = set().union(*marked.values()) if marked else set()
    assert seen == markers, (
        f"found no test carrying {sorted(markers - seen)} — either that suite is "
        f"gone (drop it here) or this scan no longer sees how tests are marked"
    )


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_every_make_target_for_the_suite_collects_from_its_homes(suite: str) -> None:
    variable, _markers, targets = SUITES[suite]
    for target in targets:
        body = _target_body(target)
        assert f"$({variable})" in body, (
            f"`make {target}` runs pytest without $({variable}); it would walk the whole tree again"
        )


@pytest.mark.parametrize(("job_id", "suite"), sorted(WORKFLOW_RUNNERS.items()))
def test_every_gate_job_that_runs_the_suite_itself_asks_for_its_homes(
    job_id: str, suite: str
) -> None:
    run = _job_run_text(job_id)
    assert "uv run pytest -m" in run, f"job {job_id!r} no longer runs pytest itself"
    assert f"$(make -s print-suite-paths SUITE={suite})" in run, (
        f"job {job_id!r} runs pytest -m without the suite's homes; it collects the "
        f"whole tree to deselect it"
    )


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_print_suite_paths_prints_the_same_list_the_targets_use(suite: str) -> None:
    variable, _markers, _targets = SUITES[suite]
    printed = subprocess.run(
        ["make", "-s", "print-suite-paths", f"SUITE={suite}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert printed.stdout.split() == _variable(variable)


@pytest.mark.parametrize("suite", ["", "nightly"], ids=["unset", "unknown"])
def test_print_suite_paths_refuses_a_suite_it_does_not_know(suite: str) -> None:
    """A wrong name must not print an empty list: pytest given no path collects
    the whole tree, which is exactly what the list exists to avoid."""
    refused = subprocess.run(
        ["make", "-s", "print-suite-paths", f"SUITE={suite}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert refused.stdout.strip() == ""
    assert "SUITE must be e2e or live" in refused.stderr
