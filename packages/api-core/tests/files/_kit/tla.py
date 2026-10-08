"""Provisioning the TLA+ tools jar the spec suites hand to TLC.

``ops/scripts/tlc.sh`` resolves the jar by path (``.cache/tla/tla2tools-<version>.jar``)
and never fetches it: a missing jar is exit 2, not a download. So every suite that shells
out to it has to install the jar first, and a suite that leaves that to somebody else
passes or fails on collection order -- which is exactly what happened while only the
tooling module ran ``ensure-tla-tools.sh``, leaving the trace replays green in a full run
and red when their module was run alone.

One helper, used by the ``tla_tools_jar`` fixture in
``packages/api-core/tests/files/conftest.py``, so both the tooling module and the
trace-conformance replays provision it the same way.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

#: Installing means copying ~4.5MB out of the checkout for a vendored version, or
#: downloading the release asset for one that has no vendored jar.
ENSURE_TIMEOUT_SECONDS = 600


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("uv.lock not found above the TLA+ test kit")


REPO_ROOT = _repo_root()
ENSURE = REPO_ROOT / "ops" / "scripts" / "ensure-tla-tools.sh"

#: How long ``java -version`` or ``docker info`` may take before the runtime counts as
#: absent. Short on purpose: a Docker that does not answer this would hang every run
#: that reaches it, and a suite waiting on that reads as a timeout, not as a skip.
RUNTIME_PROBE_SECONDS = 15

#: How long the JRE image may take to start and exit once it is pulled. A daemon that
#: answers ``docker info`` can still leave every new container in "Created"; a run on it
#: hangs until its own bound, so it counts as no runtime.
CONTAINER_PROBE_SECONDS = 60
JRE_IMAGE = "eclipse-temurin:21-jre"

#: The label ``ops/scripts/tlc.sh`` puts on a TLC container, valued with the pid of the
#: process that started it. Its next run removes every labelled container whose pid is
#: gone, so anything a test starts from that image carries the label too.
TLC_OWNER_LABEL = "alkera.tlc.owner"

TLC_RUNTIME_MISSING = (
    "TLC needs a local JRE or a Docker that starts the JRE image within "
    f"{CONTAINER_PROBE_SECONDS}s; this host has neither"
)


def _answers(argv: list[str]) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    try:
        done = subprocess.run(argv, capture_output=True, timeout=RUNTIME_PROBE_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


@functools.cache
def docker_responsive() -> bool:
    """Docker answers and starts a container from the JRE image, asked once per session.

    The probe container is removed whatever the outcome. A wedged daemon can still make
    it after the removal found nothing, so it carries the owner label as well: the next
    TLC run removes it once this process is gone.
    """
    if not _answers(["docker", "info"]):
        return False
    if not _answers(["docker", "image", "inspect", JRE_IMAGE]):
        return False
    name = f"alkera-tlc-probe-{uuid.uuid4().hex}"
    try:
        done = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--label",
                f"{TLC_OWNER_LABEL}={os.getpid()}",
                JRE_IMAGE,
                "true",
            ],
            capture_output=True,
            timeout=CONTAINER_PROBE_SECONDS,
            check=False,
        )
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=CONTAINER_PROBE_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


@functools.cache
def tlc_runtime() -> Literal["java", "docker"] | None:
    """The runtime ``ops/scripts/tlc.sh`` would pick here, or ``None`` when it has none.

    macOS ships a ``/usr/bin/java`` stub that is on PATH with no runtime behind it, so
    the probe runs ``java -version`` rather than trusting ``shutil.which``.
    """
    if _answers(["java", "-version"]):
        return "java"
    if docker_responsive():
        return "docker"
    return None


def provision_tla_tools_jar() -> Path:
    """Install and SHA-verify the pinned jar, returning where it landed.

    The script's whole stdout contract is one ``TLA_TOOLS_JAR=<path>`` line that the
    Makefile evals and CI appends to ``$GITHUB_ENV``; parsing it strictly here means a
    second line (a stray ``echo``) breaks a test rather than silently corrupting both
    consumers.
    """
    done = subprocess.run(
        [str(ENSURE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=ENSURE_TIMEOUT_SECONDS,
        check=False,
    )
    assert done.returncode == 0, f"ensure-tla-tools.sh failed:\n{done.stderr}"
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must carry exactly the env line, got {lines!r}"
    key, _, value = lines[0].partition("=")
    assert key == "TLA_TOOLS_JAR"
    jar = Path(value)
    assert jar.is_absolute() and jar.is_file()
    return jar
