"""The bootstrap's daemon install, run for real in bash against a scripted
``curl``: a served bundle installs from the host's staged copy or from the
backend on the machine credential, a digest mismatch is refused, and the
release download keeps installing from the release host."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from alkera_core.compute.bootstrap import BootstrapSpec, daemon_install_lines, render_bootstrap
from alkera_core.compute.box_contract import SOURCE_VERSION, BootstrapPlan, StartMode
from alkera_core.compute.daemon_source import (
    BUNDLE_ROUTE,
    RELEASE_DOWNLOAD,
    SERVED_BUNDLE,
)

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="runs the bootstrap's own shell lines",
)

CREDENTIAL = "alkm_the-node-credential"
API = "https://alkera.example"
TARGET = (
    "linux-arm64"
    if __import__("platform").machine().lower() in ("arm64", "aarch64")
    else "linux-x64"
)

#: Serves URLs from $SERVE by their last path segment, logs every call, and
#: copies a header file named with -H @file into the log so a test can read
#: what was sent without it ever being on argv.
CURL_STUB = r"""#!/usr/bin/env bash
out=""; url=""; hdr=""
while (($#)); do
  case "$1" in
    -o) out="$2"; shift ;;
    -H) hdr="$2"; shift ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
echo "url=$url" >>"$LOG"
if [ -n "$hdr" ]; then echo "header=$(cat "${hdr#@}")" >>"$LOG"; fi
src="$SERVE/${url##*/}"
[ -f "$src" ] || exit 22
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
"""


def _bundle(version: str = "1.2.3+abc") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, body in (
            ("alkera.dist/alkera", b"#!/bin/sh\n"),
            ("alkera.dist/VERSION", f"{version}\n".encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return raw.getvalue()


def _run(tmp: Path, lines: list[str], *, preamble: str = "") -> subprocess.CompletedProcess[str]:
    stubs = tmp / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "curl").write_text(CURL_STUB, encoding="utf-8")
    (stubs / "curl").chmod(0o755)
    (stubs / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stubs / "chown").chmod(0o755)
    box = tmp / "box"
    box.mkdir(exist_ok=True)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"BOX_ROOT={box}",
            f"MACHINE_CREDENTIAL={CREDENTIAL}",
            preamble,
            *lines,
            'echo "installed version=$DAEMON_VERSION"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "SERVE": str(tmp / "serve"),
            "LOG": str(tmp / "curl.log"),
        },
        check=False,
    )


def _serve(tmp: Path, name: str, body: bytes, *, digest: str | None = None) -> None:
    serve = tmp / "serve"
    serve.mkdir(exist_ok=True)
    (serve / name).write_bytes(body)
    sha = digest or hashlib.sha256(body).hexdigest()
    (serve / f"{name}.sha256").write_text(f"{sha}  {name}\n", encoding="utf-8")


@needs_bash
def test_a_served_bundle_is_fetched_on_the_machine_credential_and_installed(tmp_path: Path) -> None:
    _serve(tmp_path, TARGET, _bundle())
    done = _run(tmp_path, daemon_install_lines(SERVED_BUNDLE, api_url=API))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "installed version=1.2.3+abc" in done.stdout
    assert (tmp_path / "box" / "alkera.dist" / "alkera").exists()
    log = (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert f"url={API}{BUNDLE_ROUTE}/{TARGET}.sha256" in log
    assert f"header=X-Alkera-Machine-Credential: {CREDENTIAL}" in log
    assert "releases" not in log


@needs_bash
def test_a_bundle_the_provider_staged_installs_without_the_network(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    body = _bundle("9.9.9")
    (box / f"node-bundle-{TARGET}.tar.gz").write_bytes(body)
    (box / f"node-bundle-{TARGET}.tar.gz.sha256").write_text(
        f"{hashlib.sha256(body).hexdigest()}  x\n", encoding="utf-8"
    )
    done = _run(tmp_path, daemon_install_lines(SERVED_BUNDLE, api_url=API))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "installed version=9.9.9" in done.stdout
    assert not (tmp_path / "curl.log").exists()


@needs_bash
def test_a_bundle_whose_digest_does_not_match_is_refused(tmp_path: Path) -> None:
    _serve(tmp_path, TARGET, _bundle(), digest="0" * 64)
    done = _run(tmp_path, daemon_install_lines(SERVED_BUNDLE, api_url=API))
    assert done.returncode != 0
    assert "sha256 mismatch; refusing to install" in done.stdout + done.stderr
    assert not (tmp_path / "box" / "alkera.dist").exists()


@needs_bash
def test_a_backend_that_refuses_the_node_stops_the_boot(tmp_path: Path) -> None:
    (tmp_path / "serve").mkdir()
    done = _run(tmp_path, daemon_install_lines(SERVED_BUNDLE, api_url=API))
    assert done.returncode != 0
    assert "the backend served no node bundle" in done.stdout + done.stderr


@needs_bash
def test_the_release_download_still_installs_from_the_release_host(tmp_path: Path) -> None:
    name = f"alkera-{TARGET}.tar.gz"
    _serve(tmp_path, name, _bundle())
    done = _run(
        tmp_path,
        daemon_install_lines(RELEASE_DOWNLOAD, api_url=API),
        preamble="RELEASE_BASE=https://releases.example\nDAEMON_VERSION=1.2.3",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "installed version=1.2.3" in done.stdout
    log = (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert f"url=https://releases.example/cli/v1.2.3/{TARGET}/{name}" in log
    assert "header=" not in log


@needs_bash
@pytest.mark.parametrize(
    "version",
    [pytest.param("", id="none-resolved"), pytest.param("1.2.3;rm -rf /", id="not-a-label")],
)
def test_the_release_download_installs_only_a_version_the_backend_resolved(
    tmp_path: Path, version: str
) -> None:
    """The backend resolves the release before it renders the script; the box
    never falls back to a channel or the stable pointer of its own."""
    _serve(tmp_path, f"alkera-{TARGET}.tar.gz", _bundle())
    done = _run(
        tmp_path,
        daemon_install_lines(RELEASE_DOWNLOAD, api_url=API),
        preamble=f"RELEASE_BASE=https://releases.example\nDAEMON_VERSION='{version}'",
    )
    assert done.returncode != 0
    assert "refusing daemon version" in done.stdout + done.stderr
    assert not (tmp_path / "curl.log").exists()


SSH_SPEC = BootstrapSpec(
    provider="ssh",
    allocation_id="a1",
    machine_name="m",
    type_code="host",
    tenancy="dedicated",
    api_url=API,
    release_base_url="",
    sandbox_mode="none",
)
SOURCE_PLAN = BootstrapPlan(version=SOURCE_VERSION, start_mode=StartMode.SUPERVISE)


def test_a_served_bootstrap_needs_no_release_host_and_names_none() -> None:
    script = render_bootstrap(SSH_SPEC, SOURCE_PLAN, source=SERVED_BUNDLE)
    assert f"{API}" in script and BUNDLE_ROUTE in script
    assert "cli/stable.json" not in script and "cli/nodes/" not in script
    assert CREDENTIAL not in script
