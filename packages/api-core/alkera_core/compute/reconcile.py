"""The other side of the ledger: what the PROVIDER thinks we are renting.

Everything else in the plane reasons from a row outwards: the meter polls the
pod a row names, the release terminates the pod a row names. A pod whose id
the database never learned would be invisible to both and billed indefinitely.
That happens three ways:

- a create cancelled mid-flight (a client disconnect, a shutdown), so the pod
  is built after we stopped listening;
- a create that timed out, which the provider may still have acted on;
- a terminate that was never confirmed for a row that was finished anyway.

This pass closes all three by joining on the NAME. A pod this deployment
created is called ``<prefix>-<allocation id>``, and that name is committed
before the provider is called (see
:func:`backend.services.compute.service.create_allocation`), so every pod can be
traced back to the row that asked for it even when the id never reached us.

What one pass does, per pod under this deployment's prefix:

- **adopt** it when the name matches a live row that carries no pod id. The
  row owns it and never heard the answer; the id is recorded and the meter
  takes it from the next tick;
- **terminate** it when the name matches no row, matches a terminal row, or
  matches a live row that names another pod (a second create under the same
  name), since nothing would ever bill or stop it. Only once it is older than
  the grace, and never when the provider did not say how old it is;
- **leave it alone** otherwise.

And, per row, one thing a pod cannot say: a row left ``provisioning`` with no
pod id, older than the unconfirmed-create window, for which no pod of its name
exists. The provider did not build one; the row is written off so the grant
slot it holds goes back.

Everything here is idempotent, so two passes cannot double-act: adopting an id
already recorded is a no-op, and terminate is idempotent by the provider
contract. The pass is bounded where it destroys (``max_terminations`` per
tick, the rest left for the next one) and not where it reads: the pod listing
and the live rows are both taken whole, because both are arguments about
absence and a partial one would terminate a live machine.

A deployment that shares a provider account with another must have its own
``compute_pod_name_prefix``. The match is anchored to ``<prefix>-<12 hex>``
rather than being a prefix test, so ``alkera`` and ``alkera-staging`` stay
disjoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.provider import (
    ComputeProvider,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    OrphanStorage,
    ProviderPod,
)
from alkera_core.compute.transitions import is_legal, transition
from alkera_core.config import Settings, settings
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    FAILED,
    PROVISIONING,
    ComputeAllocation,
    ComputeMachineType,
)

log = get_logger(__name__)

ACTOR: dict[str, str | None] = {"kind": "system", "email": None, "name": "reconcile"}

#: Why a row a create never confirmed is finally written off.
CREATE_UNCONFIRMED = "provider_create_unconfirmed"

#: How many passes in a row may end on an untrusted listing before the pass
#: says so as its own fact rather than as one more line in the per-period log.
#: Three: long enough that a single noisy tick is not an alert, short enough
#: that a stall is named within the quarter-hour at the default period.
TRUNCATION_ALERT_PASSES = 3


class TruncationWatch:
    """How many passes in a row ended without a listing they could trust.

    ``listing_truncated`` is measured against the provider's WHOLE account,
    because the listing endpoint takes no name filter — so on an account this
    deployment shares, a sibling holding more pods than our ceiling makes every
    one of our passes read as untrusted. The pass then fails safe (it writes
    nothing off), but grant-slot recovery stops, and the only trace is one
    ``error`` line per period that looks identical to the previous one. This
    counts the streak so the stall can be said once, as a distinct fact, and so
    a metric has something to be a gauge OF. Process memory is the right store:
    the question is about THIS worker's consecutive passes."""

    def __init__(self, *, alert_after: int = TRUNCATION_ALERT_PASSES) -> None:
        self._alert_after = alert_after
        self._streak = 0

    @property
    def streak(self) -> int:
        """Consecutive passes that ended on an untrusted listing."""
        return self._streak

    def record(self, *, truncated: bool) -> bool:
        """Note one pass. True when the streak has reached the alert length —
        and only on the pass that reaches it, plus every pass after, so the
        caller can say it without the caller counting."""
        self._streak = self._streak + 1 if truncated else 0
        return self._streak >= self._alert_after


#: The shape of the allocation id in a pod name: the first 12 characters of a
#: UUID's hex, which is what :func:`pod_name_for` appends to the prefix.
_ID_PART = "[0-9a-f]{12}"


#: The name production's machines carry. Only production may use it bare.
PRODUCTION_POD_PREFIX = "alkera"


