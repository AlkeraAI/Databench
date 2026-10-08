"""Tests for the daemon's `preferences.resolve_chat_defaults` method.

Drives the handler directly (the rule logic is covered in
`apps/cli/tests/test_chat_defaults.py`); here we pin the daemon wrapper: it persists a
correction only when the catalog is reachable, leaves the saved default untouched
on an outage / signed-out, and never rewrites a still-valid default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_cli.account import auth_file
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.methods import preferences as dp
from alkera_cli.daemon.methods.preferences import (
    ResolveChatDefaultsRequest,
    preferences_resolve_chat_defaults,
)
from alkera_cli.gateway.client import GatewayUnavailableError
from alkera_cli.host import paths

OPUS = GatewayModel(
    id="opus",
    display_name="Opus",
    wire="anthropic",
    efforts=("low", "medium", "high"),
    default_effort="high",
)
TEST_MODEL = GatewayModel(
    id="t1", display_name="Fixture", wire="anthropic", efforts=("low",), family="test"
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", home / "preferences.yml")
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", home / ".preferences.lock")
    return home


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, *, token: str | None, fetch: Any) -> None:
    if token is not None:
        auth_file.save_profile(
            auth_file.profile_from_token("http://api.test", token), make_current=True
        )
    monkeypatch.setattr(
        dp, "get_settings", lambda: type("S", (), {"alkera_gateway_url": "http://gw"})()
    )
    monkeypatch.setattr(dp, "fetch_models", fetch)


def _write_prefs(home: Path, **fields: Any) -> None:
    (home / "preferences.yml").write_text(yaml.safe_dump({"schema_version": "1.4.0", **fields}))


def _read_prefs(home: Path) -> dict[str, Any]:
    return yaml.safe_load((home / "preferences.yml").read_text())


async def _resolve() -> Any:
    return await preferences_resolve_chat_defaults(object(), ResolveChatDefaultsRequest())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_never_set_seeds_the_default_without_pinning_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader who never chose gets the default, and nothing is written: a
    stored copy would pin them there when the platform default moves."""
    home = _isolate(monkeypatch, tmp_path)
    _write_prefs(home, telemetry_enabled=False)
    before = _read_prefs(home)

    async def fetch(**_kw: Any) -> list[GatewayModel]:
        return [OPUS]

    _patch_gateway(monkeypatch, token="t", fetch=fetch)
    resp = await _resolve()
    assert resp.model == "opus"
    assert resp.effort == "high"  # catalog default
    assert _read_prefs(home) == before


@pytest.mark.asyncio
async def test_a_stale_pick_is_cleared_so_the_reader_follows_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _write_prefs(home, default_chat_model="retired", default_chat_effort="high")

    async def fetch(**_kw: Any) -> list[GatewayModel]:
        return [OPUS]

    _patch_gateway(monkeypatch, token="t", fetch=fetch)
    resp = await _resolve()
    assert (resp.model, resp.effort) == ("opus", "high")
    stored = _read_prefs(home)
    assert stored["default_chat_model"] is None
    assert stored["default_chat_effort"] is None


@pytest.mark.asyncio
async def test_excludes_test_family_when_initializing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)

    async def fetch(**_kw: Any) -> list[GatewayModel]:
        return [TEST_MODEL, OPUS]  # a test-family model LEADS the catalog

    _patch_gateway(monkeypatch, token="t", fetch=fetch)
    resp = await _resolve()
    assert resp.model == "opus"  # the test fixture is not selectable


@pytest.mark.asyncio
async def test_outage_keeps_saved_and_does_not_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _write_prefs(home, default_chat_model="saved-model", default_chat_effort="high")

    async def boom(**_kw: Any) -> list[GatewayModel]:
        raise GatewayUnavailableError("gateway down")

    _patch_gateway(monkeypatch, token="t", fetch=boom)
    resp = await _resolve()
    assert resp.model == "saved-model"  # NOT wiped by the outage
    assert resp.effort == "high"
    assert _read_prefs(home)["default_chat_model"] == "saved-model"  # untouched on disk


@pytest.mark.asyncio
async def test_signed_out_returns_saved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _write_prefs(home, default_chat_model="saved-model", default_chat_effort="medium")
    _patch_gateway(monkeypatch, token=None, fetch=None)
    resp = await _resolve()
    assert resp.model == "saved-model"
    assert resp.effort == "medium"


@pytest.mark.asyncio
async def test_valid_saved_default_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _write_prefs(home, default_chat_model="opus", default_chat_effort="low")

    async def fetch(**_kw: Any) -> list[GatewayModel]:
        return [OPUS]

    _patch_gateway(monkeypatch, token="t", fetch=fetch)
    resp = await _resolve()
    assert resp.model == "opus"
    assert resp.effort == "low"  # the user's valid choice wins over catalog default "high"
    assert _read_prefs(home)["default_chat_effort"] == "low"
