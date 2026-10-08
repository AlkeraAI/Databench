"""Authorization model: principals, policies, decisions, and the persisted
actor record.

Pure data and pure functions — nothing here touches a database, a clock or a
request object. The backend resolves credentials into an
:class:`ActingContext`, asks :func:`authorize` for a :class:`Decision`, and
persists the context as an :class:`ActorChainRecord` on every event it emits.
Importing this package registers every policy under :mod:`.policies`.
"""

from __future__ import annotations

from alkera_core.authz.actor_chain import (
    AGENT_ID_ASSERTED_BY_CLIENT,
    ActorChainRecord,
    PrincipalRecord,
)
from alkera_core.authz.chat_scope import (
    PUBLISHER_ROLE,
    SCOPE_ORG,
    SCOPE_PRIVATE,
    SCOPE_TEAM_PREFIX,
    SEND_ROLE,
    chat_doc_role,
    chat_readable,
    chat_writable,
    require_scope_team,
    role_may_send,
    scope_for_team,
    scope_readable,
    team_of_scope,
)
from alkera_core.authz.decision import (
    AUTHZ_EVENT_TYPE,
    BATCH_ENTITY_ID,
    MAX_RECORDED_REFUSALS,
    NEVER_AUDITED_KEYS,
    SERVER_ONLY_EVENT_PREFIXES,
    BatchDecisionEvent,
    Decision,
    DecisionEvent,
    allow,
    audited_attrs,
    deny,
    is_server_only_event_type,
)
from alkera_core.authz.engine import (
    MissingAttributeError,
    Policy,
    PolicyFn,
    authorize,
    policy_for,
    register,
    registered_policies,
    require_attr,
    temporarily_registered,
)
from alkera_core.authz.enums import (
    Action,
    CredentialKind,
    Effect,
    PrincipalKind,
    ResourceType,
    Role,
    ScopeKind,
    expand_roles,
)
from alkera_core.authz.headers import (
    AgentAssertion,
    AgentHeaderError,
    agent_headers,
    parse_agent_assertion,
)
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.authz.resource import Resource
from alkera_core.authz.roles import (
    AssignmentRow,
    ChainLoader,
    MembershipRow,
    RoleResolver,
    TeamRoles,
    merge_roles,
)

# Imported last, on purpose: registering a policy needs the engine, and every
# policy module reaches back into the modules above.
from alkera_core.authz import policies  # isort: skip

__all__ = [
    "AGENT_ID_ASSERTED_BY_CLIENT",
    "AUTHZ_EVENT_TYPE",
    "BATCH_ENTITY_ID",
    "MAX_RECORDED_REFUSALS",
    "NEVER_AUDITED_KEYS",
    "PUBLISHER_ROLE",
    "SCOPE_ORG",
    "SCOPE_PRIVATE",
    "SCOPE_TEAM_PREFIX",
    "SEND_ROLE",
    "SERVER_ONLY_EVENT_PREFIXES",
    "ActingContext",
    "Action",
    "ActorChainRecord",
    "AgentAssertion",
    "AgentHeaderError",
    "AssignmentRow",
    "BatchDecisionEvent",
    "ChainLoader",
    "CredentialKind",
    "Decision",
    "DecisionEvent",
    "Effect",
    "MembershipRow",
    "MissingAttributeError",
    "Policy",
    "PolicyFn",
    "Principal",
    "PrincipalKind",
    "PrincipalRecord",
    "Resource",
    "ResourceType",
    "Role",
    "RoleResolver",
    "ScopeKind",
    "TeamRoles",
    "agent_headers",
    "allow",
    "audited_attrs",
    "authorize",
    "chat_doc_role",
    "chat_readable",
    "chat_writable",
    "deny",
    "expand_roles",
    "is_server_only_event_type",
    "merge_roles",
    "parse_agent_assertion",
    "policies",
    "policy_for",
    "register",
    "registered_policies",
    "require_attr",
    "require_scope_team",
    "role_may_send",
    "scope_for_team",
    "scope_readable",
    "team_of_scope",
    "temporarily_registered",
]
