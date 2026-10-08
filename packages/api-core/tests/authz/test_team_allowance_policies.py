"""Branch tables for the two "set from above" policies a team's allowance meets.

``team_allocation.set`` decides who may set or remove a team's own allowance;
``storage.team_member_cap`` decides who may cap a member's storage inside a
team. Every branch is a row with its effect, reason, opacity, exact message and
error code, and every required fact is driven removed and wrongly typed on the
allow path — the branch that consults none of the refusing facts — so a caller
that forgets one is denied rather than admitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest
from alkera_core.authz import (
    ActingContext,
    Action,
    Decision,
    Effect,
    Resource,
    ResourceType,
    Role,
    authorize,
)
from alkera_core.authz.policies import team_allocation, team_storage_cap

ORG = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_ORG = UUID("00000000-0000-4000-8000-00000000000f")
TEAM = UUID("00000000-0000-4000-8000-00000000000b")
USER = ActingContext.for_user(
    user_id=UUID("00000000-0000-4000-8000-000000000001"), org_id=ORG, email="m@example.com"
)

ALLOCATION = Resource(ResourceType.TEAM_ALLOCATION, id=str(TEAM), org_id=ORG, team_id=TEAM)
STORAGE_CAP = Resource(ResourceType.TEAM_STORAGE_CAP, id=str(TEAM), org_id=ORG, team_id=TEAM)

ADMIN_ROLES = frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER})
VERIFY = "Verify your email address to perform this action."
VERIFY_CODE = "email_verification_required"


def _expect(
    decision: Decision,
    *,
    effect: Effect,
    reason: str,
    policy: str,
    message: str,
    as_not_found: bool = False,
    error_code: str | None = None,
) -> None:
    assert decision.effect is effect, decision
    assert decision.reason == reason, decision
    assert decision.policy == policy, decision
    assert decision.message == message, decision
    assert decision.as_not_found is as_not_found, decision
    assert decision.error_code == error_code, decision


def _without(attrs: Mapping[str, object], key: str) -> dict[str, object]:
    return {k: v for k, v in attrs.items() if k != key}


# =========================================================================== #
# team_allocation.set
# =========================================================================== #

#: An admin of the team's parent setting a sub-team's allowance.
ALLOCATION_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": ADMIN_ROLES,
    "email_verified": True,
    "is_org_root": False,
    "is_admin_above": True,
    "exceeds_ceiling": False,
}
ALLOCATION_WRONG: dict[str, object] = {
    "in_org": "true",
    "roles": ["admin"],
    "email_verified": 1,
    "is_org_root": "no",
    "is_admin_above": 1,
    "exceeds_ceiling": "no",
}
#: A figure past the tightest allowance above the team, as the route names it.
PAST_CEILING: dict[str, object] = {
    "exceeds_ceiling": True,
    "ceiling_amount": "$5.00",
    "ceiling_team": "Eng",
}
ALLOCATION_CEILING = "A team's allowance can't exceed the $5.00 allowed to Eng."


@pytest.mark.parametrize("action", [Action.WRITE, Action.DELETE], ids=["set", "clear"])
@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "as_not_found", "message", "error_code"),
    [
        pytest.param({}, Effect.ALLOW, "admin_above_sets", False, "", None, id="admin-above"),
        pytest.param(
            {"is_org_root": True, "is_admin_above": False},
            Effect.ALLOW,
            "org_admin_sets_root",
            False,
            "",
            None,
            id="root-admin-sets-the-root",
        ),
        pytest.param(
            {"is_admin_above": False},
            Effect.DENY,
            "allocation_from_above",
            False,
            team_allocation.FROM_ABOVE_MESSAGE,
            None,
            id="the-teams-own-admin-cannot-raise-its-allowance",
        ),
        pytest.param(
            {"in_org": False, "roles": frozenset()},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            None,
            id="foreign-team",
        ),
        pytest.param(
            {"in_org": False, "is_org_root": True},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            None,
            id="foreign-root-is-still-not-found",
        ),
        pytest.param(
            {"roles": frozenset({Role.MEMBER, Role.VIEWER})},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            None,
            id="member",
        ),
        pytest.param(
            {"roles": frozenset({Role.MEMBER}), "is_org_root": True},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            None,
            id="a-root-member-is-not-the-org-admin",
        ),
        pytest.param(
            {"email_verified": False},
            Effect.DENY,
            "email_verification_required",
            False,
            VERIFY,
            VERIFY_CODE,
            id="unverified-admin-above",
        ),
    ],
)
def test_team_allocation_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
    error_code: str | None,
) -> None:
    _expect(
        authorize(USER, action, ALLOCATION, {**ALLOCATION_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="team_allocation.set",
        message=message,
        as_not_found=as_not_found,
        error_code=error_code,
    )


@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "message"),
    [
        pytest.param(
            PAST_CEILING,
            Effect.DENY,
            "allocation_ceiling",
            ALLOCATION_CEILING,
            id="an-admin-above-is-held-to-the-allowance-above",
        ),
        pytest.param(
            {**PAST_CEILING, "is_org_root": True, "is_admin_above": False},
            Effect.DENY,
            "allocation_ceiling",
            ALLOCATION_CEILING,
            id="the-root-is-held-to-the-orgs-own-ceiling",
        ),
        pytest.param(
            {**PAST_CEILING, "is_admin_above": False},
            Effect.DENY,
            "allocation_from_above",
            team_allocation.FROM_ABOVE_MESSAGE,
            id="from-above-is-named-before-the-ceiling",
        ),
        pytest.param(
            {**PAST_CEILING, "email_verified": False},
            Effect.DENY,
            "email_verification_required",
            VERIFY,
            id="verification-before-the-ceiling",
        ),
        pytest.param(
            {"exceeds_ceiling": False, "ceiling_amount": "$5.00", "ceiling_team": "Eng"},
            Effect.ALLOW,
            "admin_above_sets",
            "",
            id="within-the-ceiling-the-name-is-ignored",
        ),
    ],
)
def test_team_allocation_set_is_held_to_the_ceiling_above(
    overrides: dict[str, object], effect: Effect, reason: str, message: str
) -> None:
    _expect(
        authorize(USER, Action.WRITE, ALLOCATION, {**ALLOCATION_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="team_allocation.set",
        message=message,
        error_code=VERIFY_CODE if reason == "email_verification_required" else None,
    )


def test_team_allocation_clear_consults_no_ceiling() -> None:
    """A removal names no figure: a stray ceiling fact cannot refuse it, and
    the ceiling's name is never asked for."""
    stray = {**ALLOCATION_ALLOW, **PAST_CEILING}
    assert authorize(USER, Action.DELETE, ALLOCATION, stray).allowed is True
    bare = _without(ALLOCATION_ALLOW, "exceeds_ceiling")
    assert authorize(USER, Action.DELETE, ALLOCATION, bare).allowed is True


