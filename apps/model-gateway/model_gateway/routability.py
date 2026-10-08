"""Whether this gateway can route any model at all, for its readiness answer.

A model is routable when it is enabled and has an enabled route to a provider
the deployment holds a credential for: an instance key, or an org's own key
where those are in effect. Under a proxy upstream the upstream holds the keys,
so every enabled, routed model counts. A fresh install with no key yet routes
nothing; the gateway stays ready and says so, so an operator sees why no chat
can start.
"""

from __future__ import annotations

from alkera_core.config import settings
from alkera_core.entitlements import byok_active
from alkera_core.llm_provider import Provider
from alkera_core.models import ModelProviderConfig
from alkera_core.models.model_catalog import Model, ModelRoute
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from model_gateway.credentials import env_configured_providers

#: The readiness detail when nothing can be routed.
NO_ROUTABLE_MODEL = (
    "No model can be routed. Set a provider key (ANTHROPIC_API_KEY or OPENAI_API_KEY) "
    "or add one under the organization's model providers."
)


async def _org_keyed_providers(db: AsyncSession) -> set[Provider]:
    """Providers any org holds an enabled, credentialed key for."""
    rows = await db.execute(
        select(ModelProviderConfig.provider)
        .where(ModelProviderConfig.enabled.is_(True))
        .where(
            or_(
                ModelProviderConfig.api_key_encrypted.is_not(None),
                ModelProviderConfig.aws_access_key_id_encrypted.is_not(None),
                ModelProviderConfig.bedrock_auth_mode == "iam",
            )
        )
        .distinct()
    )
    return {Provider(value) for value in rows.scalars()}


async def credentialed_providers(db: AsyncSession) -> set[Provider] | None:
    """The providers this deployment can reach, or None when the upstream
    holds the keys (proxy mode) and any provider can be reached."""
    if settings.gateway_proxy_mode:
        return None
    providers = env_configured_providers()
    if byok_active():
        providers |= await _org_keyed_providers(db)
    return providers


async def has_routable_model(db: AsyncSession) -> bool:
    """Whether at least one enabled model has an enabled route this gateway
    can reach."""
    query = (
        select(Model.id)
        .join(ModelRoute, ModelRoute.model_id == Model.id)
        .where(Model.enabled.is_(True), ModelRoute.enabled.is_(True))
    )
    providers = await credentialed_providers(db)
    if providers is not None:
        if not providers:
            return False
        query = query.where(ModelRoute.provider.in_(sorted(providers)))
    return (await db.execute(query.limit(1))).first() is not None


__all__ = ["NO_ROUTABLE_MODEL", "credentialed_providers", "has_routable_model"]
