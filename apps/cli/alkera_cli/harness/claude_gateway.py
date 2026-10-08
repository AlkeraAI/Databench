"""Build the env that routes the Claude Agent harness (Claude Code CLI) through
the Alkera model gateway. Anthropic-wire only — the CLI speaks the Anthropic
Messages API, so only ``alkera-anthropic``-provider chats are gateway-routable
here (OpenAI-family chats stay on the opencode/Responses path).

The result is the ``claude_env`` bag the adapter merges into the subprocess env
(`ClaudeAgentAdapter._build_env` reads ``harness_native["claude_env"]``). Built
per-open from the chat's pinned model + a FRESH auth token (never persisted), the
same discipline as :func:`alkera_cli.harness.gateway_session.default_gateway_config_builder`.
"""

from __future__ import annotations

from typing import Any, Protocol

from alkera_cli.harness.adapters.opencode_alkera import ANTHROPIC_PROVIDER_ID
from alkera_cli.harness.gateway_session import GatewayAuthRequiredError, gateway_credential
from alkera_cli.host.config import get_settings


def build_claude_gateway_env(
    manifest_model: dict[str, Any],
    *,
    gateway_url: str,
    token: str,
    haiku_model: str | None = None,
) -> dict[str, str] | None:
    """Return the ``claude_env`` for a gateway-routed Anthropic chat, or ``None``
    when the chat isn't anthropic-wire (leave the adapter's defaults alone).

    Raises ``ValueError`` if ``token`` is empty — never point Claude Code at the
    gateway unauthenticated.
    """
    provider_id = str(manifest_model.get("provider_id") or "")
    if provider_id != ANTHROPIC_PROVIDER_ID:
        return None
    if not token:
        raise ValueError("token is required — Claude Code must authenticate to the gateway")
    model_id = str(manifest_model.get("model_id") or "")
    env: dict[str, str] = {
        # NB: NO trailing /v1 — Claude Code appends `/v1/messages` itself (unlike
        # opencode's @ai-sdk/anthropic, whose baseURL ends in `…/anthropic/v1`).
        # Both resolve to the gateway's `POST /anthropic/v1/messages`.
        "ANTHROPIC_BASE_URL": f"{gateway_url.rstrip('/')}/anthropic",
        # The gateway accepts the Alkera JWT via either header.
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_AUTH_TOKEN": token,
    }
    if model_id:
        # Pin EVERY model selector to a catalog slug the gateway allowlist accepts
        # — including the background/subagent defaults — so Claude Code never sends
        # a raw Anthropic id the gateway would 404.
        env["ANTHROPIC_MODEL"] = model_id
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model_id
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model_id
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku_model or model_id
    return env


class ClaudeEnvBuilder(Protocol):
    """(manifest.model dict) -> the ``claude_env`` bag, or None if the chat
    isn't gateway-routable here. The Claude-Code analogue of
    ``GatewayConfigBuilder``: the runtime merges a non-None result into the
    session's ``harness_native`` WITHOUT persisting it (the credential must not
    hit the manifest). ``token`` is a cloud chat's own gateway token; ``None``
    means the machine's device token — the local path."""

    def __call__(
        self, manifest_model: dict[str, Any], *, token: str | None = None
    ) -> dict[str, str] | None: ...


class ClaudeModelNotSupportedError(GatewayAuthRequiredError):
    """A Claude Code chat pinned a non-Anthropic model. Claude Code only speaks
    the Anthropic Messages API, so it cannot run OpenAI-wire models. Subclasses
    ``GatewayAuthRequiredError`` so the CLI/daemon's existing gateway-precondition
    handler surfaces this (model-specific) message and exits cleanly — no new
    catch site needed."""


def default_claude_env_builder(
    manifest_model: dict[str, Any], *, token: str | None = None
) -> dict[str, str] | None:
    """Build the gateway ``claude_env`` for a Claude Code chat from its pinned
    model + the credential the agent presents to the gateway: ``token`` (a
    cloud chat's own gateway token) or, for a local chat, a FRESH device token
    read from ``~/.alkera/auth.yml`` (never persisted) — the Claude-Code
    analogue of
    :func:`alkera_cli.harness.gateway_session.default_gateway_config_builder`.

    Always returns a non-None env for a valid (Anthropic) chat — Claude Code MUST
    point at the gateway, so unlike the opencode builder there is no "leave the
    defaults alone" path. Raises :class:`ClaudeModelNotSupportedError` for a
    non-Anthropic model and ``GatewayAuthRequiredError`` when no valid token is
    available (both surfaced by the CLI/daemon as a clean error + sign-in hint).
    """
    provider_id = str(manifest_model.get("provider_id") or "")
    if provider_id != ANTHROPIC_PROVIDER_ID:
        raise ClaudeModelNotSupportedError(
            "the claude harness requires an Anthropic model — "
            f"'{manifest_model.get('model_id') or '?'}' isn't one. "
            "Pick a Claude model, or start the chat with `--harness alkera`."
        )
    return build_claude_gateway_env(
        manifest_model,
        gateway_url=get_settings().alkera_gateway_url,
        token=gateway_credential(token),
    )


__all__ = [
    "ClaudeEnvBuilder",
    "ClaudeModelNotSupportedError",
    "build_claude_gateway_env",
    "default_claude_env_builder",
]
