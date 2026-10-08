"""authorize() denies by default and decides from registered policies.

Every branch of the engine and of every registered policy is a row in a table
here: effect, reason, whether the denial is opaque (404) and the exact message
the HTTP layer will show. The messages are contracts — the CLI displays some of
them verbatim. Each policy's required facts are driven twice more, once with
each fact removed and once with it wrongly typed, so a caller that forgets one
is denied rather than admitted by the branch that happened not to need it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from itertools import product
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz import (
    NEVER_AUDITED_KEYS,
    ActingContext,
    Action,
    CredentialKind,
    Decision,
    Effect,
    MissingAttributeError,
    Policy,
    Resource,
    ResourceType,
    Role,
    allow,
    audited_attrs,
    authorize,
    policy_for,
    register,
    registered_policies,
    require_attr,
    temporarily_registered,
)
from alkera_core.authz.policies import (
    account as account_policy,
)
from alkera_core.authz.policies import (
    chat,
    chat_template,
    compute,
    compute_grant,
    compute_offering,
    connector,
    machine_credential,
    platform_ban,
    platform_billing,
    platform_machine,
    platform_org_live_editing,
    platform_org_slack,
    platform_org_sso_domains,
    platform_org_storage,
    workspace_object,
)
from alkera_core.authz.policies import (
    file_lease as file_lease_policy,
)
from alkera_core.authz.policies import (
    files as files_policy,
)
from alkera_core.authz.policies import (
    notebook as notebook_policy,
)
from alkera_core.authz.policies import (
    org as org_policy,
)
from alkera_core.authz.policies import (
    org_audit as org_audit_policy,
)
from alkera_core.authz.policies import (
    org_machine as org_machine_policy,
)
from alkera_core.authz.policies import (
    personal_box as personal_box_policy,
)
from alkera_core.authz.policies import (
    team as team_policy,
)
from alkera_core.authz.policies import (
    team_allocation as team_allocation_policy,
)
from alkera_core.authz.policies import (
    team_membership as membership_policy,
)
from alkera_core.authz.policies import (
    team_storage_cap as team_storage_cap_policy,
)
from alkera_core.authz.policies import (
    workspace as workspace_policy,
)
from alkera_core.authz.policies import (
    workspace_machine as workspace_machine_policy,
)
from alkera_core.files.authz import ladder as files_ladder
from alkera_core.files.authz.actions import FilesAction
from alkera_core.models._enums import TeamRole

ORG = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_ORG = UUID("00000000-0000-4000-8000-00000000000f")
TEAM = UUID("00000000-0000-4000-8000-00000000000b")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
#: Another member of the same org — the person a personal row is NOT for.
OTHER_USER_ID = UUID("00000000-0000-4000-8000-0000000000ff")
TOKEN_ID = UUID("00000000-0000-4000-8000-000000000002")

USER = ActingContext.for_user(user_id=USER_ID, org_id=ORG, email="m@example.com")
AGENT = ActingContext.for_agent(
    user_id=USER_ID, org_id=ORG, email="m@example.com", session_id="sess-1"
)
PAT = ActingContext.for_pat(
    token_id=TOKEN_ID, org_id=ORG, label="pat", user_id=USER_ID, email="m@example.com"
)
SERVICE_CI = ActingContext.for_service(
    token_id=TOKEN_ID, org_id=ORG, label="ci", credential=CredentialKind.CI_TOKEN
)
SERVICE_PROXY = ActingContext.for_service(
    token_id=TOKEN_ID, org_id=ORG, label="proxy", credential=CredentialKind.PROXY_TOKEN
)

MEMBER_ID = UUID("00000000-0000-4000-8000-000000000003")
OTHER_TEAM = UUID("00000000-0000-4000-8000-00000000000c")

#: A member of the org who owns nothing in these tables.
OTHER_USER = ActingContext.for_user(user_id=MEMBER_ID, org_id=ORG, email="other@example.com")
OWNER_AGENT = ActingContext.for_agent(
    user_id=USER_ID, org_id=ORG, email="m@example.com", session_id="sess-9"
)
OTHER_AGENT = ActingContext.for_agent(
    user_id=MEMBER_ID, org_id=ORG, email="other@example.com", session_id="sess-8"
)
OWNER_PAT = ActingContext.for_pat(
    token_id=TOKEN_ID, org_id=ORG, label="pat", user_id=USER_ID, email="m@example.com"
)
#: The org's workspace machines, and the box's socket speaking as one of them
#: with the operator's (USER's) or another member's device token.
MACHINE_ID = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
OTHER_MACHINE_ID = "7b3d2c5e-4f20-4c4d-9f7e-93c5e1b2d333"
MACHINE_AGENT = ActingContext.for_agent(
    user_id=USER_ID, org_id=ORG, email="m@example.com", session_id=MACHINE_ID
)
OTHER_MEMBERS_MACHINE_AGENT = ActingContext.for_agent(
    user_id=MEMBER_ID, org_id=ORG, email="other@example.com", session_id=MACHINE_ID
)

CONNECTOR = Resource(ResourceType.CONNECTOR, id="conn-1", org_id=ORG, team_id=TEAM)
CHAT = Resource(ResourceType.CHAT, id="sess-1", org_id=ORG, team_id=TEAM)
CHAT_TEMPLATE = Resource(ResourceType.CHAT_TEMPLATE, id="tpl-1", org_id=ORG, team_id=TEAM)
OBJECT = Resource(ResourceType.WORKSPACE_OBJECT, id="obj-1", org_id=ORG, team_id=TEAM)
WORKSPACE = Resource(ResourceType.WORKSPACE, id="ws-1", org_id=ORG)
MACHINE = Resource(ResourceType.COMPUTE_MACHINE, id="alloc-1", org_id=ORG)

#: Attributes that reach each policy's allow branch with every required key present.
CONNECTOR_ALLOW: dict[str, object] = {
    "member_entitled": True,
    "enabled": True,
    "auth_mode": "shared",
    "has_shared_secret": True,
    "team_id": str(TEAM),
    # Empty is a TEAM row: nobody owns it personally, so entitlement decides.
    "owner_user_id": "",
}
#: The facts of an org-visible chat owned by ``USER``: the READ allow branch.
CHAT_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER}),
    "is_org_admin": False,
    "owner_user_id": str(USER_ID),
    "visibility_scope": "org",
    "team_ids": frozenset({TEAM}),
    "email_verified": True,
    "team_id": "",
    # No grant on the chat's node: a chat is private until shared, so only its
    # owner (USER) and the machine it is bound to read it.
    "shared_role": "",
    "bound_machine_id": "",
    # A person's request: no machine assertion was made, let alone verified.
    "asserted_machine_verified": False,
    # The deployment default: a private chat is the opaque not-found to an org
    # admin nobody shared it with, exactly as it is to any other member.
    "admin_reads_private": False,
    # A workspace of one: the chat's own rung decides who drives it.
    "in_multi_chat_workspace": False,
    "workspace_role": "",
    # Its owner is an active member: offboarding does not apply.
    "owner_departed": False,
    # Its payer (the owner) still reads it, so a send may bill them.
    "payer_stands": True,
}
#: A chat in a workspace that holds several chats, owned by ``USER``.
CHAT_IN_WORKSPACE: dict[str, object] = {**CHAT_ALLOW, "in_multi_chat_workspace": True}
#: The facts of an org-visible template owned by ``USER``: the READ allow branch.
#: Unlike a chat, a template's audience admits readers — it exists to be handed
#: round — so ``shared_role`` starts empty and the owner still reads.
TEMPLATE_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER}),
    "is_org_admin": False,
    "owner_user_id": str(USER_ID),
    "visibility_scope": "org",
    "team_ids": frozenset({TEAM}),
    "email_verified": True,
    "team_id": "",
    "shared_role": "",
}


def _template_attrs(**overrides: object) -> dict[str, object]:
    return {**TEMPLATE_ALLOW, **overrides}


#: The facts of a private project workspace owned by ``USER``, nobody else
#: holding a rung on its folder: the READ allow branch for the owner.
WORKSPACE_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER}),
    "is_org_admin": False,
    "owner_user_id": str(USER_ID),
    "visibility_scope": "private",
    "team_ids": frozenset({TEAM}),
    "email_verified": True,
    "team_id": "",
    "shared_role": "",
    "admin_reads_private": False,
    "is_main": False,
    "owner_departed": False,
    "bound_machine_id": "",
}


def _workspace_attrs(**overrides: object) -> dict[str, object]:
    return {**WORKSPACE_ALLOW, **overrides}


#: Moving ``USER``'s workspace onto an org machine they may use.
WORKSPACE_MACHINE = Resource(
    ResourceType.WORKSPACE_MACHINE, id="00000000-0000-4000-8000-0000000000d1", org_id=ORG
)
MACHINE_MOVE_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER}),
    "is_org_admin": False,
    "owner_user_id": str(USER_ID),
    "visibility_scope": "private",
    "team_ids": frozenset({TEAM}),
    "email_verified": True,
    "shared_role": "",
    "admin_reads_private": False,
    "target_kind": "org_machine",
    "target_usable": True,
}


def _machine_move_attrs(**overrides: object) -> dict[str, object]:
    return {**MACHINE_MOVE_ALLOW, **overrides}


#: The same for an org-visible object owned by ``USER``.
OBJECT_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER}),
    "is_org_admin": False,
    "owner_user_id": str(USER_ID),
    "visibility_scope": "org",
    "team_ids": frozenset({TEAM}),
    "email_verified": True,
    "held_by_machine": False,
}
#: One wrongly-typed stand-in per required fact, shared by both object policies.
OBJECT_WRONG: dict[str, object] = {
    "in_org": "true",
    "roles": ["member"],
    "is_org_admin": 1,
    "owner_user_id": USER_ID,
    "visibility_scope": 7,
    "team_ids": [str(TEAM)],
    "email_verified": 1,
}

COMPUTE_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.MEMBER, Role.VIEWER}),
    "email_verified": True,
    "is_owner": True,
    "lifecycle": "session",
    "controls": False,
}
#: The org-integration policy: an org-wide change takes org-root authority.
ORG_RESOURCE = Resource(ResourceType.ORG, id=str(ORG), org_id=ORG)
ORG_ALLOW: dict[str, object] = {
    "integration": "slack",
    "operation": "connect",
    "is_org_root": True,
    "org_member": True,
    "org_admin": True,
}
#: Only the facts ADMIN requires; the READ branch's extra fact (`org_member`)
#: has its own cases below, because removing it from an ADMIN request is
#: correctly harmless.
ORG_WRONG_TYPED: dict[str, object] = {
    "is_org_root": "yes",
    "org_admin": "true",
}

#: The org's agent-activity trail: a member's own daemon writing into it.
ORG_AUDIT_RESOURCE = Resource(ResourceType.ORG_AUDIT, id=str(ORG), org_id=ORG)
ORG_AUDIT_ALLOW: dict[str, object] = {
    "is_org_root": True,
    "org_member": True,
}
ORG_AUDIT_WRONG_TYPED: dict[str, object] = {
    "is_org_root": "yes",
    "org_member": 1,
}

#: A person's account as a whole: its export and its deletion. No org: the
#: decision is about the identity.
ACCOUNT_RESOURCE = Resource(ResourceType.ACCOUNT, id=str(USER_ID))
ACCOUNT_ALLOW: dict[str, object] = {
    "is_self": True,
    "acting_directly": True,
    "platform_role": "",
}
ACCOUNT_WRONG_TYPED: dict[str, object] = {
    "is_self": "yes",
    "acting_directly": 1,
    "platform_role": None,
}

#: A box on a person's own hardware, as its person sees it. No org: the
#: decision is about the identity.
PERSONAL_BOX_RESOURCE = Resource(ResourceType.PERSONAL_BOX, id="box-1")
PERSONAL_BOX_ALLOW: dict[str, object] = {"is_owner": True}
PERSONAL_BOX_WRONG_TYPED: dict[str, object] = {"is_owner": "yes"}

#: A team's usage reading: the same team-admin-by-descent relationship as a
#: pool read, taken at the org root when nothing narrower is named.
TEAM_RESOURCE = Resource(ResourceType.TEAM, id=str(TEAM), org_id=ORG, team_id=TEAM)
TEAM_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER}),
    "scope": "team",
}
TEAM_WRONG_TYPED: dict[str, object] = {"in_org": "true", "roles": ["admin"]}

#: A role write on a team for someone nothing reaches from above: the plain
#: case every team-admin has always had.
MEMBERSHIP_RESOURCE = Resource(
    ResourceType.TEAM_MEMBERSHIP, id=f"{TEAM}:{UUID(int=9)}", org_id=ORG, team_id=TEAM
)
MEMBERSHIP_ALLOW: dict[str, object] = {
    "in_org": True,
    "roles": frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER}),
    "requested_role": TeamRole.MEMBER,
    "descent_role": None,
    "descent_from": None,
}
MEMBERSHIP_WRONG_TYPED: dict[str, object] = {
    "in_org": "true",
    "roles": ["admin"],
    "requested_role": "member",
    "descent_role": "admin",
    "descent_from": 7,
}

COMPUTE_WRONG_TYPED: dict[str, object] = {
    "in_org": "true",
    "roles": ["member"],
    "email_verified": 1,
    "is_owner": "yes",
    "lifecycle": 7,
    "controls": "yes",
}

#: Per registered policy: module, resource, its action, allow attrs, and the
#: required attrs each paired with a wrong-typed stand-in.
NOTEBOOK = Resource(ResourceType.NOTEBOOK, id="node-nb", org_id=ORG, team_id=TEAM)
#: A person who can edit the notebook and the folder its kernel binds, under a
#: lease that admits writes.
NOTEBOOK_ALLOW: dict[str, object] = {
    "rung": "writer",
    "scope_held": True,
    "scope_rung": "writer",
    "lease_admits_writes": True,
}
NOTEBOOK_WRONG_TYPED: dict[str, object] = {
    "rung": None,
    "scope_held": "yes",
    "scope_rung": 2,
    "lease_admits_writes": "yes",
}

REGISTERED: list[tuple[Any, Resource, Action, dict[str, object], dict[str, object]]] = [
    (notebook_policy, NOTEBOOK, Action.RUN, NOTEBOOK_ALLOW, dict(NOTEBOOK_WRONG_TYPED)),
    (
        connector,
        CONNECTOR,
        Action.FETCH_CREDENTIAL,
        CONNECTOR_ALLOW,
        {
            "member_entitled": "yes",
            "enabled": 1,
            "auth_mode": 7,
            "has_shared_secret": "true",
            "owner_user_id": UUID(int=7),
        },
    ),
    (chat, CHAT, Action.READ, CHAT_ALLOW, {**OBJECT_WRONG, "team_id": TEAM}),
    (
        chat_template,
        CHAT_TEMPLATE,
        Action.READ,
        TEMPLATE_ALLOW,
        {**OBJECT_WRONG, "team_id": TEAM, "shared_role": 7},
    ),
    (
        workspace_object,
        OBJECT,
        Action.READ,
        OBJECT_ALLOW,
        {**OBJECT_WRONG, "held_by_machine": "yes"},
    ),
    (
        workspace_policy,
        WORKSPACE,
        Action.READ,
        WORKSPACE_ALLOW,
        {
            **OBJECT_WRONG,
            "team_id": TEAM,
            "shared_role": 7,
            "admin_reads_private": "no",
            "is_main": 0,
            "owner_departed": "yes",
        },
    ),
    (
        workspace_machine_policy,
        WORKSPACE_MACHINE,
        Action.WRITE,
        MACHINE_MOVE_ALLOW,
        {
            **OBJECT_WRONG,
            "shared_role": 7,
            "admin_reads_private": "no",
            "target_kind": "a_machine_kind_nobody_named",
            "target_usable": "yes",
        },
    ),
    (org_policy, ORG_RESOURCE, Action.ADMIN, ORG_ALLOW, dict(ORG_WRONG_TYPED)),
    (
        org_audit_policy,
        ORG_AUDIT_RESOURCE,
        Action.WRITE,
        ORG_AUDIT_ALLOW,
        dict(ORG_AUDIT_WRONG_TYPED),
    ),
    (team_policy, TEAM_RESOURCE, Action.READ, TEAM_ALLOW, dict(TEAM_WRONG_TYPED)),
    (
        account_policy,
        ACCOUNT_RESOURCE,
        Action.DELETE,
        ACCOUNT_ALLOW,
        dict(ACCOUNT_WRONG_TYPED),
    ),
    (
        personal_box_policy,
        PERSONAL_BOX_RESOURCE,
        Action.DELETE,
        PERSONAL_BOX_ALLOW,
        dict(PERSONAL_BOX_WRONG_TYPED),
    ),
    (
        membership_policy,
        MEMBERSHIP_RESOURCE,
        Action.WRITE,
        MEMBERSHIP_ALLOW,
        dict(MEMBERSHIP_WRONG_TYPED),
    ),
]
REGISTERED_IDS = [
    "notebook",
    "connector",
    "chat",
    "chat_template",
    "workspace_object",
    "workspace",
    "workspace_machine",
    "org",
    "org_audit",
    "team",
    "account",
    "personal_box",
    "team_membership",
]
#: Every action a policy decides, so the unsupported-action table asks each
#: policy only about verbs it does not know.
SUPPORTED_ACTIONS: dict[Any, frozenset[Action]] = {
    notebook_policy: notebook_policy.SUPPORTED,
    connector: connector.SUPPORTED,
    chat: chat.SUPPORTED,
    chat_template: chat_template.SUPPORTED,
    workspace_object: workspace_object.SUPPORTED,
    workspace_policy: workspace_policy.SUPPORTED,
    workspace_machine_policy: workspace_machine_policy.SUPPORTED,
    org_policy: org_policy.SUPPORTED,
    org_audit_policy: org_audit_policy.SUPPORTED,
    team_policy: team_policy.SUPPORTED,
    account_policy: account_policy.SUPPORTED,
    personal_box_policy: personal_box_policy.SUPPORTED,
    membership_policy: membership_policy.SUPPORTED,
}
RESOURCE_TYPE_OF: dict[Any, ResourceType] = {
    notebook_policy: ResourceType.NOTEBOOK,
    connector: ResourceType.CONNECTOR,
    chat: ResourceType.CHAT,
    chat_template: ResourceType.CHAT_TEMPLATE,
    workspace_object: ResourceType.WORKSPACE_OBJECT,
    workspace_policy: ResourceType.WORKSPACE,
    workspace_machine_policy: ResourceType.WORKSPACE_MACHINE,
    compute: ResourceType.COMPUTE_MACHINE,
    compute_grant: ResourceType.COMPUTE_GRANT,
    compute_offering: ResourceType.COMPUTE_OFFERING,
    org_policy: ResourceType.ORG,
    org_audit_policy: ResourceType.ORG_AUDIT,
    platform_ban: ResourceType.PLATFORM_BAN,
    team_policy: ResourceType.TEAM,
    membership_policy: ResourceType.TEAM_MEMBERSHIP,
    team_allocation_policy: ResourceType.TEAM_ALLOCATION,
    org_machine_policy: ResourceType.ORG_MACHINE,
    team_storage_cap_policy: ResourceType.TEAM_STORAGE_CAP,
    platform_org_storage: ResourceType.PLATFORM_ORG_STORAGE,
    platform_org_live_editing: ResourceType.PLATFORM_ORG_LIVE_EDITING,
    platform_org_sso_domains: ResourceType.PLATFORM_ORG_SSO_DOMAINS,
    platform_org_slack: ResourceType.PLATFORM_ORG_SLACK,
    platform_machine: ResourceType.PLATFORM_MACHINE,
    machine_credential: ResourceType.MACHINE_CREDENTIAL,
    file_lease_policy: ResourceType.FILE_LEASE,
    platform_billing: ResourceType.PLATFORM_BILLING,
    account_policy: ResourceType.ACCOUNT,
    personal_box_policy: ResourceType.PERSONAL_BOX,
}
UNREGISTERED_TYPES = [
    ResourceType.GATE_RUN,
    ResourceType.ARTIFACT,
]


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


def _per_required_attr() -> list[Any]:
    return [
        pytest.param(m, r, a, ok, wrong, key, id=f"{pid}-{key}")
        for (m, r, a, ok, wrong), pid in zip(REGISTERED, REGISTERED_IDS, strict=True)
        for key in wrong
    ]


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


#: Every policy the open platform registers on import, by resource type.
OPEN_POLICIES = {
    (ResourceType.CONNECTOR, "connector.credential"),
    (ResourceType.CHAT, "chat.access"),
    (ResourceType.CHAT_TEMPLATE, "chat_template.access"),
    (ResourceType.WORKSPACE_OBJECT, "objects.access"),
    (ResourceType.WORKSPACE, "workspace.access"),
    (ResourceType.WORKSPACE_MACHINE, "workspace_machine.move"),
    (ResourceType.COMPUTE_MACHINE, "compute.machine"),
    (ResourceType.COMPUTE_GRANT, "compute.grant"),
    (ResourceType.COMPUTE_OFFERING, "compute.offering"),
    (ResourceType.ORG, "org.integration"),
    (ResourceType.ORG_AUDIT, "org_audit.agent_report"),
    (ResourceType.FILE_NODE, "files.access"),
    (ResourceType.FILE_LEASE, "files.lease_kind"),
    (ResourceType.NOTEBOOK, "notebook.run"),
    (ResourceType.PLATFORM_BAN, "platform.ban"),
    (ResourceType.TEAM, "team.usage_read"),
    (ResourceType.TEAM_MEMBERSHIP, "team_membership.role_write"),
    (ResourceType.TEAM_ALLOCATION, "team_allocation.set"),
    (ResourceType.ORG_MACHINE, "org_machine.access"),
    (ResourceType.TEAM_STORAGE_CAP, "storage.team_member_cap"),
    (ResourceType.PLATFORM_ORG_STORAGE, "platform.org_storage"),
    (ResourceType.PLATFORM_ORG_LIVE_EDITING, "platform.org_live_editing"),
    (ResourceType.PLATFORM_ORG_SSO_DOMAINS, "platform.org_sso_domains"),
    (ResourceType.PLATFORM_ORG_SLACK, "platform.org_slack"),
    (ResourceType.PLATFORM_MACHINE, "platform.machine"),
    (ResourceType.MACHINE_CREDENTIAL, "compute.machine_credential"),
    (ResourceType.PLATFORM_BILLING, "platform.billing"),
    (ResourceType.ACCOUNT, "account.lifecycle"),
    (ResourceType.PERSONAL_BOX, "compute.personal_box"),
}


def test_importing_authz_registers_exactly_the_open_policies() -> None:
    """In a fresh interpreter, ``alkera_core.authz`` registers the open
    platform's policies and nothing else: a private policy (billing's pool cap)
    arrives only when its extension installs it."""
    probe = (
        "import json\n"
        "from alkera_core.authz import registered_policies as found\n"
        "print(json.dumps(sorted([p.resource_type.value, p.name] for p in found())))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )
    found = {(ResourceType(rt), name) for rt, name in json.loads(result.stdout.splitlines()[-1])}
    assert found == OPEN_POLICIES


def test_the_open_policies_are_registered_and_no_unregistered_type_has_one() -> None:
    assert OPEN_POLICIES <= {(p.resource_type, p.name) for p in registered_policies()}
    for resource_type in UNREGISTERED_TYPES:
        assert policy_for(resource_type) is None


@pytest.mark.parametrize(
    ("module", "allowlist"),
    [
        pytest.param(
            connector,
            {
                "member_entitled",
                "enabled",
                "auth_mode",
                "has_shared_secret",
                "team_id",
                "owner_user_id",
                "for_user_id",
                "chat_bound_machine_id",
                "chat_id",
                "workspace_id",
                "source_visible",
                "source_managed",
                "dest_personal",
                "dest_admin",
                "dest_team_id",
            },
            id="connector",
        ),
        pytest.param(
            platform_billing,
            {"platform_staff", "platform_admin", "operation"},
            id="platform_billing",
        ),
        pytest.param(
            chat,
            {
                "in_org",
                "roles",
                "is_org_admin",
                "owner_user_id",
                "visibility_scope",
                "team_ids",
                "email_verified",
                "team_id",
                "publisher_machine_ids",
                "shared_role",
                "bound_machine_id",
                "asserted_machine_verified",
                "admin_reads_private",
                "in_multi_chat_workspace",
                "workspace_role",
                "owner_departed",
                "payer_stands",
            },
            id="chat",
        ),
        pytest.param(
            chat_template,
            {
                "in_org",
                "roles",
                "is_org_admin",
                "owner_user_id",
                "visibility_scope",
                "team_ids",
                "email_verified",
                "team_id",
                "shared_role",
            },
            id="chat_template",
        ),
        pytest.param(
            workspace_object,
            {
                "in_org",
                "roles",
                "is_org_admin",
                "owner_user_id",
                "visibility_scope",
                "team_ids",
                "email_verified",
                "held_by_machine",
            },
            id="workspace_object",
        ),
        pytest.param(
            workspace_policy,
            {
                "in_org",
                "roles",
                "is_org_admin",
                "owner_user_id",
                "visibility_scope",
                "team_ids",
                "email_verified",
                "team_id",
                "shared_role",
                "admin_reads_private",
                "is_main",
                "owner_departed",
                "bound_machine_id",
            },
            id="workspace",
        ),
        pytest.param(
            compute,
            {"in_org", "roles", "email_verified", "is_owner", "lifecycle", "controls"},
            id="compute",
        ),
        pytest.param(
            compute_grant,
            {"platform_staff", "platform_admin", "org_exists", "target_org_id"},
            id="compute_grant",
        ),
        pytest.param(
            compute_offering,
            {
                "operation",
                "platform_staff",
                "platform_admin",
                "org_exists",
                "in_org",
                "is_member",
                "email_verified",
            },
            id="compute_offering",
        ),
        pytest.param(
            platform_ban,
            {"platform_staff", "platform_admin", "kind", "operation"},
            id="platform_ban",
        ),
        pytest.param(
            platform_org_storage,
            {"platform_staff", "platform_admin", "operation"},
            id="platform_org_storage",
        ),
        pytest.param(
            platform_org_live_editing,
            {"platform_staff", "platform_admin", "operation"},
            id="platform_org_live_editing",
        ),
        pytest.param(
            platform_org_sso_domains,
            {"platform_staff", "platform_admin", "operation"},
            id="platform_org_sso_domains",
        ),
        pytest.param(
            platform_org_slack,
            {"platform_staff", "platform_admin", "operation"},
            id="platform_org_slack",
        ),
        pytest.param(
            platform_machine,
            {"platform_staff", "platform_admin", "operation", "org_exists"},
            id="platform_machine",
        ),
        pytest.param(
            machine_credential,
            {
                "is_machine",
                "credential_live",
                "machine_matches",
                "own_standing",
                "runs_org_workers",
                "org_reached",
            },
            id="machine_credential",
        ),
        pytest.param(
            notebook_policy,
            {"rung", "scope_held", "scope_rung", "lease_admits_writes", "agent_chat_in_workspace"},
            id="notebook",
        ),
        pytest.param(
            file_lease_policy,
            {
                "purpose",
                "node_kind",
                "inside_kind",
                "readable",
                "machine_verified",
                "runs_here",
                "full_access",
                "owner_rung",
                "org_admin",
                "holder_purpose",
                "reason_given",
                "reason",
            },
            id="file_lease",
        ),
        pytest.param(
            membership_policy,
            {"in_org", "roles", "requested_role", "descent_role", "descent_from"},
            id="team_membership",
        ),
        pytest.param(
            team_allocation_policy,
            {
                "in_org",
                "roles",
                "email_verified",
                "is_org_root",
                "is_admin_above",
                "exceeds_ceiling",
                "ceiling_amount",
                "ceiling_team",
            },
            id="team_allocation",
        ),
        pytest.param(
            org_machine_policy,
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
            },
            id="org_machine",
        ),
        pytest.param(
            team_storage_cap_policy,
            {
                "in_org",
                "roles",
                "email_verified",
                "is_org_admin",
                "is_admin_above",
                "target_is_self",
                "raises_limit",
                "exceeds_ceiling",
                "ceiling_amount",
                "ceiling_team",
            },
            id="team_storage_cap",
        ),
        pytest.param(
            account_policy,
            {"is_self", "acting_directly", "platform_role"},
            id="account",
        ),
        pytest.param(personal_box_policy, {"is_owner"}, id="personal_box"),
    ],
)
def test_policy_allowlists_are_pinned(module: Any, allowlist: set[str]) -> None:
    """The allowlist is what a decision row may carry — a contract, not a detail."""
    policy = policy_for(RESOURCE_TYPE_OF[module])
    assert policy is not None
    assert policy.audited_attrs == frozenset(allowlist) == module.AUDITED
    assert policy.name == module.POLICY
    assert policy.decide is module.decide
    assert not policy.audited_attrs & NEVER_AUDITED_KEYS


def test_register_rejects_a_duplicate_resource_type() -> None:
    dup = Policy(
        name="connector.other",
        resource_type=ResourceType.CONNECTOR,
        decide=lambda *_: None,
        audited_attrs=frozenset(),
    )
    with pytest.raises(ValueError, match="already registered"):
        register(dup)
    with pytest.raises(ValueError, match="already registered"), temporarily_registered(dup):
        pass
    registered = policy_for(ResourceType.CONNECTOR)
    assert registered is not None
    assert registered.name == "connector.credential"


@pytest.mark.parametrize("key", sorted(NEVER_AUDITED_KEYS))
def test_register_rejects_never_audited_keys(key: str) -> None:
    bad = Policy(
        name="artifact.bad",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: None,
        audited_attrs=frozenset({"ok", key}),
    )
    with pytest.raises(ValueError, match="never-audited"):
        register(bad)
    assert policy_for(ResourceType.ARTIFACT) is None, "a refused policy leaves no trace"


#: Verbs a refusal of a read must not use: it refused a look, not a change.
CHANGE_VERBS = re.compile(r"\b(change|edit|delete|remove|modify|update)\b", re.IGNORECASE)


@pytest.mark.parametrize(
    "policy", [p for p in registered_policies() if p.messages], ids=lambda p: p.name
)
def test_a_refused_read_never_reads_as_a_refused_change(policy: Policy) -> None:
    reads = {key: text for key, text in policy.messages.items() if key[0] is Action.READ}
    worded_as_changes = {key: text for key, text in reads.items() if CHANGE_VERBS.search(text)}
    assert worded_as_changes == {}


def test_the_read_copy_check_covers_the_policy_it_was_written_for() -> None:
    """The check above runs over every policy that declares its sentences; the
    org machine policy, whose read refusals once said "change", is one."""
    declared = {p.name for p in registered_policies() if p.messages}
    assert org_machine_policy.POLICY in declared


@pytest.mark.parametrize(
    "text", ["You cannot change this.", "Only an admin may edit it.", "Ask to delete it."]
)
def test_the_change_verb_check_catches_change_wording(text: str) -> None:
    assert CHANGE_VERBS.search(text)


def test_register_rejects_one_sentence_for_a_read_and_a_change() -> None:
    shared = "Only an admin may touch this."
    bad = Policy(
        name="artifact.bad",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: None,
        audited_attrs=frozenset(),
        messages={(Action.READ, "spend"): shared, (Action.WRITE, "manage"): shared},
    )
    with pytest.raises(ValueError, match="same sentence"):
        register(bad)
    assert policy_for(ResourceType.ARTIFACT) is None, "a refused policy leaves no trace"


def test_message_for_falls_back_from_the_purpose_to_the_action_to_not_allowed() -> None:
    policy = Policy(
        name="artifact.messages",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: None,
        audited_attrs=frozenset(),
        messages={(Action.READ, "spend"): "No spend.", (Action.READ, ""): "No look."},
    )
    assert policy.message_for(Action.READ, "spend") == "No spend."
    assert policy.message_for(Action.READ, "list") == "No look."
    assert policy.message_for(Action.WRITE, "manage") == "Not allowed"


def test_temporarily_registered_is_removed_on_exit_even_on_error() -> None:
    policy = Policy(
        name="artifact.tmp",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: None,
        audited_attrs=frozenset(),
    )
    with temporarily_registered(policy):
        assert policy_for(ResourceType.ARTIFACT) is policy
    assert policy_for(ResourceType.ARTIFACT) is None
    with pytest.raises(RuntimeError), temporarily_registered(policy):
        assert policy_for(ResourceType.ARTIFACT) is policy
        raise RuntimeError("boom")
    assert policy_for(ResourceType.ARTIFACT) is None


# ---------------------------------------------------------------------------
# require_attr
# ---------------------------------------------------------------------------


def test_missing_attribute_is_a_key_error_naming_the_key() -> None:
    assert issubclass(MissingAttributeError, KeyError)
    with pytest.raises(MissingAttributeError) as info:
        require_attr({}, "enabled", bool)
    assert info.value.args[0] == "enabled"


@pytest.mark.parametrize(
    ("attrs", "key", "typ", "expected"),
    [
        pytest.param({"enabled": True}, "enabled", bool, True, id="bool"),
        pytest.param({"enabled": False}, "enabled", bool, False, id="bool-false"),
        pytest.param({"mode": "shared"}, "mode", str, "shared", id="str"),
        pytest.param({"role": Role.ADMIN}, "role", Role, Role.ADMIN, id="enum"),
        pytest.param({"n": 0}, "n", int, 0, id="int-zero"),
    ],
)
def test_require_attr_returns_a_present_well_typed_value(
    attrs: dict[str, object], key: str, typ: type, expected: object
) -> None:
    assert require_attr(attrs, key, typ) == expected


@pytest.mark.parametrize(
    ("attrs", "key", "typ"),
    [
        pytest.param({}, "enabled", bool, id="absent"),
        pytest.param({"other": True}, "enabled", bool, id="different-key"),
        pytest.param({"enabled": "true"}, "enabled", bool, id="str-for-bool"),
        pytest.param({"enabled": 1}, "enabled", bool, id="int-for-bool"),
        pytest.param({"enabled": None}, "enabled", bool, id="none-for-bool"),
        pytest.param({"mode": b"shared"}, "mode", str, id="bytes-for-str"),
        pytest.param({"role": "admin"}, "role", Role, id="str-for-enum"),
    ],
)
def test_require_attr_treats_absent_or_mistyped_as_missing(
    attrs: dict[str, object], key: str, typ: type
) -> None:
    with pytest.raises(MissingAttributeError) as info:
        require_attr(attrs, key, typ)
    assert info.value.args[0] == key


# ---------------------------------------------------------------------------
# The engine: deny by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource_type", "action"),
    [
        pytest.param(rt, a, id=f"{rt.value}-{a.value}")
        for rt, a in product(UNREGISTERED_TYPES, Action)
    ],
)
def test_deny_by_default_for_every_unregistered_resource_type(
    resource_type: ResourceType, action: Action
) -> None:
    resource = Resource(resource_type, id="x", org_id=ORG)
    _expect(
        authorize(USER, action, resource, {}),
        effect=Effect.DENY,
        reason="no_policy",
        policy="none",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "_"), REGISTERED, ids=REGISTERED_IDS
)
def test_cross_org_resource_is_opaque_not_found(
    module: Any, resource: Resource, action: Action, allow_attrs: dict[str, object], _: object
) -> None:
    """The tenancy floor runs before the policy: even attrs that would allow
    are answered with a not-found that names nothing."""
    foreign = Resource(resource.type, id=resource.id, org_id=OTHER_ORG, team_id=resource.team_id)
    _expect(
        authorize(USER, action, foreign, allow_attrs),
        effect=Effect.DENY,
        reason="cross_org",
        policy=module.POLICY,
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "_"), REGISTERED, ids=REGISTERED_IDS
)
def test_cross_org_wins_over_an_unsupported_action_and_missing_attrs(
    module: Any, resource: Resource, action: Action, allow_attrs: dict[str, object], _: object
) -> None:
    foreign = Resource(resource.type, id=resource.id, org_id=OTHER_ORG)
    wrong_action = next(a for a in Action if a is not action)
    assert authorize(USER, wrong_action, foreign, {}).reason == "cross_org"


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "_"), REGISTERED, ids=REGISTERED_IDS
)
def test_a_resource_without_an_org_skips_the_tenancy_floor(
    module: Any, resource: Resource, action: Action, allow_attrs: dict[str, object], _: object
) -> None:
    unscoped = Resource(resource.type, id=resource.id)
    assert authorize(USER, action, unscoped, allow_attrs).allowed is True


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "_"), REGISTERED, ids=REGISTERED_IDS
)
def test_allow_attrs_allow(
    module: Any, resource: Resource, action: Action, allow_attrs: dict[str, object], _: object
) -> None:
    decision = authorize(USER, action, resource, allow_attrs)
    assert decision.allowed is True
    assert decision.policy == module.POLICY


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "wrong_typed", "key"), _per_required_attr()
)
def test_missing_attribute_denies(
    module: Any,
    resource: Resource,
    action: Action,
    allow_attrs: dict[str, object],
    wrong_typed: dict[str, object],
    key: str,
) -> None:
    """Remove any one required fact from an otherwise allowing request: deny."""
    _expect(
        authorize(USER, action, resource, _without(allow_attrs, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy=module.POLICY,
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "wrong_typed", "key"), _per_required_attr()
)
def test_wrong_attribute_type_denies(
    module: Any,
    resource: Resource,
    action: Action,
    allow_attrs: dict[str, object],
    wrong_typed: dict[str, object],
    key: str,
) -> None:
    """A fact of the wrong type reads as missing — a truthy string is not True."""
    attrs = {**allow_attrs, key: wrong_typed[key]}
    _expect(
        authorize(USER, action, resource, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy=module.POLICY,
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("module", "resource", "action", "allow_attrs", "_", "other_action"),
    [
        pytest.param(m, r, a, ok, wrong, other, id=f"{pid}-{other.value}")
        for (m, r, a, ok, wrong), pid in zip(REGISTERED, REGISTERED_IDS, strict=True)
        for other in Action
        if other not in SUPPORTED_ACTIONS[m]
    ],
)
def test_unsupported_action_denies(
    module: Any,
    resource: Resource,
    action: Action,
    allow_attrs: dict[str, object],
    _: object,
    other_action: Action,
) -> None:
    _expect(
        authorize(USER, other_action, resource, allow_attrs),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy=module.POLICY,
        message="Not allowed",
    )


def test_unsupported_action_is_decided_before_any_attribute_is_read() -> None:
    assert authorize(USER, Action.READ, CONNECTOR, {}).reason == "action_not_supported"


def test_no_rule_matched_when_a_policy_returns_none() -> None:
    policy = Policy(
        name="artifact.none",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: None,
        audited_attrs=frozenset(),
    )
    chat = Resource(ResourceType.ARTIFACT, id="c1", org_id=ORG)
    with temporarily_registered(policy):
        _expect(
            authorize(USER, Action.READ, chat, {}),
            effect=Effect.DENY,
            reason="no_rule_matched",
            policy="artifact.none",
            message="Not allowed",
        )
    assert authorize(USER, Action.READ, chat, {}).reason == "no_policy"


def test_a_policy_raising_missing_attribute_directly_is_a_deny() -> None:
    def decide(*_: object) -> Decision | None:
        raise MissingAttributeError("anything")

    policy = Policy(
        name="artifact.strict",
        resource_type=ResourceType.ARTIFACT,
        decide=decide,
        audited_attrs=frozenset(),
    )
    with temporarily_registered(policy):
        decision = authorize(
            USER, Action.READ, Resource(ResourceType.ARTIFACT, id="c1"), {"anything": 1}
        )
    _expect(
        decision,
        effect=Effect.DENY,
        reason="missing_attribute:anything",
        policy="artifact.strict",
        message="Not allowed",
    )


def test_a_policy_decision_is_returned_as_is() -> None:
    policy = Policy(
        name="artifact.open",
        resource_type=ResourceType.ARTIFACT,
        decide=lambda *_: allow("artifact.open", "everyone"),
        audited_attrs=frozenset(),
    )
    with temporarily_registered(policy):
        decision = authorize(
            SERVICE_CI, Action.WRITE, Resource(ResourceType.ARTIFACT, id="c1", org_id=ORG), {}
        )
    assert decision.allowed is True
    assert decision.reason == "everyone"


def test_a_policy_bug_that_raises_something_else_is_not_swallowed() -> None:
    """Only a missing fact is a deny; any other exception is a bug that must
    surface, never be mistaken for a decision."""

    def decide(*_: object) -> Decision | None:
        raise RuntimeError("policy bug")

    policy = Policy(
        name="artifact.buggy",
        resource_type=ResourceType.ARTIFACT,
        decide=decide,
        audited_attrs=frozenset(),
    )
    with temporarily_registered(policy), pytest.raises(RuntimeError, match="policy bug"):
        authorize(USER, Action.READ, Resource(ResourceType.ARTIFACT, id="c1"), {})


# ---------------------------------------------------------------------------
# connector.credential
# ---------------------------------------------------------------------------

NOT_FOUND = "Connection not found"


@pytest.mark.parametrize(
    ("ctx", "overrides", "effect", "reason", "as_not_found", "message"),
    [
        pytest.param(USER, {}, Effect.ALLOW, "member_entitled", False, "", id="user-allow"),
        pytest.param(
            AGENT, {}, Effect.ALLOW, "member_entitled", False, "", id="agent-acts-as-its-user"
        ),
        pytest.param(
            PAT, {}, Effect.ALLOW, "member_entitled", False, "", id="pat-acts-as-its-owner"
        ),
        pytest.param(SERVICE_CI, {}, Effect.DENY, "user_required", True, NOT_FOUND, id="ci-token"),
        pytest.param(
            SERVICE_PROXY, {}, Effect.DENY, "user_required", True, NOT_FOUND, id="proxy-token"
        ),
        pytest.param(
            USER,
            {"member_entitled": False},
            Effect.DENY,
            "not_entitled",
            True,
            NOT_FOUND,
            id="not-entitled",
        ),
        pytest.param(
            USER,
            {"enabled": False},
            Effect.DENY,
            "connection_disabled",
            True,
            NOT_FOUND,
            id="disabled",
        ),
        pytest.param(
            USER,
            {"auth_mode": "per_user"},
            Effect.DENY,
            "auth_mode_not_shared",
            True,
            NOT_FOUND,
            id="per-user",
        ),
        pytest.param(
            USER,
            {"auth_mode": ""},
            Effect.DENY,
            "auth_mode_not_shared",
            True,
            NOT_FOUND,
            id="no-row-auth-mode",
        ),
        pytest.param(
            USER,
            {"auth_mode": "Shared"},
            Effect.DENY,
            "auth_mode_not_shared",
            True,
            NOT_FOUND,
            id="auth-mode-is-case-sensitive",
        ),
        pytest.param(
            USER,
            {"has_shared_secret": False},
            Effect.DENY,
            "no_shared_secret",
            True,
            NOT_FOUND,
            id="no-secret",
        ),
        pytest.param(
            USER,
            {
                "member_entitled": False,
                "enabled": False,
                "auth_mode": "per_user",
                "has_shared_secret": False,
            },
            Effect.DENY,
            "not_entitled",
            True,
            NOT_FOUND,
            id="entitlement-is-checked-first",
        ),
        pytest.param(
            USER,
            {"enabled": False, "auth_mode": "per_user", "has_shared_secret": False},
            Effect.DENY,
            "connection_disabled",
            True,
            NOT_FOUND,
            id="disabled-before-auth-mode",
        ),
        pytest.param(
            USER,
            {"auth_mode": "per_user", "has_shared_secret": False},
            Effect.DENY,
            "auth_mode_not_shared",
            True,
            NOT_FOUND,
            id="auth-mode-before-secret",
        ),
        pytest.param(
            USER,
            {"owner_user_id": str(USER_ID)},
            Effect.ALLOW,
            "owner",
            False,
            "",
            id="personal-row-opens-for-its-owner",
        ),
        pytest.param(
            AGENT,
            {"owner_user_id": str(USER_ID)},
            Effect.ALLOW,
            "owner",
            False,
            "",
            id="personal-row-opens-for-an-agent-in-its-owners-session",
        ),
        pytest.param(
            PAT,
            {"owner_user_id": str(USER_ID)},
            Effect.ALLOW,
            "owner",
            False,
            "",
            id="personal-row-opens-for-its-owners-token",
        ),
        pytest.param(
            USER,
            {"owner_user_id": str(OTHER_USER_ID)},
            Effect.DENY,
            "not_owner",
            True,
            NOT_FOUND,
            id="personal-row-is-not-found-for-anyone-else",
        ),
        pytest.param(
            USER,
            # A widened entitlement query is exactly the bug this clause exists
            # to survive: entitled, and still not the owner.
            {"member_entitled": True, "owner_user_id": str(OTHER_USER_ID)},
            Effect.DENY,
            "not_owner",
            True,
            NOT_FOUND,
            id="entitlement-cannot-override-the-owner",
        ),
        pytest.param(
            USER,
            {"member_entitled": False, "owner_user_id": str(OTHER_USER_ID)},
            Effect.DENY,
            "not_entitled",
            True,
            NOT_FOUND,
            id="entitlement-is-checked-before-ownership",
        ),
        pytest.param(
            USER,
            {"owner_user_id": str(OTHER_USER_ID), "enabled": False},
            Effect.DENY,
            "not_owner",
            True,
            NOT_FOUND,
            id="ownership-is-checked-before-the-enabled-switch",
        ),
        pytest.param(
            USER,
            {"owner_user_id": str(USER_ID), "has_shared_secret": False},
            Effect.DENY,
            "no_shared_secret",
            True,
            NOT_FOUND,
            id="an-owner-still-needs-a-stored-secret",
        ),
        pytest.param(
            USER,
            # The same id, rendered without dashes. Only the canonical spelling
            # the route writes is a match; anything else fails closed rather
            # than being normalized into an allow.
            {"owner_user_id": USER_ID.hex},
            Effect.DENY,
            "not_owner",
            True,
            NOT_FOUND,
            id="owner-match-is-exact-not-normalized",
        ),
        pytest.param(
            SERVICE_CI,
            {"member_entitled": False},
            Effect.DENY,
            "user_required",
            True,
            NOT_FOUND,
            id="subject-kind-before-entitlement",
        ),
    ],
)
def test_connector_credential_table(
    ctx: ActingContext,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
) -> None:
    decision = authorize(ctx, Action.FETCH_CREDENTIAL, CONNECTOR, {**CONNECTOR_ALLOW, **overrides})
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="connector.credential",
        message=message,
        as_not_found=as_not_found,
    )


# ---------------------------------------------------------------------------
# connector move — both ends of re-addressing a connection
# ---------------------------------------------------------------------------

#: The facts a move decides on, with every one present and permissive.
MOVE_ALLOW: dict[str, object] = {
    "source_visible": True,
    "source_managed": True,
    "dest_personal": False,
    "dest_admin": True,
}
MOVE_WRONG_TYPED: dict[str, object] = {
    "source_visible": "yes",
    "source_managed": 1,
    "dest_personal": "false",
    "dest_admin": "true",
}


@pytest.mark.parametrize(
    ("ctx", "overrides", "effect", "reason", "as_not_found", "message"),
    [
        pytest.param(
            USER, {}, Effect.ALLOW, "move_to_administered_team", False, "", id="admin-both-ends"
        ),
        pytest.param(
            PAT,
            {},
            Effect.ALLOW,
            "move_to_administered_team",
            False,
            "",
            id="a-personal-token-acts-as-its-owner",
        ),
        pytest.param(
            USER,
            {"dest_personal": True, "dest_admin": False},
            Effect.ALLOW,
            "move_to_own",
            False,
            "",
            id="own-destination-needs-no-second-admin",
        ),
        pytest.param(
            AGENT,
            {},
            Effect.DENY,
            "agent_cannot_move",
            False,
            connector.AGENT_REFUSED,
            id="an-agent-never-re-addresses-a-credential",
        ),
        pytest.param(
            AGENT,
            {"dest_personal": True},
            Effect.DENY,
            "agent_cannot_move",
            False,
            connector.AGENT_REFUSED,
            id="an-agent-is-refused-even-toward-its-own-user",
        ),
        pytest.param(SERVICE_CI, {}, Effect.DENY, "user_required", True, NOT_FOUND, id="ci-token"),
        pytest.param(
            SERVICE_PROXY, {}, Effect.DENY, "user_required", True, NOT_FOUND, id="proxy-token"
        ),
        pytest.param(
            USER,
            {"source_visible": False},
            Effect.DENY,
            "not_visible",
            True,
            NOT_FOUND,
            id="a-row-the-caller-cannot-see-is-not-there",
        ),
        pytest.param(
            USER,
            # Visible is not managed: the member watching a team connection is
            # told plainly that it is not theirs to change, because it is on
            # their screen either way.
            {"source_managed": False},
            Effect.DENY,
            "not_source_manager",
            False,
            connector.NOT_MANAGER,
            id="seeing-is-not-managing",
        ),
        pytest.param(
            USER,
            {"dest_admin": False},
            Effect.DENY,
            "not_dest_admin",
            False,
            connector.NOT_DEST_ADMIN,
            id="the-destination-is-asked-too",
        ),
        pytest.param(
            USER,
            {"source_visible": False, "source_managed": False},
            Effect.DENY,
            "not_visible",
            True,
            NOT_FOUND,
            id="visibility-is-checked-before-management",
        ),
        pytest.param(
            USER,
            {"source_managed": False, "dest_admin": False},
            Effect.DENY,
            "not_source_manager",
            False,
            connector.NOT_MANAGER,
            id="the-source-is-checked-before-the-destination",
        ),
        pytest.param(
            USER,
            {"source_managed": False, "dest_personal": True},
            Effect.DENY,
            "not_source_manager",
            False,
            connector.NOT_MANAGER,
            id="an-own-destination-does-not-excuse-the-source",
        ),
        pytest.param(
            SERVICE_CI,
            {"source_visible": False},
            Effect.DENY,
            "user_required",
            True,
            NOT_FOUND,
            id="subject-kind-before-any-attribute",
        ),
    ],
)
def test_connector_move_table(
    ctx: ActingContext,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
) -> None:
    decision = authorize(ctx, Action.MOVE, CONNECTOR, {**MOVE_ALLOW, **overrides})
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="connector.credential",
        message=message,
        as_not_found=as_not_found,
    )


@pytest.mark.parametrize("key", sorted(MOVE_WRONG_TYPED), ids=str)
def test_a_move_attribute_that_is_missing_denies(key: str) -> None:
    decision = authorize(USER, Action.MOVE, CONNECTOR, _without(MOVE_ALLOW, key))
    _expect(
        decision,
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="connector.credential",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(MOVE_WRONG_TYPED), ids=str)
def test_a_move_attribute_of_the_wrong_type_denies(key: str) -> None:
    """A flag that arrives as the string "false" must not read as truthy."""
    decision = authorize(USER, Action.MOVE, CONNECTOR, {**MOVE_ALLOW, key: MOVE_WRONG_TYPED[key]})
    _expect(
        decision,
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="connector.credential",
        message="Not allowed",
    )


def test_a_move_never_reads_the_credential_facts() -> None:
    """The two actions share a policy and nothing else: a move decided on the
    credential's attributes would be a move nobody could refuse."""
    assert authorize(USER, Action.MOVE, CONNECTOR, CONNECTOR_ALLOW).allowed is False


