"""What the UI sees: booleans and a reason, never a role.

The refusal reason is the point of this module — a greyed-out button that says
"this folder is held" is a different product from one that just does nothing —
so every flag has a case that pins the reason it produces.
"""

from __future__ import annotations

import pytest
from alkera_core.files.authz.actions import FilesAction as A
from alkera_core.files.authz.capabilities import (
    REASON_INSUFFICIENT_ROLE,
    REASON_NO_ROLE,
    REASON_NOT_IN_ORG,
    capabilities,
)
from alkera_core.files.authz.decider import EffectiveAccess, NodeFlag
from alkera_core.files.authz.ladder import DEFAULT_LADDER

ROLES = ("reader", "commenter", "writer", "manager", "owner")


def _access(
    role: str | None, *, flags: frozenset[str] = frozenset(), in_org: bool = True
) -> EffectiveAccess:
    actions = frozenset(a.value for a in DEFAULT_LADDER.actions_for(role))
    return EffectiveAccess(role=role, allowed_actions=actions, flags=flags, in_org=in_org)


@pytest.mark.parametrize("role", ROLES, ids=ROLES)
def test_a_capability_is_true_exactly_when_the_ladder_allows_the_action(role: str) -> None:
    caps = capabilities(_access(role))

    for action in A:
        assert caps.can(action) is DEFAULT_LADDER.allows(role, action)


def test_the_wire_shape_names_every_action() -> None:
    shape = capabilities(_access("writer")).as_dict()

    assert len(shape) == len(list(A))
    assert shape["canRead"] is True
    assert shape["canShare"] is False
    assert shape["canLeaseForce"] is False
    assert shape["canLeaseRequest"] is True


def test_no_role_name_appears_anywhere_in_the_capability_map() -> None:
    """The hygiene rule made concrete: a role string must not reach the UI."""
    caps = capabilities(_access("owner"))

    rendered = repr(caps.as_dict()) + repr(caps.refusals)
    for role in DEFAULT_LADDER.roles:
        assert role not in rendered


@pytest.mark.parametrize(
    ("flag", "action"),
    [
        pytest.param(NodeFlag.NO_DOWNLOAD, A.EXPORT, id="no_download-export"),
        pytest.param(NodeFlag.NO_RESHARE, A.SHARE, id="no_reshare-share"),
        pytest.param(NodeFlag.HELD, A.WRITE, id="held-write"),
        pytest.param(NodeFlag.HELD, A.DELETE, id="held-delete"),
        pytest.param(NodeFlag.LOCKED, A.WRITE, id="locked-write"),
    ],
)
def test_a_flag_that_removed_an_action_is_named_as_the_reason(flag: NodeFlag, action: A) -> None:
    allowed = frozenset(a.value for a in DEFAULT_LADDER.actions_for("owner") if a is not action)
    access = EffectiveAccess(
        role="owner", allowed_actions=allowed, flags=frozenset({flag.value}), in_org=True
    )

    assert capabilities(access).refusals[action.value] == flag.value


def test_frozen_is_the_reason_for_every_write_it_removed() -> None:
    access = EffectiveAccess(
        role="owner",
        allowed_actions=frozenset(
            {A.READ.value, A.EXPORT.value, A.LEASE_REQUEST.value, A.COPY.value}
        ),
        flags=frozenset({NodeFlag.FROZEN.value}),
        in_org=True,
    )

    refusals = capabilities(access).refusals

    assert refusals[A.WRITE.value] == NodeFlag.FROZEN.value
    assert refusals[A.DELETE.value] == NodeFlag.FROZEN.value
    assert A.READ.value not in refusals


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        pytest.param(
            EffectiveAccess(None, frozenset(), frozenset(), in_org=False),
            REASON_NOT_IN_ORG,
            id="another-org",
        ),
        pytest.param(_access(None), REASON_NO_ROLE, id="no-grant"),
        pytest.param(_access("reader"), REASON_INSUFFICIENT_ROLE, id="rung-too-low"),
    ],
)
def test_the_reason_when_no_flag_is_involved(access: EffectiveAccess, expected: str) -> None:
    assert capabilities(access).refusals[A.WRITE.value] == expected


def test_an_allowed_action_has_no_refusal_entry() -> None:
    caps = capabilities(_access("owner"))

    assert caps.refusals == {}


def test_two_identical_capability_maps_compare_equal() -> None:
    """They ride on item payloads, so a listing can dedupe them."""
    assert capabilities(_access("writer")) == capabilities(_access("writer"))
    assert capabilities(_access("writer")) != capabilities(_access("reader"))
    assert hash(capabilities(_access("writer"))) == hash(capabilities(_access("writer")))
