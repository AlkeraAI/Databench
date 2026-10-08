"""Gateway HTTP routes + the per-request transport factory."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
from alkera_core.auth.proxy_token import is_proxy_machine_email
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import byok_active, get_entitlements
from alkera_core.gateway import (
    IDEMPOTENCY_KEY_HEADER,
    THINKING_DISPLAY_HEADER,
    USAGE_METER_HEADER,
)
from alkera_core.llm_provider import Provider
from alkera_core.logging import get_logger
from alkera_core.model_providers import (
    AnthropicOrgCredentials,
    BedrockOrgCredentials,
    OpenAIOrgCredentials,
    OrgProviderCredentials,
)
from alkera_core.models import OrgSettings
from alkera_core.models.model_catalog import Model, ModelRoute
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_gateway.adapters import (
    AnthropicDirectTransport,
    AnthropicMessagesCodec,
    BedrockInvokeTransport,
    OpenAIResponsesCodec,
    OpenAIResponsesTransport,
    ProxyUpstreamTransport,
    Transport,
    aioboto3_bedrock_client_factory,
)
from model_gateway.auth import GatewayAuth
from model_gateway.credentials import request_credentials, usable_providers
from model_gateway.meter import UNMETERED, gateway_meter
from model_gateway.pipeline import proxy_stream
from model_gateway.resolution import list_models_with_wire, provider_wire

log = get_logger(__name__)

router = APIRouter()
_anthropic_codec = AnthropicMessagesCodec()
_openai_codec = OpenAIResponsesCodec()

# Which providers each ingress can route to — a codec is paired with exactly the
# providers that speak its wire protocol. Failover stays within this set.
_ANTHROPIC_PROVIDERS = (Provider.ANTHROPIC, Provider.BEDROCK)
_OPENAI_PROVIDERS = (Provider.OPENAI,)


def _transport_factory(
    request: Request, creds: dict[Provider, OrgProviderCredentials] | None = None
) -> Callable[[ModelRoute], Transport]:
    # Test seam: a fully-built factory injected on app.state wins (lets tests
    # drive the Bedrock path with a fake aioboto3 client).
    override = getattr(request.app.state, "transport_factory_override", None)
    if override is not None:
        factory: Callable[[ModelRoute], Transport] = override
        return factory

    client: httpx.AsyncClient = request.app.state.http_client
    # The client for an upstream an ORG ADMIN named. It vets and pins every
    # request; the operator's own endpoints keep the plain pooled client, since
    # policing a URL the operator configured would only break a deliberate
    # in-network deployment.
    byok_client: httpx.AsyncClient = request.app.state.byok_http_client

    # Proxy mode: forward EVERY route to Alkera's hosted gateway with the org's
    # proxy token (the local catalog only decides the wire + local sell price; the
    # actual provider call happens at Alkera). No provider keys are used here.
    if settings.gateway_proxy_mode:

        def build_proxy(route: ModelRoute) -> Transport:
            return ProxyUpstreamTransport(
                base_url=settings.alkera_proxy_url,
                token=settings.alkera_proxy_token or "",
                wire=provider_wire(route.provider),
                client=client,
            )

        return build_proxy

    org_creds = creds or {}

    def build(route: ModelRoute) -> Transport:
        # Direct mode: the org's BYOK credentials (resolved per request from the
        # DB — zero-downtime rotation, instant disable) win WHOLESALE for their
        # provider; without a row the instance env keys apply exactly as before.
        org = org_creds.get(route.provider)
        if route.provider is Provider.ANTHROPIC:
            if isinstance(org, AnthropicOrgCredentials):
                return AnthropicDirectTransport(
                    base_url=org.base_url,
                    api_key=org.api_key,
                    anthropic_version=settings.anthropic_version,
                    client=byok_client,
                )
            return AnthropicDirectTransport(
                base_url=settings.anthropic_base_url,
                api_key=settings.anthropic_api_key or "",
                anthropic_version=settings.anthropic_version,
                client=client,
            )
        if route.provider is Provider.BEDROCK:
            if isinstance(org, BedrockOrgCredentials):
                # BYOK Bedrock is the IAM role or the org's static pair; the
                # factory pins SigV4 for it, so an instance bearer can never
                # sign an org's request.
                return BedrockInvokeTransport(
                    client_factory=aioboto3_bedrock_client_factory(
                        region=route.region or org.region,
                        endpoint_url=settings.bedrock_base_url,
                        bearer_token=None,
                        aws_access_key_id=org.aws_access_key_id,
                        aws_secret_access_key=org.aws_secret_access_key,
                        aws_session_token=None,
                    )
                )
            return BedrockInvokeTransport(
                client_factory=aioboto3_bedrock_client_factory(
                    region=route.region or settings.aws_region,
                    endpoint_url=settings.bedrock_base_url,
                    bearer_token=settings.aws_bearer_token_bedrock,
                    aws_access_key_id=settings.aws_access_key_id,
                    aws_secret_access_key=settings.aws_secret_access_key,
                    aws_session_token=settings.aws_session_token,
                )
            )
        if route.provider is Provider.OPENAI:
            if isinstance(org, OpenAIOrgCredentials):
                return OpenAIResponsesTransport(
                    base_url=org.base_url,
                    api_key=org.api_key,
                    organization_id=org.organization_id,
                    client=byok_client,
                )
            return OpenAIResponsesTransport(
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key or "",
                client=client,
            )
        raise NotImplementedError(f"no transport for provider {route.provider}")

    return build


def _provider_filter(
    request: Request, creds: dict[Provider, OrgProviderCredentials]
) -> Callable[[Provider], bool] | None:
    """The candidate/models availability filter — active when BYOK is in effect,
    AND when a BYOK deployment's entitlement has lapsed (so the failure surfaces
    as a clear 403 naming the entitlement, never a misleading 402 that makes
    clients show top-up UI). Proxy mode reaches every provider through Alkera; a
    non-BYOK direct deployment keeps today's behavior byte-identical; a
    test-injected transport factory serves whatever it scripts."""
    if getattr(request.app.state, "transport_factory_override", None) is not None:
        return None
    if gateway_meter() is UNMETERED and not settings.gateway_proxy_mode:
        # Nothing bills this gateway, so nothing stands behind a hosted key: a
        # provider is reachable only with a credential this deployment holds
        # (an org's own key or the instance's). Decided per request, not at seed
        # time, so setting or removing a key takes effect without reloading the
        # catalog.
        usable = usable_providers(creds)
        return lambda provider: provider in usable
    byok_shaped = settings.is_self_hosted and not settings.gateway_proxy_mode
    if not byok_shaped:
        return None
    if byok_active():
        usable = usable_providers(creds)
        return lambda provider: provider in usable
    if get_entitlements().state() in ("expired", "invalid"):
        # A BYOK deployment whose entitlement lapsed must hard-fail with the
        # entitlement 403 — NOT fall back to lingering instance env keys, which
        # would silently bill at SELL basis (or 402 an unfunded org) and hide the
        # real problem. `creds` is empty here (request_credentials returns {} when
        # BYOK is inactive), so filtering on env-fallback providers would let those
        # env routes survive; reject every provider to force the 403.
        return lambda provider: False
    # A self-hosted direct deployment that was NEVER entitled keeps today's
    # behavior byte-identical (instance env keys, no BYOK fork).
    return None


# How many bytes of a POST body the gateway will ever hold in memory comes from
# `gateway_max_request_body_bytes`; every route that reads one goes through
# `bounded_body`. The edge WAF's managed body-size rule is deliberately
# overridden to `count` for the gateway host (LLM bodies dwarf its 8 KB limit),
# so without this the gateway is the one authenticated surface with no upstream
# body bound at all — and the body is buffered whole before any credit is
# reserved, so N concurrent uploads cost the attacker nothing and OOM every
# other tenant's in-flight stream.


def _declared_body_length(request: Request) -> int | None:
    """The parsed Content-Length, or None when absent or unparsable."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    return length if length >= 0 else None


