"""The fact set a workspace object's policies decide from.

A chat and a saved query are the same kind of thing to an authorization rule:
a row in an org, owned by someone, addressed to an audience, with a reader who
holds roles and may or may not have verified their email. Both policies resolve
that set the same way, through this module, so "required" means the same thing
in both and a route cannot satisfy one policy's spelling and miss the other's.

Every accessor treats an absent value and a wrongly-typed one identically:
:class:`~alkera_core.authz.engine.MissingAttributeError`, which the engine turns
into ``missing_attribute:<key>``. A ``team_ids`` that arrives as a list of
strings is a caller bug, and a caller bug denies.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from uuid import UUID

from alkera_core.authz.chat_scope import require_scope_team
from alkera_core.authz.engine import MissingAttributeError, require_attr
from alkera_core.authz.enums import Role, expand_roles
from alkera_core.authz.principal import ActingContext

#: Every attribute a workspace-object policy requires of its caller, chat's
#: extra ``team_id`` aside.
COMMON_ATTRS = frozenset(
    {
        "in_org",
        "roles",
        "is_org_admin",
        "owner_user_id",
        "visibility_scope",
        "team_ids",
        "email_verified",
    }
)


@dataclass(frozen=True, slots=True)
class ObjectFacts:
    """The resolved facts, all of them present and well typed by construction."""

    in_org: bool
    roles: frozenset[Role]
    is_org_admin: bool
    owner_user_id: str
    visibility_scope: str
    team_ids: frozenset[UUID]
    email_verified: bool
    is_owner: bool


def roles_of(attrs: Mapping[str, object]) -> frozenset[Role]:
    """The subject's roles, closed under the ladder. A collection of plain
    strings reads as missing — a policy compares :class:`Role` members."""
    held = attrs.get("roles")
    if not isinstance(held, AbstractSet) or not all(isinstance(role, Role) for role in held):
        raise MissingAttributeError("roles")
    return expand_roles(held)


def uuid_set(attrs: Mapping[str, object], key: str) -> frozenset[UUID]:
    """A set of team ids. Strings that merely look like ids read as missing:
    a membership set is compared against parsed ids, never against text."""
    held = attrs.get(key)
    if not isinstance(held, AbstractSet) or not all(isinstance(item, UUID) for item in held):
        raise MissingAttributeError(key)
    return frozenset(item for item in held if isinstance(item, UUID))


def optional_uuid(attrs: Mapping[str, object], key: str) -> UUID | None:
    """A required key holding an optional id: ``""`` is "no id", a UUID string
    is that id, and anything else — including a :class:`UUID` object, which a
    route must stringify like every other audited attribute — reads as
    missing."""
    raw = require_attr(attrs, key, str)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        raise MissingAttributeError(key) from None


def object_facts(ctx: ActingContext, attrs: Mapping[str, object]) -> ObjectFacts:
    """Resolve every common attribute, or raise for the first one missing.

    Ownership is derived, never supplied: the acting context names the user
    whose permissions apply (the delegating user behind an agent or a personal
    access token), and a request with no human behind it owns nothing.
    """
    owner_user_id = require_attr(attrs, "owner_user_id", str)
    effective = ctx.effective_user_id
    return ObjectFacts(
        in_org=require_attr(attrs, "in_org", bool),
        roles=roles_of(attrs),
        is_org_admin=require_attr(attrs, "is_org_admin", bool),
        owner_user_id=owner_user_id,
        visibility_scope=require_attr(attrs, "visibility_scope", str),
        team_ids=uuid_set(attrs, "team_ids"),
        email_verified=require_attr(attrs, "email_verified", bool),
        is_owner=bool(owner_user_id) and effective is not None and str(effective) == owner_user_id,
    )


__all__ = [
    "COMMON_ATTRS",
    "ObjectFacts",
    "object_facts",
    "optional_uuid",
    "require_scope_team",
    "roles_of",
    "uuid_set",
]
