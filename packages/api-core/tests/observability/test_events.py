"""Analytics events: stable schema, id coercion, prop scrubbing, never raises.

We monkeypatch the module logger with a recorder rather than using
`structlog.testing.capture_logs` — the app configures structlog with
`cache_logger_on_first_use=True`, which makes `capture_logs` interception
order-dependent across a shared pytest process. A recorder is deterministic.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.observability import events
from alkera_core.observability.events import EventName, emit_event


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(events, "_events_log", recorder)
    return recorder


def test_emit_event_has_stable_analytics_schema(rec: _Recorder) -> None:
    emit_event(EventName.user_signed_up, user_id="u-1", org_id="o-1", plan="free")
    event, kw = rec.calls[0]
    assert event == "auth.signed_up"
    assert kw["kind"] == "analytics"
    assert kw["user_id"] == "u-1"
    assert kw["org_id"] == "o-1"
    assert kw["plan"] == "free"


def test_emit_event_coerces_uuid_ids_to_str(rec: _Recorder) -> None:
    uid = UUID("00000000-0000-0000-0000-000000000001")
    emit_event("custom.event", user_id=uid)
    event, kw = rec.calls[0]
    assert event == "custom.event"
    assert kw["user_id"] == str(uid)
    assert kw["org_id"] is None


def test_emit_event_scrubs_secret_props(rec: _Recorder) -> None:
    emit_event(EventName.cli_token_minted, user_id="u-1", token="super-secret", count=3)
    _event, kw = rec.calls[0]
    assert kw["token"] == "[redacted]"
    assert kw["count"] == 3


def test_emit_event_drops_reserved_prop_collisions(rec: _Recorder) -> None:
    emit_event(EventName.chat_started, user_id="u-1", kind="evil", event="evil")
    event, kw = rec.calls[0]
    assert kw["kind"] == "analytics"
    assert event == "chat.started"


def test_emit_event_never_raises(rec: _Recorder) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("logger down")

    rec.info = _boom  # type: ignore[method-assign]
    # Must swallow and fall back to the warning path, not propagate.
    emit_event(EventName.chat_completed, user_id="u-1")
    assert rec.calls[-1][0] == "analytics.emit_failed"


def test_event_names_are_dot_namespaced() -> None:
    for name in EventName:
        assert "." in name.value