def _too_large(request: Request, size: int) -> JSONResponse:
    log.warning(
        "gateway.body_too_large",
        path=request.url.path,
        size=size,
        limit=settings.gateway_max_request_body_bytes,
    )
    return JSONResponse(
        status_code=413,
        content={
            "error": (
                f"request body exceeds the {settings.gateway_max_request_body_bytes}-byte limit"
            ),
        },
    )


async def bounded_body(request: Request) -> bytes | JSONResponse:
    """The raw body, read with a hard ceiling on what is ever held in memory.

    An over-large **declared** size is refused without reading a byte; a body that
    declares nothing (or lies, or arrives chunked) is read incrementally and
    abandoned the moment it crosses the ceiling. Either way the task never buffers
    more than the limit, so concurrent uploads cannot OOM the shared process.
    """
    limit = settings.gateway_max_request_body_bytes
    declared = _declared_body_length(request)
    if declared is not None and declared > limit:
        return _too_large(request, declared)
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return _too_large(request, size)
        chunks.append(chunk)
    return b"".join(chunks)


#: The client-supplied idempotency key is echoed into structured logs and
#: persisted as the ProxyRequest's ``request_id``, so it is held to an
#: unambiguous token charset rather than arbitrary header bytes. Its length is
#: bounded by `gateway_max_idempotency_key_length`, which the settings validator
#: keeps inside that column — an over-long value would otherwise be refused by
#: the database mid-admission and surface as an unhandled 500 on every retry.
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]+")


