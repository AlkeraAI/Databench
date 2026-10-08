"""Every branch of the two platform-box policies.

``platform.machine`` decides the console's acts on the boxes the platform runs
(mint, list, revoke, assign, unassign); ``compute.machine_credential`` decides
whether a box may claim and heartbeat the one machine its credential names.
Each table drives every branch, every required attribute removed, and every
attribute mistyped — a caller that forgets a fact must be denied, never
allowed by the branch that happened not to need it.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.authz import ActingContext, Resource, ResourceType, authorize
from alkera_core.authz.decision import Decision, Effect
from alkera_core.authz.engine import policy_for
from alkera_core.authz.enums import Action, CredentialKind
from alkera_core.authz.policies import machine_credential, platform_machine

ORG = UUID("00000000-0000-4000-8000-00000000000a")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
TOKEN_ID = UUID("00000000-0000-4000-8000-0000000000aa")
USER = ActingContext.for_user(user_id=USER_ID, org_id=ORG, email="m@example.com")
BOX = ActingContext.for_agent(
    user_id=USER_ID, org_id=ORG, email="box@example.com", session_id="machine-1"
)
PROXY = ActingContext.for_service(
    token_id=TOKEN_ID, org_id=ORG, label="proxy", credential=CredentialKind.PROXY_TOKEN
)


def _expect(
    decision: Decision,
    *,
    effect: Effect,
    reason: str,
    policy: str,
    message: str,
    as_not_found: bool = False,
) -> None:
    assert decision.effect is effect, decision
    assert decision.reason == reason, decision
    assert decision.policy == policy, decision
    assert decision.message == message, decision
    assert decision.as_not_found is as_not_found, decision


# --------------------------------------------------------------------------- #
# platform.machine — the console's acts on the platform's boxes
# --------------------------------------------------------------------------- #

MACHINES = Resource(ResourceType.PLATFORM_MACHINE, id="machines")

MACHINE_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "operation": "mint",
    "org_exists": True,
}
MACHINE_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "operation": ["mint"],
    "org_exists": "yes",
}
MACHINE_ACTIONS = [Action.READ, Action.ADMIN]


def test_the_policies_are_registered_under_their_resource_types() -> None:
    machine = policy_for(ResourceType.PLATFORM_MACHINE)
    credential = policy_for(ResourceType.MACHINE_CREDENTIAL)
    assert machine is not None and machine.name == platform_machine.POLICY
    assert machine.audited_attrs == platform_machine.AUDITED
    assert credential is not None and credential.name == machine_credential.POLICY
    assert credential.audited_attrs == machine_credential.AUDITED


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message", "as_not_found"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "staff_read", "", False, id="admin-reads"),
        pytest.param(
            Action.READ,
            {"platform_admin": False, "operation": "list"},
            Effect.ALLOW,
            "staff_read",
            "",
            False,
            id="support-reads-the-machines-page",
        ),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_changes", "", False, id="admin-mints"),
        pytest.param(
            Action.ADMIN,
            {"operation": "assign"},
            Effect.ALLOW,
            "admin_changes",
            "",
            False,
            id="admin-assigns",
        ),
        pytest.param(
            Action.ADMIN,
            {"operation": "revoke"},
            Effect.ALLOW,
            "admin_changes",
            "",
            False,
            id="admin-revokes",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            False,
            id="support-may-not-mint",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False, "operation": "assign"},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            False,
            id="support-may-not-assign",
        ),
        pytest.param(
            Action.READ,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            False,
            id="a-tenant-cannot-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            False,
            id="a-tenant-cannot-mint",
        ),
        pytest.param(
            Action.READ,
            {"org_exists": False, "operation": "assignment"},
            Effect.DENY,
            "org_not_found",
            "Organization not found",
            True,
            id="an-unknown-org-is-not-found-to-staff",
        ),
        pytest.param(
            Action.ADMIN,
            {"org_exists": False, "operation": "assign"},
            Effect.DENY,
            "org_not_found",
            "Organization not found",
            True,
            id="an-unknown-org-cannot-be-assigned",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False, "org_exists": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            False,
            id="a-stranger-cannot-probe-which-orgs-exist",
        ),
    ],
)
def test_platform_machine_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
) -> None:
    _expect(
        authorize(USER, action, MACHINES, {**MACHINE_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="platform.machine",
        message=message,
        as_not_found=as_not_found,
    )


def test_platform_machine_admin_flag_alone_is_not_staff() -> None:
    _expect(
        authorize(USER, Action.ADMIN, MACHINES, {**MACHINE_ALLOW, "platform_staff": False}),
        effect=Effect.DENY,
        reason="platform_staff_required",
        policy="platform.machine",
        message="Platform staff role required",
    )


@pytest.mark.parametrize(
    "other", [a for a in Action if a not in MACHINE_ACTIONS], ids=lambda a: a.value
)
def test_platform_machine_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, MACHINES, MACHINE_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="platform.machine",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", MACHINE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(MACHINE_ALLOW))
def test_platform_machine_missing_attribute_denies(action: Action, key: str) -> None:
    attrs = {k: v for k, v in MACHINE_ALLOW.items() if k != key}
    assert authorize(USER, action, MACHINES, attrs).effect is Effect.DENY


@pytest.mark.parametrize("action", MACHINE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(MACHINE_WRONG_TYPED))
def test_platform_machine_wrong_attribute_type_denies(action: Action, key: str) -> None:
    attrs = {**MACHINE_ALLOW, key: MACHINE_WRONG_TYPED[key]}
    assert authorize(USER, action, MACHINES, attrs).effect is Effect.DENY


# --------------------------------------------------------------------------- #
# compute.machine_credential — a box claiming and heartbeating its own machine
# --------------------------------------------------------------------------- #

CLAIM = Resource(ResourceType.MACHINE_CREDENTIAL, id="i-0123", org_id=ORG)

CLAIM_ALLOW: dict[str, object] = {
    "is_machine": True,
    "credential_live": True,
    "machine_matches": True,
    "own_standing": True,
    "runs_org_workers": False,
    "org_reached": True,
}
CLAIM_WRONG_TYPED: dict[str, object] = {
    "is_machine": "true",
    "credential_live": 1,
    "machine_matches": "yes",
    "own_standing": "true",
    "runs_org_workers": 1,
    "org_reached": None,
}


@pytest.mark.parametrize(
    ("ctx", "overrides", "effect", "reason", "message", "as_not_found"),
    [
        pytest.param(BOX, {}, Effect.ALLOW, "machine_self", "", False, id="the-box-claims"),
        pytest.param(
            USER,
            {},
            Effect.ALLOW,
            "machine_self",
            "",
            False,
            id="a-plain-session-with-a-credential-claims",
        ),
        pytest.param(
            BOX,
            {"is_machine": False, "credential_live": False, "machine_matches": False},
            Effect.DENY,
            "machine_credential_required",
            "A machine credential is required",
            False,
            id="no-credential-in-the-request",
        ),
        pytest.param(
            BOX,
            {"credential_live": False, "machine_matches": False},
            Effect.DENY,
            "credential_refused",
            "Machine credential refused",
            False,
            id="a-revoked-credential",
        ),
        pytest.param(
            BOX,
            {"machine_matches": False},
            Effect.DENY,
            "not_this_machine",
            "Machine not found",
            True,
            id="a-credential-claimed-by-another-box",
        ),
        pytest.param(
            PROXY,
            {},
            Effect.ALLOW,
            "machine_self",
            "",
            False,
            id="the-facts-decide-not-the-principal-kind",
        ),
        pytest.param(
            BOX,
            {"runs_org_workers": True},
            Effect.ALLOW,
            "machine_self",
            "",
            False,
            id="a-box-running-org-workers-keeps-its-own-standing",
        ),
        pytest.param(
            BOX,
            {"own_standing": False},
            Effect.ALLOW,
            "machine_serves_in_one_process",
            "",
            False,
            id="a-single-process-box-reaches-beyond-its-standing",
        ),
        pytest.param(
            BOX,
            {"own_standing": False, "runs_org_workers": True},
            Effect.DENY,
            "worker_credential_required",
            machine_credential.WORKER_REQUIRED_MESSAGE,
            False,
            id="a-box-running-org-workers-is-refused-beyond-its-standing",
        ),
        pytest.param(
            BOX,
            {"own_standing": False, "runs_org_workers": True, "org_reached": False},
            Effect.DENY,
            "worker_credential_required",
            machine_credential.WORKER_REQUIRED_MESSAGE,
            False,
            id="the-worker-refusal-outranks-an-org-it-does-not-reach",
        ),
        pytest.param(
            BOX,
            {"org_reached": False},
            Effect.DENY,
            "org_not_reached",
            "Machine not found",
            True,
            id="a-mint-for-an-org-with-nothing-bound-to-the-machine",
        ),
        pytest.param(
            BOX,
            {"org_reached": False, "machine_matches": False},
            Effect.DENY,
            "not_this_machine",
            "Machine not found",
            True,
            id="another-box-is-not-this-machine-before-its-orgs-are-asked",
        ),
        pytest.param(
            BOX,
            {"credential_live": False, "own_standing": False, "runs_org_workers": True},
            Effect.DENY,
            "credential_refused",
            "Machine credential refused",
            False,
            id="a-dead-credential-is-refused-as-a-credential-first",
        ),
    ],
)
def test_machine_credential_table(
    ctx: ActingContext,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
) -> None:
    _expect(
        authorize(ctx, Action.WRITE, CLAIM, {**CLAIM_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="compute.machine_credential",
        message=message,
        as_not_found=as_not_found,
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param(
            {"is_machine": False, "credential_live": False, "machine_matches": False},
            "machine_credential_required",
            id="none-presented",
        ),
        pytest.param(
            {"credential_live": False, "machine_matches": False},
            "machine_credential_refused",
            id="revoked-or-never-minted",
        ),
    ],
)
def test_a_dead_credential_is_refused_as_a_credential(
    overrides: dict[str, object], code: str
) -> None:
    """A missing or dead credential is a 401 the box stops on, named by a
    code the daemon keys off; the box that presented another box's live
    credential is the opaque not-found instead, never both."""
    from alkera_core.machine_refusals import FATAL_MACHINE_REFUSALS

    decision = authorize(BOX, Action.WRITE, CLAIM, {**CLAIM_ALLOW, **overrides})
    assert decision.unauthenticated is True
    assert decision.error_code == code
    assert code in FATAL_MACHINE_REFUSALS
    other = authorize(BOX, Action.WRITE, CLAIM, {**CLAIM_ALLOW, "machine_matches": False})
    assert other.unauthenticated is False and other.as_not_found is True
    allowed = authorize(BOX, Action.WRITE, CLAIM, CLAIM_ALLOW)
    assert allowed.unauthenticated is False


def test_the_worker_credential_refusal_is_a_coded_403() -> None:
    """A box running a worker per org that presents its machine credential on
    a chat or data route is told which credential to use, as a forbidden with
    a code — not as a dead credential (it is live and keeps its standing) and
    not as a missing resource."""
    from alkera_core.machine_refusals import (
        FATAL_MACHINE_REFUSALS,
        MACHINE_WORKER_CREDENTIAL_REQUIRED,
    )

    decision = authorize(
        BOX,
        Action.READ,
        CLAIM,
        {**CLAIM_ALLOW, "own_standing": False, "runs_org_workers": True},
    )
    assert decision.error_code == MACHINE_WORKER_CREDENTIAL_REQUIRED
    assert (decision.unauthenticated, decision.as_not_found) == (False, False)
    assert MACHINE_WORKER_CREDENTIAL_REQUIRED not in FATAL_MACHINE_REFUSALS


def test_machine_credential_refuses_a_foreign_org_resource() -> None:
    other = Resource(ResourceType.MACHINE_CREDENTIAL, id="i-0123", org_id=UUID(int=99))
    assert authorize(BOX, Action.WRITE, other, CLAIM_ALLOW).effect is Effect.DENY


@pytest.mark.parametrize(
    "other", [a for a in Action if a not in (Action.READ, Action.WRITE)], ids=lambda a: a.value
)
def test_machine_credential_refuses_every_action_but_read_and_write(other: Action) -> None:
    _expect(
        authorize(BOX, other, CLAIM, CLAIM_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="compute.machine_credential",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(CLAIM_ALLOW))
def test_machine_credential_missing_attribute_denies(key: str) -> None:
    attrs = {k: v for k, v in CLAIM_ALLOW.items() if k != key}
    assert authorize(BOX, Action.WRITE, CLAIM, attrs).effect is Effect.DENY


@pytest.mark.parametrize("key", sorted(CLAIM_WRONG_TYPED))
def test_machine_credential_wrong_attribute_type_denies(key: str) -> None:
    attrs = {**CLAIM_ALLOW, key: CLAIM_WRONG_TYPED[key]}
    assert authorize(BOX, Action.WRITE, CLAIM, attrs).effect is Effect.DENY
