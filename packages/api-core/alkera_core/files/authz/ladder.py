"""The role ladder — data, not code.

:data:`LADDER` is the table from the permission model, one row per role, each
row naming the complete set of actions that role allows. It is a constant so a
role can be added, or an action moved between rows, without touching a branch
anywhere: :class:`RoleLadder` reads whatever table it is handed, and the
platform RBAC engine that lands later hands it a table loaded from its own
storage instead of this constant.

Roles are strings on purpose. They are persisted (``file_shares.role``,
``file_acls.body[].role``) and they must survive an engine that invents rungs we
have not thought of, so nothing here treats an unknown role as an error — it is
simply a role that ranks below every rung this ladder knows and allows nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from alkera_core.files.authz.actions import FilesAction

A = FilesAction

#: The rungs, spelled once. Every other module that has to name a role — a
#: listing chip, the audience grant an object projection makes — imports one of
#: these rather than repeating the string, so the ladder stays the only place
#: that knows how a role is spelled on the wire and in the database.
ROLE_READER: Final = "reader"
ROLE_COMMENTER: Final = "commenter"
ROLE_WRITER: Final = "writer"
ROLE_MANAGER: Final = "manager"
ROLE_OWNER: Final = "owner"

#: The permission model's table, weakest rung first. Each row is complete: it
#: names every action the role allows, not the delta from the row above, so a
#: reader can check a row against the product's documentation line by line.
LADDER: Final[tuple[tuple[str, frozenset[FilesAction]], ...]] = (
    (ROLE_READER, frozenset({A.READ, A.EXPORT, A.LEASE_REQUEST, A.COPY})),
    (ROLE_COMMENTER, frozenset({A.READ, A.EXPORT, A.LEASE_REQUEST, A.COPY, A.COMMENT})),
    (
        ROLE_WRITER,
        frozenset(
            {
                A.READ,
                A.EXPORT,
                A.LEASE_REQUEST,
                A.COPY,
                A.COMMENT,
                A.WRITE,
                A.RESTORE,
                A.LEASE,
                A.SNAPSHOT,
                A.LOCK,
            }
        ),
    ),
    (
        ROLE_MANAGER,
        frozenset(
            {
                A.READ,
                A.EXPORT,
                A.LEASE_REQUEST,
                A.COPY,
                A.COMMENT,
                A.WRITE,
                A.RESTORE,
                A.LEASE,
                A.SNAPSHOT,
                A.LOCK,
                A.SHARE,
                A.LEASE_FORCE,
            }
        ),
    ),
    (
        ROLE_OWNER,
        frozenset(
            {
                A.READ,
                A.EXPORT,
                A.LEASE_REQUEST,
                A.COPY,
                A.COMMENT,
                A.WRITE,
                A.RESTORE,
                A.LEASE,
                A.SNAPSHOT,
                A.LOCK,
                A.SHARE,
                A.LEASE_FORCE,
                A.DELETE,
                A.HOLD,
            }
        ),
    ),
)


#: What a person is shown where the wire says a rung. The internal names are
#: about actions ("writer"); the labels are about what the person gets ("Can
#: edit"), which is the vocabulary every share dialog in the world uses. It is a
#: mapping rather than a branch for the same reason :data:`LADDER` is: a rung an
#: engine invents that this table has never heard of falls back to its own name
#: through :func:`role_label` instead of rendering blank.
ROLE_LABELS: Final[Mapping[str, str]] = {
    ROLE_READER: "Can view",
    ROLE_COMMENTER: "Can comment",
    ROLE_WRITER: "Can edit",
    ROLE_MANAGER: "Full access",
    ROLE_OWNER: "Owner",
}


#: The rungs a person hands out through a share, weakest first. Ownership is
#: not one: it moves by transfer. Commenting is not offered while nothing in
#: the product lets a commenter comment.
OFFERED_ROLES: Final[tuple[str, ...]] = (ROLE_READER, ROLE_WRITER, ROLE_MANAGER)

#: Rungs the ladder keeps but a share no longer hands out, each shown as the
#: offered rung it behaves like. A commenter reads and cannot write, which is
#: what "Can view" promises, so a grant made on that rung before it was
#: withdrawn reads, and is selected, as one; it is never a blank picker or a
#: second name for read access.
SHOWN_AS: Final[Mapping[str, str]] = {ROLE_COMMENTER: ROLE_READER}


def shown_role(role: str) -> str:
    """The rung ``role`` is shown and selected as: itself, unless withdrawn."""
    return SHOWN_AS.get(role, role)


def role_label(role: str) -> str:
    """The label to show for ``role``; the role itself when it has no label."""
    return ROLE_LABELS.get(role, role)


class RoleLadder:
    """An ordered table of role names and the actions each allows."""

    def __init__(self, table: Sequence[tuple[str, frozenset[FilesAction]]] = LADDER) -> None:
        self._order: tuple[str, ...] = tuple(name for name, _ in table)
        if len(set(self._order)) != len(self._order):
            raise ValueError(f"a ladder names a role twice: {self._order}")
        self._actions: Mapping[str, frozenset[FilesAction]] = {
            name: actions for name, actions in table
        }

    @property
    def roles(self) -> tuple[str, ...]:
        """Every rung, weakest first."""
        return self._order

    def rank(self, role: str | None) -> int:
        """Where ``role`` sits, ``-1`` for ``None`` or a rung this ladder has
        never heard of — so an unknown role never outranks a known one."""
        if role is None:
            return -1
        try:
            return self._order.index(role)
        except ValueError:
            return -1

    def actions_for(self, role: str | None) -> frozenset[FilesAction]:
        """What ``role`` allows; the empty set for ``None`` or an unknown rung."""
        if role is None:
            return frozenset()
        return self._actions.get(role, frozenset())

    def allows(self, role: str | None, action: FilesAction) -> bool:
        return action in self.actions_for(role)

    def max(self, roles: Iterable[str]) -> str | None:
        """The strongest of ``roles``, or ``None`` when none of them is a rung."""
        best: str | None = None
        best_rank = -1
        for role in roles:
            rank = self.rank(role)
            if rank > best_rank:
                best, best_rank = role, rank
        return best


#: The ladder the product runs on.
DEFAULT_LADDER: Final[RoleLadder] = RoleLadder()

__all__ = [
    "DEFAULT_LADDER",
    "LADDER",
    "OFFERED_ROLES",
    "ROLE_LABELS",
    "SHOWN_AS",
    "RoleLadder",
    "role_label",
    "shown_role",
]