def test_a_credential_fetch_never_reads_the_move_facts() -> None:
    assert authorize(USER, Action.FETCH_CREDENTIAL, CONNECTOR, MOVE_ALLOW).allowed is False


def test_connector_service_subject_is_refused_before_attributes_are_read() -> None:
    """A userless credential never learns which attribute was missing."""
    assert authorize(SERVICE_CI, Action.FETCH_CREDENTIAL, CONNECTOR, {}).reason == "user_required"


def test_the_publisher_machines_fact_is_required_on_the_write_branch_alone() -> None:
    """Only the machine's report reads ``publisher_machine_ids``: every other
    chat branch neither needs it nor is denied for its absence, and on WRITE
    a missing or mistyped set is a deny naming the key."""
    for action in (Action.READ, Action.SEND, Action.PROMOTE, Action.CREATE):
        assert authorize(USER, action, CHAT, CHAT_ALLOW).allowed is True, action
    missing = authorize(MACHINE_AGENT, Action.WRITE, CHAT, CHAT_ALLOW)
    assert (missing.allowed, missing.reason) == (False, "missing_attribute:publisher_machine_ids")
    for wrong in ([MACHINE_ID], MACHINE_ID, frozenset({UUID(MACHINE_ID)}), None):
        decision = authorize(
            MACHINE_AGENT, Action.WRITE, CHAT, {**CHAT_ALLOW, "publisher_machine_ids": wrong}
        )
        assert decision.reason == "missing_attribute:publisher_machine_ids", repr(wrong)


# ---------------------------------------------------------------------------
# Audited attributes through the registered allowlists
# ---------------------------------------------------------------------------


def test_audited_attrs_coerce_uuid_and_cut_strings_through_the_connector_allowlist() -> None:
    policy = policy_for(ResourceType.CONNECTOR)
    assert policy is not None
    attrs: dict[str, object] = {
        **CONNECTOR_ALLOW,
        **MOVE_ALLOW,
        "dest_team_id": str(TEAM),
        "team_id": TEAM,
        "auth_mode": "s" * 300,
        "for_user_id": str(OTHER_USER_ID),
        "chat_bound_machine_id": MACHINE_ID,
        "chat_id": "sess-1",
        "workspace_id": "",
    }
    out = audited_attrs(attrs, policy.audited_attrs)
    assert out["team_id"] == str(TEAM)
    assert out["auth_mode"] == "s" * 128
    assert set(out) == policy.audited_attrs


def test_no_registered_policy_can_ever_audit_a_secret() -> None:
    for policy in registered_policies():
        assert not policy.audited_attrs & NEVER_AUDITED_KEYS
        attrs: dict[str, object] = {"secret": "x", **dict.fromkeys(policy.audited_attrs, True)}
        assert "secret" not in audited_attrs(attrs, policy.audited_attrs)


# ---------------------------------------------------------------------------
# chat.access — the branch table
# ---------------------------------------------------------------------------

TEAM_CHAT: dict[str, object] = {
    **CHAT_ALLOW,
    "visibility_scope": f"team:{TEAM}",
    "team_id": str(TEAM),
}
PRIVATE_CHAT: dict[str, object] = {**CHAT_ALLOW, "visibility_scope": "private"}


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "reason"),
    [
        pytest.param(USER, Action.READ, CHAT_ALLOW, "owner_reads", id="read-as-owner"),
        pytest.param(USER, Action.READ, TEAM_CHAT, "owner_reads", id="read-own-team-chat-as-owner"),
        pytest.param(USER, Action.READ, PRIVATE_CHAT, "owner_reads", id="read-private-as-owner"),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "email_verified": False},
            "owner_reads",
            id="read-needs-no-verified-email",
        ),
        pytest.param(
            OWNER_AGENT, Action.READ, CHAT_ALLOW, "owner_reads", id="read-as-the-owners-agent"
        ),
        pytest.param(
            OWNER_PAT, Action.READ, CHAT_ALLOW, "owner_reads", id="read-through-the-owners-pat"
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "shared_role": "reader", "bound_machine_id": MACHINE_ID},
            "owner_reads",
            id="read-as-owner-costs-no-share-or-machine-fact",
        ),
        *[
            pytest.param(
                OTHER_USER,
                Action.READ,
                {**CHAT_ALLOW, "shared_role": rung},
                "shared_reads",
                id=f"read-as-a-shared-{rung}",
            )
            for rung in ("reader", "commenter", "writer", "manager", "owner")
        ],
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**PRIVATE_CHAT, "shared_role": "reader"},
            "shared_reads",
            id="read-a-private-chat-shared-at-can-view",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**TEAM_CHAT, "team_ids": frozenset({OTHER_TEAM}), "shared_role": "reader"},
            "shared_reads",
            id="read-a-team-chat-of-another-team-through-a-share",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**CHAT_ALLOW, "shared_role": "reader", "email_verified": False},
            "shared_reads",
            id="read-through-a-share-needs-no-verified-email",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.READ,
            {
                **CHAT_ALLOW,
                "owner_user_id": str(OTHER_USER_ID),
                "bound_machine_id": MACHINE_ID,
                "asserted_machine_verified": True,
            },
            "bound_machine_reads",
            id="read-as-the-bound-machine",
        ),
        pytest.param(
            OTHER_MEMBERS_MACHINE_AGENT,
            Action.READ,
            {**CHAT_ALLOW, "bound_machine_id": MACHINE_ID, "asserted_machine_verified": True},
            "bound_machine_reads",
            id="read-as-the-bound-machine-whoever-signed-it-in",
        ),
        pytest.param(USER, Action.CLAIM, CHAT_ALLOW, "owner_claims", id="claim-as-owner"),
        pytest.param(
            USER, Action.CLAIM, PRIVATE_CHAT, "owner_claims", id="claim-a-private-spare-as-owner"
        ),
        pytest.param(
            OWNER_AGENT, Action.CLAIM, CHAT_ALLOW, "owner_claims", id="claim-as-the-owners-agent"
        ),
        pytest.param(USER, Action.CREATE, CHAT_ALLOW, "org_member_creates", id="create-as-member"),
        pytest.param(
            USER, Action.CREATE, TEAM_CHAT, "org_member_creates", id="create-into-own-team"
        ),
        pytest.param(
            USER,
            Action.CREATE,
            PRIVATE_CHAT,
            "org_member_creates",
            id="create-a-private-chat-as-its-owner",
        ),
        pytest.param(
            OTHER_USER,
            Action.CREATE,
            {**CHAT_ALLOW, "owner_user_id": ""},
            "org_member_creates",
            id="create-before-an-owner-exists",
        ),
        pytest.param(USER, Action.SEND, CHAT_ALLOW, "writer_may_send", id="send-as-owner"),
        pytest.param(
            USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "reader"},
            "writer_may_send",
            id="send-as-owner-outranks-a-weak-grant-on-their-own-chat",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "writer"},
            "writer_may_send",
            id="send-as-a-shared-writer",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "manager"},
            "writer_may_send",
            id="send-as-a-shared-manager",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "owner"},
            "writer_may_send",
            id="send-as-a-shared-full-access",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.SEND,
            CHAT_ALLOW,
            "writer_may_send",
            id="send-as-the-owners-agent",
        ),
        pytest.param(
            USER,
            Action.SEND,
            {**CHAT_IN_WORKSPACE, "workspace_role": "owner"},
            "workspace_writer_may_send",
            id="send-in-a-workspace-as-its-owner",
        ),
        pytest.param(
            USER,
            Action.SEND,
            {**CHAT_IN_WORKSPACE, "workspace_role": "writer"},
            "workspace_writer_may_send",
            id="send-in-own-chat-as-a-workspace-writer",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_IN_WORKSPACE, "shared_role": "writer", "workspace_role": "writer"},
            "workspace_writer_may_send",
            id="send-in-a-colleagues-chat-as-a-workspace-writer",
        ),
        pytest.param(USER, Action.PROMOTE, CHAT_ALLOW, "owner_promotes", id="promote-as-owner"),
        pytest.param(
            OTHER_USER,
            Action.PROMOTE,
            {**CHAT_ALLOW, "is_org_admin": True, "shared_role": "reader"},
            "owner_promotes",
            id="promote-as-an-org-admin-who-may-read-the-chat",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.PROMOTE,
            CHAT_ALLOW,
            "owner_promotes",
            id="promote-as-the-owners-agent",
        ),
        pytest.param(
            OWNER_PAT,
            Action.PROMOTE,
            CHAT_ALLOW,
            "owner_promotes",
            id="promote-through-the-owners-pat",
        ),
        pytest.param(USER, Action.DELETE, CHAT_ALLOW, "owner_deletes", id="delete-as-owner"),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {**CHAT_ALLOW, "is_org_admin": True, "admin_reads_private": True},
            "org_admin_deletes",
            id="delete-as-an-org-admin-who-may-read-it",
        ),
        pytest.param(
            OWNER_AGENT, Action.DELETE, CHAT_ALLOW, "owner_deletes", id="delete-as-the-owners-agent"
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({MACHINE_ID}),
                "asserted_machine_verified": True,
            },
            "bound_machine_reports",
            id="publisher-state-by-the-bound-machine",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({OTHER_MACHINE_ID, MACHINE_ID}),
                "asserted_machine_verified": True,
            },
            "bound_machine_reports",
            id="publisher-state-by-the-orgs-current-machine",
        ),
        pytest.param(
            OTHER_MEMBERS_MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({MACHINE_ID}),
                "asserted_machine_verified": True,
            },
            "bound_machine_reports",
            id="publisher-state-by-the-machine-whoever-signed-it-in",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({MACHINE_ID}),
                "email_verified": False,
                "asserted_machine_verified": True,
            },
            "bound_machine_reports",
            id="publisher-state-needs-no-verified-email-no-human-acts",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **TEAM_CHAT,
                "team_ids": frozenset(),
                "publisher_machine_ids": frozenset({MACHINE_ID}),
                "asserted_machine_verified": True,
            },
            "bound_machine_reports",
            id="publisher-state-by-a-machine-whose-user-holds-no-rung-on-the-chat",
        ),
    ],
)
def test_chat_allow_branches(
    ctx: ActingContext, action: Action, attrs: dict[str, object], reason: str
) -> None:
    decision = authorize(ctx, action, CHAT, attrs)
    assert decision.allowed is True, decision
    assert (decision.policy, decision.reason) == (chat.POLICY, reason)


# -- RENAME: the ladder's write rungs, and nobody else ----------------------


@pytest.mark.parametrize("rung", ["writer", "manager", "owner"])
def test_a_rung_that_writes_the_node_renames_the_chat(rung: str) -> None:
    """A chat's title is the name of its folder, so the rungs that name a node
    name the chat: "Can edit", "Full access" and the top rung."""
    decision = authorize(OTHER_USER, Action.RENAME, CHAT, {**PRIVATE_CHAT, "shared_role": rung})
    assert (decision.allowed, decision.policy, decision.reason) == (
        True,
        chat.POLICY,
        "writer_may_rename",
    )


def test_the_owner_renames_their_own_chat_with_no_grant() -> None:
    decision = authorize(USER, Action.RENAME, CHAT, PRIVATE_CHAT)
    assert (decision.allowed, decision.reason) == (True, "writer_may_rename")


@pytest.mark.parametrize("rung", ["reader", "commenter"])
def test_a_watching_rung_may_not_rename_and_is_told_why(rung: str) -> None:
    """They can already see the chat, so the refusal need not hide it — it
    names the rung they are short of, like the send refusal beside it."""
    decision = authorize(OTHER_USER, Action.RENAME, CHAT, {**PRIVATE_CHAT, "shared_role": rung})
    assert decision.allowed is False
    assert (decision.reason, decision.error_code) == (
        "rename_rung_required",
        chat.RENAME_DENIED_CODE,
    )
    assert decision.as_not_found is False


def test_an_org_admin_with_no_rung_renames_nothing_and_is_not_told_it_exists() -> None:
    """The inversion this branch exists to end: the rename used to be decided
    by a rule that asked about org-admin and not about the share, so the person
    holding "Full access" was refused and the person holding nothing was not."""
    decision = authorize(OTHER_USER, Action.RENAME, CHAT, {**PRIVATE_CHAT, "is_org_admin": True})
    assert decision.allowed is False
    assert (decision.reason, decision.as_not_found) == ("not_in_audience", True)
    assert decision.message == chat.NOT_FOUND


def test_a_rename_by_a_shared_writer_still_needs_a_verified_email() -> None:
    decision = authorize(
        OTHER_USER,
        Action.RENAME,
        CHAT,
        {**PRIVATE_CHAT, "shared_role": "writer", "email_verified": False},
    )
    assert (decision.allowed, decision.error_code) == (False, chat.VERIFY_EMAIL_CODE)


def test_an_unshared_rung_the_ladder_never_heard_of_renames_nothing() -> None:
    decision = authorize(OTHER_USER, Action.RENAME, CHAT, {**PRIVATE_CHAT, "shared_role": "editor"})
    assert (decision.allowed, decision.reason) == (False, "not_in_audience")


# -- the one flag: may an org admin open a chat nobody shared with them ------


def test_the_admin_read_flag_is_required_on_every_branch() -> None:
    """Required like every other fact, on branches that consult it and on
    branches that do not: a door that forgets to resolve it is denied rather
    than admitted by the branch that happened not to need it."""
    for action in (Action.READ, Action.SEND, Action.RENAME, Action.DELETE, Action.CREATE):
        decision = authorize(USER, action, CHAT, _without(CHAT_ALLOW, "admin_reads_private"))
        assert decision.reason == "missing_attribute:admin_reads_private", action
    for wrong in ("true", 1, None):
        decision = authorize(USER, Action.READ, CHAT, {**CHAT_ALLOW, "admin_reads_private": wrong})
        assert decision.reason == "missing_attribute:admin_reads_private", repr(wrong)


def test_the_flag_off_keeps_a_private_chat_opaque_to_its_orgs_admin() -> None:
    decision = authorize(OTHER_USER, Action.READ, CHAT, {**PRIVATE_CHAT, "is_org_admin": True})
    assert (decision.allowed, decision.as_not_found) == (False, True)


def test_the_flag_on_admits_an_org_admin_to_the_read_and_says_so() -> None:
    decision = authorize(
        OTHER_USER,
        Action.READ,
        CHAT,
        {**PRIVATE_CHAT, "is_org_admin": True, "admin_reads_private": True},
    )
    assert (decision.allowed, decision.reason) == (True, "org_admin_reads_private")


def test_the_flag_on_still_does_not_let_an_admin_rename_or_speak() -> None:
    """The flag is a READ branch and only a READ branch. An admin it admits can
    open the conversation and can neither retitle it nor drive the agent."""
    attrs = {**PRIVATE_CHAT, "is_org_admin": True, "admin_reads_private": True}
    rename = authorize(OTHER_USER, Action.RENAME, CHAT, attrs)
    assert (rename.allowed, rename.reason) == (False, "rename_rung_required")
    send = authorize(OTHER_USER, Action.SEND, CHAT, attrs)
    assert (send.allowed, send.reason) == (False, "send_rung_required")


def test_the_flag_on_admits_nobody_who_is_not_an_org_admin() -> None:
    decision = authorize(
        OTHER_USER, Action.READ, CHAT, {**PRIVATE_CHAT, "admin_reads_private": True}
    )
    assert (decision.allowed, decision.reason) == (False, "not_in_audience")


# -- promoting is content, deleting is governance ---------------------------


def test_an_org_admin_who_may_not_read_the_chat_may_not_promote_out_of_it() -> None:
    """Promoting lifts a turn's result out of the conversation into a durable
    object of the org — and that object is decided by the OBJECT policy, which
    has no idea it came out of a private chat. So the gate has to be here: an
    admin nobody shared the chat with is told it does not exist, exactly as a
    read is, rather than being handed its contents through the sibling policy.
    """
    decision = authorize(OTHER_USER, Action.PROMOTE, CHAT, {**PRIVATE_CHAT, "is_org_admin": True})
    assert decision.allowed is False
    assert (decision.reason, decision.as_not_found) == ("not_in_audience", True)
    assert decision.message == chat.NOT_FOUND


