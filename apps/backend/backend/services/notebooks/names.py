"""How the notebook routes name who did something.

Every answer that says who edited, ran, holds a caret or is in a cell names
them in words a reader knows: a person by their name, an agent as "<the
brand's agent name> for <the person's name>" (the person it acts for), the
platform itself by the product's name. A raw id (``user:<uuid>``, ``agent:<chat
id>``) is never a name.

Names are resolved when answering, not when recorded, so a renamed person
reads under their new name. What a row recorded is the last known name: it
answers for a person no longer in the users table, and "A former member"
answers when nothing usable was recorded.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from alkera_core import brand
from alkera_core.models import User
from alkera_core.notebooks.schemas import ActingForRef, ActorRef
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt import author_display

ActorKind = Literal["person", "agent", "system"]

#: A person nothing usable names any more.
FORMER_MEMBER: Final = "A former member"

_RAW_PREFIXES: Final = ("user:", "agent:", "machine:")


def system_name() -> str:
    """The platform itself, as it is named."""
    return brand.product_name()


def agent_prefix() -> str:
    """What an agent acting for a person is named before the person's name."""
    return f"{brand.agent_name()} for "


def agent_name(person: str) -> str:
    """An agent acting for ``person`` (their name), named."""
    return f"{agent_prefix()}{person}"


def person_name(user: User) -> str:
    """A person's name: their first and last name, else their email."""
    return author_display(user)


def usable(recorded: str | None) -> str | None:
    """A recorded name when it names someone; ``None`` for an empty one, a
    raw id or a placeholder."""
    text = (recorded or "").strip()
    if not text or text.startswith(_RAW_PREFIXES) or text.lower() == "someone":
        return None
    return text


def user_of_key(actor_key: str | None) -> uuid.UUID | None:
    """The user a ``user:<uuid>`` key names."""
    if not actor_key or not actor_key.startswith("user:"):
        return None
    try:
        return uuid.UUID(actor_key.removeprefix("user:"))
    except ValueError:
        return None


@dataclass
class Names:
    """Resolves people by id, once per answer (every id it is asked for is
    read in one query)."""

    db: AsyncSession
    _users: dict[uuid.UUID, User | None] = field(default_factory=dict)

    async def load(self, ids: Iterable[uuid.UUID | None]) -> None:
        wanted = {one for one in ids if one is not None and one not in self._users}
        if not wanted:
            return
        found = (await self.db.execute(select(User).where(User.id.in_(wanted)))).scalars()
        by_id = {user.id: user for user in found}
        for one in wanted:
            self._users[one] = by_id.get(one)

    def user(self, user_id: uuid.UUID | None) -> User | None:
        return None if user_id is None else self._users.get(user_id)

    def person(self, user_id: uuid.UUID | None, recorded: str | None = None) -> str:
        """A person's name now; what was recorded when they are gone."""
        user = self.user(user_id)
        if user is not None:
            return person_name(user)
        return usable(recorded) or FORMER_MEMBER

    def actor(
        self,
        *,
        kind: str,
        actor_key: str,
        user_id: uuid.UUID | None,
        recorded: str | None,
    ) -> ActorRef:
        """Who an edit, a run or a caret was, named. ``user_id`` is the person
        (for an agent, the person it acts for)."""
        if kind == "agent":
            if user_id is not None and self.user(user_id) is not None:
                person = self.person(user_id)
            else:
                remembered = usable(recorded)
                prefix = agent_prefix()
                if remembered is not None and remembered.startswith(prefix):
                    person = remembered.removeprefix(prefix) or FORMER_MEMBER
                else:
                    person = FORMER_MEMBER
            return ActorRef(
                kind="agent",
                id=actor_key,
                display_name=agent_name(person),
                acting_for=ActingForRef(
                    id=f"user:{user_id}" if user_id is not None else "", display_name=person
                ),
            )
        if kind == "person":
            return ActorRef(
                kind="person", id=actor_key, display_name=self.person(user_id, recorded)
            )
        return ActorRef(kind="system", id=actor_key or "system", display_name=system_name())

    def label(
        self,
        *,
        kind: str,
        actor_key: str,
        user_id: uuid.UUID | None,
        recorded: str | None,
    ) -> str:
        return self.actor(
            kind=kind, actor_key=actor_key, user_id=user_id, recorded=recorded
        ).display_name


__all__ = [
    "FORMER_MEMBER",
    "ActorKind",
    "Names",
    "agent_name",
    "agent_prefix",
    "person_name",
    "system_name",
    "usable",
    "user_of_key",
]
