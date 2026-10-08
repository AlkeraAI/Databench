"""The shared Temporal contract: queue names, workflow types and id builders.

These are the strings the backend nudges, the worker registries and the
schedule catalog all spell through one module, so the tests pin the shape of
the contract itself — every type has a queue, the queues are exactly the four
agreed names, and the ids a nudge builds are stable and URL / log safe.
"""

from __future__ import annotations

import pytest
from alkera_core.config import TASK_QUEUE_NAMES
from alkera_core.extensions import ExtensionPoint
from alkera_core.temporal import (
    JOB_QUEUES,
    MORE_WORK_SIGNAL,
    QUEUE_FOR,
    JobQueueTable,
    TaskQueue,
    WorkflowType,
    drain_workflow_id,
    keyed_workflow_id,
)

# The queue each job lands on — spelled out here (not derived from QUEUE_FOR) so
# a mis-assigned job in the contract fails this test instead of silently moving
# a metering job onto the housekeeping queue.
EXPECTED_QUEUES: dict[str, str] = {
    "auth.prune_expired_tokens": "default",
    "auth.prune_expired_device_codes": "default",
    "auth.prune_login_lockouts": "default",
    "auth.prune_identity_security_events": "default",
    "notebooks.sweep_runs": "default",
    "entitlements.watchdog": "default",
    "deployment_health.run": "default",
    "compute.meter": "money",
    # Reconciling the provider's fleet against the rows that own it can end a
    # rental the ledger never opened, which is money.
    "compute.reconcile": "money",
    "compute.org_machine_reconcile": "money",
    "compute.sweep": "default",
    # The catalog refresh re-prices machine types for the NEXT rental; a running
    # machine keeps the price pinned at its start, so nothing here is money.
    "compute.catalog": "default",
    "workspace.machine_move": "default",
    "workspace.machine_move_recover": "default",
    # Reaping an expired chat spare is housekeeping: no money, no connector.
    "chat.reap_spares": "default",
    "workspace.reconcile": "default",
    # Finishing a deleted workspace's chats is housekeeping: no money, no connector.
    "workspace.finish_deletions": "default",
    "account.lifecycle_sweep": "default",
    "account.reerase": "default",
    # Files runs its tree work on the housekeeping queue: none of it is money
    # and none of it is a connector sync, and a long ACL rewrite must not sit
    # in front of a receipt or a credential check.
    "files.promote": "default",
    "files.janitor": "default",
    "files.gc": "default",
    "files.acl_rewrite": "default",
    "files.large_move": "default",
    "files.copy": "default",
    "files.bulk": "default",
    "files.recover_queued": "default",
}


def test_queue_values_are_exactly_the_four_names() -> None:
    assert [q.value for q in TaskQueue] == ["money", "email", "sync", "default"]


def test_settings_and_contract_agree_on_the_queue_names() -> None:
    """The settings validator cannot import the contract (import cycle), so it
    carries its own copy of the names; the two spellings must never drift."""
    assert tuple(q.value for q in TaskQueue) == TASK_QUEUE_NAMES


def test_every_workflow_type_has_a_queue() -> None:
    assert set(WorkflowType) <= set(QUEUE_FOR)
    assert len(WorkflowType) == 27


def test_a_registered_family_adds_its_types_beside_the_platforms() -> None:
    point: ExtensionPoint[dict[str, TaskQueue]] = ExtensionPoint("test_job_queues")
    point.register({"family.drain": TaskQueue.SYNC})
    table = JobQueueTable({"files.gc": TaskQueue.DEFAULT}, point)
    assert dict(table) == {"files.gc": TaskQueue.DEFAULT, "family.drain": TaskQueue.SYNC}
    assert point.frozen


def test_a_platform_lookup_leaves_the_registrations_open() -> None:
    point: ExtensionPoint[dict[str, TaskQueue]] = ExtensionPoint("test_job_queues")
    table = JobQueueTable({"files.gc": TaskQueue.DEFAULT}, point)
    assert table["files.gc"] is TaskQueue.DEFAULT
    point.register({"family.drain": TaskQueue.SYNC})
    assert table["family.drain"] is TaskQueue.SYNC


