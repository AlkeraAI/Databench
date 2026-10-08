"""Retry policy per activity is explicit data: every activity has one, the
non-idempotent ones never retry, and the transient / permanent split the
interceptor relies on is pinned."""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

import httpx
import pytest
from alkera_core.temporal import COMPANION_ACTIVITIES, QUEUE_FOR, WorkflowType
from sqlalchemy.exc import InterfaceError, OperationalError
from worker.tasks._hardening import is_transient_error
from worker.temporal.retry import (
    ACTIVITY_POLICIES,
    FILES_NON_RETRYABLE,
    FILES_RETRY,
    NO_RETRY,
    TRANSIENT_RETRY,
    ActivityPolicy,
    merge_policies,
    policy_for,
)

#: Every activity the worker serves: each workflow's namesake, the platform's and
#: every registered family's, plus the companions that are a second step of one.
ALL_ACTIVITY_TYPES = {str(t) for t in QUEUE_FOR} | set(COMPANION_ACTIVITIES)
#: The platform's own. A registered family pins its rows beside its code.
OPEN_ACTIVITY_TYPES = {t.value for t in WorkflowType} | set(COMPANION_ACTIVITIES)

# Activities a second attempt could duplicate (GitHub check-run creation) or where a
# retry buys nothing (predicate deletes, a log line). Spelled out, not derived.
NEVER_RETRIED = {
    "auth.prune_expired_tokens",
    "auth.prune_expired_device_codes",
    "auth.prune_login_lockouts",
    "entitlements.watchdog",
    "compute.sweep",
    # Creating a machine is not idempotent at every provider; each machine is
    # committed before its create, and the thirty-second tick is the retry.
    "compute.org_machine_reconcile",
    "chat.reap_spares",
    "workspace.reconcile",
    "files.janitor",
    # A retried pass would offer every abandoned operation a second time and
    # count a second attempt against each, spending an operation's whole
    # allowance on one unlucky minute. Its thirty-second tick is the recovery.
    "files.recover_queued",
    # Each erasure commits whole or writes nothing; a blocked one is recorded
    # and the next fifteen-minute pass is its retry.
    "account.lifecycle_sweep",
}
ROW_VISIBILITY: set[str] = set()
# The transient budget, minus the refusals a second attempt cannot change.
FILES_TYPED = {
    "files.promote",
    "files.acl_rewrite",
    "files.large_move",
    "files.copy",
    "files.bulk",
}
# How long each heartbeating activity may go quiet. Two minutes is the fleet
# pass's figure — a pass that dies a hundred rows in is noticed in minutes
# rather than at its deadline. ONE verification is a different shape: the whole
# attempt is under a minute, so two minutes could never fire inside it, and its
# timeout is sized to the pulse the dial makes while it is out instead.
HEARTBEATING = {
    "workspace.finish_deletions": timedelta(minutes=2),
    "compute.meter": timedelta(minutes=2),
    "compute.reconcile": timedelta(minutes=2),
    "files.gc": timedelta(minutes=2),
    "files.promote": timedelta(minutes=2),
    "files.acl_rewrite": timedelta(minutes=2),
    "files.large_move": timedelta(minutes=2),
    "files.copy": timedelta(minutes=2),
    "files.bulk": timedelta(minutes=2),
    "auth.prune_identity_security_events": timedelta(minutes=2),
}


def test_transient_retry_matches_the_legacy_budget() -> None:
    """One try plus five retries, doubling from 1 s and capped at 5 min."""
    assert TRANSIENT_RETRY.initial_interval == timedelta(seconds=1)
    assert TRANSIENT_RETRY.backoff_coefficient == 2.0
    assert TRANSIENT_RETRY.maximum_interval == timedelta(seconds=300)
    assert TRANSIENT_RETRY.maximum_attempts == 6


def test_no_retry_is_exactly_one_attempt() -> None:
    assert NO_RETRY.maximum_attempts == 1


def test_every_activity_has_a_policy_and_nothing_else_does() -> None:
    assert set(ACTIVITY_POLICIES) == ALL_ACTIVITY_TYPES
    assert OPEN_ACTIVITY_TYPES <= ALL_ACTIVITY_TYPES


def test_the_pinned_sets_name_real_activities() -> None:
    """Guard the guards: a renamed activity must fail here, not silently drop out."""
    assert NEVER_RETRIED <= ALL_ACTIVITY_TYPES
    assert ROW_VISIBILITY <= ALL_ACTIVITY_TYPES
    assert set(HEARTBEATING) <= ALL_ACTIVITY_TYPES
    assert NEVER_RETRIED.isdisjoint(ROW_VISIBILITY)
    assert NEVER_RETRIED.isdisjoint(HEARTBEATING)
    assert FILES_TYPED <= ALL_ACTIVITY_TYPES
    assert FILES_TYPED.isdisjoint(NEVER_RETRIED)


