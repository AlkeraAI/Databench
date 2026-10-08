"""The shape every per-member cap inside a team is decided on.

A team admin rations what the team was allocated from above: a member's budget
on the team pool, a member's storage inside the team's folder. Whatever the
resource, the same relationship decides a write — a team outside the caller's
org is a not-found before anything else, then team admin (by descent), then a
proven address — and then two refusals. A figure past the team's ceiling (the
tightest allowance on the team or any team above it) is refused whoever writes
it, the org admin included: a stored cap larger than what binds would mislead
every reader, and the writer who may raise the ceiling raises it first, where
the refusal points them. A change that widens the caller's OWN allowance (a
higher own cap, or removing their own cap, which falls back to the ceiling or
to no limit) needs an admin of a team ABOVE this one — the org admin always is.

Each resource's policy registers under its own name and wording; this module is
the relationship they share, so a cap on a new resource admits the same rule by
calling these helpers rather than restating it. Every accessor treats an absent
fact and a wrongly-typed one alike (``MissingAttributeError``), and reads every
fact it is handed before deciding, so a caller that forgets one is denied rather
than allowed by the branch that happened not to consult it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, deny
from alkera_core.authz.engine import require_attr
from alkera_core.authz.enums import Role
from alkera_core.authz.policies._facts import roles_of

VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"

#: The facts a ceiling refusal is decided and worded from. ``exceeds_ceiling``
#: is read on every write; the amount and the team only once it is true, to
#: name the ceiling in the refusal.
CEILING_FACTS = ("exceeds_ceiling", "ceiling_amount", "ceiling_team")


def ceiling_refusal(policy: str, attrs: Mapping[str, object], *, template: str) -> Decision | None:
    """The refusal a figure past the ceiling meets, whoever writes it.

    ``template`` is the resource's own sentence with ``{amount}`` and
    ``{team}`` slots — the ceiling's figure as the caller formatted it and the
    team it was set on — so the person refused knows where the allowance they
    ran into is set."""
    if not require_attr(attrs, "exceeds_ceiling", bool):
        return None
    amount = require_attr(attrs, "ceiling_amount", str)
    team = require_attr(attrs, "ceiling_team", str)
    return deny(policy, "allocation_ceiling", message=template.format(amount=amount, team=team))


def team_admin_gate(policy: str, attrs: Mapping[str, object]) -> Decision | None:
    """In-org, team admin, proven address — the front of every cap write."""
    if not require_attr(attrs, "in_org", bool):
        return deny(policy, "team_not_in_org", message="Team not found", as_not_found=True)
    if Role.ADMIN not in roles_of(attrs):
        return deny(policy, "team_admin_required", message="team admin role required")
    if not require_attr(attrs, "email_verified", bool):
        return deny(
            policy,
            "email_verification_required",
            message=VERIFY_EMAIL_MESSAGE,
            error_code=VERIFY_EMAIL_CODE,
        )
    return None


def admin_above(attrs: Mapping[str, object]) -> bool:
    """An admin of a team above this one; the org admin always is. Both facts
    are read so neither can be forgotten."""
    is_admin_above = require_attr(attrs, "is_admin_above", bool)
    is_org_admin = require_attr(attrs, "is_org_admin", bool)
    return is_admin_above or is_org_admin


def own_raise(attrs: Mapping[str, object]) -> bool:
    """The write widens the caller's own allowance. Both facts are read before
    either decides, so neither is required only on the branch that consults it."""
    target_is_self = require_attr(attrs, "target_is_self", bool)
    raises_limit = require_attr(attrs, "raises_limit", bool)
    return target_is_self and raises_limit


def refuse_from_inside(
    policy: str,
    attrs: Mapping[str, object],
    *,
    own_raise_reason: str,
    own_raise_message: str,
    ceiling_message: str | None,
) -> Decision | None:
    """The two refusals a cap write meets after the gate: the ceiling, which
    holds everyone, and the own-raise, which holds a team admin but not an
    admin above.

    ``ceiling_message`` is ``None`` for a clear, which names no figure to hold
    to the ceiling (it widens only the caller's own allowance, decided by
    ``own_raise``); a write passes its template and so is also held to it.
    Every fact is read before either refusal is decided.
    """
    above = admin_above(attrs)
    raising = own_raise(attrs)
    if ceiling_message is not None:
        refused = ceiling_refusal(policy, attrs, template=ceiling_message)
        if refused is not None:
            return refused
    if raising and not above:
        return deny(policy, own_raise_reason, message=own_raise_message)
    return None


__all__ = [
    "CEILING_FACTS",
    "VERIFY_EMAIL_CODE",
    "VERIFY_EMAIL_MESSAGE",
    "admin_above",
    "ceiling_refusal",
    "own_raise",
    "refuse_from_inside",
    "team_admin_gate",
]
