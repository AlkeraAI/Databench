"""The version tag a cap slot carries, and how an ``If-Match`` is read against it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.cap_versions import UNSET_VERSION, cap_version, version_matches

T0 = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)


def test_an_empty_slot_has_the_unset_tag() -> None:
    assert cap_version(None, None) == UNSET_VERSION
    assert cap_version(None, T0) == UNSET_VERSION


def test_a_tag_moves_with_the_figure_and_with_the_write() -> None:
    same = cap_version(4_000_000_000, T0)
    assert cap_version(4_000_000_000, T0) == same
    assert cap_version(6_000_000_000, T0) != same, "a different figure is a different reading"
    assert cap_version(4_000_000_000, T0 + timedelta(microseconds=1)) != same, (
        "the same figure written again is a different reading"
    )
    assert same != UNSET_VERSION and len(same) == 16


def test_a_tag_is_opaque_and_never_carries_the_figure() -> None:
    assert "4000000000" not in cap_version(4_000_000_000, T0)


@pytest.mark.parametrize(
    ("expected", "current", "matches"),
    [
        pytest.param(None, "abc", True, id="no-header-is-unconditional"),
        pytest.param("*", "abc", True, id="wildcard-is-unconditional"),
        pytest.param("abc", "abc", True, id="the-same-tag"),
        pytest.param('"abc"', "abc", True, id="a-quoted-entity-tag"),
        pytest.param('W/"abc"', "abc", True, id="a-weak-entity-tag"),
        pytest.param("  abc  ", "abc", True, id="surrounding-space"),
        pytest.param("abd", "abc", False, id="another-tag"),
        pytest.param(
            UNSET_VERSION, "abc", False, id="a-reading-of-an-empty-slot-that-since-filled"
        ),
        pytest.param("abc", UNSET_VERSION, False, id="a-reading-of-a-slot-since-emptied"),
        pytest.param("", "abc", False, id="an-empty-header-is-not-a-match"),
    ],
)
def test_version_matches(expected: str | None, current: str, matches: bool) -> None:
    assert version_matches(expected, current) is matches