@pytest.mark.parametrize(
    ("ctx", "attrs", "allowed", "reason"),
    [
        pytest.param(
            OTHER_USER,
            {"is_org_admin": True, "owner_departed": True},
            True,
            "org_admin_offboards",
            id="an-org-admin-deletes-a-departed-members-chat-unread",
        ),
        pytest.param(
            OTHER_USER,
            {"owner_departed": True},
            False,
            "not_in_audience",
            id="a-member-gains-nothing-from-a-departed-owner",
        ),
        pytest.param(
            OTHER_USER,
            {"is_org_admin": True, "owner_departed": True, "email_verified": False},
            False,
            "email_verification_required",
            id="offboarding-still-takes-a-verified-email",
        ),
    ],
)
def test_offboarding_deletes_a_departed_members_private_chat(
    ctx: ActingContext, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    decision = authorize(ctx, Action.DELETE, CHAT, {**PRIVATE_CHAT, **attrs})
    assert (decision.allowed, decision.reason) == (allowed, reason)
    read = authorize(ctx, Action.READ, CHAT, {**PRIVATE_CHAT, **attrs})
    assert read.allowed is False, "offboarding never reads the chat"


def test_nor_may_the_same_org_admin_delete_it() -> None:
    """A chat an admin cannot read is the same not-found on DELETE as on every
    other door: deleting is never a way to learn that a private chat exists,
    or to end it unseen."""
    decision = authorize(OTHER_USER, Action.DELETE, CHAT, {**PRIVATE_CHAT, "is_org_admin": True})
    assert (decision.allowed, decision.reason, decision.as_not_found) == (
        False,
        "not_in_audience",
        True,
    )


def test_an_org_admin_the_flag_admits_may_promote_again() -> None:
    """The gate is the READ, not the share: wherever the deployment says an
    admin may read a private chat, promoting out of it follows."""
    decision = authorize(
        OTHER_USER,
        Action.PROMOTE,
        CHAT,
        {**PRIVATE_CHAT, "is_org_admin": True, "admin_reads_private": True},
    )
    assert (decision.allowed, decision.reason) == (True, "owner_promotes")


def test_the_owner_promotes_without_any_of_that() -> None:
    decision = authorize(USER, Action.PROMOTE, CHAT, PRIVATE_CHAT)
    assert (decision.allowed, decision.reason) == (True, "owner_promotes")


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "reason", "message", "not_found", "code"),
    [
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "in_org": False},
            "not_in_org",
            chat.NOT_FOUND,
            True,
            None,
            id="chat-outside-the-callers-org",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "roles": frozenset()},
            "org_member_required",
            chat.NOT_FOUND,
            True,
            None,
            id="no-roles-at-all",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "roles": frozenset({Role.VIEWER})},
            "org_member_required",
            chat.NOT_FOUND,
            True,
            None,
            id="viewer-is-not-yet-a-member",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "roles": frozenset({Role.AGENT})},
            "org_member_required",
            chat.NOT_FOUND,
            True,
            None,
            id="the-agent-marker-grants-no-human-rung",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "visibility_scope": f"team:{TEAM}"},
            "scope_team_mismatch",
            chat.NOT_FOUND,
            True,
            None,
            id="team-scope-with-no-team-id",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "team_id": str(TEAM)},
            "scope_team_mismatch",
            chat.NOT_FOUND,
            True,
            None,
            id="org-scope-with-a-team-id",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**TEAM_CHAT, "team_id": str(OTHER_TEAM)},
            "scope_team_mismatch",
            chat.NOT_FOUND,
            True,
            None,
            id="team-scope-naming-another-team",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "visibility_scope": "world"},
            "scope_team_mismatch",
            chat.NOT_FOUND,
            True,
            None,
            id="a-scope-grammar-we-do-not-know",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            CHAT_ALLOW,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="unshared-chat-of-another-member",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**CHAT_ALLOW, "is_org_admin": True},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="unshared-chat-as-org-admin",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**TEAM_CHAT, "team_ids": frozenset({TEAM, OTHER_TEAM})},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="unshared-team-chat-as-a-member-of-that-team",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**TEAM_CHAT, "team_ids": frozenset({OTHER_TEAM})},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="team-scoped-chat-of-another-team",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            PRIVATE_CHAT,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="private-chat-of-another-member",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**CHAT_ALLOW, "shared_role": "editor"},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="read-on-a-rung-the-ladder-has-never-heard-of",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.READ,
            CHAT_ALLOW,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="unshared-chat-through-another-members-agent",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.READ,
            {
                **CHAT_ALLOW,
                "owner_user_id": str(OTHER_USER_ID),
                "bound_machine_id": OTHER_MACHINE_ID,
                "asserted_machine_verified": True,
            },
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="a-machine-the-chat-is-not-bound-to",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.READ,
            {**CHAT_ALLOW, "owner_user_id": str(OTHER_USER_ID)},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="a-machine-when-the-chat-is-bound-to-none",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**CHAT_ALLOW, "owner_user_id": str(OTHER_USER_ID), "bound_machine_id": MACHINE_ID},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="the-bound-machines-user-as-a-person-holds-no-rung",
        ),
        pytest.param(
            SERVICE_CI,
            Action.READ,
            PRIVATE_CHAT,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="private-chat-and-no-human-behind-the-call",
        ),
        pytest.param(
            OTHER_USER,
            Action.CLAIM,
            {**CHAT_ALLOW, "shared_role": "owner"},
            "spare_owner_required",
            chat.NOT_FOUND,
            True,
            None,
            id="claim-on-the-top-rung-is-still-not-the-owner",
        ),
        pytest.param(
            OTHER_USER,
            Action.CLAIM,
            {**CHAT_ALLOW, "is_org_admin": True, "shared_role": "owner"},
            "spare_owner_required",
            chat.NOT_FOUND,
            True,
            None,
            id="claim-by-an-org-admin-is-not-found",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.CLAIM,
            {
                **CHAT_ALLOW,
                "owner_user_id": str(OTHER_USER_ID),
                "bound_machine_id": MACHINE_ID,
                "asserted_machine_verified": True,
            },
            "spare_owner_required",
            chat.NOT_FOUND,
            True,
            None,
            id="claim-by-the-bound-machine-is-not-found",
        ),
        pytest.param(
            USER,
            Action.CLAIM,
            {**CHAT_ALLOW, "email_verified": False},
            "email_verification_required",
            chat.VERIFY_EMAIL_MESSAGE,
            False,
            chat.VERIFY_EMAIL_CODE,
            id="claim-needs-a-verified-email",
        ),
        pytest.param(
            OTHER_USER,
            Action.PROMOTE,
            {**CHAT_ALLOW, "shared_role": "reader"},
            "owner_or_org_admin_required",
            chat.PROMOTE_DENIED_MESSAGE,
            False,
            None,
            id="promote-by-a-shared-reader",
        ),
        pytest.param(
            OTHER_USER,
            Action.PROMOTE,
            {**CHAT_ALLOW, "shared_role": "owner"},
            "owner_or_org_admin_required",
            chat.PROMOTE_DENIED_MESSAGE,
            False,
            None,
            id="promote-on-the-top-rung-is-still-not-the-owner",
        ),
        pytest.param(
            OTHER_USER,
            Action.PROMOTE,
            CHAT_ALLOW,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="promote-by-a-member-the-chat-is-not-shared-with",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.PROMOTE,
            {**CHAT_ALLOW, "shared_role": "reader"},
            "owner_or_org_admin_required",
            chat.PROMOTE_DENIED_MESSAGE,
            False,
            None,
            id="promote-by-another-members-agent",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {**CHAT_ALLOW, "shared_role": "writer"},
            "owner_or_org_admin_required",
            chat.DELETE_DENIED_MESSAGE,
            False,
            None,
            id="delete-by-a-shared-writer",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            CHAT_ALLOW,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="delete-by-a-member-the-chat-is-not-shared-with",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.DELETE,
            {**CHAT_ALLOW, "shared_role": "reader"},
            "owner_or_org_admin_required",
            chat.DELETE_DENIED_MESSAGE,
            False,
            None,
            id="delete-by-another-members-agent",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {**TEAM_CHAT, "team_ids": frozenset()},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="create-into-a-team-the-creator-is-not-on",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {**TEAM_CHAT, "team_ids": frozenset(), "email_verified": False},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="create-outside-the-audience-is-not-found-before-verification",
        ),
        pytest.param(
            OTHER_USER,
            Action.CREATE,
            PRIVATE_CHAT,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="create-a-private-chat-for-somebody-else",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {**CHAT_ALLOW, "email_verified": False},
            "email_verification_required",
            chat.VERIFY_EMAIL_MESSAGE,
            False,
            chat.VERIFY_EMAIL_CODE,
            id="create-with-an-unverified-email",
        ),
        pytest.param(
            USER,
            Action.SEND,
            {**CHAT_ALLOW, "email_verified": False},
            "email_verification_required",
            chat.VERIFY_EMAIL_MESSAGE,
            False,
            chat.VERIFY_EMAIL_CODE,
            id="send-with-an-unverified-email",
        ),
        pytest.param(
            USER,
            Action.PROMOTE,
            {**CHAT_ALLOW, "email_verified": False},
            "email_verification_required",
            chat.VERIFY_EMAIL_MESSAGE,
            False,
            chat.VERIFY_EMAIL_CODE,
            id="promote-with-an-unverified-email",
        ),
        pytest.param(
            USER,
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset({MACHINE_ID})},
            "publisher_machine_required",
            "Not allowed",
            False,
            None,
            id="publisher-state-by-the-owner-as-a-person",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset({MACHINE_ID})},
            "publisher_machine_required",
            "Not allowed",
            False,
            None,
            id="publisher-state-by-an-agent-of-another-id",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset(), "asserted_machine_verified": True},
            "publisher_machine_required",
            "Not allowed",
            False,
            None,
            id="publisher-state-for-a-chat-with-no-machine",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({OTHER_MACHINE_ID}),
                "asserted_machine_verified": True,
            },
            "publisher_machine_required",
            "Not allowed",
            False,
            None,
            id="publisher-state-by-a-machine-that-is-neither-bound-nor-current",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {
                **CHAT_ALLOW,
                "publisher_machine_ids": frozenset({MACHINE_ID}),
                "in_org": False,
                "asserted_machine_verified": True,
            },
            "not_in_org",
            chat.NOT_FOUND,
            True,
            None,
            id="publisher-state-outside-the-org",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            CHAT_ALLOW,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="send-by-a-member-the-chat-is-not-shared-with",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.SEND,
            {
                **CHAT_ALLOW,
                "owner_user_id": str(OTHER_USER_ID),
                "bound_machine_id": MACHINE_ID,
                "asserted_machine_verified": True,
            },
            "send_rung_required",
            chat.SEND_DENIED_MESSAGE,
            False,
            chat.SEND_DENIED_CODE,
            id="send-as-the-bound-machine-reads-but-holds-no-rung",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "reader"},
            "send_rung_required",
            chat.SEND_DENIED_MESSAGE,
            False,
            chat.SEND_DENIED_CODE,
            id="send-as-a-shared-viewer",
        ),
        pytest.param(
            USER,
            Action.SEND,
            CHAT_IN_WORKSPACE,
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="send-in-a-chat-you-started-after-the-workspace-share-was-revoked",
        ),
        pytest.param(
            USER,
            Action.SEND,
            {**CHAT_IN_WORKSPACE, "workspace_role": "reader"},
            "workspace_send_rung_required",
            chat.WORKSPACE_SEND_DENIED_MESSAGE,
            False,
            chat.WORKSPACE_SEND_DENIED_CODE,
            id="send-in-own-chat-as-a-workspace-viewer",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_IN_WORKSPACE, "shared_role": "writer"},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="send-through-a-share-on-the-chat-alone",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "commenter"},
            "send_rung_required",
            chat.SEND_DENIED_MESSAGE,
            False,
            chat.SEND_DENIED_CODE,
            id="send-as-a-shared-commenter",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "editor"},
            "not_in_audience",
            chat.NOT_FOUND,
            True,
            None,
            id="send-on-a-rung-the-ladder-has-never-heard-of",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "writer", "is_org_admin": True, "in_org": False},
            "not_in_org",
            chat.NOT_FOUND,
            True,
            None,
            id="send-as-a-writer-who-is-not-in-the-org",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "writer", "roles": frozenset()},
            "org_member_required",
            chat.NOT_FOUND,
            True,
            None,
            id="send-as-a-writer-who-is-not-a-member",
        ),
        pytest.param(
            OTHER_USER,
            Action.SEND,
            {**CHAT_ALLOW, "shared_role": "writer", "email_verified": False},
            "email_verification_required",
            chat.VERIFY_EMAIL_MESSAGE,
            False,
            chat.VERIFY_EMAIL_CODE,
            id="send-as-a-writer-who-has-not-verified-their-email",
        ),
    ],
)
def test_chat_deny_branches(
    ctx: ActingContext,
    action: Action,
    attrs: dict[str, object],
    reason: str,
    message: str,
    not_found: bool,
    code: str | None,
) -> None:
    _expect(
        authorize(ctx, action, CHAT, attrs),
        effect=Effect.DENY,
        reason=reason,
        policy=chat.POLICY,
        message=message,
        as_not_found=not_found,
        error_code=code,
    )


@pytest.mark.parametrize(
    "action",
    [Action.READ, Action.CREATE, Action.SEND, Action.PROMOTE, Action.DELETE],
    ids=["read", "create", "send", "promote", "delete"],
)
@pytest.mark.parametrize(
    "wrong",
    [None, 1, True, ["writer"], frozenset({"writer"}), b"writer"],
    ids=["absent", "int", "bool", "list", "set", "bytes"],
)
@pytest.mark.parametrize("key", ["shared_role", "bound_machine_id"], ids=["rung", "machine"])
def test_the_share_facts_are_required_on_every_chat_branch(
    action: Action, wrong: object, key: str
) -> None:
    """The rung and the bound machine — the facts the read gate turns on for
    everyone but the owner — are required on every branch, the way every
    other chat fact is, even for the owner whose read never consults them: a
    door that forgets to resolve one, or resolves it to something that is not
    a string, is denied naming the key rather than admitted by a branch that
    happened not to read it."""
    attrs = _without(CHAT_ALLOW, key) if wrong is None else {**CHAT_ALLOW, key: wrong}
    decision = authorize(USER, action, CHAT, attrs)
    assert (decision.allowed, decision.reason) == (False, f"missing_attribute:{key}"), repr(wrong)


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "reason", "as_not_found"),
    [
        pytest.param(
            MACHINE_AGENT,
            Action.READ,
            {**CHAT_ALLOW, "owner_user_id": str(OTHER_USER_ID), "bound_machine_id": MACHINE_ID},
            "not_in_audience",
            True,
            id="read-naming-the-bound-machine-on-an-unverified-session",
        ),
        pytest.param(
            OTHER_MEMBERS_MACHINE_AGENT,
            Action.READ,
            {**CHAT_ALLOW, "bound_machine_id": MACHINE_ID},
            "not_in_audience",
            True,
            id="read-by-a-colleague-forging-the-bound-machines-id",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.SEND,
            {**CHAT_ALLOW, "owner_user_id": str(OTHER_USER_ID), "bound_machine_id": MACHINE_ID},
            "not_in_audience",
            True,
            id="send-naming-the-bound-machine-on-an-unverified-session",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset({MACHINE_ID})},
            "publisher_machine_required",
            False,
            id="publisher-state-naming-the-bound-machine-on-an-unverified-session",
        ),
        pytest.param(
            MACHINE_AGENT,
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset({OTHER_MACHINE_ID, MACHINE_ID})},
            "publisher_machine_required",
            False,
            id="publisher-state-naming-the-current-machine-on-an-unverified-session",
        ),
    ],
)
def test_a_machine_assertion_the_route_did_not_verify_admits_nobody(
    ctx: ActingContext, action: Action, attrs: dict[str, object], reason: str, as_not_found: bool
) -> None:
    """A machine's id is on every chat it serves, and the agent assertion is a
    header any member can put on their own session. So the asserted id alone
    never makes a caller the machine: with ``asserted_machine_verified``
    False — the route could not prove the delegating user registered a live
    machine of this org under that id — the bound-machine read, the send it
    would have reached, and the machine's publisher report are all refused
    exactly as a stranger's are. The identical facts with the assertion
    verified are the allow rows of the branch table above."""
    decision = authorize(ctx, action, CHAT, attrs)
    assert (decision.allowed, decision.reason, decision.as_not_found) == (
        False,
        reason,
        as_not_found,
    )
    verified = authorize(ctx, action, CHAT, {**attrs, "asserted_machine_verified": True})
    if action is Action.SEND:
        # Verified, the machine reads the chat — and is refused at the send
        # gate as a reader who holds no rung, no longer as a stranger.
        assert (verified.allowed, verified.reason) == (False, "send_rung_required")
    else:
        assert verified.allowed is True, verified.reason


@pytest.mark.parametrize(
    ("action", "attrs"),
    [
        pytest.param(Action.READ, CHAT_ALLOW, id="read"),
        pytest.param(Action.CREATE, CHAT_ALLOW, id="create"),
        pytest.param(Action.SEND, CHAT_ALLOW, id="send"),
        pytest.param(Action.PROMOTE, CHAT_ALLOW, id="promote"),
        pytest.param(Action.DELETE, CHAT_ALLOW, id="delete"),
        pytest.param(
            Action.WRITE,
            {**CHAT_ALLOW, "publisher_machine_ids": frozenset({MACHINE_ID})},
            id="write",
        ),
    ],
)
@pytest.mark.parametrize(
    "wrong",
    [None, 1, 0, "true", "false", "", ["true"], frozenset({True})],
    ids=["absent", "one", "zero", "str-true", "str-false", "empty-str", "list", "set"],
)
def test_the_machine_verification_fact_is_required_on_every_chat_branch(
    action: Action, attrs: dict[str, object], wrong: object
) -> None:
    """``asserted_machine_verified`` is required on every branch, even the
    owner's and the person's whose answer it cannot change, and only a real
    bool counts: a door that forgets to resolve it, or resolves it to ``1``
    or ``"false"``, is denied naming the key rather than admitted by a branch
    that happened not to read it — the same rule as every other chat fact."""
    given = (
        _without(attrs, "asserted_machine_verified")
        if wrong is None
        else {**attrs, "asserted_machine_verified": wrong}
    )
    for ctx in (USER, MACHINE_AGENT):
        decision = authorize(ctx, action, CHAT, given)
        assert (decision.allowed, decision.reason) == (
            False,
            "missing_attribute:asserted_machine_verified",
        ), (ctx.acting_principal.kind, repr(wrong))


#: A send that would otherwise be allowed: the owner in a workspace of one, a
#: writer granted Can edit on the chat, and a writer on a shared workspace.
PAYER_SENDS = [
    pytest.param(USER, CHAT_ALLOW, "writer_may_send", id="owner"),
    pytest.param(
        OTHER_USER, {**CHAT_ALLOW, "shared_role": "writer"}, "writer_may_send", id="chat-writer"
    ),
    pytest.param(
        OTHER_USER,
        {**CHAT_IN_WORKSPACE, "workspace_role": "writer"},
        "workspace_writer_may_send",
        id="workspace-writer",
    ),
]


@pytest.mark.parametrize(("ctx", "attrs", "reason"), PAYER_SENDS)
def test_a_send_is_refused_when_the_chats_payer_can_no_longer_read_it(
    ctx: ActingContext, attrs: dict[str, object], reason: str
) -> None:
    """A writer may not spend the money of a payer who can no longer open,
    stop or delete the chat. The refusal is a plain 403 naming what to do,
    and the same facts with the payer standing are the allow."""
    assert authorize(ctx, Action.SEND, CHAT, attrs).reason == reason
    refused = authorize(ctx, Action.SEND, CHAT, {**attrs, "payer_stands": False})
    assert (refused.allowed, refused.reason, refused.error_code, refused.as_not_found) == (
        False,
        "payer_lost_access",
        "payer_lost_access",
        False,
    )
    assert refused.message == chat.PAYER_LOST_ACCESS_MESSAGE


@pytest.mark.parametrize(
    ("attrs", "reason"),
    [
        pytest.param({**CHAT_ALLOW, "shared_role": "reader"}, "send_rung_required", id="chat"),
        pytest.param(
            {**CHAT_IN_WORKSPACE, "workspace_role": "reader"},
            "workspace_send_rung_required",
            id="workspace",
        ),
    ],
)
def test_a_sender_without_the_rung_is_told_that_before_the_payer(
    attrs: dict[str, object], reason: str
) -> None:
    decision = authorize(OTHER_USER, Action.SEND, CHAT, {**attrs, "payer_stands": False})
    assert (decision.allowed, decision.reason) == (False, reason)


@pytest.mark.parametrize(("ctx", "attrs", "reason"), PAYER_SENDS)
@pytest.mark.parametrize(
    "wrong",
    [None, 1, 0, "true", "", frozenset({True})],
    ids=["absent", "one", "zero", "str-true", "empty-str", "set"],
)
def test_the_payer_fact_is_required_on_a_send(
    ctx: ActingContext, attrs: dict[str, object], reason: str, wrong: object
) -> None:
    """A send door that forgets to resolve the payer, or resolves it to
    anything but a bool, is refused naming the key."""
    given = _without(attrs, "payer_stands") if wrong is None else {**attrs, "payer_stands": wrong}
    decision = authorize(ctx, Action.SEND, CHAT, given)
    assert (decision.allowed, decision.reason) == (False, "missing_attribute:payer_stands")


@pytest.mark.parametrize(
    "action", [Action.READ, Action.DELETE, Action.PROMOTE, Action.RENAME, Action.CLAIM]
)
def test_the_payer_fact_decides_nothing_but_a_send(action: Action) -> None:
    """Resolved for a send alone: a payer who lost access leaves every other
    verb as it was, and a door that never resolved it is not refused for it."""
    for attrs in (_without(CHAT_ALLOW, "payer_stands"), {**CHAT_ALLOW, "payer_stands": False}):
        assert authorize(USER, action, CHAT, attrs).allowed is True, action


def test_a_chat_denial_that_could_confirm_the_chat_exists_is_always_opaque() -> None:
    """Every refusal a non-reader can provoke is a 404, so the answer never
    tells them which gate they failed — the reason lives on the decision row."""
    foreign = Resource(ResourceType.CHAT, id="sess-1", org_id=OTHER_ORG)
    denials = [
        authorize(USER, Action.READ, CHAT, {**CHAT_ALLOW, "in_org": False}),
        authorize(USER, Action.READ, CHAT, {**CHAT_ALLOW, "roles": frozenset()}),
        authorize(OTHER_USER, Action.READ, CHAT, PRIVATE_CHAT),
        authorize(USER, Action.READ, foreign, CHAT_ALLOW),
    ]
    assert {(d.as_not_found, d.effect) for d in denials} == {(True, Effect.DENY)}


# ---------------------------------------------------------------------------
# objects.access — the branch table
# ---------------------------------------------------------------------------

TEAM_OBJECT: dict[str, object] = {**OBJECT_ALLOW, "visibility_scope": f"team:{TEAM}"}
PRIVATE_OBJECT: dict[str, object] = {**OBJECT_ALLOW, "visibility_scope": "private"}
OWNER_ACTIONS = [Action.CREATE, Action.WRITE, Action.EXPORT, Action.RERUN]


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "reason"),
    [
        pytest.param(USER, Action.READ, OBJECT_ALLOW, "in_audience", id="read-org-scope"),
        pytest.param(
            OTHER_USER, Action.READ, OBJECT_ALLOW, "in_audience", id="read-org-scope-non-owner"
        ),
        pytest.param(USER, Action.READ, TEAM_OBJECT, "in_audience", id="read-own-team"),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**TEAM_OBJECT, "team_ids": frozenset(), "is_org_admin": True},
            "in_audience",
            id="read-foreign-team-as-org-admin",
        ),
        pytest.param(USER, Action.READ, PRIVATE_OBJECT, "in_audience", id="read-private-as-owner"),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**PRIVATE_OBJECT, "is_org_admin": True},
            "in_audience",
            id="read-private-as-org-admin",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**OBJECT_ALLOW, "email_verified": False},
            "in_audience",
            id="read-needs-no-verified-email",
        ),
        pytest.param(USER, Action.WRITE, OBJECT_ALLOW, "owner_write", id="write-as-owner"),
        pytest.param(USER, Action.CREATE, OBJECT_ALLOW, "owner_create", id="create-as-owner"),
        pytest.param(USER, Action.CREATE, TEAM_OBJECT, "owner_create", id="create-into-own-team"),
        pytest.param(USER, Action.EXPORT, OBJECT_ALLOW, "owner_export", id="export-as-owner"),
        pytest.param(
            USER,
            Action.EXPORT,
            {**OBJECT_ALLOW, "email_verified": False},
            "owner_export",
            id="export-is-not-gated-on-a-verified-email",
        ),
        pytest.param(USER, Action.RERUN, OBJECT_ALLOW, "owner_rerun", id="rerun-as-owner"),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {**OBJECT_ALLOW, "is_org_admin": True},
            "org_admin_write",
            id="write-as-org-admin",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.UPLOAD_PAYLOAD,
            OBJECT_ALLOW,
            "owner_upload_payload",
            id="upload-as-the-owners-agent",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.UPLOAD_PAYLOAD,
            {**OBJECT_ALLOW, "is_org_admin": True},
            "org_admin_upload_payload",
            id="upload-as-an-org-admins-agent",
        ),
    ],
)
def test_object_allow_branches(
    ctx: ActingContext, action: Action, attrs: dict[str, object], reason: str
) -> None:
    decision = authorize(ctx, action, OBJECT, attrs)
    assert decision.allowed is True, decision
    assert (decision.policy, decision.reason) == (workspace_object.POLICY, reason)


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "reason", "message", "not_found", "code"),
    [
        pytest.param(
            USER,
            Action.READ,
            {**OBJECT_ALLOW, "in_org": False},
            "not_in_org",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="object-outside-the-callers-org",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**OBJECT_ALLOW, "roles": frozenset({Role.VIEWER})},
            "org_member_required",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="viewer-is-not-yet-a-member",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {**TEAM_OBJECT, "team_ids": frozenset({OTHER_TEAM})},
            "not_in_audience",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="team-scoped-object-of-another-team",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            PRIVATE_OBJECT,
            "not_in_audience",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="private-object-of-another-member",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**OBJECT_ALLOW, "visibility_scope": "team:not-a-uuid"},
            "not_in_audience",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="a-team-scope-that-does-not-parse",
        ),
        pytest.param(
            USER,
            Action.READ,
            {**OBJECT_ALLOW, "visibility_scope": "world"},
            "not_in_audience",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="a-scope-grammar-we-do-not-know",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {**TEAM_OBJECT, "team_ids": frozenset()},
            "not_in_audience",
            workspace_object.NOT_FOUND,
            True,
            None,
            id="create-into-a-team-the-creator-is-not-on",
        ),
        pytest.param(
            OTHER_USER,
            Action.CREATE,
            OBJECT_ALLOW,
            "owner_or_org_admin_required",
            "Not allowed",
            False,
            None,
            id="create-for-somebody-else",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {**OBJECT_ALLOW, "email_verified": False},
            "email_verification_required",
            workspace_object.VERIFY_EMAIL_MESSAGE,
            False,
            workspace_object.VERIFY_EMAIL_CODE,
            id="create-with-an-unverified-email",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            OBJECT_ALLOW,
            "owner_or_org_admin_required",
            "Not allowed",
            False,
            None,
            id="write-by-a-reader",
        ),
        pytest.param(
            OTHER_USER,
            Action.EXPORT,
            OBJECT_ALLOW,
            "owner_or_org_admin_required",
            "Not allowed",
            False,
            None,
            id="export-by-a-reader",
        ),
        pytest.param(
            OTHER_USER,
            Action.RERUN,
            OBJECT_ALLOW,
            "owner_or_org_admin_required",
            "Not allowed",
            False,
            None,
            id="rerun-by-a-reader",
        ),
        pytest.param(
            USER,
            Action.UPLOAD_PAYLOAD,
            OBJECT_ALLOW,
            "agent_principal_required",
            "Not allowed",
            False,
            None,
            id="upload-from-a-browser-session",
        ),
        pytest.param(
            OWNER_PAT,
            Action.UPLOAD_PAYLOAD,
            OBJECT_ALLOW,
            "agent_principal_required",
            "Not allowed",
            False,
            None,
            id="upload-through-the-owners-personal-access-token",
        ),
        pytest.param(
            SERVICE_PROXY,
            Action.UPLOAD_PAYLOAD,
            OBJECT_ALLOW,
            "agent_principal_required",
            "Not allowed",
            False,
            None,
            id="upload-from-a-service-token",
        ),
        pytest.param(
            OTHER_AGENT,
            Action.UPLOAD_PAYLOAD,
            OBJECT_ALLOW,
            "owner_or_org_admin_required",
            "Not allowed",
            False,
            None,
            id="upload-by-another-members-agent",
        ),
        pytest.param(
            USER,
            Action.WRITE,
            {**OBJECT_ALLOW, "email_verified": False},
            "email_verification_required",
            workspace_object.VERIFY_EMAIL_MESSAGE,
            False,
            workspace_object.VERIFY_EMAIL_CODE,
            id="write-with-an-unverified-email",
        ),
        pytest.param(
            USER,
            Action.RERUN,
            {**OBJECT_ALLOW, "email_verified": False},
            "email_verification_required",
            workspace_object.VERIFY_EMAIL_MESSAGE,
            False,
            workspace_object.VERIFY_EMAIL_CODE,
            id="rerun-with-an-unverified-email",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.UPLOAD_PAYLOAD,
            {**OBJECT_ALLOW, "email_verified": False},
            "email_verification_required",
            workspace_object.VERIFY_EMAIL_MESSAGE,
            False,
            workspace_object.VERIFY_EMAIL_CODE,
            id="upload-for-a-user-with-an-unverified-email",
        ),
    ],
)
def test_object_deny_branches(
    ctx: ActingContext,
    action: Action,
    attrs: dict[str, object],
    reason: str,
    message: str,
    not_found: bool,
    code: str | None,
) -> None:
    _expect(
        authorize(ctx, action, OBJECT, attrs),
        effect=Effect.DENY,
        reason=reason,
        policy=workspace_object.POLICY,
        message=message,
        as_not_found=not_found,
        error_code=code,
    )


@pytest.mark.parametrize("action", OWNER_ACTIONS, ids=[a.value for a in OWNER_ACTIONS])
def test_an_object_a_member_cannot_read_is_not_found_before_it_is_forbidden(
    action: Action,
) -> None:
    """Precedence: a member outside the audience learns nothing about the
    object, not even that they lack the right to change it."""
    decision = authorize(OTHER_USER, action, OBJECT, PRIVATE_OBJECT)
    assert (decision.reason, decision.as_not_found) == ("not_in_audience", True)


@pytest.mark.parametrize("key", sorted(OBJECT_ALLOW))
def test_every_object_fact_is_required_on_the_agent_upload_branch_too(key: str) -> None:
    """The agent-only branch resolves the same fact set as every other: a
    daemon that omits one is denied, never admitted by the check it skipped."""
    attrs = {k: v for k, v in OBJECT_ALLOW.items() if k != key}
    decision = authorize(OWNER_AGENT, Action.UPLOAD_PAYLOAD, OBJECT, attrs)
    assert decision.reason == f"missing_attribute:{key}"


def test_an_empty_owner_id_makes_nobody_the_owner() -> None:
    """An unowned object is not everyone's: a member reads it because the org
    scope says so, and still may not write it."""
    attrs = {**OBJECT_ALLOW, "owner_user_id": ""}
    assert authorize(USER, Action.READ, OBJECT, attrs).allowed is True
    assert authorize(USER, Action.WRITE, OBJECT, attrs).reason == "owner_or_org_admin_required"


def test_a_team_id_that_is_not_a_uuid_reads_as_missing_on_a_chat() -> None:
    """A malformed id is not an absent one by accident: it denies, and it names
    the attribute it could not parse."""
    attrs = {**CHAT_ALLOW, "visibility_scope": f"team:{TEAM}", "team_id": "not-a-uuid"}
    assert authorize(USER, Action.READ, CHAT, attrs).reason == "missing_attribute:team_id"


# ---------------------------------------------------------------------------
# compute.machine
# ---------------------------------------------------------------------------

COMPUTE_ACTIONS = [Action.READ, Action.WRITE]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message", "as_not_found", "error_code"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "member_read", "", False, None, id="read"),
        pytest.param(Action.WRITE, {}, Effect.ALLOW, "member_write", "", False, None, id="write"),
        pytest.param(
            Action.READ,
            {"email_verified": False},
            Effect.ALLOW,
            "member_read",
            "",
            False,
            None,
            id="read-needs-no-verified-email",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.OWNER})},
            Effect.ALLOW,
            "member_write",
            "",
            False,
            None,
            id="owner-implies-member",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.ADMIN}), "lifecycle": "workspace"},
            Effect.ALLOW,
            "member_write",
            "",
            False,
            None,
            id="admin-writes-a-workspace-machine",
        ),
        pytest.param(
            Action.WRITE,
            {
                "roles": frozenset({Role.MEMBER, Role.ADMIN}),
                "lifecycle": "workspace",
                "controls": True,
            },
            Effect.ALLOW,
            "admin_controls",
            "",
            False,
            None,
            id="admin-controls-the-org-box",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.OWNER}), "lifecycle": "workspace", "controls": True},
            Effect.ALLOW,
            "admin_controls",
            "",
            False,
            None,
            id="owner-controls-the-org-box",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "workspace", "controls": True},
            Effect.DENY,
            "org_admin_required",
            "Only an org admin may set up or take down the organization's machine.",
            False,
            "org_admin_required",
            id="member-cannot-register-the-org-box",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "session", "controls": True},
            Effect.ALLOW,
            "member_controls_own_allocation",
            "",
            False,
            None,
            id="a-members-own-session-allocation-is-not-the-org-box",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "some-future-lifecycle", "controls": True},
            Effect.ALLOW,
            "member_controls_own_allocation",
            "",
            False,
            None,
            id="a-lifecycle-that-is-not-the-org-box-takes-a-named-branch",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "workspace", "controls": True, "roles": frozenset({Role.MEMBER})},
            Effect.DENY,
            "org_admin_required",
            "Only an org admin may set up or take down the organization's machine.",
            False,
            "org_admin_required",
            id="a-member-cannot-take-the-org-box-down-either",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "workspace", "controls": False},
            Effect.ALLOW,
            "member_write",
            "",
            False,
            None,
            id="a-member-still-heartbeats-the-box-they-operate",
        ),
        pytest.param(
            Action.READ,
            {"lifecycle": "workspace", "controls": True},
            Effect.ALLOW,
            "member_read",
            "",
            False,
            None,
            id="registering-is-a-write-concept-reads-are-untouched",
        ),
        pytest.param(
            Action.WRITE,
            {"lifecycle": "workspace", "controls": True, "email_verified": False},
            Effect.DENY,
            "email_verification_required",
            "Verify your email address to perform this action.",
            False,
            "email_verification_required",
            id="unverified-is-answered-before-the-admin-question",
        ),
        pytest.param(
            Action.READ,
            {"in_org": False},
            Effect.DENY,
            "team_not_in_org",
            "Machine not found",
            True,
            None,
            id="read-outside-org-is-opaque",
        ),
        pytest.param(
            Action.WRITE,
            {"in_org": False, "roles": frozenset()},
            Effect.DENY,
            "team_not_in_org",
            "Machine not found",
            True,
            None,
            id="not-in-org-wins-over-no-role",
        ),
        pytest.param(
            Action.READ,
            {"is_owner": False},
            Effect.DENY,
            "not_owner",
            "Machine not found",
            True,
            None,
            id="foreign-allocation-is-opaque",
        ),
        pytest.param(
            Action.WRITE,
            {"is_owner": False, "email_verified": False},
            Effect.DENY,
            "not_owner",
            "Machine not found",
            True,
            None,
            id="not-owner-wins-over-unverified",
        ),
        pytest.param(
            Action.READ,
            {"roles": frozenset({Role.VIEWER})},
            Effect.DENY,
            "org_member_required",
            "org member role required",
            False,
            None,
            id="viewer-cannot-read",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset()},
            Effect.DENY,
            "org_member_required",
            "org member role required",
            False,
            None,
            id="no-role-cannot-write",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.AGENT})},
            Effect.DENY,
            "org_member_required",
            "org member role required",
            False,
            None,
            id="agent-marker-is-not-a-member",
        ),
        pytest.param(
            Action.WRITE,
            {"email_verified": False},
            Effect.DENY,
            "email_verification_required",
            "Verify your email address to perform this action.",
            False,
            "email_verification_required",
            id="write-needs-a-verified-email",
        ),
    ],
)
def test_compute_machine_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
    error_code: str | None,
) -> None:
    decision = authorize(USER, action, MACHINE, {**COMPUTE_ALLOW, **overrides})
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="compute.machine",
        message=message,
        as_not_found=as_not_found,
        error_code=error_code,
    )


