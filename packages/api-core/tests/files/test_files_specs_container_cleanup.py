"""A TLC run under Docker never leaves its container behind.

``ops/scripts/tlc.sh`` falls back to a JRE in Docker on a host with no Java. The
caller that gives up on a run (a test timeout, Ctrl-C, a killed suite) used to
take the Docker CLI down with it and leave the container: still running, or
stuck in "Created" when the CLI died before the start. Hundreds of those jam
the Docker daemon for everything else on the machine.

Each case here ends a run a different way and then asks Docker what is left.
They all need Docker itself, so they skip on a host without one that answers.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.files._kit.tla import REPO_ROOT
from tests.files._kit.tla import TLC_OWNER_LABEL as OWNER_LABEL

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="the TLC runner is a bash script with POSIX signals"
    ),
    # Starting a JVM container is Docker's time, not this test's: on a loaded host it
    # alone can pass the suite's ninety seconds. The waits below are each bounded.
    pytest.mark.timeout(420),
]

TLC = REPO_ROOT / "ops" / "scripts" / "tlc.sh"
SPEC_DIR = REPO_ROOT / "packages" / "api-core" / "specs" / "files"
IMAGE = "eclipse-temurin:21-jre"

#: A cold image pull is inside this; nothing else a case waits for is slow.
START_SECONDS = 180
#: How long after a run ended Docker may take to show its container gone.
GONE_SECONDS = 60
DOCKER_SECONDS = 60
#: The runner gives the container this long past its own wall-clock bound.
CONTAINER_GRACE_SECONDS = 30


def _docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=DOCKER_SECONDS, check=False
    )


def _containers_of(owner: int) -> list[str]:
    """Every container, in any state, that the run with this pid started."""
    done = _docker("ps", "-aq", "--filter", f"label={OWNER_LABEL}={owner}")
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def _running_containers_of(owner: int) -> list[str]:
    done = _docker(
        "ps", "-q", "--filter", f"label={OWNER_LABEL}={owner}", "--filter", "status=running"
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def _processes_naming(path: Path) -> list[str]:
    """Live processes whose command line names ``path``, other than the Docker CLI
    (which a SIGKILL leaves behind until its container ends)."""
    done = subprocess.run(
        ["pgrep", "-fl", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    return [line for line in done.stdout.splitlines() if "docker" not in line]


def _wait_until(condition: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out after {seconds}s waiting until {what}"
        time.sleep(0.5)


@pytest.fixture
def endless_spec(tmp_path: Path) -> tuple[Path, Path]:
    """The smoke counter with a bound TLC cannot reach while a test waits: two
    thousand million states, one per step."""
    spec = tmp_path / "_smoke.tla"
    cfg = tmp_path / "_smoke.cfg"
    shutil.copyfile(SPEC_DIR / "_smoke.tla", spec)
    original = (SPEC_DIR / "_smoke.cfg").read_text(encoding="utf-8")
    widened = original.replace("Limit = 5", "Limit = 2000000000")
    assert widened != original, "the smoke config's bound moved; update this fixture"
    cfg.write_text(widened, encoding="utf-8")
    return spec, cfg


Launch = Callable[..., "subprocess.Popen[bytes]"]


def _interruptible() -> None:
    """Give the runner SIGINT at its default. A suite started in the background
    hands every child SIGINT already ignored, and a shell cannot trap a signal it
    was started ignoring, so the interrupted case would measure the launcher."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.fixture
def launch(tla_tools_jar: Path, tlc_in_docker: None, tmp_path: Path) -> Iterator[Launch]:
    """Start the runner on its Docker path, output to files so a surviving child
    can never hold a pipe this test is reading. Whatever a case leaves is removed."""
    assert tla_tools_jar.is_file()
    started: list[subprocess.Popen[bytes]] = []

    def start(spec: Path, cfg: Path, *, wall_seconds: int | None = None) -> subprocess.Popen[bytes]:
        env = {**os.environ, "TLC_RUNTIME": "docker"}
        if wall_seconds is not None:
            env["TLC_WALL_SECONDS"] = str(wall_seconds)
        index = len(started)
        with (
            (tmp_path / f"run{index}.out").open("wb") as out,
            (tmp_path / f"run{index}.err").open("wb") as err,
        ):
            run = subprocess.Popen(
                [str(TLC), str(spec), str(cfg), "1"],
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                preexec_fn=_interruptible,
            )
        started.append(run)
        return run

    yield start

    for run in started:
        if run.poll() is None:
            run.kill()
            run.wait(timeout=GONE_SECONDS)
        for container in _containers_of(run.pid):
            _docker("rm", "-f", container)