@pytest.mark.parametrize("key", ["ceiling_amount", "ceiling_team"])
def test_team_allocation_past_the_ceiling_requires_the_ceilings_name(key: str) -> None:
    attrs = _without({**ALLOCATION_ALLOW, **PAST_CEILING}, key)
    _expect(
        authorize(USER, Action.WRITE, ALLOCATION, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="team_allocation.set",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("key", "wrong"),
    [
        pytest.param("ceiling_amount", 5, id="int-for-amount"),
        pytest.param("ceiling_team", 1, id="int-for-team"),
    ],
)
def test_team_allocation_ceiling_name_of_the_wrong_type_denies(key: str, wrong: object) -> None:
    attrs = {**ALLOCATION_ALLOW, **PAST_CEILING, key: wrong}
    assert authorize(USER, Action.WRITE, ALLOCATION, attrs).reason == f"missing_attribute:{key}"


@pytest.mark.parametrize("key", sorted(ALLOCATION_ALLOW))
def test_team_allocation_missing_attribute_denies(key: str) -> None:
    _expect(
        authorize(USER, Action.WRITE, ALLOCATION, _without(ALLOCATION_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="team_allocation.set",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(ALLOCATION_WRONG))
def test_team_allocation_wrong_attribute_type_denies(key: str) -> None:
    _expect(
        authorize(USER, Action.WRITE, ALLOCATION, {**ALLOCATION_ALLOW, key: ALLOCATION_WRONG[key]}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="team_allocation.set",
        message="Not allowed",
    )


def test_team_allocation_root_still_requires_the_above_fact() -> None:
    """The root branch does not consult ``is_admin_above``; it is required all
    the same, so a caller cannot reach the root allow by forgetting it."""
    attrs = _without({**ALLOCATION_ALLOW, "is_org_root": True}, "is_admin_above")
    assert (
        authorize(USER, Action.WRITE, ALLOCATION, attrs).reason
        == "missing_attribute:is_admin_above"
    )


@pytest.mark.parametrize("action", [Action.READ, Action.ADMIN, Action.SET_BUDGET])
def test_team_allocation_unsupported_action_denies(action: Action) -> None:
    decision = authorize(USER, action, ALLOCATION, ALLOCATION_ALLOW)
    assert (decision.effect, decision.reason) == (Effect.DENY, "action_not_supported")


def test_team_allocation_of_another_org_is_opaque() -> None:
    foreign = Resource(ResourceType.TEAM_ALLOCATION, id=str(TEAM), org_id=OTHER_ORG, team_id=TEAM)
    decision = authorize(USER, Action.WRITE, foreign, ALLOCATION_ALLOW)
    assert decision.effect is Effect.DENY and decision.as_not_found is True


# =========================================================================== #
# storage.team_member_cap
# =========================================================================== #

#: A team admin capping another member within the team's allocation: the
#: branch that consults none of the refusing facts.
STORAGE_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": ADMIN_ROLES,
    "email_verified": True,
    "is_org_admin": False,
    "is_admin_above": False,
    "target_is_self": False,
    "raises_limit": False,
    "exceeds_ceiling": False,
}
STORAGE_CLEAR_ALLOW = _without(STORAGE_ALLOW, "exceeds_ceiling")
STORAGE_WRONG: dict[str, object] = {
    "in_org": "true",
    "roles": ["admin"],
    "email_verified": 1,
    "is_org_admin": "yes",
    "is_admin_above": 1,
    "target_is_self": "false",
    "raises_limit": 0,
    "exceeds_ceiling": "no",
}
OWN_RAISE = team_storage_cap.OWN_RAISE_MESSAGE
#: A figure past the ceiling on the team's chain, as the route names it.
STORAGE_PAST_CEILING: dict[str, object] = {
    "exceeds_ceiling": True,
    "ceiling_amount": "5 GB",
    "ceiling_team": "Data",
}
CEILING = "A member's storage can't exceed the 5 GB allowed to Data."


@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "as_not_found", "message", "error_code"),
    [
        pytest.param({}, Effect.ALLOW, "team_admin_set", False, "", None, id="another-member"),
        pytest.param(
            {"target_is_self": True, "raises_limit": False},
            Effect.ALLOW,
            "team_admin_set",
            False,
            "",
            None,
            id="lowering-their-own-cap",
        ),
        pytest.param(
            {"target_is_self": True, "raises_limit": True},
            Effect.DENY,
            "own_storage_raise",
            False,
            OWN_RAISE,
            None,
            id="raising-their-own-cap",
        ),
        pytest.param(
            {"target_is_self": False, "raises_limit": True},
            Effect.ALLOW,
            "team_admin_set",
            False,
            "",
            None,
            id="raising-a-teammate-within-the-allocation",
        ),
        pytest.param(
            STORAGE_PAST_CEILING,
            Effect.DENY,
            "allocation_ceiling",
            False,
            CEILING,
            None,
            id="a-teammate-above-the-ceiling",
        ),
        pytest.param(
            {**STORAGE_PAST_CEILING, "target_is_self": True, "raises_limit": True},
            Effect.DENY,
            "allocation_ceiling",
            False,
            CEILING,
            None,
            id="the-ceiling-is-named-before-the-own-raise",
        ),
        pytest.param(
            {**STORAGE_PAST_CEILING, "is_admin_above": True},
            Effect.DENY,
            "allocation_ceiling",
            False,
            CEILING,
            None,
            id="an-admin-above-is-held-to-the-ceiling-too",
        ),
        pytest.param(
            {**STORAGE_PAST_CEILING, "is_org_admin": True},
            Effect.DENY,
            "allocation_ceiling",
            False,
            CEILING,
            None,
            id="the-org-admin-is-held-to-the-ceiling-too",
        ),
        pytest.param(
            {"target_is_self": True, "raises_limit": True, "is_admin_above": True},
            Effect.ALLOW,
            "team_admin_set",
            False,
            "",
            None,
            id="admin-above-may-raise-their-own",
        ),
        pytest.param(
            {"in_org": False, "roles": frozenset()},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            None,
            id="foreign-team",
        ),
        pytest.param(
            {"roles": frozenset({Role.MEMBER, Role.VIEWER})},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            None,
            id="member",
        ),
        pytest.param(
            {"email_verified": False, **STORAGE_PAST_CEILING},
            Effect.DENY,
            "email_verification_required",
            False,
            VERIFY,
            VERIFY_CODE,
            id="verification-before-the-ceiling",
        ),
    ],
)
def test_storage_cap_set_table(
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
    error_code: str | None,
) -> None:
    _expect(
        authorize(USER, Action.WRITE, STORAGE_CAP, {**STORAGE_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="storage.team_member_cap",
        message=message,
        as_not_found=as_not_found,
        error_code=error_code,
    )


@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "message"),
    [
        pytest.param({}, Effect.ALLOW, "team_admin_clear", "", id="another-member"),
        pytest.param(
            {"target_is_self": True, "raises_limit": True},
            Effect.DENY,
            "own_storage_raise",
            OWN_RAISE,
            id="clearing-their-own-cap",
        ),
        pytest.param(
            {"target_is_self": True, "raises_limit": True, "is_org_admin": True},
            Effect.ALLOW,
            "team_admin_clear",
            "",
            id="org-admin-clears-their-own",
        ),
        pytest.param(
            {"target_is_self": True, "raises_limit": False},
            Effect.ALLOW,
            "team_admin_clear",
            "",
            id="own-cap-that-changes-nothing",
        ),
    ],
)
def test_storage_cap_clear_table(
    overrides: dict[str, object], effect: Effect, reason: str, message: str
) -> None:
    _expect(
        authorize(USER, Action.DELETE, STORAGE_CAP, {**STORAGE_CLEAR_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="storage.team_member_cap",
        message=message,
    )


def test_storage_cap_clear_needs_no_ceiling_fact() -> None:
    """A removal names no figure, so it is never judged against the ceiling:
    a stray ``exceeds_ceiling`` cannot refuse it."""
    assert authorize(USER, Action.DELETE, STORAGE_CAP, STORAGE_CLEAR_ALLOW).allowed is True
    stray = {**STORAGE_CLEAR_ALLOW, **STORAGE_PAST_CEILING}
    assert authorize(USER, Action.DELETE, STORAGE_CAP, stray).allowed is True


@pytest.mark.parametrize("key", ["ceiling_amount", "ceiling_team"])
def test_storage_cap_past_the_ceiling_requires_the_ceilings_name(key: str) -> None:
    attrs = _without({**STORAGE_ALLOW, **STORAGE_PAST_CEILING}, key)
    _expect(
        authorize(USER, Action.WRITE, STORAGE_CAP, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="storage.team_member_cap",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("key", "wrong"),
    [
        pytest.param("ceiling_amount", 5, id="int-for-amount"),
        pytest.param("ceiling_team", 1, id="int-for-team"),
    ],
)
def test_storage_cap_ceiling_name_of_the_wrong_type_denies(key: str, wrong: object) -> None:
    attrs = {**STORAGE_ALLOW, **STORAGE_PAST_CEILING, key: wrong}
    assert authorize(USER, Action.WRITE, STORAGE_CAP, attrs).reason == f"missing_attribute:{key}"


@pytest.mark.parametrize("key", sorted(STORAGE_ALLOW))
def test_storage_cap_set_missing_attribute_denies(key: str) -> None:
    _expect(
        authorize(USER, Action.WRITE, STORAGE_CAP, _without(STORAGE_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="storage.team_member_cap",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(STORAGE_WRONG))
def test_storage_cap_set_wrong_attribute_type_denies(key: str) -> None:
    _expect(
        authorize(USER, Action.WRITE, STORAGE_CAP, {**STORAGE_ALLOW, key: STORAGE_WRONG[key]}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="storage.team_member_cap",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(STORAGE_CLEAR_ALLOW))
def test_storage_cap_clear_missing_attribute_denies(key: str) -> None:
    _expect(
        authorize(USER, Action.DELETE, STORAGE_CAP, _without(STORAGE_CLEAR_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="storage.team_member_cap",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(STORAGE_CLEAR_ALLOW))
def test_storage_cap_clear_wrong_attribute_type_denies(key: str) -> None:
    _expect(
        authorize(
            USER, Action.DELETE, STORAGE_CAP, {**STORAGE_CLEAR_ALLOW, key: STORAGE_WRONG[key]}
        ),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="storage.team_member_cap",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", [Action.READ, Action.SET_BUDGET, Action.ADMIN])
def test_storage_cap_unsupported_action_denies(action: Action) -> None:
    decision = authorize(USER, action, STORAGE_CAP, STORAGE_ALLOW)
    assert (decision.effect, decision.reason) == (Effect.DENY, "action_not_supported")
