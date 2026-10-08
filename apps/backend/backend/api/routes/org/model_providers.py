"""Org-admin BYOK model-provider credentials (/api/v1/org/model-providers).

The whole surface is gated on the signed BYOK entitlement with a bare 404 —
on SaaS and unentitled installs it must look nonexistent, never locked.
Secrets are write-only; every mutation and connection test is recorded in the
org audit trail (never with key material).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.entitlements import Feature
from alkera_core.llm_provider import Provider
from alkera_core.models import ModelProviderConfig
from alkera_core.schemas.tenancy.model_providers import (
    ModelProviderRead,
    ModelProvidersResponse,
    ModelProviderTestResult,
    ModelProviderUpdateRequest,
)
from fastapi import APIRouter, Depends, HTTPException, status

from backend.auth.dependencies import (
    CurrentOrg,
    DbSession,
    OrgAdmin,
    OrgAdminVerified,
    require_entitled,
)
from backend.services import model_providers as model_provider_service
from backend.services.audit import org_audit as org_audit_service
from backend.services.model_providers import (
    BYOK_PROVIDERS,
    ModelProviderValidationError,
    env_fallback_present,
)

router = APIRouter(
    prefix="/api/v1/org/model-providers",
    tags=["model-providers"],
    dependencies=[Depends(require_entitled(Feature.BYOK))],
)


def _read(provider: Provider, row: ModelProviderConfig | None) -> ModelProviderRead:
    if row is None:
        return ModelProviderRead(
            provider=provider.value,
            configured=False,
            enabled=False,
            has_credentials=False,
            secret_hint=None,
            base_url=None,
            openai_organization_id=None,
            bedrock_region=None,
            bedrock_auth_mode=None,
            env_fallback=env_fallback_present(provider),
            last_verified_at=None,
            last_verified_status=None,
            updated_at=None,
        )
    # IAM-mode Bedrock stores no key — the ambient role IS the credential — so the
    # config is credentialed even with no ciphertext on the row. Reporting False here
    # would make a fully-functional IAM setup look uncredentialed to any consumer.
    is_iam_bedrock = provider is Provider.BEDROCK and row.bedrock_auth_mode == "iam"
    has_credentials = is_iam_bedrock or bool(
        row.api_key_encrypted or row.aws_access_key_id_encrypted
    )
    return ModelProviderRead(
        provider=provider.value,
        configured=True,
        enabled=row.enabled,
        has_credentials=has_credentials,
        secret_hint=row.secret_hint or None,
        base_url=row.base_url,
        openai_organization_id=row.openai_organization_id,
        bedrock_region=row.bedrock_region,
        bedrock_auth_mode=row.bedrock_auth_mode,  # type: ignore[arg-type]
        env_fallback=env_fallback_present(provider),
        last_verified_at=row.last_verified_at,
        last_verified_status=row.last_verified_status,  # type: ignore[arg-type]
        updated_at=row.updated_at,
    )


def _audit_detail(
    provider: Provider, row: ModelProviderConfig, *, rotated: bool
) -> dict[str, object]:
    # Non-secret fields only — the audit service also redacts known key names,
    # but nothing sensitive is ever placed here to begin with.
    return {
        "provider": provider.value,
        "enabled": row.enabled,
        "base_url": row.base_url,
        "bedrock_region": row.bedrock_region,
        "bedrock_auth_mode": row.bedrock_auth_mode,
        "credentials_rotated": rotated,
    }


@router.get("", response_model=ModelProvidersResponse)
async def list_model_providers(
    db: DbSession, admin: OrgAdmin, org_id: CurrentOrg
) -> ModelProvidersResponse:
    """All three provider cards, configured or not — the page renders the full
    picture (including the instance-env-fallback state)."""
    rows = await model_provider_service.list_configs(db, org_id)
    return ModelProvidersResponse(providers=[_read(p, rows.get(p)) for p in BYOK_PROVIDERS])


@router.put("/{provider}", response_model=ModelProviderRead)
async def upsert_model_provider(
    provider: Provider,
    payload: ModelProviderUpdateRequest,
    db: DbSession,
    admin: OrgAdminVerified,
    org_id: CurrentOrg,
) -> ModelProviderRead:
    if provider not in BYOK_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")
    try:
        row, created = await model_provider_service.upsert_config(
            db, org_team_id=org_id, provider=provider, payload=payload, actor=admin
        )
    except ModelProviderValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    rotated = bool((payload.api_key or "").strip() or (payload.aws_access_key_id or "").strip())
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="model_provider.configured" if created else "model_provider.updated",
        target=provider.value,
        detail=_audit_detail(provider, row, rotated=rotated),
    )
    return _read(provider, row)


@router.delete("/{provider}", response_model=ModelProviderRead)
async def remove_model_provider(
    provider: Provider, db: DbSession, admin: OrgAdminVerified, org_id: CurrentOrg
) -> ModelProviderRead:
    if provider not in BYOK_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")
    if not await model_provider_service.delete_config(db, org_team_id=org_id, provider=provider):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not configured")
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="model_provider.removed",
        target=provider.value,
        detail={"provider": provider.value},
    )
    return _read(provider, None)


@router.post("/{provider}/test", response_model=ModelProviderTestResult)
async def test_model_provider(
    provider: Provider, db: DbSession, admin: OrgAdminVerified, org_id: CurrentOrg
) -> ModelProviderTestResult:
    """A cheap REAL provider call with the STORED credentials (save first, then
    test). 200 even on a failed test — the failure IS the result."""
    if provider not in BYOK_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")
    result, row = await model_provider_service.test_connection(
        db, org_team_id=org_id, provider=provider
    )
    tested_at = datetime.now(UTC)
    if result is None:
        return ModelProviderTestResult(
            ok=False,
            status="not_configured",
            detail="No usable credentials stored for this provider",
            tested_at=tested_at,
        )
    status_value = row.last_verified_status if row is not None else "network"
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="model_provider.tested",
        target=provider.value,
        detail={"provider": provider.value, "status": status_value},
    )
    return ModelProviderTestResult(
        ok=result.classification == "ok",
        status=status_value,  # type: ignore[arg-type]
        detail=result.detail,
        tested_at=row.last_verified_at or tested_at if row else tested_at,
    )
