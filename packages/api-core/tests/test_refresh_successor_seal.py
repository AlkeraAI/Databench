"""The seal a rotated refresh token keeps its successor under.

A concurrent re-presentation of a rotated token is handed the same successor,
so the parent row holds the successor's raw value. It must open only for the
parent's own raw value and only on the parent's own row: a database read, or a
seal copied onto another row, yields nothing.
"""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest
from alkera_core.auth.refresh import open_successor, seal_successor


def _raw() -> str:
    return secrets.token_urlsafe(32)


def test_the_parent_opens_its_successor() -> None:
    parent, child, row = _raw(), _raw(), uuid4()
    assert open_successor(parent, row, seal_successor(parent, row, child)) == child


def test_the_seal_does_not_carry_the_successor_in_the_clear() -> None:
    parent, child, row = _raw(), _raw(), uuid4()
    assert child not in seal_successor(parent, row, child)


def test_two_seals_of_one_successor_differ() -> None:
    """A fresh nonce per seal: equal successors never produce equal ciphertext."""
    parent, child, row = _raw(), _raw(), uuid4()
    assert seal_successor(parent, row, child) != seal_successor(parent, row, child)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("another-presenter", id="a-different-raw-token"),
        pytest.param("another-row", id="the-seal-moved-to-another-row"),
        pytest.param("tampered", id="one-byte-flipped"),
        pytest.param("truncated", id="truncated"),
        pytest.param("not-base64", id="garbage"),
    ],
)
def test_the_seal_opens_for_nothing_else(case: str) -> None:
    parent, child, row = _raw(), _raw(), uuid4()
    sealed = seal_successor(parent, row, child)
    presenter, at = parent, row
    if case == "another-presenter":
        presenter = _raw()
    elif case == "another-row":
        at = uuid4()
    elif case == "tampered":
        flipped = "A" if sealed[20] != "A" else "B"
        sealed = sealed[:20] + flipped + sealed[21:]
    elif case == "truncated":
        sealed = sealed[:16]
    else:
        sealed = "%%% not a seal %%%"
    assert open_successor(presenter, at, sealed) is None
