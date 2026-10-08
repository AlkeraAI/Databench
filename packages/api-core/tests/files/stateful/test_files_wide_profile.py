"""The nightly profile widens the reference-model machines, and never narrows them.

Hypothesis resolves an explicitly-set setting ahead of the active profile, and
every machine in this package pins ``max_examples`` on its ``TestCase`` — so
``--hypothesis-profile wide`` on its own would buy the nightly job nothing at
all. That is what :func:`widen_to_profile` exists to fix, and what these cases
hold it to on both sides: the nightly profile raises a narrow budget, and every
other profile leaves the committed budget exactly as written.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from hypothesis import settings as hypothesis_settings
from tests.files.stateful.conftest import WIDE_PROFILE, widen_to_profile


@pytest.fixture
def under_nightly_profile() -> Iterator[None]:
    """Run the body with the nightly profile active, then restore the one in force."""
    previous = hypothesis_settings._current_profile
    hypothesis_settings.load_profile(WIDE_PROFILE)
    try:
        yield
    finally:
        hypothesis_settings.load_profile(previous)


def test_the_nightly_profile_asks_for_the_budget_the_acl_harness_bug_needed() -> None:
    profile = hypothesis_settings.get_profile(WIDE_PROFILE)
    assert (profile.max_examples, profile.stateful_step_count) == (150, 12)


@pytest.mark.parametrize(
    ("committed", "expected"),
    [
        pytest.param((5, 8), (150, 12), id="acl-budget-raised-on-both-axes"),
        pytest.param((8, 14), (150, 14), id="lease-keeps-its-longer-step-count"),
        pytest.param((30, 40), (150, 40), id="namespace-keeps-its-deeper-runs"),
        pytest.param((150, 12), (150, 12), id="a-machine-exactly-at-the-budget-is-stable"),
        pytest.param((400, 60), (400, 60), id="a-machine-already-wider-is-left-alone"),
    ],
)
def test_the_nightly_profile_is_a_floor_and_never_a_ceiling(
    under_nightly_profile: None,
    committed: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    widened = widen_to_profile(
        hypothesis_settings(max_examples=committed[0], stateful_step_count=committed[1])
    )
    assert (widened.max_examples, widened.stateful_step_count) == expected


@pytest.mark.parametrize(
    "committed",
    [
        pytest.param((5, 8), id="acl"),
        pytest.param((8, 14), id="lease"),
        pytest.param((30, 40), id="namespace"),
    ],
)
def test_every_other_profile_leaves_the_committed_budget_alone(
    committed: tuple[int, int],
) -> None:
    """The default gate must not silently inherit the nightly cost."""
    assert hypothesis_settings.default is not hypothesis_settings.get_profile(WIDE_PROFILE)
    widened = widen_to_profile(
        hypothesis_settings(max_examples=committed[0], stateful_step_count=committed[1])
    )
    assert (widened.max_examples, widened.stateful_step_count) == committed
