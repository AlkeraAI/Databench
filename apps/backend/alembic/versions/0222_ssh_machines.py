"""compute: an org may add a machine it runs by its SSH details

Revision ID: 0222
Revises: 0221
Create Date: 2026-10-06

* ``ssh_machine_endpoints``: where an added org machine is reached (host,
  port, username, the auth kind, the sealed credential and the host key the
  admin confirmed). A tenant table under the ``tenant_isolation`` policy for
  ``alkera_tenant_app``, the same statements revision 0196 used. Its pointer
  at the org machine is a composite foreign key onto
  ``org_machines(id, org_team_id)``.
* ``ck_compute_machine_types_provider`` admits ``ssh``.
* ``ck_org_machines_acquisition`` admits ``added``.

Hand-written. Each constraint swap is added NOT VALID; 0224 validates them in
a transaction of its own. The indexes are built concurrently, and
``bound_lock_wait`` keeps a busy table from stalling a deploy. Idempotent: the
table is created only when missing.

``downgrade()`` drops the table and restores the narrower constraints NOT
VALID, leaving rows a database already holds rather than failing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from backend.migration_safety import (
    add_check_not_valid,
    bound_lock_wait,
    create_index_concurrently,
)
from sqlalchemy import text

revision: str = "0222"
down_revision: str | None = "0220"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
#: Creates ssh_machine_endpoints and widens two open tables' constraints.
SIDE: str = "open"

TABLE = "ssh_machine_endpoints"
ROLE = "alkera_tenant_app"
POLICY = "tenant_isolation"
#: The content table this revision polices, and its tenant column; with 0188's
#: and the later policing revisions' lists it is
#: ``alkera_core.db.row_security.CONTENT_TABLES``.
TABLES: tuple[tuple[str, str], ...] = ((TABLE, "org_team_id"),)

# Spelled literally: a migration describes the schema at ITS revision.
_PROVIDERS_BEFORE: tuple[str, ...] = ("container", "ec2", "localdev", "personal", "runpod")
_PROVIDERS_AFTER: tuple[str, ...] = (*_PROVIDERS_BEFORE, "ssh")
_ACQUISITIONS_BEFORE: tuple[str, ...] = ("purchased", "granted")
_ACQUISITIONS_AFTER: tuple[str, ...] = (*_ACQUISITIONS_BEFORE, "added")

_CONSTRAINTS = (
    ("compute_machine_types", "ck_compute_machine_types_provider", "provider"),
    ("org_machines", "ck_org_machines_acquisition", "acquisition"),
)


def _in(values: Sequence[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _swap(table: str, name: str, column: str, values: Sequence[str]) -> None:
    add_check_not_valid(table, name, f"{column} IN {_in(values)}")


def _table_exists() -> bool:
    if op.get_context().as_sql:
        return False
    return bool(op.get_bind().execute(text(f"SELECT to_regclass('{TABLE}') IS NOT NULL")).scalar())


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_team_id", sa.Uuid(), nullable=False),
        sa.Column("org_machine_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("auth_kind", sa.String(length=16), nullable=False),
        sa.Column("secret_sealed", sa.Text(), server_default="", nullable=False),
        sa.Column("host_key", sa.Text(), nullable=False),
        sa.Column("host_key_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("os", sa.String(length=64), server_default="", nullable=False),
        sa.Column("arch", sa.String(length=32), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "auth_kind IN ('password', 'private_key')", name="ck_ssh_machine_endpoints_auth_kind"
        ),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_ssh_machine_endpoints_port"),
        sa.ForeignKeyConstraint(
            ["org_team_id"],
            ["teams.id"],
            name="fk_ssh_machine_endpoints_org_team_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_ssh_machine_endpoints_org_machine_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="ssh_machine_endpoints_pkey"),
    )


def _police() -> None:
    predicate = "org_team_id = ANY (alkera_org_ids())"
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} AS PERMISSIVE FOR ALL TO {ROLE} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {ROLE}")


def upgrade() -> None:
    bound_lock_wait()
    _swap(*_CONSTRAINTS[0], _PROVIDERS_AFTER)
    _swap(*_CONSTRAINTS[1], _ACQUISITIONS_AFTER)
    if not _table_exists():
        _create_table()
    _police()
    create_index_concurrently(
        "ux_ssh_machine_endpoints_org_machine_id", TABLE, "org_machine_id", unique=True
    )
    create_index_concurrently("ix_ssh_machine_endpoints_org_team_id", TABLE, "org_team_id")


def downgrade() -> None:
    bound_lock_wait()
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
    _swap(*_CONSTRAINTS[0], _PROVIDERS_BEFORE)
    _swap(*_CONSTRAINTS[1], _ACQUISITIONS_BEFORE)