@pytest.mark.parametrize(
    "other",
    [a for a in Action if a not in COMPUTE_ACTIONS],
    ids=lambda a: a.value,
)
def test_compute_refuses_every_action_but_read_and_write_before_reading_a_fact(
    other: Action,
) -> None:
    _expect(
        authorize(USER, other, MACHINE, {}),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="compute.machine",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", COMPUTE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(COMPUTE_WRONG_TYPED), ids=str)
def test_compute_missing_attribute_denies(action: Action, key: str) -> None:
    """Remove any one fact from an otherwise allowing request: deny — on BOTH
    actions, including the facts the branch would not have consulted."""
    _expect(
        authorize(USER, action, MACHINE, _without(COMPUTE_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.machine",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", COMPUTE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(COMPUTE_WRONG_TYPED), ids=str)
def test_compute_wrong_attribute_type_denies(action: Action, key: str) -> None:
    attrs = {**COMPUTE_ALLOW, key: COMPUTE_WRONG_TYPED[key]}
    _expect(
        authorize(USER, action, MACHINE, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.machine",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", COMPUTE_ACTIONS, ids=lambda a: a.value)
def test_compute_cross_org_is_opaque_before_the_policy_runs(action: Action) -> None:
    foreign = Resource(ResourceType.COMPUTE_MACHINE, id="alloc-1", org_id=OTHER_ORG)
    _expect(
        authorize(USER, action, foreign, COMPUTE_ALLOW),
        effect=Effect.DENY,
        reason="cross_org",
        policy="compute.machine",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("ctx", [AGENT, PAT], ids=["agent", "pat"])
@pytest.mark.parametrize("action", COMPUTE_ACTIONS, ids=lambda a: a.value)
def test_compute_delegated_principals_drive_machines_like_their_user(
    ctx: ActingContext, action: Action
) -> None:
    """The daemon on the box is an agent acting for the customer's user: it
    controls and heartbeats with the user's roles."""
    assert authorize(ctx, action, MACHINE, COMPUTE_ALLOW).allowed is True


@pytest.mark.parametrize("ctx", [SERVICE_CI, SERVICE_PROXY], ids=["ci", "proxy"])
def test_compute_decides_on_the_supplied_roles_whatever_the_subject(ctx: ActingContext) -> None:
    """A service principal carries no org role, so the resolver hands the policy
    an empty set and it is refused as a non-member — by the facts, not the kind."""
    assert authorize(ctx, Action.READ, MACHINE, COMPUTE_ALLOW).allowed is True
    denied = authorize(ctx, Action.READ, MACHINE, {**COMPUTE_ALLOW, "roles": frozenset()})
    assert denied.reason == "org_member_required"


# ---------------------------------------------------------------------------
# compute.grant — the platform's own dial
# ---------------------------------------------------------------------------

#: The grant resource is PLATFORM-scoped: no org_id, so the engine's cross-org
#: guard never fires and the target org is decided as a fact instead.
GRANT = Resource(ResourceType.COMPUTE_GRANT, id=str(ORG))

GRANT_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "org_exists": True,
    "target_org_id": str(ORG),
}
GRANT_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "org_exists": "yes",
    "target_org_id": ORG,
}
GRANT_ACTIONS = [Action.READ, Action.ADMIN]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message", "as_not_found"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "staff_read", "", False, id="admin-reads"),
        pytest.param(
            Action.READ,
            {"platform_admin": False},
            Effect.ALLOW,
            "staff_read",
            "",
            False,
            id="support-reads",
        ),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_grant", "", False, id="admin-grants"),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            False,
            id="support-may-not-grant",
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
            id="a-tenant-cannot-grant",
        ),
        pytest.param(
            Action.READ,
            {"org_exists": False},
            Effect.DENY,
            "org_not_found",
            "Organization not found",
            True,
            id="unknown-org-reads-as-missing",
        ),
        pytest.param(
            Action.ADMIN,
            {"org_exists": False},
            Effect.DENY,
            "org_not_found",
            "Organization not found",
            True,
            id="unknown-org-cannot-be-granted",
        ),
    ],
)
def test_compute_grant_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
) -> None:
    _expect(
        authorize(USER, action, GRANT, {**GRANT_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="compute.grant",
        message=message,
        as_not_found=as_not_found,
    )


def test_compute_grant_hides_org_existence_from_a_non_staff_caller() -> None:
    """The staff check comes FIRST: a stranger who guesses an org id must not
    learn from the status code whether it was a real one."""
    stranger = {**GRANT_ALLOW, "platform_staff": False, "platform_admin": False}
    real = authorize(USER, Action.READ, GRANT, stranger)
    ghost = authorize(USER, Action.READ, GRANT, {**stranger, "org_exists": False})
    assert real.reason == ghost.reason == "platform_staff_required"
    assert real.as_not_found is ghost.as_not_found is False


@pytest.mark.parametrize(
    "other",
    [a for a in Action if a not in GRANT_ACTIONS],
    ids=lambda a: a.value,
)
def test_compute_grant_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, GRANT, GRANT_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="compute.grant",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", GRANT_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(GRANT_WRONG_TYPED), ids=str)
def test_compute_grant_missing_attribute_denies(action: Action, key: str) -> None:
    """Remove any one fact from an otherwise allowing request: deny — on BOTH
    actions, including the facts that branch would not have consulted."""
    _expect(
        authorize(USER, action, GRANT, _without(GRANT_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.grant",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", GRANT_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(GRANT_WRONG_TYPED), ids=str)
def test_compute_grant_wrong_attribute_type_denies(action: Action, key: str) -> None:
    _expect(
        authorize(USER, action, GRANT, {**GRANT_ALLOW, key: GRANT_WRONG_TYPED[key]}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.grant",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    "ctx", [AGENT, PAT, SERVICE_CI, SERVICE_PROXY], ids=["agent", "pat", "ci", "proxy"]
)
@pytest.mark.parametrize("action", GRANT_ACTIONS, ids=lambda a: a.value)
def test_compute_grant_decides_on_the_facts_whatever_the_subject(
    ctx: ActingContext, action: Action
) -> None:
    """The policy never reads the principal: a delegated or machine credential
    that carries the staff facts decides identically, and one that does not is
    refused identically. Staffness is a fact the route resolves, not a kind."""
    assert authorize(ctx, action, GRANT, GRANT_ALLOW).allowed is True
    refused = authorize(ctx, action, GRANT, {**GRANT_ALLOW, "platform_staff": False})
    assert refused.reason == "platform_staff_required"


# ---------------------------------------------------------------------------
# platform.ban — who is shut out of the product
# ---------------------------------------------------------------------------

#: Platform-scoped like the compute grant: no org_id, the tenant of the banned
#: person is never the resource's org.
BAN = Resource(ResourceType.PLATFORM_BAN, id=str(USER_ID))

BAN_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "kind": "user",
    "operation": "ban",
}
BAN_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "kind": 7,
    "operation": ["ban"],
}
BAN_ACTIONS = [Action.READ, Action.ADMIN]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "admin_reads", "", id="admin-reads"),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_changes", "", id="admin-bans"),
        pytest.param(
            Action.ADMIN,
            {"kind": "domain", "operation": "lift"},
            Effect.ALLOW,
            "admin_changes",
            "",
            id="admin-lifts-a-domain",
        ),
        pytest.param(
            Action.READ,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-read-the-register",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-ban",
        ),
        pytest.param(
            Action.READ,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-ban",
        ),
    ],
)
def test_platform_ban_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
) -> None:
    _expect(
        authorize(USER, action, BAN, {**BAN_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="platform.ban",
        message=message,
    )


def test_platform_ban_admin_flag_alone_is_not_staff() -> None:
    """The staff floor is checked first: a caller whose facts say admin but not
    staff is a contradiction the policy refuses at the floor."""
    contradiction = {**BAN_ALLOW, "platform_staff": False, "platform_admin": True}
    assert authorize(USER, Action.ADMIN, BAN, contradiction).reason == "platform_staff_required"


@pytest.mark.parametrize(
    "other",
    [a for a in Action if a not in BAN_ACTIONS],
    ids=lambda a: a.value,
)
def test_platform_ban_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, BAN, BAN_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="platform.ban",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", BAN_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(BAN_WRONG_TYPED), ids=str)
def test_platform_ban_missing_attribute_denies(action: Action, key: str) -> None:
    """Remove any one fact from an otherwise allowing request: deny — on BOTH
    actions, the record-only facts included."""
    _expect(
        authorize(USER, action, BAN, _without(BAN_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="platform.ban",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", BAN_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(BAN_WRONG_TYPED), ids=str)
def test_platform_ban_wrong_attribute_type_denies(action: Action, key: str) -> None:
    _expect(
        authorize(USER, action, BAN, {**BAN_ALLOW, key: BAN_WRONG_TYPED[key]}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="platform.ban",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    "ctx", [AGENT, PAT, SERVICE_CI, SERVICE_PROXY], ids=["agent", "pat", "ci", "proxy"]
)
@pytest.mark.parametrize("action", BAN_ACTIONS, ids=lambda a: a.value)
def test_platform_ban_decides_on_the_facts_whatever_the_subject(
    ctx: ActingContext, action: Action
) -> None:
    """Staffness is a fact the route resolves, never read off the principal."""
    assert authorize(ctx, action, BAN, BAN_ALLOW).allowed is True
    refused = authorize(ctx, action, BAN, {**BAN_ALLOW, "platform_admin": False})
    assert refused.reason == "platform_admin_required"


# --------------------------------------------------------------------------
# files.access — one node of the Files tree
# --------------------------------------------------------------------------

FILE_NODE = Resource(ResourceType.FILE_NODE, id="node-1", org_id=ORG, team_id=TEAM)

#: Every ``allowed_actions`` set the ladder can hand the policy, plus the empty
#: one a caller with no grant gets. Read off the ladder table rather than
#: re-spelled here, so a rung added to the product is covered by this table.
LADDER_SETS: dict[str, frozenset[str]] = {
    "no_role": frozenset(),
    **{role: frozenset(a.value for a in actions) for role, actions in files_ladder.LADDER},
}

#: Every action the Files surface decides about.
FILE_ACTIONS: tuple[Action, ...] = tuple(Action(a.value) for a in FilesAction)

FILES_ALLOW: dict[str, object] = {
    "exists": True,
    "in_org": True,
    "allowed_actions": LADDER_SETS["owner"],
    "flags": frozenset(),
    "is_agent": False,
    "is_link": False,
    "drive_kind": "org",
    "held": False,
    "locked": False,
    "trashed": False,
    "seal_self_only": False,
    "no_reshare_chain": False,
    "read_via_conditional_grant": False,
    "agent_machine_verified": False,
    "chat_subtree": False,
    "chat_bound_elsewhere": False,
    "workspace_subtree": False,
    "workspace_bound_elsewhere": False,
    "holds_lease": False,
    "machine_runs_it": False,
    "grant_within_rung": True,
}

FILES_WRONG_TYPED: dict[str, object] = {
    "exists": "true",
    "in_org": "true",
    "allowed_actions": ["read", "write"],
    "flags": ["held"],
    "is_agent": "no",
    "is_link": 1,
    "drive_kind": 7,
    "held": "false",
    "locked": None,
    "trashed": "no",
    "seal_self_only": 1,
    "no_reshare_chain": "yes",
    "read_via_conditional_grant": 1,
    "agent_machine_verified": "yes",
    "chat_subtree": "yes",
    "chat_bound_elsewhere": "yes",
    "workspace_subtree": "yes",
    "workspace_bound_elsewhere": "yes",
    "holds_lease": "yes",
    "machine_runs_it": "yes",
    "grant_within_rung": "yes",
}


def _files_attrs(**overrides: object) -> dict[str, object]:
    return {**FILES_ALLOW, **overrides}


def test_files_a_share_above_the_sharers_own_rung_is_refused_visibly() -> None:
    """The ceiling on a share. The ladder handed the caller ``share`` (they are
    at least a manager) and the rung they are granting or withdrawing is above
    their own: refused, on record, with its own code — they can read the node,
    so the refusal names itself."""
    _expect(
        authorize(USER, Action.SHARE, FILE_NODE, _files_attrs(grant_within_rung=False)),
        effect=Effect.DENY,
        reason="grant_exceeds_rung",
        policy="files.access",
        message="Not allowed",
        error_code="files.grant_exceeds_rung",
    )


def test_files_the_share_ceiling_never_leaks_past_the_read_gate() -> None:
    """A caller with no read on the node is told nothing exists, whatever the
    rung they asked for: the ceiling is decided after the action check."""
    _expect(
        authorize(
            USER,
            Action.SHARE,
            FILE_NODE,
            _files_attrs(allowed_actions=frozenset(), grant_within_rung=False),
        ),
        effect=Effect.DENY,
        reason="action_not_allowed_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    "action", [a for a in FILE_ACTIONS if a is not Action.SHARE], ids=lambda a: a.value
)
def test_files_the_share_ceiling_touches_no_other_action(action: Action) -> None:
    """The negative twin: the ceiling is about granting a rung and nothing
    else, so a caller who could write, copy or purge still can."""
    assert authorize(USER, action, FILE_NODE, _files_attrs(grant_within_rung=False)).allowed


# -- undoing a trash -----------------------------------------------------------
# An undo is decided as the work it does: undoing a trash is RESTORE on the
# trashed root. Asking the drive root for WRITE instead refused every member
# their own undo, because a member reads the org drive's root and writes nothing
# there.


@pytest.mark.parametrize(
    ("action", "attrs", "allowed", "reason", "as_not_found"),
    [
        pytest.param(
            Action.RESTORE,
            {"trashed": True},
            True,
            "allowed_action",
            False,
            id="the-owner-restores-their-own-trashed-file",
        ),
        pytest.param(
            Action.RESTORE,
            {"trashed": True, "allowed_actions": LADDER_SETS["reader"]},
            False,
            "action_not_allowed",
            False,
            id="a-reader-of-the-trashed-file-is-refused-visibly",
        ),
        pytest.param(
            Action.RESTORE,
            {"trashed": True, "allowed_actions": frozenset()},
            False,
            "action_not_allowed_unreadable",
            True,
            id="a-stranger-to-the-trashed-file-is-told-nothing",
        ),
        pytest.param(
            Action.WRITE,
            {"allowed_actions": LADDER_SETS["reader"]},
            False,
            "action_not_allowed",
            False,
            id="a-member-writes-nothing-at-the-drive-root",
        ),
    ],
)
def test_files_undoing_a_trash_is_decided_as_a_restore(
    action: Action,
    attrs: dict[str, object],
    allowed: bool,
    reason: str,
    as_not_found: bool,
) -> None:
    decision = authorize(USER, action, FILE_NODE, _files_attrs(**attrs))
    assert (decision.allowed, decision.reason, decision.as_not_found) == (
        allowed,
        reason,
        as_not_found,
    )


# -- a chat's own record ------------------------------------------------------

#: What a record refuses everyone but the holder, as the policy spells it.
RECORD_BLOCKED: tuple[Action, ...] = (Action.WRITE, Action.DELETE, Action.RESTORE)
CHAT_RECORD_READ_ONLY = "files.chat_record_read_only"


def _record_attrs(**overrides: object) -> dict[str, object]:
    """A chat's record as the OWNER of the chat meets it: the full owner set,
    the record flag, and no lease held — the decider would already have taken
    the three actions away, and the policy must refuse them even when it has
    not, which is the second net this table pins."""
    return _files_attrs(**{"flags": frozenset({"record"}), "holds_lease": False, **overrides})


@pytest.mark.parametrize("action", RECORD_BLOCKED, ids=lambda a: a.value)
def test_files_a_chats_record_is_read_only_to_a_person_however_much_they_hold(
    action: Action,
) -> None:
    """The chat's owner, a member with edit, an org admin: all hold WRITE on
    the folder, and none may rewrite, purge or restore the record of what was
    said. Refused visibly, with its own code, on record under its own reason —
    so the row and the body say "this is the chat's record", not "you lack
    the rung"."""
    _expect(
        authorize(USER, action, FILE_NODE, _record_attrs()),
        effect=Effect.DENY,
        reason="chat_record",
        policy="files.access",
        message="Not allowed",
        error_code=CHAT_RECORD_READ_ONLY,
    )


@pytest.mark.parametrize("action", RECORD_BLOCKED, ids=lambda a: a.value)
def test_files_the_machine_holding_the_chats_lease_writes_its_record(action: Action) -> None:
    """The one writer a record admits: the box proven to hold the live lease
    over it, which is the process that produces the record in the first
    place. ``holds_lease`` is the decider's finding — agent, verified machine,
    lease covering the node — never a header the caller sent."""
    assert authorize(AGENT, action, FILE_NODE, _record_attrs(holds_lease=True)).allowed


@pytest.mark.parametrize(
    "action", [a for a in FILE_ACTIONS if a not in RECORD_BLOCKED], ids=lambda a: a.value
)
def test_files_a_record_refuses_nothing_but_the_three(action: Action) -> None:
    """The negative twin: a record is a thing a person may see, comment on,
    copy and lease — only changing it is the box's."""
    assert authorize(USER, action, FILE_NODE, _record_attrs()).allowed


@pytest.mark.parametrize("action", RECORD_BLOCKED, ids=lambda a: a.value)
def test_files_the_record_refusal_never_leaks_past_the_read_gate(action: Action) -> None:
    """A caller who cannot read the record is told nothing exists — the code
    would confirm that a chat, and its transcript, are there."""
    _expect(
        authorize(USER, action, FILE_NODE, _record_attrs(allowed_actions=frozenset())),
        effect=Effect.DENY,
        reason="chat_record_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", RECORD_BLOCKED, ids=lambda a: a.value)
def test_files_holding_the_lease_adds_no_action_the_ladder_did_not_hand_out(
    action: Action,
) -> None:
    """``holds_lease`` lifts the record's refusal and nothing else: a holder
    the ladder gave no WRITE is still refused on the rung, and the row says
    so under the rung's reason rather than the record's."""
    decision = authorize(
        AGENT,
        action,
        FILE_NODE,
        _record_attrs(holds_lease=True, allowed_actions=frozenset({"read"})),
    )
    assert decision.allowed is False
    assert decision.reason == "action_not_allowed"


def test_files_a_plain_file_is_untouched_by_the_record_rule() -> None:
    """The flag decides, not the lease fact: an unflagged node with no lease
    held writes as its rung allows."""
    assert authorize(USER, Action.WRITE, FILE_NODE, _files_attrs(holds_lease=False)).allowed


@pytest.mark.parametrize("role", sorted(LADDER_SETS), ids=sorted(LADDER_SETS))
@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_allows_exactly_the_actions_the_ladder_handed_it(role: str, action: Action) -> None:
    """The policy is a table over ``allowed_actions`` and nothing else: no role
    name reaches it, so on an unflagged node the only thing that can decide is
    whether the action is in the set."""
    allowed = LADDER_SETS[role]
    decision = authorize(USER, action, FILE_NODE, _files_attrs(allowed_actions=allowed))
    assert decision.allowed is (action.value in allowed), decision


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_reader_who_lacks_the_action_gets_a_visible_403(action: Action) -> None:
    """They can already read the node, so naming the refusal tells them nothing
    they did not know — and a bare 404 would be a lie they cannot act on."""
    allowed = LADDER_SETS["reader"]
    if action.value in allowed:
        pytest.skip("a reader holds this action")
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(allowed_actions=allowed)),
        effect=Effect.DENY,
        reason="action_not_allowed",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_caller_who_cannot_read_is_told_nothing_exists(action: Action) -> None:
    """The negative twin of the case above: with no read there is no 403 to be
    had on any action, because a 403 would confirm the node."""
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(allowed_actions=frozenset())),
        effect=Effect.DENY,
        reason="action_not_allowed_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_node_that_is_not_there_is_opaque_for_every_action(action: Action) -> None:
    """A node that does not exist is decided by the policy, not by an early
    return in the caller, so it leaves the same decision row a node the caller
    may not read leaves — and the answer is the same opaque not-found."""
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(exists=False)),
        effect=Effect.DENY,
        reason="no_such_node",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_absence_outranks_every_grant_the_caller_holds(action: Action) -> None:
    """The negative twin: an owner's full action set, an unheld unlocked node
    and a caller squarely in the org still cannot make a missing node allowed.
    Absence is checked before anything the caller could influence."""
    decision = authorize(
        USER,
        action,
        FILE_NODE,
        _files_attrs(exists=False, in_org=True, allowed_actions=LADDER_SETS["owner"]),
    )
    assert decision.allowed is False, decision
    assert decision.as_not_found is True
    assert decision.error_code is None


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_out_of_org_is_opaque_for_every_action(action: Action) -> None:
    """Even holding every action, a caller the decider placed outside the org
    gets the not-found a stranger gets."""
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(in_org=False)),
        effect=Effect.DENY,
        reason="not_in_org",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    ("flag_attrs", "action", "reason"),
    [
        pytest.param({"held": True}, Action.WRITE, "held", id="held-write"),
        pytest.param({"held": True}, Action.DELETE, "held", id="held-delete"),
        pytest.param({"locked": True}, Action.WRITE, "locked", id="locked-write"),
        pytest.param({"flags": frozenset({"frozen"})}, Action.WRITE, "frozen", id="frozen-write"),
        pytest.param({"flags": frozenset({"frozen"})}, Action.DELETE, "frozen", id="frozen-delete"),
        pytest.param({"flags": frozenset({"frozen"})}, Action.SHARE, "frozen", id="frozen-share"),
    ],
)
def test_files_a_flag_refuses_an_action_the_ladder_left_standing(
    flag_attrs: dict[str, object], action: Action, reason: str
) -> None:
    """The second net. ``allowed_actions`` here is the owner's full set — the
    decider has *not* removed the action — and the policy still refuses, so a
    decider bug or a future engine cannot write to a held node."""
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(**flag_attrs)),
        effect=Effect.DENY,
        reason=reason,
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize(
    ("flag_attrs", "action"),
    [
        pytest.param({"held": True}, Action.READ, id="held-read"),
        pytest.param({"held": True}, Action.SHARE, id="held-share"),
        pytest.param({"locked": True}, Action.DELETE, id="locked-delete"),
        pytest.param({"locked": True}, Action.READ, id="locked-read"),
        pytest.param({"flags": frozenset({"frozen"})}, Action.READ, id="frozen-read"),
        pytest.param({"flags": frozenset({"frozen"})}, Action.EXPORT, id="frozen-export"),
        pytest.param(
            {"flags": frozenset({"frozen"})}, Action.LEASE_REQUEST, id="frozen-lease-request"
        ),
        pytest.param(
            {"flags": frozenset({"no_download", "no_reshare"})},
            Action.WRITE,
            id="narrowing-flags-the-decider-owns",
        ),
    ],
)
def test_files_a_flag_narrows_only_what_it_names(
    flag_attrs: dict[str, object], action: Action
) -> None:
    """The negative twin: a hold stops writes, not reads; a freeze leaves the
    read-only actions standing; the flags the decider alone enforces
    (``no_download``, ``no_reshare``) do not refuse anything a second time."""
    assert authorize(USER, action, FILE_NODE, _files_attrs(**flag_attrs)).allowed is True


