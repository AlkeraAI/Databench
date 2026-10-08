"""An open install deletes accounts and keeps no personal data export.

Runs in a fresh interpreter with only the open compositions: the open backend's
extensions (``backend.open_product``) and the open worker with nothing
installed. The suite's own app installed the product already, and a point
freezes once read. The open platform keeps the account lifecycle's deletion
routes, its sweep and the erasure; it serves no export route, names no export
job, schedules no export sweep and gives the erasure nothing to delete from the
archive store.
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

pytestmark = pytest.mark.xdist_group("open_account_lifecycle")

PROBE = """
import json

from backend import open_product

open_product.install()

from backend.app_factory import create_app

app = create_app()

from alkera_core.account.contributors import contributors
from alkera_core.temporal import QUEUE_FOR
from worker.schedules import catalog
from worker.temporal import queues

routes = sorted(
    f"{method} {route.path}"
    for route in app.routes
    for method in sorted(getattr(route, "methods", None) or ())
    if "/account" in route.path
)
print("REPORT" + json.dumps({
    "routes": routes,
    "schemas": sorted(app.openapi()["components"]["schemas"]),
    "workflows": sorted(queues.workflow_type_name(w) for w in queues.ALL_WORKFLOWS),
    "activities": sorted(queues.activity_type_name(a) for a in queues.ALL_ACTIVITIES),
    "queues": sorted(QUEUE_FOR),
    "schedules": sorted(entry.id for entry in catalog()),
    "contributors": sorted(c.name for c in contributors()),
}))
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    home = tmp_path_factory.mktemp("home")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(PROBE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(home)},
        cwd=Path(__file__).parent,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    parsed: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return parsed


def test_the_open_backend_serves_deletion_and_no_export_route(report: dict[str, Any]) -> None:
    assert "POST /api/v1/me/account/deletion" in report["routes"]
    assert "GET /admin/v1/users/{user_id}/account" in report["routes"]
    assert [route for route in report["routes"] if "export" in route] == []
    assert {"ExportRead", "ExportListRead"}.isdisjoint(report["schemas"])


def test_the_open_worker_runs_the_lifecycle_sweep_and_no_export_job(
    report: dict[str, Any],
) -> None:
    assert "account.lifecycle_sweep" in report["workflows"]
    assert "account-lifecycle-sweep" in report["schedules"]
    for names in (report["workflows"], report["activities"], report["queues"]):
        assert [name for name in names if name.startswith("account.export")] == []
    assert [entry for entry in report["schedules"] if "export" in entry] == []


def test_the_open_erasure_has_no_archive_to_delete(report: dict[str, Any]) -> None:
    assert report["contributors"] == []
