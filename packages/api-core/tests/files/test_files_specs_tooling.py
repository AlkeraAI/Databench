"""The TLA+ model-checking pipeline runs, and fails red when a spec is wrong.

`make files-specs` is the gate that will carry `commit_gc.tla` and
`lease_fencing.tla`. A runner that always exits 0 would pass that gate forever
while proving nothing, so the negative case here is the point: the same runner,
handed a spec whose invariant is violated, must exit non-zero and say which
invariant broke.

The specs run under a real JRE when the host has one and under
`eclipse-temurin:21-jre` in Docker otherwise, so these skip only when the host
has neither a JRE nor a Docker that answers (the `tlc_runtime` fixture).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.files._kit import tla

# `ops/scripts/tlc.sh` and `ops/scripts/ensure-tla-tools.sh` are bash scripts:
# handing one to `subprocess.run` on Windows is `[WinError 193] %1 is not a
# valid Win32 application`, before any spec is read. The specs themselves are
# model-checked by the dedicated `files-specs (TLA+)` pr-gate job on Linux, so
# nothing goes unproven by skipping the replay here.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the TLC runner is a bash script; the specs are model-checked "
        "by the files-specs job on Linux"
    ),
)

# TLC on the smoke spec finishes in seconds; the ceiling is here so a wedged JVM or a
# cold `docker pull` fails the test rather than hanging the suite.
TLC_TIMEOUT_SECONDS = 600

# The SHA-256 that ops/scripts/ensure-tla-tools.sh pins for tla2tools 1.8.0 -- the build
# committed at ops/tools/tla/. Spelled out here independently of the script so a silent
# re-pin of the jar the repo executes fails a test instead of passing unnoticed.
PINNED_JAR_SHA256 = "20322939d1b55bb0a3f674ab34bb69b87c711a6b35559d32445cb7d7f6d3bb58"


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("uv.lock not found above the test file")


REPO_ROOT = _repo_root()
ENSURE = REPO_ROOT / "ops" / "scripts" / "ensure-tla-tools.sh"
TLC = REPO_ROOT / "ops" / "scripts" / "tlc.sh"
SPEC_DIR = REPO_ROOT / "packages" / "api-core" / "specs" / "files"


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TLC_TIMEOUT_SECONDS,
        check=False,
    )


def test_ensure_tla_tools_caches_the_pinned_jar(tla_tools_jar: Path) -> None:
    """The bytes the repo hands a JVM are the pinned release, hashed independently."""
    digest = hashlib.sha256(tla_tools_jar.read_bytes()).hexdigest()
    assert digest == PINNED_JAR_SHA256
    assert tla_tools_jar.parent == REPO_ROOT / ".cache" / "tla"


def test_ensure_tla_tools_refuses_a_version_with_no_recorded_digest() -> None:
    """The negative twin: an unpinned version is refused BEFORE anything downloads."""
    cache = REPO_ROOT / ".cache" / "tla"
    before = sorted(p.name for p in cache.iterdir()) if cache.is_dir() else []

    result = subprocess.run(
        [str(ENSURE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TLC_TIMEOUT_SECONDS,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TLA_TOOLS_VERSION": "0.0.0-nope"},
    )

    assert result.returncode != 0
    assert "no pinned SHA-256" in result.stderr
    assert result.stdout.strip() == ""
    after = sorted(p.name for p in cache.iterdir()) if cache.is_dir() else []
    assert after == before, "a refused version must not leave a jar in the cache"


def test_tlc_runner_checks_the_smoke_spec_green(tla_tools_jar: Path, tlc_runtime: str) -> None:
    assert tla_tools_jar.is_file()
    result = _run([str(TLC), str(SPEC_DIR / "_smoke.tla"), str(SPEC_DIR / "_smoke.cfg"), "2"])
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "Model checking completed" in output
    assert "runtime:" in result.stderr, "the runner must say which runtime it used"


def test_tlc_runner_goes_red_when_the_invariant_is_broken(
    tla_tools_jar: Path, tlc_runtime: str, tmp_path: Path
) -> None:
    """A runner that cannot fail is not a gate.

    The counter reaches Limit, so narrowing InRange to 0..(Limit - 1) is violated by a
    reachable state and by nothing else -- TLC must report the violation, not merely
    finish.
    """
    assert tla_tools_jar.is_file()
    spec = tmp_path / "_smoke.tla"
    original = (SPEC_DIR / "_smoke.tla").read_text()
    mutated = original.replace("InRange == n \\in 0..Limit", "InRange == n \\in 0..(Limit - 1)")
    assert mutated != original, "the invariant line moved; update this mutation"
    spec.write_text(mutated)
    shutil.copyfile(SPEC_DIR / "_smoke.cfg", tmp_path / "_smoke.cfg")

    result = _run([str(TLC), str(spec), str(tmp_path / "_smoke.cfg"), "2"])
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "Invariant" in output
    assert "InRange" in output


@pytest.mark.parametrize("invariant", ["0..Limit", "0..(Limit - 1)"], ids=["green", "red"])
def test_tlc_runner_never_writes_into_the_spec_directory(
    tla_tools_jar: Path, tlc_runtime: str, tmp_path: Path, invariant: str
) -> None:
    """A run must leave the committed specs byte-identical, red runs included.

    TLC's default metadata directory is beside the spec, and on a violation it also
    drops a `<spec>_TTrace_<ts>.tla` trace-expression module there -- which would make
    every failing spec dirty the working tree and fail the drift gate. The red case is
    the one that catches a missing `-noTE`; the green case is its twin.
    """
    assert tla_tools_jar.is_file()
    original = (SPEC_DIR / "_smoke.tla").read_text()
    spec = tmp_path / "_smoke.tla"
    spec.write_text(original.replace("0..Limit", invariant))
    shutil.copyfile(SPEC_DIR / "_smoke.cfg", tmp_path / "_smoke.cfg")

    _run([str(TLC), str(spec), str(tmp_path / "_smoke.cfg"), "2"])

    # Whatever directory the spec lives in -- here a tmp one standing in for the
    # committed packages/api-core/specs/files -- must hold exactly what was put there.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_smoke.cfg", "_smoke.tla"]
    # ...and the committed directory holds only specs, configs and the README, so a
    # later spec landing here does not need this test edited.
    assert {p.suffix for p in SPEC_DIR.iterdir()} <= {".tla", ".cfg", ".md"}


def test_parallel_installs_of_the_tla_tools_all_succeed(tmp_path: Path) -> None:
    """Every xdist worker that replays a spec runs ensure-tla-tools.sh, and on a
    fresh checkout they race to install the same jar. Copying straight onto the
    shared path let one worker fail its copy, or read a half-written jar, fail the
    digest check and delete it under the rest. The install goes beside the jar and
    is renamed into place, so every concurrent run sees a whole jar or none."""
    vendored = sorted((REPO_ROOT / "ops" / "tools" / "tla").glob("tla2tools-*.jar"))
    assert vendored, "the vendored TLA+ jar is missing from the checkout"
    (tmp_path / "ops" / "scripts").mkdir(parents=True)
    (tmp_path / "ops" / "tools" / "tla").mkdir(parents=True)
    script = tmp_path / "ops" / "scripts" / ENSURE.name
    shutil.copyfile(ENSURE, script)
    for jar in vendored:
        shutil.copyfile(jar, tmp_path / "ops" / "tools" / "tla" / jar.name)

    # A copy that lands in two writes, as one does on a slow disk: a partial file
    # first, the whole one a moment later. On a filesystem that clones (APFS) a
    # real copy is instant and the window never opens.
    shims = tmp_path / "shims"
    shims.mkdir()
    slow_cp = shims / "cp"
    slow_cp.write_text(
        '#!/bin/sh\nhead -c 1024 "$1" > "$2"\nsleep 0.5\ncat "$1" > "$2"\n', encoding="utf-8"
    )
    slow_cp.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TLA_TOOLS_")}
    env["PATH"] = f"{shims}{os.pathsep}{env.get('PATH', '')}"

    def start() -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    # The rest start while the first is half-way through its copy: the moment a
    # worker can find a jar on disk that is not yet whole.
    runs = [start()]
    time.sleep(0.2)
    runs += [start() for _ in range(15)]
    results = [(run.wait(timeout=TLC_TIMEOUT_SECONDS), *run.communicate()) for run in runs]

    failed = [(code, err) for code, _out, err in results if code != 0]
    assert not failed, f"{len(failed)} of {len(runs)} concurrent installs failed: {failed[:2]}"
    cache = tmp_path / ".cache" / "tla"
    assert [p.name for p in cache.iterdir()] == ["tla2tools-1.8.0.jar"], (
        "a partial install was left behind in the cache"
    )


def test_a_probe_the_daemon_never_finishes_is_labelled_for_the_next_run_to_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged daemon can make the probe's container after the probe's own removal
    found nothing. The runner removes labelled containers whose pid is gone, so the
    probe has to start its container under this process's label."""
    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "calls.txt"
    docker = shims / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$@" >> "{calls}"\n[ "$1" = run ] && exec sleep 30\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(tla, "CONTAINER_PROBE_SECONDS", 1)

    # The uncached function: the session's own answer about the real Docker stays as it is.
    assert tla.docker_responsive.__wrapped__() is False

    asked = [line.split() for line in calls.read_text(encoding="utf-8").splitlines()]
    (started,) = [argv for argv in asked if argv[0] == "run"]
    name = started[started.index("--name") + 1]
    assert started[started.index("--label") + 1] == f"alkera.tlc.owner={os.getpid()}"
    assert ["rm", "-f", name] in asked