@pytest.mark.parametrize(
    ("source_attrs", "reason"),
    [
        pytest.param({"trashed": True}, "trashed", id="trashed"),
        pytest.param({"flags": frozenset({"no_download"})}, "sealed", id="sealed-subtree"),
        pytest.param(
            {"flags": frozenset({"no_download"}), "trashed": True},
            "trashed",
            id="trashed-outranks-sealed",
        ),
    ],
)
def test_files_copy_refuses_a_trashed_or_sealed_source_visibly(
    source_attrs: dict[str, object], reason: str
) -> None:
    """The ladder handed the owner ``copy`` and the policy still refuses: a node
    in the trash is restored or gone, never copied, and a seal that covers the
    subtree would be left behind on the copy — a download by another name."""
    _expect(
        authorize(USER, Action.COPY, FILE_NODE, _files_attrs(**source_attrs)),
        effect=Effect.DENY,
        reason=reason,
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize(
    "source_attrs",
    [
        pytest.param({"trashed": True}, id="trashed"),
        pytest.param({"flags": frozenset({"no_download"})}, id="sealed-subtree"),
    ],
)
def test_files_copy_of_an_unreadable_trashed_or_sealed_source_is_opaque(
    source_attrs: dict[str, object],
) -> None:
    """A caller who cannot read the source learns nothing from the copy
    refusal either — not that it is in the trash, not that it is sealed."""
    _expect(
        authorize(
            USER, Action.COPY, FILE_NODE, _files_attrs(allowed_actions=frozenset(), **source_attrs)
        ),
        effect=Effect.DENY,
        reason="action_not_allowed_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    "source_attrs",
    [
        pytest.param({}, id="plain"),
        pytest.param(
            {"flags": frozenset({"no_download"}), "seal_self_only": True}, id="chat-seal-self-only"
        ),
        pytest.param({"flags": frozenset({"frozen"})}, id="frozen-drive-still-a-source"),
        pytest.param({"held": True}, id="held-still-a-source"),
        pytest.param({"locked": True}, id="locked-still-a-source"),
        pytest.param({"allowed_actions": LADDER_SETS["reader"]}, id="reader-may-copy"),
    ],
)
def test_files_copy_allows_what_a_reader_may_read(source_attrs: dict[str, object]) -> None:
    """The negative twins. A chat folder is sealed self-only — the conversation
    never leaves as a file, and its copy is stamped the same — so it copies; a
    frozen, held or locked node is still readable and so still a source; the
    lowest rung that reads may copy."""
    assert authorize(USER, Action.COPY, FILE_NODE, _files_attrs(**source_attrs)).allowed is True


@pytest.mark.parametrize(
    "action", [a for a in FILE_ACTIONS if a is not Action.COPY], ids=lambda a: a.value
)
def test_files_trashed_and_sealed_only_refuse_a_copy(action: Action) -> None:
    """The two source facts decide a copy and nothing else: reading, sharing
    and deleting a trashed node are other routes' business."""
    attrs = _files_attrs(trashed=True, flags=frozenset({"no_download"}))
    decision = authorize(USER, action, FILE_NODE, attrs)
    assert decision.reason not in ("trashed", "sealed"), decision


def test_files_an_agent_never_shares_even_as_owner() -> None:
    """A delegated principal cannot widen the reach of the human it acts for."""
    _expect(
        authorize(AGENT, Action.SHARE, FILE_NODE, _files_attrs(is_agent=True)),
        effect=Effect.DENY,
        reason="agent_may_not_share",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


def test_files_an_agent_that_cannot_read_is_refused_opaquely_for_share() -> None:
    _expect(
        authorize(
            AGENT,
            Action.SHARE,
            FILE_NODE,
            _files_attrs(is_agent=True, allowed_actions=frozenset()),
        ),
        effect=Effect.DENY,
        reason="agent_may_not_share_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    "action",
    [a for a in FILE_ACTIONS if a is not Action.SHARE and a.value not in files_policy.MACHINE_ONLY],
    ids=lambda a: a.value,
)
def test_files_an_agent_keeps_every_other_action_it_holds(action: Action) -> None:
    """The negative twin of the SHARE rule: confinement is the decider's job and
    it has already run; the policy only removes sharing and the three actions
    that are the machine's alone."""
    attrs = _files_attrs(is_agent=True, chat_subtree=True)
    assert authorize(AGENT, action, FILE_NODE, attrs).allowed is True


# -- the assertion is not the proof ----------------------------------------


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_an_unverified_agent_is_refused_what_only_the_machine_may_do(action: str) -> None:
    """The two agent headers are a claim, and a box's id is public — it rides
    every chat that box serves. An agent whose assertion was not checked against
    the registration is a person spelling a public id, so holding the folder,
    writing in it and snapshotting it are all refused, by that name."""
    _expect(
        authorize(
            AGENT,
            Action(action),
            FILE_NODE,
            _files_attrs(is_agent=True, agent_machine_verified=False, chat_subtree=True),
        ),
        effect=Effect.DENY,
        reason="machine_unverified",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_an_unverified_agent_that_cannot_read_is_refused_opaquely(action: str) -> None:
    """A refusal that named the reason to a caller who cannot read the node
    would confirm the node is there. The unreadable variant is the same opaque
    not-found every other refusal collapses to."""
    _expect(
        authorize(
            AGENT,
            Action(action),
            FILE_NODE,
            _files_attrs(
                is_agent=True,
                agent_machine_verified=False,
                chat_subtree=True,
                allowed_actions=frozenset(),
            ),
        ),
        effect=Effect.DENY,
        reason="machine_unverified_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_verified_machine_may_do_what_the_ladder_gave_it(action: str) -> None:
    """The positive twin: the very same request, with the assertion proven,
    passes — so the refusal above is the verification and not the rung."""
    decision = authorize(
        AGENT,
        Action(action),
        FILE_NODE,
        _files_attrs(is_agent=True, agent_machine_verified=True, chat_subtree=True),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_person_is_never_asked_to_prove_a_machine(action: str) -> None:
    """The narrowing is on the assertion, never on the human. A member acting on
    their own session carries ``agent_machine_verified=False`` — there is no
    machine to verify — and keeps everything their rung gives them."""
    decision = authorize(
        USER,
        Action(action),
        FILE_NODE,
        _files_attrs(is_agent=False, agent_machine_verified=False, chat_subtree=True),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize(
    "action",
    [a for a in FILE_ACTIONS if a.value not in files_policy.MACHINE_ONLY and a is not Action.SHARE],
    ids=lambda a: a.value,
)
def test_files_an_unverified_agent_keeps_what_is_not_the_machines(action: Action) -> None:
    """The asymmetric case. Reading, exporting, copying and asking for a folder
    back are the user's, not the box's: an unverified assertion loses exactly
    the three actions that would let it act AS the machine and nothing else."""
    decision = authorize(
        AGENT,
        action,
        FILE_NODE,
        _files_attrs(is_agent=True, agent_machine_verified=False, chat_subtree=True),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_an_unverified_agent_outside_a_chat_keeps_the_machines_actions(action: str) -> None:
    """The scope half. A mount of an ordinary shared folder is not a chat a box
    runs, and nothing there is the machine's to lose: the proof is asked for on
    the folders a chat lives in and nowhere else."""
    decision = authorize(
        AGENT,
        Action(action),
        FILE_NODE,
        _files_attrs(is_agent=True, agent_machine_verified=False, chat_subtree=False),
    )
    assert decision.allowed is True, decision


# -- proven, and proven on THIS chat ----------------------------------------


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_proven_box_is_refused_a_chat_that_runs_on_another_box(action: str) -> None:
    """A box controls once and speaks for every chat it serves on that one
    credential, so proving it is a machine proves nothing about WHICH chat is
    its own. On a folder whose chat runs somewhere else it loses the same three
    actions an unproven assertion loses — and the row says which of the two
    failed, because re-sending a perfectly good assertion would not help."""
    _expect(
        authorize(
            AGENT,
            Action(action),
            FILE_NODE,
            _files_attrs(
                is_agent=True,
                agent_machine_verified=True,
                chat_subtree=True,
                chat_bound_elsewhere=True,
            ),
        ),
        effect=Effect.DENY,
        reason="machine_mismatch",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_proven_box_on_another_chat_it_cannot_read_is_refused_opaquely(
    action: str,
) -> None:
    """The box's operator may not read the conversation either. Naming the
    reason would confirm the folder is there, so the refusal collapses to the
    same opaque not-found a node that does not exist produces."""
    _expect(
        authorize(
            AGENT,
            Action(action),
            FILE_NODE,
            _files_attrs(
                is_agent=True,
                agent_machine_verified=True,
                chat_subtree=True,
                chat_bound_elsewhere=True,
                allowed_actions=frozenset(),
            ),
        ),
        effect=Effect.DENY,
        reason="machine_mismatch_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_an_unproven_assertion_on_another_chat_reads_as_unproven(action: str) -> None:
    """Both halves fail at once. The row names the assertion, not the binding:
    an agent nobody verified is not a machine at all, so "you are the wrong
    machine" would be a finding about a fact that was never established."""
    _expect(
        authorize(
            AGENT,
            Action(action),
            FILE_NODE,
            _files_attrs(
                is_agent=True,
                agent_machine_verified=False,
                chat_subtree=True,
                chat_bound_elsewhere=True,
            ),
        ),
        effect=Effect.DENY,
        reason="machine_unverified",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )


@pytest.mark.parametrize(
    "action",
    [a for a in FILE_ACTIONS if a.value not in files_policy.MACHINE_ONLY and a is not Action.SHARE],
    ids=lambda a: a.value,
)
def test_files_a_proven_box_on_another_chat_keeps_what_is_not_the_machines(
    action: Action,
) -> None:
    """The asymmetric case. The binding narrows exactly what acting AS the
    machine buys — holding the folder, changing it, snapshotting it. Reading a
    colleague's chat, exporting from it and asking for it back are the
    operator's own reach and are untouched by whose box is running it."""
    decision = authorize(
        AGENT,
        action,
        FILE_NODE,
        _files_attrs(
            is_agent=True,
            agent_machine_verified=True,
            chat_subtree=True,
            chat_bound_elsewhere=True,
        ),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_proven_box_outside_a_chat_is_never_asked_whose_chat_it_is(action: str) -> None:
    """The scope half, again. An ordinary shared folder is nobody's chat, so
    there is no binding to fail: a stray ``chat_bound_elsewhere`` cannot narrow
    a mount that has nothing to do with a conversation."""
    decision = authorize(
        AGENT,
        Action(action),
        FILE_NODE,
        _files_attrs(
            is_agent=True,
            agent_machine_verified=True,
            chat_subtree=False,
            chat_bound_elsewhere=True,
        ),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize("action", sorted(files_policy.MACHINE_ONLY))
def test_files_a_person_is_never_asked_whose_chat_it_is(action: str) -> None:
    """The narrowing is on the assertion, never on the human. A member opening
    a chat that a box happens to be running is not a rival machine: they act on
    their own session and keep everything their rung gives them."""
    decision = authorize(
        USER,
        Action(action),
        FILE_NODE,
        _files_attrs(is_agent=False, chat_subtree=True, chat_bound_elsewhere=True),
    )
    assert decision.allowed is True, decision


@pytest.mark.parametrize("key", sorted(FILES_ALLOW), ids=sorted(FILES_ALLOW))
@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_missing_required_fact_denies(key: str, action: Action) -> None:
    _expect(
        authorize(USER, action, FILE_NODE, _without(FILES_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="files.access",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(FILES_ALLOW), ids=sorted(FILES_ALLOW))
@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_mistyped_required_fact_denies(key: str, action: Action) -> None:
    """A ``frozenset`` that arrives as a list, or a flag as the string
    ``"false"``, reads as absent rather than as truthy."""
    _expect(
        authorize(USER, action, FILE_NODE, _files_attrs(**{key: FILES_WRONG_TYPED[key]})),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="files.access",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_cross_org_is_opaque_before_the_policy_runs(action: Action) -> None:
    foreign = Resource(ResourceType.FILE_NODE, id="node-1", org_id=OTHER_ORG)
    _expect(
        authorize(USER, action, foreign, FILES_ALLOW),
        effect=Effect.DENY,
        reason="cross_org",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


def test_files_audits_ids_and_flags_but_never_a_name() -> None:
    """The decision row carries the facts the policy decided on and nothing
    that could name the node — a denial that quoted the filename would answer
    the question the denial exists to refuse."""
    policy = policy_for(ResourceType.FILE_NODE)
    assert policy is not None
    assert policy.audited_attrs & NEVER_AUDITED_KEYS == frozenset()
    assert {"name", "path", "symlink_target", "facts"} & policy.audited_attrs == set()
    recorded = audited_attrs(
        {**FILES_ALLOW, "facts": {"name": "secret.txt"}, "name": "secret.txt"},
        policy.audited_attrs,
    )
    assert set(recorded) == set(FILES_ALLOW)
    assert "secret.txt" not in str(recorded)
    assert recorded["allowed_actions"] == sorted(LADDER_SETS["owner"])


# ---------------------------------------------------------------------------
# org.integration — who may connect or disconnect an org-wide integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(org_policy.SUPPORTED))
def test_org_a_team_that_is_not_the_root_is_opaque(action: Action) -> None:
    """The route only ever decides about the org itself, so a team id here is a
    mistake — answered as not-found rather than as a directory of teams."""
    _expect(
        authorize(USER, action, ORG_RESOURCE, {**ORG_ALLOW, "is_org_root": False}),
        effect=Effect.DENY,
        reason="not_the_org_root",
        policy=org_policy.POLICY,
        message="Not found",
        as_not_found=True,
    )


def test_org_a_member_may_read_the_integration_status() -> None:
    """The link flow tells people to ask an org admin; checking whether the
    workspace is connected is what makes that instruction actionable."""
    decision = authorize(USER, Action.READ, ORG_RESOURCE, {**ORG_ALLOW, "org_admin": False})
    _expect(
        decision,
        effect=Effect.ALLOW,
        reason="org_member_reads",
        policy=org_policy.POLICY,
        message="",
    )


def test_org_a_non_member_cannot_even_read_the_status() -> None:
    _expect(
        authorize(USER, Action.READ, ORG_RESOURCE, {**ORG_ALLOW, "org_member": False}),
        effect=Effect.DENY,
        reason="not_in_org",
        policy=org_policy.POLICY,
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("key", ["org_member"])
def test_org_read_requires_its_own_fact(key: str) -> None:
    """`org_member` is required by READ and by READ only — removing it from an
    ADMIN request is correctly harmless, which is why it is asserted here."""
    _expect(
        authorize(USER, Action.READ, ORG_RESOURCE, _without(ORG_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy=org_policy.POLICY,
        message="Not allowed",
    )
    _expect(
        authorize(USER, Action.READ, ORG_RESOURCE, {**ORG_ALLOW, key: 1}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy=org_policy.POLICY,
        message="Not allowed",
    )
    # ...and the same fact missing does NOT stop an admin changing the thing.
    assert authorize(USER, Action.ADMIN, ORG_RESOURCE, _without(ORG_ALLOW, key)).allowed


def test_org_a_plain_member_may_not_change_the_integration() -> None:
    """A team admin holds a member row at the root: descent runs downwards only."""
    _expect(
        authorize(USER, Action.ADMIN, ORG_RESOURCE, {**ORG_ALLOW, "org_admin": False}),
        effect=Effect.DENY,
        reason="not_org_admin",
        policy=org_policy.POLICY,
        message="Only an organization admin can change this integration",
        error_code="org_admin_required",
    )


def test_org_an_admin_may_change_the_integration() -> None:
    _expect(
        authorize(USER, Action.ADMIN, ORG_RESOURCE, ORG_ALLOW),
        effect=Effect.ALLOW,
        reason="org_admin",
        policy=org_policy.POLICY,
        message="",
    )


def test_org_another_orgs_root_is_refused_before_the_policy_runs() -> None:
    """The tenancy floor, not the policy: a root team in another org never gets
    as far as being asked whether this caller is its admin."""
    foreign = Resource(ResourceType.ORG, id=str(OTHER_ORG), org_id=OTHER_ORG)
    _expect(
        authorize(USER, Action.ADMIN, foreign, ORG_ALLOW),
        effect=Effect.DENY,
        reason="cross_org",
        policy=org_policy.POLICY,
        message="Not found",
        as_not_found=True,
    )


def test_org_audited_attrs_name_no_secret() -> None:
    assert org_policy.AUDITED.isdisjoint(NEVER_AUDITED_KEYS)
    assert set(audited_attrs(ORG_ALLOW, org_policy.AUDITED)) == {
        "integration",
        "operation",
        "is_org_root",
        "org_admin",
        "org_member",
    }


# ---------------------------------------------------------------------------
# org_audit.agent_report — who may write into the org's agent-activity trail
# ---------------------------------------------------------------------------


def test_org_audit_a_member_of_an_entitled_org_reports() -> None:
    _expect(
        authorize(USER, Action.WRITE, ORG_AUDIT_RESOURCE, ORG_AUDIT_ALLOW),
        effect=Effect.ALLOW,
        reason="org_member_reports",
        policy=org_audit_policy.POLICY,
        message="",
    )


def test_org_audit_a_team_that_is_not_the_root_is_opaque() -> None:
    _expect(
        authorize(
            USER, Action.WRITE, ORG_AUDIT_RESOURCE, {**ORG_AUDIT_ALLOW, "is_org_root": False}
        ),
        effect=Effect.DENY,
        reason="not_the_org_root",
        policy=org_audit_policy.POLICY,
        message="Not found",
        as_not_found=True,
    )


def test_org_audit_a_token_whose_membership_is_gone_may_not_write_the_trail() -> None:
    """A CLI token outlives the member row that justified it. Nothing else on
    this route ever looked, so the chain took entries from someone the org had
    already removed."""
    _expect(
        authorize(USER, Action.WRITE, ORG_AUDIT_RESOURCE, {**ORG_AUDIT_ALLOW, "org_member": False}),
        effect=Effect.DENY,
        reason="not_in_org",
        policy=org_audit_policy.POLICY,
        message=org_audit_policy.MEMBER_MESSAGE,
        error_code=org_audit_policy.MEMBER_CODE,
    )


def test_org_audit_weighs_nothing_but_the_membership() -> None:
    """The organization's PLAN is not a fact here, whatever else is in `attrs`.
    Evidence is recorded for every organization and the plan decides who may
    read it; a plan gate on the write drops evidence nobody can get back, and
    leaves every daemon in a plan-less org refused for ever."""
    for plan in (True, False, "whatever"):
        _expect(
            authorize(
                USER, Action.WRITE, ORG_AUDIT_RESOURCE, {**ORG_AUDIT_ALLOW, "enterprise": plan}
            ),
            effect=Effect.ALLOW,
            reason="org_member_reports",
            policy=org_audit_policy.POLICY,
            message="",
        )
    assert "enterprise" not in org_audit_policy.AUDITED


def test_org_audit_another_orgs_trail_is_refused_before_the_policy_runs() -> None:
    foreign = Resource(ResourceType.ORG_AUDIT, id=str(OTHER_ORG), org_id=OTHER_ORG)
    _expect(
        authorize(USER, Action.WRITE, foreign, ORG_AUDIT_ALLOW),
        effect=Effect.DENY,
        reason="cross_org",
        policy=org_audit_policy.POLICY,
        message="Not found",
        as_not_found=True,
    )


def test_org_audit_audited_attrs_name_no_secret() -> None:
    assert org_audit_policy.AUDITED.isdisjoint(NEVER_AUDITED_KEYS)
    assert set(audited_attrs(ORG_AUDIT_ALLOW, org_audit_policy.AUDITED)) == {
        "is_org_root",
        "org_member",
    }


# ---------------------------------------------------------------------------
# team.usage_read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "as_not_found", "message"),
    [
        pytest.param(
            {"roles": frozenset({Role.ADMIN})},
            Effect.ALLOW,
            "team_admin_read",
            False,
            "",
            id="team-admin-reads-its-own-team",
        ),
        pytest.param(
            {"roles": frozenset({Role.OWNER})},
            Effect.ALLOW,
            "team_admin_read",
            False,
            "",
            id="owner-implies-admin",
        ),
        pytest.param(
            {"scope": "org"},
            Effect.ALLOW,
            "team_admin_read",
            False,
            "",
            id="the-org-scope-is-the-same-decision-at-the-root",
        ),
        pytest.param(
            {"roles": frozenset({Role.MEMBER, Role.VIEWER})},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            id="a-plain-member-does-not-read-a-team",
        ),
        pytest.param(
            {"roles": frozenset()},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            id="a-sibling-teams-admin-holds-no-role-here",
        ),
        pytest.param(
            {"in_org": False, "roles": frozenset({Role.ADMIN})},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            id="a-foreign-team-is-opaque-before-any-role",
        ),
    ],
)
def test_team_usage_read_table(
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
) -> None:
    decision = authorize(USER, Action.READ, TEAM_RESOURCE, {**TEAM_ALLOW, **overrides})
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="team.usage_read",
        message=message,
        as_not_found=as_not_found,
    )


def test_team_usage_read_refuses_a_write_verb() -> None:
    """The policy decides one reading. A verb it does not know is refused with
    the opaque message, never allowed by the team-admin branch below it."""
    decision = authorize(USER, Action.SET_BUDGET, TEAM_RESOURCE, TEAM_ALLOW)
    _expect(
        decision,
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="team.usage_read",
        message="Not allowed",
    )


# --------------------------------------------------------------------------- #
# platform.org_storage — an org's storage ceiling, set by hand
# --------------------------------------------------------------------------- #

ORG_STORAGE = Resource(ResourceType.PLATFORM_ORG_STORAGE, id=str(ORG))

ORG_STORAGE_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "operation": "set",
}
ORG_STORAGE_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "operation": ["set"],
}
ORG_STORAGE_ACTIONS = [Action.READ, Action.ADMIN]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "admin_reads", "", id="admin-reads"),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_changes", "", id="admin-sets"),
        pytest.param(
            Action.ADMIN,
            {"operation": "clear"},
            Effect.ALLOW,
            "admin_changes",
            "",
            id="admin-clears",
        ),
        pytest.param(
            Action.READ,
            {"platform_admin": False},
            Effect.ALLOW,
            "staff_reads",
            "",
            id="support-reads",
        ),
        pytest.param(
            Action.READ,
            {"platform_admin": False, "operation": "read"},
            Effect.ALLOW,
            "staff_reads",
            "",
            id="support-reads-whatever-the-operation-label",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-set",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False, "operation": "clear"},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-clear",
        ),
        pytest.param(
            Action.READ,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-set",
        ),
    ],
)
def test_platform_org_storage_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
) -> None:
    _expect(
        authorize(USER, action, ORG_STORAGE, {**ORG_STORAGE_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="platform.org_storage",
        message=message,
    )


@pytest.mark.parametrize("action", ORG_STORAGE_ACTIONS, ids=lambda a: a.value)
def test_platform_org_storage_admin_flag_alone_is_not_staff(action: Action) -> None:
    _expect(
        authorize(USER, action, ORG_STORAGE, {**ORG_STORAGE_ALLOW, "platform_staff": False}),
        effect=Effect.DENY,
        reason="platform_staff_required",
        policy="platform.org_storage",
        message="Platform staff role required",
    )


#: A support caller's facts: staff, not admin. The READ branch allows on these,
#: so the attribute checks below must still deny it when one is missing.
ORG_STORAGE_SUPPORT_READ: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": False,
    "operation": "read",
}


@pytest.mark.parametrize("key", sorted(ORG_STORAGE_SUPPORT_READ))
def test_platform_org_storage_support_read_missing_attribute_denies(key: str) -> None:
    attrs = {k: v for k, v in ORG_STORAGE_SUPPORT_READ.items() if k != key}
    assert authorize(USER, Action.READ, ORG_STORAGE, attrs).effect is Effect.DENY


@pytest.mark.parametrize("key", sorted(ORG_STORAGE_WRONG_TYPED))
def test_platform_org_storage_support_read_wrong_attribute_type_denies(key: str) -> None:
    attrs = {**ORG_STORAGE_SUPPORT_READ, key: ORG_STORAGE_WRONG_TYPED[key]}
    assert authorize(USER, Action.READ, ORG_STORAGE, attrs).effect is Effect.DENY


@pytest.mark.parametrize(
    "other", [a for a in Action if a not in ORG_STORAGE_ACTIONS], ids=lambda a: a.value
)
def test_platform_org_storage_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, ORG_STORAGE, ORG_STORAGE_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="platform.org_storage",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", ORG_STORAGE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(ORG_STORAGE_ALLOW))
def test_platform_org_storage_missing_attribute_denies(action: Action, key: str) -> None:
    attrs = {k: v for k, v in ORG_STORAGE_ALLOW.items() if k != key}
    assert authorize(USER, action, ORG_STORAGE, attrs).effect is Effect.DENY


@pytest.mark.parametrize("action", ORG_STORAGE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(ORG_STORAGE_WRONG_TYPED))
def test_platform_org_storage_wrong_attribute_type_denies(action: Action, key: str) -> None:
    attrs = {**ORG_STORAGE_ALLOW, key: ORG_STORAGE_WRONG_TYPED[key]}
    assert authorize(USER, action, ORG_STORAGE, attrs).effect is Effect.DENY


# --------------------------------------------------------------------------- #
# platform.org_live_editing and platform.org_sso_domains — a setting the
# platform holds about one tenant: staff read it, only a platform admin
# changes it. The two share one rule, so one table runs against each.
# --------------------------------------------------------------------------- #

ORG_LIVE = Resource(ResourceType.PLATFORM_ORG_LIVE_EDITING, id=str(ORG))

#: Each policy that follows the rule, by the resource it decides.
STAFF_READS_ADMIN_CHANGES = [
    pytest.param(ORG_LIVE, "platform.org_live_editing", id="live_editing"),
    pytest.param(
        Resource(ResourceType.PLATFORM_ORG_SSO_DOMAINS, id=str(ORG)),
        "platform.org_sso_domains",
        id="sso_domains",
    ),
]

ORG_LIVE_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "operation": "set",
}
ORG_LIVE_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "operation": ["set"],
}
#: A support caller's facts: staff, not admin. The READ branch allows on
#: these, so a missing or mistyped one must still deny it.
ORG_LIVE_SUPPORT_READ: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": False,
    "operation": "read",
}
ORG_LIVE_ACTIONS = [Action.READ, Action.ADMIN]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "admin_reads", "", id="admin-reads"),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_changes", "", id="admin-sets"),
        pytest.param(
            Action.ADMIN,
            {"operation": "clear"},
            Effect.ALLOW,
            "admin_changes",
            "",
            id="admin-clears",
        ),
        pytest.param(
            Action.READ,
            {"platform_admin": False},
            Effect.ALLOW,
            "staff_reads",
            "",
            id="support-reads",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-set",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False, "operation": "clear"},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-clear",
        ),
        pytest.param(
            Action.READ,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-set",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="the-admin-flag-alone-is-not-staff",
        ),
    ],
)
@pytest.mark.parametrize(("resource", "policy"), STAFF_READS_ADMIN_CHANGES)
def test_platform_org_live_editing_table(
    resource: Resource,
    policy: str,
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
) -> None:
    _expect(
        authorize(USER, action, resource, {**ORG_LIVE_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy=policy,
        message=message,
    )


@pytest.mark.parametrize(
    "other", [a for a in Action if a not in ORG_LIVE_ACTIONS], ids=lambda a: a.value
)
@pytest.mark.parametrize(("resource", "policy"), STAFF_READS_ADMIN_CHANGES)
def test_platform_org_live_editing_refuses_every_action_but_read_and_admin(
    resource: Resource, policy: str, other: Action
) -> None:
    _expect(
        authorize(USER, other, resource, ORG_LIVE_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy=policy,
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        pytest.param(Action.READ, ORG_LIVE_SUPPORT_READ, id="support-read"),
        pytest.param(Action.ADMIN, ORG_LIVE_ALLOW, id="admin-set"),
    ],
)
@pytest.mark.parametrize("key", sorted(ORG_LIVE_ALLOW))
@pytest.mark.parametrize(("resource", "policy"), STAFF_READS_ADMIN_CHANGES)
def test_platform_org_live_editing_missing_attribute_denies(
    resource: Resource, policy: str, action: Action, allowed: dict[str, object], key: str
) -> None:
    assert authorize(USER, action, resource, allowed).effect is Effect.ALLOW
    attrs = {k: v for k, v in allowed.items() if k != key}
    denied = authorize(USER, action, resource, attrs)
    assert denied.effect is Effect.DENY
    assert denied.policy == policy


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        pytest.param(Action.READ, ORG_LIVE_SUPPORT_READ, id="support-read"),
        pytest.param(Action.ADMIN, ORG_LIVE_ALLOW, id="admin-set"),
    ],
)
@pytest.mark.parametrize("key", sorted(ORG_LIVE_WRONG_TYPED))
@pytest.mark.parametrize(("resource", "policy"), STAFF_READS_ADMIN_CHANGES)
def test_platform_org_live_editing_wrong_attribute_type_denies(
    resource: Resource, policy: str, action: Action, allowed: dict[str, object], key: str
) -> None:
    attrs = {**allowed, key: ORG_LIVE_WRONG_TYPED[key]}
    assert authorize(USER, action, resource, attrs).effect is Effect.DENY


# --------------------------------------------------------------------------- #
# platform.billing — Alkera's own money, moved by hand
# --------------------------------------------------------------------------- #

MONEY = Resource(ResourceType.PLATFORM_BILLING, id=str(TEAM))

MONEY_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "operation": platform_billing.Operation.GRANT,
}
MONEY_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "operation": ["grant"],
}


@pytest.mark.parametrize("operation", list(platform_billing.Operation), ids=lambda op: op.value)
def test_platform_billing_admin_may_make_every_money_write(
    operation: platform_billing.Operation,
) -> None:
    _expect(
        authorize(USER, Action.ADMIN, MONEY, {**MONEY_ALLOW, "operation": operation}),
        effect=Effect.ALLOW,
        reason="admin_moves_money",
        policy="platform.billing",
        message="",
    )


@pytest.mark.parametrize(
    ("overrides", "reason", "message"),
    [
        pytest.param(
            {"platform_admin": False},
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not",
        ),
        pytest.param(
            {"platform_staff": False, "platform_admin": False},
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot",
        ),
        pytest.param(
            {"platform_staff": False},
            "platform_staff_required",
            "Platform staff role required",
            id="admin-flag-alone-is-not-staff",
        ),
    ],
)
@pytest.mark.parametrize("operation", list(platform_billing.Operation), ids=lambda op: op.value)
def test_platform_billing_refuses_everyone_but_the_admin(
    operation: platform_billing.Operation,
    overrides: dict[str, object],
    reason: str,
    message: str,
) -> None:
    """Every money write, refused for support and for a tenant alike; the two
    refusals read differently so the decision row says which floor was missed."""
    _expect(
        authorize(USER, Action.ADMIN, MONEY, {**MONEY_ALLOW, "operation": operation, **overrides}),
        effect=Effect.DENY,
        reason=reason,
        policy="platform.billing",
        message=message,
    )


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("refund", id="a-verb-nobody-registered"),
        pytest.param("", id="empty"),
        pytest.param("GRANT", id="wrong-case"),
        pytest.param("grant ", id="trailing-space"),
    ],
)
def test_platform_billing_refuses_an_operation_it_was_not_told_about(operation: str) -> None:
    """The operation is a closed vocabulary: a write nobody registered denies
    even for the admin, so a new money path cannot slip in under an unsearchable
    name. Registering it in :class:`Operation` is what admits it."""
    _expect(
        authorize(USER, Action.ADMIN, MONEY, {**MONEY_ALLOW, "operation": operation}),
        effect=Effect.DENY,
        reason="unknown_operation",
        policy="platform.billing",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    "other", [a for a in Action if a is not Action.ADMIN], ids=lambda a: a.value
)
def test_platform_billing_refuses_every_action_but_admin(other: Action) -> None:
    """Reads never reach this policy — staff read on the router floor — so a
    READ that does arrive is a caller bug and denies like any other verb."""
    _expect(
        authorize(USER, other, MONEY, MONEY_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="platform.billing",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(MONEY_ALLOW))
def test_platform_billing_missing_attribute_denies(key: str) -> None:
    attrs = {k: v for k, v in MONEY_ALLOW.items() if k != key}
    _expect(
        authorize(USER, Action.ADMIN, MONEY, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="platform.billing",
        message="Not allowed",
    )


@pytest.mark.parametrize("key", sorted(MONEY_WRONG_TYPED))
def test_platform_billing_wrong_attribute_type_denies(key: str) -> None:
    attrs = {**MONEY_ALLOW, key: MONEY_WRONG_TYPED[key]}
    _expect(
        authorize(USER, Action.ADMIN, MONEY, attrs),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="platform.billing",
        message="Not allowed",
    )


def test_platform_billing_addressed_with_an_org_is_a_tenancy_miss() -> None:
    """The routes address the resource with no org on purpose: it is the
    platform's money. Naming a foreign org re-arms the engine's tenancy floor,
    so the no-org form is a deliberate choice, not a hole."""
    foreign = Resource(ResourceType.PLATFORM_BILLING, id=str(TEAM), org_id=OTHER_ORG)
    _expect(
        authorize(USER, Action.ADMIN, foreign, MONEY_ALLOW),
        effect=Effect.DENY,
        reason="cross_org",
        policy="platform.billing",
        message="Not found",
        as_not_found=True,
    )


def test_platform_billing_audits_the_operation_by_its_value() -> None:
    """The decision row carries the operation as its plain string, so it can be
    searched for without knowing the enum."""
    audited = audited_attrs(MONEY_ALLOW, platform_billing.AUDITED)
    assert audited == {"operation": "grant", "platform_admin": True, "platform_staff": True}
    assert type(audited["operation"]) is str


# --------------------------------------------------------------------------- #
# platform.org_slack — whose org the deployment's Slack workspace serves
# --------------------------------------------------------------------------- #

ORG_SLACK = Resource(ResourceType.PLATFORM_ORG_SLACK, id=str(ORG))

ORG_SLACK_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "operation": "connect",
}
ORG_SLACK_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "operation": ["connect"],
}
ORG_SLACK_ACTIONS = [Action.READ, Action.ADMIN]


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "message"),
    [
        pytest.param(Action.READ, {}, Effect.ALLOW, "admin_reads", "", id="admin-reads"),
        pytest.param(Action.ADMIN, {}, Effect.ALLOW, "admin_changes", "", id="admin-connects"),
        pytest.param(
            Action.ADMIN,
            {"operation": "disconnect"},
            Effect.ALLOW,
            "admin_changes",
            "",
            id="admin-disconnects",
        ),
        pytest.param(
            Action.READ,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            id="support-may-not-connect",
        ),
        pytest.param(
            Action.READ,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-read",
        ),
        pytest.param(
            Action.ADMIN,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            id="a-tenant-cannot-connect",
        ),
    ],
)
def test_platform_org_slack_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
) -> None:
    _expect(
        authorize(USER, action, ORG_SLACK, {**ORG_SLACK_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="platform.org_slack",
        message=message,
    )


def test_platform_org_slack_admin_flag_alone_is_not_staff() -> None:
    """An org admin who somehow carried `platform_admin: True` is still not
    staff, and the refusal must say so rather than letting the second check
    decide on a fact the first one never established."""
    _expect(
        authorize(USER, Action.ADMIN, ORG_SLACK, {**ORG_SLACK_ALLOW, "platform_staff": False}),
        effect=Effect.DENY,
        reason="platform_staff_required",
        policy="platform.org_slack",
        message="Platform staff role required",
    )


@pytest.mark.parametrize(
    "other", [a for a in Action if a not in ORG_SLACK_ACTIONS], ids=lambda a: a.value
)
def test_platform_org_slack_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, ORG_SLACK, ORG_SLACK_ALLOW),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="platform.org_slack",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", ORG_SLACK_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(ORG_SLACK_ALLOW))
def test_platform_org_slack_missing_attribute_denies(action: Action, key: str) -> None:
    attrs = {k: v for k, v in ORG_SLACK_ALLOW.items() if k != key}
    assert authorize(USER, action, ORG_SLACK, attrs).effect is Effect.DENY


@pytest.mark.parametrize("action", ORG_SLACK_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(ORG_SLACK_WRONG_TYPED))
def test_platform_org_slack_wrong_attribute_type_denies(action: Action, key: str) -> None:
    attrs = {**ORG_SLACK_ALLOW, key: ORG_SLACK_WRONG_TYPED[key]}
    assert authorize(USER, action, ORG_SLACK, attrs).effect is Effect.DENY


# ---------------------------------------------------------------------------
# team_membership.role_write — a role written on a team for someone descent reaches
# ---------------------------------------------------------------------------

_DESCENT_ADMIN: dict[str, object] = {"descent_role": TeamRole.ADMIN, "descent_from": "Alkera Dev"}
_FIXED = "Admin by descent from Alkera Dev; change their role there"
_OUTRANKS = (
    "Already an admin of this team by descent from Alkera Dev; a member row here cannot lower that"
)


@pytest.mark.parametrize(
    ("action", "overrides", "effect", "reason", "as_not_found", "message"),
    [
        pytest.param(
            Action.WRITE, {}, Effect.ALLOW, "no_descent", False, "", id="change-nothing-reaches"
        ),
        pytest.param(
            Action.CREATE, {}, Effect.ALLOW, "no_descent", False, "", id="add-nothing-reaches"
        ),
        pytest.param(
            Action.CREATE,
            {**_DESCENT_ADMIN, "requested_role": TeamRole.ADMIN},
            Effect.ALLOW,
            "direct_admin_added",
            False,
            "",
            id="add-a-descent-admin-as-a-direct-admin",
        ),
        pytest.param(
            Action.CREATE,
            {**_DESCENT_ADMIN, "requested_role": TeamRole.MEMBER},
            Effect.DENY,
            "descent_outranks_request",
            False,
            _OUTRANKS,
            id="add-a-descent-admin-as-a-member",
        ),
        pytest.param(
            Action.WRITE,
            {**_DESCENT_ADMIN, "requested_role": TeamRole.MEMBER},
            Effect.DENY,
            "role_fixed_by_descent",
            False,
            _FIXED,
            id="demote-a-descent-admin",
        ),
        pytest.param(
            Action.WRITE,
            {**_DESCENT_ADMIN, "requested_role": TeamRole.ADMIN},
            Effect.DENY,
            "role_fixed_by_descent",
            False,
            _FIXED,
            id="restate-a-descent-admin",
        ),
        pytest.param(
            Action.WRITE,
            {**_DESCENT_ADMIN, "descent_from": None, "requested_role": TeamRole.MEMBER},
            Effect.DENY,
            "role_fixed_by_descent",
            False,
            "Admin by descent from a team above; change their role there",
            id="an-unnamed-source-still-locks",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.MEMBER})},
            Effect.DENY,
            "team_admin_required",
            False,
            "team admin role required",
            id="a-member-of-the-team-writes-nothing",
        ),
        pytest.param(
            Action.WRITE,
            {"roles": frozenset({Role.OWNER})},
            Effect.ALLOW,
            "no_descent",
            False,
            "",
            id="owner-implies-admin",
        ),
        pytest.param(
            Action.WRITE,
            {"in_org": False},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            id="foreign-team-is-a-not-found",
        ),
        pytest.param(
            Action.WRITE,
            {"in_org": False, **_DESCENT_ADMIN},
            Effect.DENY,
            "team_not_in_org",
            True,
            "Team not found",
            id="foreign-team-wins-over-descent",
        ),
    ],
)
def test_team_membership_role_write_table(
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    as_not_found: bool,
    message: str,
) -> None:
    decision = authorize(USER, action, MEMBERSHIP_RESOURCE, {**MEMBERSHIP_ALLOW, **overrides})
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="team_membership.role_write",
        message=message,
        as_not_found=as_not_found,
        error_code=None,
    )


@pytest.mark.parametrize("action", sorted(membership_policy.SUPPORTED), ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(MEMBERSHIP_ALLOW))
def test_team_membership_missing_attribute_denies(action: Action, key: str) -> None:
    """Every fact the write consults, removed: a deny naming the fact, never an
    allow from the branch that happened not to read it — `descent_role` and
    `descent_from` included, whose None is a value and whose absence is not."""
    _expect(
        authorize(USER, action, MEMBERSHIP_RESOURCE, _without(MEMBERSHIP_ALLOW, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="team_membership.role_write",
        message="Not allowed",
    )


@pytest.mark.parametrize("action", sorted(membership_policy.SUPPORTED), ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(MEMBERSHIP_WRONG_TYPED))
def test_team_membership_wrong_attribute_type_denies(action: Action, key: str) -> None:
    """A role name as a bare string is not a role: `"admin"` in `descent_role`
    must not read as admin-by-descent, nor `"member"` as a requested member."""
    _expect(
        authorize(
            USER,
            action,
            MEMBERSHIP_RESOURCE,
            {**MEMBERSHIP_ALLOW, key: MEMBERSHIP_WRONG_TYPED[key]},
        ),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="team_membership.role_write",
        message="Not allowed",
    )


# --------------------------------------------------------------------------
# chat_template.access — a saved chat, handed round
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "allowed", "reason"),
    [
        pytest.param(USER, Action.READ, {}, True, "owner_reads", id="owner-reads"),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"is_org_admin": True},
            True,
            "org_admin_reads",
            id="org-admin-reads",
        ),
        pytest.param(
            OTHER_USER, Action.READ, {}, True, "audience_reads", id="the-org-audience-reads"
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"visibility_scope": f"team:{TEAM}", "team_id": str(TEAM)},
            True,
            "audience_reads",
            id="a-member-of-the-addressed-team-reads",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {
                "visibility_scope": f"team:{OTHER_TEAM}",
                "team_id": str(OTHER_TEAM),
                "shared_role": "reader",
            },
            True,
            "shared_reads",
            id="a-rung-reads-from-outside-the-audience",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"visibility_scope": f"team:{OTHER_TEAM}", "team_id": str(OTHER_TEAM)},
            False,
            "not_in_audience",
            id="outside-the-audience-with-no-rung",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {
                "visibility_scope": f"team:{OTHER_TEAM}",
                "team_id": str(OTHER_TEAM),
                "shared_role": "a_rung_the_ladder_has_never_heard_of",
            },
            False,
            "not_in_audience",
            id="an-unknown-rung-is-not-a-weak-rung",
        ),
        pytest.param(USER, Action.WRITE, {}, True, "writer_may_edit", id="owner-edits"),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"is_org_admin": True},
            True,
            "writer_may_edit",
            id="org-admin-edits",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"shared_role": "writer"},
            True,
            "writer_may_edit",
            id="the-write-rung-edits",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"shared_role": "manager"},
            True,
            "writer_may_edit",
            id="a-rung-above-write-edits",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {},
            False,
            "write_rung_required",
            id="the-audience-alone-does-not-edit",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"shared_role": "commenter"},
            False,
            "write_rung_required",
            id="a-rung-below-write-does-not-edit",
        ),
        pytest.param(USER, Action.DELETE, {}, True, "owner_deletes", id="owner-deletes"),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True},
            True,
            "owner_deletes",
            id="org-admin-deletes",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"shared_role": "owner"},
            False,
            "owner_or_org_admin_required",
            id="even-the-top-rung-does-not-delete",
        ),
        pytest.param(
            USER, Action.CREATE, {}, True, "org_member_creates", id="creating-into-the-org"
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {"visibility_scope": "private"},
            True,
            "org_member_creates",
            id="saving-a-private-template-the-author-is-its-audience",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"visibility_scope": "private"},
            True,
            "owner_reads",
            id="the-owner-reads-their-private-template",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"visibility_scope": "private"},
            False,
            "not_in_audience",
            id="a-colleague-does-not-read-a-private-template",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"visibility_scope": "private", "is_org_admin": True},
            True,
            "org_admin_reads",
            id="an-org-admin-reads-a-private-template",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"visibility_scope": "private", "shared_role": "reader"},
            True,
            "shared_reads",
            id="a-rung-on-the-folder-reads-a-private-template",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"visibility_scope": "private", "shared_role": "reader"},
            False,
            "write_rung_required",
            id="a-rung-below-write-does-not-edit-a-private-template",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {"visibility_scope": f"team:{TEAM}", "team_id": str(TEAM)},
            True,
            "org_member_creates",
            id="creating-into-a-team-the-author-is-on",
        ),
        pytest.param(
            OTHER_USER,
            Action.CREATE,
            {"visibility_scope": f"team:{OTHER_TEAM}", "team_id": str(OTHER_TEAM)},
            False,
            "not_in_audience",
            id="creating-into-a-team-the-author-is-not-on",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"in_org": False},
            False,
            "not_in_org",
            id="not-in-the-org",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"roles": frozenset({Role.VIEWER})},
            False,
            "org_member_required",
            id="a-viewer-is-not-a-member",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"visibility_scope": f"team:{TEAM}", "team_id": ""},
            False,
            "scope_team_mismatch",
            id="the-scope-and-the-team-column-disagree",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"visibility_scope": "a_scope_nobody_writes", "team_id": ""},
            False,
            "scope_team_mismatch",
            id="an-unparseable-scope-is-no-audience",
        ),
    ],
)
def test_chat_template_branch_table(
    ctx: ActingContext, action: Action, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    decision = authorize(ctx, action, CHAT_TEMPLATE, _template_attrs(**attrs))
    assert decision.allowed is allowed, decision
    assert decision.reason == reason, decision
    assert decision.policy == "chat_template.access", decision


def test_chat_template_a_reader_is_refused_the_edit_visibly() -> None:
    """The acceptance the sharing rule asks for: someone the template was shared
    with at a reading rung sees it and is told plainly that changing it takes
    more. Hiding it would be a lie they can disprove by reading it."""
    _expect(
        authorize(OTHER_USER, Action.WRITE, CHAT_TEMPLATE, _template_attrs(shared_role="reader")),
        effect=Effect.DENY,
        reason="write_rung_required",
        policy="chat_template.access",
        message="You need edit access to this template to change it.",
        error_code="chat_template.write_rung_required",
    )


def test_chat_template_an_outsider_is_told_it_does_not_exist() -> None:
    """The other half: no audience and no rung is the opaque not-found another
    org's member gets, so an id that exists cannot be told from one that does
    not by the wording of its refusal."""
    for action in sorted(chat_template.SUPPORTED, key=lambda a: a.value):
        _expect(
            authorize(
                OTHER_USER,
                action,
                CHAT_TEMPLATE,
                _template_attrs(visibility_scope=f"team:{OTHER_TEAM}", team_id=str(OTHER_TEAM)),
            ),
            effect=Effect.DENY,
            reason="not_in_audience",
            policy="chat_template.access",
            message="Not found",
            as_not_found=True,
        )


@pytest.mark.parametrize(
    "action",
    [
        pytest.param(Action.CREATE, id="create"),
        pytest.param(Action.WRITE, id="write"),
        pytest.param(Action.DELETE, id="delete"),
    ],
)
def test_chat_template_every_mutation_takes_a_verified_email(action: Action) -> None:
    _expect(
        authorize(USER, action, CHAT_TEMPLATE, _template_attrs(email_verified=False)),
        effect=Effect.DENY,
        reason="email_verification_required",
        policy="chat_template.access",
        message="Verify your email address to perform this action.",
        error_code="email_verification_required",
    )


def test_chat_template_reading_it_does_not_take_a_verified_email() -> None:
    """The negative twin: an unverified invitee still sees what they were shown.
    Gating the read would make the email gate a share revocation."""
    assert (
        authorize(USER, Action.READ, CHAT_TEMPLATE, _template_attrs(email_verified=False)).allowed
        is True
    )


def test_chat_template_the_shared_rung_is_required_even_where_no_branch_reads_it() -> None:
    """A route that forgets to resolve the rung is denied, not admitted by the
    owner branch that happened not to need it."""
    decision = authorize(USER, Action.READ, CHAT_TEMPLATE, _without(TEMPLATE_ALLOW, "shared_role"))
    assert decision.allowed is False, decision
    assert decision.reason == "missing_attribute:shared_role", decision


# --------------------------------------------------------------------------
# workspace.access: chats that share one file tree
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "allowed", "reason"),
    [
        pytest.param(USER, Action.READ, {}, True, "owner_reads", id="owner-reads"),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"shared_role": "reader"},
            True,
            "shared_reads",
            id="a-rung-on-the-folder-reads",
        ),
        pytest.param(OTHER_USER, Action.READ, {}, False, "not_in_audience", id="no-rung-no-read"),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"shared_role": "a_rung_the_ladder_has_never_heard_of"},
            False,
            "not_in_audience",
            id="an-unknown-rung-is-not-a-weak-rung",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"is_org_admin": True},
            False,
            "not_in_audience",
            id="an-org-admin-does-not-read-a-private-workspace-by-default",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"is_org_admin": True, "admin_reads_private": True},
            True,
            "org_admin_reads_private",
            id="an-org-admin-reads-where-the-deployment-says-so",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"admin_reads_private": True},
            False,
            "not_in_audience",
            id="the-admin-setting-admits-only-admins",
        ),
        pytest.param(USER, Action.RENAME, {}, True, "writer_may_rename", id="owner-renames"),
        pytest.param(
            OTHER_USER,
            Action.RENAME,
            {"shared_role": "writer"},
            True,
            "writer_may_rename",
            id="the-write-rung-renames",
        ),
        pytest.param(
            OTHER_USER,
            Action.RENAME,
            {"shared_role": "commenter"},
            False,
            "rename_rung_required",
            id="a-rung-below-write-does-not-rename",
        ),
        pytest.param(
            OTHER_USER,
            Action.RENAME,
            {"is_org_admin": True, "admin_reads_private": True},
            False,
            "rename_rung_required",
            id="an-admin-who-may-read-may-not-rename",
        ),
        pytest.param(USER, Action.WRITE, {}, True, "writer_may_add_chat", id="owner-adds-a-chat"),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"shared_role": "manager"},
            True,
            "writer_may_add_chat",
            id="a-rung-above-write-adds-a-chat",
        ),
        pytest.param(
            OTHER_USER,
            Action.WRITE,
            {"shared_role": "reader"},
            False,
            "write_rung_required",
            id="a-viewer-does-not-add-a-chat",
        ),
        pytest.param(USER, Action.DELETE, {}, True, "owner_deletes", id="owner-deletes"),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True, "admin_reads_private": True},
            True,
            "org_admin_deletes",
            id="an-org-admin-who-may-read-it-deletes",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True},
            False,
            "not_in_audience",
            id="an-org-admin-who-cannot-read-it-is-told-it-does-not-exist",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.DELETE,
            {},
            False,
            "agent_may_not_delete",
            id="an-agent-never-deletes-a-workspace",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"shared_role": "owner"},
            False,
            "owner_or_org_admin_required",
            id="even-the-top-rung-does-not-delete",
        ),
        pytest.param(
            USER,
            Action.DELETE,
            {"is_main": True},
            False,
            "main_not_deletable",
            id="the-owner-does-not-delete-their-main-workspace",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_main": True, "is_org_admin": True, "admin_reads_private": True},
            False,
            "main_not_deletable",
            id="nor-does-an-org-admin",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_main": True},
            False,
            "not_in_audience",
            id="a-stranger-is-not-told-a-main-workspace-exists",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True, "owner_departed": True},
            True,
            "org_admin_offboards",
            id="an-org-admin-ends-a-departed-members-workspace-unread",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True, "owner_departed": True, "is_main": True},
            True,
            "org_admin_offboards",
            id="their-main-workspace-included",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"owner_departed": True},
            False,
            "not_in_audience",
            id="a-member-gains-nothing-from-a-departed-owner",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"is_org_admin": True, "owner_departed": True},
            False,
            "not_in_audience",
            id="offboarding-deletes-it-and-never-reads-it",
        ),
        pytest.param(USER, Action.CREATE, {}, True, "org_member_creates", id="a-member-creates"),
        pytest.param(
            OWNER_AGENT,
            Action.CREATE,
            {},
            False,
            "agent_may_not_create_project",
            id="an-agent-does-not-start-a-project",
        ),
        pytest.param(
            OWNER_AGENT,
            Action.CREATE,
            {"is_main": True},
            True,
            "org_member_creates",
            id="an-agent-may-have-its-users-main-workspace-made",
        ),
        pytest.param(
            USER,
            Action.CREATE,
            {"visibility_scope": f"team:{OTHER_TEAM}", "team_id": str(OTHER_TEAM)},
            False,
            "not_in_audience",
            id="creating-into-a-team-the-author-is-not-on",
        ),
        pytest.param(USER, Action.READ, {"in_org": False}, False, "not_in_org", id="not-in-org"),
        pytest.param(
            USER,
            Action.READ,
            {"roles": frozenset({Role.VIEWER})},
            False,
            "org_member_required",
            id="a-viewer-is-not-a-member",
        ),
        pytest.param(
            USER,
            Action.READ,
            {"visibility_scope": f"team:{TEAM}", "team_id": ""},
            False,
            "scope_team_mismatch",
            id="the-scope-and-the-team-column-disagree",
        ),
    ],
)
def test_workspace_branch_table(
    ctx: ActingContext, action: Action, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    decision = authorize(ctx, action, WORKSPACE, _workspace_attrs(**attrs))
    assert decision.allowed is allowed, decision
    assert decision.reason == reason, decision
    assert decision.policy == "workspace.access", decision


@pytest.mark.parametrize("action", sorted(workspace_policy.SUPPORTED, key=lambda a: a.value))
def test_workspace_a_box_is_never_admitted(action: Action) -> None:
    """A box serves chats and is admitted by a chat's own policy; a workspace
    is a person's, and a machine is told it does not exist whatever it asks."""
    machine = ActingContext.for_machine(
        machine_id=UUID(MACHINE_ID),
        credential_id=TOKEN_ID,
        org_id=ORG,
        label="box",
        served_org_ids=frozenset({ORG}),
    )
    _expect(
        authorize(machine, action, WORKSPACE, _workspace_attrs()),
        effect=Effect.DENY,
        reason="machine_action_not_allowed",
        policy="workspace.access",
        message="Not found",
        as_not_found=True,
    )


def _box(machine_id: str = MACHINE_ID, *, serves: UUID = ORG) -> ActingContext:
    return ActingContext.for_machine(
        machine_id=UUID(machine_id),
        credential_id=TOKEN_ID,
        org_id=serves,
        label="box",
        served_org_ids=frozenset({serves}),
    )


@pytest.mark.parametrize(
    "action", sorted(workspace_policy.CONNECTION_ACTIONS, key=lambda a: a.value)
)
def test_workspace_the_box_holding_it_lists_and_leases_its_connections(action: Action) -> None:
    decision = authorize(_box(), action, WORKSPACE, _workspace_attrs(bound_machine_id=MACHINE_ID))
    assert decision.allowed, decision
    assert (decision.policy, decision.reason) == ("workspace.access", "machine_holds_workspace")


@pytest.mark.parametrize(
    "action",
    sorted(workspace_policy.SUPPORTED - workspace_policy.CONNECTION_ACTIONS, key=lambda a: a.value),
)
def test_workspace_holding_it_gives_a_box_nothing_else(action: Action) -> None:
    _expect(
        authorize(_box(), action, WORKSPACE, _workspace_attrs(bound_machine_id=MACHINE_ID)),
        effect=Effect.DENY,
        reason="machine_action_not_allowed",
        policy="workspace.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    ("ctx", "bound", "reason"),
    [
        pytest.param(
            _box(OTHER_MACHINE_ID), MACHINE_ID, "machine_action_not_allowed", id="another-box"
        ),
        pytest.param(_box(), "", "machine_action_not_allowed", id="held-by-no-box"),
        pytest.param(USER, MACHINE_ID, "machine_required", id="its-owner"),
        pytest.param(MACHINE_AGENT, MACHINE_ID, "machine_required", id="an-agent-naming-the-box"),
    ],
)
def test_workspace_connections_are_the_holding_boxs_alone(
    ctx: ActingContext, bound: str, reason: str
) -> None:
    _expect(
        authorize(
            ctx, Action.LIST_CONNECTIONS, WORKSPACE, _workspace_attrs(bound_machine_id=bound)
        ),
        effect=Effect.DENY,
        reason=reason,
        policy="workspace.access",
        message="Not found",
        as_not_found=True,
    )


def test_workspace_a_box_of_another_org_is_refused_at_the_tenancy_floor() -> None:
    decision = authorize(
        _box(serves=OTHER_ORG),
        Action.LIST_CONNECTIONS,
        WORKSPACE,
        _workspace_attrs(bound_machine_id=MACHINE_ID),
    )
    assert not decision.allowed
    assert decision.as_not_found


@pytest.mark.parametrize(
    ("action", "role", "message", "code"),
    [
        pytest.param(
            Action.RENAME,
            "reader",
            workspace_policy.RENAME_DENIED_MESSAGE,
            workspace_policy.RENAME_DENIED_CODE,
            id="rename",
        ),
        pytest.param(
            Action.WRITE,
            "commenter",
            workspace_policy.WRITE_DENIED_MESSAGE,
            workspace_policy.WRITE_DENIED_CODE,
            id="add-a-chat",
        ),
    ],
)
def test_workspace_a_reader_is_refused_a_change_visibly(
    action: Action, role: str, message: str, code: str
) -> None:
    """Someone who can see the workspace is told plainly that changing it takes
    more; hiding it would be a lie they can disprove by reading it."""
    _expect(
        authorize(OTHER_USER, action, WORKSPACE, _workspace_attrs(shared_role=role)),
        effect=Effect.DENY,
        reason="rename_rung_required" if action is Action.RENAME else "write_rung_required",
        policy="workspace.access",
        message=message,
        error_code=code,
    )


@pytest.mark.parametrize(
    "action", [Action.CREATE, Action.RENAME, Action.WRITE, Action.DELETE], ids=lambda a: a.value
)
def test_workspace_every_change_takes_a_verified_email(action: Action) -> None:
    _expect(
        authorize(USER, action, WORKSPACE, _workspace_attrs(email_verified=False)),
        effect=Effect.DENY,
        reason="email_verification_required",
        policy="workspace.access",
        message="Verify your email address to perform this action.",
        error_code="email_verification_required",
    )


def test_workspace_reading_it_does_not_take_a_verified_email() -> None:
    assert authorize(USER, Action.READ, WORKSPACE, _workspace_attrs(email_verified=False)).allowed


def test_workspace_a_main_workspace_refusal_names_why() -> None:
    _expect(
        authorize(USER, Action.DELETE, WORKSPACE, _workspace_attrs(is_main=True)),
        effect=Effect.DENY,
        reason="main_not_deletable",
        policy="workspace.access",
        message=workspace_policy.MAIN_DELETE_DENIED_MESSAGE,
        error_code=workspace_policy.MAIN_DELETE_DENIED_CODE,
    )


# --------------------------------------------------------------------------
# workspace_machine.move: changing the machine a workspace runs on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx", "attrs", "allowed", "reason"),
    [
        pytest.param(USER, {}, True, "owner_moves", id="the-owner-moves-it"),
        pytest.param(
            USER,
            {"target_kind": "default", "target_usable": False},
            True,
            "owner_moves_to_default",
            id="the-default-is-everyones",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "manager"},
            True,
            "full_access_moves",
            id="full-access-moves-it",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "owner"},
            True,
            "full_access_moves",
            id="the-owner-rung-moves-it",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "manager", "target_kind": "default"},
            True,
            "full_access_to_default",
            id="full-access-moves-it-to-the-default",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "writer"},
            False,
            "full_access_required",
            id="an-editor-does-not-move-it",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "reader"},
            False,
            "full_access_required",
            id="a-viewer-does-not-move-it",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "commenter", "target_kind": "default"},
            False,
            "full_access_required",
            id="nor-to-the-default",
        ),
        pytest.param(
            OTHER_USER,
            {"is_org_admin": True, "admin_reads_private": True},
            False,
            "full_access_required",
            id="an-org-admin-who-reads-it-does-not-move-it",
        ),
        pytest.param(
            OTHER_USER, {}, False, "not_in_audience", id="a-stranger-is-told-it-does-not-exist"
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "a_rung_the_ladder_has_never_heard_of"},
            False,
            "not_in_audience",
            id="an-unknown-rung-is-no-rung",
        ),
        pytest.param(
            USER,
            {"target_usable": False},
            False,
            "target_not_usable",
            id="a-machine-outside-the-callers-audience",
        ),
        pytest.param(
            OTHER_USER,
            {"shared_role": "manager", "target_usable": False},
            False,
            "target_not_usable",
            id="full-access-does-not-widen-the-machines-audience",
        ),
        pytest.param(
            USER,
            {"email_verified": False},
            False,
            "email_verification_required",
            id="an-unverified-owner",
        ),
        pytest.param(USER, {"in_org": False}, False, "not_in_org", id="not-in-org"),
        pytest.param(
            USER,
            {"roles": frozenset({Role.VIEWER})},
            False,
            "org_member_required",
            id="an-org-viewer-is-not-a-member",
        ),
        pytest.param(OWNER_AGENT, {}, False, "agent_may_not_move", id="an-agent-never-moves-it"),
    ],
)
def test_workspace_machine_branch_table(
    ctx: ActingContext, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    decision = authorize(ctx, Action.WRITE, WORKSPACE_MACHINE, _machine_move_attrs(**attrs))
    assert decision.allowed is allowed, decision
    assert decision.reason == reason, decision
    assert decision.policy == "workspace_machine.move", decision


@pytest.mark.parametrize(
    ("ctx", "attrs", "reason", "message", "code", "opaque"),
    [
        pytest.param(
            OTHER_USER,
            {"shared_role": "writer"},
            "full_access_required",
            workspace_machine_policy.FULL_ACCESS_MESSAGE,
            workspace_machine_policy.FULL_ACCESS_CODE,
            False,
            id="editor",
        ),
        pytest.param(
            USER,
            {"target_usable": False},
            "target_not_usable",
            workspace_machine_policy.TARGET_MESSAGE,
            workspace_machine_policy.TARGET_CODE,
            False,
            id="target",
        ),
        pytest.param(
            OTHER_USER, {}, "not_in_audience", "Not found", None, True, id="unreadable-workspace"
        ),
    ],
)
def test_workspace_machine_refusals_say_why_or_hide_the_workspace(
    ctx: ActingContext,
    attrs: dict[str, object],
    reason: str,
    message: str,
    code: str | None,
    opaque: bool,
) -> None:
    _expect(
        authorize(ctx, Action.WRITE, WORKSPACE_MACHINE, _machine_move_attrs(**attrs)),
        effect=Effect.DENY,
        reason=reason,
        policy="workspace_machine.move",
        message=message,
        as_not_found=opaque,
        error_code=code,
    )


def test_workspace_machine_a_box_is_never_admitted() -> None:
    machine = ActingContext.for_machine(
        machine_id=UUID(MACHINE_ID),
        credential_id=TOKEN_ID,
        org_id=ORG,
        label="box",
        served_org_ids=frozenset({ORG}),
    )
    _expect(
        authorize(machine, Action.WRITE, WORKSPACE_MACHINE, _machine_move_attrs()),
        effect=Effect.DENY,
        reason="machine_action_not_allowed",
        policy="workspace_machine.move",
        message="Not found",
        as_not_found=True,
    )


# --------------------------------------------------------------------------
# files.access — a copy that would outrun the source's own sharing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_attrs", "reason"),
    [
        pytest.param({"no_reshare_chain": True}, "no_reshare", id="no-reshare-on-the-chain"),
        pytest.param(
            {"read_via_conditional_grant": True}, "conditional_grant", id="read-only-on-loan"
        ),
    ],
)
def test_files_copy_refuses_a_source_whose_reach_the_copy_would_escape(
    source_attrs: dict[str, object], reason: str
) -> None:
    """The ladder handed this caller ``copy`` and they can read the source, and
    the policy still refuses: the copy lands where the source's restriction
    cannot follow it. The code is its own, because "you may read this but not
    duplicate it" is not the same refusal as "you lack the rung"."""
    _expect(
        authorize(USER, Action.COPY, FILE_NODE, _files_attrs(**source_attrs)),
        effect=Effect.DENY,
        reason=reason,
        policy="files.access",
        message="Not allowed",
        error_code="files.copy_refused",
    )


