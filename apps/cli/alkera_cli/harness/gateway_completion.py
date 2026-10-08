"""A one-shot metered LLM completion through the Alkera gateway — ANY wire.

The auto-mode safety judge (``safety_judge.py``) needs a SMALL, cheap model call
to decide whether a write is safe — billed + ZDR'd through the gateway exactly
like a chat turn. The judge model is whatever the user's catalog serves cheapest,
which may be an Anthropic model (the gateway's ``/anthropic/v1/messages``) OR an
OpenAI one (``/openai/v1/responses``) — so :func:`complete` dispatches on the
model's ``wire`` and accumulates the streamed text either way (the gateway is a
streaming proxy on both paths). The gateway reserves credit, forwards
wire-faithfully, and settles; an out-of-credit user gets a 402 (mapped to
:class:`GatewayInsufficientCreditError`) so the caller can stop the turn the same
way a normal credit exhaustion does.

Auth is the user's Alkera JWT + the gateway base URL — the same pair the harness
itself uses.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from alkera_cli.gateway.client import (
    GatewayAuthError,
    GatewayUnavailableError,
    WireProtocol,
)
from alkera_cli.host.limits import env_count, env_seconds

#: Anthropic Messages API version the gateway forwards wire-faithfully.
_ANTHROPIC_VERSION = "2023-06-01"

#: Budget for the whole judge call. The gateway is a network hop away and the
#: call is on the critical path of a write the model is waiting to make, so it
#: has to fail rather than hang — but a gateway under load that answers in
#: forty seconds is a judge that worked, and auto mode silently degrading into
#: repeated write refusals is the worse failure. 0 waits for the gateway.
ENV_JUDGE_TIMEOUT = "ALKERA_JUDGE_TIMEOUT_SECONDS"
TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_JUDGE_TIMEOUT), default=30.0)

#: The judge's reply budget. Its answer is one small JSON object, but a
#: reasoning-capable cheap model can spend the whole budget before any text and
#: hand back a verdict that cannot be read — which fails closed, i.e. blocks a
#: write that was fine. A non-positive value keeps the default; "no reply
#: budget" is not something the provider APIs accept.
ENV_JUDGE_MAX_TOKENS = "ALKERA_JUDGE_MAX_TOKENS"
_MAX_TOKENS_DEFAULT = 256
MAX_TOKENS = env_count(os.environ.get(ENV_JUDGE_MAX_TOKENS), default=_MAX_TOKENS_DEFAULT) or (
    _MAX_TOKENS_DEFAULT
)


class GatewayInsufficientCreditError(GatewayUnavailableError):
    """The gateway refused the request for lack of credit (HTTP 402)."""


async def complete(
    *,
    gateway_url: str,
    token: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    wire: WireProtocol = "anthropic",
    max_tokens: int = MAX_TOKENS,
    timeout_seconds: float | None = TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Stream one completion through the gateway on the model's wire and return
    the accumulated assistant text.

    ``wire`` selects the gateway ingress + request/SSE dialect:
    ``"anthropic"`` → ``/anthropic/v1/messages`` (Messages API);
    ``"openai"`` → ``/openai/v1/responses`` (Responses API).

    Raises :class:`GatewayInsufficientCreditError` on 402, ``GatewayAuthError`` on
    401/403, ``GatewayUnavailableError`` on any other non-200 or transport error.
    ``client`` is injectable for tests (a caller-owned client is not closed)."""
    base = gateway_url.rstrip("/")
    if wire == "openai":
        url = f"{base}/openai/v1/responses"
        headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_output_tokens": max_tokens,
            "stream": True,
        }
    else:
        url = f"{base}/anthropic/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")
                _raise_for_status(resp.status_code, gateway_url, detail)
            chunks: list[str] = []
            async for line in resp.aiter_lines():
                text = _sse_text_delta(line, wire)
                if text:
                    chunks.append(text)
            return "".join(chunks)
    except httpx.HTTPError as exc:
        raise GatewayUnavailableError(
            f"could not reach the gateway at {gateway_url}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()


def _raise_for_status(status: int, gateway_url: str, detail: str) -> None:
    if status == 402:
        raise GatewayInsufficientCreditError("insufficient credit for the auto-mode judge")
    if status in (401, 403):
        raise GatewayAuthError(f"the gateway rejected the token (HTTP {status})")
    raise GatewayUnavailableError(f"the gateway returned HTTP {status}: {detail[:200]}")


def _sse_text_delta(line: str, wire: WireProtocol) -> str:
    """The assistant-text delta of one SSE line, or "" for non-text frames.

    Anthropic Messages: ``content_block_delta`` events with ``delta.type ==
    "text_delta"``. OpenAI Responses: ``response.output_text.delta`` events with a
    string ``delta``. Anything unparseable / [DONE] / other event types → ""."""
    line = line.strip()
    if not line.startswith("data:"):
        return ""
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        event = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(event, dict):
        return ""
    if wire == "openai":
        if event.get("type") != "response.output_text.delta":
            return ""
        delta = event.get("delta")
        return delta if isinstance(delta, str) else ""
    if event.get("type") != "content_block_delta":
        return ""
    block_delta = event.get("delta")
    if isinstance(block_delta, dict) and block_delta.get("type") == "text_delta":
        text = block_delta.get("text")
        return text if isinstance(text, str) else ""
    return ""


__all__ = ["GatewayInsufficientCreditError", "complete"]
