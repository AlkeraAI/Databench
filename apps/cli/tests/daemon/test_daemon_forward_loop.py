"""The daemon's event forwarder must not leave the editor hanging if its event
subscription dies: on an unexpected crash it emits a terminal `error` status so
the extension can stop the spinner + offer a retry."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_cli.daemon.methods.harness import _forward_loop


class _RecordingServer:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, Any]] = []

    async def notify(self, method: str, params: Any) -> None:
        self.notifications.append((method, params))


class _BoomSub:
    """An event subscription that raises (non-Cancelled) on first iteration."""

    def __aiter__(self) -> _BoomSub:
        return self

    async def __anext__(self) -> Any:
        raise RuntimeError("event bus exploded")


@pytest.mark.asyncio
async def test_forward_loop_emits_error_status_on_crash() -> None:
    server = _RecordingServer()
    # Must NOT raise — the loop swallows the crash after surfacing it.
    await _forward_loop(server, "sid-1", _BoomSub())

    assert server.notifications, "no terminal notification emitted on crash"
    method, params = server.notifications[-1]
    assert method == "harness.event"
    event = params.event  # HarnessEventNotification.event (a JSON dict)
    assert event["event_type"] == "session.status_changed"
    assert event["status"] == "error"
    assert event["session_id"] == "sid-1"
    assert "event stream lost" in event["detail"]