def pod_name_base(config: Settings | None = None) -> str:
    """This deployment's pod prefix, without the trailing hyphen.

    ``alkera`` is production's. Anywhere else it is namespaced by the
    environment (``alkera-local``, ``alkera-staging``): a laptop worker
    holding a key to production's provider account, with the default prefix,
    would otherwise list production's machines, find no row for them in its
    own database and terminate them once they passed the grace. The anchored
    match keeps ``alkera-local-<12 hex>`` and ``alkera-<12 hex>`` disjoint."""
    config = config or settings
    base = config.compute_pod_name_prefix
    if base == PRODUCTION_POD_PREFIX and config.app_env != "production":
        return f"{base}-{config.app_env}"
    return base


def pod_name_prefix() -> str:
    """The prefix every pod of THIS deployment is named with."""
    return f"{pod_name_base()}-"


def pod_name_for(allocation_id: UUID) -> str:
    """The name the provider knows an allocation's pod by."""
    return f"{pod_name_prefix()}{allocation_id.hex[:12]}"


def pod_name_pattern() -> re.Pattern[str]:
    """The ANCHORED pattern a pod name must match to be this deployment's.

    A prefix test is not enough: ``"alkera-staging-aabbccddeeff"`` starts with
    ``"alkera-"``, so a deployment prefixed ``alkera`` would list and reap the
    pods of one prefixed ``alkera-staging``. Matching the prefix followed by
    exactly the 12 hex characters of an allocation id keeps them disjoint."""
    return re.compile(rf"{re.escape(pod_name_prefix())}{_ID_PART}\Z")


def is_our_pod_name(name: str) -> bool:
    """Whether ``name`` is a pod THIS deployment named."""
    return pod_name_pattern().match(name) is not None


def _grace() -> timedelta:
    return timedelta(seconds=settings.compute_reconcile_grace_seconds)


def _unconfirmed_window() -> timedelta:
    """How long a create with no answer is given before the row is written off.
    Never shorter than the grace: a row must not be closed in a pass that was
    still forbidden to reap the pod it might own."""
    return max(
        timedelta(seconds=settings.compute_unconfirmed_create_seconds),
        _grace(),
    )


@dataclass
class ReconcileSummary:
    """What one pass saw and did (for the task log / tests)."""

    listed: int = 0
    adopted: int = 0
    terminated: int = 0
    written_off: int = 0
    #: Disks deleted because nothing names them (``OrphanStorage``).
    released_storage: int = 0
    #: Pods left alone because they are younger than the grace, or because the
    #: provider did not report their age.
    held: int = 0
    #: The pass could not ask the provider at all.
    unavailable: bool = False
    #: Pods the provider listed in total, ours and everybody else's. The
    #: endpoint takes no name filter, so on a shared account this is the whole
    #: account — which is what ``listing_truncated`` is measured against.
    listed_all: int = 0
    #: The provider's answer could not be trusted to be the WHOLE fleet (it
    #: came back at or above the ceiling a complete listing is expected to sit
    #: under). Nothing is written off on such a pass.
    listing_truncated: bool = False
    #: Consecutive passes, this one included, that ended on a listing they
    #: could not trust. Past :data:`TRUNCATION_ALERT_PASSES` the stall is said
    #: as its own fact: write-offs have stopped and nobody has been told.
    truncated_streak: int = 0
    #: Terminations this pass declined to make because it had already made as
    #: many as one pass may.
    deferred_terminations: int = 0
    adopted_ids: list[str] = field(default_factory=list)
    terminated_ids: list[str] = field(default_factory=list)