def _idempotency_key(request: Request) -> str | None | JSONResponse:
    """The validated idempotency key header, None when absent, or a 400 when
    the caller sent something the request row cannot hold."""
    raw = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if raw is None or not raw.strip():
        # An empty header means "no key", exactly as omitting it does — the gateway
        # then mints its own request id, which is the long-standing behavior.
        return None
    max_length = settings.gateway_max_idempotency_key_length
    if len(raw) > max_length or _IDEMPOTENCY_KEY_RE.fullmatch(raw) is None:
        log.warning("gateway.idempotency_key_invalid", path=request.url.path, length=len(raw))
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"{IDEMPOTENCY_KEY_HEADER} must be at most "
                    f"{max_length} characters of [A-Za-z0-9._:-]"
                )
            },
        )
    return raw


async def _json_object_body(request: Request) -> dict[str, Any] | JSONResponse:
    """A body the gateway can't parse is the caller's error: 400, never a 500."""
    raw = await bounded_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    try:
        body: Any = json.loads(raw)
    except ValueError:
        # Enough to tell an empty body from a compressed/mangled one, and a
        # Bun/opencode sender from an httpx one, without logging the payload.
        log.warning(
            "gateway.body_unparseable",
            path=request.url.path,
            content_type=request.headers.get("content-type"),
            content_encoding=request.headers.get("content-encoding"),
            content_length=request.headers.get("content-length"),
            user_agent=request.headers.get("user-agent"),
            body_prefix=raw[:32].hex(),
        )
        return JSONResponse(status_code=400, content={"error": "body must be valid JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a JSON object"})
    return body


@router.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request, claims: GatewayAuth) -> Response:
    body = await _json_object_body(request)
    if isinstance(body, JSONResponse):
        return body
    key = _idempotency_key(request)
    if isinstance(key, JSONResponse):
        return key
    creds = await request_credentials(claims.org_team_id)
    return await proxy_stream(
        claims=claims,
        body=body,
        transport_factory=_transport_factory(request, creds),
        codec=_anthropic_codec,
        compatible_providers=_ANTHROPIC_PROVIDERS,
        request_id=key,
        thinking_display=request.headers.get(THINKING_DISPLAY_HEADER),
        usage_meter=request.headers.get(USAGE_METER_HEADER),
        provider_available=_provider_filter(request, creds),
        # A self-hosted gateway relays its whole deployment through one machine
        # principal, bounded by the process-wide stream cap alone.
        shared_principal=is_proxy_machine_email(claims.email),
    )


