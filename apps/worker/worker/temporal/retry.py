"""Retry policy per activity — explicit, exhaustive, and data only.

Temporal retries an activity according to the policy the workflow attaches
when it schedules it. That is the right place for the decision, because it
makes "may this job run twice?" a per-activity fact an operator can read here
instead of a property of how a worker happened to die:

- ``TRANSIENT_RETRY`` for idempotent work (advisory-locked sweeps, the inbox
  drains keyed by event id, ledger writes keyed by a stable ref). Six attempts
  with doubling backoff capped at five minutes.
- ``NO_RETRY`` where a second attempt could duplicate an external effect
  (GitHub check-run creation is not idempotent) or where a retry buys nothing
  (a predicate ``DELETE``, a pure log line). Their schedules are the recovery.
Temporal adds no jitter; the advisory lock absorbs synchronized retries. The
``start_to_close`` timeouts also bound every job's runtime, which the retired
task runner never did.

A job family a private extension registers (``worker.temporal.queues``) brings
the policies of its activities with it into :data:`ACTIVITY_POLICY_SETS`; the
table a workflow reads, :data:`ACTIVITY_POLICIES`, is the open rows below plus
every registered set, built on first read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from types import MappingProxyType

from alkera_core.extensions import ExtensionPoint
from alkera_core.temporal import RECOVER_SETTLE_ACTIVITY, WorkflowType
from temporalio.common import RetryPolicy

TRANSIENT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=300),
    maximum_attempts=6,
)
"""One try plus five retries."""

NO_RETRY = RetryPolicy(maximum_attempts=1)

FILES_NON_RETRYABLE: tuple[str, ...] = (
    "ChecksumMismatch",
    "QuotaExceeded",
    "Denied",
    "InvalidRequest",
)
"""The Files refusals a second attempt cannot change: the bytes do not match
the checksum the client declared, the org is over quota, the caller has lost
the authorization the upload opened under, or the request was never valid. The
activity raises each as an ``ApplicationError`` of that type name, so Temporal
fails the operation once instead of six times while a client waits."""

FILES_RETRY = RetryPolicy(
    initial_interval=TRANSIENT_RETRY.initial_interval,
    backoff_coefficient=TRANSIENT_RETRY.backoff_coefficient,
    maximum_interval=TRANSIENT_RETRY.maximum_interval,
    maximum_attempts=TRANSIENT_RETRY.maximum_attempts,
    non_retryable_error_types=list(FILES_NON_RETRYABLE),
)
"""The transient budget, minus the refusals that are facts about the upload."""


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    """How a workflow schedules one activity type."""

    retry: RetryPolicy
    start_to_close: timedelta
    heartbeat_timeout: timedelta | None = None
    schedule_to_close: timedelta | None = None
    """A bound on the whole activity, queue wait and every retry included.
    Set where the workflow that schedules the activity may have no execution
    timeout of its own (a nudged singleton drain), so a worker that never
    picks the activity up ends the run instead of holding it open forever."""


MINUTE = timedelta(minutes=1)


def activity_policy(
    retry: RetryPolicy,
    start_to_close: timedelta,
    heartbeat: timedelta | None = None,
    schedule_to_close: timedelta | None = None,
) -> ActivityPolicy:
    """A policy row: the retry, the start-to-close bound and the optional
    heartbeat and schedule-to-close bounds."""
    return ActivityPolicy(
        retry=retry,
        start_to_close=start_to_close,
        heartbeat_timeout=heartbeat,
        schedule_to_close=schedule_to_close,
    )


# The inbox drains are also started by nudges, with no execution timeout; the
# schedule-to-close is the hour a scheduled run already gets from the catalog.
DRAIN = activity_policy(
    TRANSIENT_RETRY, 10 * MINUTE, heartbeat=2 * MINUTE, schedule_to_close=60 * MINUTE
)
HOUSEKEEPING = activity_policy(TRANSIENT_RETRY, 10 * MINUTE)
_PRUNE = activity_policy(NO_RETRY, 10 * MINUTE)
_OPEN_POLICIES: Mapping[str, ActivityPolicy] = MappingProxyType(
    {
        # Ends only runs still open, so a retry repeats nothing.
        WorkflowType.SWEEP_NOTEBOOK_RUNS.value: HOUSEKEEPING,
        # Its own budget is 15 s; the timeout is the backstop.
        WorkflowType.DEPLOYMENT_HEALTH.value: activity_policy(
            TRANSIENT_RETRY, timedelta(seconds=60)
        ),
        # Predicate deletes and a pure log line: a retry buys nothing.
        WorkflowType.PRUNE_EXPIRED_TOKENS.value: _PRUNE,
        WorkflowType.PRUNE_EXPIRED_DEVICE_CODES.value: _PRUNE,
        WorkflowType.PRUNE_LOGIN_LOCKOUTS.value: _PRUNE,
        # Batched, and every batch commits on its own: a retry after a dropped
        # connection deletes only what the failed attempt had not reached, so
        # it is worth another attempt rather than a day's wait. A heartbeat
        # because a first run over a large backlog takes many batches.
        WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value: activity_policy(
            TRANSIENT_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        WorkflowType.ENTITLEMENTS_WATCHDOG.value: activity_policy(NO_RETRY, timedelta(seconds=30)),
        # The compute meter is idempotent (whole minutes past a high-water mark,
        # under an advisory lock) and touches the ledger: transient retry, and a
        # heartbeat so a worker that dies mid-fleet is noticed. The reachability
        # sweep only announces transitions; a retry buys nothing its five-minute
        # schedule does not, so it never retries.
        WorkflowType.COMPUTE_METER.value: activity_policy(
            TRANSIENT_RETRY, 5 * MINUTE, heartbeat=2 * MINUTE
        ),
        WorkflowType.COMPUTE_SWEEP.value: activity_policy(NO_RETRY, 2 * MINUTE),
        # The catalog refresh is idempotent (it reconciles the live feed onto the
        # rows under the advisory lock and never touches a running allocation),
        # and it comes back in an hour, so it retries like the other housekeeping.
        WorkflowType.COMPUTE_CATALOG.value: HOUSEKEEPING,
        # The reconciler is idempotent (adopting an id already recorded changes
        # nothing, and terminate is idempotent by the provider contract) and it
        # is the only thing that finds a pod nobody is billing: transient retry,
        # with a heartbeat because one pass can walk the whole fleet.
        WorkflowType.COMPUTE_RECONCILE.value: activity_policy(
            TRANSIENT_RETRY, 10 * MINUTE, heartbeat=2 * MINUTE
        ),
        # The org-machine reconcile asks providers to create machines, which is
        # not idempotent at every provider: it never retries. Each machine is
        # committed ``provisioning`` before its create, so a pass that dies is
        # finished by the next one, thirty seconds later. Ten minutes bounds a
        # pass whose creates wait out their own retry schedules.
        WorkflowType.ORG_MACHINE_RECONCILE.value: activity_policy(NO_RETRY, 10 * MINUTE),
        # Reaping a spare is a predicate delete and a purge, both idempotent, and
        # the sweep runs again in a minute: its schedule is the retry.
        WorkflowType.REAP_CHAT_SPARES.value: activity_policy(NO_RETRY, 2 * MINUTE),
        # Both steps are idempotent writes over a bounded batch, and the pass
        # runs again in fifteen minutes: its schedule is the retry.
        WorkflowType.RECONCILE_WORKSPACES.value: activity_policy(NO_RETRY, 5 * MINUTE),
        # Finishing a deleted workspace's chats is idempotent row by row (a
        # finished chat is unstamped in the same transaction), and the person
        # who deleted it is waiting on the rest: a transient failure is worth
        # another attempt rather than the schedule's next tick.
        WorkflowType.FINISH_WORKSPACE_DELETIONS.value: DRAIN,
        # The lifecycle pass never retries: every erasure is one transaction
        # that either commits or writes nothing, a blocked one is recorded and
        # retried by the next pass, and that pass is fifteen minutes away.
        WorkflowType.ACCOUNT_LIFECYCLE_SWEEP.value: activity_policy(NO_RETRY, 14 * MINUTE),
        # Re-erasure is idempotent per identity (a tombstone is skipped), and an
        # identity a restore brought back must not wait for the next boot: a
        # transient failure is worth another attempt.
        WorkflowType.ACCOUNT_REERASE.value: activity_policy(TRANSIENT_RETRY, 30 * MINUTE),
        # Files. The promote is the commit half of an upload — verify, sniff,
        # land the version — and is idempotent by construction (the operation
        # row carries the node it already created), so a transient failure is
        # worth another attempt; a heartbeat so a worker that dies moving a
        # multi-gigabyte object is noticed in minutes. Its refusals — a bad
        # hash, an exhausted quota, an authorization the caller has since lost,
        # a malformed request — are facts about the upload, not about this
        # attempt, and are declared non-retryable so the operation fails once
        # instead of six times. The ACL rewrite is resumable from its own
        # cursor and equally worth retrying. The janitor never retries: every
        # sweeper is idempotent and budgeted, and its five-minute schedule is a
        # better recovery than a backoff that would still be running when the
        # next tick fires.
        WorkflowType.FILES_PROMOTE.value: activity_policy(
            FILES_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        WorkflowType.FILES_ACL_REWRITE.value: activity_policy(
            FILES_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        # Resumable by construction: every batch commits its cursor on the
        # operation row, so a retry picks up where the killed attempt stopped
        # rather than rewriting the subtree from the beginning.
        WorkflowType.FILES_LARGE_MOVE.value: activity_policy(
            FILES_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        # Same shape as the move: the plan and the cursor live on the
        # operation row, so a retry resumes at the batch boundary the killed
        # attempt committed rather than copying the subtree twice.
        WorkflowType.FILES_COPY.value: activity_policy(
            FILES_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        # A batch is the same shape again: each window of items commits its
        # own results and cursor, so a retry applies the items the killed
        # attempt never reached and re-applies none that it did.
        WorkflowType.FILES_BULK.value: activity_policy(
            FILES_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        # NO_RETRY, like the janitor: the tick is the recovery. A retried pass
        # would count a second offer against every row it already claimed,
        # spending an operation's attempts on one unlucky minute.
        WorkflowType.FILES_RECOVER_QUEUED.value: activity_policy(NO_RETRY, 10 * MINUTE),
        # The settlement, unlike the pass, IS worth retrying: its writes are
        # what keep a row from being offered forever, and every one of them is
        # a compare-and-swap on a state a runner may have taken meanwhile.
        RECOVER_SETTLE_ACTIVITY: activity_policy(TRANSIENT_RETRY, 5 * MINUTE),
        WorkflowType.FILES_JANITOR.value: activity_policy(NO_RETRY, 30 * MINUTE),
        # The collector is idempotent by construction -- an object it already
        # parked is no longer under the prefix the next attempt walks, and a
        # page it already finished is behind the cursor -- so a transient
        # store failure mid-bucket is worth another attempt rather than
        # waiting a day. A heartbeat because one page can move a lot of bytes
        # and a worker that died doing it should be noticed in minutes.
        WorkflowType.FILES_GC.value: activity_policy(
            TRANSIENT_RETRY, 30 * MINUTE, heartbeat=2 * MINUTE
        ),
        # One step of a workspace move: a short transaction keyed by the move
        # id, which reads the row first and does nothing a step already did,
        # so a transient failure is worth another attempt. The waits between
        # steps are the workflow's timers, never an activity's.
        WorkflowType.WORKSPACE_MACHINE_MOVE.value: activity_policy(TRANSIENT_RETRY, 2 * MINUTE),
        # The pass only reads, so a transient failure is worth another attempt
        # before the next tick.
        WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER.value: activity_policy(
            TRANSIENT_RETRY, 5 * MINUTE
        ),
    }
)
"""The open worker's own rows, keyed by activity type name."""

