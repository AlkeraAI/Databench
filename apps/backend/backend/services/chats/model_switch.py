"""Which models an open chat may move to, and the refusal when it may not.

The rule and the checker live in :mod:`alkera_core.chat_models.switching`; this
module gathers its inputs for one chat: the catalog the chat's *payer* may use,
the harness a cloud chat runs (always opencode), and the chat's reasoning
*ledger*.

The payer (``membership_tokens.billed_user_of``, the chat's owner) is billed
for the turns a box with no person behind it runs, so a switch is checked
against the models the payer's plan entitles them to, never the switcher's: a
collaborator on a richer plan cannot move the owner's chat onto a model the
owner does not have, and one on a poorer plan still sees every model the
owner's chat may run. A payer who has left the org confirms no model: the
options read lists none and a switch is refused.

The ledger is derived from the transcript rather than stored: every reply the
box published carries the model that ran it (``message.created`` from opencode,
``turn.started`` from the claude agent), and every reasoning block it produced
is its own ``part.created`` (a ``reasoning`` part naming the reply's message).
So the formats the chat's history holds are the formats of the models whose
root-session replies produced reasoning; a reply that produced none (a model
run with thinking off) leaves nothing to lose. A reply with no stamp (a box
built before stamps) is taken to have run on the chat's pin, which is exact: a
box that old never moved a running agent off the pin; with no pin either, it
contributes :data:`UNATTRIBUTED_FORMAT`, which nothing reads. A claude-agent
turn cannot be tied to its parts and counts whenever it ran. A model the
catalog no longer lists contributes ``model:<id>``, a format nothing reads, so
a history the server cannot vouch for blocks a switch rather than allowing one.

Two things the transcript does not show yet count too. A turn still working
(the chat document says so) has not published its reasoning, so the pin's
format is counted as if it had. And a box too old to move a running agent
(no ``model_switch_v1``) applies a switch by reopening the agent, which
replays no reasoning across models, so on such a box a chat with any
reasoning history stays on its model.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from alkera_core.auth.tenancy import MembershipRefused
from alkera_core.chat_models.harnesses import CLOUD_HARNESS, capabilities_of
from alkera_core.chat_models.switching import (
    ModelFacts,
    SwitchVerdict,
    evaluate_switch,
    group_message_for,
    message_for,
)
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.models import ChatMessage, RealtimeDoc, User, WorkspaceObject
from alkera_core.models.compute import ComputeAllocation
from alkera_core.schemas.me_chat import (
    ChatModelCurrent,
    ChatModelOption,
    ChatModelOptions,
    ChatModelRead,
)
from alkera_core.schemas.objects import ChatModelPin
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.membership_tokens import billed_user_of
from backend.services.chats import catalog as chat_catalog
from backend.services.chats.chat_service import chat_spec_of

logger = structlog.get_logger(__name__)

#: The format a reasoning reply contributes when neither its stamp nor the
#: chat's pin says which model wrote it. No model reads it, so such a history
#: blocks every move to another model rather than allowing one.
UNATTRIBUTED_FORMAT = "unattributed"
#: How a refusal names that format.
UNATTRIBUTED_NAME = "an earlier model"


@dataclass
class ModelSwitchRefusedError(Exception):
    """A switch the server will not make: ``status`` and the error body."""

    status: int
    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def facts_of(model: chat_catalog.CatalogModel | ChatModelPin) -> ModelFacts:
    return ModelFacts(
        id=model.id,
        display_name=model.display_name,
        wire=str(model.wire),
        efforts=tuple(model.efforts),
        reasoning_format=model.reasoning_format,
        reads_reasoning_formats=frozenset(model.reads_reasoning_formats),
    )


def _read_of(model: chat_catalog.CatalogModel) -> ChatModelRead:
    return ChatModelRead(
        id=model.id,
        display_name=model.display_name,
        wire=model.wire,  # type: ignore[arg-type]  # narrowed by the catalog parser
        efforts=list(model.efforts),
        default_effort=model.default_effort,
        family=model.family,
        context_window=model.context_window,
        reasoning_format=model.reasoning_format,
        reads_reasoning_formats=list(model.reads_reasoning_formats),
    )


async def ran_models(db: AsyncSession, chat: WorkspaceObject) -> tuple[set[str], bool]:
    """The model ids whose root-session replies produced reasoning, and whether
    a reply with no stamp at all produced some (see the module doc)."""
    payload = ChatMessage.payload["payload"]
    rows = await db.execute(
        select(
            ChatMessage.kind,
            payload["model"]["model_id"].astext,
            payload["session_id"].astext,
            payload["message_id"].astext,
        ).where(
            ChatMessage.chat_id == chat.id,
            or_(
                (ChatMessage.kind == "message.created") & (ChatMessage.role == "assistant"),
                ChatMessage.kind == "turn.started",
            ),
        )
    )
    reasoned = set(
        (
            await db.execute(
                select(payload["part"]["message_id"].astext).where(
                    ChatMessage.chat_id == chat.id,
                    ChatMessage.kind == "part.created",
                    payload["part"]["type"].astext == "reasoning",
                )
            )
        )
        .scalars()
        .all()
    )
    root = str(chat.id)
    stamped: set[str] = set()
    unstamped = False
    for kind, model_id, session_id, message_id in rows.all():
        if session_id and session_id != root:
            continue  # a subagent's own session keeps its own history
        if kind == "message.created" and message_id not in reasoned:
            continue  # this reply produced no reasoning to lose
        if model_id:
            stamped.add(model_id)
        else:
            unstamped = True
    return stamped, unstamped


def ledger_of(
    stamped: Iterable[str],
    unstamped: bool,
    pin: ChatModelPin | None,
    catalog: Mapping[str, chat_catalog.CatalogModel],
) -> list[str]:
    """The reasoning formats a chat's history carries (see the module doc)."""
    ran = set(stamped)
    formats: list[str] = []
    if unstamped:
        if pin is not None and pin.id:
            ran.add(pin.id)
        else:
            formats.append(UNATTRIBUTED_FORMAT)
    for model_id in sorted(ran):
        if pin is not None and model_id == pin.id and pin.reasoning_format:
            fmt: str | None = pin.reasoning_format
        elif (known := catalog.get(model_id)) is not None:
            fmt = known.reasoning_format
        else:
            fmt = f"model:{model_id}"
        if fmt and fmt not in formats:
            formats.append(fmt)
    return formats