@pytest.mark.parametrize(
    "source_attrs",
    [
        pytest.param({"no_reshare_chain": True}, id="no-reshare-on-the-chain"),
        pytest.param({"read_via_conditional_grant": True}, id="read-only-on-loan"),
    ],
)
def test_files_a_copy_refusal_on_an_unreadable_source_is_still_opaque(
    source_attrs: dict[str, object],
) -> None:
    """The new code never leaks past the read gate: a caller who cannot read the
    source learns neither that it is there nor why the copy was refused."""
    _expect(
        authorize(
            USER, Action.COPY, FILE_NODE, _files_attrs(allowed_actions=frozenset(), **source_attrs)
        ),
        effect=Effect.DENY,
        reason="action_not_allowed_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize(
    "source_attrs",
    [
        pytest.param({"no_reshare_chain": True}, id="no-reshare-on-the-chain"),
        pytest.param({"read_via_conditional_grant": True}, id="read-only-on-loan"),
    ],
)
def test_files_the_two_reach_facts_refuse_the_copy_and_nothing_else(
    source_attrs: dict[str, object], action: Action
) -> None:
    """The negative twin: neither fact touches reading, writing, sharing or
    exporting. A folder marked "do not pass this on" is still a folder its
    readers work in, and a grant that expires still grants until it does."""
    decision = authorize(USER, action, FILE_NODE, _files_attrs(**source_attrs))
    assert decision.allowed is (action is not Action.COPY), decision


def test_files_a_trashed_source_outranks_both_reach_facts() -> None:
    """Branch order, pinned: the cheapest true thing about the node is what the
    decision row says, so a refusal does not vary with how the caller got there."""
    decision = authorize(
        USER,
        Action.COPY,
        FILE_NODE,
        _files_attrs(trashed=True, no_reshare_chain=True, read_via_conditional_grant=True),
    )
    assert decision.reason == "trashed", decision


# ---------------------------------------------------------------------------
# a box on its own machine credential
# ---------------------------------------------------------------------------

CREDENTIAL_ID = UUID("00000000-0000-4000-8000-0000000000c1")
#: A dedicated box: minted in ORG, assigned to OTHER_ORG, holding MACHINE_ID.
DEDICATED_BOX = ActingContext.for_machine(
    machine_id=UUID(MACHINE_ID),
    credential_id=CREDENTIAL_ID,
    org_id=ORG,
    label="box",
    served_org_ids=frozenset({OTHER_ORG}),
)
#: A pool box: minted in ORG, serving every org, holding MACHINE_ID.
POOL_BOX = ActingContext.for_machine(
    machine_id=UUID(MACHINE_ID),
    credential_id=CREDENTIAL_ID,
    org_id=ORG,
    label="box",
    serves_every_org=True,
)
#: A box that has not claimed a machine yet: its principal id is the credential's.
UNCLAIMED_BOX = ActingContext.for_machine(
    machine_id=None, credential_id=CREDENTIAL_ID, org_id=ORG, label="box"
)
THIRD_ORG = UUID("00000000-0000-4000-8000-0000000000e0")
#: The facts a route resolves for a machine: a member of nothing, verified as
#: nothing, with the chat's own binding and the machines that may speak for it.
MACHINE_CHAT: dict[str, object] = {
    **CHAT_ALLOW,
    "in_org": False,
    "roles": frozenset(),
    "team_ids": frozenset(),
    "email_verified": False,
    "owner_user_id": str(OTHER_USER_ID),
    "bound_machine_id": MACHINE_ID,
}
MACHINE_MACHINE: dict[str, object] = {
    "in_org": True,
    "roles": frozenset(),
    "email_verified": False,
    "is_owner": False,
    "lifecycle": "workspace",
    "controls": False,
}
OWN_MACHINE = Resource(ResourceType.COMPUTE_MACHINE, id=MACHINE_ID, org_id=ORG)
OTHER_MACHINE = Resource(ResourceType.COMPUTE_MACHINE, id=OTHER_MACHINE_ID, org_id=ORG)


def test_a_machine_context_never_delegates_and_only_a_machine_serves_elsewhere() -> None:
    assert DEDICATED_BOX.is_machine and DEDICATED_BOX.delegating_user is None
    assert DEDICATED_BOX.effective_user_id is None
    assert DEDICATED_BOX.subject is DEDICATED_BOX.acting_principal
    assert DEDICATED_BOX.acting_principal.credential is CredentialKind.MACHINE
    assert DEDICATED_BOX.acting_principal.credential_id == str(CREDENTIAL_ID)
    assert UNCLAIMED_BOX.acting_principal.id == str(CREDENTIAL_ID)
    with pytest.raises(ValueError, match="only a machine serves"):
        ActingContext(acting_principal=USER.acting_principal, serves_every_org=True)
    with pytest.raises(ValueError, match="only a machine serves"):
        ActingContext(acting_principal=SERVICE_CI.acting_principal, served_org_ids={OTHER_ORG})
    with pytest.raises(ValueError, match="never for a user"):
        ActingContext(
            acting_principal=DEDICATED_BOX.acting_principal,
            delegating_user=USER.acting_principal,
            delegation_chain=(USER.acting_principal, DEDICATED_BOX.acting_principal),
        )


@pytest.mark.parametrize(
    ("ctx", "org", "served"),
    [
        pytest.param(USER, ORG, True, id="user-own-org"),
        pytest.param(USER, OTHER_ORG, False, id="user-other-org"),
        pytest.param(AGENT, OTHER_ORG, False, id="agent-other-org"),
        pytest.param(PAT, OTHER_ORG, False, id="pat-other-org"),
        pytest.param(SERVICE_CI, OTHER_ORG, False, id="ci-other-org"),
        pytest.param(SERVICE_PROXY, OTHER_ORG, False, id="proxy-other-org"),
        pytest.param(DEDICATED_BOX, ORG, True, id="dedicated-operator-org"),
        pytest.param(DEDICATED_BOX, OTHER_ORG, True, id="dedicated-assigned-org"),
        pytest.param(DEDICATED_BOX, THIRD_ORG, False, id="dedicated-third-org"),
        pytest.param(POOL_BOX, THIRD_ORG, True, id="pool-any-org"),
        pytest.param(UNCLAIMED_BOX, OTHER_ORG, False, id="unclaimed-serves-nothing-else"),
    ],
)
def test_the_tenancy_floor_follows_what_the_context_serves(
    ctx: ActingContext, org: UUID, served: bool
) -> None:
    """The engine's cross-org refusal now asks ``serves``: unchanged for every
    human and token shape (their own org and nothing else), widened for a box
    to exactly the orgs it is assigned to. Serving an org is a ceiling, not a
    grant: the policy still decides — here a chat the box does not hold."""
    assert ctx.serves(org) is served
    chat = Resource(ResourceType.CHAT, id="sess-1", org_id=org)
    decision = authorize(ctx, Action.READ, chat, {**MACHINE_CHAT, "bound_machine_id": ""})
    assert decision.allowed is False
    if served:
        assert decision.reason != "cross_org"
    else:
        assert (decision.reason, decision.as_not_found) == ("cross_org", True)


