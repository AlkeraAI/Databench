"""Every default path under the Alkera home is resolved when it is needed, not
when its module is imported.

``alkera_cli.host.paths.ALKERA_HOME`` is the one seam that re-points the per-user
directory (a test's monkeypatch, or ``ALKERA_HOME`` in a spawned child's
environment). A module that binds the constant at import keeps the home it saw
first and ignores every later re-point: under a parallel test run each worker
then writes to the developer's real ``~/.alkera`` — one audit spool behind one
lock — and a worker's audit delivery waits on the lock a crashed worker held.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from alkera_cli.daemon.paths import daemon_log_dir, daemon_root
from alkera_cli.files.mount import mounts_dir
from alkera_cli.files.push import push_state_path
from alkera_cli.host import paths
from alkera_cli.observability.audit_report import AuditReporter
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionRecord


def _repoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "repointed-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    return home


@pytest.mark.parametrize(
    ("resolve", "expected"),
    [
        pytest.param(daemon_root, Path("daemon"), id="daemon-root"),
        pytest.param(daemon_log_dir, Path("logs"), id="daemon-logs"),
        pytest.param(mounts_dir, Path("files") / "mounts", id="files-mounts"),
    ],
)
def test_a_default_path_follows_the_home_it_is_resolved_under(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resolve: Callable[[], Path],
    expected: Path,
) -> None:
    home = _repoint(monkeypatch, tmp_path)
    assert resolve() == home / expected


def test_the_push_state_follows_the_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _repoint(monkeypatch, tmp_path)
    state = push_state_path(tmp_path / "tree", "org/dest")
    assert state.is_relative_to(home / "files" / "pushes")


def test_the_audit_spool_is_written_under_the_repointed_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _repoint(monkeypatch, tmp_path)
    reporter = AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: None,
        transport=httpx.MockTransport(lambda _request: httpx.Response(201, json={"accepted": 0})),
        start_worker=False,
    )
    reporter.on_decision_record(
        DecisionRecord(
            at=1_752_900_000.0,
            session_id="sess-1",
            request_id="req-1",
            source="harness",
            capability="fs",
            effect="write",
            operation="write README.md",
            raw=None,
            targets=["README.md"],
            mode="default",
            decision="reject",
            decided_by="rule",
            reasons=["disallowed by policy"],
        )
    )
    reporter.flush_now()
    reporter.close()
    assert (home / "audit" / "spool.jsonl").exists()