async def reconcile_pods(
    db: AsyncSession,
    *,
    provider: ComputeProvider,
    provider_kind: str,
    now: datetime | None = None,
    max_pods: int | None = None,
    max_terminations: int | None = None,
    watch: TruncationWatch | None = None,
) -> ReconcileSummary:
    """One reconciliation pass over this deployment's pods. Commits.

    ``provider_kind`` is the catalog's name for the provider being listed. It
    scopes the write-off: a row whose machine type belongs to a DIFFERENT
    provider is not evidenced one way or the other by this provider's list, and
    closing it would free a grant slot for a machine that is running somewhere
    this pass never looked.
    """
    moment = now or datetime.now(UTC)
    ceiling = max_pods if max_pods is not None else settings.compute_reconcile_max_pods
    termination_budget = (
        max_terminations
        if max_terminations is not None
        else settings.compute_reconcile_max_terminations
    )
    summary = ReconcileSummary()
    prefix = pod_name_prefix()
    try:
        pods = await provider.list_pods(name_prefix=prefix)
    except ComputeProviderUnavailableError:
        # "Not here" — this deployment cannot enumerate through this provider.
        # Emphatically NOT "there are no pods": an empty answer inferred from a
        # refusal is a licence to terminate everything.
        summary.unavailable = True
        return summary
    except ComputeProviderError as exc:
        log.warning("compute.reconcile.list_failed", error=str(exc))
        summary.unavailable = True
        return summary

    # The name filter is applied here rather than trusted to the provider, and
    # it is ANCHORED: a provider that ignores the argument, or a sibling
    # deployment whose prefix extends ours, must not be able to widen the blast
    # radius to machines this deployment did not create.
    mine = [p for p in pods if is_our_pod_name(p.name)]
    summary.listed = len(mine)
    summary.listed_all = len(pods)
    # A listing at or above the ceiling is not evidence of absence: the
    # provider may have paged and we would be holding only the first page.
    # Everything below still runs — adopting is safe on a partial list, and so
    # is terminating a pod we can see is unowned — but the write-off, which is
    # an argument FROM absence, does not.
    summary.listing_truncated = len(pods) >= ceiling
    stalled = watch.record(truncated=summary.listing_truncated) if watch is not None else False
    summary.truncated_streak = watch.streak if watch is not None else 0
    if summary.listing_truncated:
        log.error(
            "compute.reconcile.listing_truncated",
            listed_all=len(pods),
            listed_ours=len(mine),
            ceiling=ceiling,
            streak=summary.truncated_streak,
            detail="the provider returned at least as many pods as a complete "
            "listing is expected to stay under; nothing will be written off "
            "on this pass",
        )
    if stalled:
        # The distinct fact, said once it is one: this is no longer a noisy
        # tick, it is grant-slot recovery that has stopped. The counts say
        # whether the account is ours to shrink or a sibling's — the listing
        # endpoint takes no name filter, so we can only ever count the whole
        # account and report our share of it.
        log.error(
            "compute.reconcile.write_off_stalled",
            passes=summary.truncated_streak,
            listed_all=len(pods),
            listed_ours=len(mine),
            ceiling=ceiling,
            detail="no allocation row has been written off for this many "
            "consecutive passes because the pod listing could not be trusted; "
            "raise COMPUTE_RECONCILE_MAX_PODS above the account's pod count, "
            "or give this deployment its own provider account",
        )
    seen = {p.name for p in mine}

    live = await _live_rows(db)
    for pod in mine:
        alloc = live.get(pod.name)
        # A live row owns the pod it names by id, or the first pod of its name
        # while it has no id yet. A second pod under the same name (a create
        # that timed out after the provider built it, retried before the
        # listing showed it) is nobody's, and is reaped like any orphan.
        if alloc is not None and alloc.provider_machine_id not in ("", None, pod.pod_id):
            alloc = None
        if alloc is not None:
            if not alloc.provider_machine_id:
                alloc.provider_machine_id = pod.pod_id
                if alloc.state != PROVISIONING and is_legal(alloc.state, PROVISIONING):
                    transition(
                        db, alloc, PROVISIONING, reason="the provider has its pod", actor=ACTOR
                    )
                alloc.error = ""
                summary.adopted += 1
                summary.adopted_ids.append(pod.pod_id)
                log.info(
                    "compute.reconcile.adopted",
                    allocation_id=str(alloc.id),
                    pod_id=pod.pod_id,
                    pod_name=pod.name,
                )
            continue
        # Nothing live owns this pod: either no row was ever written for that
        # name, or the row that was is finished.
        if not _reapable(pod, now=moment):
            summary.held += 1
            continue
        if summary.terminated >= termination_budget:
            # The destructive action is the one that is budgeted, so a pass
            # that meets a surprising number of orphans stops and says so
            # rather than emptying an account on one tick. The next pass picks
            # up where this left off; nothing is written off meanwhile.
            summary.deferred_terminations += 1
            continue
        if await _terminate(provider, pod):
            summary.terminated += 1
            summary.terminated_ids.append(pod.pod_id)
    await db.commit()

    if summary.deferred_terminations:
        log.error(
            "compute.reconcile.termination_budget_reached",
            terminated=summary.terminated,
            deferred=summary.deferred_terminations,
            budget=termination_budget,
        )
    if not summary.listing_truncated and isinstance(provider, OrphanStorage):
        # A disk that is an object of its own (a RunPod CPU pod's network
        # volume) outlives its pod when the pod went first. One no live row
        # and no pod of ours names is nobody's, and bills until it is deleted.
        # Only on a whole listing: ``seen`` is an argument about absence.
        try:
            released = await provider.release_orphan_storage(
                ours=is_our_pod_name, owned=set(live) | seen
            )
        except ComputeProviderError as exc:
            log.warning("compute.reconcile.storage_sweep_failed", error=str(exc))
        else:
            summary.released_storage = len(released)
            if released:
                log.warning("compute.reconcile.orphan_storage_released", volume_ids=released)
    if not summary.listing_truncated:
        summary.written_off = await _write_off_unconfirmed(
            db, now=moment, seen=seen, provider_kind=provider_kind, limit=ceiling
        )
    log.info(
        "compute.reconcile",
        provider=provider_kind,
        listed=summary.listed,
        adopted=summary.adopted,
        terminated=summary.terminated,
        written_off=summary.written_off,
        held=summary.held,
        deferred_terminations=summary.deferred_terminations,
        listing_truncated=summary.listing_truncated,
    )
    return summary


