"""Every persisted state column belongs to a registered state machine, or is
listed here with the reason it needs none.

A column named ``state``, ``status``, ``*_state`` or ``*_status`` is a state
some actor writes and some actor must finish. One the registry does not know
is a state nothing is held to ending. The allowlist records today's columns,
each with its reason, and may only shrink: registering a column's machine
removes its entry, and a stale entry fails.
"""

from __future__ import annotations

import re

import alkera_core.models
import alkera_core.notebooks.models  # noqa: F401
from alkera_core.db.base import Base
from alkera_core.lifecycle import registered
from sqlalchemy import Column, Integer, MetaData, String, Table

STATE_COLUMN = re.compile(r"^(state|status|.*_state|.*_status)$")

#: Columns no registered machine owns, and why each needs none (yet).
ALLOWED: dict[str, str] = {
    "account_deletion_requests.status": "owned by the account lifecycle sweep; to register",
    "account_export_requests.status": "owned by the product's export sweep; to register",
    "billing_proxy_requests.status": "terminal on write: a request's recorded answer",
    "billing_subscriptions.status": "Stripe's word, mirrored; Stripe ends it",
    "chat_workspace_states.state": "a box's last report, read with its age; not a lifecycle",
    "compute_allocation_events.from_state": "history: an event row, never moved on",
    "compute_allocation_events.to_state": "history: an event row, never moved on",
    "compute_allocations.last_reported_status": "the status last announced, for change detection",
    "connection_inventory.status": "display: derived on every read",
    "connection_verifications.state": "owned by the verification recovery sweep; to register",
    "deployment_health_checks.status": "terminal on write: a check's result",
    "device_authorizations.status": "expires on read and by the daily prune",
    "file_conflicts.state": "waits on a person to resolve it",
    "file_idempotency_keys.status": "terminal on write, pruned by age",
    "file_lease_live_entries.state": "deleted with its lease by the reaper",
    "gate_receipts.status": "terminal on write: a gate's verdict",
    "kb_items.status": "a document's editorial state; waits on a person",
    "model_provider_configs.last_verified_status": "terminal on write: a check's result",
    "org_memberships.status": "waits on a person (active, deactivated)",
    "realtime_docs.state": "the document's content, not a lifecycle",
    "team_connections.credential_state": "waits on a person to re-authenticate",
    "team_connections.verification_state": "owned by the verification recovery sweep; to register",
    "user_oauth_tokens.state": "owned by the OAuth refresh job; to register",
}


def state_columns(metadata: MetaData) -> set[str]:
    return {
        f"{table.name}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if STATE_COLUMN.match(column.name)
    }


def owned() -> set[str]:
    return {column for machine in registered().values() for column in machine.columns}


def test_every_state_column_is_registered_or_allowed_with_a_reason() -> None:
    unowned = state_columns(Base.metadata) - owned()
    assert unowned == set(ALLOWED), "register the new state's machine, or say why it needs none"
    assert all(reason.strip() for reason in ALLOWED.values())


def test_a_registered_machine_names_columns_that_exist() -> None:
    real = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
    }
    named = {column for column in owned() if column.count(".") == 1}
    assert named <= real, sorted(named - real)


def test_the_scan_finds_a_new_state_column() -> None:
    decoy = MetaData()
    Table(
        "decoy_jobs",
        decoy,
        Column("id", Integer, primary_key=True),
        Column("run_state", String),
        Column("status", String),
        Column("statement", String),
    )
    assert state_columns(decoy) == {"decoy_jobs.run_state", "decoy_jobs.status"}