@pytest.mark.parametrize("ctx", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize(
    ("action", "attrs", "allowed", "reason"),
    [
        pytest.param(Action.READ, MACHINE_CHAT, True, "machine_holds_chat", id="reads-its-chat"),
        pytest.param(
            Action.READ,
            {**MACHINE_CHAT, "bound_machine_id": OTHER_MACHINE_ID},
            False,
            "machine_does_not_hold_chat",
            id="another-boxs-chat-is-not-found",
        ),
        pytest.param(
            Action.LIST_CONNECTIONS,
            MACHINE_CHAT,
            True,
            "machine_holds_chat",
            id="reads-the-connections-of-its-chat",
        ),
        pytest.param(
            Action.LIST_CONNECTIONS,
            {**MACHINE_CHAT, "bound_machine_id": OTHER_MACHINE_ID},
            False,
            "machine_does_not_hold_chat",
            id="another-boxs-chats-connections-are-not-found",
        ),
        pytest.param(
            Action.LIST_CONNECTIONS,
            {**MACHINE_CHAT, "bound_machine_id": ""},
            False,
            "machine_does_not_hold_chat",
            id="an-unbound-chats-connections-are-not-found",
        ),
        pytest.param(
            Action.LEASE_CONNECTION_CREDENTIAL,
            MACHINE_CHAT,
            True,
            "machine_holds_chat",
            id="leases-a-credential-for-its-chat",
        ),
        pytest.param(
            Action.LEASE_CONNECTION_CREDENTIAL,
            {**MACHINE_CHAT, "bound_machine_id": OTHER_MACHINE_ID},
            False,
            "machine_does_not_hold_chat",
            id="another-boxs-chat-leases-nothing",
        ),
        pytest.param(
            Action.LEASE_CONNECTION_CREDENTIAL,
            {**MACHINE_CHAT, "bound_machine_id": ""},
            False,
            "machine_does_not_hold_chat",
            id="an-unbound-chat-leases-nothing",
        ),
        pytest.param(
            Action.READ,
            {**MACHINE_CHAT, "bound_machine_id": ""},
            False,
            "machine_does_not_hold_chat",
            id="an-unbound-chat-is-not-found",
        ),
        pytest.param(
            Action.READ,
            {
                **MACHINE_CHAT,
                "shared_role": "owner",
                "in_org": True,
                "roles": frozenset({Role.ADMIN}),
                "is_org_admin": True,
                "admin_reads_private": True,
                "bound_machine_id": "",
            },
            False,
            "machine_does_not_hold_chat",
            id="no-human-fact-admits-a-box",
        ),
        pytest.param(
            Action.WRITE,
            {**MACHINE_CHAT, "publisher_machine_ids": frozenset({MACHINE_ID})},
            True,
            "machine_holds_chat",
            id="reports-on-its-chat",
        ),
        pytest.param(
            Action.WRITE,
            {
                **MACHINE_CHAT,
                "bound_machine_id": "",
                "publisher_machine_ids": frozenset({MACHINE_ID}),
            },
            True,
            "machine_holds_chat",
            id="reports-on-an-unbound-chat-as-the-orgs-current-machine",
        ),
        pytest.param(
            Action.WRITE,
            {
                **MACHINE_CHAT,
                "bound_machine_id": OTHER_MACHINE_ID,
                "publisher_machine_ids": frozenset({MACHINE_ID, OTHER_MACHINE_ID}),
            },
            False,
            "machine_does_not_hold_chat",
            id="the-current-machine-may-not-report-on-another-boxs-chat",
        ),
        pytest.param(
            Action.WRITE,
            {
                **MACHINE_CHAT,
                "bound_machine_id": "",
                "publisher_machine_ids": frozenset({OTHER_MACHINE_ID}),
            },
            False,
            "machine_does_not_hold_chat",
            id="a-box-that-may-not-speak-for-an-unbound-chat",
        ),
        *[
            pytest.param(
                action,
                {**MACHINE_CHAT, "publisher_machine_ids": frozenset({MACHINE_ID})},
                False,
                "machine_action_not_allowed",
                id=f"{action.value}-is-a-persons-verb",
            )
            for action in (
                Action.SEND,
                Action.CREATE,
                Action.DELETE,
                Action.PROMOTE,
                Action.CLAIM,
                Action.RENAME,
            )
        ],
    ],
)
def test_chat_machine_branches(
    ctx: ActingContext, action: Action, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    """A box on its credential reaches the chats bound to it and nothing else;
    every refusal is the opaque not-found a stranger gets."""
    decision = authorize(ctx, action, CHAT, attrs)
    assert (decision.allowed, decision.reason) == (allowed, reason)
    assert decision.policy == "chat.access"
    if not allowed:
        assert (decision.as_not_found, decision.message) == (True, "Not found")


def test_an_unclaimed_box_holds_no_chat() -> None:
    decision = authorize(UNCLAIMED_BOX, Action.READ, CHAT, MACHINE_CHAT)
    assert (decision.allowed, decision.reason) == (False, "machine_does_not_hold_chat")


CONNECTION_ACTIONS = [
    pytest.param(Action.LIST_CONNECTIONS, id="list"),
    pytest.param(Action.LEASE_CONNECTION_CREDENTIAL, id="lease"),
]


@pytest.mark.parametrize("action", CONNECTION_ACTIONS)
def test_an_unclaimed_box_reaches_no_chats_connections(action: Action) -> None:
    decision = authorize(UNCLAIMED_BOX, action, CHAT, MACHINE_CHAT)
    assert (decision.allowed, decision.reason) == (False, "machine_does_not_hold_chat")


# ---------------------------------------------------------------------------
# connector.credential — a box leasing for the owner of a chat it holds
# ---------------------------------------------------------------------------

#: The facts the machine's door resolves: the person's row facts (for the
#: chat's OWNER, not the caller), plus who is acted for, the chat's binding and
#: the chat itself.
MACHINE_CONNECTOR: dict[str, object] = {
    **CONNECTOR_ALLOW,
    "for_user_id": str(OTHER_USER_ID),
    "chat_bound_machine_id": MACHINE_ID,
    "chat_id": "sess-1",
    "workspace_id": "",
}
MACHINE_CONNECTOR_WRONG_TYPED: dict[str, object] = {
    "member_entitled": "yes",
    "enabled": 1,
    "auth_mode": 7,
    "has_shared_secret": "true",
    "owner_user_id": UUID(int=7),
    "for_user_id": OTHER_USER_ID,
    "chat_bound_machine_id": UUID(MACHINE_ID),
    "chat_id": 1,
    "workspace_id": 2,
}


@pytest.mark.parametrize("ctx", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize(
    ("overrides", "effect", "reason"),
    [
        pytest.param({}, Effect.ALLOW, "machine_for_chat_owner", id="for-the-owner-of-its-chat"),
        pytest.param(
            {"chat_bound_machine_id": OTHER_MACHINE_ID},
            Effect.DENY,
            "machine_does_not_hold_chat",
            id="a-chat-bound-to-another-box",
        ),
        pytest.param(
            {"chat_bound_machine_id": ""},
            Effect.DENY,
            "machine_does_not_hold_chat",
            id="an-unbound-chat",
        ),
        pytest.param(
            {"chat_bound_machine_id": MACHINE_ID.upper()},
            Effect.DENY,
            "machine_does_not_hold_chat",
            id="the-binding-is-matched-exactly",
        ),
        pytest.param(
            {"for_user_id": ""}, Effect.DENY, "chat_has_no_owner", id="a-chat-nobody-owns"
        ),
        pytest.param(
            {"member_entitled": False}, Effect.DENY, "not_entitled", id="owner-not-entitled"
        ),
        pytest.param(
            {"owner_user_id": str(USER_ID)},
            Effect.DENY,
            "not_owner",
            id="another-persons-own-connection",
        ),
        pytest.param(
            {"owner_user_id": str(OTHER_USER_ID)},
            Effect.ALLOW,
            "machine_for_chat_owner",
            id="the-owners-own-personal-connection",
        ),
        pytest.param(
            {"owner_user_id": OTHER_USER_ID.hex},
            Effect.DENY,
            "not_owner",
            id="the-owner-match-is-exact-not-normalized",
        ),
        pytest.param({"enabled": False}, Effect.DENY, "connection_disabled", id="disabled"),
        pytest.param(
            {"auth_mode": "per_user", "has_shared_secret": False},
            Effect.ALLOW,
            "machine_for_chat_owner_grant",
            id="per-user-is-the-owners-own-grant",
        ),
        pytest.param(
            {"chat_id": "", "workspace_id": "ws-1"},
            Effect.ALLOW,
            "machine_for_workspace_owner",
            id="for-the-owner-of-its-workspace",
        ),
        pytest.param(
            {"chat_id": "", "workspace_id": "ws-1", "auth_mode": "per_user"},
            Effect.ALLOW,
            "machine_for_workspace_owner_grant",
            id="the-workspace-owners-own-grant",
        ),
        pytest.param(
            {"chat_id": "", "workspace_id": ""}, Effect.DENY, "scope_unnamed", id="neither-named"
        ),
        pytest.param({"workspace_id": "ws-1"}, Effect.DENY, "scope_unnamed", id="both-named"),
        pytest.param(
            {"auth_mode": "per_user", "owner_user_id": str(USER_ID)},
            Effect.DENY,
            "not_owner",
            id="a-per-user-grant-is-only-ever-the-owners",
        ),
        pytest.param(
            {"auth_mode": "per_user", "member_entitled": False},
            Effect.DENY,
            "not_entitled",
            id="a-per-user-row-the-owner-may-not-use",
        ),
        pytest.param(
            {"auth_mode": "per_user", "enabled": False},
            Effect.DENY,
            "connection_disabled",
            id="a-disabled-per-user-row",
        ),
        pytest.param(
            {"auth_mode": "nonsense"}, Effect.DENY, "auth_mode_not_shared", id="an-unknown-mode"
        ),
        pytest.param({"has_shared_secret": False}, Effect.DENY, "no_shared_secret", id="no-secret"),
        pytest.param(
            {"chat_bound_machine_id": OTHER_MACHINE_ID, "member_entitled": False, "enabled": False},
            Effect.DENY,
            "machine_does_not_hold_chat",
            id="the-binding-is-checked-before-any-row-fact",
        ),
        pytest.param(
            {"for_user_id": "", "member_entitled": False},
            Effect.DENY,
            "chat_has_no_owner",
            id="the-owner-is-named-before-entitlement",
        ),
        pytest.param(
            {"member_entitled": False, "owner_user_id": str(USER_ID), "enabled": False},
            Effect.DENY,
            "not_entitled",
            id="entitlement-before-ownership-before-the-switch",
        ),
    ],
)
def test_connector_credential_machine_table(
    ctx: ActingContext, overrides: dict[str, object], effect: Effect, reason: str
) -> None:
    """A box leases for the owner of a chat it holds: the binding is compared
    against the box itself first, then the owner's row facts in the person's
    order, so the machine's door and the person's can never disagree about
    which connections hand out a credential."""
    decision = authorize(
        ctx, Action.FETCH_CREDENTIAL, CONNECTOR, {**MACHINE_CONNECTOR, **overrides}
    )
    _expect(
        decision,
        effect=effect,
        reason=reason,
        policy="connector.credential",
        message="" if effect is Effect.ALLOW else NOT_FOUND,
        as_not_found=effect is Effect.DENY,
    )


def test_an_unclaimed_box_leases_no_credential() -> None:
    """Its principal id is the credential's, never a machine's, so no chat's
    binding can name it."""
    decision = authorize(UNCLAIMED_BOX, Action.FETCH_CREDENTIAL, CONNECTOR, MACHINE_CONNECTOR)
    assert (decision.allowed, decision.reason) == (False, "machine_does_not_hold_chat")


def test_a_box_leases_nothing_in_an_org_it_does_not_serve() -> None:
    """The engine's tenancy floor answers before the policy: a dedicated box
    reaches the orgs it is assigned to and its own, and a connection anywhere
    else is the opaque not-found even with every fact permissive."""
    elsewhere = Resource(ResourceType.CONNECTOR, id="conn-9", org_id=THIRD_ORG)
    decision = authorize(DEDICATED_BOX, Action.FETCH_CREDENTIAL, elsewhere, MACHINE_CONNECTOR)
    assert (decision.allowed, decision.reason, decision.as_not_found) == (False, "cross_org", True)


@pytest.mark.parametrize(
    ("ctx", "overrides", "reason"),
    [
        pytest.param(USER, {}, "member_entitled", id="a-person-is-decided-as-themselves"),
        pytest.param(
            USER,
            {"owner_user_id": str(OTHER_USER_ID)},
            "not_owner",
            id="naming-another-owner-does-not-open-their-personal-row",
        ),
        pytest.param(
            AGENT,
            {"owner_user_id": str(OTHER_USER_ID), "chat_bound_machine_id": MACHINE_ID},
            "not_owner",
            id="an-agent-on-a-persons-session-is-not-a-machine",
        ),
        pytest.param(
            MACHINE_AGENT,
            {"owner_user_id": str(OTHER_USER_ID)},
            "not_owner",
            id="an-agent-asserting-the-machine-is-still-its-person",
        ),
    ],
)
def test_the_chat_owner_facts_never_admit_a_person(
    ctx: ActingContext, overrides: dict[str, object], reason: str
) -> None:
    """The machine facts ride along on a person's request and change nothing:
    a person is decided on their own id, so naming a colleague as the chat's
    owner cannot lease that colleague's personal connection to them."""
    decision = authorize(
        ctx, Action.FETCH_CREDENTIAL, CONNECTOR, {**MACHINE_CONNECTOR, **overrides}
    )
    assert decision.reason == reason
    assert decision.allowed is (reason == "member_entitled")


@pytest.mark.parametrize("key", sorted(set(MACHINE_CONNECTOR) - {"team_id"}))
@pytest.mark.parametrize("shape", ["removed", "mistyped"])
def test_every_machine_lease_fact_is_required(key: str, shape: str) -> None:
    """A route that forgets or mistypes a fact is denied naming it — the
    binding and the person acted for included — never admitted by a branch
    that happened not to read it."""
    attrs = (
        _without(MACHINE_CONNECTOR, key)
        if shape == "removed"
        else {**MACHINE_CONNECTOR, key: MACHINE_CONNECTOR_WRONG_TYPED[key]}
    )
    decision = authorize(POOL_BOX, Action.FETCH_CREDENTIAL, CONNECTOR, attrs)
    assert (decision.allowed, decision.reason) == (False, f"missing_attribute:{key}")


@pytest.mark.parametrize("action", CONNECTION_ACTIONS)
@pytest.mark.parametrize(
    ("ctx", "overrides"),
    [
        pytest.param(USER, {}, id="the-owner"),
        pytest.param(AGENT, {}, id="an-agent-in-the-owners-session"),
        pytest.param(PAT, {}, id="the-owners-token"),
        pytest.param(
            USER,
            {"is_org_admin": True, "roles": frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER})},
            id="an-org-admin",
        ),
        pytest.param(
            USER,
            {"owner_user_id": str(OTHER_USER_ID), "shared_role": "owner"},
            id="a-reader-holding-full-access",
        ),
        pytest.param(
            MACHINE_AGENT,
            {"asserted_machine_verified": True},
            id="a-verified-agent-of-the-bound-machine",
        ),
    ],
)
def test_a_chats_connections_are_never_a_persons_door(
    action: Action, ctx: ActingContext, overrides: dict[str, object]
) -> None:
    """The set is the owner's, handed to the box so the agent has it without
    the owner's credential — the names and the leases alike. A person has
    ``/me`` for their own; here every person — the owner, an admin, a reader
    with the top rung, an agent that proved itself the machine on a person's
    session — is told nothing, so no human learns a colleague's warehouses, or
    leases their credentials, through a chat they can open."""
    decision = authorize(ctx, action, CHAT, {**CHAT_ALLOW, **overrides})
    _expect(
        decision,
        effect=Effect.DENY,
        reason="machine_required",
        policy="chat.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    "key",
    sorted(set(CHAT_ALLOW) - {"team_id", "payer_stands"}),
)
def test_every_chat_fact_is_still_required_on_the_machine_branch(key: str) -> None:
    """The branch runs first but it does not run cheap: a route that forgets a
    fact is denied naming the key, not admitted by a branch that happened not
    to read it — the same rule as every other chat fact. The payer fact is the
    one a box is never given: it is resolved for a person's send alone."""
    decision = authorize(DEDICATED_BOX, Action.READ, CHAT, _without(MACHINE_CHAT, key))
    assert (decision.allowed, decision.reason) == (False, f"missing_attribute:{key}")


def test_the_machine_write_still_requires_the_publisher_set() -> None:
    decision = authorize(DEDICATED_BOX, Action.WRITE, CHAT, MACHINE_CHAT)
    assert decision.reason == "missing_attribute:publisher_machine_ids"


def test_a_verified_agent_that_is_the_orgs_current_machine_still_reports_on_a_past_boxs_chat() -> (
    None
):
    """The operator-session rule is unchanged by the machine branch: the org's
    current workspace machine — the box that now serves the org — may report
    on a chat bound to the box it replaced. Only a box on its own credential
    is held to holding the chat."""
    attrs = {
        **CHAT_ALLOW,
        "asserted_machine_verified": True,
        "bound_machine_id": OTHER_MACHINE_ID,
        "publisher_machine_ids": frozenset({MACHINE_ID, OTHER_MACHINE_ID}),
    }
    decision = authorize(MACHINE_AGENT, Action.WRITE, CHAT, attrs)
    assert (decision.allowed, decision.reason) == (True, "bound_machine_reports")


@pytest.mark.parametrize("ctx", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize(
    ("action", "resource", "attrs", "allowed", "reason"),
    [
        pytest.param(
            Action.WRITE,
            OWN_MACHINE,
            MACHINE_MACHINE,
            True,
            "machine_holds_machine",
            id="beats-its-own-row",
        ),
        pytest.param(
            Action.WRITE,
            OTHER_MACHINE,
            MACHINE_MACHINE,
            False,
            "machine_does_not_hold_machine",
            id="another-machine-is-not-found",
        ),
        pytest.param(
            Action.READ,
            OWN_MACHINE,
            MACHINE_MACHINE,
            False,
            "machine_does_not_hold_machine",
            id="a-box-reads-no-banner",
        ),
        pytest.param(
            Action.WRITE,
            OWN_MACHINE,
            {**MACHINE_MACHINE, "controls": True},
            False,
            "machine_does_not_hold_machine",
            id="a-box-controls-nothing",
        ),
        pytest.param(
            Action.WRITE,
            OWN_MACHINE,
            {**MACHINE_MACHINE, "lifecycle": "session"},
            False,
            "machine_does_not_hold_machine",
            id="a-session-row-is-not-a-box",
        ),
        pytest.param(
            Action.WRITE,
            OWN_MACHINE,
            {
                **MACHINE_MACHINE,
                "in_org": True,
                "is_owner": True,
                "roles": frozenset({Role.ADMIN}),
                "email_verified": True,
                "controls": True,
            },
            False,
            "machine_does_not_hold_machine",
            id="no-human-fact-lets-a-box-control",
        ),
    ],
)
def test_compute_machine_branches(
    ctx: ActingContext,
    action: Action,
    resource: Resource,
    attrs: dict[str, object],
    allowed: bool,
    reason: str,
) -> None:
    decision = authorize(ctx, action, resource, attrs)
    assert (decision.allowed, decision.reason) == (allowed, reason)
    if not allowed:
        assert (decision.as_not_found, decision.message) == (True, "Machine not found")


@pytest.mark.parametrize("key", sorted(MACHINE_MACHINE))
def test_every_compute_fact_is_still_required_on_the_machine_branch(key: str) -> None:
    decision = authorize(DEDICATED_BOX, Action.WRITE, OWN_MACHINE, _without(MACHINE_MACHINE, key))
    assert (decision.allowed, decision.reason) == (False, f"missing_attribute:{key}")


def test_an_unclaimed_box_beats_nothing() -> None:
    decision = authorize(UNCLAIMED_BOX, Action.WRITE, OWN_MACHINE, MACHINE_MACHINE)
    assert (decision.allowed, decision.reason) == (False, "machine_does_not_hold_machine")


# ---------------------------------------------------------------------------
# files.access — a box on its own machine credential
# ---------------------------------------------------------------------------

#: A node in the org a dedicated box serves; the pool box serves it too.
SERVED_FILE_NODE = Resource(
    ResourceType.FILE_NODE, id="node-1", org_id=OTHER_ORG, team_id=OTHER_ORG
)
#: A node in an org neither box serves.
FOREIGN_FILE_NODE = Resource(
    ResourceType.FILE_NODE, id="node-2", org_id=THIRD_ORG, team_id=THIRD_ORG
)
#: What the ladder hands a box inside a chat folder it runs: the machine rung's
#: actions, the decider having already taken ``share`` away from an automated
#: principal. The policy is asked with ``share`` still in the set below where
#: the case is about the policy's own refusal of it.
MACHINE_FOLDER_ACTIONS: frozenset[str] = LADDER_SETS["writer"] - {"share"}
#: The facts a route resolves for a box on a folder of a chat bound to it.
MACHINE_FILES_ALLOW: dict[str, object] = {
    **FILES_ALLOW,
    "allowed_actions": MACHINE_FOLDER_ACTIONS,
    "is_agent": True,
    "agent_machine_verified": True,
    "chat_subtree": True,
    "chat_bound_elsewhere": False,
    "machine_runs_it": True,
}


def _machine_files_attrs(**overrides: object) -> dict[str, object]:
    return {**MACHINE_FILES_ALLOW, **overrides}


@pytest.mark.parametrize("box", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize(
    "action", sorted(Action(a) for a in MACHINE_FOLDER_ACTIONS), ids=lambda a: a.value
)
def test_files_a_box_holds_its_chats_folder_and_the_allow_says_so(
    box: ActingContext, action: Action
) -> None:
    """Inside the folder of a chat bound to its machine, in an org its
    credential serves, a box does what the machine rung hands it — and the
    row names the reason as the box's, not a person's rung."""
    _expect(
        authorize(box, action, SERVED_FILE_NODE, _machine_files_attrs()),
        effect=Effect.ALLOW,
        reason="machine_holds_chat",
        policy="files.access",
        message="",
    )


def test_files_the_machine_reason_is_the_machines_alone() -> None:
    """The same facts under a person's agent allow for the ordinary reason:
    ``machine_holds_chat`` is never written for anyone but a box."""
    decision = authorize(AGENT, Action.READ, FILE_NODE, _machine_files_attrs())
    assert (decision.effect, decision.reason) == (Effect.ALLOW, "allowed_action")


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_box_outside_a_chat_folder_is_told_nothing_is_there(action: Action) -> None:
    """Whatever the ladder handed it — here the owner's whole set — a node
    that is not in a chat's folder is the opaque not-found to a box: it holds
    no rung a person granted, so a folder it is turned away from is one it
    must not learn is there."""
    _expect(
        authorize(
            DEDICATED_BOX,
            action,
            SERVED_FILE_NODE,
            _machine_files_attrs(allowed_actions=LADDER_SETS["owner"], chat_subtree=False),
        ),
        effect=Effect.DENY,
        reason="machine_outside_chat",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_box_on_another_boxs_chat_is_told_nothing_is_there(action: Action) -> None:
    """A chat folder bound to some other machine — the fact the route resolves
    from the chat row's binding — is refused opaquely even with every action
    handed in: a box learns nothing about a colleague's chat."""
    _expect(
        authorize(
            DEDICATED_BOX,
            action,
            SERVED_FILE_NODE,
            _machine_files_attrs(allowed_actions=LADDER_SETS["owner"], chat_bound_elsewhere=True),
        ),
        effect=Effect.DENY,
        reason="machine_mismatch",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_box_whose_machine_is_not_proven_holds_nothing(action: Action) -> None:
    """A route that resolved no machine for a machine principal is a route
    with a bug; the policy refuses rather than trusts it."""
    _expect(
        authorize(
            DEDICATED_BOX,
            action,
            SERVED_FILE_NODE,
            _machine_files_attrs(
                allowed_actions=LADDER_SETS["owner"], agent_machine_verified=False
            ),
        ),
        effect=Effect.DENY,
        reason="machine_unverified",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("box", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize(
    "subtree",
    [
        pytest.param({"chat_subtree": True, "workspace_subtree": False}, id="chat"),
        pytest.param({"chat_subtree": False, "workspace_subtree": True}, id="workspace"),
    ],
)
@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_box_that_does_not_run_the_folder_is_told_nothing_is_there(
    box: ActingContext, subtree: dict[str, object], action: Action
) -> None:
    """Bound to no other machine is not bound to this one. A chat or
    workspace no machine runs, handed the whole owner set (a grant to
    everyone in the org would hand it one), is the opaque not-found to a box
    that neither runs it nor holds its lease."""
    _expect(
        authorize(
            box,
            action,
            SERVED_FILE_NODE,
            _machine_files_attrs(
                allowed_actions=LADDER_SETS["owner"], machine_runs_it=False, **subtree
            ),
        ),
        effect=Effect.DENY,
        reason="machine_does_not_run",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


def test_files_the_box_holding_the_lease_is_admitted_by_it() -> None:
    """The departing holder: the binding moved, the lease did not. The
    decider reports the lease as the box running the folder, and the box is
    admitted on it."""
    attrs = _machine_files_attrs(machine_runs_it=True, holds_lease=True)
    decision = authorize(DEDICATED_BOX, Action.WRITE, SERVED_FILE_NODE, attrs)
    assert (decision.effect, decision.reason) == (Effect.ALLOW, "machine_holds_chat")


@pytest.mark.parametrize("caller", [USER, AGENT], ids=["user", "agent"])
def test_files_the_machine_running_fact_moves_nothing_for_a_person(caller: ActingContext) -> None:
    """``machine_runs_it`` admits a box and refuses nobody else: a person, or
    a person's agent, on a chat folder they were shared is decided on the
    actions their rung hands them."""
    attrs = _files_attrs(chat_subtree=True, machine_runs_it=False, is_agent=caller is AGENT)
    assert authorize(caller, Action.READ, FILE_NODE, attrs).allowed


def test_files_a_box_never_shares_its_chats_folder() -> None:
    """Even handed ``share`` by a ladder that mistook it for a manager, a box
    hands out nobody's reach. Visible, because the box can read the folder;
    opaque when it cannot."""
    _expect(
        authorize(
            DEDICATED_BOX,
            Action.SHARE,
            SERVED_FILE_NODE,
            _machine_files_attrs(allowed_actions=LADDER_SETS["owner"]),
        ),
        effect=Effect.DENY,
        reason="machine_may_not_share",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )
    _expect(
        authorize(
            DEDICATED_BOX,
            Action.SHARE,
            SERVED_FILE_NODE,
            _machine_files_attrs(allowed_actions=frozenset({"share"})),
        ),
        effect=Effect.DENY,
        reason="machine_may_not_share_unreadable",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


def test_files_a_box_is_held_to_the_same_table_inside_its_folder() -> None:
    """Being the chat's box admits it to the table, not past it: an action the
    rung does not hand it is refused where it can see the folder, and a held,
    locked or frozen folder refuses the box's write as anyone's."""
    _expect(
        authorize(
            DEDICATED_BOX,
            Action.WRITE,
            SERVED_FILE_NODE,
            _machine_files_attrs(allowed_actions=frozenset({"read"})),
        ),
        effect=Effect.DENY,
        reason="action_not_allowed",
        policy="files.access",
        message="Not allowed",
        error_code="files.forbidden",
    )
    for flag_attrs, reason in (
        ({"held": True, "flags": frozenset({"held"})}, "held"),
        ({"locked": True, "flags": frozenset({"locked"})}, "locked"),
        ({"flags": frozenset({"frozen"})}, "frozen"),
    ):
        _expect(
            authorize(
                DEDICATED_BOX, Action.WRITE, SERVED_FILE_NODE, _machine_files_attrs(**flag_attrs)
            ),
            effect=Effect.DENY,
            reason=reason,
            policy="files.access",
            message="Not allowed",
            error_code="files.forbidden",
        )
    assert authorize(
        DEDICATED_BOX,
        Action.READ,
        SERVED_FILE_NODE,
        _machine_files_attrs(flags=frozenset({"frozen"})),
    ).allowed


@pytest.mark.parametrize(
    ("box", "resource"),
    [
        pytest.param(DEDICATED_BOX, FOREIGN_FILE_NODE, id="dedicated-box-other-org"),
        pytest.param(UNCLAIMED_BOX, SERVED_FILE_NODE, id="unclaimed-box-serves-nothing"),
    ],
)
def test_files_a_box_reaches_no_drive_its_credential_does_not_serve(
    box: ActingContext, resource: Resource
) -> None:
    """The engine's tenancy floor, before the policy: a node in an org the
    credential does not serve is the opaque not-found with every fact in the
    box's favour."""
    _expect(
        authorize(box, Action.READ, resource, _machine_files_attrs()),
        effect=Effect.DENY,
        reason="cross_org",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


def test_files_absence_and_a_foreign_row_outrank_the_machine_branch() -> None:
    """A node that is not there, and one whose row is not in the org, are
    decided as they are for everyone — before the box is asked about at all."""
    _expect(
        authorize(DEDICATED_BOX, Action.READ, SERVED_FILE_NODE, _machine_files_attrs(exists=False)),
        effect=Effect.DENY,
        reason="no_such_node",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )
    _expect(
        authorize(DEDICATED_BOX, Action.READ, SERVED_FILE_NODE, _machine_files_attrs(in_org=False)),
        effect=Effect.DENY,
        reason="not_in_org",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("key", sorted(FILES_ALLOW), ids=lambda k: f"without-{k}")
def test_files_a_box_is_denied_when_a_fact_is_missing(key: str) -> None:
    decision = authorize(
        DEDICATED_BOX, Action.READ, SERVED_FILE_NODE, _without(MACHINE_FILES_ALLOW, key)
    )
    assert decision.effect is Effect.DENY, decision
    assert decision.reason == f"missing_attribute:{key}", decision


@pytest.mark.parametrize("key", sorted(FILES_WRONG_TYPED), ids=lambda k: f"mistyped-{k}")
def test_files_a_box_is_denied_when_a_fact_is_mistyped(key: str) -> None:
    decision = authorize(
        DEDICATED_BOX,
        Action.READ,
        SERVED_FILE_NODE,
        _machine_files_attrs(**{key: FILES_WRONG_TYPED[key]}),
    )
    assert decision.effect is Effect.DENY, decision
    assert decision.reason == f"missing_attribute:{key}", decision


# ---------------------------------------------------------------------------
# objects.access — a box on its own machine credential
# ---------------------------------------------------------------------------

#: A result in the org a dedicated box serves; the pool box serves it too.
SERVED_OBJECT = Resource(
    ResourceType.WORKSPACE_OBJECT, id="obj-2", org_id=OTHER_ORG, team_id=OTHER_ORG
)
FOREIGN_OBJECT = Resource(
    ResourceType.WORKSPACE_OBJECT, id="obj-3", org_id=THIRD_ORG, team_id=THIRD_ORG
)
#: The facts a route resolves for a box: a member of nothing, verified as
#: nothing, and the one fact that is the box's — the object came out of a chat
#: bound to its machine.
MACHINE_OBJECT: dict[str, object] = {
    **OBJECT_ALLOW,
    "in_org": False,
    "roles": frozenset(),
    "team_ids": frozenset(),
    "email_verified": False,
    "owner_user_id": str(OTHER_USER_ID),
    "held_by_machine": True,
}
MACHINE_OBJECT_ACTIONS = (Action.READ, Action.UPLOAD_PAYLOAD)


@pytest.mark.parametrize("box", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
@pytest.mark.parametrize("action", MACHINE_OBJECT_ACTIONS, ids=lambda a: a.value)
def test_objects_a_box_reads_and_fills_the_results_of_its_own_chats(
    box: ActingContext, action: Action
) -> None:
    _expect(
        authorize(box, action, SERVED_OBJECT, MACHINE_OBJECT),
        effect=Effect.ALLOW,
        reason="machine_holds_source_chat",
        policy="objects.access",
        message="",
    )


@pytest.mark.parametrize(
    "action",
    # Sorted: SUPPORTED is a frozenset, and xdist refuses a run whose workers
    # collected the same cases in different hash orders.
    sorted(
        (a for a in workspace_object.SUPPORTED if a not in MACHINE_OBJECT_ACTIONS),
        key=lambda a: a.value,
    ),
    ids=lambda a: a.value,
)
def test_objects_a_box_has_no_other_verb_on_its_own_result(action: Action) -> None:
    """Editing, exporting, re-running and creating are a person's; the box is
    told the object is not there for them, whatever it holds."""
    _expect(
        authorize(DEDICATED_BOX, action, SERVED_OBJECT, MACHINE_OBJECT),
        effect=Effect.DENY,
        reason="machine_does_not_hold_object",
        policy="objects.access",
        message="Object not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("action", MACHINE_OBJECT_ACTIONS, ids=lambda a: a.value)
def test_objects_a_result_of_a_chat_the_box_does_not_run_is_not_there(action: Action) -> None:
    _expect(
        authorize(
            DEDICATED_BOX, action, SERVED_OBJECT, {**MACHINE_OBJECT, "held_by_machine": False}
        ),
        effect=Effect.DENY,
        reason="machine_does_not_hold_object",
        policy="objects.access",
        message="Object not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    ("box", "resource"),
    [
        pytest.param(DEDICATED_BOX, FOREIGN_OBJECT, id="dedicated-box-other-org"),
        pytest.param(UNCLAIMED_BOX, SERVED_OBJECT, id="unclaimed-box-serves-nothing"),
    ],
)
def test_objects_a_box_reaches_no_org_its_credential_does_not_serve(
    box: ActingContext, resource: Resource
) -> None:
    _expect(
        authorize(box, Action.READ, resource, MACHINE_OBJECT),
        effect=Effect.DENY,
        reason="cross_org",
        policy="objects.access",
        message="Not found",
        as_not_found=True,
    )


def test_objects_the_machine_fact_moves_nothing_for_a_person() -> None:
    """A route that resolved ``held_by_machine`` true for a person (a bug) has
    not made them a machine: they are decided as the member they are, and an
    upload still wants the agent."""
    decision = authorize(USER, Action.READ, OBJECT, {**OBJECT_ALLOW, "held_by_machine": True})
    assert (decision.effect, decision.reason) == (Effect.ALLOW, "in_audience")
    _expect(
        authorize(USER, Action.UPLOAD_PAYLOAD, OBJECT, {**OBJECT_ALLOW, "held_by_machine": True}),
        effect=Effect.DENY,
        reason="agent_principal_required",
        policy="objects.access",
        message="Not allowed",
    )


# ---------------------------------------------------------------------------
# files.access — a native workspace's tree
# ---------------------------------------------------------------------------

#: The facts a route resolves for a box on the shared tree of a workspace
#: whose chats are bound to it.
WORKSPACE_FILES_ALLOW: dict[str, object] = {
    **MACHINE_FILES_ALLOW,
    "chat_subtree": False,
    "workspace_subtree": True,
}


@pytest.mark.parametrize("box", [DEDICATED_BOX, POOL_BOX], ids=["dedicated", "pool"])
def test_files_a_box_holds_its_workspaces_tree_and_the_allow_says_so(box: ActingContext) -> None:
    _expect(
        authorize(box, Action.WRITE, SERVED_FILE_NODE, {**WORKSPACE_FILES_ALLOW}),
        effect=Effect.ALLOW,
        reason="machine_holds_workspace",
        policy="files.access",
        message="",
    )


@pytest.mark.parametrize("action", FILE_ACTIONS, ids=lambda a: a.value)
def test_files_a_box_on_another_boxs_workspace_is_told_nothing_is_there(action: Action) -> None:
    _expect(
        authorize(
            DEDICATED_BOX,
            action,
            SERVED_FILE_NODE,
            {
                **WORKSPACE_FILES_ALLOW,
                "allowed_actions": LADDER_SETS["owner"],
                "workspace_bound_elsewhere": True,
            },
        ),
        effect=Effect.DENY,
        reason="machine_mismatch",
        policy="files.access",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize(
    "action",
    sorted(Action(a) for a in files_policy.MACHINE_ONLY),
    ids=lambda a: a.value,
)
@pytest.mark.parametrize(
    ("verified", "elsewhere", "reason"),
    [
        pytest.param(False, False, "machine_unverified", id="an-unproven-agent"),
        pytest.param(True, True, "machine_mismatch", id="a-box-the-workspace-is-not-bound-to"),
    ],
)
def test_files_only_the_workspaces_own_box_takes_writes_or_snapshots_its_tree(
    action: Action, verified: bool, elsewhere: bool, reason: str
) -> None:
    """An agent on its operator's session, whatever rung that operator holds,
    takes a workspace's tree over only as the proven machine the workspace
    runs on: the same two facts a chat's folder asks."""
    decision = authorize(
        AGENT,
        action,
        FILE_NODE,
        _files_attrs(
            is_agent=True,
            agent_machine_verified=verified,
            workspace_subtree=True,
            workspace_bound_elsewhere=elsewhere,
        ),
    )
    assert (decision.allowed, decision.reason, decision.error_code) == (
        False,
        reason,
        files_policy.FORBIDDEN,
    )


def test_files_a_person_on_a_workspace_tree_is_decided_by_their_rung_alone() -> None:
    """The machine rules narrow agents and boxes, never a person writing into
    a workspace's files through their own session."""
    decision = authorize(USER, Action.WRITE, FILE_NODE, _files_attrs(workspace_subtree=True))
    assert (decision.allowed, decision.reason) == (True, "allowed_action")


# ---------------------------------------------------------------------------
# files.lease_kind — who takes, and who forces off, which kind of lease
# ---------------------------------------------------------------------------

FILE_LEASE = Resource(ResourceType.FILE_LEASE, id="node-1", org_id=ORG, team_id=TEAM)

LEASE_ALLOW: dict[str, object] = {
    "purpose": "workspace",
    "node_kind": "workspace",
    "inside_kind": "plain",
    "readable": True,
    "machine_verified": True,
    "runs_here": True,
    "full_access": False,
    "owner_rung": False,
    "org_admin": False,
    "holder_purpose": "",
    "reason_given": False,
}

LEASE_WRONG_TYPED: dict[str, object] = {
    "purpose": 1,
    "node_kind": None,
    "inside_kind": 2,
    "readable": "yes",
    "machine_verified": "yes",
    "runs_here": 1,
    "full_access": "no",
    "owner_rung": "no",
    "org_admin": 0,
    "holder_purpose": None,
    "reason_given": "yes",
}


def _lease(**overrides: object) -> dict[str, object]:
    return {**LEASE_ALLOW, **overrides}


def test_lease_kind_the_bound_box_takes_its_workspace() -> None:
    _expect(
        authorize(DEDICATED_BOX, Action.LEASE, FILE_LEASE, _lease()),
        effect=Effect.ALLOW,
        reason="bound_box_takes_workspace",
        policy="files.lease_kind",
        message="",
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param(
            {"node_kind": "plain"}, "workspace_lease_off_a_workspace", id="a-plain-folder"
        ),
        pytest.param({"node_kind": "chat"}, "workspace_lease_off_a_workspace", id="a-chat-folder"),
        pytest.param(
            {"machine_verified": False, "runs_here": False},
            "workspace_lease_needs_its_box",
            id="a-person-or-an-unproven-agent",
        ),
        pytest.param(
            {"machine_verified": False, "full_access": True, "owner_rung": True, "org_admin": True},
            "workspace_lease_needs_its_box",
            id="an-org-admin-owner-is-still-not-the-box",
        ),
        pytest.param(
            {"runs_here": False}, "workspace_runs_elsewhere", id="a-box-it-is-not-bound-to"
        ),
    ],
)
def test_lease_kind_a_workspace_lease_is_the_bound_boxs_alone(
    overrides: dict[str, object], reason: str
) -> None:
    _expect(
        authorize(USER, Action.LEASE, FILE_LEASE, _lease(**overrides)),
        effect=Effect.DENY,
        reason=reason,
        policy="files.lease_kind",
        message="Not allowed",
        error_code=file_lease_policy.REFUSED,
    )


def test_lease_kind_a_refusal_on_an_unreadable_folder_is_opaque() -> None:
    _expect(
        authorize(USER, Action.LEASE, FILE_LEASE, _lease(readable=False, runs_here=False)),
        effect=Effect.DENY,
        reason="workspace_runs_elsewhere_unreadable",
        policy="files.lease_kind",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("purpose", sorted(file_lease_policy.PERSON_PURPOSES))
@pytest.mark.parametrize(
    ("node_kind", "inside_kind"),
    [
        pytest.param("workspace", "plain", id="on-the-workspace-folder"),
        pytest.param("plain", "workspace", id="under-the-workspace-folder"),
    ],
)
def test_lease_kind_a_persons_lease_in_a_workspace_takes_full_access(
    purpose: str, node_kind: str, inside_kind: str
) -> None:
    """A collaborator who may only edit cannot park the workspace (a lease
    there stops its box from taking it); Full access may."""
    facts = _lease(
        purpose=purpose,
        node_kind=node_kind,
        inside_kind=inside_kind,
        machine_verified=False,
        runs_here=False,
    )
    refused = authorize(USER, Action.LEASE, FILE_LEASE, facts)
    assert (refused.allowed, refused.reason) == (False, "workspace_lease_needs_full_access")
    allowed = authorize(USER, Action.LEASE, FILE_LEASE, {**facts, "full_access": True})
    assert (allowed.allowed, allowed.reason) == (True, "ladder_decides_lease")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"purpose": "mount", "node_kind": "plain"}, id="a-mount-of-an-ordinary-folder"
        ),
        pytest.param({"purpose": "share", "node_kind": "chat"}, id="a-share-of-a-chats-folder"),
    ],
)
def test_lease_kind_a_persons_lease_outside_a_workspace_is_the_ladders_answer(
    overrides: dict[str, object],
) -> None:
    facts = _lease(machine_verified=False, runs_here=False, **overrides)
    decision = authorize(USER, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (True, "ladder_decides_lease")


@pytest.mark.parametrize(
    ("node_kind", "inside_kind"),
    [
        pytest.param("workspace", "plain", id="on-a-workspace-folder"),
        pytest.param("plain", "workspace", id="under-a-workspace-folder"),
        pytest.param("chat", "workspace", id="on-a-chat-folder-in-a-workspace"),
    ],
)
@pytest.mark.parametrize("full_access", [False, True], ids=["may-edit", "full-access"])
def test_lease_kind_a_person_never_takes_a_chat_lease_in_a_workspace(
    node_kind: str, inside_kind: str, full_access: bool
) -> None:
    """A chat lease is a box's claim, which only the owner or an org admin may
    force off: a person holding one on or in a workspace would stop its box
    from taking it, whatever rung they hold."""
    facts = _lease(
        purpose="chat",
        node_kind=node_kind,
        inside_kind=inside_kind,
        machine_verified=False,
        runs_here=False,
        full_access=full_access,
        owner_rung=full_access,
    )
    decision = authorize(USER, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (False, "box_purpose_needs_its_box")
    assert decision.error_code == file_lease_policy.REFUSED


@pytest.mark.parametrize(
    "node_kind",
    [pytest.param("chat", id="a-chats-own-folder"), pytest.param("plain", id="an-ordinary-folder")],
)
def test_lease_kind_a_persons_chat_lease_outside_a_workspace_is_the_ladders(
    node_kind: str,
) -> None:
    facts = _lease(
        purpose="chat",
        node_kind=node_kind,
        inside_kind="plain",
        machine_verified=False,
        runs_here=False,
    )
    decision = authorize(USER, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (True, "ladder_decides_lease")


def test_lease_kind_a_purpose_nobody_takes_is_refused_whoever_asks() -> None:
    for ctx, verified in ((USER, False), (DEDICATED_BOX, True)):
        facts = _lease(purpose="cellar", node_kind="plain", machine_verified=verified)
        decision = authorize(ctx, Action.LEASE, FILE_LEASE, facts)
        assert (decision.allowed, decision.reason) == (False, "unknown_purpose")


@pytest.mark.parametrize(
    "inside_kind",
    [pytest.param("plain", id="a-chat-of-its-own"), pytest.param("workspace", id="in-a-workspace")],
)
def test_lease_kind_the_bound_box_takes_its_chats_folder(inside_kind: str) -> None:
    facts = _lease(purpose="chat", node_kind="chat", inside_kind=inside_kind)
    decision = authorize(DEDICATED_BOX, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (True, "bound_box_takes_chat")


def test_lease_kind_a_boxs_chat_lease_on_a_folder_outside_any_workspace_is_the_ladders() -> None:
    facts = _lease(purpose="chat", node_kind="plain", inside_kind="plain")
    decision = authorize(DEDICATED_BOX, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (True, "ladder_decides_lease")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param(
            {"node_kind": "plain", "inside_kind": "workspace"},
            "chat_lease_off_a_chat",
            id="a-chat-lease-under-a-workspace-folder",
        ),
        pytest.param(
            {"node_kind": "workspace"},
            "chat_lease_off_a_chat",
            id="a-chat-lease-on-a-workspace-folder",
        ),
        pytest.param({"runs_here": False}, "chat_runs_elsewhere", id="a-box-it-is-not-bound-to"),
    ],
)
def test_lease_kind_a_chat_lease_is_its_bound_boxs_alone(
    overrides: dict[str, object], reason: str
) -> None:
    facts = _lease(**{"purpose": "chat", "node_kind": "chat", **overrides})
    decision = authorize(DEDICATED_BOX, Action.LEASE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (False, reason)


@pytest.mark.parametrize("holder", sorted(file_lease_policy.BOX_PURPOSES))
@pytest.mark.parametrize(
    ("owner", "org_admin", "reason_given", "allowed", "reason"),
    [
        pytest.param(
            True, False, True, True, "owner_forces_box_lease", id="the-owner-with-a-reason"
        ),
        pytest.param(
            False, True, True, True, "owner_forces_box_lease", id="an-org-admin-with-a-reason"
        ),
        pytest.param(True, False, False, False, "box_lease_force_needs_reason", id="no-reason"),
        pytest.param(
            False, False, True, False, "box_lease_force_needs_owner", id="full-access-is-not-enough"
        ),
    ],
)
def test_lease_kind_forcing_a_boxs_lease_off_is_the_owners_with_a_reason(
    holder: str, owner: bool, org_admin: bool, reason_given: bool, allowed: bool, reason: str
) -> None:
    facts = _lease(
        purpose="",
        holder_purpose=holder,
        owner_rung=owner,
        org_admin=org_admin,
        reason_given=reason_given,
        full_access=True,
    )
    decision = authorize(USER, Action.LEASE_FORCE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (allowed, reason)


def test_lease_kind_forcing_a_persons_mount_off_stays_the_ladders() -> None:
    facts = _lease(purpose="", holder_purpose="mount", full_access=True)
    decision = authorize(USER, Action.LEASE_FORCE, FILE_LEASE, facts)
    assert (decision.allowed, decision.reason) == (True, "ladder_decides_force")


@pytest.mark.parametrize("action", sorted(file_lease_policy.SUPPORTED), ids=lambda a: a.value)
@pytest.mark.parametrize("key", sorted(LEASE_ALLOW))
def test_lease_kind_every_fact_is_required(action: Action, key: str) -> None:
    missing = authorize(DEDICATED_BOX, action, FILE_LEASE, _without(LEASE_ALLOW, key))
    assert (missing.allowed, missing.reason) == (False, f"missing_attribute:{key}")
    wrong = authorize(DEDICATED_BOX, action, FILE_LEASE, _lease(**{key: LEASE_WRONG_TYPED[key]}))
    assert (wrong.allowed, wrong.reason) == (False, f"missing_attribute:{key}")


def test_lease_kind_an_unknown_folder_kind_is_refused() -> None:
    decision = authorize(DEDICATED_BOX, Action.LEASE, FILE_LEASE, _lease(node_kind="cellar"))
    assert (decision.allowed, decision.reason) == (False, "unknown_node_kind")


def test_lease_kind_decides_nothing_but_lease_and_force() -> None:
    decision = authorize(DEDICATED_BOX, Action.READ, FILE_LEASE, _lease())
    assert (decision.allowed, decision.reason) == (False, "action_not_supported")


# -- a chat in a shared workspace belongs to the workspace ---------------------


@pytest.mark.parametrize(
    ("ctx", "action", "attrs", "allowed", "reason"),
    [
        pytest.param(
            USER, Action.READ, {}, False, "not_in_audience", id="the-starter-without-the-workspace"
        ),
        pytest.param(
            USER, Action.DELETE, {}, False, "not_in_audience", id="nor-may-they-delete-it"
        ),
        pytest.param(USER, Action.RENAME, {}, False, "not_in_audience", id="nor-rename-it"),
        pytest.param(
            USER,
            Action.READ,
            {"shared_role": "owner"},
            False,
            "not_in_audience",
            id="a-rung-on-the-chat-alone-counts-for-nothing",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"workspace_role": "reader"},
            True,
            "shared_reads",
            id="a-workspace-viewer-reads-any-chat-in-it",
        ),
        pytest.param(
            OTHER_USER,
            Action.READ,
            {"workspace_role": "owner"},
            True,
            "owner_reads",
            id="the-workspace-owner-reads-a-colleagues-chat",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"workspace_role": "owner"},
            True,
            "owner_deletes",
            id="and-deletes-it",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"workspace_role": "manager"},
            True,
            "workspace_manager_deletes",
            id="full-access-on-the-workspace-deletes",
        ),
        pytest.param(
            USER,
            Action.DELETE,
            {"workspace_role": "writer"},
            False,
            "owner_or_org_admin_required",
            id="edit-access-does-not-delete-even-the-chat-you-started",
        ),
        pytest.param(
            USER,
            Action.RENAME,
            {"workspace_role": "writer"},
            True,
            "writer_may_rename",
            id="edit-access-renames",
        ),
        pytest.param(
            USER,
            Action.RENAME,
            {"workspace_role": "reader"},
            False,
            "rename_rung_required",
            id="a-workspace-viewer-does-not-rename-the-chat-they-started",
        ),
        pytest.param(
            OTHER_USER,
            Action.DELETE,
            {"is_org_admin": True, "owner_departed": True},
            False,
            "not_in_audience",
            id="offboarding-the-starter-does-not-reach-the-workspaces-chat",
        ),
    ],
)
def test_a_chat_in_a_shared_workspace_answers_to_the_workspace(
    ctx: ActingContext, action: Action, attrs: dict[str, object], allowed: bool, reason: str
) -> None:
    decision = authorize(ctx, action, CHAT, {**CHAT_IN_WORKSPACE, **attrs})
    assert (decision.allowed, decision.reason) == (allowed, reason)


# ---------------------------------------------------------------------------
# notebook.run - who may run a notebook
# ---------------------------------------------------------------------------

#: An agent acting for a chat in the notebook's workspace.
NOTEBOOK_AGENT_ALLOW: dict[str, object] = {**NOTEBOOK_ALLOW, "agent_chat_in_workspace": True}


@pytest.mark.parametrize(
    ("ctx", "attrs", "reason"),
    [
        pytest.param(USER, NOTEBOOK_ALLOW, "can_edit", id="a-writer"),
        pytest.param(USER, {**NOTEBOOK_ALLOW, "rung": "manager"}, "can_edit", id="full-access"),
        pytest.param(USER, {**NOTEBOOK_ALLOW, "rung": "owner"}, "can_edit", id="the-owner"),
        pytest.param(
            AGENT, NOTEBOOK_AGENT_ALLOW, "agent_in_workspace_can_edit", id="an-agent-in-workspace"
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "scope_rung": "manager"},
            "can_edit",
            id="full-access-on-the-folder",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "scope_held": False, "scope_rung": ""},
            "can_edit",
            id="no-machine-holds-the-folder-so-nothing-runs-yet",
        ),
    ],
)
def test_notebook_run_allows_can_edit_and_above(
    ctx: ActingContext, attrs: dict[str, object], reason: str
) -> None:
    _expect(
        authorize(ctx, Action.RUN, NOTEBOOK, attrs),
        effect=Effect.ALLOW,
        reason=reason,
        policy="notebook.run",
        message="",
    )


@pytest.mark.parametrize(
    ("ctx", "attrs", "reason", "message"),
    [
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "rung": "reader"},
            "needs_can_edit",
            "Running a notebook takes Can edit",
            id="a-reader",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "rung": "commenter"},
            "needs_can_edit",
            "Running a notebook takes Can edit",
            id="a-commenter",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "lease_admits_writes": False},
            "lease_refuses_writes",
            "The notebook's folder is held by another writer",
            id="a-writer-under-a-lease-refusing-writes",
        ),
        pytest.param(
            AGENT,
            {**NOTEBOOK_AGENT_ALLOW, "agent_chat_in_workspace": False},
            "agent_chat_outside_workspace",
            "This chat is not part of the workspace that holds the notebook",
            id="an-agent-whose-chat-is-elsewhere",
        ),
        pytest.param(
            AGENT,
            {**NOTEBOOK_AGENT_ALLOW, "rung": "reader"},
            "needs_can_edit",
            "Running a notebook takes Can edit",
            id="an-agent-in-workspace-for-a-reader",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "scope_rung": ""},
            "needs_can_edit_on_folder",
            "Running this notebook takes Can edit on the folder it runs in",
            id="a-notebook-shared-alone-inside-a-workspace",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "scope_rung": "commenter"},
            "needs_can_edit_on_folder",
            "Running this notebook takes Can edit on the folder it runs in",
            id="can-comment-on-the-folder",
        ),
        pytest.param(
            USER,
            {**NOTEBOOK_ALLOW, "scope_rung": "admin"},
            "needs_can_edit_on_folder",
            "Running this notebook takes Can edit on the folder it runs in",
            id="a-folder-role-that-is-not-a-rung",
        ),
        pytest.param(
            AGENT,
            {**NOTEBOOK_AGENT_ALLOW, "scope_rung": "reader"},
            "needs_can_edit_on_folder",
            "Running this notebook takes Can edit on the folder it runs in",
            id="an-agent-whose-person-only-reads-the-folder",
        ),
        pytest.param(
            DEDICATED_BOX,
            {**NOTEBOOK_ALLOW, "rung": "owner"},
            "machine_runs_nothing",
            "A machine cannot run a notebook here",
            id="a-box-on-its-own-credential",
        ),
    ],
)
def test_notebook_run_refuses_with_a_reason(
    ctx: ActingContext, attrs: dict[str, object], reason: str, message: str
) -> None:
    _expect(
        authorize(ctx, Action.RUN, NOTEBOOK, attrs),
        effect=Effect.DENY,
        reason=reason,
        policy="notebook.run",
        message=message,
        error_code=notebook_policy.REFUSED,
    )


@pytest.mark.parametrize("rung", ["", "admin", "Writer"], ids=["none", "not-a-rung", "miscased"])
def test_notebook_run_without_a_rung_is_opaque(rung: str) -> None:
    """A caller with no rung may not learn the notebook exists; a role name
    that is not a Files rung counts as none."""
    _expect(
        authorize(USER, Action.RUN, NOTEBOOK, {**NOTEBOOK_ALLOW, "rung": rung}),
        effect=Effect.DENY,
        reason="no_rung",
        policy="notebook.run",
        message="Not found",
        as_not_found=True,
    )


@pytest.mark.parametrize("value", [None, "yes", 1], ids=["missing", "string", "int"])
def test_notebook_run_an_agent_needs_its_workspace_fact(value: object) -> None:
    attrs = dict(NOTEBOOK_ALLOW)
    if value is not None:
        attrs["agent_chat_in_workspace"] = value
    decision = authorize(AGENT, Action.RUN, NOTEBOOK, attrs)
    assert (decision.allowed, decision.reason) == (
        False,
        "missing_attribute:agent_chat_in_workspace",
    )


def test_notebook_run_a_person_needs_no_workspace_fact() -> None:
    """The workspace fact narrows agents only: a person's run is their rung's."""
    assert "agent_chat_in_workspace" not in NOTEBOOK_ALLOW
    assert authorize(USER, Action.RUN, NOTEBOOK, NOTEBOOK_ALLOW).allowed is True


@pytest.mark.parametrize(
    "action", [a for a in Action if a is not Action.RUN], ids=lambda a: a.value
)
def test_notebook_run_decides_running_only(action: Action) -> None:
    decision = authorize(USER, action, NOTEBOOK, NOTEBOOK_ALLOW)
    assert (decision.allowed, decision.reason) == (False, "action_not_supported")


def test_loading_the_notebook_policy_does_not_build_the_settings() -> None:
    import subprocess
    import sys

    probe = (
        "import sys; import alkera_core.authz.policies.notebook; "
        "assert 'alkera_core.config' not in sys.modules, 'settings built at import'"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------
# org_machine.access — who sees, uses, buys and runs an org's machines
# ---------------------------------------------------------------------------

ORG_MACHINE = Resource(ResourceType.ORG_MACHINE, id="om-1", org_id=ORG, team_id=TEAM)

#: An audience member who is no admin, reading an assigned machine.
OM_READ: dict[str, object] = {
    "in_org": True,
    "roles": {Role.MEMBER},
    "is_org_admin": False,
    "in_audience": True,
    "use_mode": "assigned",
    "purpose": "read",
}
OM_LIST: dict[str, object] = {"in_org": True, "roles": {Role.MEMBER}, "purpose": "list"}
OM_SETTINGS_READ: dict[str, object] = {"in_org": True, "is_org_admin": True, "purpose": "settings"}
#: A team admin of the owner team, verified, buying an assigned machine.
OM_PURCHASE: dict[str, object] = {
    "in_org": True,
    "roles": {Role.ADMIN},
    "is_org_admin": False,
    "email_verified": True,
    "operation": "purchase",
    "offering_visible": True,
    "quota_left": True,
    "sets_pool": False,
    "org_allows_pool": False,
}
#: A manager (admin of the owner team), verified, changing an assigned machine.
OM_MANAGE: dict[str, object] = {
    "in_org": True,
    "roles": {Role.ADMIN},
    "is_org_admin": False,
    "in_audience": False,
    "use_mode": "assigned",
    "email_verified": True,
    "operation": "manage",
    "sets_pool": False,
    "org_allows_pool": False,
}
OM_DELETE: dict[str, object] = {
    "in_org": True,
    "roles": {Role.ADMIN},
    "is_org_admin": False,
    "in_audience": False,
    "use_mode": "assigned",
    "email_verified": True,
}
OM_SETTINGS_WRITE: dict[str, object] = {
    "in_org": True,
    "is_org_admin": True,
    "email_verified": True,
    "operation": "settings",
}
#: A verified org admin adding a host the org runs, where adding is on.
OM_ADD: dict[str, object] = {
    "in_org": True,
    "is_org_admin": True,
    "email_verified": True,
    "operation": "add",
    "adding_enabled": True,
    "sets_pool": False,
    "org_allows_pool": False,
}

#: A value of the wrong type for every fact the policy reads.
OM_WRONG_TYPED: dict[str, object] = {
    "in_org": "true",
    "roles": {"admin"},
    "is_org_admin": 1,
    "in_audience": "yes",
    "use_mode": None,
    "purpose": 1,
    "email_verified": "true",
    "operation": None,
    "offering_visible": "true",
    "quota_left": 1,
    "sets_pool": "false",
    "org_allows_pool": 0,
    "adding_enabled": "yes",
}

#: Each question with the facts that allow it; every fact is required.
OM_QUESTIONS = [
    pytest.param(Action.READ, OM_READ, "audience_reads", id="read"),
    pytest.param(Action.READ, {**OM_READ, "purpose": "use"}, "audience_uses", id="use"),
    pytest.param(
        Action.READ,
        {**OM_READ, "purpose": "spend", "roles": {Role.ADMIN}},
        "manager_reads_spend",
        id="spend",
    ),
    pytest.param(Action.READ, OM_LIST, "member_lists", id="list"),
    pytest.param(Action.READ, OM_SETTINGS_READ, "org_admin_reads_settings", id="settings-read"),
    pytest.param(Action.WRITE, OM_PURCHASE, "manager_purchases", id="purchase"),
    pytest.param(Action.WRITE, OM_MANAGE, "manager_changes", id="manage"),
    pytest.param(Action.WRITE, OM_SETTINGS_WRITE, "org_admin_sets_settings", id="settings-write"),
    pytest.param(Action.WRITE, OM_ADD, "org_admin_adds", id="add"),
    pytest.param(Action.DELETE, OM_DELETE, "manager_deletes", id="delete"),
]


def _om_per_fact() -> list[Any]:
    return [
        pytest.param(action, attrs, key, id=f"{param.id}-{key}")
        for param in OM_QUESTIONS
        for action, attrs, _ in [param.values]
        for key in attrs
    ]


@pytest.mark.parametrize(("action", "attrs", "reason"), OM_QUESTIONS)
def test_org_machine_allow_facts_allow(
    action: Action, attrs: dict[str, object], reason: str
) -> None:
    decision = authorize(USER, action, ORG_MACHINE, attrs)
    assert (decision.allowed, decision.reason, decision.policy) == (
        True,
        reason,
        "org_machine.access",
    )


@pytest.mark.parametrize(("action", "attrs", "key"), _om_per_fact())
def test_org_machine_each_fact_removed_denies(
    action: Action, attrs: dict[str, object], key: str
) -> None:
    _expect(
        authorize(USER, action, ORG_MACHINE, _without(attrs, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="org_machine.access",
        message="Not allowed",
    )


@pytest.mark.parametrize(("action", "attrs", "key"), _om_per_fact())
def test_org_machine_each_fact_mistyped_denies(
    action: Action, attrs: dict[str, object], key: str
) -> None:
    _expect(
        authorize(USER, action, ORG_MACHINE, {**attrs, key: OM_WRONG_TYPED[key]}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="org_machine.access",
        message="Not allowed",
    )


_OM_NOT_FOUND = {"message": "Machine not found", "as_not_found": True}


@pytest.mark.parametrize(
    ("action", "attrs", "allowed", "reason", "extra"),
    [
        # Reading a machine.
        pytest.param(
            Action.READ,
            {**OM_READ, "in_audience": False, "roles": {Role.ADMIN}},
            True,
            "manager_reads",
            {},
            id="a-manager-outside-the-audience-reads",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "in_audience": False, "is_org_admin": True},
            True,
            "manager_reads",
            {},
            id="an-org-admin-reads-a-team-machine",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "in_audience": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="a-member-outside-the-audience-does-not-learn-it-exists",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "in_audience": False, "use_mode": "pool"},
            True,
            "audience_reads",
            {},
            id="a-pool-machine-is-read-by-every-member",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "in_org": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="read-not-in-org-is-not-found",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "steal"},
            False,
            "unknown_purpose",
            {"message": "Not allowed"},
            id="an-unknown-purpose-denies",
        ),
        # Using a machine (pin or move a workspace onto it).
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "use", "in_audience": False, "roles": {Role.ADMIN}},
            False,
            "not_in_audience",
            {"message": "This machine isn't shared with you.", "error_code": "machine_not_shared"},
            id="a-manager-outside-the-audience-does-not-use-it",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "use", "in_audience": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="an-outsider-cannot-use-what-it-cannot-see",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "use", "in_audience": False, "use_mode": "pool"},
            True,
            "audience_uses",
            {},
            id="every-member-uses-a-pool-machine",
        ),
        # Spend and runway.
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "spend"},
            False,
            "spend_needs_manager",
            {"message": "Only an admin of the team that holds this machine can see what it costs."},
            id="the-audience-does-not-see-spend",
        ),
        pytest.param(
            Action.READ,
            {**OM_READ, "purpose": "spend", "is_org_admin": True},
            True,
            "manager_reads_spend",
            {},
            id="an-org-admin-sees-spend",
        ),
        # Listing and the org's settings.
        pytest.param(
            Action.READ,
            {**OM_LIST, "roles": {Role.VIEWER}},
            False,
            "org_member_required",
            {"message": "org member role required"},
            id="a-viewer-does-not-list",
        ),
        pytest.param(
            Action.READ,
            {**OM_SETTINGS_READ, "is_org_admin": False},
            False,
            "settings_need_org_admin",
            {"message": "Only an org admin can see the organization's compute settings."},
            id="only-an-org-admin-reads-compute-settings",
        ),
        # Buying.
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "roles": {Role.MEMBER}, "is_org_admin": True},
            True,
            "manager_purchases",
            {},
            id="an-org-admin-buys-for-any-team",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "roles": {Role.MEMBER}},
            False,
            "team_admin_required",
            {"message": "Only an admin of the team that will hold the machine can buy it."},
            id="a-member-does-not-buy",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "in_org": False},
            False,
            "team_not_in_org",
            {"message": "Team not found", "as_not_found": True},
            id="an-owner-team-of-another-org-is-not-found",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "email_verified": False},
            False,
            "email_verification_required",
            {
                "message": "Verify your email address to perform this action.",
                "error_code": "email_verification_required",
            },
            id="an-unproven-address-does-not-buy",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "offering_visible": False},
            False,
            "offering_not_visible",
            {"message": "Offering not found", "as_not_found": True},
            id="an-offering-the-org-cannot-see-is-not-found",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "quota_left": False},
            False,
            "machine_quota",
            {"message": "Your plan doesn't allow another machine.", "error_code": "machine_quota"},
            id="a-plan-at-its-quota-refuses",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "sets_pool": True, "org_allows_pool": True, "is_org_admin": True},
            True,
            "manager_purchases",
            {},
            id="an-enterprise-org-admin-buys-into-the-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "sets_pool": True, "org_allows_pool": False, "is_org_admin": True},
            False,
            "org_pool_unavailable",
            {
                "message": "The org pool needs an Enterprise plan.",
                "error_code": "org_pool_unavailable",
            },
            id="a-self-serve-org-has-no-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "sets_pool": True, "org_allows_pool": True},
            False,
            "org_pool_needs_org_admin",
            {
                "message": "Only an org admin can put a machine in the org pool.",
                "error_code": "org_admin_required",
            },
            id="a-team-admin-does-not-buy-into-the-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_PURCHASE, "operation": "steal"},
            False,
            "unknown_operation",
            {"message": "Not allowed"},
            id="an-unknown-operation-denies",
        ),
        # Changing a machine.
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "roles": {Role.MEMBER}, "in_audience": True},
            False,
            "manager_required",
            {"message": "Only an admin of the team that holds this machine can change it."},
            id="the-audience-does-not-change-it",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "roles": {Role.MEMBER}},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="an-outsider-changing-it-is-not-found",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "in_org": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="manage-not-in-org-is-not-found",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "email_verified": False},
            False,
            "email_verification_required",
            {
                "message": "Verify your email address to perform this action.",
                "error_code": "email_verification_required",
            },
            id="an-unproven-manager-does-not-change-it",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "sets_pool": True, "org_allows_pool": True},
            False,
            "org_pool_needs_org_admin",
            {
                "message": "Only an org admin can put a machine in the org pool.",
                "error_code": "org_admin_required",
            },
            id="a-team-admin-does-not-move-a-machine-into-the-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "sets_pool": True, "org_allows_pool": False, "is_org_admin": True},
            False,
            "org_pool_unavailable",
            {
                "message": "The org pool needs an Enterprise plan.",
                "error_code": "org_pool_unavailable",
            },
            id="a-downgraded-org-cannot-switch-back-to-the-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_MANAGE, "use_mode": "pool", "roles": {Role.MEMBER}},
            False,
            "manager_required",
            {"message": "Only an admin of the team that holds this machine can change it."},
            id="every-member-reads-a-pool-machine-but-does-not-change-it",
        ),
        # Deleting.
        pytest.param(
            Action.DELETE,
            {**OM_DELETE, "roles": {Role.MEMBER}, "in_audience": True},
            False,
            "manager_required",
            {"message": "Only an admin of the team that holds this machine can change it."},
            id="the-audience-does-not-delete-it",
        ),
        pytest.param(
            Action.DELETE,
            {**OM_DELETE, "roles": {Role.MEMBER}},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="an-outsider-deleting-it-is-not-found",
        ),
        pytest.param(
            Action.DELETE,
            {**OM_DELETE, "in_org": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="delete-not-in-org-is-not-found",
        ),
        pytest.param(
            Action.DELETE,
            {**OM_DELETE, "email_verified": False},
            False,
            "email_verification_required",
            {
                "message": "Verify your email address to perform this action.",
                "error_code": "email_verification_required",
            },
            id="an-unproven-manager-does-not-delete-it",
        ),
        # The org's compute settings.
        pytest.param(
            Action.WRITE,
            {**OM_SETTINGS_WRITE, "is_org_admin": False},
            False,
            "settings_need_org_admin",
            {"message": "Only an org admin can change the organization's compute settings."},
            id="a-team-admin-does-not-change-compute-settings",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_SETTINGS_WRITE, "email_verified": False},
            False,
            "email_verification_required",
            {
                "message": "Verify your email address to perform this action.",
                "error_code": "email_verification_required",
            },
            id="an-unproven-org-admin-does-not-change-compute-settings",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_SETTINGS_WRITE, "in_org": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="settings-not-in-org-is-not-found",
        ),
        # Adding a host the org runs.
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "is_org_admin": False},
            False,
            "add_needs_org_admin",
            {"message": "Only an org admin can add a machine."},
            id="a-member-does-not-add-a-machine",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "adding_enabled": False},
            False,
            "adding_unavailable",
            {
                "message": "Adding your own machines is off in this deployment.",
                "error_code": "machine_adding_unavailable",
            },
            id="no-one-adds-where-the-deployment-turned-it-off",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "email_verified": False},
            False,
            "email_verification_required",
            {
                "message": "Verify your email address to perform this action.",
                "error_code": "email_verification_required",
            },
            id="an-unproven-org-admin-does-not-add",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "sets_pool": True},
            False,
            "org_pool_unavailable",
            {
                "message": "The org pool needs an Enterprise plan.",
                "error_code": "org_pool_unavailable",
            },
            id="an-added-machine-joins-the-pool-only-where-the-org-has-one",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "sets_pool": True, "org_allows_pool": True},
            True,
            "org_admin_adds",
            {},
            id="an-org-admin-adds-a-pool-machine-where-the-org-has-a-pool",
        ),
        pytest.param(
            Action.WRITE,
            {**OM_ADD, "in_org": False},
            False,
            "machine_not_visible",
            _OM_NOT_FOUND,
            id="add-not-in-org-is-not-found",
        ),
    ],
)
def test_org_machine_branches(
    action: Action,
    attrs: dict[str, object],
    allowed: bool,
    reason: str,
    extra: dict[str, Any],
) -> None:
    decision = authorize(USER, action, ORG_MACHINE, attrs)
    if allowed:
        assert (decision.allowed, decision.reason) == (True, reason)
        return
    _expect(
        decision,
        effect=Effect.DENY,
        reason=reason,
        policy="org_machine.access",
        message=extra["message"],
        as_not_found=extra.get("as_not_found", False),
        error_code=extra.get("error_code"),
    )


