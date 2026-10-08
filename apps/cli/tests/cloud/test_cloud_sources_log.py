"""A box says what it can read from when that CHANGES, not every fifteen seconds.

The poll tick rewrites the workspace source cards and used to announce the
result each time, so a demo box's log carried "workspace sources: planetscale,
tinybird" every 15 s — thousands of identical lines a day, burying everything
worth reading. The rewrite still happens on every tick (an admin's new
connection has to reach the agent without a restart); only the line is on
change.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from _mirror_service import SERVICE_LOGGER, Clock, build_service

#: The box's data plane as the service reads it; each test stands one in.
DATA_PLANE = "alkera_cli.cloud.service.box_data"


class _Sources:
    """A data plane whose source cards name ``connections``."""

    def __init__(self, *connections: str) -> None:
        self.connections = connections

    def refresh_sources(self, _project: object) -> tuple[str, ...]:
        return self.connections


def _said(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("workspace sources")
    ]


def test_the_source_list_is_announced_once_not_on_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, _ = build_service(tmp_path, clock=Clock())
    listed = _Sources("planetscale", "tinybird")
    monkeypatch.setattr(DATA_PLANE, lambda: listed)
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        for _ in range(4):  # four poll ticks, one minute of a demo box
            service.refresh_sources()
    assert _said(caplog) == ["workspace sources: planetscale, tinybird"]


def test_a_connection_the_admin_adds_or_removes_is_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, _ = build_service(tmp_path, clock=Clock())
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        for listed in (
            _Sources("planetscale"),
            _Sources("planetscale"),
            _Sources("planetscale", "tinybird"),
            _Sources("planetscale", "tinybird"),
            _Sources(),
        ):
            monkeypatch.setattr(DATA_PLANE, lambda doc=listed: doc)
            service.refresh_sources()
    assert _said(caplog) == [
        "workspace sources: planetscale",
        "workspace sources: planetscale, tinybird",
        "workspace sources: none",
    ]


def test_a_list_that_comes_back_is_announced_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The suppression is "same as the last line", not "said once ever" — a
    connection that drops and returns is news both times."""
    service, _ = build_service(tmp_path, clock=Clock())
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        for listed in (_Sources("tinybird"), _Sources(), _Sources("tinybird")):
            monkeypatch.setattr(DATA_PLANE, lambda doc=listed: doc)
            service.refresh_sources()
    assert _said(caplog) == [
        "workspace sources: tinybird",
        "workspace sources: none",
        "workspace sources: tinybird",
    ]
