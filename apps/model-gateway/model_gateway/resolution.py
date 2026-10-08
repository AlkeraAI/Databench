"""Resolve a request to its provider routes.

Routing returns the *ordered, codec-compatible* candidate list so the pipeline
can fail over from a down provider to the next. Who funds the request is the
meter's question.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alkera_core.llm_provider import Provider
from alkera_core.models.model_catalog import Model, ModelRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_routes(
    db: AsyncSession, model_id: str, providers: Sequence[Provider]
) -> list[ModelRoute]:
    """Enabled routes of an ENABLED model whose provider is codec-compatible with
    the ingress, ordered by `priority` (primary first) for failover.

    The model's own ``enabled`` flag is enforced here, not just in the ``/v1/models``
    listing: a slug the operator has staged, deprecated or withdrawn is a public
    vendor name, so filtering it out of the listing alone leaves it servable to
    anyone who types it. A newly added model is exactly this shape by default —
    routes are created enabled while the model is created disabled — and a staged
    model usually has routes before it has sell prices, which would make it
    unpriced (and therefore unmetered) inference on Alkera's provider keys.
    Returning no routes makes a disabled model answer identically to an unknown
    one, so the two can't be told apart from outside."""
    result = await db.execute(
        select(ModelRoute)
        .join(Model, Model.id == ModelRoute.model_id)
        .where(
            ModelRoute.model_id == model_id,
            ModelRoute.enabled.is_(True),
            Model.enabled.is_(True),
            ModelRoute.provider.in_(list(providers)),
        )
        .order_by(ModelRoute.priority.asc())
    )
    return list(result.scalars().all())


async def list_entitled_models(
    db: AsyncSession, org_id: UUID | None, *, usable: set[Provider] | None = None
) -> list[Model]:
    """Enabled models that have at least one enabled route (i.e. the gateway can
    actually serve them). ``usable`` narrows routes to providers with usable
    credentials (the BYOK filter); None = no narrowing. Per-org entitlement
    filtering is a future hook; for now every enabled, routable model is offered
    to every authenticated caller."""
    routable_q = select(ModelRoute.model_id).where(ModelRoute.enabled.is_(True))
    if usable is not None:
        routable_q = routable_q.where(ModelRoute.provider.in_(list(usable)))
    result = await db.execute(
        select(Model)
        .where(Model.enabled.is_(True), Model.id.in_(routable_q))
        .order_by(Model.id.asc())
    )
    return list(result.scalars().all())


def provider_wire(provider: Provider) -> str:
    """The client-side wire protocol a provider speaks — i.e. which opencode
    provider (alkera-anthropic vs alkera-openai) a model must be filed under.
    Anthropic + Bedrock both speak the Anthropic Messages protocol."""
    return "openai" if provider is Provider.OPENAI else "anthropic"


async def list_models_with_wire(
    db: AsyncSession, org_id: UUID | None, *, usable: set[Provider] | None = None
) -> list[tuple[Model, str]]:
    """Entitled models tagged with their wire protocol, taken from the *primary*
    (lowest-priority) enabled route — so the client knows which ingress / opencode
    provider each model is reached through. With ``usable`` set (BYOK), only
    routes to credentialed providers count — for the listing AND the wire."""
    models = await list_entitled_models(db, org_id, usable=usable)
    if not models:
        return []
    routes_q = (
        select(ModelRoute.model_id, ModelRoute.provider)
        .where(
            ModelRoute.enabled.is_(True),
            ModelRoute.model_id.in_([m.id for m in models]),
        )
        .order_by(ModelRoute.priority.asc())
    )
    if usable is not None:
        routes_q = routes_q.where(ModelRoute.provider.in_(list(usable)))
    rows = (await db.execute(routes_q)).all()
    wire_by_model: dict[str, str] = {}
    for model_id, provider in rows:
        wire_by_model.setdefault(model_id, provider_wire(provider))  # primary wins
    return [(m, wire_by_model.get(m.id, "anthropic")) for m in models]