@pytest.mark.parametrize(
    ("sent", "exit_code"),
    [
        pytest.param(signal.SIGTERM, 143, id="terminated"),
        pytest.param(signal.SIGINT, 130, id="interrupted"),
    ],
)
def test_a_run_that_is_stopped_removes_its_container(
    launch: Launch, endless_spec: tuple[Path, Path], sent: signal.Signals, exit_code: int
) -> None:
    run = launch(*endless_spec)
    _wait_until(lambda: bool(_running_containers_of(run.pid)), START_SECONDS, "TLC is running")

    run.send_signal(sent)

    assert run.wait(timeout=GONE_SECONDS) == exit_code
    assert _containers_of(run.pid) == []


def test_a_run_over_its_wall_clock_bound_stops_and_removes_its_container(
    launch: Launch, endless_spec: tuple[Path, Path], tmp_path: Path
) -> None:
    run = launch(*endless_spec, wall_seconds=20)

    assert run.wait(timeout=START_SECONDS) == 124
    assert "stopped after 20s" in (tmp_path / "run0.err").read_text(encoding="utf-8")
    assert _containers_of(run.pid) == []


def test_a_run_killed_outright_leaves_a_container_that_stops_itself(
    launch: Launch, endless_spec: tuple[Path, Path]
) -> None:
    """Nothing in the script runs after SIGKILL, so the bound has to be inside
    the container: it ends on its own and Docker removes it."""
    spec, cfg = endless_spec
    run = launch(spec, cfg, wall_seconds=30)
    _wait_until(lambda: bool(_running_containers_of(run.pid)), START_SECONDS, "TLC is running")

    run.kill()
    run.wait(timeout=GONE_SECONDS)

    # The script's own watchdogs are subshells with its command line; none may outlive it.
    _wait_until(lambda: not _processes_naming(spec), GONE_SECONDS, "the watchdogs exited")

    _wait_until(
        lambda: _containers_of(run.pid) == [],
        30 + CONTAINER_GRACE_SECONDS + GONE_SECONDS,
        "the container removed itself",
    )


@pytest.fixture
def leftover(tlc_in_docker: None) -> Iterator[Callable[[int], str]]:
    """A container in "Created", labelled as a TLC run of the given pid: what a
    run leaves when its Docker CLI dies between the create and the start."""
    names: list[str] = []

    def create(owner: int) -> str:
        name = f"alkera-tlc-test-{uuid.uuid4().hex}"
        done = _docker("create", "--name", name, "--label", f"{OWNER_LABEL}={owner}", IMAGE, "true")
        assert done.returncode == 0, done.stderr
        names.append(name)
        return name

    yield create

    for name in names:
        _docker("rm", "-f", name)


def _exists(name: str) -> bool:
    done = _docker("ps", "-aq", "--filter", f"name=^{name}$")
    assert done.returncode == 0, done.stderr
    return bool(done.stdout.split())


def _pid_of_a_finished_process() -> int:
    done = subprocess.Popen(["true"])
    done.wait(timeout=30)
    return done.pid


def test_the_next_run_removes_a_container_whose_run_is_gone_and_no_other(
    launch: Launch, leftover: Callable[[int], str]
) -> None:
    """The asymmetry is the point: a container whose run is alive belongs to it."""
    orphan = leftover(_pid_of_a_finished_process())
    in_use = leftover(os.getpid())

    run = launch(SPEC_DIR / "_smoke.tla", SPEC_DIR / "_smoke.cfg")

    assert run.wait(timeout=START_SECONDS) == 0
    assert not _exists(orphan)
    assert _exists(in_use)
    assert _containers_of(run.pid) == []
