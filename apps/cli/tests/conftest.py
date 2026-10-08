"""Shared fixtures for the CLI test suite.

The default suite must be free + deterministic, so any test that touches the
context KB uses the deterministic ``FakeEmbedding`` (no model download). Tests
that genuinely need the real local model opt in with the ``context_embed_eval``
marker and resolve it themselves.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest


@pytest.fixture
def sigchld_restored() -> Iterator[None]:
    """For a test that changes ``SIGCHLD``: the process leaves with the kernel's
    disposition it had, a handler set in C included (see ``_helpers.sigchld``)."""
    from _helpers.sigchld import sigchld_preserved

    with sigchld_preserved():
        yield


@pytest.fixture
def require_effective_chmod(tmp_path: Path) -> None:
    """Skip when tmp_path's filesystem doesn't honor chmod mode bits — e.g.
    WSL DrvFs (/mnt/c) without the metadata mount option, where every file
    stats as 0o777. The product state these tests model (ALKERA_HOME, mode-bit
    executability) lives on a POSIX filesystem even under WSL; only workspace
    files land on DrvFs."""
    probe = tmp_path / ".chmod-probe"
    probe.write_text("x")
    probe.chmod(0o600)
    if (probe.stat().st_mode & 0o777) != 0o600:
        pytest.skip("filesystem does not honor chmod mode bits (WSL DrvFs?)")


@pytest.fixture
def require_effective_utime(tmp_path: Path) -> None:
    """Skip when tmp_path's filesystem doesn't honor os.utime back-dating —
    observed on WSL DrvFs (/mnt/c), where 9P silently ignores it. Age-based
    logic under test is meaningless there."""
    probe = tmp_path / ".utime-probe"
    probe.write_text("x")
    os.utime(probe, (1_000_000.0, 1_000_000.0))
    if abs(probe.stat().st_mtime - 1_000_000.0) > 1.0:
        pytest.skip("filesystem does not honor utime back-dating (WSL DrvFs?)")


@pytest.fixture
def spawn_pinned_exe() -> Iterator[Callable[[Path], subprocess.Popen[bytes]]]:
    """Windows-only helper: copy a self-contained system exe to a path and
    start it, so the image is genuinely mapped — Windows then refuses to delete
    or replace the file, the state the aside-rename staging and the updater's
    fresh-dir installs exist for. Every spawned process is killed on teardown."""
    procs: list[subprocess.Popen[bytes]] = []

    def _spawn(dst: Path) -> subprocess.Popen[bytes]:
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        shutil.copyfile(system32 / "cmd.exe", dst)
        proc = subprocess.Popen(
            [str(dst), "/c", "pause"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    yield _spawn
    for proc in procs:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=30)


#: Env vars the plugins' connection discovery reads. A developer's real credentials
#: (.env.local, an exported shell var) must never leak into a test: discovery would
#: mint live vendor connections inside any test that enumerates it, so counts and
#: suggested-set assertions flake by machine. A test that wants discovery sets the
#: vars explicitly with monkeypatch, which composes after this scrub.
_VENDOR_DISCOVERY_ENV = (
    "HEX_API_TOKEN",
    "HEX_API_URL",
    "HEX_WORKSPACE",
    "SIGMA_BASE_URL",
    "SIGMA_CLIENT_ID",
    "SIGMA_CLIENT_SECRET",
    "SIGMA_ORG",
    "FIVETRAN_API_KEY",
    "FIVETRAN_API_SECRET",
    "FIVETRAN_API_URL",
    "TABLEAU_SERVER_URL",
    "TABLEAU_SITE",
    "TABLEAU_TOKEN_NAME",
    "TABLEAU_API_TOKEN",
    "LOOKERSDK_BASE_URL",
    "LOOKERSDK_CLIENT_ID",
    "LOOKERSDK_CLIENT_SECRET",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


@pytest.fixture(scope="session")
def _absent_aws_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory under the run's basetemp that is never created, for env vars that
    only have to name a file which does not exist. Deliberately NOT a ``tmp_path``:
    that fixture allocates a numbered directory per test, and allocating one scans the
    worker's whole (ever-growing) basetemp, mkdirs, and re-points a symlink — a cost
    every one of the CLI suite's thousands of tests would pay for a path nothing reads
    or writes."""
    return tmp_path_factory.getbasetemp() / "_scrubbed_aws"


@pytest.fixture(autouse=True)
def _scrub_vendor_discovery_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, _absent_aws_home: Path
) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    for var in _VENDOR_DISCOVERY_ENV:
        monkeypatch.delenv(var, raising=False)
    # Point the AWS config + SSO token cache at an empty tmp location. Unlike the vars
    # above, `~/.aws/config` is AMBIENT MACHINE STATE: it exists for anyone who has ever
    # run the AWS CLI, so without this the AWS plugin discovers the developer's own
    # profiles and self-activates inside unrelated tests — making activation assertions
    # pass or fail depending on whose laptop runs them. A test that wants AWS config
    # writes its own file and re-points these itself.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(_absent_aws_home / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(_absent_aws_home / "credentials"))


#: Markers whose tests authenticate against real external systems; those keep
#: the developer's real home (its `auth.yml`, its connector state).
_REAL_HOME_MARKERS = ("live", "slow_live")

