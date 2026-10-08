"""The role ladder is the permission model's table, and it is data.

The first test transcribes A6's table cell by cell — an independent second
statement of the same contract, so a row edited in ``ladder.py`` without a
matching product decision fails here rather than shipping. The last two prove
the "as data" claim: a ladder with a rung the product has never heard of works
with no code change, and an unknown role never outranks a known one.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from alkera_core.files.authz.actions import FilesAction as A
from alkera_core.files.authz.ladder import (
    DEFAULT_LADDER,
    LADDER,
    ROLE_LABELS,
    RoleLadder,
    role_label,
)

#: A6's table, transcribed by hand: role → every action that role allows.
#: Written out rather than derived from LADDER so the two disagree loudly.
A6_TABLE: dict[str, set[A]] = {
    "reader": {A.READ, A.EXPORT, A.LEASE_REQUEST, A.COPY},
    "commenter": {A.READ, A.EXPORT, A.LEASE_REQUEST, A.COPY, A.COMMENT},
    "writer": {
        A.READ,
        A.EXPORT,
        A.LEASE_REQUEST,
        A.COPY,
        A.COMMENT,
        A.WRITE,
        A.RESTORE,
        A.LEASE,
        A.SNAPSHOT,
        A.LOCK,
    },
    "manager": {
        A.READ,
        A.EXPORT,
        A.LEASE_REQUEST,
        A.COPY,
        A.COMMENT,
        A.WRITE,
        A.RESTORE,
        A.LEASE,
        A.SNAPSHOT,
        A.LOCK,
        A.SHARE,
        A.LEASE_FORCE,
    },
    "owner": {
        A.READ,
        A.EXPORT,
        A.LEASE_REQUEST,
        A.COPY,
        A.COMMENT,
        A.WRITE,
        A.RESTORE,
        A.LEASE,
        A.SNAPSHOT,
        A.LOCK,
        A.SHARE,
        A.LEASE_FORCE,
        A.DELETE,
        A.HOLD,
    },
}


@pytest.mark.parametrize(
    ("role", "action"),
    [pytest.param(role, action, id=f"{role}-{action.value}") for role in A6_TABLE for action in A],
)
def test_every_cell_of_the_permission_table(role: str, action: A) -> None:
    assert DEFAULT_LADDER.allows(role, action) is (action in A6_TABLE[role])


@pytest.mark.parametrize("role", list(A6_TABLE), ids=list(A6_TABLE))
def test_actions_for_is_exactly_the_row(role: str) -> None:
    assert DEFAULT_LADDER.actions_for(role) == frozenset(A6_TABLE[role])


def test_the_ladder_is_monotone() -> None:
    """Each rung allows everything the rung below it does. A row that broke this
    would make a promotion take something away."""
    rows = [set(actions) for _, actions in LADDER]
    for lower, higher in pairwise(rows):
        assert lower < higher


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        pytest.param(["reader", "manager", "writer"], "manager", id="strongest-wins"),
        pytest.param(["owner", "reader"], "owner", id="owner-tops"),
        pytest.param([], None, id="no-grants"),
        pytest.param(["auditor"], None, id="unknown-only"),
        pytest.param(["auditor", "reader"], "reader", id="unknown-never-outranks"),
        pytest.param(["reader", "reader"], "reader", id="duplicates"),
    ],
)
def test_max_over_roles(roles: list[str], expected: str | None) -> None:
    assert DEFAULT_LADDER.max(roles) == expected


@pytest.mark.parametrize(
    "role", [pytest.param("auditor", id="unknown"), pytest.param(None, id="none")]
)
def test_an_unknown_role_allows_nothing(role: str | None) -> None:
    assert DEFAULT_LADDER.actions_for(role) == frozenset()
    assert DEFAULT_LADDER.rank(role) == -1
    assert not DEFAULT_LADDER.allows(role, A.READ)


def test_a_role_added_as_data_is_honoured_with_no_code_change() -> None:
    """The whole point of the table: a test-local ladder with an ``auditor``
    rung between reader and commenter behaves like a first-class role — the
    default ladder, which does not carry it, still refuses it."""
    table = (
        ("reader", frozenset({A.READ})),
        ("auditor", frozenset({A.READ, A.EXPORT})),
        ("commenter", frozenset({A.READ, A.EXPORT, A.COMMENT})),
    )
    ladder = RoleLadder(table)

    assert ladder.roles == ("reader", "auditor", "commenter")
    assert ladder.allows("auditor", A.EXPORT)
    assert not ladder.allows("auditor", A.COMMENT)
    assert ladder.max(["reader", "auditor"]) == "auditor"
    assert ladder.max(["auditor", "commenter"]) == "commenter"
    # The same string against the shipped ladder is simply not a rung.
    assert DEFAULT_LADDER.actions_for("auditor") == frozenset()


def test_a_ladder_that_names_a_role_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="names a role twice"):
        RoleLadder((("reader", frozenset()), ("reader", frozenset({A.READ}))))


def test_every_shipped_rung_has_the_product_label() -> None:
    """The labels are the ladder read aloud: one per rung, in the ladder's own
    order, so a rung added to the table without a label is caught here rather
    than rendering an internal word in a share dialog."""
    assert [ROLE_LABELS[role] for role in DEFAULT_LADDER.roles] == [
        "Can view",
        "Can comment",
        "Can edit",
        "Full access",
        "Owner",
    ]


def test_a_rung_with_no_label_falls_back_to_its_own_name() -> None:
    """A role an engine invents must still render as something a person can
    read, so the lookup degrades to the rung's own spelling rather than blank."""
    assert role_label("auditor") == "auditor"
    assert role_label("writer") == "Can edit"
