"""The compute-reconciler bounds a deployment may tune.

These six decide what the reconciliation pass is allowed to DESTROY, which is
why each one has a validator and why the validators are pinned here rather than
taken on trust. The pass terminates pods at the provider and closes allocation
rows, and every one of these numbers is a guard on that:

- the prefix is the whole blast radius — it decides which machines at the
  provider this deployment believes are its own;
- the period, the grace and the unconfirmed-create window are the margins that
  keep a create still in flight from being read as an orphan;
- the two ceilings decide when a listing stops being evidence and how many
  machines one tick may reap.

Each is pinned four ways: the default is the figure the code shipped with, the
environment moves it, the edges of the bound are ACCEPTED, and a value outside
it is refused at construction — because a bound nothing enforces is a comment.
"""

from __future__ import annotations

import pytest
from alkera_core.config import Settings

#: ``field -> (default, env var, lowest legal, highest legal)``.
RANGED: dict[str, tuple[int, str, int, int]] = {
    "compute_reconcile_period_seconds": (60, "COMPUTE_RECONCILE_PERIOD_SECONDS", 30, 86_400),
    "compute_reconcile_grace_seconds": (600, "COMPUTE_RECONCILE_GRACE_SECONDS", 60, 86_400),
    "compute_unconfirmed_create_seconds": (900, "COMPUTE_UNCONFIRMED_CREATE_SECONDS", 60, 86_400),
}

#: ``field -> (default, env var, lowest legal)`` — a floor, with no ceiling.
FLOORED: dict[str, tuple[int, str, int]] = {
    "compute_reconcile_max_pods": (5_000, "COMPUTE_RECONCILE_MAX_PODS", 1),
    "compute_reconcile_max_terminations": (25, "COMPUTE_RECONCILE_MAX_TERMINATIONS", 1),
}

_NUMERIC: dict[str, tuple[int, str]] = {
    **{f: (d, e) for f, (d, e, _lo, _hi) in RANGED.items()},
    **{f: (d, e) for f, (d, e, _lo) in FLOORED.items()},
}

PREFIX_ENV = "COMPUTE_POD_NAME_PREFIX"
PREFIX_DEFAULT = "alkera"


def _settings(monkeypatch: pytest.MonkeyPatch, env_var: str, value: str) -> Settings:
    monkeypatch.setenv(env_var, value)
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", sorted(_NUMERIC), ids=sorted(_NUMERIC))
def test_the_default_is_the_figure_the_code_shipped_with(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that sets nothing keeps the behaviour it had before."""
    default, env_var = _NUMERIC[field]
    monkeypatch.delenv(env_var, raising=False)

    assert getattr(Settings(_env_file=None), field) == default


@pytest.mark.parametrize("field", sorted(_NUMERIC), ids=sorted(_NUMERIC))
def test_the_environment_moves_the_bound(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is the one an operator reads back, under the usual naming."""
    _, env_var = _NUMERIC[field]
    monkeypatch.delenv(env_var, raising=False)
    wanted = 1_234  # inside every one of these bounds

    assert getattr(_settings(monkeypatch, env_var, str(wanted)), field) == wanted


@pytest.mark.parametrize("field", sorted(RANGED), ids=sorted(RANGED))
def test_both_edges_of_a_range_are_legal(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is inclusive at both ends: an operator who reads the message
    and sets exactly what it names must not be refused."""
    _, env_var, low, high = RANGED[field]

    assert getattr(_settings(monkeypatch, env_var, str(low)), field) == low
    assert getattr(_settings(monkeypatch, env_var, str(high)), field) == high


@pytest.mark.parametrize("field", sorted(RANGED), ids=sorted(RANGED))
def test_a_value_outside_the_range_is_refused_at_either_end(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One below the floor and one above the ceiling, each refused by name.

    Zero is the one that matters most: a zero period is a Temporal interval of
    zero — not "never" but "as fast as the worker will take it" — and a zero
    grace reaps a machine somebody is still creating.
    """
    _, env_var, low, high = RANGED[field]

    for nonsense in (low - 1, high + 1, 0, -1):
        monkeypatch.setenv(env_var, str(nonsense))
        with pytest.raises(ValueError, match=env_var):
            Settings(_env_file=None)


@pytest.mark.parametrize("field", sorted(FLOORED), ids=sorted(FLOORED))
def test_the_floor_is_legal_and_anything_under_it_is_refused(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling of zero would make every listing read as truncated and every
    termination as over budget — a pass that runs for ever and does nothing,
    silently. That is the failure this floor exists to refuse."""
    _, env_var, low = FLOORED[field]

    assert getattr(_settings(monkeypatch, env_var, str(low)), field) == low
    for nonsense in (low - 1, -1):
        monkeypatch.setenv(env_var, str(nonsense))
        with pytest.raises(ValueError, match=env_var):
            Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# The prefix — the blast radius itself
# --------------------------------------------------------------------------- #


def test_the_prefix_default_is_the_name_the_code_shipped_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PREFIX_ENV, raising=False)

    assert Settings(_env_file=None).compute_pod_name_prefix == PREFIX_DEFAULT


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("alkera", id="the-default"),
        pytest.param("alkera-staging", id="the-sibling-deployment-the-docs-recommend"),
        pytest.param("acme", id="a-customers-own-name"),
        pytest.param("a1", id="digits-are-fine"),
        pytest.param("a-b-c", id="several-segments"),
    ],
)
def test_a_well_formed_prefix_is_accepted_and_moves_the_value(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _settings(monkeypatch, PREFIX_ENV, value).compute_pod_name_prefix == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty-claims-every-pod-named-dash-hex"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(" alkera", id="leading-space"),
        pytest.param("alkera ", id="trailing-space"),
        pytest.param("Alkera", id="upper-case-never-matches-our-own-pods"),
        pytest.param("alkera-", id="trailing-hyphen-doubles-the-separator"),
        pytest.param("-alkera", id="leading-hyphen"),
        pytest.param("alkera--staging", id="double-hyphen"),
        pytest.param("alkera_staging", id="underscore"),
        pytest.param("alkera staging", id="inner-space"),
    ],
)
def test_a_prefix_that_would_widen_or_miss_the_blast_radius_is_refused(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of these either claims machines that are not ours or fails to match
    the ones that are. An empty prefix is the worst of them: every pod named
    ``-<12 hex>`` on the account becomes a candidate for termination."""
    monkeypatch.setenv(PREFIX_ENV, value)

    with pytest.raises(ValueError, match=PREFIX_ENV):
        Settings(_env_file=None)
