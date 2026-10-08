"""The README's first-day path runs clean on a fresh clone.

Two ways it broke for a new developer, each pinned here through what `make`
actually executes rather than through the Makefile's text:

* ``make infra-up`` ran ``docker compose up --wait`` over every service, and
  ``--wait`` reports a service that exits as a failure even when it exits 0. The
  one-shot ``seaweedfs-init`` bucket creator always exits, so every
  ``make infra-up`` failed on a healthy stack and ``make infra-up && make migrate``
  stopped at the first step. A one-shot service must be kept out of the wait and
  waited on by itself (``compose wait``), where its exit code is the answer.
* ``make bootstrap`` (``uv sync``) and the targets after it (``uv run``) re-resolved
  ``uv.lock`` on the Mac, which drops a Linux-only line, so a fresh clone showed a
  modified lockfile that CI's drift job rejects before the developer changed
  anything. The first-day targets must run uv frozen.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE_LOCAL = REPO_ROOT / "deploy" / "docker" / "compose.local.yml"

pytestmark = [
    pytest.mark.skipif(not MAKEFILE.is_file(), reason="the repo Makefile is not in this tree"),
    pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed"),
    pytest.mark.skipif(sys.platform == "win32", reason="local tooling is POSIX-only"),
]

#: The README's quick start, in order, plus the single-server targets it offers
#: in place of `dev-all`. Every one of them shells out to uv.
FIRST_DAY_TARGETS = (
    "bootstrap",
    "migrate",
    "seed",
    "dev-all",
    "dev-backend",
    "dev-worker",
    "dev-gateway",
    "dev-cli",
)


def _compose_services() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_LOCAL.read_text(encoding="utf-8"))
    services: dict[str, Any] = loaded["services"]
    return services


def _one_shot_services() -> list[str]:
    """Default-profile services that run to completion and that nothing waits on.

    A service another one depends on with `service_completed_successfully` is one
    compose's `--wait` already understands; any other service that is not meant to
    keep running is one `--wait` would report as failed the moment it exits."""
    services = _compose_services()
    awaited: set[str] = set()
    for spec in services.values():
        depends = spec.get("depends_on") or {}
        if isinstance(depends, dict):
            for name, condition in depends.items():
                if (condition or {}).get("condition") == "service_completed_successfully":
                    awaited.add(name)
    return sorted(
        name
        for name, spec in services.items()
        if not spec.get("profiles")
        and str(spec.get("restart", "no")) == "no"
        and name not in awaited
    )


def _dry_run(target: str) -> list[list[str]]:
    """The commands `make -n <target>` would run, each split into words."""
    ran = subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    commands = []
    for line in ran.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            commands.append(shlex.split(stripped))
        except ValueError:
            continue
    return commands


def _compose_calls(commands: list[list[str]]) -> list[list[str]]:
    """The `docker compose … <subcommand> …` calls, with the -f/-p prefix dropped."""
    calls = []
    for words in commands:
        if words[:2] != ["docker", "compose"]:
            continue
        rest = words[2:]
        while rest and rest[0] in {"-f", "-p"}:
            rest = rest[2:]
        calls.append(rest)
    return calls


def test_the_local_stack_has_a_one_shot_service_to_keep_out_of_the_wait() -> None:
    """Guards the next test from passing vacuously: if the bucket creator stops being
    a one-shot, the list below must be revisited, not silently emptied."""
    assert "seaweedfs-init" in _one_shot_services()


def _infra_up_text() -> str:
    """`make -n infra-up`, as text: the wait's service list is computed in the
    recipe's shell, so its exclusions read off the command, not off argv."""
    ran = subprocess.run(
        ["make", "--no-print-directory", "-n", "infra-up"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return ran.stdout


def test_infra_up_never_waits_on_a_service_that_exits() -> None:
    text = _infra_up_text()
    waits = [line for line in text.splitlines() if " up --wait" in line]
    assert len(waits) == 1, f"expected one `docker compose up --wait`, got {waits}"
    # The wait takes the service list computed just before it; every one-shot
    # must be filtered out of that list.
    for name in _one_shot_services():
        assert f"-e {name}" in text and "grep -vxF" in text, (
            f"`make infra-up` waits on {name!r}, which exits when its job is done; "
            f"`up --wait` reports that exit as a failure and the target fails on a "
            f"healthy stack. Keep it out of the wait and wait on it on its own."
        )


def test_infra_up_waits_on_each_one_shot_after_the_stack_and_answers_with_its_exit() -> None:
    """Kept out of the wait is not enough: the bucket must still be created, and a
    creator that fails must fail the target, which `compose wait` (it returns the
    container's exit code) does."""
    calls = _compose_calls(_dry_run("infra-up"))
    wait_at = next(i for i, call in enumerate(calls) if call[:1] == ["up"] and "--wait" in call)
    for name in _one_shot_services():
        started = [i for i, call in enumerate(calls) if call[:1] == ["up"] and call[-1] == name]
        waited = [i for i, call in enumerate(calls) if call == ["wait", name]]
        assert started and waited, f"`make infra-up` never runs and waits on the one-shot {name!r}"
        assert wait_at < started[0] < waited[0], f"{name!r} runs out of order: {calls}"


def test_bootstrap_installs_from_the_lock_without_rewriting_it() -> None:
    syncs = [words for words in _dry_run("bootstrap") if words[:2] == ["uv", "sync"]]
    assert syncs, "`make bootstrap` no longer runs `uv sync`"
    for words in syncs:
        assert "--frozen" in words or "--locked" in words, (
            f"`make bootstrap` runs {' '.join(words)!r}, which re-resolves uv.lock on a "
            f"Mac and leaves a fresh clone with a modified lockfile"
        )


@pytest.fixture
def env_reporting_shell(tmp_path: Path) -> Path:
    """A stand-in for the recipe shell that prints UV_FROZEN instead of running."""
    shell = tmp_path / "report-env.sh"
    shell.write_text('#!/bin/sh\nprintf "UV_FROZEN=%s\\n" "${UV_FROZEN-unset}"\n', encoding="utf-8")
    shell.chmod(shell.stat().st_mode | stat.S_IXUSR)
    return shell


@pytest.mark.parametrize("target", FIRST_DAY_TARGETS)
def test_first_day_targets_run_uv_frozen(target: str, env_reporting_shell: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "UV_FROZEN"}
    ran = subprocess.run(
        ["make", "--no-print-directory", target, f"SHELL={env_reporting_shell}"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    reported = [line for line in ran.stdout.splitlines() if line.startswith("UV_FROZEN=")]
    assert reported, f"`make {target}` ran no recipe line: {ran.stdout}{ran.stderr}"
    assert set(reported) == {"UV_FROZEN=1"}, (
        f"`make {target}` runs uv without UV_FROZEN, so it re-locks uv.lock on a Mac: "
        f"{sorted(set(reported))}"
    )


def test_a_target_off_the_first_day_path_is_not_frozen(env_reporting_shell: Path) -> None:
    """The freeze is scoped to the first-day targets, not exported repo-wide: the
    previous test must be seeing the target-specific export, not an ambient one."""
    env = {key: value for key, value in os.environ.items() if key != "UV_FROZEN"}
    ran = subprocess.run(
        ["make", "--no-print-directory", "migrate-check", f"SHELL={env_reporting_shell}"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    reported = {line for line in ran.stdout.splitlines() if line.startswith("UV_FROZEN=")}
    assert reported == {"UV_FROZEN=unset"}
