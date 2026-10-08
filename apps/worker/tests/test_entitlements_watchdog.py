"""The daily entitlements watchdog: self-skips when unconfigured, re-emits the
status line at the right level while a grant slides into grace and past it.

Drives the activity directly (awaited outside a Worker, it is a plain
coroutine); the workflow around it is covered in ``test_entitlements_workflow.py``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from alkera_core import entitlements as ent
from alkera_core.config import settings
from alkera_core.entitlements import Feature, generate_keypair, mint_entitlement_token
from freezegun import freeze_time
from worker.activities.entitlements import watchdog

PRIV, PUB = generate_keypair()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append(("warning", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.calls.append(("error", event, kw))


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    ent.get_entitlements.cache_clear()
    monkeypatch.setattr(settings, "alkera_entitlements", None)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", None)
    monkeypatch.setattr(settings, "app_env", "local")
    yield
    ent.get_entitlements.cache_clear()


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(ent, "log", recorder)
    return recorder


def _entitled(monkeypatch: pytest.MonkeyPatch, expires_on: date) -> None:
    token = mint_entitlement_token(
        customer="acme-corp",
        features=Feature.BYOK,
        expires_on=expires_on,
        serial=1,
        signing_key_b64=PRIV,
    )
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", PUB)
    ent.get_entitlements.cache_clear()


@pytest.mark.asyncio
async def test_watchdog_self_skips_when_no_entitlement(rec: _Recorder) -> None:
    result = await watchdog()
    assert result == {"skipped": "no entitlement configured"}
    assert rec.calls == []


@pytest.mark.asyncio
async def test_watchdog_reports_valid_grant(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    _entitled(monkeypatch, date(2026, 12, 31))
    with freeze_time("2026-07-01", real_asyncio=True):
        result = await watchdog()
    assert result["state"] == "valid"
    assert result["customer"] == "acme-corp"
    assert result["features"] == ["byok"]
    levels = [lvl for lvl, event, _ in rec.calls if event == "entitlements.status"]
    assert levels == ["info"]


@pytest.mark.asyncio
async def test_watchdog_warns_in_grace_and_errors_after(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    _entitled(monkeypatch, date(2026, 6, 30))
    with freeze_time("2026-07-10", real_asyncio=True):  # inside the 30-day grace window
        assert (await watchdog())["state"] == "grace"
    with freeze_time("2026-09-01", real_asyncio=True):  # well past grace
        assert (await watchdog())["state"] == "expired"
    levels = [lvl for lvl, event, _ in rec.calls if event == "entitlements.status"]
    assert levels == ["warning", "error"]