@router.post("/openai/v1/responses")
async def openai_responses(request: Request, claims: GatewayAuth) -> Response:
    body = await _json_object_body(request)
    if isinstance(body, JSONResponse):
        return body
    key = _idempotency_key(request)
    if isinstance(key, JSONResponse):
        return key
    creds = await request_credentials(claims.org_team_id)
    return await proxy_stream(
        claims=claims,
        body=body,
        transport_factory=_transport_factory(request, creds),
        codec=_openai_codec,
        compatible_providers=_OPENAI_PROVIDERS,
        request_id=key,
        thinking_display=request.headers.get(THINKING_DISPLAY_HEADER),
        usage_meter=request.headers.get(USAGE_METER_HEADER),
        provider_available=_provider_filter(request, creds),
        # A self-hosted gateway relays its whole deployment through one machine
        # principal, bounded by the process-wide stream cap alone.
        shared_principal=is_proxy_machine_email(claims.email),
    )


def _model_payload(model: Model, wire: str) -> dict[str, Any]:
    return {
        "id": model.id,
        "object": "model",
        "owned_by": "alkera",
        "display_name": model.display_name,
        "family": model.family,
        "wire": wire,  # "anthropic" | "openai" — which ingress/opencode provider
        "context_window": model.context_window,
        # Max output tokens — the CLI gives opencode `limit: {context, output}` so its
        # overflow check sizes the usable input budget as roughly `context - output`.
        "max_output_tokens": model.max_output_tokens,
        "supports_thinking": model.supports_thinking,
        "supports_caching": model.supports_caching,
        # Capability/cost tier — lets the CLI route subagents (explore → cheap).
        "tier": model.tier.value,
        # Reasoning-effort variants the caller may pick (sent back as
        # "<id>::<effort>"); empty = no variant choice.
        "efforts": list(model.reasoning_efforts),
        "default_effort": model.default_effort,
        # Which reasoning the model emits and which it reads on replay: what a
        # chat may switch onto without losing the reasoning in its history.
        "reasoning_format": model.reasoning_format,
        "reads_reasoning_formats": list(model.reads_reasoning_formats),
    }


@router.get("/v1/models")
async def list_models(request: Request, claims: GatewayAuth) -> dict[str, Any]:
    """Enabled, routable models the caller may select (OpenAI-style list shape,
    enriched with Alkera display metadata + the wire protocol each is reached
    through). Requires a valid Alkera token. Under BYOK the list is filtered to
    models with at least one route whose provider has usable credentials — a
    model the gateway cannot actually reach is not offered."""
    creds = await request_credentials(claims.org_team_id)
    available = _provider_filter(request, creds)
    usable = {p for p in Provider if available(p)} if available is not None else None
    async with AsyncSessionLocal() as db:
        models = await list_models_with_wire(db, claims.org_team_id, usable=usable)
        web_search = await _web_search_enabled(db, claims.org_team_id)
    return {
        "object": "list",
        "data": [_model_payload(m, wire) for m, wire in models],
        # Org-wide feature toggles the CLI reads at session bootstrap (this is
        # already its per-org, per-session fetch). Top-level so the `data` list
        # keeps the plain OpenAI shape.
        "org_flags": {
            "web_search_enabled": web_search,
            # `web.fetch` reaches any public URL the model names, in every
            # permission mode, and the only thing in front of it is the network.
            # An install that wants its agents off the public internet turns this
            # off with an env change and a gateway restart instead of an image
            # roll or a VPC change. It can only ever subtract from the org
            # toggle, never grant past it.
            "web_fetch_enabled": web_search and settings.agent_web_fetch_enabled,
        },
    }


async def _web_search_enabled(db: AsyncSession, org_team_id: UUID) -> bool:
    """The org's resolved web-tools toggle: the stored `org_settings` value when
    set, else the deployment default (SaaS on, self-hosted off). A missing row
    reads as unset — same contract as the backend's `get_effective`."""
    row = (
        await db.execute(select(OrgSettings).where(OrgSettings.org_team_id == org_team_id))
    ).scalar_one_or_none()
    if row is not None:
        return row.web_search_effective
    return not settings.is_self_hosted