@pytest.mark.parametrize(
    "action",
    [action for action in Action if action not in {Action.READ, Action.WRITE, Action.DELETE}],
)
def test_org_machine_unsupported_actions_deny(action: Action) -> None:
    decision = authorize(USER, action, ORG_MACHINE, {})
    assert (decision.allowed, decision.reason) == (False, "action_not_supported")


@pytest.mark.parametrize(("action", "attrs", "_reason"), OM_QUESTIONS)
def test_a_box_on_its_own_credential_reaches_no_org_machine(
    action: Action, attrs: dict[str, object], _reason: str
) -> None:
    box = ActingContext.for_machine(
        machine_id=UUID(MACHINE_ID), credential_id=TOKEN_ID, org_id=ORG, label="box"
    )
    decision = authorize(box, action, ORG_MACHINE, attrs)
    assert (decision.allowed, decision.reason, decision.as_not_found) == (
        False,
        "machine_not_visible",
        True,
    )


def test_an_org_machine_of_another_org_is_not_found_before_the_policy() -> None:
    foreign = Resource(ResourceType.ORG_MACHINE, id="om-2", org_id=OTHER_ORG)
    decision = authorize(USER, Action.READ, foreign, {**OM_READ, "is_org_admin": True})
    assert (decision.allowed, decision.as_not_found) == (False, True)


# ---------------------------------------------------------------------------
# compute.offering — what Alkera sells, and machines it gives an org
# ---------------------------------------------------------------------------

#: The platform catalog: no org_id, the decision is about the platform.
CATALOG = Resource(ResourceType.COMPUTE_OFFERING, id="catalog")
#: The tenant's list of what its org may buy: carries the caller's org.
ORG_CATALOG = Resource(ResourceType.COMPUTE_OFFERING, id="offerings", org_id=ORG)

OFFERING_PLATFORM_ALLOW: dict[str, object] = {
    "platform_staff": True,
    "platform_admin": True,
    "org_exists": True,
}
OFFERING_PLATFORM_WRONG_TYPED: dict[str, object] = {
    "platform_staff": "true",
    "platform_admin": 1,
    "org_exists": "yes",
}
OFFERING_BROWSE_ALLOW: dict[str, object] = {
    "operation": "browse",
    "in_org": True,
    "is_member": True,
    "email_verified": True,
}
OFFERING_BROWSE_WRONG_TYPED: dict[str, object] = {
    "in_org": "true",
    "is_member": 1,
    "email_verified": "yes",
}
#: Every platform operation with the action it is decided under.
OFFERING_PLATFORM_OPERATIONS: list[tuple[str, Action]] = [
    ("list", Action.READ),
    ("org_machines", Action.READ),
    ("create", Action.ADMIN),
    ("update", Action.ADMIN),
    ("grant", Action.ADMIN),
]


def _platform_offering(operation: str, **over: object) -> dict[str, object]:
    return {**OFFERING_PLATFORM_ALLOW, "operation": operation, **over}


@pytest.mark.parametrize(
    ("operation", "action", "overrides", "effect", "reason", "message", "as_not_found"),
    [
        pytest.param(
            op,
            act,
            {},
            Effect.ALLOW,
            "staff_read" if act is Action.READ else "admin_changes",
            "",
            False,
            id=f"admin-{op}",
        )
        for op, act in OFFERING_PLATFORM_OPERATIONS
    ]
    + [
        pytest.param(
            "list",
            Action.READ,
            {"platform_admin": False},
            Effect.ALLOW,
            "staff_read",
            "",
            False,
            id="support-reads-the-catalog",
        ),
        pytest.param(
            "org_machines",
            Action.READ,
            {"platform_admin": False},
            Effect.ALLOW,
            "staff_read",
            "",
            False,
            id="support-reads-an-orgs-machines",
        ),
    ]
    + [
        pytest.param(
            op,
            Action.ADMIN,
            {"platform_admin": False},
            Effect.DENY,
            "platform_admin_required",
            "Platform admin role required",
            False,
            id=f"support-may-not-{op}",
        )
        for op in ("create", "update", "grant")
    ]
    + [
        pytest.param(
            op,
            act,
            {"platform_staff": False, "platform_admin": False},
            Effect.DENY,
            "platform_staff_required",
            "Platform staff role required",
            False,
            id=f"a-tenant-may-not-{op}",
        )
        for op, act in OFFERING_PLATFORM_OPERATIONS
    ]
    + [
        pytest.param(
            op,
            act,
            {"org_exists": False},
            Effect.DENY,
            "org_not_found",
            "Organization not found",
            True,
            id=f"unknown-org-{op}",
        )
        for op, act in (("org_machines", Action.READ), ("grant", Action.ADMIN))
    ],
)
def test_compute_offering_platform_table(
    operation: str,
    action: Action,
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
) -> None:
    _expect(
        authorize(USER, action, CATALOG, _platform_offering(operation, **overrides)),
        effect=effect,
        reason=reason,
        policy="compute.offering",
        message=message,
        as_not_found=as_not_found,
    )


def test_compute_offering_hides_org_existence_from_a_non_staff_caller() -> None:
    """Staff is decided first: a stranger who guesses an org id must not learn
    from the status code whether it was a real one."""
    stranger = _platform_offering("grant", platform_staff=False, platform_admin=False)
    real = authorize(USER, Action.ADMIN, CATALOG, stranger)
    ghost = authorize(USER, Action.ADMIN, CATALOG, {**stranger, "org_exists": False})
    assert real.reason == ghost.reason == "platform_staff_required"
    assert real.as_not_found is ghost.as_not_found is False


@pytest.mark.parametrize(
    ("overrides", "effect", "reason", "message", "as_not_found", "error_code"),
    [
        pytest.param({}, Effect.ALLOW, "member_browses", "", False, None, id="verified-member"),
        pytest.param(
            {"in_org": False},
            Effect.DENY,
            "not_in_org",
            "Organization not found",
            True,
            None,
            id="outside-the-org-reads-as-missing",
        ),
        pytest.param(
            {"is_member": False},
            Effect.DENY,
            "org_member_required",
            "org member role required",
            False,
            None,
            id="a-viewer-is-refused",
        ),
        pytest.param(
            {"email_verified": False},
            Effect.DENY,
            "email_verification_required",
            "Verify your email address to perform this action.",
            False,
            "email_verification_required",
            id="an-unverified-member-is-refused",
        ),
    ],
)
def test_compute_offering_browse_table(
    overrides: dict[str, object],
    effect: Effect,
    reason: str,
    message: str,
    as_not_found: bool,
    error_code: str | None,
) -> None:
    _expect(
        authorize(USER, Action.READ, ORG_CATALOG, {**OFFERING_BROWSE_ALLOW, **overrides}),
        effect=effect,
        reason=reason,
        policy="compute.offering",
        message=message,
        as_not_found=as_not_found,
        error_code=error_code,
    )


def test_compute_offering_browse_needs_no_platform_role() -> None:
    """A tenant browses on tenant facts alone: the platform facts are neither
    required nor able to stand in for membership."""
    staff_stranger = {
        **OFFERING_BROWSE_ALLOW,
        "in_org": False,
        "platform_staff": True,
        "platform_admin": True,
        "org_exists": True,
    }
    assert authorize(USER, Action.READ, ORG_CATALOG, OFFERING_BROWSE_ALLOW).allowed is True
    assert authorize(USER, Action.READ, ORG_CATALOG, staff_stranger).reason == "not_in_org"


def test_compute_offering_browse_is_bound_to_the_callers_org() -> None:
    """The engine's tenancy floor runs before the policy: browsing another
    org's list is the opaque not-found whatever the facts say."""
    foreign = Resource(ResourceType.COMPUTE_OFFERING, id="offerings", org_id=OTHER_ORG)
    decision = authorize(USER, Action.READ, foreign, OFFERING_BROWSE_ALLOW)
    assert decision.allowed is False
    assert decision.as_not_found is True


@pytest.mark.parametrize(
    ("operation", "action"),
    [
        pytest.param("browse", Action.ADMIN, id="browse-is-not-a-write"),
        pytest.param("create", Action.READ, id="a-read-cannot-create"),
        pytest.param("update", Action.READ, id="a-read-cannot-update"),
        pytest.param("grant", Action.READ, id="a-read-cannot-grant"),
        pytest.param("list", Action.ADMIN, id="list-is-not-a-write"),
        pytest.param("org_machines", Action.ADMIN, id="org-machines-is-not-a-write"),
        pytest.param("delete", Action.ADMIN, id="an-unknown-operation"),
        pytest.param("", Action.READ, id="an-empty-operation"),
    ],
)
def test_compute_offering_binds_each_operation_to_its_action(
    operation: str, action: Action
) -> None:
    attrs = {**OFFERING_PLATFORM_ALLOW, **OFFERING_BROWSE_ALLOW, "operation": operation}
    _expect(
        authorize(USER, action, CATALOG, attrs),
        effect=Effect.DENY,
        reason="operation_not_supported",
        policy="compute.offering",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    "other",
    [a for a in Action if a not in (Action.READ, Action.ADMIN)],
    ids=lambda a: a.value,
)
def test_compute_offering_refuses_every_action_but_read_and_admin(other: Action) -> None:
    _expect(
        authorize(USER, other, CATALOG, _platform_offering("create")),
        effect=Effect.DENY,
        reason="action_not_supported",
        policy="compute.offering",
        message="Not allowed",
    )


def _offering_fact_cases() -> list[Any]:
    cases: list[Any] = []
    for op, act in OFFERING_PLATFORM_OPERATIONS:
        for key in sorted(OFFERING_PLATFORM_WRONG_TYPED):
            cases.append(
                pytest.param(
                    act,
                    CATALOG,
                    _platform_offering(op),
                    key,
                    OFFERING_PLATFORM_WRONG_TYPED[key],
                    id=f"{op}-{key}",
                )
            )
    for key in sorted(OFFERING_BROWSE_WRONG_TYPED):
        cases.append(
            pytest.param(
                Action.READ,
                ORG_CATALOG,
                OFFERING_BROWSE_ALLOW,
                key,
                OFFERING_BROWSE_WRONG_TYPED[key],
                id=f"browse-{key}",
            )
        )
    for op, act in [*OFFERING_PLATFORM_OPERATIONS, ("browse", Action.READ)]:
        allow_attrs = OFFERING_BROWSE_ALLOW if op == "browse" else _platform_offering(op)
        cases.append(
            pytest.param(
                act,
                ORG_CATALOG if op == "browse" else CATALOG,
                allow_attrs,
                "operation",
                7,
                id=f"{op}-operation",
            )
        )
    return cases


@pytest.mark.parametrize(
    ("action", "resource", "allow_attrs", "key", "wrong"), _offering_fact_cases()
)
def test_compute_offering_missing_attribute_denies(
    action: Action, resource: Resource, allow_attrs: dict[str, object], key: str, wrong: object
) -> None:
    """Remove any fact an operation needs from an otherwise allowing request:
    deny, including the facts the branch taken would not have consulted."""
    assert authorize(USER, action, resource, allow_attrs).allowed is True
    _expect(
        authorize(USER, action, resource, _without(allow_attrs, key)),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.offering",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    ("action", "resource", "allow_attrs", "key", "wrong"), _offering_fact_cases()
)
def test_compute_offering_wrong_attribute_type_denies(
    action: Action, resource: Resource, allow_attrs: dict[str, object], key: str, wrong: object
) -> None:
    _expect(
        authorize(USER, action, resource, {**allow_attrs, key: wrong}),
        effect=Effect.DENY,
        reason=f"missing_attribute:{key}",
        policy="compute.offering",
        message="Not allowed",
    )


@pytest.mark.parametrize(
    "ctx", [AGENT, PAT, SERVICE_CI, SERVICE_PROXY], ids=["agent", "pat", "ci", "proxy"]
)
def test_compute_offering_decides_on_the_facts_whatever_the_subject(ctx: ActingContext) -> None:
    """The policy never reads the principal: staffness and membership are
    facts the route resolves, not credential kinds."""
    assert authorize(ctx, Action.ADMIN, CATALOG, _platform_offering("create")).allowed is True
    refused = authorize(
        ctx, Action.ADMIN, CATALOG, _platform_offering("create", platform_staff=False)
    )
    assert refused.reason == "platform_staff_required"


# ---------------------------------------------------------------------------
# chat.access ANSWER_STANDING: an "Always" answer is the chat owner's alone
# ---------------------------------------------------------------------------


def test_the_chat_owner_may_answer_always() -> None:
    _expect(
        authorize(USER, Action.ANSWER_STANDING, CHAT, CHAT_ALLOW),
        effect=Effect.ALLOW,
        reason="owner_answers_standing",
        policy=chat.POLICY,
        message="",
    )


@pytest.mark.parametrize(
    ("ctx", "attrs"),
    [
        pytest.param(OTHER_USER, {**CHAT_ALLOW, "shared_role": "writer"}, id="a-shared-editor"),
        pytest.param(
            OTHER_USER,
            {**CHAT_IN_WORKSPACE, "workspace_role": chat.WORKSPACE_OWNER_ROLE},
            id="the-workspace-owner-in-a-colleagues-chat",
        ),
        pytest.param(
            OTHER_USER,
            {**CHAT_IN_WORKSPACE, "workspace_role": "writer"},
            id="a-workspace-editor",
        ),
    ],
)
def test_anyone_but_the_chat_owner_answers_once(
    ctx: ActingContext, attrs: dict[str, object]
) -> None:
    """A standing answer becomes a rule of the chat's own owner, so whoever
    else may speak here (and drive the agent) is refused with a code."""
    _expect(
        authorize(ctx, Action.ANSWER_STANDING, CHAT, attrs),
        effect=Effect.DENY,
        reason="standing_answer_owner_required",
        policy=chat.POLICY,
        message=chat.STANDING_ANSWER_DENIED_MESSAGE,
        error_code=chat.STANDING_ANSWER_DENIED_CODE,
    )


def test_a_reader_who_cannot_see_the_chat_learns_nothing_from_answering_always() -> None:
    _expect(
        authorize(OTHER_USER, Action.ANSWER_STANDING, CHAT, CHAT_ALLOW),
        effect=Effect.DENY,
        reason="not_in_audience",
        policy=chat.POLICY,
        message=chat.NOT_FOUND,
        as_not_found=True,
    )


# ---------------------------------------------------------------------------
# compute.personal_box: who may list and revoke a person's own boxes
# ---------------------------------------------------------------------------

_PERSONAL_BOX_ACTIONS = sorted(personal_box_policy.SUPPORTED, key=lambda a: a.value)


@pytest.mark.parametrize("action", _PERSONAL_BOX_ACTIONS, ids=lambda a: a.value)
def test_a_person_may_list_and_revoke_their_own_box(action: Action) -> None:
    _expect(
        authorize(USER, action, PERSONAL_BOX_RESOURCE, PERSONAL_BOX_ALLOW),
        effect=Effect.ALLOW,
        reason="box_owner",
        policy=personal_box_policy.POLICY,
        message="",
    )


@pytest.mark.parametrize("action", _PERSONAL_BOX_ACTIONS, ids=lambda a: a.value)
def test_someone_elses_box_is_not_found(action: Action) -> None:
    _expect(
        authorize(OTHER_USER, action, PERSONAL_BOX_RESOURCE, {"is_owner": False}),
        effect=Effect.DENY,
        reason="not_your_box",
        policy=personal_box_policy.POLICY,
        message=personal_box_policy.NOT_FOUND_MESSAGE,
        as_not_found=True,
    )


@pytest.mark.parametrize("action", _PERSONAL_BOX_ACTIONS, ids=lambda a: a.value)
def test_an_agent_may_not_manage_its_persons_boxes(action: Action) -> None:
    _expect(
        authorize(AGENT, action, PERSONAL_BOX_RESOURCE, PERSONAL_BOX_ALLOW),
        effect=Effect.DENY,
        reason="agent_acting",
        policy=personal_box_policy.POLICY,
        message=personal_box_policy.AGENT_MESSAGE,
        error_code=personal_box_policy.AGENT_CODE,
    )


@pytest.mark.parametrize("action", _PERSONAL_BOX_ACTIONS, ids=lambda a: a.value)
def test_a_non_person_credential_sees_no_box(action: Action) -> None:
    _expect(
        authorize(PAT, action, PERSONAL_BOX_RESOURCE, PERSONAL_BOX_ALLOW),
        effect=Effect.DENY,
        reason="not_a_person",
        policy=personal_box_policy.POLICY,
        message=personal_box_policy.NOT_FOUND_MESSAGE,
        as_not_found=True,
    )


# ---------------------------------------------------------------------------
# account.lifecycle: who may export, delete, or read an account's requests
# ---------------------------------------------------------------------------

_ACCOUNT_ACTIONS = sorted(account_policy.SUPPORTED, key=lambda a: a.value)


@pytest.mark.parametrize("action", _ACCOUNT_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize(
    "platform_role", ["", "alkera_support", "alkera_admin"], ids=["none", "support", "admin"]
)
def test_account_owner_on_their_own_session_may_do_everything(
    action: Action, platform_role: str
) -> None:
    _expect(
        authorize(
            USER, action, ACCOUNT_RESOURCE, {**ACCOUNT_ALLOW, "platform_role": platform_role}
        ),
        effect=Effect.ALLOW,
        reason="account_owner",
        policy=account_policy.POLICY,
        message="",
    )


@pytest.mark.parametrize("action", _ACCOUNT_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize("platform_role", ["", "alkera_admin"], ids=["none", "admin"])
def test_account_owner_not_acting_directly_is_refused_with_a_code(
    action: Action, platform_role: str
) -> None:
    """An agent, a CLI token or an access key acting for the person: refused
    with a code the client can act on, whatever role the person holds."""
    attrs = {**ACCOUNT_ALLOW, "acting_directly": False, "platform_role": platform_role}
    _expect(
        authorize(AGENT, action, ACCOUNT_RESOURCE, attrs),
        effect=Effect.DENY,
        reason="not_acting_directly",
        policy=account_policy.POLICY,
        message=account_policy.DIRECT_MESSAGE,
        error_code=account_policy.DIRECT_CODE,
    )


@pytest.mark.parametrize("action", _ACCOUNT_ACTIONS, ids=lambda a: a.value)
def test_platform_admin_may_act_on_someone_elses_account(action: Action) -> None:
    attrs = {"is_self": False, "acting_directly": True, "platform_role": "alkera_admin"}
    _expect(
        authorize(OTHER_USER, action, ACCOUNT_RESOURCE, attrs),
        effect=Effect.ALLOW,
        reason="platform_admin",
        policy=account_policy.POLICY,
        message="",
    )


def test_platform_support_may_only_read_someone_elses_account() -> None:
    attrs = {"is_self": False, "acting_directly": True, "platform_role": "alkera_support"}
    _expect(
        authorize(OTHER_USER, Action.READ, ACCOUNT_RESOURCE, attrs),
        effect=Effect.ALLOW,
        reason="platform_support_reads",
        policy=account_policy.POLICY,
        message="",
    )


@pytest.mark.parametrize("action", [Action.EXPORT, Action.DELETE], ids=lambda a: a.value)
def test_platform_support_may_not_export_or_delete_someone_elses_account(action: Action) -> None:
    attrs = {"is_self": False, "acting_directly": True, "platform_role": "alkera_support"}
    _expect(
        authorize(OTHER_USER, action, ACCOUNT_RESOURCE, attrs),
        effect=Effect.DENY,
        reason="not_your_account",
        policy=account_policy.POLICY,
        message=account_policy.NOT_FOUND_MESSAGE,
        as_not_found=True,
    )


@pytest.mark.parametrize("action", _ACCOUNT_ACTIONS, ids=lambda a: a.value)
@pytest.mark.parametrize(
    "platform_role", ["", "alkera_admin_typo", "ALKERA_ADMIN"], ids=["none", "unknown", "case"]
)
def test_anyone_else_gets_an_opaque_not_found(action: Action, platform_role: str) -> None:
    """A role string that only resembles admin grants nothing."""
    attrs = {"is_self": False, "acting_directly": True, "platform_role": platform_role}
    _expect(
        authorize(OTHER_USER, action, ACCOUNT_RESOURCE, attrs),
        effect=Effect.DENY,
        reason="not_your_account",
        policy=account_policy.POLICY,
        message=account_policy.NOT_FOUND_MESSAGE,
        as_not_found=True,
    )


@pytest.mark.parametrize("action", _ACCOUNT_ACTIONS, ids=lambda a: a.value)
def test_platform_admin_through_a_token_cannot_act_on_someone_elses_account(
    action: Action,
) -> None:
    """Staff power over another person's account needs staff on their own session."""
    attrs = {"is_self": False, "acting_directly": False, "platform_role": "alkera_admin"}
    _expect(
        authorize(PAT, action, ACCOUNT_RESOURCE, attrs),
        effect=Effect.DENY,
        reason="not_acting_directly",
        policy=account_policy.POLICY,
        message=account_policy.NOT_FOUND_MESSAGE,
        as_not_found=True,
    )
