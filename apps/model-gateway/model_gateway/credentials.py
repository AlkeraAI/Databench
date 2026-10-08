"""Per-request BYOK credential resolution for the gateway.

Only a BYOK-active deployment (self-hosted, direct upstream, entitled) ever
reads the per-org credential rows — everywhere else these helpers return empty
and the transport factory's settings-based behavior is byte-identical to
before. No caching, deliberately: one indexed query per request is noise next
to the existing resolve queries, and it buys zero-downtime rotation + instant
disable from the admin page.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import byok_active
from alkera_core.llm_provider import Provider
from alkera_core.model_providers import OrgProviderCredentials, resolve_all


async def request_credentials(org_team_id: UUID | None) -> dict[Provider, OrgProviderCredentials]:
    """The requesting org's usable BYOK credentials — empty unless BYOK is in
    effect for this deployment (then org rows override env keys per provider)."""
    if org_team_id is None or not byok_active():
        return {}
    async with AsyncSessionLocal() as db:
        return await resolve_all(db, org_team_id)


def env_configured_providers() -> set[Provider]:
    """Providers reachable with INSTANCE env credentials (today's direct-mode
    configuration surface)."""
    configured: set[Provider] = set()
    if settings.anthropic_api_key:
        configured.add(Provider.ANTHROPIC)
    if settings.openai_api_key:
        configured.add(Provider.OPENAI)
    if (
        settings.aws_bearer_token_bedrock
        or settings.aws_access_key_id
        or settings.gateway_assume_bedrock_iam
    ):
        configured.add(Provider.BEDROCK)
    return configured


def usable_providers(creds: dict[Provider, OrgProviderCredentials]) -> set[Provider]:
    """Providers this request can actually reach: the org's BYOK rows plus any
    instance env fallback."""
    return set(creds) | env_configured_providers()
