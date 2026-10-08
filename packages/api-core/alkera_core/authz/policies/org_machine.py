"""Who may see, use, buy and run an org's machines.

An org machine is held by a team (the org root, or the team whose admin bought
it). Its **managers** are the org admins and the admins of that team or any
team above it: admin descends, so the roles the caller holds on the owner team,
closed under the ladder and resolved by one ancestor walk, already say it. The
people who may **use** it (pin a workspace to it, move a workspace onto it) are
its audience; a machine in the org pool serves every member, so its audience is
the whole org.

Three actions, each with the fact that says which question is asked:

- ``READ`` with ``purpose``: ``list`` (ask for the org's machines at all: any
  member; each row is then filtered by ``read``), ``read`` (a machine's name,
  spec, state and rate: managers, the audience, and every member for a pool
  machine), ``use`` (pin or move a workspace onto it: the audience, every
  member for a pool machine), ``spend`` (what it has cost this cycle and how
  long the credit lasts: managers only) and ``settings`` (the org's compute
  settings: org admins). Use is a read with a purpose rather than a verb of
  its own because it writes nothing on the machine; the workspace it lands on
  decides that write.
- ``WRITE`` with ``operation``: ``purchase`` (buy one for the owner team the
  caller names), ``add`` (attach a host the org runs by its SSH details, or
  test one before attaching it: org admins, where the deployment allows
  attaching, ``adding_enabled``), ``manage`` (rename, settings, audience, start, stop,
  replace) or ``settings`` (the org's compute settings: org admins).
- ``DELETE``: managers.

A machine the caller could not even read is a not-found on every action, so a
member outside its audience cannot learn that it exists by probing ids. One the
caller reads but may not change is a 403 naming why.

The org pool is enterprise or self-hosted only, and choosing it decides where
every member's regular chats run, so a write that puts a machine in the pool
(``sets_pool``) needs both ``org_allows_pool`` and an org admin, whoever else
may manage the machine. A purchase also needs a proven address, an offering the
org may buy (``offering_visible``; an offering the org cannot see is a
not-found) and room in the plan's quota (``quota_left``; denied with the
``machine_quota`` code, which the route answers as 429).

Each question reads every fact it needs before any refusal is decided, so a
caller that forgets one is denied rather than allowed by the branch that
happened not to consult it.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import MessageKey, Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType, Role
from alkera_core.authz.policies._facts import roles_of
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY = "org_machine.access"
AUDITED = frozenset(
    {
        "in_org",
        "roles",
        "is_org_admin",
        "in_audience",
        "use_mode",
        "email_verified",
        "purpose",
        "operation",
        "sets_pool",
        "org_allows_pool",
        "offering_visible",
        "quota_left",
        "adding_enabled",
    }
)

SUPPORTED = frozenset({Action.READ, Action.WRITE, Action.DELETE})

#: What a READ asks.
LIST_PURPOSE = "list"
READ_PURPOSE = "read"
USE_PURPOSE = "use"
SPEND_PURPOSE = "spend"
SETTINGS_PURPOSE = "settings"
PURPOSES = frozenset({LIST_PURPOSE, READ_PURPOSE, USE_PURPOSE, SPEND_PURPOSE, SETTINGS_PURPOSE})

#: What a WRITE asks.
PURCHASE = "purchase"
ADD = "add"
MANAGE = "manage"
SETTINGS = "settings"
OPERATIONS = frozenset({PURCHASE, ADD, MANAGE, SETTINGS})

#: The use mode whose audience is the whole org.
POOL = "pool"

NOT_FOUND_MESSAGE = "Machine not found"
TEAM_NOT_FOUND_MESSAGE = "Team not found"
OFFERING_NOT_FOUND_MESSAGE = "Offering not found"
MEMBER_MESSAGE = "org member role required"
SPEND_MESSAGE = "Only an admin of the team that holds this machine can see what it costs."
MANAGER_MESSAGE = "Only an admin of the team that holds this machine can change it."
BUY_MESSAGE = "Only an admin of the team that will hold the machine can buy it."
SETTINGS_READ_MESSAGE = "Only an org admin can see the organization's compute settings."
SETTINGS_MESSAGE = "Only an org admin can change the organization's compute settings."
USE_MESSAGE = "This machine isn't shared with you."
USE_CODE = "machine_not_shared"
VERIFY_EMAIL_MESSAGE = "Verify your email address to perform this action."
VERIFY_EMAIL_CODE = "email_verification_required"
POOL_PLAN_MESSAGE = "The org pool needs an Enterprise plan."
POOL_PLAN_CODE = "org_pool_unavailable"
POOL_ADMIN_MESSAGE = "Only an org admin can put a machine in the org pool."
POOL_ADMIN_CODE = "org_admin_required"
QUOTA_MESSAGE = "Your plan doesn't allow another machine."
QUOTA_CODE = "machine_quota"
ADD_MESSAGE = "Only an org admin can add a machine."
ADD_UNAVAILABLE_MESSAGE = "Adding your own machines is off in this deployment."
ADD_UNAVAILABLE_CODE = "machine_adding_unavailable"


#: Each refusal's sentence, per action and purpose: a read refused says what
#: could not be seen, a change refused what could not be changed.
MESSAGES: Mapping[MessageKey, str] = {
    (Action.READ, LIST_PURPOSE): MEMBER_MESSAGE,
    (Action.READ, SPEND_PURPOSE): SPEND_MESSAGE,
    (Action.READ, SETTINGS_PURPOSE): SETTINGS_READ_MESSAGE,
    (Action.READ, USE_PURPOSE): USE_MESSAGE,
    (Action.WRITE, PURCHASE): BUY_MESSAGE,
    (Action.WRITE, MANAGE): MANAGER_MESSAGE,
    (Action.WRITE, SETTINGS): SETTINGS_MESSAGE,
    (Action.DELETE, ""): MANAGER_MESSAGE,
}


def _message(action: Action, purpose: str) -> str:
    return _POLICY.message_for(action, purpose)


def _is_manager(attrs: Mapping[str, object]) -> bool:
    """An org admin, or an admin of the owner team or above. Both facts are
    read so neither can be forgotten."""
    is_org_admin = require_attr(attrs, "is_org_admin", bool)
    return Role.ADMIN in roles_of(attrs) or is_org_admin


def _may_use(attrs: Mapping[str, object]) -> bool:
    """The audience, and every member for a pool machine."""
    in_audience = require_attr(attrs, "in_audience", bool)
    use_mode = require_attr(attrs, "use_mode", str)
    return in_audience or use_mode == POOL


def _unverified(attrs: Mapping[str, object]) -> Decision | None:
    if require_attr(attrs, "email_verified", bool):
        return None
    return deny(
        POLICY,
        "email_verification_required",
        message=VERIFY_EMAIL_MESSAGE,
        error_code=VERIFY_EMAIL_CODE,
    )


def _pool_refusal(attrs: Mapping[str, object]) -> Decision | None:
    """A write that puts the machine in the org pool: enterprise or self-hosted,
    and an org admin. Every fact is read before either refusal."""
    sets_pool = require_attr(attrs, "sets_pool", bool)
    org_allows_pool = require_attr(attrs, "org_allows_pool", bool)
    is_org_admin = require_attr(attrs, "is_org_admin", bool)
    if not sets_pool:
        return None
    if not org_allows_pool:
        return deny(
            POLICY, "org_pool_unavailable", message=POOL_PLAN_MESSAGE, error_code=POOL_PLAN_CODE
        )
    if not is_org_admin:
        return deny(
            POLICY,
            "org_pool_needs_org_admin",
            message=POOL_ADMIN_MESSAGE,
            error_code=POOL_ADMIN_CODE,
        )
    return None


def _not_found() -> Decision:
    return deny(POLICY, "machine_not_visible", message=NOT_FOUND_MESSAGE, as_not_found=True)


def _read(attrs: Mapping[str, object], purpose: str) -> Decision:
    if purpose == LIST_PURPOSE:
        if Role.MEMBER not in roles_of(attrs):
            return deny(POLICY, "org_member_required", message=_message(Action.READ, LIST_PURPOSE))
        return allow(POLICY, "member_lists")
    if purpose == SETTINGS_PURPOSE:
        if not require_attr(attrs, "is_org_admin", bool):
            return deny(
                POLICY,
                "settings_need_org_admin",
                message=_message(Action.READ, SETTINGS_PURPOSE),
            )
        return allow(POLICY, "org_admin_reads_settings")
    manager = _is_manager(attrs)
    may_use = _may_use(attrs)
    if not (manager or may_use):
        return _not_found()
    if purpose == READ_PURPOSE:
        return allow(POLICY, "manager_reads" if manager else "audience_reads")
    if purpose == USE_PURPOSE:
        if not may_use:
            return deny(
                POLICY,
                "not_in_audience",
                message=_message(Action.READ, USE_PURPOSE),
                error_code=USE_CODE,
            )
        return allow(POLICY, "audience_uses")
    if not manager:
        return deny(POLICY, "spend_needs_manager", message=_message(Action.READ, SPEND_PURPOSE))
    return allow(POLICY, "manager_reads_spend")


def _purchase(attrs: Mapping[str, object]) -> Decision:
    manager = _is_manager(attrs)
    offering_visible = require_attr(attrs, "offering_visible", bool)
    quota_left = require_attr(attrs, "quota_left", bool)
    unverified = _unverified(attrs)
    pool = _pool_refusal(attrs)
    if not manager:
        return deny(POLICY, "team_admin_required", message=_message(Action.WRITE, PURCHASE))
    if unverified is not None:
        return unverified
    if not offering_visible:
        return deny(
            POLICY, "offering_not_visible", message=OFFERING_NOT_FOUND_MESSAGE, as_not_found=True
        )
    if pool is not None:
        return pool
    if not quota_left:
        return deny(POLICY, "machine_quota", message=QUOTA_MESSAGE, error_code=QUOTA_CODE)
    return allow(POLICY, "manager_purchases")


def _add(attrs: Mapping[str, object]) -> Decision:
    is_org_admin = require_attr(attrs, "is_org_admin", bool)
    adding_enabled = require_attr(attrs, "adding_enabled", bool)
    unverified = _unverified(attrs)
    pool = _pool_refusal(attrs)
    if not adding_enabled:
        return deny(
            POLICY,
            "adding_unavailable",
            message=ADD_UNAVAILABLE_MESSAGE,
            error_code=ADD_UNAVAILABLE_CODE,
        )
    if not is_org_admin:
        return deny(POLICY, "add_needs_org_admin", message=ADD_MESSAGE)
    if unverified is not None:
        return unverified
    if pool is not None:
        return pool
    return allow(POLICY, "org_admin_adds")


def _manage(attrs: Mapping[str, object], *, deleting: bool) -> Decision:
    manager = _is_manager(attrs)
    may_use = _may_use(attrs)
    unverified = _unverified(attrs)
    pool = None if deleting else _pool_refusal(attrs)
    if not (manager or may_use):
        return _not_found()
    if not manager:
        refused = Action.DELETE if deleting else Action.WRITE
        return deny(
            POLICY, "manager_required", message=_message(refused, "" if deleting else MANAGE)
        )
    if unverified is not None:
        return unverified
    if pool is not None:
        return pool
    return allow(POLICY, "manager_deletes" if deleting else "manager_changes")


def _settings(attrs: Mapping[str, object]) -> Decision:
    is_org_admin = require_attr(attrs, "is_org_admin", bool)
    unverified = _unverified(attrs)
    if not is_org_admin:
        return deny(POLICY, "settings_need_org_admin", message=_message(Action.WRITE, SETTINGS))
    if unverified is not None:
        return unverified
    return allow(POLICY, "org_admin_sets_settings")


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    in_org = require_attr(attrs, "in_org", bool)
    if ctx.is_machine:
        # A box on its own credential holds the machine it runs on and nothing
        # about the org's machines: never their list, never their settings.
        return _not_found()
    if action is Action.READ:
        purpose = require_attr(attrs, "purpose", str)
        if purpose not in PURPOSES:
            return deny(POLICY, "unknown_purpose", message="Not allowed")
        if not in_org:
            return _not_found()
        return _read(attrs, purpose)
    if action is Action.DELETE:
        if not in_org:
            return _not_found()
        return _manage(attrs, deleting=True)
    operation = require_attr(attrs, "operation", str)
    if operation not in OPERATIONS:
        return deny(POLICY, "unknown_operation", message="Not allowed")
    if operation == PURCHASE:
        if not in_org:
            # The owner team the caller named is not in their org.
            return deny(
                POLICY, "team_not_in_org", message=TEAM_NOT_FOUND_MESSAGE, as_not_found=True
            )
        return _purchase(attrs)
    if not in_org:
        return _not_found()
    if operation == ADD:
        return _add(attrs)
    if operation == SETTINGS:
        return _settings(attrs)
    return _manage(attrs, deleting=False)


_POLICY = Policy(
    name=POLICY,
    resource_type=ResourceType.ORG_MACHINE,
    decide=decide,
    audited_attrs=AUDITED,
    messages=MESSAGES,
)
register(_POLICY)

__all__ = [
    "ADD",
    "ADD_UNAVAILABLE_CODE",
    "AUDITED",
    "LIST_PURPOSE",
    "MANAGE",
    "MANAGER_MESSAGE",
    "MESSAGES",
    "NOT_FOUND_MESSAGE",
    "OPERATIONS",
    "POLICY",
    "POOL",
    "PURCHASE",
    "PURPOSES",
    "QUOTA_CODE",
    "READ_PURPOSE",
    "SETTINGS",
    "SETTINGS_MESSAGE",
    "SETTINGS_PURPOSE",
    "SETTINGS_READ_MESSAGE",
    "SPEND_MESSAGE",
    "SPEND_PURPOSE",
    "SUPPORTED",
    "USE_CODE",
    "USE_PURPOSE",
    "decide",
]
