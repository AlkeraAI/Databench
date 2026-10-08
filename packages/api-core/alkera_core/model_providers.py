"""Per-org BYOK provider credentials: the shared read contract + probe.

This module is the single home for everything BOTH the backend (org-admin CRUD
+ the test-connection button) and the model gateway (per-request credential
resolution) need — the gateway must never import ``backend.*``. The worker's
deployment-health runner reuses :func:`probe_provider_credentials` too, so the
admin-clicked test and the background check can never drift apart.

Resolution is fail-closed per credential: a row that is disabled, incomplete,
or whose ciphertext no longer decrypts (a key-management fault) resolves to
``None`` — the caller falls back to instance env keys or reports the provider
unconfigured. Decrypted secrets exist only in these frozen DTOs, in flight.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.secret_box import InvalidToken, decrypt_secret
from alkera_core.llm_provider import Provider
from alkera_core.logging import get_logger
from alkera_core.models import ModelProviderConfig

log = get_logger(__name__)

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
# Sent on the Anthropic probe; the gateway pins its own version for real calls.
_ANTHROPIC_VERSION = "2023-06-01"

BYOK_PROVIDERS: tuple[Provider, ...] = (Provider.ANTHROPIC, Provider.OPENAI, Provider.BEDROCK)

BedrockAuthMode = Literal["iam", "access_key"]


def secret_hint(plaintext: str) -> str:
    """The masked display hint stored beside the ciphertext: "…abcd" (last 4).
    Empty for short values — better no hint than most of the secret."""
    if len(plaintext) < 8:
        return ""
    return f"…{plaintext[-4:]}"


@dataclass(frozen=True, slots=True)
class AnthropicOrgCredentials:
    api_key: str
    base_url: str = ANTHROPIC_DEFAULT_BASE_URL

    @property
    def provider(self) -> Provider:
        return Provider.ANTHROPIC


@dataclass(frozen=True, slots=True)
class OpenAIOrgCredentials:
    api_key: str
    base_url: str = OPENAI_DEFAULT_BASE_URL
    organization_id: str | None = None

    @property
    def provider(self) -> Provider:
        return Provider.OPENAI


@dataclass(frozen=True, slots=True)
class BedrockOrgCredentials:
    region: str
    auth_mode: BedrockAuthMode = "iam"
    # Decrypted; None in "iam" mode (the ambient chain / instance profile).
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    @property
    def provider(self) -> Provider:
        return Provider.BEDROCK


OrgProviderCredentials = AnthropicOrgCredentials | OpenAIOrgCredentials | BedrockOrgCredentials


def _decrypt_or_none(ciphertext: str | None, *, org_team_id: UUID, provider: str) -> str | None:
    if ciphertext is None:
        return None
    try:
        return decrypt_secret(ciphertext)
    except InvalidToken:
        # A key-management fault (rotated away the only decrypt key). Fail
        # closed for THIS credential, loudly — never take the request down.
        log.error("model_providers.decrypt_failed", org_id=str(org_team_id), provider=provider)
        return None


def credentials_from_row(row: ModelProviderConfig) -> OrgProviderCredentials | None:
    """An ENABLED row -> its decrypted DTO, or None when incomplete/corrupt."""
    if not row.enabled:
        return None
    if row.provider == Provider.ANTHROPIC.value:
        key = _decrypt_or_none(
            row.api_key_encrypted, org_team_id=row.org_team_id, provider=row.provider
        )
        if key is None:
            return None
        return AnthropicOrgCredentials(
            api_key=key, base_url=row.base_url or ANTHROPIC_DEFAULT_BASE_URL
        )
    if row.provider == Provider.OPENAI.value:
        key = _decrypt_or_none(
            row.api_key_encrypted, org_team_id=row.org_team_id, provider=row.provider
        )
        if key is None:
            return None
        return OpenAIOrgCredentials(
            api_key=key,
            base_url=row.base_url or OPENAI_DEFAULT_BASE_URL,
            organization_id=row.openai_organization_id,
        )
    if row.provider == Provider.BEDROCK.value:
        if not row.bedrock_region:
            return None
        if row.bedrock_auth_mode == "iam":
            return BedrockOrgCredentials(region=row.bedrock_region, auth_mode="iam")
        if row.bedrock_auth_mode == "access_key":
            key_id = _decrypt_or_none(
                row.aws_access_key_id_encrypted,
                org_team_id=row.org_team_id,
                provider=row.provider,
            )
            secret = _decrypt_or_none(
                row.aws_secret_access_key_encrypted,
                org_team_id=row.org_team_id,
                provider=row.provider,
            )
            if key_id is None or secret is None:
                return None
            return BedrockOrgCredentials(
                region=row.bedrock_region,
                auth_mode="access_key",
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
            )
        return None
    return None


async def resolve_org_provider_credentials(
    db: AsyncSession, *, org_team_id: UUID, provider: Provider
) -> OrgProviderCredentials | None:
    """The org's ENABLED, fully-configured credentials for one provider,
    decrypted — or None. Pure data read: does NOT check the BYOK entitlement
    (the gateway gates before calling; the backend routes 404 unentitled)."""
    row = (
        await db.execute(
            select(ModelProviderConfig).where(
                ModelProviderConfig.org_team_id == org_team_id,
                ModelProviderConfig.provider == provider.value,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return credentials_from_row(row)


async def resolve_all(
    db: AsyncSession, org_team_id: UUID
) -> dict[Provider, OrgProviderCredentials]:
    """Every provider the org has usable credentials for, in one query."""
    rows = (
        (
            await db.execute(
                select(ModelProviderConfig).where(ModelProviderConfig.org_team_id == org_team_id)
            )
        )
        .scalars()
        .all()
    )
    resolved: dict[Provider, OrgProviderCredentials] = {}
    for row in rows:
        creds = credentials_from_row(row)
        if creds is not None:
            resolved[creds.provider] = creds
    return resolved


# --- the shared probe (test-connection button + deployment health) ---------------

ProbeClassification = Literal["ok", "invalid_key", "permission", "network", "unavailable"]

# An aioboto3 "bedrock" (control-plane) client context-manager factory — the
# injectable seam for tests and for processes without aioboto3 installed.
BedrockControlClientFactory = Callable[..., AbstractAsyncContextManager[Any]]


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    classification: ProbeClassification
    detail: str
    latency_ms: int


def _default_bedrock_factory(creds: BedrockOrgCredentials) -> AbstractAsyncContextManager[Any]:
    import aioboto3  # heavy; only processes that actually probe Bedrock need it

    session = aioboto3.Session()
    kwargs: dict[str, Any] = {"region_name": creds.region}
    if creds.auth_mode == "access_key":
        kwargs["aws_access_key_id"] = creds.aws_access_key_id
        kwargs["aws_secret_access_key"] = creds.aws_secret_access_key
    client: AbstractAsyncContextManager[Any] = session.client("bedrock", **kwargs)
    return client


def _classify_http(status_code: int) -> tuple[ProbeClassification, str]:
    if status_code in (200, 204):
        return "ok", "credentials accepted"
    if status_code == 401:
        return "invalid_key", "the API key was rejected (401) — update or rotate it"
    if status_code == 403:
        return (
            "permission",
            "the key authenticated but lacks permission (403) — check its scopes/role",
        )
    if status_code == 429:
        # Rate-limited IS authenticated — the key works.
        return "ok", "credentials accepted (provider rate-limited the probe)"
    return "network", f"unexpected provider response (HTTP {status_code})"


async def probe_provider_credentials(
    creds: OrgProviderCredentials,
    *,
    http_client: httpx.AsyncClient,
    bedrock_client_factory: BedrockControlClientFactory | None = None,
    timeout_s: float = 10.0,
) -> ProviderProbeResult:
    """A cheap REAL call proving the credentials authenticate — Anthropic/OpenAI
    list-models, Bedrock control-plane list-foundation-models. Never raises;
    every failure classifies with an actionable, secret-free detail."""
    started = time.monotonic()

    def _done(classification: ProbeClassification, detail: str) -> ProviderProbeResult:
        return ProviderProbeResult(
            classification=classification,
            detail=detail,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        async with asyncio.timeout(timeout_s):
            if isinstance(creds, AnthropicOrgCredentials):
                resp = await http_client.get(
                    f"{creds.base_url.rstrip('/')}/v1/models",
                    headers={
                        "x-api-key": creds.api_key,
                        "anthropic-version": _ANTHROPIC_VERSION,
                    },
                )
                return _done(*_classify_http(resp.status_code))
            if isinstance(creds, OpenAIOrgCredentials):
                headers = {"Authorization": f"Bearer {creds.api_key}"}
                if creds.organization_id:
                    headers["OpenAI-Organization"] = creds.organization_id
                resp = await http_client.get(
                    f"{creds.base_url.rstrip('/')}/models", headers=headers
                )
                return _done(*_classify_http(resp.status_code))
            return await _probe_bedrock(creds, bedrock_client_factory, _done)
    except TimeoutError:
        return _done("network", f"the provider did not respond within {timeout_s:.0f}s")
    except httpx.HTTPError as exc:
        return _done("network", f"could not reach the provider ({type(exc).__name__})")


async def _probe_bedrock(
    creds: BedrockOrgCredentials,
    factory: BedrockControlClientFactory | None,
    done: Callable[[ProbeClassification, str], ProviderProbeResult],
) -> ProviderProbeResult:
    if factory is None:
        try:
            client_cm = _default_bedrock_factory(creds)
        except ImportError:
            return done(
                "unavailable", "Bedrock probe unavailable in this process (aioboto3 not installed)"
            )
    else:
        client_cm = factory(creds)
    try:
        async with client_cm as client:
            await client.list_foundation_models(maxResults=1)
        return done("ok", "credentials accepted")
    except Exception as exc:  # botocore raises dynamically-generated classes
        name = type(exc).__name__
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
        if code in ("UnrecognizedClientException", "InvalidSignatureException") or name in (
            "NoCredentialsError",
            "PartialCredentialsError",
        ):
            detail = (
                "no ambient AWS credentials found on this server — attach an IAM role "
                "or switch to access keys"
                if creds.auth_mode == "iam" and name == "NoCredentialsError"
                else "AWS rejected the credentials — check the access key pair"
            )
            return done("invalid_key", detail)
        if code in ("AccessDeniedException", "AccessDenied"):
            return done(
                "permission",
                "the AWS credentials lack bedrock:ListFoundationModels — check the IAM policy",
            )
        if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"):
            return done("network", f"could not reach Bedrock in {creds.region} ({name})")
        return done("network", f"Bedrock probe failed ({code or name})")


__all__ = [
    "ANTHROPIC_DEFAULT_BASE_URL",
    "BYOK_PROVIDERS",
    "OPENAI_DEFAULT_BASE_URL",
    "AnthropicOrgCredentials",
    "BedrockAuthMode",
    "BedrockControlClientFactory",
    "BedrockOrgCredentials",
    "OpenAIOrgCredentials",
    "OrgProviderCredentials",
    "ProbeClassification",
    "ProviderProbeResult",
    "credentials_from_row",
    "probe_provider_credentials",
    "resolve_all",
    "resolve_org_provider_credentials",
    "secret_hint",
]
