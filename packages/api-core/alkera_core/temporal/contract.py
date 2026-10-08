"""The Temporal contract shared by every service that starts or serves work.

Pure data, no SDK import: the backend (which only *starts* and *signals*
workflows), the worker (which serves them) and the health probe all agree on
these names by importing this one module, so a queue or workflow-type string
is spelled in exactly one place.

Every workflow type is the historical task name of the job it replaced, and the
activity that does the work registers under the same string. The two registries
are separate in Temporal, so the collision is deliberate: searching the UI for
``files.gc`` finds the workflow, and its history shows the
identically named activity.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from alkera_core.extensions import ExtensionPoint


class TaskQueue(StrEnum):
    """The four task queues; one worker process serves any subset of them."""

    MONEY = "money"
    """Metering work: the compute meter and the passes that reconcile what it bills."""
    EMAIL = "email"
    """Outbound mail dispatch."""
    SYNC = "sync"
    """Connector probes and inbound webhook work."""
    DEFAULT = "default"
    """Housekeeping: prunes, janitors, health, heartbeats."""


MORE_WORK_SIGNAL = "more_work"
"""Signal a nudge sends to a running drain so it performs one more pass."""


class WorkflowType(StrEnum):
    """Every workflow type the platform serves. The value is the historical
    task name. A distribution's own job families name theirs in an enum of
    their own and register its queues in :data:`JOB_QUEUES`."""

    PRUNE_EXPIRED_TOKENS = "auth.prune_expired_tokens"
    PRUNE_EXPIRED_DEVICE_CODES = "auth.prune_expired_device_codes"
    PRUNE_LOGIN_LOCKOUTS = "auth.prune_login_lockouts"
    # The identity security log's retention window.
    PRUNE_IDENTITY_SECURITY_EVENTS = "auth.prune_identity_security_events"
    DEPLOYMENT_HEALTH = "deployment_health.run"
    ENTITLEMENTS_WATCHDOG = "entitlements.watchdog"
    # Ends the notebook runs no engine will end, past their deadlines.
    SWEEP_NOTEBOOK_RUNS = "notebooks.sweep_runs"
    # The compute plane: the per-minute metering tick and the reachability sweep.
    COMPUTE_METER = "compute.meter"
    COMPUTE_SWEEP = "compute.sweep"
    # What the provider thinks we are renting, against the rows that own it.
    COMPUTE_RECONCILE = "compute.reconcile"
    # Each provider's live price + stock reconciled onto its own catalog rows.
    COMPUTE_CATALOG = "compute.catalog"
    # Every org machine converged on what its org asked of it: started,
    # replaced, stopped, released.
    ORG_MACHINE_RECONCILE = "compute.org_machine_reconcile"
    # One workspace moved from one machine to another, sleep then wake.
    WORKSPACE_MACHINE_MOVE = "workspace.machine_move"
    # The net under every move: a move whose workflow went with Temporal's
    # history (a fresh server, a lost namespace) is driven again.
    WORKSPACE_MACHINE_MOVE_RECOVER = "workspace.machine_move_recover"
    REAP_CHAT_SPARES = "chat.reap_spares"
    # Every chat in a workspace, and no workspace of one outliving its chat.
    RECONCILE_WORKSPACES = "workspace.reconcile"
    # What a workspace deletion leaves after its request: each chat's ending
    # finished and the workspace's folder trashed, a committed batch at a time.
    FINISH_WORKSPACE_DELETIONS = "workspace.finish_deletions"
    # Files: the upload commit a request hands off, the reconciliation pass,
    # the daily collection of bytes whose whole tenant is gone, the resumable
    # ACL cache rewrite and the batched move of an oversized subtree.
    FILES_PROMOTE = "files.promote"
    FILES_JANITOR = "files.janitor"
    FILES_GC = "files.gc"
    FILES_ACL_REWRITE = "files.acl_rewrite"
    FILES_LARGE_MOVE = "files.large_move"
    FILES_COPY = "files.copy"
    FILES_BULK = "files.bulk"
    FILES_RECOVER_QUEUED = "files.recover_queued"
    # Account lifecycle: the pass that runs due erasures.
    ACCOUNT_LIFECYCLE_SWEEP = "account.lifecycle_sweep"
    # Erase again every identity the erasure ledger names that a database
    # restore brought back: started on every worker boot (a no-op unless one is
    # found) and by the restore runbook.
    ACCOUNT_REERASE = "account.reerase"


_PLATFORM_QUEUES: Mapping[str, TaskQueue] = MappingProxyType(
    {
        WorkflowType.COMPUTE_METER: TaskQueue.MONEY,
        WorkflowType.COMPUTE_RECONCILE: TaskQueue.MONEY,
        WorkflowType.ORG_MACHINE_RECONCILE: TaskQueue.MONEY,
        WorkflowType.PRUNE_EXPIRED_TOKENS: TaskQueue.DEFAULT,
        WorkflowType.PRUNE_EXPIRED_DEVICE_CODES: TaskQueue.DEFAULT,
        WorkflowType.PRUNE_LOGIN_LOCKOUTS: TaskQueue.DEFAULT,
        WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS: TaskQueue.DEFAULT,
        WorkflowType.SWEEP_NOTEBOOK_RUNS: TaskQueue.DEFAULT,
        WorkflowType.ENTITLEMENTS_WATCHDOG: TaskQueue.DEFAULT,
        WorkflowType.DEPLOYMENT_HEALTH: TaskQueue.DEFAULT,
        WorkflowType.COMPUTE_SWEEP: TaskQueue.DEFAULT,
        WorkflowType.COMPUTE_CATALOG: TaskQueue.DEFAULT,
        WorkflowType.WORKSPACE_MACHINE_MOVE: TaskQueue.DEFAULT,
        WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER: TaskQueue.DEFAULT,
        WorkflowType.REAP_CHAT_SPARES: TaskQueue.DEFAULT,
        WorkflowType.RECONCILE_WORKSPACES: TaskQueue.DEFAULT,
        WorkflowType.FINISH_WORKSPACE_DELETIONS: TaskQueue.DEFAULT,
        WorkflowType.FILES_PROMOTE: TaskQueue.DEFAULT,
        WorkflowType.FILES_JANITOR: TaskQueue.DEFAULT,
        WorkflowType.FILES_GC: TaskQueue.DEFAULT,
        WorkflowType.FILES_ACL_REWRITE: TaskQueue.DEFAULT,
        WorkflowType.FILES_LARGE_MOVE: TaskQueue.DEFAULT,
        WorkflowType.FILES_COPY: TaskQueue.DEFAULT,
        WorkflowType.FILES_BULK: TaskQueue.DEFAULT,
        WorkflowType.FILES_RECOVER_QUEUED: TaskQueue.DEFAULT,
        WorkflowType.ACCOUNT_LIFECYCLE_SWEEP: TaskQueue.DEFAULT,
        WorkflowType.ACCOUNT_REERASE: TaskQueue.DEFAULT,
    }
)
"""Which queue serves each platform workflow type. Exhaustive: a test pins every member."""

JOB_QUEUES: ExtensionPoint[Mapping[str, TaskQueue]] = ExtensionPoint("temporal_job_queues")
"""The workflow types a distribution's job families add, each with its queue.
Registered during composition by every process that starts or serves them."""


class JobQueueTable(Mapping[str, TaskQueue]):
    """``platform``'s queues and every set registered in ``registered``, as one
    read-only mapping. Built on the first read past the platform's own types,
    which closes ``registered``; a type declared twice is a wiring bug, so it
    raises."""

    def __init__(
        self,
        platform: Mapping[str, TaskQueue],
        registered: ExtensionPoint[Mapping[str, TaskQueue]],
    ) -> None:
        self._platform = platform
        self._registered = registered
        self._table: Mapping[str, TaskQueue] | None = None

    def _merged(self) -> Mapping[str, TaskQueue]:
        if self._table is None:
            table: dict[str, TaskQueue] = dict(self._platform)
            for queues in self._registered.items():
                clash = sorted(table.keys() & queues.keys())
                if clash:
                    raise ValueError(f"workflow types declared twice: {clash}")
                table.update(queues)
            self._table = MappingProxyType(table)
        return self._table

    def __getitem__(self, workflow: str) -> TaskQueue:
        # A platform type is answered without reading the registrations, so a
        # module that looks up its own queue at import never closes them.
        own = self._platform.get(workflow)
        if own is not None:
            return own
        return self._merged()[workflow]

    def __iter__(self) -> Iterator[str]:
        return iter(self._merged())

    def __len__(self) -> int:
        return len(self._merged())


QUEUE_FOR: Mapping[str, TaskQueue] = JobQueueTable(_PLATFORM_QUEUES, JOB_QUEUES)
"""Which queue serves each workflow type, the platform's and every registered family's."""


RECOVER_SETTLE_ACTIVITY = f"{WorkflowType.FILES_RECOVER_QUEUED.value}.settle"
"""The second half of a queued-operation recovery tick: what the orchestrator
answered for each row it was offered. An activity of its own rather than a
workflow, because only the workflow can start a runner and only an activity can
write what starting it proved."""

COMPANION_ACTIVITIES: Final[dict[str, WorkflowType]] = {
    RECOVER_SETTLE_ACTIVITY: WorkflowType.FILES_RECOVER_QUEUED,
}
"""Activities that are not a workflow's namesake, and the workflow they belong to.