ACTIVITY_POLICY_SETS: ExtensionPoint[Mapping[str, ActivityPolicy]] = ExtensionPoint(
    "worker_activity_policies"
)
"""The policies a private job family brings for its activities, registered
beside its job modules. With nothing registered the table is the open rows."""


def merge_policies(
    own: Mapping[str, ActivityPolicy], registered: tuple[Mapping[str, ActivityPolicy], ...]
) -> Mapping[str, ActivityPolicy]:
    """One read-only table of ``own`` and every registered set. An activity
    declared twice is a wiring bug, so it raises instead of letting the later
    registration win."""
    table = dict(own)
    for policies in registered:
        clash = sorted(table.keys() & policies.keys())
        if clash:
            raise ValueError(f"activity policies declared twice: {clash}")
        table.update(policies)
    return MappingProxyType(table)


@cache
def _table() -> Mapping[str, ActivityPolicy]:
    return merge_policies(_OPEN_POLICIES, ACTIVITY_POLICY_SETS.items())


# Declared, not assigned: the module ``__getattr__`` below builds it on first
# read, after composition has registered every family's policies.
ACTIVITY_POLICIES: Mapping[str, ActivityPolicy]
"""Keyed by activity type name; exhaustive over every activity the worker
serves (a test pins it). Reading it closes :data:`ACTIVITY_POLICY_SETS`."""

_LAZY: Mapping[str, Callable[[], object]] = {"ACTIVITY_POLICIES": _table}


def __getattr__(name: str) -> object:
    build = _LAZY.get(name)
    if build is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return build()


def policy_for(activity_type: str) -> ActivityPolicy:
    """The policy for an activity type; a missing entry is a wiring bug, not a default.

    An open row is answered without reading the registered sets, so an open
    workflow module that reads its policy at import never closes them."""
    own = _OPEN_POLICIES.get(activity_type)
    if own is not None:
        return own
    try:
        return _table()[activity_type]
    except KeyError:
        raise KeyError(
            f"no retry policy declared for activity {activity_type!r}; add it to "
            "the open table in worker/temporal/retry.py, or to the policies its "
            "family registers"
        ) from None
