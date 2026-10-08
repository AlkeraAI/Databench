"""compute: validate the provider and acquisition checks 0222 and 0223 widened

Revision ID: 0224
Revises: 0223
Create Date: 2026-10-06

0222 and 0223 replaced ``ck_compute_machine_types_provider`` and
``ck_org_machines_acquisition`` without checking the rows already there. This
revision checks them, each in a transaction of its own, so the scan holds no
writer off the table, and a deploy whose scan runs long or fails is this one,
not the one that widened the vocabulary. Safe to run again.

``downgrade()`` leaves the checks validated: 0223's and 0222's own downgrades
put the narrower ones back.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.migration_safety import bound_lock_wait, validate_constraints

revision: str = "0224"
down_revision: str | None = "0223"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
#: Validates two open tables' constraints.
SIDE: str = "open"


def upgrade() -> None:
    bound_lock_wait()
    validate_constraints(
        [
            ("compute_machine_types", "ck_compute_machine_types_provider"),
            ("org_machines", "ck_org_machines_acquisition"),
        ]
    )


def downgrade() -> None:
    bound_lock_wait()
