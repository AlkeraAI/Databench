"""Daemon `harness.list_models` + the model/variant request surface."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_cli.account import auth_file
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.methods import harness as dh
from alkera_cli.daemon.methods.harness import (
    HarnessListModelsRequest,
    HarnessOpenChatRequest,
    HarnessSendPromptRequest,
    harness_list_models,
)
from alkera_cli.daemon.server import AuthRequiredError
from alkera_cli.gateway.client import GatewayAuthError


def _patch(monkeypatch: pytest.MonkeyPatch, *, token: str | None, fetch: Any) -> None:
    if token is not None:
        auth_file.save_profile(
            auth_file.profile_from_token("http://api.test", token), make_current=True
        )
    monkeypatch.setattr(
        dh, "get_settings", lambda: type("S", (), {"alkera_gateway_url": "http://gw"})()
    )
    monkeypatch.setattr(dh, "fetch_models", fetch)


@pytest.mark.asyncio
async def test_list_models_returns_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(**_kw: Any) -> list[GatewayModel]:
        return [
            GatewayModel(
                id="claude-opus-4.5",
                display_name="Claude Opus 4.5",
                wire="anthropic",
                efforts=("low", "high"),
                default_effort="low",
            )
        ]

    _patch(monkeypatch, token="t", fetch=fake_fetch)
    resp = await harness_list_models(object(), HarnessListModelsRequest())  # type: ignore[arg-type]
    assert len(resp.models) == 1
    m = resp.models[0]
    assert m.id == "claude-opus-4.5"
    assert m.wire == "anthropic"
    assert m.efforts == ["low", "high"]
    assert m.default_effort == "low"


@pytest.mark.asyncio
async def test_list_models_excludes_test_families(monkeypatch: pytest.MonkeyPatch) -> None:
    """The editor dropdown must match the TUI picker — e2e test-fixture families
    are dropped so a dev gateway's accumulated fixtures never reach the user."""

    async def fake_fetch(**_kw: Any) -> list[GatewayModel]:
        return [
            GatewayModel(id="t1", display_name="Fixture", wire="anthropic", family="test"),
            GatewayModel(
                id="claude-opus-4.5",
                display_name="Claude Opus 4.5",
                wire="anthropic",
                family="claude",
            ),
        ]

    _patch(monkeypatch, token="t", fetch=fake_fetch)
    resp = await harness_list_models(object(), HarnessListModelsRequest())  # type: ignore[arg-type]
    assert [m.id for m in resp.models] == ["claude-opus-4.5"]


@pytest.mark.asyncio
async def test_list_models_without_auth_raises_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, token=None, fetch=None)
    with pytest.raises(AuthRequiredError):
        await harness_list_models(object(), HarnessListModelsRequest())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_models_gateway_auth_error_maps_reason_to_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(**_kw: Any) -> list[GatewayModel]:
        raise GatewayAuthError("email_verification_required", status_code=403)

    _patch(monkeypatch, token="t", fetch=fake_fetch)
    with pytest.raises(AuthRequiredError) as exc_info:
        await harness_list_models(object(), HarnessListModelsRequest())  # type: ignore[arg-type]
    assert exc_info.value.reason == "email_verification_required"


def test_request_models_accept_model_and_variant() -> None:
    # The editor sends the gateway SELECTION (+ effort); the daemon builds the
    # pinned manifest from it. The per-turn variant rides on send_prompt.
    open_req = HarnessOpenChatRequest(
        project_path="/x",
        create=True,
        model={"id": "m", "wire": "anthropic", "efforts": ["low", "high"]},
        effort="high",
    )
    assert open_req.model is not None
    assert open_req.model.id == "m"
    assert open_req.model.wire == "anthropic"
    assert open_req.effort == "high"
    prompt_req = HarnessSendPromptRequest(session_id="s", text="hi", variant="high")
    assert prompt_req.variant == "high"