async def _catalog(
    db: AsyncSession, user: User, org_team_id: uuid.UUID
) -> list[chat_catalog.CatalogModel]:
    catalog = await chat_catalog.fetch_catalog(db, user, org_team_id)
    return chat_catalog.selectable(catalog.models)


class PayerGoneError(Exception):
    """The chat's payer no longer exists or stands in the chat's org, so no
    model can be confirmed for it."""


async def payer_catalog(db: AsyncSession, chat: WorkspaceObject) -> list[chat_catalog.CatalogModel]:
    """The models the chat's payer may run. Raises ``CatalogUnavailableError``
    on a gateway outage and :class:`PayerGoneError` for a payer with no row or
    with no standing in the chat's org."""
    payer = await db.get(User, billed_user_of(chat))
    if payer is None:
        raise PayerGoneError(str(chat.owner_user_id))
    # In the chat's org: what the payer may run there, never in their home org.
    try:
        return await _catalog(db, payer, chat.org_team_id)
    except MembershipRefused as exc:
        # Deactivated, or no longer a member of the chat's org: nobody can
        # vouch for a model on their plan there.
        raise PayerGoneError(str(payer.id)) from exc


def _format_names(models: Iterable[chat_catalog.CatalogModel]) -> dict[str, str]:
    names = {m.reasoning_format: m.display_name for m in models if m.reasoning_format}
    return {UNATTRIBUTED_FORMAT: UNATTRIBUTED_NAME, **names}


def _current_facts(
    pin: ChatModelPin | None, by_id: Mapping[str, chat_catalog.CatalogModel]
) -> ModelFacts | None:
    """The chat's model, with the catalog's reasoning facts when the pin is older
    than the fields that carry them."""
    if pin is None or not pin.id:
        return None
    known = by_id.get(pin.id)
    if pin.reasoning_format is None and known is not None:
        return facts_of(known)
    return facts_of(pin)


