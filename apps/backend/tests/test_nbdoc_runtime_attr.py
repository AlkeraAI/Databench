"""The notebook service finds the realtime runtime on the application under
the name the realtime package keeps it at, spelled twice to keep the import
graph acyclic."""

from __future__ import annotations

from backend.services import realtime
from backend.services.notebooks import app as notebook_app


def test_the_notebook_service_reads_the_runtime_where_the_realtime_package_keeps_it() -> None:
    assert notebook_app.RUNTIME_ATTR == realtime.STATE_ATTR
