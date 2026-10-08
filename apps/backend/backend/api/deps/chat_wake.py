"""The one wake a person asks of a chat, as every route that asks it answers.

Opening a chat, opening a workspace with no chat, and a notebook action that
needs the notebook's folder on a box all ask the same wake: the chat policy
decides the caller may ``SEND`` (a wake spends the org's compute), a chat whose
workspace lost its machine is answered with what was lost instead of being
woken, and :mod:`backend.services.chats.wake_intent` owns the wake and its
throttle. The routes decide which chat they mean and shape their answer; the
wake itself is this module's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.models import User, WorkspaceObject
from alkera_core.objects import chat_spares
from alkera_core.schemas.objects import ChatWakeRead
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.compute import refused
from backend.authz import enforce, role_resolver
from backend.services import chats as chat_domain
from backend.services import compute, sharing, workspaces


async def wake_chat_as(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    chat: WorkspaceObject,
    resource: Resource,
    attrs: Mapping[str, object],
    *,
    user: User,
    reader: sharing.Reader,
) -> ChatWakeRead:
    """The wake for a chat the policy lets this caller send in. A chat whose
    workspace lost its machine is not woken: the opener is answered with what
    was lost and where they may wake it instead. A start admission refuses is
    raised as the compute refusal's own ``402`` / ``429``."""
    standing = await chat_domain.wake_standing(db, chat=chat)
    if standing is not None:
        return ChatWakeRead(outcome=standing)
    await enforce(request, db, ctx, Action.SEND, resource, attrs)
    roles = role_resolver(request, db, ctx)
    held = await workspaces.wake_held_for_choice(
        db,
        viewer=await compute.org_machine_viewer(db, ctx=ctx, user=user, resolver=roles),
        chat=chat,
        workspace_attrs=lambda workspace: sharing.workspace_attrs(db, workspace, reader),
    )
    if held is not None:
        await db.commit()
        return ChatWakeRead(outcome="machine_unavailable", machine_unavailable=held)
    try:
        outcome = await chat_domain.wake_on_open(db, chat=chat, ctx=ctx)
    except chat_domain.WakeRefusedError as exc:
        raise refused(exc.refusal) from exc
    return ChatWakeRead(outcome=outcome)


async def wake_first_sendable(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    chats: Sequence[WorkspaceObject],
    *,
    user: User,
    reader: sharing.Reader,
) -> tuple[WorkspaceObject, ChatWakeRead] | None:
    """Wake the first of ``chats`` (most recently active first) this caller
    may read and send in: that chat and the wake's answer. ``None`` when they
    may send in none of them, and then nothing is woken. A spare nobody has
    claimed is never woken."""
    picked = await first_sendable(db, ctx, chats, reader=reader)
    if picked is None:
        return None
    chat, attrs = picked
    resource = sharing.object_resource(chat, type=ResourceType.CHAT)
    woke = await wake_chat_as(request, db, ctx, chat, resource, attrs, user=user, reader=reader)
    return chat, woke


async def first_sendable(
    db: AsyncSession,
    ctx: ActingContext,
    chats: Sequence[WorkspaceObject],
    *,
    reader: sharing.Reader,
) -> tuple[WorkspaceObject, Mapping[str, object]] | None:
    """The first of ``chats`` (most recently active first) this caller may
    read and send in, with its policy facts: the chat a wake goes through,
    and so the chat a notebook's editor opens in. ``None`` when they may send
    in none of them. A spare nobody has claimed is never picked."""
    for chat, attrs in await sharing.readable_chat_facts(db, chats, reader):
        if chat_spares.is_spare(chat) or not chat_domain.may_send(ctx, chat, attrs):
            continue
        return chat, attrs
    return None


async def reader_of(
    request: Request, db: AsyncSession, ctx: ActingContext, user: User
) -> sharing.Reader:
    """Who is asking, as the sharing reads resolve them."""
    return await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )


__all__ = [
    "first_sendable",
    "reader_of",
    "wake_chat_as",
    "wake_first_sendable",
]