@pytest.mark.parametrize("activity_type", sorted(OPEN_ACTIVITY_TYPES))
def test_only_the_non_idempotent_activities_never_retry(activity_type: str) -> None:
    policy = ACTIVITY_POLICIES[activity_type]
    if activity_type in NEVER_RETRIED:
        assert policy.retry is NO_RETRY
        assert policy.retry.maximum_attempts == 1
    elif activity_type in ROW_VISIBILITY:
        # A nudge that outran its row's commit: four quick, flat attempts.
        assert policy.retry.maximum_attempts == 4
        assert policy.retry.backoff_coefficient == 1.0
    elif activity_type in FILES_TYPED:
        assert policy.retry is FILES_RETRY
        assert policy.retry.maximum_attempts == TRANSIENT_RETRY.maximum_attempts
        assert policy.retry.non_retryable_error_types == list(FILES_NON_RETRYABLE)
    else:
        assert policy.retry is TRANSIENT_RETRY
        assert policy.retry.maximum_attempts > 1


@pytest.mark.parametrize("activity_type", sorted(OPEN_ACTIVITY_TYPES))
def test_heartbeats_exactly_where_a_long_pass_needs_one(activity_type: str) -> None:
    policy = ACTIVITY_POLICIES[activity_type]
    if activity_type in HEARTBEATING:
        assert policy.heartbeat_timeout == HEARTBEATING[activity_type]
        assert policy.heartbeat_timeout < policy.start_to_close
    else:
        assert policy.heartbeat_timeout is None


@pytest.mark.parametrize("activity_type", sorted(OPEN_ACTIVITY_TYPES))
def test_every_activity_has_a_bounded_positive_timeout(activity_type: str) -> None:
    """Bounded, and never a full hour: the longest schedule interval any sweep
    runs on is hourly, so a budget of an hour or more would let runs stack."""
    policy = ACTIVITY_POLICIES[activity_type]
    assert timedelta(0) < policy.start_to_close < timedelta(hours=1)


@pytest.mark.parametrize(
    ("activity_type", "start_to_close"),
    [
        pytest.param("deployment_health.run", timedelta(seconds=60), id="health-backstop"),
        pytest.param("entitlements.watchdog", timedelta(seconds=30), id="watchdog"),
    ],
)
def test_timeouts_are_pinned(activity_type: str, start_to_close: timedelta) -> None:
    assert ACTIVITY_POLICIES[activity_type].start_to_close == start_to_close


INBOX_DRAINS = {
    "workspace.finish_deletions",
}


@pytest.mark.parametrize("activity_type", sorted(OPEN_ACTIVITY_TYPES))
def test_only_the_inbox_drains_carry_a_schedule_to_close(activity_type: str) -> None:
    """The drains are also nudged into runs that have no execution timeout; a
    bound on the whole attempt sequence keeps such a run from waiting on a
    missing worker forever. It is the hour a scheduled run already gets, so a
    scheduled run behaves exactly as before. Nothing else is bounded that way —
    the email dispatch singleton in particular waits for a worker as long as
    it takes."""
    policy = ACTIVITY_POLICIES[activity_type]
    if activity_type in INBOX_DRAINS:
        assert policy.schedule_to_close == timedelta(hours=1)
        assert policy.schedule_to_close > policy.start_to_close
    else:
        assert policy.schedule_to_close is None


def test_policy_for_names_the_missing_activity() -> None:
    assert policy_for("files.gc") is ACTIVITY_POLICIES["files.gc"]
    with pytest.raises(KeyError, match=r"no retry policy declared for activity 'files\.nope'"):
        policy_for("files.nope")


def test_the_table_and_its_rows_are_immutable() -> None:
    assert isinstance(ACTIVITY_POLICIES, MappingProxyType)
    with pytest.raises(TypeError):
        ACTIVITY_POLICIES["x"] = ActivityPolicy(NO_RETRY, timedelta(seconds=1))  # type: ignore[index]
    with pytest.raises(AttributeError):
        ACTIVITY_POLICIES["files.gc"].retry = NO_RETRY  # type: ignore[misc]


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(OperationalError("SELECT 1", {}, Exception("dropped")), id="db-operational"),
        pytest.param(InterfaceError("SELECT 1", {}, Exception("closed")), id="db-interface"),
        pytest.param(httpx.ConnectError("refused"), id="httpx-transport"),
    ],
)
def test_transient_errors_are_left_for_temporal_to_retry(exc: Exception) -> None:
    assert is_transient_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("poison"), id="programming-error"),
        pytest.param(KeyError("missing"), id="key-error"),
    ],
)
def test_permanent_errors_are_classified_as_poison(exc: Exception) -> None:
    assert is_transient_error(exc) is False


def test_a_family_declaring_an_activity_the_table_already_has_is_refused() -> None:
    own = {"files.gc": ActivityPolicy(NO_RETRY, timedelta(seconds=1))}
    theirs = {"files.gc": ActivityPolicy(TRANSIENT_RETRY, timedelta(seconds=2))}
    with pytest.raises(ValueError, match=r"files\.gc"):
        merge_policies(own, (theirs,))


def test_registered_sets_join_the_open_rows_read_only() -> None:
    own = {"a": ActivityPolicy(NO_RETRY, timedelta(seconds=1))}
    theirs = {"b": ActivityPolicy(TRANSIENT_RETRY, timedelta(seconds=2))}
    merged = merge_policies(own, (theirs,))
    assert dict(merged) == {**own, **theirs}
    assert isinstance(merged, MappingProxyType)
