"""Who may see and drive the org's compute machines.

One policy for the whole compute surface — the catalog, a user's session
allocations, and the org's workspace machine — decided in the order the routes
have always answered: a resource outside the caller's org is a 404 before
anything else, so is a session allocation that belongs to someone else (a
foreign machine is indistinguishable from a missing one), then org membership,
and a write additionally needs a verified email. ``READ`` covers the catalog,
listing and fetching; ``WRITE`` covers starting, releasing, renewing,
registering and heartbeating. Every fact is required on every branch, so a
caller that forgets one is denied rather than allowed by the branch that
happened not to need it.

Some writes are not a member's to make: the ones that decide whether the box
the whole organization's chats run on exists at all. Every chat placed on it
hands it that member's prompts, their attachments and the connection
credentials the box is leased, so whoever may stand one up may read the
organization — and whoever may tear one down may take its compute away.
``controls`` marks those writes: creating or re-claiming a machine, releasing
it, renewing it. They take ADMIN of the org root when the machine is a
``workspace`` one. Heartbeating is deliberately NOT one of them — a box says it
is alive, it does not decide that it should be — and neither is anything a
member does to a session allocation they rented for themselves, which serves
that member and nobody else.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MissingAttributeError, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role, expand_roles
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "compute.machine"
AUDITED = frozenset({"in_org", "roles", "email_verified", "is_owner", "lifecycle", "controls"})

NOT_FOUND_MESSAGE = "Machine not found"
MEMBER_MESSAGE = "org member role required"
VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"
ADMIN_MESSAGE = "Only an org admin may set up or take down the organization's machine."
ADMIN_CODE = "org_admin_required"

#: The lifecycle of a box the whole organisation's chats are placed on.
WORKSPACE = "workspace"

_SUPPORTED = (Action.READ, Action.WRITE)


def _roles(attrs: Mapping[str, object]) -> frozenset[Role]:
    held = attrs.get("roles")
    if not isinstance(held, AbstractSet) or not all(isinstance(role, Role) for role in held):
        raise MissingAttributeError("roles")
    return frozenset(held)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in _SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    in_org = require_attr(attrs, "in_org", bool)
    is_owner = require_attr(attrs, "is_owner", bool)
    roles = expand_roles(_roles(attrs))
    email_verified = require_attr(attrs, "email_verified", bool)
    lifecycle = require_attr(attrs, "lifecycle", str)
    controls = require_attr(attrs, "controls", bool)
    if ctx.is_machine:
        return _machine_holds_machine(ctx, action, resource, lifecycle=lifecycle, controls=controls)
    if not in_org:
        return deny(POLICY, "team_not_in_org", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not is_owner:
        return deny(POLICY, "not_owner", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if Role.MEMBER not in roles:
        return deny(POLICY, "org_member_required", message=MEMBER_MESSAGE)
    if action is Action.READ:
        return allow(POLICY, "member_read")
    if not email_verified:
        return deny(
            POLICY,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    if controls:
        if lifecycle != WORKSPACE:
            # A named branch, not a fall-through: everything that is not a
            # workspace box is compute a member rented for their own session,
            # and the member who rented it decides its life. A lifecycle added
            # later lands here and has to be thought about, rather than being
            # admitted by the branch that happened not to name it.
            return allow(POLICY, "member_controls_own_allocation")
        if Role.ADMIN not in roles:
            return deny(POLICY, "org_admin_required", message=ADMIN_MESSAGE, error_code=ADMIN_CODE)
        return allow(POLICY, "admin_controls")
    return allow(POLICY, "member_write")


def _machine_holds_machine(
    ctx: ActingContext, action: Action, resource: Resource, *, lifecycle: str, controls: bool
) -> Decision:
    """A box on its own credential says it is alive and nothing more: a WRITE
    that does not control the machine's existence, on the workspace row whose
    id IS the machine the credential holds. It never reads the org's banner,
    never registers, releases or renews anything — those belong to the org's
    admin, and the box has no roles at all. Any other machine, and any other
    verb, is the opaque not-found a stranger gets."""
    if (
        action is Action.WRITE
        and not controls
        and lifecycle == WORKSPACE
        and resource.id == ctx.acting_principal.id
    ):
        return allow(POLICY, "machine_holds_machine")
    return deny(
        POLICY, "machine_does_not_hold_machine", message=NOT_FOUND_MESSAGE, as_not_found=True
    )


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.COMPUTE_MACHINE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "ADMIN_CODE",
    "ADMIN_MESSAGE",
    "AUDITED",
    "MEMBER_MESSAGE",
    "NOT_FOUND_MESSAGE",
    "POLICY",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "WORKSPACE",
    "decide",
]
