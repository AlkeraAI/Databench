"""A headless run converges a stale saved default, as the TUI and the daemon do.

``resolve_chat_defaults`` says when a saved Default Chat Model + Effort no
longer resolves against the live catalog (``reset``), and every surface that
seeds a new chat from it must write the correction back so the stored value
converges. The daemon did; ``alkera run`` (the headless runner) resolved the
same defaults and never persisted, so every unattended run kept re-deriving the
fallback from a stale file. Both surfaces run the same cases here (the product's
terminal chat runs them in its own suite), and none of them writes when the
saved default is still valid.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest
from _helpers.chat_defaults import CASES, OPUS, assert_converged, stored, use_home, write_prefs
from alkera_cli.chat import headless
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.methods import preferences as daemon_preferences
from alkera_cli.gateway.client import GatewayCatalog
from alkera_cli.preferences.chat_defaults import resolve_and_persist_chat_defaults

pytestmark = pytest.mark.asyncio


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return use_home(monkeypatch, tmp_path)


async def _run_headless_setup(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    """Build the runtime exactly as ``alkera run`` does, signed in, against a
    reachable catalog. The orphan sweep is stubbed: it reaps real processes."""

    async def catalog(**_kwargs: Any) -> GatewayCatalog:
        return GatewayCatalog(models=[OPUS])

    monkeypatch.setattr(
        headless, "profile_for_project", lambda _project: types.SimpleNamespace(token="tok")
    )
    monkeypatch.setattr(headless, "fetch_catalog", catalog)
    monkeypatch.setattr(headless, "sweep_orphaned_agents", lambda: None)
    await headless._build_production_runtime(
        project, harness_type="opencode", model=None, effort=None, system_block_overrides=None
    )


async def _run_daemon_resolve(monkeypatch: pytest.MonkeyPatch, _project: Path) -> None:
    async def models(**_kwargs: Any) -> list[GatewayModel]:
        return [OPUS]

    monkeypatch.setattr(
        daemon_preferences,
        "project_profile",
        lambda *_a, **_k: types.SimpleNamespace(token="t", org_team_id=""),
    )
    monkeypatch.setattr(daemon_preferences, "fetch_models", models)
    await daemon_preferences.preferences_resolve_chat_defaults(
        object(),  # type: ignore[arg-type]
        daemon_preferences.ResolveChatDefaultsRequest(),
    )


@pytest.mark.parametrize(
    "surface",
    [
        pytest.param(_run_headless_setup, id="headless"),
        pytest.param(_run_daemon_resolve, id="daemon"),
    ],
)
@pytest.mark.parametrize(("saved", "expected"), CASES)
async def test_every_surface_converges_the_saved_default_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    tmp_path: Path,
    surface: Any,
    saved: tuple[str | None, str | None],
    expected: tuple[str | None, str | None],
) -> None:
    write_prefs(home, *saved)
    before = (home / "preferences.yml").read_bytes()
    await surface(monkeypatch, tmp_path / "project")
    assert_converged(home, before, saved, expected)


@pytest.mark.parametrize(("saved", "expected"), CASES)
async def test_the_shared_function_persists_and_returns_the_seed(
    home: Path,
    saved: tuple[str | None, str | None],
    expected: tuple[str | None, str | None],
) -> None:
    write_prefs(home, *saved)
    before = (home / "preferences.yml").read_bytes()
    resolved = resolve_and_persist_chat_defaults([OPUS])
    assert_converged(home, before, saved, expected)
    # The seed is always an offered model, whatever was stored.
    assert resolved.model_id == "opus"


async def test_an_unreachable_catalog_never_rewrites_the_saved_default(home: Path) -> None:
    write_prefs(home, "retired", "high")
    resolved = resolve_and_persist_chat_defaults([])
    assert (resolved.model_id, resolved.effort, resolved.reset) == ("retired", "high", False)
    assert stored(home) == ("retired", "high")


async def test_a_reader_who_saved_nothing_is_not_pinned(home: Path) -> None:
    write_prefs(home, None, None)
    resolved = resolve_and_persist_chat_defaults([OPUS])
    assert (resolved.model_id, resolved.effort) == ("opus", "high")
    assert stored(home) == (None, None)
