"""What else goes when a person leaves an org, a team is deleted, or an org is,
and what a team holds that its deletion must not destroy.

The org services own those three moments; resources that hang off a person, a
team or an org (an org machine's audience, the team that holds a machine, the
machines themselves) register what must happen to them here, and each moment
calls one function, inside its own transaction. A resource added later is one
registration, with no edit to the org services.

An open resource registers when its module is imported, and the package that
owns it imports that module (``backend.services.compute`` imports the org
machine service), so any process that loads the resource's domain holds its
hooks. A private domain registers from its extension's ``install``. Each moment
is an :class:`~alkera_core.extensions.ExtensionPoint`, so a hook registered
after the moment first ran is refused rather than silently missed, and two
hooks under one name are refused at the first run. A moment never runs without
the hooks :data:`REQUIRED` names: a process that never loaded them is refused,
rather than deleting a person, team or org and leaving what hung off it behind.

A domain whose rows hang off a team on a cascading key, and are not the kind
of thing a deletion may hand up or throw away (a stored connection
credential), registers a :class:`TeamDeleteBlocker`. Every blocker is asked
before any team-deleted hook runs, so a refused deletion has changed nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from uuid import UUID

from alkera_core.extensions import ExtensionError, ExtensionPoint
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class MemberLeft:
    """A person removed from, or deactivated in, an org."""

    org_id: UUID
    user_id: UUID


@dataclass(frozen=True, slots=True)
class TeamDeleted:
    """A non-root team about to be deleted, and the team that takes what it held."""

    org_id: UUID
    team_id: UUID
    parent_team_id: UUID


@dataclass(frozen=True, slots=True)
class OrgDeleted:
    """An org about to be purged."""

    org_id: UUID
    actor: Mapping[str, Any] | None


MemberHook = Callable[[AsyncSession, MemberLeft], Awaitable[None]]
TeamHook = Callable[[AsyncSession, TeamDeleted], Awaitable[None]]
OrgHook = Callable[[AsyncSession, OrgDeleted], Awaitable[None]]

E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class NamedHook(Generic[E]):
    """A hook and the name :data:`REQUIRED` and :func:`registered` know it by."""

    name: str
    run: Callable[[AsyncSession, E], Awaitable[None]]


#: The hooks each moment must hold before it runs, by registered name.
REQUIRED: Mapping[str, frozenset[str]] = {
    "member_left": frozenset({"org_machines.audience"}),
    "team_deleted": frozenset({"org_machines.owner"}),
    "org_deleted": frozenset({"org_machines.machines"}),
}

MEMBER_LEFT: ExtensionPoint[NamedHook[MemberLeft]] = ExtensionPoint("org_member_left")
TEAM_DELETED: ExtensionPoint[NamedHook[TeamDeleted]] = ExtensionPoint("org_team_deleted")
ORG_DELETED: ExtensionPoint[NamedHook[OrgDeleted]] = ExtensionPoint("org_deleted")


class HooksMissingError(RuntimeError):
    """A moment ran in a process that never imported a resource's hooks."""


@dataclass(frozen=True, slots=True)
class TeamDeleteBlocker:
    """Something a team can hold that keeps it from being deleted: ``refusal``
    answers the reason the admin is told, or ``None`` when the team holds none."""

    name: str
    refusal: Callable[[AsyncSession, UUID], Awaitable[str | None]]


TEAM_DELETE_BLOCKERS: ExtensionPoint[TeamDeleteBlocker] = ExtensionPoint("org_team_delete_blockers")


async def team_delete_refusal(db: AsyncSession, *, team_id: UUID) -> str | None:
    """The first registered blocker's reason ``team_id`` cannot be deleted yet,
    or ``None``."""
    for blocker in TEAM_DELETE_BLOCKERS.items():
        reason = await blocker.refusal(db, team_id)
        if reason is not None:
            return reason
    return None


def on_member_left(name: str) -> Callable[[MemberHook], MemberHook]:
    def register(hook: MemberHook) -> MemberHook:
        MEMBER_LEFT.register(NamedHook(name, hook))
        return hook

    return register


def on_team_deleted(name: str) -> Callable[[TeamHook], TeamHook]:
    def register(hook: TeamHook) -> TeamHook:
        TEAM_DELETED.register(NamedHook(name, hook))
        return hook

    return register


def on_org_deleted(name: str) -> Callable[[OrgHook], OrgHook]:
    def register(hook: OrgHook) -> OrgHook:
        ORG_DELETED.register(NamedHook(name, hook))
        return hook

    return register


def hooks_of(point: ExtensionPoint[NamedHook[E]]) -> dict[str, NamedHook[E]]:
    """The hooks registered on ``point`` by name, in registration order.
    Freezes the point; refuses two hooks under one name."""
    hooks: dict[str, NamedHook[E]] = {}
    for hook in point.items():
        if hook.name in hooks:
            raise ExtensionError(f"two hooks are registered as {hook.name!r} on {point.name!r}")
        hooks[hook.name] = hook
    return hooks


def _require(moment: str, registered: Mapping[str, object]) -> None:
    missing = REQUIRED[moment] - set(registered)
    if missing:
        raise HooksMissingError(
            f"{moment} has no hook registered for {', '.join(sorted(missing))}; "
            "the module that registers it was never imported"
        )


async def member_left(db: AsyncSession, *, org_id: UUID, user_id: UUID) -> None:
    hooks = hooks_of(MEMBER_LEFT)
    _require("member_left", hooks)
    event = MemberLeft(org_id=org_id, user_id=user_id)
    for hook in hooks.values():
        await hook.run(db, event)


async def team_deleted(
    db: AsyncSession, *, org_id: UUID, team_id: UUID, parent_team_id: UUID
) -> None:
    hooks = hooks_of(TEAM_DELETED)
    _require("team_deleted", hooks)
    event = TeamDeleted(org_id=org_id, team_id=team_id, parent_team_id=parent_team_id)
    for hook in hooks.values():
        await hook.run(db, event)


async def org_deleted(
    db: AsyncSession, *, org_id: UUID, actor: Mapping[str, Any] | None = None
) -> None:
    hooks = hooks_of(ORG_DELETED)
    _require("org_deleted", hooks)
    event = OrgDeleted(org_id=org_id, actor=actor)
    for hook in hooks.values():
        await hook.run(db, event)


def registered() -> dict[str, tuple[str, ...]]:
    """The names registered per moment, for a test to pin. Freezes them."""
    return {
        "member_left": tuple(hooks_of(MEMBER_LEFT)),
        "team_deleted": tuple(hooks_of(TEAM_DELETED)),
        "org_deleted": tuple(hooks_of(ORG_DELETED)),
    }


__all__ = [
    "MEMBER_LEFT",
    "ORG_DELETED",
    "REQUIRED",
    "TEAM_DELETED",
    "TEAM_DELETE_BLOCKERS",
    "HooksMissingError",
    "MemberLeft",
    "NamedHook",
    "OrgDeleted",
    "TeamDeleteBlocker",
    "TeamDeleted",
    "hooks_of",
    "member_left",
    "on_member_left",
    "on_org_deleted",
    "on_team_deleted",
    "org_deleted",
    "registered",
    "team_delete_refusal",
    "team_deleted",
]