def verdict_for(
    target: chat_catalog.CatalogModel,
    *,
    current: ModelFacts | None,
    ledger: list[str],
    offered: bool,
    replays_reasoning: bool = True,
) -> SwitchVerdict:
    """The rule's verdict, narrowed for a box that cannot carry reasoning
    across a switch (``replays_reasoning`` false): any move to another model
    would drop every format the ledger holds, so each blocks it."""
    verdict = evaluate_switch(
        current,
        facts_of(target),
        ledger=ledger,
        harness_wires=capabilities_of(CLOUD_HARNESS).wires,
        offered=offered,
    )
    if verdict.state != "available" or replays_reasoning or not ledger:
        return verdict
    return SwitchVerdict(
        state="unavailable",
        reason_code="reasoning_not_readable",
        blocking_formats=tuple(sorted(set(ledger))),
        escape_new_chat_model=target.id,
    )


async def turn_working(db: AsyncSession, chat: WorkspaceObject) -> bool:
    """Whether the chat's document says a turn is running now: the live
    column first, and for a document stamped before the column existed, the
    word in its meta (the same reading the chat listing makes)."""
    found = await db.scalar(
        select(RealtimeDoc.doc_id).where(
            RealtimeDoc.doc_type == "chat",
            RealtimeDoc.doc_id == str(chat.id),
            (RealtimeDoc.turn_state == "working")
            | (
                RealtimeDoc.turn_state.is_(None)
                & (RealtimeDoc.state["meta"]["turn_state"]["state"].astext == "working")
            ),
        )
    )
    return found is not None


async def chat_ledger(
    db: AsyncSession,
    chat: WorkspaceObject,
    by_id: Mapping[str, chat_catalog.CatalogModel],
) -> list[str]:
    """The formats the chat's history carries, with the running turn's: while
    a turn works on the pin its reasoning is not on the transcript yet, and a
    switch judged without it would let that reasoning be dropped next turn."""
    pin = chat_spec_of(chat).model
    stamped, unstamped = await ran_models(db, chat)
    ledger = ledger_of(stamped, unstamped, pin, by_id)
    running = _current_facts(pin, by_id)
    if running is not None and running.reasoning_format and await turn_working(db, chat):
        if running.reasoning_format not in ledger:
            ledger.append(running.reasoning_format)
    return ledger


async def switch_applies(db: AsyncSession, chat: WorkspaceObject) -> str:
    """When a switch takes effect: on the next turn when the chat's box reports
    it can move a running agent (or no box holds the chat: the next open spawns
    on the new pin), else once its agent restarts."""
    machine_id = chat_spec_of(chat).machine_id
    if not machine_id:
        return "next_turn"
    try:
        alloc = await db.get(ComputeAllocation, uuid.UUID(machine_id))
    except ValueError:
        return "next_turn"
    if alloc is None or BoxCapability.MODEL_SWITCH_V1 in (alloc.capabilities_json or []):
        return "next_turn"
    return "after_reopen"


async def model_options(
    db: AsyncSession, chat: WorkspaceObject, user: User, *, can_switch: bool
) -> ChatModelOptions:
    """Every model the chat's payer may run, with the verdict on moving the chat
    to it. An unreachable catalog lists nothing (and never fails the read).
    ``billed_to_owner`` tells a caller who is not the payer whose plan the
    list is."""
    applies = await switch_applies(db, chat)
    pin = chat_spec_of(chat).model
    current = ChatModelCurrent(model_id=pin.id if pin else None, effort=pin.effort if pin else None)
    billed_to_owner = billed_user_of(chat) != user.id
    try:
        models = await payer_catalog(db, chat)
    except (chat_catalog.CatalogUnavailableError, PayerGoneError):
        return ChatModelOptions(
            current=current,
            can_switch=can_switch,
            applies=applies,  # type: ignore[arg-type]
            billed_to_owner=billed_to_owner,
        )
    by_id = {m.id: m for m in models}
    ledger = await chat_ledger(db, chat, by_id)
    names = _format_names(models)
    current_facts = _current_facts(pin, by_id)
    replays = applies == "next_turn"
    options: list[ChatModelOption] = []
    for model in models:
        verdict = verdict_for(
            model, current=current_facts, ledger=ledger, offered=True, replays_reasoning=replays
        )
        options.append(
            ChatModelOption(
                model=_read_of(model),
                state=verdict.state,
                reason_code=verdict.reason_code,
                message=message_for(verdict, facts_of(model), format_names=names),
                group_message=group_message_for(verdict, format_names=names),
                escape_new_chat_model=verdict.escape_new_chat_model,
            )
        )
    return ChatModelOptions(
        current=current,
        can_switch=can_switch,
        applies=applies,  # type: ignore[arg-type]
        options=options,
        billed_to_owner=billed_to_owner,
    )


