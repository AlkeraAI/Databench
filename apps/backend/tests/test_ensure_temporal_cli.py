"""The Temporal CLI resolver hands ONE env line to TWO consumers, and both must
read the same path from it.

``ops/scripts/ensure-temporal-cli.sh`` prints ``ALKERA_TEMPORAL_BIN=<path>``.
CI appends that line to ``$GITHUB_ENV`` (a literal ``name=value``); the
Makefile's ``test-py`` ``eval``s it (shell re-parses the value); and on the
Windows shards BOTH happen in sequence -- the CI export lands in the
environment, then ``make test-py`` runs the resolver again, which honours the
exported value as its override and re-emits it through ``eval``. A value one
consumer mangles is a dev server nobody can start, and it fails only on the
shard that runs under Git Bash, hours into the gate.

These tests execute the real script against a fake ``temporal`` and, for the
MSYS shape, a fake ``cygpath`` that models the two conversions Git Bash offers
(``-m`` mixed ``C:/…``, ``-w`` Windows ``C:\\…``), and drive the line through
each consumer exactly as the Makefile and the workflow spell it. They run on
POSIX only: on a Windows runner the real ``cygpath`` cannot be taken off the
PATH to prove the POSIX shape, and ``shutil.which("bash")`` may find the WSL
launcher -- the point is that both shapes are proven on every PR, on Linux.

The second half covers what the resolver *installs*. Its third tier downloads a
release archive over the network and hands back an executable this repo then
runs, so the bytes are pinned by SHA-256 and verified before anything is
unpacked or made executable. Those tests drive the download branch with a fake
``curl`` that serves prepared bytes -- so a tampered archive, a release with no
recorded digest, and the honest case are each exercised against the real script,
with no network and nothing installed on the host outside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "ensure-temporal-cli.sh"
MAKEFILE = REPO_ROOT / "Makefile"
PR_GATE = REPO_ROOT / ".github" / "workflows" / "pr-gate.yml"

#: A version the fake binary reports and the script is told to expect, so the
#: cached-download branch's version check passes on the fake.
VERSION = "9.9.9"
ENV_LINE = re.compile(r"^ALKERA_TEMPORAL_BIN=(.*)$")
FAKE_TEMPORAL = f'#!/bin/sh\necho "temporal version {VERSION} (Server 1.0.0, UI 1.0.0)"\n'
#: Models MSYS cygpath for the two forms the resolver may use. A POSIX path is
#: mapped onto drive C, the way Git Bash maps its root.
FAKE_CYGPATH = r"""#!/bin/sh
mode="$1"; p="$2"
case "$p" in /*) p="C:$p" ;; esac
case "$mode" in
  -m) printf '%s\n' "$p" | tr '\\' '/' ;;
  -w) printf '%s\n' "$p" | tr '/' '\\' ;;
  *) echo "fake cygpath: unsupported $mode" >&2; exit 2 ;;
esac
"""
#: The Makefile's consumer, verbatim minus make's `$$` escaping.
MAKEFILE_CONSUMER = (
    'eval "$$(bash ops/scripts/ensure-temporal-cli.sh)" && export ALKERA_TEMPORAL_BIN'
)
#: CI's consumer, verbatim. The private gate runs the open copy of the script.
CI_CONSUMER = 'bash Databench/ops/scripts/ensure-temporal-cli.sh >> "$GITHUB_ENV"'

SHAPES = ("posix", "msys")
SOURCES = ("override", "on-path", "cached")

pytestmark = [
    pytest.mark.skipif(
        not SCRIPT.is_file(), reason="ops scripts are not part of this distribution"
    ),
    pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None,
        reason="executes the resolver under bash with a fake cygpath (POSIX hosts only)",
    ),
]


def _mixed(path: str) -> str:
    """What the fake `cygpath -m` returns for ``path``."""
    return ("C:" + path if path.startswith("/") else path).replace("\\", "/")


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@dataclass
class Rig:
    tmp: Path
    cygpath_bin: Path
    path_bin: Path
    cache_root: Path
    override: Path
    base_path: str

    @property
    def cached(self) -> Path:
        return self.cache_root / "alkera" / "temporal-cli" / VERSION / "temporal"

    def expected(self, shape: str, source: str) -> str:
        raw = {
            "override": str(self.override),
            "on-path": str(self.path_bin / "temporal"),
            "cached": str(self.cached),
        }[source]
        return _mixed(raw) if shape == "msys" else raw

    def env(self, shape: str, source: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        entries = []
        if shape == "msys":
            entries.append(str(self.cygpath_bin))
        if source == "on-path":
            entries.append(str(self.path_bin))
        env = {
            "PATH": os.pathsep.join([*entries, self.base_path]),
            "HOME": str(self.tmp),
            "XDG_CACHE_HOME": str(self.cache_root),
            "TEMPORAL_CLI_VERSION": VERSION,
        }
        if source == "override":
            env["ALKERA_TEMPORAL_BIN"] = str(self.override)
        env.update(extra or {})
        return env

    def run(self, shape: str, source: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.env(shape, source, extra),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def via_eval(self, shape: str, source: str, **extra: str) -> str:
        """The Makefile's consumer: eval the line, export, read the variable back."""
        result = subprocess.run(
            [
                "bash",
                "-c",
                'eval "$(bash "$1")" && export ALKERA_TEMPORAL_BIN && '
                'printf "%s" "$ALKERA_TEMPORAL_BIN"',
                "_",
                str(SCRIPT),
            ],
            env=self.env(shape, source, extra),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout

    def via_github_env(self, shape: str, source: str, **extra: str) -> dict[str, str]:
        """CI's consumer: append the line to $GITHUB_ENV, then read the file the
        way the runner does -- one `name=value` per line, the value literal."""
        github_env = self.tmp / f"github-env-{shape}-{source}"
        github_env.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", 'bash "$1" >> "$GITHUB_ENV"', "_", str(SCRIPT)],
            env=self.env(shape, source, {"GITHUB_ENV": str(github_env), **extra}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        exported: dict[str, str] = {}
        for line in github_env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            exported[name] = value
        return exported


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    # A real `temporal` anywhere on the host PATH would short-circuit the
    # override and cached branches; keep only the directories without one.
    base_path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not (Path(entry) / "temporal").exists()
    )
    cygpath_bin = tmp_path / "cygpath-bin"
    _executable(cygpath_bin / "cygpath", FAKE_CYGPATH)
    path_bin = tmp_path / "path-bin"
    _executable(path_bin / "temporal", FAKE_TEMPORAL)
    cache_root = tmp_path / "cache"
    rig = Rig(
        tmp=tmp_path,
        cygpath_bin=cygpath_bin,
        path_bin=path_bin,
        cache_root=cache_root,
        override=_executable(tmp_path / "own-build" / "temporal", FAKE_TEMPORAL),
        base_path=base_path,
    )
    _executable(rig.cached, FAKE_TEMPORAL)
    return rig


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("shape", SHAPES)
def test_both_consumers_read_the_same_path(rig: Rig, shape: str, source: str) -> None:
    """One line, one value, whichever way the binary was found and whichever
    shell reads it. Under MSYS that value is the mixed spelling -- Python on
    Windows opens `C:/…`, and it has no backslash for eval to eat."""
    expected = rig.expected(shape, source)
    result = rig.run(shape, source)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    match = ENV_LINE.match(lines[0])
    assert match, lines[0]
    assert match.group(1) == expected
    assert rig.via_eval(shape, source) == expected
    assert rig.via_github_env(shape, source) == {"ALKERA_TEMPORAL_BIN": expected}
    if shape == "msys":
        assert expected.startswith("C:/") and "\\" not in expected
    else:
        assert Path(expected).is_file(), "the POSIX value must be a path the interpreter opens"


@pytest.mark.parametrize("shape", SHAPES)
def test_the_ci_export_survives_the_makefile_eval(rig: Rig, shape: str) -> None:
    """The chain the Windows shards run: CI appends the resolver's line to
    $GITHUB_ENV, the runner exports it, and `make test-py` runs the resolver
    again through eval with that export in place (the resolver honours it as
    the override). The value must come out of the second hop unchanged."""
    first = rig.via_github_env(shape, "cached")["ALKERA_TEMPORAL_BIN"]
    second = rig.via_eval(shape, "cached", ALKERA_TEMPORAL_BIN=first)
    assert second == first, (first, second)


@pytest.mark.parametrize(
    "character",
    [
        pytest.param(" ", id="space"),
        pytest.param("$", id="dollar"),
        pytest.param("'", id="single-quote"),
        pytest.param('"', id="double-quote"),
        pytest.param("\\", id="backslash"),
        pytest.param("`", id="backtick"),
        pytest.param(";", id="semicolon"),
        pytest.param("*", id="glob"),
    ],
)
def test_a_path_the_shell_would_reinterpret_is_refused(
    tmp_path: Path, rig: Rig, character: str
) -> None:
    """No quoting satisfies both consumers, so such a path is refused before
    anything reaches stdout -- with the two knobs that move the binary."""
    cache_root = tmp_path / f"ca{character}che"
    _executable(cache_root / "alkera" / "temporal-cli" / VERSION / "temporal", FAKE_TEMPORAL)
    result = rig.run("posix", "cached", XDG_CACHE_HOME=str(cache_root))
    assert result.returncode != 0
    assert result.stdout == "", "nothing may be emitted for a path eval would mangle"
    assert "refusing" in result.stderr
    assert "XDG_CACHE_HOME" in result.stderr and "ALKERA_TEMPORAL_BIN" in result.stderr


def test_progress_never_reaches_stdout(rig: Rig) -> None:
    result = rig.run("posix", "cached")
    assert result.returncode == 0, result.stderr
    assert "[ensure-temporal-cli]" not in result.stdout
    assert "[ensure-temporal-cli]" in result.stderr


def test_the_two_consumers_are_the_ones_the_repo_runs() -> None:
    """The shapes exercised above are the Makefile's and the workflow's exact
    spellings; the Windows shards are where both meet, under Git Bash."""
    assert MAKEFILE_CONSUMER in MAKEFILE.read_text(encoding="utf-8")
    jobs = yaml.safe_load(PR_GATE.read_text(encoding="utf-8"))["jobs"]
    for job in ("test-linux", "test-windows"):
        runs = [str(step.get("run", "")).strip() for step in jobs[job]["steps"]]
        assert CI_CONSUMER in runs, (job, runs)
    assert jobs["test-windows"]["defaults"]["run"]["shell"] == "bash"
    assert "make test-py" in " ".join(
        str(step.get("run", "")) for step in jobs["test-windows"]["steps"]
    )


# --------------------------------------------------------------------------
# the download tier: pinned bytes, verified before anything becomes executable
# --------------------------------------------------------------------------

#: The version the script downloads when nothing overrides it -- the one whose
#: digests are recorded in the script.
PINNED_VERSION = "1.8.3"
#: `<version>/<os>/<arch>) echo <sha256> ;;` -- the recorded pins, read back out
#: of the shipped script.
PIN_LINE = re.compile(r"^\s*([\w.]+)/(\w+)/(\w+)\)\s*echo\s+([0-9a-f]{64})\s*;;", re.M)
#: A `curl` that never reaches the network: it writes prepared bytes to the -o
#: target and records that it was called.
FAKE_CURL = r"""#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "$out" >> "$FAKE_CURL_LOG"
cat "$FAKE_CURL_PAYLOAD" > "$out"
"""


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _recorded_pins() -> dict[tuple[str, str, str], str]:
    return {
        (version, os_name, arch): digest
        for version, os_name, arch, digest in PIN_LINE.findall(_script_text())
    }


#: A fake binary that reports the version the script downloads, so the honest
#: case clears the post-install version check the way the real release does.
FAKE_PINNED_TEMPORAL = (
    f'#!/bin/sh\necho "temporal version {PINNED_VERSION} (Server 1.0.0, UI 1.0.0)"\n'
)


def _archive(tmp_path: Path, name: str, body: str = FAKE_PINNED_TEMPORAL) -> Path:
    """A tar.gz carrying one executable `temporal`, the shape the script unpacks."""
    path = tmp_path / name
    member = tarfile.TarInfo("temporal")
    payload = body.encode()
    member.size = len(payload)
    member.mode = 0o755
    with tarfile.open(path, "w:gz") as tar:
        tar.addfile(member, io.BytesIO(payload))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class DownloadRig:
    """Drives the resolver's download tier with a fake curl and an empty cache."""

    tmp: Path
    cache_root: Path
    curl_bin: Path
    log: Path
    payload: Path
    base_path: str

    @property
    def cached(self) -> Path:
        return self.cache_root / "alkera" / "temporal-cli" / PINNED_VERSION / "temporal"

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.pathsep.join([str(self.curl_bin), self.base_path]),
            "HOME": str(self.tmp),
            "XDG_CACHE_HOME": str(self.cache_root),
            "FAKE_CURL_LOG": str(self.log),
            "FAKE_CURL_PAYLOAD": str(self.payload),
        }
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )

    @property
    def downloads(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").split() if self.log.exists() else []


@pytest.fixture
def download(tmp_path: Path, rig: Rig) -> DownloadRig:
    curl_bin = tmp_path / "curl-bin"
    _executable(curl_bin / "curl", FAKE_CURL)
    log = tmp_path / "curl.log"
    return DownloadRig(
        tmp=tmp_path,
        cache_root=tmp_path / "download-cache",
        curl_bin=curl_bin,
        log=log,
        payload=_archive(tmp_path, "payload.tar.gz"),
        # `rig`'s base PATH already has every directory holding a real `temporal`
        # stripped, so tier 2 cannot short-circuit the download.
        base_path=rig.base_path,
    )


def test_a_tampered_archive_is_refused_and_nothing_is_installed(download: DownloadRig) -> None:
    """The download is an executable the suite then runs, so bytes that do not
    match the recorded digest must never be unpacked, never land in the cache and
    never be made executable -- the refusal leaves the host as it was."""
    result = download.run()
    assert download.downloads, "the script never reached the download tier"
    assert result.returncode != 0
    assert result.stdout == "", "nothing may be emitted for an archive that failed verification"
    assert "sha256" in result.stderr and "refusing" in result.stderr
    assert _sha256(download.payload) in result.stderr, "the actual digest must be reported"
    assert not download.cached.exists(), "a rejected archive was unpacked into the cache"
    assert not download.cached.parent.exists()


def test_an_archive_matching_the_expected_digest_is_installed(download: DownloadRig) -> None:
    """The honest case through the same branch: verification passes, the binary is
    unpacked, made executable and handed out on the one env line."""
    result = download.run(TEMPORAL_CLI_SHA256=_sha256(download.payload))
    assert result.returncode == 0, result.stderr
    assert download.downloads, "the script never reached the download tier"
    match = ENV_LINE.match(result.stdout.strip())
    assert match and match.group(1) == str(download.cached), result.stdout
    assert os.access(download.cached, os.X_OK)


def test_a_version_with_no_recorded_digest_is_refused_before_the_download(
    download: DownloadRig,
) -> None:
    """No digest, no download: an unpinned release is refused rather than fetched
    and trusted, so there is no path that installs unverified bytes."""
    result = download.run(TEMPORAL_CLI_VERSION="99.99.99")
    assert result.returncode != 0
    assert result.stdout == ""
    assert download.downloads == [], "an unpinned version must not be fetched at all"
    assert "no pinned SHA-256" in result.stderr
    assert "99.99.99" in result.stderr


def test_every_platform_the_script_supports_has_a_recorded_digest() -> None:
    """A pin table missing the runner's platform is a resolver that refuses on
    that host only -- so the table must cover the whole cross product the script
    maps `uname` onto, for the version it downloads by default."""
    text = _script_text()
    assert f'TEMPORAL_CLI_VERSION="${{TEMPORAL_CLI_VERSION:-{PINNED_VERSION}}}"' in text
    systems = {"darwin", "linux", "windows"}
    machines = {"amd64", "arm64"}
    for name in systems:
        assert f'os="{name}"' in text, name
    for name in machines:
        assert f'arch="{name}"' in text, name
    pins = _recorded_pins()
    expected = {(PINNED_VERSION, o, a) for o in systems for a in machines}
    assert expected <= set(pins), sorted(expected - set(pins))
    assert len(set(pins.values())) == len(pins), "two platforms share a digest"


def test_the_verification_happens_before_the_archive_is_unpacked() -> None:
    """Order is the whole point: hashing after extraction would already have
    written attacker-chosen bytes into the cache."""
    text = _script_text()
    assert text.index("sha256_of") < text.index('mkdir -p "$dest_dir"')
    assert text.index("does not match the pinned") < text.index("chmod +x")


# --------------------------------------------------------------------------
# the Windows tier: a zip, on a host whose `tar` cannot read one
# --------------------------------------------------------------------------
#
# The Windows shards run this resolver under an MSYS shell, which ships neither
# bsdtar nor unzip and whose `tar` is GNU tar -- it refuses a zip outright, so a
# chain that ends at `tar` ends at a failed extraction hours into the gate. What
# every supported Windows image does have is bsdtar at System32\tar.exe (Windows
# Server 2019+ / Windows 10 1803+) and PowerShell's Expand-Archive.
#
# `uname` is faked so the real script takes its Windows branch on a POSIX host,
# and each unpacker is a fake that records that it was called, checks the
# arguments it was handed, and materialises the member -- so these cases pin
# WHICH tool is reached, in what order, and that the refusal names them all.

#: What the faked `uname` reports, so the resolver resolves os=windows/arch=amd64.
FAKE_UNAME = """#!/bin/sh
case "$1" in
  -s) echo "MINGW64_NT-10.0-19045" ;;
  -m) echo "x86_64" ;;
  *) echo "fake uname: unsupported $1" >&2; exit 2 ;;
esac
"""
#: `System32\\tar.exe` -- bsdtar. Reached by absolute path, never through PATH.
FAKE_SYSTEM32_TAR = """#!/bin/sh
echo "system32-tar" >> "$FAKE_EXTRACT_LOG"
[ "$1" = "-xf" ] || { echo "system32 tar: expected -xf, got '$1'" >&2; exit 2; }
[ -f "$2" ] || { echo "system32 tar: no archive at '$2'" >&2; exit 2; }
[ "$3" = "-C" ] || { echo "system32 tar: expected -C, got '$3'" >&2; exit 2; }
[ -d "$4" ] || { echo "system32 tar: no destination at '$4'" >&2; exit 2; }
[ -n "$5" ] || { echo "system32 tar: no member named" >&2; exit 2; }
cp "$FAKE_MEMBER_BODY" "$4/$5"
"""
FAKE_UNZIP = """#!/bin/sh
echo "unzip" >> "$FAKE_EXTRACT_LOG"
archive=""; member=""; dest=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-q) shift ;;
    -d) dest="$2"; shift 2 ;;
    *) if [ -z "$archive" ]; then archive="$1"; else member="$1"; fi; shift ;;
  esac
done
[ -f "$archive" ] || { echo "unzip: no archive at '$archive'" >&2; exit 2; }
[ -n "$member" ] || { echo "unzip: no member named" >&2; exit 2; }
[ -d "$dest" ] || { echo "unzip: no destination at '$dest'" >&2; exit 2; }
cp "$FAKE_MEMBER_BODY" "$dest/$member"
"""
FAKE_POWERSHELL = r"""#!/bin/sh
echo "powershell" >> "$FAKE_EXTRACT_LOG"
[ "$1" = "-NoProfile" ] || { echo "powershell: expected -NoProfile, got '$1'" >&2; exit 2; }
[ "$2" = "-Command" ] || { echo "powershell: expected -Command, got '$2'" >&2; exit 2; }
case "$3" in *Expand-Archive*) ;; *) echo "powershell: not Expand-Archive: '$3'" >&2; exit 2 ;; esac
archive=$(printf '%s\n' "$3" | sed -n "s/.*-LiteralPath '\([^']*\)'.*/\1/p")
dest=$(printf '%s\n' "$3" | sed -n "s/.*-DestinationPath '\([^']*\)'.*/\1/p")
[ -f "$archive" ] || { echo "powershell: no archive at '$archive'" >&2; exit 2; }
[ -d "$dest" ] || { echo "powershell: no destination at '$dest'" >&2; exit 2; }
cp "$FAKE_MEMBER_BODY" "$dest/temporal.exe"
"""
#: GNU tar, as the MSYS shell provides it: it records the call and refuses the zip.
#: Reaching it at all is the defect these cases exist to catch.
FAKE_GNU_TAR = """#!/bin/sh
echo "gnu-tar" >> "$FAKE_EXTRACT_LOG"
echo "tar: This does not look like a tar archive" >&2
exit 2
"""
#: What the script needs from a real host while its PATH is cut down to nothing
#: else -- so a `unzip` the developer's machine happens to ship cannot answer a
#: case that is about a host without one.
_MINIMAL_TOOLS = (
    "mktemp",
    "mkdir",
    "chmod",
    "rm",
    "cp",
    "cat",
    "head",
    "cut",
    "sed",
    "sha256sum",
    "shasum",
    "openssl",
)


def _minimal_bin(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for tool in _MINIMAL_TOOLS:
        found = shutil.which(tool)
        if found and not (directory / tool).exists():
            (directory / tool).symlink_to(found)
    return directory


def _windows_zip(path: Path, body: str) -> Path:
    """A real zip carrying `temporal.exe`, the shape the release publishes."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("temporal.exe", body)
    return path


