"""The reader's own chat settings: the model catalog, the new-chat seed, and
the preferences behind both.

Everything here is self-scoped — the caller reads and writes their OWN row and
nothing else, so there is no resource to decide against and no policy: the
authenticated identity IS the authorization, the same way ``/me/credits`` works.
A caller can no more name another user here than they can on the cookie.

Two stores hold one shape. ``~/.alkera/preferences.yml`` is the desktop's copy,
written by the CLI and the daemon; the row behind these routes is the server's,
and it governs chats started in the BROWSER. They are deliberately not synced —
a sync needs a conflict rule, and a wrong one silently overwrites a setting the
person chose on the other surface. What they do share is
:class:`alkera_core.schemas.preferences.Preferences`, so a field added for one
surface is readable by the other the day it ships.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from alkera_core.chat_defaults import resolve_chat_defaults
from alkera_core.models import User
from alkera_core.schemas.me_chat import (
    ChatDefaultsRead,
    ChatModelList,
    ChatModelRead,
    PreferencesRead,
    PreferencesUpdate,
)
from alkera_core.schemas.preferences import Preferences
from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError

from backend.auth.dependencies import CurrentOrg, CurrentUser, DbSession
from backend.services.chats import catalog as chat_catalog
from backend.services.org import preferences as preferences_service

router = APIRouter(prefix="/api/v1/me", tags=["me-chat"])


async def _resolved(
    db: DbSession, caller: User, org_id: UUID, stored: Preferences
) -> PreferencesRead:
    """The stored document with its Default Chat Model resolved for reading.

    The saved pick comes back as saved while the catalog still offers it; a pick
    that no longer resolves, or none at all, reads as the model a new chat would
    actually open on — the one resolver's answer, flagged ``chat_model_defaulted``
    so an editing client can show "no preference" instead of pinning it. An
    outage answers the document as stored: nothing to resolve against, and a
    read must not fail because the gateway blinked. Nothing is written.
    """
    document = stored.model_dump(mode="json")
    try:
        catalog = await chat_catalog.fetch_catalog(db, caller, org_id)
    except chat_catalog.CatalogUnavailableError:
        return PreferencesRead(preferences=document)
    selectable = chat_catalog.selectable(catalog.models)
    if not selectable:
        return PreferencesRead(preferences=document)
    resolved = resolve_chat_defaults(
        selectable, stored.default_chat_model, stored.default_chat_effort
    )
    document["default_chat_model"] = resolved.model_id
    document["default_chat_effort"] = resolved.effort
    return PreferencesRead(preferences=document, chat_model_defaulted=resolved.defaulted)


def _refused(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "message": message, **extra},
    )


async def _checked_choice(
    db: DbSession, caller: User, org_id: UUID, stored: Preferences, incoming: dict[str, Any]
) -> dict[str, Any]:
    """``incoming`` with its Default Chat Model checked against the live catalog.

    Only a pick that CHANGES is checked: every save of the Settings page sends
    the model it loaded, and a gateway outage must not stop somebody saving
    their permission stance. Clearing the pick (``null``) is always allowed — it
    is how a reader goes back to the platform default. A ``"<model>::<effort>"``
    variant is stored as its two halves.
    """
    if "default_chat_model" not in incoming and "default_chat_effort" not in incoming:
        return incoming
    raw_model = incoming.get("default_chat_model", stored.default_chat_model)
    raw_effort = incoming.get("default_chat_effort", stored.default_chat_effort)
    model = raw_model.strip() or None if isinstance(raw_model, str) else raw_model
    effort = raw_effort.strip() or None if isinstance(raw_effort, str) else raw_effort
    if model is None:
        return incoming
    if not isinstance(model, str) or (effort is not None and not isinstance(effort, str)):
        return incoming  # the schema refuses a non-string with its own 422
    if model == stored.default_chat_model and effort == stored.default_chat_effort:
        return incoming
    try:
        base, checked_effort = await chat_catalog.check_default_choice(
            db, caller, org_id, model, effort
        )
    except chat_catalog.ModelChoiceRefusedError as exc:
        raise _refused(exc.code, exc.message, model=exc.model) from exc
    return {**incoming, "default_chat_model": base, "default_chat_effort": checked_effort}


@router.get("/preferences", response_model=PreferencesRead)
async def my_preferences(caller: CurrentUser, org_id: CurrentOrg, db: DbSession) -> PreferencesRead:
    """This caller's preferences, with the Default Chat Model resolved.

    A caller who has never saved one gets the DEFAULT document rather than a
    404: "nothing stored" and "the defaults" are the same state, and a client
    that had to branch on which would end up spelling the defaults a second
    time. The saved Default Chat Model reads through the same resolver a new
    chat does, so a model since retired, disabled or never real reads as the
    platform default instead of a value no chat can open on.
    """
    stored = await preferences_service.load(db, user_id=caller.id, org_id=org_id)
    return await _resolved(db, caller, org_id, stored)


@router.patch("/preferences", response_model=PreferencesRead)
async def update_my_preferences(
    payload: PreferencesUpdate, caller: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> PreferencesRead:
    """Apply these fields ON TOP of what is stored, and answer with the whole.

    A MERGE, not a replacement — the same rule the daemon's ``preferences.set``
    obeys. Two clients of different ages write this one document, and a replace
    would let the older one, which cannot even name the newer one's field,
    delete a setting the person chose in a client it has never seen.

    A value the schema refuses — a permission mode that is not one, a timeout
    that is not a number — is a 422 naming the field, not a stored value for
    some later reader to trip over. So is a Default Chat Model the catalog does
    not offer (``model_not_offered``), an effort that model does not offer
    (``effort_not_offered``), or a new pick made while the catalog cannot be
    reached to check it (``model_catalog_unavailable``).
    """
    stored = await preferences_service.load(db, user_id=caller.id, org_id=org_id)
    incoming = await _checked_choice(db, caller, org_id, stored, payload.preferences)
    try:
        merged = await preferences_service.merge(
            db, user_id=caller.id, org_id=org_id, incoming=incoming
        )
    except ValidationError as exc:
        raise _refused(
            "invalid_preference",
            "That value isn't one this preference accepts",
            errors=[
                {"field": ".".join(str(part) for part in error["loc"]), "reason": error["msg"]}
                for error in exc.errors()
            ],
        ) from exc
    await db.commit()
    return await _resolved(db, caller, org_id, merged)


@router.get("/chat-models", response_model=ChatModelList)
async def my_chat_models(caller: CurrentUser, org_id: CurrentOrg, db: DbSession) -> ChatModelList:
    """The models this caller may start a chat on.

    The gateway decides it — entitlement, BYOK credentials, routes — and this is
    a proxy, not a second opinion. A gateway that cannot be reached answers an
    EMPTY list rather than a 502: the composer polls while the catalog is empty
    and self-heals, which is what the editor's picker already does, and a failed
    page load would leave the reader with no chat at all.
    """
    try:
        catalog = await chat_catalog.fetch_catalog(db, caller, org_id)
    except chat_catalog.CatalogUnavailableError:
        return ChatModelList(items=[])
    return ChatModelList(
        items=[
            ChatModelRead(
                id=model.id,
                display_name=model.display_name,
                wire=model.wire,  # type: ignore[arg-type]  # narrowed by the parser
                efforts=list(model.efforts),
                default_effort=model.default_effort,
                family=model.family,
                context_window=model.context_window,
                reasoning_format=model.reasoning_format,
                reads_reasoning_formats=list(model.reads_reasoning_formats),
            )
            for model in chat_catalog.selectable(catalog.models)
        ]
    )


@router.get("/chat-defaults", response_model=ChatDefaultsRead)
async def my_chat_defaults(
    caller: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> ChatDefaultsRead:
    """The model + effort a NEW chat should open on, resolved against the live catalog.

    The rules are :func:`alkera_core.chat_defaults.resolve_chat_defaults` — the
    same ones the CLI and the editor obey — and the one that matters here is the
    outage branch: when the gateway cannot be reached the saved values come back
    UNTOUCHED and nothing is written. A resolver that "reset to the first model"
    on a transient 401 would wipe the reader's default on a hiccup they never
    saw.

    A correction (the saved model is no longer offered) IS persisted, so the
    value converges instead of being re-corrected on every read — as "no
    pick", so the reader follows the platform default from then on. A reader
    who never picked is not written at all: the default they land on is the
    platform's, and it must still move when the platform's does.

    The stance rides along because a composer with no chat yet still displays
    one, and the only honest source for it is the resolver the create route
    obeys — :func:`preferences_service.starting_cloud_mode`. It is answered on
    the outage branch too: an outage costs the reader their model seed, and must
    not also cost them the stance their next chat will run in.

    Both branches read the stance LAST, after any correction this read persists,
    because a correction writes the reader's preference row — and a stance read
    before that write can name one the very next create disagrees with.
    """
    stored = await preferences_service.load(db, user_id=caller.id, org_id=org_id)
    try:
        catalog = await chat_catalog.fetch_catalog(db, caller, org_id)
    except chat_catalog.CatalogUnavailableError:
        return ChatDefaultsRead(
            model=stored.default_chat_model,
            effort=stored.default_chat_effort,
            permission_mode=await preferences_service.starting_cloud_mode(
                db, user_id=caller.id, org_id=org_id
            ),
        )

    selectable = chat_catalog.selectable(catalog.models)
    resolved = resolve_chat_defaults(
        selectable, stored.default_chat_model, stored.default_chat_effort
    )
    if resolved.reset and selectable:
        await preferences_service.merge(
            db,
            user_id=caller.id,
            org_id=org_id,
            incoming={
                "default_chat_model": resolved.saved_model,
                "default_chat_effort": resolved.saved_effort,
            },
        )
        await db.commit()
    return ChatDefaultsRead(
        model=resolved.model_id,
        effort=resolved.effort,
        permission_mode=await preferences_service.starting_cloud_mode(
            db, user_id=caller.id, org_id=org_id
        ),
    )
