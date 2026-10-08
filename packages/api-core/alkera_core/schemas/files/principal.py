"""Who holds access, and how they came to hold it.

A platform-wide RBAC/ABAC engine replaces Files' own grant reads later, so the
shapes here are written to survive that swap: the principal ``kind`` is an open
registry (``user``/``team``/``org`` today; ``agent``, ``service``, ``link`` and
``public`` later, and the engine may add more without a migration), the role is
a *string* validated against the ladder rather than an enum — the ladder is data
the engine will own — and every grant carries ``expires_at`` and ``conditions``
from day one so a time-boxed or attribute-conditioned grant needs no new field.

Role names are validated in this module and nowhere else. That is the whole
point of keeping them here: when the engine lands, one module changes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Discriminator, Field, Tag, field_validator

from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

ROLE_LADDER: tuple[str, ...] = ("reader", "commenter", "writer", "manager", "owner")
"""Ordered weakest to strongest. Allow-only: there is no deny role."""

_ORIGIN_TAGS = {"direct", "inherited", "drive_default"}


class Principal(VersionedModel):
    """``{kind, id}`` — the only identity shape any grant, ACE or decision uses."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    #: An open registry, never an enum: an unknown kind read from an older or
    #: newer writer must round-trip rather than raise.
    kind: str
    id: str


class DirectGrant(VersionedModel):
    """Granted on this node itself."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["direct"] = "direct"


class InheritedGrant(VersionedModel):
    """Trickled down from an ancestor; revoking it here is refused with the ancestor named."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["inherited"] = "inherited"
    ancestor_node_id: str


class DriveDefaultGrant(VersionedModel):
    """The drive's default, which every node in it starts from."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["drive_default"] = "drive_default"


class RawGrantOrigin(VersionedModel):
    """An origin tag this reader does not know — carried through, never raised on."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: str = "__unknown__"


_origin_tag = make_unknown_tag_discriminator(_ORIGIN_TAGS, field="kind")

GrantOrigin = Annotated[
    Annotated[DirectGrant, Tag("direct")]
    | Annotated[InheritedGrant, Tag("inherited")]
    | Annotated[DriveDefaultGrant, Tag("drive_default")]
    | Annotated[RawGrantOrigin, Tag("__unknown__")],
    Discriminator(_origin_tag),
]


def validate_role(role: str) -> str:
    """The single place a role name is checked against the ladder."""
    if role not in ROLE_LADDER:
        raise ValueError(f"unknown role {role!r}; the ladder is {', '.join(ROLE_LADDER)}")
    return role


class Grant(VersionedModel):
    """One principal's role on one node, and where that role came from."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    principal: Principal
    #: A string, not an enum: the ladder is data the coming engine owns.
    role: str
    origin: GrantOrigin = Field(default_factory=DirectGrant)
    #: Nullable from day one so a time-boxed grant needs no migration.
    expires_at: datetime | None = None
    #: Reserved for ABAC; unread today, present so a condition needs no column.
    conditions: dict[str, Any] | None = None

    @field_validator("role")
    @classmethod
    def _role_in_ladder(cls, role: str) -> str:
        return validate_role(role)


def _principal_example() -> Principal:
    return Principal(kind="team", id="6f1d2a3b-0c4e-4d5f-8a91-2b3c4d5e6f70")


def _grant_example() -> Grant:
    return Grant(
        principal=Principal(kind="user", id="e7c0f4a4-1f8e-4a6e-9b06-1f3d1e1a0001"),
        role="writer",
        origin=InheritedGrant(ancestor_node_id="1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"),
        expires_at=None,
        conditions=None,
    )


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    ("principal", _principal_example),
    ("grant", _grant_example),
]
