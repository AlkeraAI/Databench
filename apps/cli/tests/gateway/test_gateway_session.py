"""Gateway session wiring: per-prompt variant composition (model pinned), the
default gateway-config builder, and the runtime injecting the config WITHOUT
persisting the token to the manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.account import auth_file
from alkera_cli.harness import EventBus, HarnessRuntime, SessionConfig, gateway_session
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.gateway_session import (
    GatewayAuthRequiredError,
    default_gateway_config_builder,
)
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.runtime import AdapterFactory
from alkera_core.project.directory import ProjectDirectory


def _dummy_binary() -> ResolvedOpencodeBinary:
    return ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="env")


def _adapter(tmp_path: Path, model: dict[str, str] | None) -> OpencodeHttpAdapter:
    cfg = SessionConfig(
        session_id="s", project_dir=tmp_path, chat_dir=tmp_path / "chat", model=model
    )
    return OpencodeHttpAdapter(cfg, binary=_dummy_binary())


# --------------------------------------------------------------------------- #
# Per-prompt variant composition (model pinned)
# --------------------------------------------------------------------------- #


def test_variant_composes_from_pinned_session_model(tmp_path: Path) -> None:
    a = _adapter(tmp_path, {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"})
    eff = a._effective_prompt_model(PromptInput(text="x", variant="high"))
    assert eff == {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5::high"}


def test_variant_strips_existing_effort_before_rejoining(tmp_path: Path) -> None:
    a = _adapter(tmp_path, None)
    a._state.last_model = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5::low"}
    eff = a._effective_prompt_model(PromptInput(text="x", variant="high"))
    assert eff == {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5::high"}


def test_no_variant_no_model_sends_nothing(tmp_path: Path) -> None:
    # Normal turn: rely on opencode's configured default model.
    a = _adapter(tmp_path, {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"})
    assert a._effective_prompt_model(PromptInput(text="x")) is None


def test_no_variant_explicit_model_is_honored(tmp_path: Path) -> None:
    a = _adapter(tmp_path, None)
    eff = a._effective_prompt_model(
        PromptInput(text="x", model={"provider_id": "openai", "model_id": "mock-model"})
    )
    assert eff == {"provider_id": "openai", "model_id": "mock-model"}


# --------------------------------------------------------------------------- #
# default_gateway_config_builder
# --------------------------------------------------------------------------- #


def test_builder_returns_none_for_non_gateway_provider() -> None:
    assert default_gateway_config_builder({"provider_id": "opencode", "model_id": "bp"}) is None
    assert default_gateway_config_builder({}) is None


def test_builder_raises_without_a_chat_credential_even_when_signed_in() -> None:
    """The builder renders only the credential the runtime hands it: with none
    it refuses, and it never falls back to whatever sign-in is stored now."""
    from _profiles import ORG_A, store

    store(ORG_A, current=True)
    with pytest.raises(GatewayAuthRequiredError):
        default_gateway_config_builder(
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}
        )


def test_builder_builds_config_with_token_and_efforts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    cfg = default_gateway_config_builder(
        {
            "provider_id": "alkera-anthropic",
            "model_id": "claude-opus-4.5",
            "efforts": ["low", "high"],
            "effort": "high",
            "display_name": "Claude Opus 4.5",
            "context_window": 1_000_000,
            "max_output_tokens": 64_000,
        },
        token="jwt-xyz",
    )
    assert cfg is not None
    prov = cfg["provider"]["alkera-anthropic"]
    assert prov["options"]["apiKey"] == "jwt-xyz"
    assert prov["options"]["baseURL"] == "https://gw.example/anthropic/v1"
    assert "claude-opus-4.5::high" in prov["models"]
    assert cfg["model"] == "alkera-anthropic/claude-opus-4.5::high"
    # The manifest-persisted limits flow through to opencode's per-model `limit` —
    # so the daemon/editor single-model path arms native auto-compaction too, with the
    # ~90% `input` compaction-trigger headroom.
    assert prov["models"]["claude-opus-4.5::high"]["limit"] == {
        "context": 1_000_000,
        "input": 900_000,
        "output": 64_000,
    }


def test_builder_omits_limit_when_manifest_has_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An older manifest written before limits were persisted → no `limit` (the safe
    # legacy behavior, never a bogus context: 0).
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    cfg = default_gateway_config_builder(
        {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}, token="jwt"
    )
    assert cfg is not None
    assert "limit" not in cfg["provider"]["alkera-anthropic"]["models"]["claude-opus-4.5"]


@pytest.mark.parametrize(
    ("remembered", "declared"),
    [
        pytest.param(
            ["claude-opus-4.5", "gpt-5.5"],
            {"alkera-anthropic": {"claude-opus-4.5"}, "alkera-openai": {"gpt-5.5"}},
            id="a-catalog-holding-the-pin-is-declared-whole",
        ),
        pytest.param(
            ["gpt-5.5"],
            {"alkera-anthropic": {"claude-opus-4.5"}},
            id="a-catalog-without-the-pin-declares-the-pin-alone",
        ),
        pytest.param([], {"alkera-anthropic": {"claude-opus-4.5"}}, id="no-catalog-read-yet"),
    ],
)
def test_the_editor_spawns_on_the_catalog_it_last_read(
    monkeypatch: pytest.MonkeyPatch, remembered: list[str], declared: dict[str, set[str]]
) -> None:
    """So an open chat in the editor can move models without a respawn."""
    from alkera_cli.contracts.gateway_model import GatewayModel
    from alkera_cli.gateway.client import REMEMBERED_CATALOG

    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    wires = {"claude-opus-4.5": "anthropic", "gpt-5.5": "openai"}
    monkeypatch.setattr(
        REMEMBERED_CATALOG,
        "_models",
        tuple(GatewayModel(id=m, display_name=m, wire=wires[m]) for m in remembered),  # type: ignore[arg-type]
    )
    cfg = gateway_session.remembered_catalog_config_builder(
        {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}, token="jwt"
    )
    assert cfg is not None
    assert {p: set(v["models"]) for p, v in cfg["provider"].items()} == declared


# --------------------------------------------------------------------------- #
# Runtime injection (non-persisted)
# --------------------------------------------------------------------------- #


class _CapturingFactory(AdapterFactory):
    def __init__(self) -> None:
        super().__init__(binary=None)
        self.configs: list[SessionConfig] = []

    def __call__(  # type: ignore[override]
        self, config: SessionConfig, *, bus: EventBus, harness_type: str = "oc"
    ) -> FakeAdapter:
        self.configs.append(config)
        adapter = FakeAdapter()
        adapter._bus = bus
        return adapter


@pytest.mark.asyncio
async def test_runtime_injects_gateway_config_without_persisting_token(tmp_path: Path) -> None:
    sentinel = {
        "provider": {"alkera-anthropic": {"options": {"apiKey": "secret-jwt"}}},
        "model": "alkera-anthropic/claude-opus-4.5::high",
    }

    def builder(manifest_model: dict[str, Any]) -> dict[str, Any] | None:
        if str(manifest_model.get("provider_id", "")).startswith("alkera-"):
            return sentinel
        return None

    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _CapturingFactory()
    rt = HarnessRuntime(project, adapter_factory=factory, gateway_config_builder=builder)

    session = await rt.open_chat(
        create=True,
        model={
            "provider_id": "alkera-anthropic",
            "model_id": "claude-opus-4.5",
            "efforts": ["high"],
            "effort": "high",
        },
    )
    sid = session.session_id

    # The adapter saw the injected gateway config; the session model is the BASE.
    cfg = factory.configs[0]
    assert cfg.harness_native["agent_config"] == sentinel
    assert cfg.model == {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}

    await rt.close_chat(sid)

    # The token-bearing config is NOT written to the manifest on disk.
    manifest = next(m for m in rt.list_chats() if m.session_id == sid)
    assert "agent_config" not in manifest.harness


# --------------------------------------------------------------------------- #
# A cloud chat's own gateway token: the device token never reaches the agent
# --------------------------------------------------------------------------- #

_CLOUD_MODEL = {
    "provider_id": "alkera-anthropic",
    "model_id": "claude-opus-4.5",
    "efforts": ["low", "high"],
    "effort": "high",
    "display_name": "Claude Opus 4.5",
}


def _no_auth_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never() -> None:
        raise AssertionError("the device token file must not be read for a cloud chat")

    monkeypatch.setattr(auth_file, "_read_document", _never)
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )


def test_default_builder_renders_only_the_chats_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_auth_file(monkeypatch)
    cfg = default_gateway_config_builder(_CLOUD_MODEL, token="gw-chat-token")
    assert cfg is not None
    rendered = json.dumps(cfg)
    assert cfg["provider"]["alkera-anthropic"]["options"]["apiKey"] == "gw-chat-token"
    assert rendered.count("gw-chat-token") == 1
    assert "device-jwt" not in rendered


def test_catalog_builder_renders_only_the_chats_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_cli.contracts.gateway_model import GatewayModel
    from alkera_cli.harness.gateway_session import gateway_config_builder_for_catalog

    _no_auth_file(monkeypatch)
    catalog = [
        GatewayModel(
            id="claude-opus-4.5", display_name="Opus", wire="anthropic", efforts=("high",)
        ),
        GatewayModel(id="gpt-5", display_name="GPT", wire="openai", efforts=()),
    ]
    cfg = gateway_config_builder_for_catalog(catalog)(_CLOUD_MODEL, token="gw-chat-token")
    assert cfg is not None
    for provider in cfg["provider"].values():
        assert provider["options"]["apiKey"] == "gw-chat-token"
    assert json.dumps(cfg).count("gw-chat-token") == len(cfg["provider"])


def test_an_empty_gateway_token_is_no_credential_and_never_a_stored_one() -> None:
    # `token=""` is "no credential given": refused, even with a sign-in stored.
    from _profiles import ORG_A, store

    store(ORG_A, current=True)
    with pytest.raises(GatewayAuthRequiredError):
        default_gateway_config_builder(_CLOUD_MODEL, token="")


def test_a_non_gateway_chat_ignores_the_token() -> None:
    assert default_gateway_config_builder({"provider_id": "opencode"}, token="gw") is None


@pytest.mark.asyncio
async def test_runtime_hands_the_agent_the_sessions_gateway_token_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one seam a cloud chat's credential flows through: ``open_chat``
    passes it to the builder for that open, the adapter's config carries it,
    the manifest on disk never does — and the device token file is not read."""
    _no_auth_file(monkeypatch)
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _CapturingFactory()
    rt = HarnessRuntime(
        project, adapter_factory=factory, gateway_config_builder=default_gateway_config_builder
    )
    session = await rt.open_chat(create=True, model=dict(_CLOUD_MODEL), gateway_token="gw-1")
    sid = session.session_id
    cfg = factory.configs[0]
    rendered = json.dumps(cfg.harness_native["agent_config"])
    assert (
        cfg.harness_native["agent_config"]["provider"]["alkera-anthropic"]["options"]["apiKey"]
        == "gw-1"
    )
    assert rendered.count("gw-1") == 1
    await rt.close_chat(sid)
    manifest = next(m for m in rt.list_chats() if m.session_id == sid)
    assert "agent_config" not in manifest.harness
    assert "gw-1" not in (tmp_path / ".alkera").joinpath("chats", sid, "manifest.json").read_text()

    # Reopened with a fresh token: the agent sees the new one, not the old.
    session = await rt.open_chat(sid, gateway_token="gw-2")
    rendered = json.dumps(factory.configs[1].harness_native["agent_config"])
    assert rendered.count("gw-2") == 1 and "gw-1" not in rendered
    await rt.close_chat(sid)


@pytest.mark.asyncio
async def test_runtime_without_a_gateway_token_keeps_the_local_path(tmp_path: Path) -> None:
    # A builder written for the local path takes one positional argument; the
    # runtime calls it that way when no per-session credential is given.
    seen: list[dict[str, Any]] = []

    def builder(manifest_model: dict[str, Any]) -> dict[str, Any] | None:
        seen.append(manifest_model)
        return {"provider": {"alkera-anthropic": {"options": {"apiKey": "device-jwt"}}}}

    project = ProjectDirectory(tmp_path / ".alkera")
    factory = _CapturingFactory()
    rt = HarnessRuntime(project, adapter_factory=factory, gateway_config_builder=builder)
    session = await rt.open_chat(create=True, model=dict(_CLOUD_MODEL))
    assert len(seen) == 1
    assert (
        factory.configs[0].harness_native["agent_config"]["provider"]["alkera-anthropic"][
            "options"
        ]["apiKey"]
        == "device-jwt"
    )
    await rt.close_chat(session.session_id)
