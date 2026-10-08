"""A file a checkpoint push leaves behind is said once per folder, not per push."""

from __future__ import annotations

import logging

import pytest
from alkera_cli.cloud.left_behind import LeftBehind
from alkera_cli.files.push import PushSummary

NO_ROOM = "sparse-20g: not saved (the drive has no room for it)"


def _summary(*failed: str) -> PushSummary:
    return PushSummary(
        failed=list(failed),
        warnings=[f"{path}: not saved (the drive has no room for it)" for path in failed],
    )


def test_each_left_behind_file_is_said_once_with_why(caplog: pytest.LogCaptureFixture) -> None:
    notes = LeftBehind()
    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.folder"):
        notes.said("chat-a", _summary("sparse-20g"))
        notes.said("chat-a", _summary("sparse-20g"))
        notes.said("chat-a", _summary("sparse-20g", "huge.bin"))
        notes.said("chat-b", _summary("sparse-20g"))
    said = [r.getMessage() for r in caplog.records]
    assert said == [
        f"chat chat-a: {NO_ROOM}; it stays on this machine",
        "chat chat-a: huge.bin: not saved (the drive has no room for it); it stays on this machine",
        f"chat chat-b: {NO_ROOM}; it stays on this machine",
    ]
