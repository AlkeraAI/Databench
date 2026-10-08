"""The gateway env builder for the Claude Agent harness (anthropic-wire only)."""

from __future__ import annotations

import pytest
from alkera_cli.harness.adapters.opencode_alkera import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
)
from alkera_cli.harness.claude_gateway import (
    ClaudeModelNotSupportedError,
    build_claude_gateway_env,
    default_claude_env_builder,
)
from alkera_cli.harness.gateway_session import GatewayAuthRequiredError


def test_anthropic_chat_builds_env() -> None:
    env = build_claude_gateway_env(
        {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "claude-opus-4.5"},
        gateway_url="https://gw.example.com",
        token="jwt-abc",
    )
    assert env is not None
    # NO trailing /v1 — Claude Code appends /v1/messages itself.
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "jwt-abc"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "jwt-abc"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4.5"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4.5"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-opus-4.5"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-opus-4.5"


def test_haiku_model_override() -> None:
    env = build_claude_gateway_env(
        {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "claude-opus-4.5"},
        gateway_url="https://gw",
        token="t",
        haiku_model="claude-haiku-4.5",
    )
    assert env is not None
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4.5"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4.5"


def test_trailing_slash_stripped() -> None:
    env = build_claude_gateway_env(
        {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "m"},
        gateway_url="https://gw/",
        token="t",
    )
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"] == "https://gw/anthropic"


def test_non_anthropic_provider_returns_none() -> None:
    assert (
        build_claude_gateway_env(
            {"provider_id": OPENAI_PROVIDER_ID, "model_id": "gpt-5"},
            gateway_url="https://gw",
            token="t",
        )
        is None
    )
    assert (
        build_claude_gateway_env(
            {"provider_id": "", "model_id": "x"}, gateway_url="https://gw", token="t"
        )
        is None
    )


def test_empty_token_raises() -> None:
    with pytest.raises(ValueError, match="token is required"):
        build_claude_gateway_env(
            {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "m"},
            gateway_url="https://gw",
            token="",
        )


def test_no_model_id_omits_model_vars() -> None:
    env = build_claude_gateway_env(
        {"provider_id": ANTHROPIC_PROVIDER_ID},
        gateway_url="https://gw",
        token="t",
    )
    assert env is not None
    assert "ANTHROPIC_MODEL" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://gw/anthropic"


# ---------------------------------------------------------------------------
# default_claude_env_builder — renders the chat's credential + settings
# ---------------------------------------------------------------------------


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch, gateway_url: str = "https://gw.example.com"
) -> None:
    import alkera_cli.harness.claude_gateway as cg

    monkeypatch.setattr(
        cg, "get_settings", lambda: type("S", (), {"alkera_gateway_url": gateway_url})()
    )


def test_default_builder_anthropic_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)
    env = default_claude_env_builder(
        {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "claude-opus-4.5"}, token="jwt-xyz"
    )
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "jwt-xyz"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4.5"


def test_default_builder_non_anthropic_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)
    with pytest.raises(ClaudeModelNotSupportedError, match="requires an Anthropic model"):
        default_claude_env_builder(
            {"provider_id": OPENAI_PROVIDER_ID, "model_id": "gpt-5"}, token="jwt-xyz"
        )


def test_default_builder_not_signed_in_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)
    with pytest.raises(GatewayAuthRequiredError, match="not signed in"):
        default_claude_env_builder(
            {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "claude-opus-4.5"}
        )


def test_model_not_supported_is_auth_error_subclass() -> None:
    """The CLI/daemon's gateway-precondition handler catches both via the parent."""
    assert issubclass(ClaudeModelNotSupportedError, GatewayAuthRequiredError)


def test_default_builder_hands_claude_only_the_chats_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloud chat's Claude Code process gets the chat's gateway token in
    BOTH auth variables and the device token file is never read."""
    from alkera_cli.account import auth_file
    from alkera_cli.harness import claude_gateway

    def _never() -> None:
        raise AssertionError("the device token file must not be read for a cloud chat")

    monkeypatch.setattr(auth_file, "_read_document", _never)
    monkeypatch.setattr(
        claude_gateway,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    env = claude_gateway.default_claude_env_builder(
        {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}, token="gw-chat"
    )
    assert env is not None
    assert env["ANTHROPIC_API_KEY"] == "gw-chat" and env["ANTHROPIC_AUTH_TOKEN"] == "gw-chat"
    assert [k for k, v in env.items() if "gw-chat" in v] == [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ]
    assert "device" not in "".join(env.values())


def test_default_builder_with_no_token_never_reads_a_stored_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local chat's credential is the profile it bound when it opened, handed
    in by the runtime; with none the builder refuses rather than borrow the
    sign-in that is current now."""
    from _profiles import ORG_A, store
    from alkera_cli.harness import claude_gateway

    store(ORG_A, current=True)
    monkeypatch.setattr(
        claude_gateway,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    with pytest.raises(GatewayAuthRequiredError):
        claude_gateway.default_claude_env_builder(
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5"}
        )