def test_with_nothing_registered_the_table_is_the_platforms() -> None:
    table = JobQueueTable({"files.gc": TaskQueue.DEFAULT}, ExtensionPoint("test_job_queues"))
    assert dict(table) == {"files.gc": TaskQueue.DEFAULT}


def test_a_type_declared_twice_is_refused() -> None:
    point: ExtensionPoint[dict[str, TaskQueue]] = ExtensionPoint("test_job_queues")
    point.register({"files.gc": TaskQueue.SYNC})
    table = JobQueueTable({"files.gc": TaskQueue.DEFAULT}, point)
    with pytest.raises(ValueError, match="declared twice"):
        dict(table)


def test_the_job_queue_point_is_the_contracts() -> None:
    assert JOB_QUEUES.name == "temporal_job_queues"


def test_queue_for_is_read_only() -> None:
    with pytest.raises(TypeError):
        QUEUE_FOR[WorkflowType.FILES_GC] = TaskQueue.DEFAULT  # type: ignore[index]


@pytest.mark.parametrize(
    ("type_name", "queue"),
    [pytest.param(name, queue, id=name) for name, queue in EXPECTED_QUEUES.items()],
)
def test_each_workflow_type_lands_on_its_agreed_queue(type_name: str, queue: str) -> None:
    assert QUEUE_FOR[type_name] == TaskQueue(queue)


def test_the_expected_table_is_itself_exhaustive() -> None:
    """Guard the guard: the pinned table above covers every member, so a new
    workflow type cannot be added without also deciding its queue here."""
    assert set(EXPECTED_QUEUES) == {t.value for t in WorkflowType}


def test_workflow_type_values_are_the_historical_task_names() -> None:
    """Each value is `<family>.<job>` — the search key an operator already knows."""
    for t in WorkflowType:
        family, _, job = t.value.partition(".")
        assert family and job, t
        assert " " not in t.value and "/" not in t.value


def test_workflow_type_values_are_unique() -> None:
    assert len({t.value for t in WorkflowType}) == len(WorkflowType)


def test_more_work_signal_name() -> None:
    assert MORE_WORK_SIGNAL == "more_work"


@pytest.mark.parametrize("workflow", list(WorkflowType), ids=lambda t: t.value)
def test_drain_workflow_id_is_the_type_name(workflow: WorkflowType) -> None:
    assert drain_workflow_id(workflow) == workflow.value


def test_keyed_workflow_id_shape() -> None:
    key = "0b6a8b6e-3f2c-4d6e-9a1b-2c3d4e5f6a7b"
    assert keyed_workflow_id(WorkflowType.FILES_PROMOTE, key) == f"files.promote:{key}"


def test_keyed_workflow_ids_are_distinct_per_key_and_per_type() -> None:
    a = keyed_workflow_id(WorkflowType.FILES_PROMOTE, "k1")
    b = keyed_workflow_id(WorkflowType.FILES_PROMOTE, "k2")
    c = keyed_workflow_id(WorkflowType.WORKSPACE_MACHINE_MOVE, "k1")
    assert len({a, b, c}) == 3


def test_a_keyed_id_never_collides_with_a_drain_id() -> None:
    """A keyed run must not attach to (or be signalled as) the singleton drain."""
    keyed = {keyed_workflow_id(t, "x") for t in WorkflowType}
    drains = {drain_workflow_id(t) for t in WorkflowType}
    assert keyed.isdisjoint(drains)


@pytest.mark.parametrize(
    "bad_key",
    [
        pytest.param("", id="empty"),
        pytest.param("has space", id="space"),
        pytest.param("tab\tinside", id="tab"),
        pytest.param("new\nline", id="newline"),
        pytest.param("a/b", id="slash"),
    ],
)
def test_keyed_workflow_id_rejects_unsafe_keys(bad_key: str) -> None:
    with pytest.raises(ValueError, match="workflow key"):
        keyed_workflow_id(WorkflowType.FILES_PROMOTE, bad_key)


@pytest.mark.parametrize("workflow", list(WorkflowType), ids=lambda t: t.value)
def test_ids_carry_no_whitespace_or_slash(workflow: WorkflowType) -> None:
    for built in (drain_workflow_id(workflow), keyed_workflow_id(workflow, "abc-123")):
        assert not any(ch.isspace() for ch in built)
        assert "/" not in built
