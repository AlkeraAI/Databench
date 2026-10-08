"""Whether a role may be written on a team for a person whose standing there
is already decided from above.

Admin descends: an admin of a team is an admin of every team beneath it, and
an admin of the org root is an admin everywhere. That standing is a fact of
the team above, so the team below cannot edit it — a row written there can
add direct standing but never lower or restate what descent already grants.

Two verbs. ``CREATE`` is a new row on the team: allowed for anyone descent
does not reach, and for someone it does reach only as ``admin`` (a direct
admin row is the one thing not yet true of them); a ``member`` row for a
descent admin would display a role they do not hold. ``WRITE`` is a change to
the row already there: refused outright for a descent admin, because no value
written here changes what they hold — the change belongs on the team the
standing comes from.

The caller must administer the team (the route dependency refuses earlier;
the fact is still decided and recorded here), and a team outside the caller's
org is a not-found before any role is consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MissingAttributeError, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role, expand_roles
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.models._enums import TeamRole

POLICY = "team_membership.role_write"

SUPPORTED = frozenset({Action.CREATE, Action.WRITE})

AUDITED = frozenset({"in_org", "roles", "requested_role", "descent_role", "descent_from"})


def _roles(attrs: Mapping[str, object]) -> frozenset[Role]:
    held = attrs.get("roles")
    if not isinstance(held, AbstractSet) or not all(isinstance(role, Role) for role in held):
        raise MissingAttributeError("roles")
    return frozenset(held)


def _optional_role(attrs: Mapping[str, object], key: str) -> TeamRole | None:
    """``attrs[key]`` as a team role or ``None``. Absent is a caller bug and
    reads as missing; so does a bare string."""
    try:
        value = attrs[key]
    except KeyError:
        raise MissingAttributeError(key) from None
    if value is None or isinstance(value, TeamRole):
        return value
    raise MissingAttributeError(key)


def _optional_str(attrs: Mapping[str, object], key: str) -> str | None:
    try:
        value = attrs[key]
    except KeyError:
        raise MissingAttributeError(key) from None
    if value is None or isinstance(value, str):
        return value
    raise MissingAttributeError(key)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    if not require_attr(attrs, "in_org", bool):
        return deny(POLICY, "team_not_in_org", message="Team not found", as_not_found=True)
    if Role.ADMIN not in expand_roles(_roles(attrs)):
        return deny(POLICY, "team_admin_required", message="team admin role required")
    requested = require_attr(attrs, "requested_role", TeamRole)
    descent = _optional_role(attrs, "descent_role")
    source = _optional_str(attrs, "descent_from") or "a team above"
    if descent is None:
        return allow(POLICY, "no_descent")
    if action is Action.CREATE:
        if requested is TeamRole.ADMIN:
            return allow(POLICY, "direct_admin_added")
        return deny(
            POLICY,
            "descent_outranks_request",
            message=(
                f"Already an admin of this team by descent from {source}; "
                "a member row here cannot lower that"
            ),
        )
    return deny(
        POLICY,
        "role_fixed_by_descent",
        message=f"Admin by descent from {source}; change their role there",
    )


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.TEAM_MEMBERSHIP,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["AUDITED", "POLICY", "SUPPORTED", "decide"]
