"""The box's root process loads the supervisor and nothing of the command tree."""

from __future__ import annotations

import subprocess
import sys

import pytest
from alkera_cli.supervisor import supervisor_args
from alkera_core.compute.box_contract import START_MODE_ENV

HEAVY = ("typer", "rich", "alkera_cli.main", "alkera_cli.cloud", "alkera_cli.harness")

_PROBE = """
import atexit, sys
heavy = {heavy!r}
atexit.register(lambda: print("LOADED=" + ",".join(sorted(
    m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in heavy)))))
sys.argv = {argv!r}
from alkera_cli.entry import main
main()
"""


def _loaded(argv: list[str]) -> tuple[int, set[str]]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE.format(heavy=HEAVY, argv=argv)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("LOADED="))
    names = {n for n in line.removeprefix("LOADED=").split(",") if n}
    return done.returncode, {n.split(".")[0] if n in ("typer", "rich") else n for n in names}


def test_the_supervisor_command_never_loads_the_command_tree() -> None:
    code, loaded = _loaded(["alkera", "cloud-mirror", "supervise", "--help"])
    assert code == 0
    assert loaded == set()


def test_every_other_command_still_goes_to_the_full_app() -> None:
    code, loaded = _loaded(["alkera", "cloud-mirror", "--help"])
    assert code == 0
    assert "typer" in loaded and "alkera_cli.main" in loaded


# ---- the one start command every build has ---------------------------------


@pytest.mark.parametrize(
    ("argv", "env", "expected"),
    [
        pytest.param(
            ["alkera", "cloud-mirror", "run"],
            {START_MODE_ENV: "supervise"},
            [],
            id="a-supervising-node-starts-the-supervisor",
        ),
        pytest.param(
            ["alkera", "cloud-mirror", "run"],
            {START_MODE_ENV: "single"},
            None,
            id="a-single-node-runs-the-daemon",
        ),
        pytest.param(
            ["alkera", "cloud-mirror", "run"], {}, None, id="a-node-booted-before-the-mode"
        ),
        pytest.param(
            ["alkera", "cloud-mirror", "run", "--log-level", "debug"],
            {START_MODE_ENV: "supervise"},
            None,
            id="a-run-someone-typed-runs-as-written",
        ),
        pytest.param(
            ["alkera", "cloud-mirror", "supervise", "--help"],
            {},
            ["--help"],
            id="the-supervisor-command",
        ),
        pytest.param(
            ["alkera", "cloud-mirror", "worker"],
            {START_MODE_ENV: "supervise"},
            None,
            id="an-org-worker-is-never-the-supervisor",
        ),
    ],
)
def test_the_start_command_becomes_the_supervisor_only_on_a_supervising_node(
    argv: list[str], env: dict[str, str], expected: list[str] | None
) -> None:
    assert supervisor_args(argv, env) == expected
