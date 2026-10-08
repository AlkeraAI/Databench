"""The model a new chat is pinned to, as the create routes resolve it.

A chat is never created without a model the gateway serves this org: a pick is
resolved against the live catalog, no pick falls back to the reader's default,
and a template's pin is a suggestion checked the same way. Each refusal is a
422 with a code a client can act on.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.schemas.objects import ChatModelPin, ChatTemplateSpec
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.chats import catalog as chat_catalog

log = get_logger(__name__)


async def default_model_pin(db: AsyncSession, user: User, *, org_id: UUID) -> ChatModelPin:
    """The model a create that named none opens on: the reader's saved default
    resolved against the live catalog, else the catalog's first model — the
    same answer ``GET /me/chat-defaults`` gives their composer.

    A chat is never created without one. The box runs a chat only on a model the
    gateway serves and has no default of its own, so "no pin" is not a chat that
    runs on something sensible — it is a chat that cannot answer, or (before
    that was closed) one that answered on a model nobody chose, metered or
    billed. Refused with the reason, so the reader can tell "try again" (the
    catalog could not be reached) from "ask an admin" (it offers nothing).
    """
    try:
        return await chat_catalog.default_pin_for(db, user, org_id)
    except chat_catalog.CatalogUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "model_catalog_unavailable",
                "message": (
                    "Can't reach the model catalog to choose a model for this chat — "
                    "try again in a moment"
                ),
                "model": "",
            },
        ) from exc
    except chat_catalog.NoModelOfferedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "no_model_offered",
                "message": (
                    "This workspace has no model a chat can run on — ask an org admin to enable one"
                ),
                "model": "",
            },
        ) from exc


async def resolve_model_pin(
    db: AsyncSession, user: User, *, org_id: UUID, model_id: str | None, effort: str | None
) -> ChatModelPin:
    """The model the reader picked, as the catalog describes it — or, when they
    picked none, the one their composer would have preselected.

    Resolved HERE rather than taken on trust: the id has to be one the gateway
    will actually serve this org, and the box needs the catalog's facts (the
    wire, the offered efforts, the limits) to file the model under the right
    provider — it cannot look them up itself at open time on a gateway it may
    not reach. A chat pinned to a model that does not resolve would fail on its
    first turn with nothing to say why, so an unknown id is refused here.

    Choosing nothing is not an error either, but it is no longer "no pin": the
    box has no default of its own, so a chat created with none could not run a
    turn. It resolves through :func:`default_model_pin` instead — and an
    effort named WITHOUT a model is applied to the model that resolves, when
    it offers that effort: the composer names the effort off its preselected
    model and can send the pick before the catalog it names the model from
    has loaded, so dropping the effort there would open the chat at the
    default effort under a chip that promised another. A gateway that
    is DOWN is refused in both cases: a reader who picked a model and got a 201
    saying ``model: null`` had been told their choice was honoured while the
    chat ran on a model nobody chose. The refusal names the model when there is
    one, because the reader picked it by its display name and the id is what
    they must quote.
    """
    if not model_id:
        pin = await default_model_pin(db, user, org_id=org_id)
        if effort and effort in pin.efforts:
            return pin.model_copy(update={"effort": effort})
        return pin
    try:
        catalog = await chat_catalog.fetch_catalog(db, user, org_id)
    except chat_catalog.CatalogUnavailableError as exc:
        # 422 and not 503, though the fault is the server's: the app's error
        # envelope only carries a `{code, message}` through for a 4xx — a 5xx
        # reaches the client as a bare `internal_error`, and this refusal is
        # useless unless the client can tell "unreachable, retry the same pick"
        # from "not offered, pick another".
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "model_catalog_unavailable",
                "message": (
                    f"Can't reach the model catalog to start this chat on {model_id} — "
                    "try again, or start it without picking a model"
                ),
                "model": model_id,
            },
        ) from exc
    model = chat_catalog.find(chat_catalog.selectable(catalog.models), model_id)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "model_not_offered",
                "message": f"{model_id} isn't a model this workspace can run a chat on",
                "model": model_id,
            },
        )
    return chat_catalog.pin_for(model, effort)


async def template_pin(
    db: AsyncSession, user: User, template: ChatTemplateSpec, *, org_id: UUID
) -> ChatModelPin:
    """The model a chat started from this template runs on.

    The template's pin is a SUGGESTION and is resolved through the same catalog
    check any picked model goes through, so a template saved a year ago cannot
    open a chat on a model the gateway no longer serves. When it no longer
    resolves the reader's own preference decides instead — the answer carries
    the pin actually used, so nobody is told a model was honoured that was not.
    """
    if template.model is not None:
        try:
            return await resolve_model_pin(
                db, user, org_id=org_id, model_id=template.model.id, effort=template.model.effort
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_422_UNPROCESSABLE_ENTITY:
                raise
    return await resolve_model_pin(db, user, org_id=org_id, model_id=None, effort=None)


async def saved_default_pin(
    db: AsyncSession, owner: User, *, org_id: UUID, chat_id: UUID
) -> ChatModelPin | None:
    """``owner``'s default model in ``org_id`` for a chat that predates pins, or
    ``None`` (logged) when the catalog cannot be reached or offers nothing:
    such a chat stays unpinned until a later read can resolve it."""
    try:
        return await chat_catalog.default_pin_for(db, owner, org_id)
    except (chat_catalog.CatalogUnavailableError, chat_catalog.NoModelOfferedError) as exc:
        log.info("chat.legacy_pin.unresolved", chat_id=str(chat_id), reason=str(exc))
        return None


__all__ = ["default_model_pin", "resolve_model_pin", "saved_default_pin", "template_pin"]