@dataclass
class WindowsRig:
    """Drives the resolver's Windows download tier with a faked `uname`, a faked
    `curl`, and whichever unpackers a case chooses to make available."""

    tmp: Path
    cache_root: Path
    fake_bin: Path
    windows_root: Path
    extract_log: Path
    payload: Path
    member_body: Path
    minimal: Path

    @property
    def cached(self) -> Path:
        return self.cache_root / "alkera" / "temporal-cli" / PINNED_VERSION / "temporal.exe"

    @property
    def system32_tar(self) -> Path:
        return self.windows_root / "System32" / "tar.exe"

    def run(
        self, *, system32_tar: bool, unzip: bool, powershell: bool
    ) -> subprocess.CompletedProcess[str]:
        if system32_tar:
            _executable(self.system32_tar, FAKE_SYSTEM32_TAR)
        if unzip:
            _executable(self.fake_bin / "unzip", FAKE_UNZIP)
        if powershell:
            _executable(self.fake_bin / "powershell", FAKE_POWERSHELL)
        env = {
            "PATH": os.pathsep.join([str(self.fake_bin), str(self.minimal)]),
            "HOME": str(self.tmp),
            "XDG_CACHE_HOME": str(self.cache_root),
            "SYSTEMROOT": str(self.windows_root),
            "FAKE_CURL_LOG": str(self.tmp / "curl.log"),
            "FAKE_CURL_PAYLOAD": str(self.payload),
            "FAKE_EXTRACT_LOG": str(self.extract_log),
            "FAKE_MEMBER_BODY": str(self.member_body),
            # The fake payload is not the published archive, so name its digest the
            # way a version bump does; the pin table is covered by its own case.
            "TEMPORAL_CLI_SHA256": _sha256(self.payload),
        }
        # The PATH above is the host shape under test, so it holds no interpreter:
        # name bash where this host keeps it.
        return subprocess.run(
            [str(shutil.which("bash")), str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def unpackers(self) -> list[str]:
        """Every unpacker that was invoked, in order."""
        if not self.extract_log.exists():
            return []
        return self.extract_log.read_text(encoding="utf-8").split()


@pytest.fixture
def windows(tmp_path: Path) -> WindowsRig:
    fake_bin = tmp_path / "windows-fake-bin"
    _executable(fake_bin / "uname", FAKE_UNAME)
    _executable(fake_bin / "curl", FAKE_CURL)
    _executable(fake_bin / "tar", FAKE_GNU_TAR)
    member_body = tmp_path / "temporal.exe.body"
    member_body.write_text(FAKE_PINNED_TEMPORAL, encoding="utf-8")
    return WindowsRig(
        tmp=tmp_path,
        cache_root=tmp_path / "windows-cache",
        fake_bin=fake_bin,
        windows_root=tmp_path / "Windows",
        extract_log=tmp_path / "extract.log",
        payload=_windows_zip(tmp_path / "temporal.zip", FAKE_PINNED_TEMPORAL),
        member_body=member_body,
        minimal=_minimal_bin(tmp_path / "minimal-bin"),
    )


def _assert_installed(rig: WindowsRig, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    match = ENV_LINE.match(result.stdout.strip())
    assert match and match.group(1) == str(rig.cached), result.stdout
    assert os.access(rig.cached, os.X_OK), "the unpacked binary was never made executable"


def test_the_windows_zip_goes_to_system32_bsdtar_before_anything_on_path(
    windows: WindowsRig,
) -> None:
    """PATH order on Windows is cmd's and PowerShell's, not the MSYS shell's, so
    the one unpacker every supported image is known to have is reached by its
    absolute path -- ahead of an unzip that may or may not be installed."""
    result = windows.run(system32_tar=True, unzip=True, powershell=True)
    _assert_installed(windows, result)
    assert windows.unpackers == ["system32-tar"], windows.unpackers


def test_the_windows_zip_falls_back_to_unzip_when_system32_has_no_tar(
    windows: WindowsRig,
) -> None:
    result = windows.run(system32_tar=False, unzip=True, powershell=True)
    _assert_installed(windows, result)
    assert windows.unpackers == ["unzip"], windows.unpackers


def test_the_windows_zip_falls_back_to_powershell_expand_archive(windows: WindowsRig) -> None:
    """The last rung: no bsdtar, no unzip. GNU tar is on PATH and must not be the
    answer -- it cannot read a zip, so reaching it is a failed install."""
    result = windows.run(system32_tar=False, unzip=False, powershell=True)
    _assert_installed(windows, result)
    assert windows.unpackers == ["powershell"], windows.unpackers


def test_a_windows_host_with_no_zip_reader_refuses_and_names_what_it_tried(
    windows: WindowsRig,
) -> None:
    """No silent fall-through to a tar that will fail on the archive: the refusal
    says which three tools were looked for, so the operator can install one."""
    result = windows.run(system32_tar=False, unzip=False, powershell=False)
    assert result.returncode != 0
    assert result.stdout == "", "nothing may be emitted when the archive was never unpacked"
    assert "no zip reader" in result.stderr
    assert str(windows.system32_tar) in result.stderr
    assert "unzip" in result.stderr and "Expand-Archive" in result.stderr
    assert windows.unpackers == [], windows.unpackers
    assert not windows.cached.exists()


@pytest.mark.parametrize(
    ("system32_tar", "unzip", "powershell"),
    [
        pytest.param(True, True, True, id="every-tool"),
        pytest.param(False, True, True, id="no-system32-tar"),
        pytest.param(False, False, True, id="powershell-only"),
        pytest.param(False, False, False, id="nothing"),
    ],
)
def test_gnu_tar_is_never_handed_the_zip(
    windows: WindowsRig, system32_tar: bool, unzip: bool, powershell: bool
) -> None:
    """The defect this tier exists for: `tar` on the MSYS shell's PATH is GNU tar,
    which refuses a zip, so it must not appear in the chain under any host shape."""
    windows.run(system32_tar=system32_tar, unzip=unzip, powershell=powershell)
    assert "gnu-tar" not in windows.unpackers, windows.unpackers


def test_a_host_without_systemroot_still_looks_where_windows_keeps_bsdtar() -> None:
    """`SYSTEMROOT` is normally set, but the resolver must not depend on it: with
    neither spelling exported it falls back to the standard location, and a
    backslashed value is normalised rather than passed to exec as-is."""
    text = _script_text()
    assert 'windows_root="${SYSTEMROOT:-${SystemRoot:-C:/Windows}}"' in text
    assert 'system32_tar="${windows_root//\\\\//}/System32/tar.exe"' in text