def _reapable(pod: ProviderPod, *, now: datetime) -> bool:
    """Whether an unowned pod is old enough to be terminated.

    A pod whose age the provider did not report is never reaped: the grace is
    the only thing standing between this pass and a machine somebody is
    mid-way through creating, and an unknown age cannot clear it."""
    if pod.created_at is None:
        return False
    return now - pod.created_at >= _grace()


async def _terminate(provider: ComputeProvider, pod: ProviderPod) -> bool:
    try:
        await provider.terminate_pod(pod.pod_id)
    except ComputeProviderError as exc:
        # Left for the next pass rather than reported gone.
        log.warning("compute.reconcile.terminate_failed", pod_id=pod.pod_id, error=str(exc))
        return False
    log.warning(
        "compute.reconcile.orphan_terminated",
        pod_id=pod.pod_id,
        pod_name=pod.name,
        created_at=pod.created_at.isoformat() if pod.created_at else None,
    )
    return True


async def _live_rows(db: AsyncSession) -> dict[str, ComputeAllocation]:
    """Every allocation that is not finished, keyed by the pod name it owns.

    Read WHOLE, deliberately and without a limit: this set is what stands
    between a running machine and a terminate. A row missing from it reads as
    "nothing owns that pod", so a paged or capped read here would eventually
    reap a customer's box. It stays affordable because it is scoped to the live
    fleet — bounded by the grants that admitted it — not to the table, which
    keeps every allocation the deployment has ever made.

    Only the live rows are read: a pod matching a finished row and a pod
    matching no row are the same fact, since nothing will bill or stop either.
    The name carries the first 12 hex characters of the allocation id, so the
    join is made here rather than with a functional predicate no index could
    serve."""
    rows = (
        (
            await db.execute(
                select(ComputeAllocation).where(
                    ComputeAllocation.state.notin_(COMPUTE_TERMINAL_STATES)
                )
            )
        )
        .scalars()
        .all()
    )
    return {pod_name_for(row.id): row for row in rows}


async def _write_off_unconfirmed(
    db: AsyncSession, *, now: datetime, seen: set[str], provider_kind: str, limit: int
) -> int:
    """Close the rows whose create was never confirmed and whose pod does not
    exist — they hold a grant slot for a machine nobody ever built.

    This is an argument FROM ABSENCE, so it is only made where absence was
    actually established. ``seen`` is every pod name of this deployment that
    the pass found, and the caller runs this only when that listing was whole,
    so a row whose pod IS there — including one just adopted — is never closed.

    ``provider_kind`` scopes it the other way. The listing came from ONE
    provider, and a row whose machine type belongs to another was not looked
    for: closing it would free a grant slot while its machine runs somewhere
    this pass never asked about. That costs nothing today, with one provider
    that provisions, and is the whole bug the day a second one lands.
    """
    cutoff = now - _unconfirmed_window()
    rows = (
        (
            await db.execute(
                select(ComputeAllocation)
                .join(
                    ComputeMachineType,
                    ComputeMachineType.id == ComputeAllocation.machine_type_id,
                )
                .where(
                    ComputeAllocation.state == PROVISIONING,
                    ComputeAllocation.provider_machine_id == "",
                    ComputeAllocation.created_at <= cutoff,
                    ComputeMachineType.provider == provider_kind,
                )
                .order_by(ComputeAllocation.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for row in rows:
        if pod_name_for(row.id) in seen:
            continue
        transition(db, row, FAILED, reason=CREATE_UNCONFIRMED, actor=ACTOR, now=now)
        row.terminated_reason = CREATE_UNCONFIRMED
        row.released_at = now
        if not row.error:
            row.error = "the provider never confirmed the create and has no pod of this name"
        closed += 1
        log.warning(
            "compute.reconcile.create_written_off",
            allocation_id=str(row.id),
            pod_name=pod_name_for(row.id),
        )
    if closed:
        await db.commit()
    return closed


__all__ = [
    "CREATE_UNCONFIRMED",
    "PRODUCTION_POD_PREFIX",
    "TRUNCATION_ALERT_PASSES",
    "ReconcileSummary",
    "TruncationWatch",
    "is_our_pod_name",
    "pod_name_base",
    "pod_name_for",
    "pod_name_pattern",
    "pod_name_prefix",
    "reconcile_pods",
]