Nearly every activity shares its workflow's name, which is how the registry
knows its queue. The few that do not are a second step of one workflow, and
they run where that workflow runs -- naming them here is what keeps a worker
from registering an activity onto a queue that never polls for it.
"""


def drain_workflow_id(workflow: StrEnum) -> str:
    """The workflow id of a singleton drain or sweep: the type name itself.

    A nudge signals-with-start against this id, so at most one drain of a kind
    runs at a time and a nudge during a running pass simply queues one more
    pass. Scheduled runs never collide with it — the server suffixes a
    schedule's action id with the fire time.
    """
    return workflow.value


@dataclass(frozen=True, slots=True)
class FilesOperationInput:
    """What every queued Files workflow runs on: the operation and its org.

    The workflows take it as positional strings across a process boundary, so
    the order is a contract nothing type-checks. Every starter -- the backend's
    hand-off and the recovery pass's child start -- builds the args here, and a
    test binds :meth:`args` to each workflow's declared ``run`` by name, so the
    two sides cannot drift apart again (the drift that stranded every upload).
    """

    op_id: str
    org_team_id: str

    def args(self) -> list[str]:
        """The positional args, in the order every Files ``run`` declares them."""
        return [self.op_id, self.org_team_id]


def keyed_workflow_id(workflow: StrEnum, key: str) -> str:
    """The workflow id of per-entity work: ``<type>:<key>``.

    Starting it with the use-existing conflict policy attaches a second nudge
    for the same entity to the run already in flight instead of duplicating it.
    The key is refused when it could not survive a log line or a URL: an empty
    key would alias every entity onto one id, and whitespace or a slash would
    split the id in the UI's search and in path-shaped tooling.
    """
    if not key or any(ch.isspace() for ch in key) or "/" in key:
        raise ValueError(
            f"workflow key must be a non-empty token without whitespace or '/': {key!r}"
        )
    return f"{workflow.value}:{key}"
