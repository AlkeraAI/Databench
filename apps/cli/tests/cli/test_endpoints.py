"""Build-profile endpoint defaults: the local self-hosted stack unless a release
build baked its own endpoints, and runtime env still overrides the baked default
(so self-hosted needs no rebuild)."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from alkera_cli.host import build_profile, endpoints


@pytest.fixture
def reload_endpoints() -> Iterator[None]:
    yield
    importlib.reload(endpoints)


def test_the_committed_profile_bakes_no_endpoint() -> None:
    assert (build_profile.API_URL, build_profile.FRONTEND_URL, build_profile.GATEWAY_URL) == (
        "",
        "",
        "",
    )


def test_with_nothing_baked_the_defaults_are_the_local_stack(
    monkeypatch: pytest.MonkeyPatch, reload_endpoints: None
) -> None:
    for name in ("API_URL", "FRONTEND_URL", "GATEWAY_URL"):
        monkeypatch.setattr(build_profile, name, "")
    importlib.reload(endpoints)
    assert endpoints.DEFAULT_API_URL == "http://localhost:8000"
    assert endpoints.DEFAULT_FRONTEND_URL == "http://localhost:5173"
    assert endpoints.DEFAULT_GATEWAY_URL == "http://localhost:8081"


def test_a_baked_endpoint_wins_over_the_local_default(
    monkeypatch: pytest.MonkeyPatch, reload_endpoints: None
) -> None:
    monkeypatch.setattr(build_profile, "API_URL", "https://api.example.com")
    monkeypatch.setattr(build_profile, "FRONTEND_URL", "")
    monkeypatch.setattr(build_profile, "GATEWAY_URL", "https://gw.example.com")
    importlib.reload(endpoints)
    assert endpoints.DEFAULT_API_URL == "https://api.example.com"
    assert endpoints.DEFAULT_FRONTEND_URL == "http://localhost:5173"
    assert endpoints.DEFAULT_GATEWAY_URL == "https://gw.example.com"


def test_env_overrides_a_baked_default(
    monkeypatch: pytest.MonkeyPatch, reload_endpoints: None
) -> None:
    from alkera_cli.host import config

    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", "/nonexistent/system.yml")
    monkeypatch.setenv("ALKERA_GATEWAY_URL", "https://gw.override.example")
    monkeypatch.setattr(build_profile, "GATEWAY_URL", "https://gw.example.com")
    importlib.reload(endpoints)
    try:
        importlib.reload(config)
        assert config.get_settings().alkera_gateway_url == "https://gw.override.example"
    finally:
        monkeypatch.undo()
        importlib.reload(endpoints)
        importlib.reload(config)
