"""Org-admin CRUD for per-org BYOK provider credentials.

The impure wrapper around ``alkera_core.model_providers``: loads/persists the
encrypted rows, enforces the write-only + anti-exfiltration rules, and runs the
admin-clicked connection test (which stamps ``last_verified_*`` — that stamp
means "an admin tested this"; the background health runner probes with the same
shared function but never writes it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from alkera_core.auth.secret_box import encrypt_secret
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_or_insert
from alkera_core.egress import EgressPolicy
from alkera_core.http import async_client
from alkera_core.llm_provider import Provider
from alkera_core.model_providers import (
    BYOK_PROVIDERS,
    BedrockControlClientFactory,
    ProviderProbeResult,
    credentials_from_row,
    probe_provider_credentials,
    secret_hint,
)
from alkera_core.models import ModelProviderConfig, User
from alkera_core.schemas.tenancy.model_providers import ModelProviderUpdateRequest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

#: How long the admin-clicked connection test may take. Short: it runs inside the
#: request an admin is watching, and a provider that has not answered a cheap
#: model list in ten seconds has failed the test either way.
_PROBE_TIMEOUT_SECONDS = 10.0


class ModelProviderValidationError(ValueError):
    """A payload the form should not have produced — the route maps it to 400."""


def env_fallback_present(provider: Provider) -> bool:
    """An INSTANCE-level env credential exists for this provider (informational
    — on a split deployment only the gateway may hold env keys)."""
    if provider is Provider.ANTHROPIC:
        return bool(settings.anthropic_api_key)
    if provider is Provider.OPENAI:
        return bool(settings.openai_api_key)
    return bool(
        settings.aws_bearer_token_bedrock
        or settings.aws_access_key_id
        or settings.gateway_assume_bedrock_iam
    )


async def list_configs(db: AsyncSession, org_team_id: UUID) -> dict[Provider, ModelProviderConfig]:
    rows = (
        (
            await db.execute(
                select(ModelProviderConfig).where(ModelProviderConfig.org_team_id == org_team_id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[Provider, ModelProviderConfig] = {}
    for row in rows:
        try:
            out[Provider(row.provider)] = row
        except ValueError:  # a row from a future provider this build doesn't know
            continue
    return out


async def get_config(
    db: AsyncSession, org_team_id: UUID, provider: Provider
) -> ModelProviderConfig | None:
    return (
        await db.execute(
            select(ModelProviderConfig).where(
                ModelProviderConfig.org_team_id == org_team_id,
                ModelProviderConfig.provider == provider.value,
            )
        )
    ).scalar_one_or_none()


def _norm_url(url: str | None) -> str | None:
    return url.rstrip("/") if url else None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ModelProviderValidationError(message)


async def upsert_config(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    provider: Provider,
    payload: ModelProviderUpdateRequest,
    actor: User,
) -> tuple[ModelProviderConfig, bool]:
    """Create or update one provider's config. Returns (row, created).

    Rules, in order:
      1. per-provider required fields;
      2. write-only secrets — blank keeps the stored ciphertext (the AWS pair is
         all-or-nothing per request);
      3. anti-exfiltration — changing ``base_url`` with the key left blank is
         rejected (a stored valid key must never be repointed silently);
      4. switching Bedrock to "iam" wipes the stored static pair;
      5. any credential/endpoint/mode change clears ``last_verified_*``.
    """
    locked, created = await lock_or_insert(
        db,
        LockRank.ORG_SETTINGS,
        select(ModelProviderConfig)
        .where(
            ModelProviderConfig.org_team_id == org_team_id,
            ModelProviderConfig.provider == provider.value,
        )
        .execution_options(populate_existing=True),
        insert(ModelProviderConfig).values(
            id=uuid4(), org_team_id=org_team_id, provider=provider.value
        ),
    )
    row = locked.scalar_one()

    material_change = False

    if provider in (Provider.ANTHROPIC, Provider.OPENAI):
        new_key = (payload.api_key or "").strip()
        new_url = _norm_url(payload.base_url)
        if created:
            _require(bool(new_key), f"An API key is required to configure {provider.value}")
        elif new_url != _norm_url(row.base_url) and not new_key:
            raise ModelProviderValidationError(
                "Changing the base URL requires re-entering the API key"
            )
        if new_url != _norm_url(row.base_url):
            row.base_url = new_url
            material_change = True
        if new_key:
            row.api_key_encrypted = encrypt_secret(new_key)
            row.secret_hint = secret_hint(new_key)
            material_change = True
        if provider is Provider.OPENAI:
            row.openai_organization_id = payload.openai_organization_id or None
    else:  # Bedrock
        _require(bool(payload.bedrock_region), "A Bedrock region is required")
        _require(
            payload.bedrock_auth_mode in ("iam", "access_key"),
            "The bedrock_auth_mode must be 'iam' or 'access_key'",
        )
        new_key_id = (payload.aws_access_key_id or "").strip()
        new_secret = (payload.aws_secret_access_key or "").strip()
        if payload.bedrock_region != row.bedrock_region:
            row.bedrock_region = payload.bedrock_region
            material_change = True
        if payload.bedrock_auth_mode != row.bedrock_auth_mode:
            row.bedrock_auth_mode = payload.bedrock_auth_mode
            material_change = True
        if payload.bedrock_auth_mode == "iam":
            _require(
                not new_key_id and not new_secret,
                "IAM-role mode uses the server's ambient credentials — do not supply keys",
            )
            # Switching away from static keys wipes them: no orphaned secrets.
            if row.aws_access_key_id_encrypted or row.aws_secret_access_key_encrypted:
                row.aws_access_key_id_encrypted = None
                row.aws_secret_access_key_encrypted = None
                row.secret_hint = None
                material_change = True
        else:  # access_key
            _require(
                bool(new_key_id) == bool(new_secret),
                "Supply BOTH the access key id and the secret access key (or neither, "
                "to keep the stored pair)",
            )
            if created or not row.aws_access_key_id_encrypted:
                _require(
                    bool(new_key_id),
                    "An AWS access key pair is required to configure Bedrock with static keys",
                )
            if new_key_id:
                row.aws_access_key_id_encrypted = encrypt_secret(new_key_id)
                row.aws_secret_access_key_encrypted = encrypt_secret(new_secret)
                row.secret_hint = secret_hint(new_key_id)
                material_change = True

    row.enabled = payload.enabled
    row.updated_by_id = actor.id
    if material_change:
        # A stale verification is no proof — the admin re-tests after a change.
        row.last_verified_at = None
        row.last_verified_status = None
    await db.flush()
    # onupdate=func.now() leaves updated_at expired after an UPDATE flush;
    # refresh eagerly so response serialization never lazy-loads.
    await db.refresh(row)
    return row, created


async def delete_config(db: AsyncSession, *, org_team_id: UUID, provider: Provider) -> bool:
    row = await get_config(db, org_team_id, provider)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def test_connection(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    provider: Provider,
    http_client: httpx.AsyncClient | None = None,
    bedrock_client_factory: BedrockControlClientFactory | None = None,
) -> tuple[ProviderProbeResult | None, ModelProviderConfig | None]:
    """Probe the STORED credentials (save first, then test) and stamp the
    outcome on the row. Returns (result, row); result None = not configured."""
    row = await get_config(db, org_team_id, provider)
    creds = credentials_from_row(row) if row is not None else None
    if row is None or creds is None:
        return None, row

    owns_client = http_client is None
    # The same org-admin string the gateway dials, so it gets the same judge and
    # the same pin. Without that the console answers "connection ok" for an
    # endpoint the gateway refuses at request time, and the probe itself becomes
    # a host-and-port oracle for the deployment's own network.
    client = http_client or async_client(
        timeout=_PROBE_TIMEOUT_SECONDS,
        egress_policy=EgressPolicy.from_settings(allow_private=settings.is_self_hosted),
    )
    try:
        result = await probe_provider_credentials(
            creds, http_client=client, bedrock_client_factory=bedrock_client_factory
        )
    finally:
        if owns_client:
            await client.aclose()

    row.last_verified_at = datetime.now(UTC)
    # "unavailable" can't happen here (the backend declares aioboto3); map it
    # defensively to network so the API vocabulary stays closed.
    row.last_verified_status = (
        "network" if result.classification == "unavailable" else result.classification
    )
    await db.flush()
    await db.refresh(row)
    return result, row


__all__ = [
    "BYOK_PROVIDERS",
    "ModelProviderValidationError",
    "delete_config",
    "env_fallback_present",
    "get_config",
    "list_configs",
    "test_connection",
    "upsert_config",
]
