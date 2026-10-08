"""The backend emits exactly one entitlements.status line at app creation —
the operator-facing signal that a grant is present/valid/expiring."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core import entitlements as ent


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append(("warning", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.calls.append(("error", event, kw))


def test_create_app_emits_one_entitlements_status_line(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(ent, "log", recorder)
    ent.get_entitlements.cache_clear()

    from backend.app_factory import create_app

    create_app()

    status = [(lvl, kw) for lvl, event, kw in recorder.calls if event == "entitlements.status"]
    assert len(status) == 1
    _level, kw = status[0]
    assert kw["component"] == "backend"
    assert kw["state"] in ("valid", "grace", "expired", "invalid", "absent")
