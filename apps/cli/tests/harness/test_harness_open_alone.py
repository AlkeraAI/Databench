"""The open harness runs with nothing registered on its extension points, and
with the open platform's composition.

The probe runs in a fresh interpreter that never installs the product (the CLI
suite's conftest installs it in this one), so it sees the harness exactly as
the open platform ships it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.spread]

_PROBE = """
import asyncio, json, sys
from pathlib import Path

from alkera_core.extensions import installed_extensions
from alkera_core.project.directory import ProjectDirectory

from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness.extension_points import session_spend
from alkera_cli.harness.runtime_scheduling import protected_kinds

root = Path(sys.argv[1])
if sys.argv[2] == "open":
    from alkera_cli.app import open_product

    open_product.install()
project = ProjectDirectory(root / ".alkera")
runtime = HarnessRuntime(project)

armed = runtime.schedule_context_jobs()
runtime.ensure_context_runners()
scheduler = runtime.scheduler()
runtime.nudge_standing_jobs("files_changed")
asyncio.run(runtime.trigger_connection_refresh(set(), True))

report = {
    "installed": list(installed_extensions()),
    "armed": sorted(armed),
    "jobs": sorted({job.kind for job in scheduler.list_jobs()}),
    "job_ids": sorted(job.job_id for job in scheduler.list_jobs()),
    "runners": sorted(scheduler.registered_kinds()),
    "protected": sorted(protected_kinds()),
    "hints": runtime.lineage_hints(),
    "queries": session_spend(project, "chat-1").queries,
}
print("REPORT" + json.dumps(report))
"""

_PLATFORM_KINDS = ["alkera_cloud_sync", "blob_gc"]


def _probe(tmp_path: Path, composition: str) -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_PROBE), str(workspace), composition],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(tmp_path / "home"), "NO_COLOR": "1"},
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    report: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return report


def test_the_bare_harness_runs_only_the_platform_jobs(
    tmp_path: Path,
) -> None:
    report = _probe(tmp_path, "none")

    assert report["installed"] == []
    # Only the platform's own standing jobs are armed, bound and protected: no
    # knowledge base seed or prune, no credential sweep, no column drain.
    assert report["armed"] == _PLATFORM_KINDS
    assert report["runners"] == _PLATFORM_KINDS
    assert report["protected"] == _PLATFORM_KINDS
    # Cloud sync runs registered lanes, and with nothing installed there is none
    # to schedule.
    assert report["jobs"] == ["blob_gc"]
    # A repository edit brought nothing forward and armed nothing new.
    # No connection record: no pending connection to name, no spend to settle.
    assert report["hints"] == []
    assert report["queries"] == 0


def test_the_open_composition_schedules_the_connections_lane(
    tmp_path: Path,
) -> None:
    report = _probe(tmp_path, "open")

    assert "alkera.team-connections-sync" in report["installed"]
    # Still only the platform's own kinds: the open composition adds a lane to
    # cloud sync, not a data job.
    assert report["armed"] == _PLATFORM_KINDS
    assert report["runners"] == _PLATFORM_KINDS
    assert report["protected"] == _PLATFORM_KINDS
    assert report["jobs"] == _PLATFORM_KINDS
    assert "alkera_cloud_sync:connections" in report["job_ids"]
    assert report["hints"] == []
    assert report["queries"] == 0
