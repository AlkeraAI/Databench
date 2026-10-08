"""The closed vocabularies of the authorization model.

Every value here is a storage and wire contract (decision rows, role
assignments, actor documents), so the value sets are pinned literally — a
renamed member is a schema change, and this is where it gets noticed.
"""

from __future__ import annotations

from itertools import product

import pytest
from alkera_core.authz import (
    Action,
    CredentialKind,
    Effect,
    PrincipalKind,
    ResourceType,
    Role,
    ScopeKind,
    expand_roles,
)

#: What holding each role means holding — the ladder VIEWER < MEMBER < ADMIN <
#: OWNER, plus the two kind markers that imply only themselves.
IMPLIED: dict[Role, frozenset[Role]] = {
    Role.OWNER: frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER}),
    Role.ADMIN: frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER}),
    Role.MEMBER: frozenset({Role.MEMBER, Role.VIEWER}),
    Role.VIEWER: frozenset({Role.VIEWER}),
    Role.AGENT: frozenset({Role.AGENT}),
    Role.SERVICE: frozenset({Role.SERVICE}),
}


def test_implied_table_covers_every_role() -> None:
    assert set(IMPLIED) == set(Role)


@pytest.mark.parametrize(
    ("role", "other"),
    [pytest.param(r, o, id=f"{r.value}->{o.value}") for r, o in product(Role, Role)],
)
def test_role_implies_matches_the_ladder(role: Role, other: Role) -> None:
    assert role.implies(other) is (other in IMPLIED[role])


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        pytest.param(Role.VIEWER, Role.MEMBER, id="viewer-not-member"),
        pytest.param(Role.MEMBER, Role.ADMIN, id="member-not-admin"),
        pytest.param(Role.ADMIN, Role.OWNER, id="admin-not-owner"),
        pytest.param(Role.AGENT, Role.VIEWER, id="agent-not-viewer"),
        pytest.param(Role.SERVICE, Role.VIEWER, id="service-not-viewer"),
        pytest.param(Role.OWNER, Role.AGENT, id="owner-not-agent"),
        pytest.param(Role.OWNER, Role.SERVICE, id="owner-not-service"),
        pytest.param(Role.AGENT, Role.SERVICE, id="agent-not-service"),
    ],
)
def test_role_implication_never_climbs_or_crosses_kinds(lower: Role, higher: Role) -> None:
    assert lower.implies(higher) is False


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        pytest.param((), frozenset(), id="empty"),
        pytest.param((Role.VIEWER,), {Role.VIEWER}, id="viewer"),
        pytest.param((Role.MEMBER,), {Role.MEMBER, Role.VIEWER}, id="member"),
        pytest.param((Role.ADMIN,), {Role.ADMIN, Role.MEMBER, Role.VIEWER}, id="admin"),
        pytest.param((Role.OWNER,), {Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER}, id="owner"),
        pytest.param((Role.AGENT,), {Role.AGENT}, id="agent"),
        pytest.param((Role.SERVICE,), {Role.SERVICE}, id="service"),
        pytest.param(
            (Role.MEMBER, Role.AGENT), {Role.MEMBER, Role.VIEWER, Role.AGENT}, id="member+agent"
        ),
        pytest.param(
            (Role.VIEWER, Role.OWNER),
            {Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER},
            id="viewer+owner",
        ),
        pytest.param((Role.MEMBER, Role.MEMBER), {Role.MEMBER, Role.VIEWER}, id="duplicates"),
    ],
)
def test_expand_roles_closes_under_implies(held: tuple[Role, ...], expected: set[Role]) -> None:
    expanded = expand_roles(held)
    assert isinstance(expanded, frozenset)
    assert expanded == frozenset(expected)


def test_expand_roles_is_idempotent_and_accepts_any_iterable() -> None:
    once = expand_roles(role for role in (Role.ADMIN, Role.AGENT))
    assert expand_roles(once) == once
    assert expand_roles([Role.ADMIN, Role.AGENT]) == once
    assert expand_roles({Role.ADMIN, Role.AGENT}) == once


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        pytest.param(
            PrincipalKind, {"user", "agent", "service", "pat", "machine"}, id="PrincipalKind"
        ),
        pytest.param(
            CredentialKind,
            {"jwt", "ci_token", "proxy_token", "pat", "agent_header", "machine", "machine_worker"},
            id="CredentialKind",
        ),
        pytest.param(Role, {"owner", "admin", "member", "viewer", "agent", "service"}, id="Role"),
        pytest.param(ScopeKind, {"org", "team"}, id="ScopeKind"),
        pytest.param(
            ResourceType,
            {
                "connector",
                "billing_pool",
                "kb_item",
                "gate_run",
                "github_installation",
                "org",
                "org_audit",
                "team",
                "team_membership",
                "team_allocation",
                "team_storage_cap",
                "chat",
                "chat_template",
                "workspace",
                "artifact",
                "workspace_object",
                "compute_machine",
                "compute_grant",
                "org_machine",
                "compute_offering",
                "workspace_machine",
                "file_node",
                "file_lease",
                "notebook",
                "platform_ban",
                "platform_org_slack",
                "platform_org_storage",
                "platform_org_live_editing",
                "platform_org_sso_domains",
                "platform_machine",
                "machine_credential",
                "platform_billing",
                "account",
                "personal_box",
            },
            id="ResourceType",
        ),
        pytest.param(
            Action,
            {
                "read",
                "write",
                "admin",
                "create",
                "send",
                "export",
                "rerun",
                "upload_payload",
                "fetch_credential",
                "set_budget",
                "promote",
                "delete",
                "claim",
                "move",
                "rename",
                "list_connections",
                "lease_connection_credential",
                "answer_standing",
                "share",
                "restore",
                "lease",
                "lease_request",
                "lease_force",
                "snapshot",
                "lock",
                "hold",
                "comment",
                "copy",
                "run",
            },
            id="Action",
        ),
        pytest.param(Effect, {"allow", "deny"}, id="Effect"),
    ],
)
def test_vocabulary_values_are_pinned(enum: type[Role], values: set[str]) -> None:
    assert {member.value for member in enum} == values
    # StrEnum: the member IS its value, so a stored string compares equal.
    assert all(member == member.value for member in enum)
