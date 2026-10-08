"""Whether a replica starts sweeping for unsaved live file sessions.

A deployment always does: it is what writes back the edits of a process that
stopped between an edit and its write back. The test suite turns it off by
default (the root conftest), because a worker database holds every earlier
test's sessions and a whole-database sweep would write those back under the
tests running beside it.
"""

from __future__ import annotations

import pytest
from alkera_core.config import Settings, settings
from backend.authz import decide_on_record
from backend.services.realtime import runtime
from fastapi import FastAPI


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_runtime_sweeps_only_when_the_setting_says(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(settings, "realtime_crdt_unsaved_sweep_enabled", enabled)
    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    app = FastAPI()
    started = await runtime.start(app, decide=decide_on_record)
    try:
        assert started.crdt is not None
        assert started.crdt.sweeping is enabled
    finally:
        await runtime.stop(app, started)


def test_a_deployment_sweeps_by_default() -> None:
    assert Settings(_env_file=None).realtime_crdt_unsaved_sweep_enabled is True  # type: ignore[call-arg]


def test_the_suite_runs_without_a_whole_database_sweep() -> None:
    """Set by the root conftest before any app import."""
    assert settings.realtime_crdt_unsaved_sweep_enabled is False
