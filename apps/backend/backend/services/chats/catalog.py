"""The model catalog a browser reader picks a chat's model from.

The gateway's ``GET /v1/models`` is the one authority on what a caller may
select: it applies the org's entitlement, the BYOK credential filter (a model
the gateway cannot actually reach is not offered) and the per-provider wire, and
none of that is re-derivable from the backend's own tables without duplicating
the gateway's rules. So the backend asks it, exactly as the CLI's
``gateway_client.fetch_models`` does — the difference is only how the caller is
identified: the CLI holds the user's own bearer token, and the browser holds a
session cookie, so a session JWT is minted for the SAME user, in the org the
request is in, and sent on the hop. Nothing widens: the token names the caller's
membership in that org and nothing else, it never leaves this process except to
the configured gateway, and it is never returned to the browser.

A gateway that cannot be reached is an OUTAGE, not an answer: it raises
:class:`CatalogUnavailableError` and every caller treats an empty catalog as
"unknown", never as "the reader has no models". That distinction is what keeps a
transient 401 from wiping a saved default (see
:func:`alkera_core.chat_defaults.resolve_chat_defaults`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from alkera_core.chat_defaults import default_effort_for, resolve_chat_defaults
from alkera_core.config import settings
from alkera_core.gateway import split_model_effort
from alkera_core.http import async_client
from alkera_core.models import User
from alkera_core.schemas.objects.specs import ChatModelPin
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.membership_tokens import mint_for_membership
from backend.services.org import preferences as preferences_service

#: Gateway families that are e2e fixtures, never real models. A dev gateway DB
#: accumulates hundreds of these from test runs; they must never appear in a
#: human model picker. The CLI filters the same families
#: (``gateway_client.selectable_models``) — one vocabulary, two pickers.
TEST_MODEL_FAMILIES = frozenset({"test", "t"})


def _catalog_timeout_seconds() -> float:
    """How long the create waits on the gateway, read per call so a deployment
    whose gateway is a region away can widen it without a release."""
    return settings.chat_model_catalog_timeout_seconds


class CatalogUnavailableError(RuntimeError):
    """The gateway could not be reached, or answered something unusable.

    Deliberately NOT an empty catalog: a caller must be able to tell "the
    gateway is down" from "this org has no models", because only the second one
    may reset a saved preference.
    """


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One model the gateway can serve for this caller.

    The fields are the catalog's own — the backend adds nothing and interprets
    nothing. ``efforts``/``default_effort`` satisfy
    :class:`alkera_core.chat_defaults.EffortModel`, which is how the same
    resolver serves the browser and the CLI.
    """

    id: str
    display_name: str
    wire: str
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    tier: str = "standard"
    family: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    reasoning_format: str | None = None
    reads_reasoning_formats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Catalog:
    models: list[CatalogModel] = field(default_factory=list)