async def plan_switch(
    db: AsyncSession,
    chat: WorkspaceObject,
    user: User,
    *,
    model_id: str,
    effort: str | None,
) -> tuple[ChatModelPin, list[str]]:
    """The pin a switch to ``model_id`` writes and the ledger it was checked
    against, or :class:`ModelSwitchRefusedError`: 422 for a model the payer's
    catalog cannot confirm (as a create refuses it) or an effort the model does
    not offer, 409 for a switch the rule refuses. Nothing is written either
    way. ``user`` is the switcher, recorded by the caller; it never decides
    which models are offered."""
    try:
        return await _plan_switch(db, chat, model_id=model_id, effort=effort)
    except ModelSwitchRefusedError as refused:
        logger.info(
            "chat.model_switch.refused",
            chat_id=str(chat.id),
            org_id=str(chat.org_team_id),
            switcher_id=str(user.id),
            model=model_id,
            effort=effort,
            status=refused.status,
            code=refused.code,
        )
        raise


async def _plan_switch(
    db: AsyncSession, chat: WorkspaceObject, *, model_id: str, effort: str | None
) -> tuple[ChatModelPin, list[str]]:
    try:
        models = await payer_catalog(db, chat)
    except PayerGoneError as exc:
        raise ModelSwitchRefusedError(
            422,
            "model_not_offered",
            f"{model_id} can't be confirmed for this chat's owner",
            {"model": model_id},
        ) from exc
    except chat_catalog.CatalogUnavailableError as exc:
        raise ModelSwitchRefusedError(
            422,
            "model_catalog_unavailable",
            f"Can't reach the model catalog to move this chat onto {model_id} — try again",
            {"model": model_id},
        ) from exc
    by_id = {m.id: m for m in models}
    target = by_id.get(model_id)
    if target is None:
        raise ModelSwitchRefusedError(
            422,
            "model_not_offered",
            f"{model_id} isn't a model this chat's owner can run",
            {"model": model_id},
        )
    if effort is not None and effort not in target.efforts:
        offered = ", ".join(target.efforts) or "none"
        raise ModelSwitchRefusedError(
            422,
            "effort_not_offered",
            f"{target.display_name} doesn't offer the {effort!r} effort (offers: {offered})",
            {"model": model_id, "effort": effort, "efforts": list(target.efforts)},
        )
    pin = chat_spec_of(chat).model
    ledger = await chat_ledger(db, chat, by_id)
    verdict = verdict_for(
        target,
        current=_current_facts(pin, by_id),
        ledger=ledger,
        offered=True,
        replays_reasoning=await switch_applies(db, chat) == "next_turn",
    )
    if not verdict.allowed:
        message = message_for(verdict, facts_of(target), format_names=_format_names(models))
        raise ModelSwitchRefusedError(
            409,
            str(verdict.reason_code),
            message or "",
            {
                "model": model_id,
                "blocking_formats": list(verdict.blocking_formats),
                "escape_new_chat_model": verdict.escape_new_chat_model,
            },
        )
    return chat_catalog.pin_for(target, effort), ledger


__all__ = [
    "UNATTRIBUTED_FORMAT",
    "ModelSwitchRefusedError",
    "PayerGoneError",
    "chat_ledger",
    "facts_of",
    "ledger_of",
    "model_options",
    "payer_catalog",
    "plan_switch",
    "ran_models",
    "switch_applies",
    "turn_working",
]