#: One home per test per worker, numbered rather than a `tmp_path`: allocating
#: a numbered tmp dir scans the worker's whole basetemp, a cost every one of
#: the suite's thousands of tests would pay for a directory most never touch.
_HOME_SERIAL = itertools.count()


@pytest.fixture(autouse=True)
def _isolated_alkera_home(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Give every test its own Alkera home — the module attribute AND the env
    var, so an in-process read and a spawned child agree.

    Every default path the CLI derives from the home is resolved when it is
    needed (the audit spool, the daemon's scratch and log dirs, the Files
    mount and push state), so a test that never names a home would otherwise
    read and write the developer's real ``~/.alkera``. Under a parallel run
    that is one audit spool behind one lock for every worker: a worker that
    dies holding it stalls the delivery thread of the next, and a test that
    waits for that thread never ends. Pinned by ``test_home_seam.py``.

    The agent subprocess tests need the isolation for a second reason. The
    adapter derives the agent's config / cache / state roots from
    ``paths.ALKERA_HOME`` (``opencode_http.agent_config_root`` →
    ``<home>/harness/<session id>``) and DELETES that root when the process it
    spawned stops. Left alone the home is the developer's own ``~/.alkera`` and
    the test runners all name a CONSTANT session, so a suite run sixteen wide
    puts every harness-e2e test in ONE directory: each teardown then pulls the
    config root out from under the agents the other workers still have running.
    An agent whose config root vanishes fails its instance boot, and the first
    request the adapter makes after the spawn — ``GET /session`` — comes back
    500, killing a start that had nothing to do with the test that deleted the
    directory. A per-test home gives the isolation and the per-session
    uniqueness both.

    The ``live`` connector tests authenticate against real external systems
    and keep the developer's real home.
    """
    if any(request.node.get_closest_marker(marker) for marker in _REAL_HOME_MARKERS):
        yield
        return
    from alkera_cli.account import auth_file
    from alkera_cli.host import paths

    home = tmp_path_factory.getbasetemp() / "alkera-homes" / str(next(_HOME_SERIAL))
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setenv("ALKERA_HOME", str(home))
    # The sign-in file too: a chat binds the stored profile as it opens, so a
    # test that never signed in must find none rather than the developer's own.
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    for name in (auth_file.TOKEN_ENV, auth_file.ORG_ENV):
        monkeypatch.delenv(name, raising=False)
    auth_file.set_invocation_org(None)
    yield
    auth_file.set_invocation_org(None)


@pytest.fixture(autouse=True)
def _signed_out_gate() -> Iterator[None]:
    """Sign the permission gate out and clear the process-wide acting principal.
    Otherwise a test that starts the tool server resolves the machine's real
    login over authenticated HTTP and installs a principal that outlives the
    test. A test that wants a sign-in calls ``set_auth_source`` in its body,
    which runs after this setup and wins."""
    from alkera_cli.plugins.plugin_base.permissions.actor import (
        set_acting_principal,
        set_auth_source,
    )

    set_auth_source(lambda: None)
    set_acting_principal(None)
    yield
    set_acting_principal(None)
    set_auth_source(None)


@pytest.fixture(autouse=True)
def _fake_context_embedder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the context layer's embedder to the deterministic fake, except for
    ``context_embed_eval`` tests which exercise the real bge-small model."""
    # Run the column-parse pass SERIALLY in unit tests: a process pool would be slow to
    # spawn, can't see a monkeypatched ``parse_column_lineage``, and isn't needed at unit
    # scale. Tests that exercise parallelism pass ``max_workers=`` to run_upgrade_to_parsed
    # explicitly (it overrides the env). Production defaults to ~half the cores.
    monkeypatch.setenv("ALKERA_LINEAGE_PARSE_WORKERS", "1")
    if request.node.get_closest_marker("context_embed_eval") is not None:
        return
    monkeypatch.setenv("ALKERA_CONTEXT_EMBEDDING_MODEL", "fake")


@pytest.fixture(autouse=True)
def _stop_project_file_watchers() -> Iterator[None]:
    """End any project file watch a test left running.

    A daemon RPC that opens a project starts one, and the daemon ends it on shutdown — but a
    test that calls the method directly has no daemon to shut down, and the watch task keeps
    itself alive. Every running watch holds one of the forty worker threads anyio hands out per
    event loop, and this suite shares ONE loop for the whole session, so forty of them is every
    thread the loop will ever get: from then on anything that needs one waits, however unrelated
    it is. That is not a CLI-only bill — an xdist worker that ran these tests and then a backend
    route test found every request waiting on the thread FastAPI runs a synchronous dependency
    in, and a Files download module timed out on all sixteen of its tests.

    Untouched when the watcher module was never imported, which is most of this suite.
    """
    yield
    watcher_module = sys.modules.get("alkera_cli.harness.file_watcher")
    if watcher_module is not None:
        watcher_module.stop_all_watchers()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Live-tier cost lines recorded by warehouse-backed modules (each real
    run reports what it spent; absence means nothing live executed)."""
    lines = getattr(config, "_alkera_live_cost_lines", [])
    if lines:
        terminalreporter.section("live warehouse cost")
        for line in lines:
            terminalreporter.line(line)