def _non_negative_int(value: Any) -> int:
    """A non-negative int from a gateway payload field, else 0 (= unknown)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_model(item: Any) -> CatalogModel | None:
    """One catalog row, or ``None`` when it is not one this client can use.

    A row with no id, or reached over a wire this client does not know, is
    dropped rather than raised on: a newer gateway adding a third wire must not
    empty the picker.
    """
    if not isinstance(item, dict):
        return None
    model_id = item.get("id")
    wire = item.get("wire")
    if not isinstance(model_id, str) or not model_id:
        return None
    if wire not in ("anthropic", "openai"):
        return None
    display = item.get("display_name")
    tier = item.get("tier")
    family = item.get("family")
    default_effort = item.get("default_effort")
    return CatalogModel(
        id=model_id,
        display_name=display if isinstance(display, str) and display else model_id,
        wire=wire,
        efforts=tuple(e for e in (item.get("efforts") or []) if isinstance(e, str)),
        default_effort=default_effort if isinstance(default_effort, str) else None,
        tier=tier if isinstance(tier, str) and tier else "standard",
        family=family if isinstance(family, str) else "",
        context_window=_non_negative_int(item.get("context_window")),
        max_output_tokens=_non_negative_int(item.get("max_output_tokens")),
        # An older gateway names neither: the model then reads only itself.
        reasoning_format=fmt
        if isinstance(fmt := item.get("reasoning_format"), str) and fmt
        else None,
        reads_reasoning_formats=tuple(
            f for f in item.get("reads_reasoning_formats") or () if isinstance(f, str) and f
        ),
    )


def selectable(models: list[CatalogModel]) -> list[CatalogModel]:
    """The models a human may pick — the catalog with e2e fixture families out."""
    return [m for m in models if m.family not in TEST_MODEL_FAMILIES]


async def hop_token(db: AsyncSession, user: User, org_team_id: UUID) -> str:
    """A session JWT for ``user``'s membership in ``org_team_id``, minted for
    the gateway hop.

    It carries exactly the claims the caller's own credential carries — the
    same user, the same org and membership, the same platform role — so the
    gateway decides the catalog for the person who asked, in the org they
    asked from, and for nobody else. It is minted per request, never
    registered and never returned to the browser.
    """
    token, _claims = await mint_for_membership(db, user, org_team_id, kind="hop")
    return token


async def fetch_catalog(
    db: AsyncSession,
    user: User,
    org_team_id: UUID,
    *,
    client: httpx.AsyncClient | None = None,
    base_url: str | None = None,
) -> Catalog:
    """What the gateway will serve ``user`` in ``org_team_id`` (the request's
    org). Raises on an outage, never lies.

    ``client`` is injectable so a test drives the whole route against a
    ``MockTransport`` with no gateway process — the seam the money path already
    uses.
    """
    url = f"{(base_url or settings.gateway_base_url).rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {await hop_token(db, user, org_team_id)}"}
    owns = client is None
    client = client or async_client(timeout=_catalog_timeout_seconds())
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CatalogUnavailableError(f"the model gateway could not be reached: {exc}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code != 200:
        raise CatalogUnavailableError(
            f"the model gateway answered HTTP {response.status_code}",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CatalogUnavailableError("the model gateway answered with no catalog") from exc
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise CatalogUnavailableError("the model gateway answered with no catalog")
    parsed = [model for model in (_parse_model(row) for row in rows) if model is not None]
    return Catalog(models=parsed)


def find(models: list[CatalogModel], model_id: str) -> CatalogModel | None:
    return next((m for m in models if m.id == model_id), None)


__all__ = [
    "TEST_MODEL_FAMILIES",
    "Catalog",
    "CatalogModel",
    "CatalogUnavailableError",
    "ModelChoiceRefusedError",
    "NoModelOfferedError",
    "check_default_choice",
    "default_pin_for",
    "fetch_catalog",
    "find",
    "hop_token",
    "pin_for",
    "selectable",
]


def pin_for(model: CatalogModel, effort: str | None) -> ChatModelPin:
    """The catalog's facts about a model, in the shape a chat spec pins.

    Spelled once because every client that opens a chat needs it -- the web
    route from an explicit pick, the Slack receiver from the owner's saved
    default -- and a chat whose pin was built two different ways is a chat whose
    limits disagree with the gateway on one of the two paths.
    """
    return ChatModelPin(
        id=model.id,
        display_name=model.display_name,
        wire=model.wire,  # type: ignore[arg-type]  # narrowed by the catalog parser
        efforts=list(model.efforts),
        effort=effort if effort in model.efforts else default_effort_for(model),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        reasoning_format=model.reasoning_format,
        reads_reasoning_formats=list(model.reads_reasoning_formats),
    )


class NoModelOfferedError(RuntimeError):
    """The catalog answered, and offers this reader no model a chat can run on.

    Distinct from :class:`CatalogUnavailableError` so a caller can tell the
    reader "try again" from "ask an admin to enable a model".
    """


class ModelChoiceRefusedError(ValueError):
    """A Default Chat Model / Effort the catalog cannot honour, refused at SET.

    ``code`` is the machine answer the client branches on:
    ``model_catalog_unavailable`` (try again), ``model_not_offered`` (pick another
    model), ``effort_not_offered`` (pick another effort for this model).
    """

    def __init__(self, code: str, message: str, *, model: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.model = model


async def check_default_choice(
    db: AsyncSession, user: User, org_team_id: UUID, model_id: str, effort: str | None
) -> tuple[str, str | None]:
    """Refuse a saved default the live catalog does not offer this user.

    Returns ``(model_id, effort)`` normalised: a ``"<model>::<effort>"`` variant
    is split into its halves (an explicit ``effort`` wins over the variant's).
    The same catalog every reader resolves against, so a value accepted here is
    one the next new chat can actually open on. A catalog that cannot be reached
    refuses rather than guesses: storing an unchecked slug is exactly the stale
    value every reader would then have to fall back from.
    """
    base, variant_effort = split_model_effort(model_id)
    effort = effort if effort is not None else variant_effort
    try:
        catalog = await fetch_catalog(db, user, org_team_id)
    except CatalogUnavailableError as exc:
        raise ModelChoiceRefusedError(
            "model_catalog_unavailable",
            f"Can't reach the model catalog to check {base} — try again",
            model=base,
        ) from exc
    model = find(selectable(catalog.models), base)
    if model is None:
        raise ModelChoiceRefusedError(
            "model_not_offered",
            f"{base} isn't a model this workspace can run a chat on",
            model=base,
        )
    if effort is not None and effort not in model.efforts:
        offered = ", ".join(model.efforts) or "none"
        raise ModelChoiceRefusedError(
            "effort_not_offered",
            f"{model.display_name} doesn't offer the {effort} effort (offered: {offered})",
            model=base,
        )
    return base, effort


async def default_pin_for(db: AsyncSession, user: User, org_team_id: UUID) -> ChatModelPin:
    """The model a NEW chat opened on this user's behalf in ``org_team_id``
    carries.

    The same three steps ``GET /me/chat-defaults`` takes -- saved preference,
    live catalog, :func:`resolve_chat_defaults` -- so a chat started from Slack
    or from a composer that named no model opens on exactly the model the
    reader's web composer would have preselected.

    A chat ALWAYS carries a model: the box runs a chat only on a model the
    gateway serves and has no default of its own, so a chat created without one
    would sit unanswerable (or, before that was closed, run on a model nobody
    chose, metered or billed). When no model can be resolved the create is
    refused, not softened: :class:`CatalogUnavailableError` when the gateway
    cannot be reached, :class:`NoModelOfferedError` when it offers nothing.

    Nothing is written back. Correcting a stale saved default is the reader's
    own read (``/me/chat-defaults``), not a side effect of someone mentioning a
    bot.
    """
    stored = await preferences_service.load(db, user_id=user.id, org_id=org_team_id)
    catalog = await fetch_catalog(db, user, org_team_id)
    models = selectable(catalog.models)
    resolved = resolve_chat_defaults(models, stored.default_chat_model, stored.default_chat_effort)
    model = find(models, resolved.model_id) if resolved.model_id is not None else None
    if model is None:
        raise NoModelOfferedError(
            "the model catalog offers this workspace no model a chat can run on"
        )
    return pin_for(model, resolved.effort)
