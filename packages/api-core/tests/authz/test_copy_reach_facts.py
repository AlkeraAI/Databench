"""The two facts that tell a copy from a read.

Both are the decider's because both are about things the policy cannot see: the
folders ABOVE the node, and the grants that produced the caller's read. The
policy is a table over facts, so if these are computed wrongly the refusal is
wrong no matter how careful the branch is — which is why they get their own
table here, driven with plain rows and no database.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files.authz.decider import (
    NO_RESHARE_BIT,
    AccessFacts,
    LadderDecider,
    effective_role,
)
from alkera_core.files.authz.grants import Grant
from alkera_core.files.authz.grants import Principal as GrantPrincipal
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode

ORG = uuid.UUID("00000000-0000-4000-8000-00000000000a")
TEAM = uuid.UUID("00000000-0000-4000-8000-00000000000b")
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
EARLIER = NOW - timedelta(hours=1)

CTX = ActingContext(
    acting_principal=ActingPrincipal(
        kind=PrincipalKind.USER, id=str(USER_ID), org_id=ORG, credential=CredentialKind.JWT
    )
)


def _node(*, flags: int = 0, traversal_only: bool = False) -> FileNode:
    """A row the decider can read, built in memory: column defaults never run
    without a session, so every field a branch touches is spelled here."""
    node = FileNode()
    node.id = uuid.uuid4()
    node.org_team_id = ORG
    node.flags = flags
    node.state = "active"
    node.traversal_only = traversal_only
    return node


def _drive() -> FileDrive:
    drive = FileDrive()
    drive.id = uuid.uuid4()
    drive.org_team_id = ORG
    drive.frozen_reason = None
    return drive


def _grant(
    role: str = "reader",
    *,
    expires_at: datetime | None = None,
) -> Grant:
    return Grant(
        principal=GrantPrincipal(kind="user", id=USER_ID), role=role, expires_at=expires_at
    )


def _team_grant(role: str = "reader", *, conditions: dict[str, object] | None = None) -> Grant:
    """A grant to a team the caller is on and administers, so a ``team_role``
    condition is one the decider can actually evaluate as holding."""
    return Grant(principal=GrantPrincipal(kind="team", id=TEAM), role=role, conditions=conditions)


def _decide(
    *,
    node: FileNode | None = None,
    chain: Sequence[FileNode] = (),
    grants: Sequence[Grant] = (),
    org_admin: bool = False,
) -> object:
    subject = node if node is not None else _node()
    facts = AccessFacts(
        team_ids=frozenset({TEAM}),
        team_admin_ids=frozenset({TEAM}),
        org_admin=org_admin,
        now=NOW,
    )
    return effective_role(CTX, subject, chain, grants, _drive(), facts, decider=LadderDecider())


# --------------------------------------------------------------------------
# no_reshare_chain
# --------------------------------------------------------------------------


def test_the_node_s_own_no_reshare_is_on_the_chain() -> None:
    assert _decide(node=_node(flags=NO_RESHARE_BIT), grants=[_grant()]).no_reshare_chain is True


def test_an_ancestor_s_no_reshare_is_on_the_chain() -> None:
    """The whole point of the fact: the flag does not descend onto the rows
    beneath it, so a file inside a folder marked "do not pass this on" answers
    for the folder or the marker means nothing."""
    parent = _node(flags=NO_RESHARE_BIT)
    assert _decide(chain=(parent, _node()), grants=[_grant()]).no_reshare_chain is True


def test_a_clean_chain_carries_nothing() -> None:
    assert _decide(chain=(_node(), _node()), grants=[_grant()]).no_reshare_chain is False


def test_the_chain_fact_does_not_remove_an_action() -> None:
    """It is a fact for the policy, not a narrowing: the flag's own effect
    (no SHARE) is still the decider's, and everything else survives."""
    access = _decide(chain=(_node(flags=NO_RESHARE_BIT),), grants=[_grant("writer")])
    assert "read" in access.allowed_actions
    assert "write" in access.allowed_actions
    assert "copy" in access.allowed_actions


# --------------------------------------------------------------------------
# read_via_conditional_grant
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grants",
    [
        pytest.param([_grant(expires_at=LATER)], id="one-expiring-grant"),
        pytest.param([_team_grant(conditions={"team_role": "admin"})], id="one-conditional-grant"),
        pytest.param(
            [_grant(expires_at=LATER), _grant("writer", expires_at=LATER)],
            id="two-expiring-grants",
        ),
    ],
)
def test_a_read_that_rests_only_on_temporary_grants_is_on_loan(grants: list[Grant]) -> None:
    access = _decide(grants=grants)
    assert "read" in access.allowed_actions
    assert access.read_via_conditional_grant is True


@pytest.mark.parametrize(
    "grants",
    [
        pytest.param([_grant()], id="one-plain-grant"),
        pytest.param([_grant(), _grant("writer", expires_at=LATER)], id="plain-beside-expiring"),
        pytest.param([_grant(expires_at=LATER), _grant()], id="expiring-beside-plain"),
    ],
)
def test_one_plain_grant_settles_it(grants: list[Grant]) -> None:
    """A caller holding a permanent read and a temporary one is reading on the
    permanent one; the temporary grant lapsing changes nothing they can see."""
    access = _decide(grants=grants)
    assert "read" in access.allowed_actions
    assert access.read_via_conditional_grant is False


def test_a_grant_that_has_already_lapsed_is_not_what_the_caller_reads_on() -> None:
    """An expired grant is not a candidate at all, so it cannot make a plain
    read look borrowed — nor, on its own, produce a read to borrow."""
    assert _decide(grants=[_grant(expires_at=EARLIER), _grant()]).read_via_conditional_grant is (
        False
    )
    lapsed = _decide(grants=[_grant(expires_at=EARLIER)])
    assert "read" not in lapsed.allowed_actions
    assert lapsed.read_via_conditional_grant is False


def test_an_unreadable_condition_drops_the_grant_rather_than_lending_it() -> None:
    """A condition the decider cannot evaluate fails closed: no candidacy, no
    read, and nothing on loan either."""
    refused = _decide(grants=[_team_grant(conditions={"a_clause_this_build_does_not_model": True})])
    assert "read" not in refused.allowed_actions
    assert refused.read_via_conditional_grant is False


def test_an_org_admin_s_read_is_never_on_loan() -> None:
    """Their read comes from the descent rule, which no grant's expiry reaches."""
    assert _decide(
        grants=[_grant(expires_at=LATER)], org_admin=True
    ).read_via_conditional_grant is (False)


def test_a_signpost_read_is_never_on_loan() -> None:
    """The traversal-only rule hands every member READ on the containers that
    carry no grants, so there is no grant for it to rest on."""
    access = _decide(node=_node(traversal_only=True), grants=[_grant(expires_at=LATER)])
    assert "read" in access.allowed_actions
    assert access.read_via_conditional_grant is False


def test_no_grants_at_all_is_not_a_loan() -> None:
    access = _decide()
    assert "read" not in access.allowed_actions
    assert access.read_via_conditional_grant is False


def test_a_node_in_another_org_answers_both_facts_at_their_closed_value() -> None:
    stranger = _node()
    stranger.org_team_id = uuid.UUID("00000000-0000-4000-8000-00000000000f")
    access = _decide(node=stranger, grants=[_grant(expires_at=LATER)])
    assert access.in_org is False
    assert access.no_reshare_chain is False
    assert access.read_via_conditional_grant is False
