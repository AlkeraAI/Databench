"""compute: a self-hosted box registers under its own provider kind

Revision ID: 0223
Revises: 0222
Create Date: 2026-10-06

* ``ck_compute_machine_types_provider`` admits ``self_hosted``: the box a
  one-machine install runs beside its services registers as that kind rather
  than as a RunPod flavor.

Hand-written. The constraint swap is added NOT VALID; 0224 validates it in a
transaction of its own, and ``bound_lock_wait`` keeps a busy table from
stalling a deploy.

``downgrade()`` restores the narrower constraint NOT VALID, leaving rows a
database already holds rather than failing.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.migration_safety import add_check_not_valid, bound_lock_wait

revision: str = "0223"
down_revision: str | None = "0222"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
#: Widens an open table's constraint.
SIDE: str = "open"

# Spelled literally: a migration describes the schema at ITS revision.
_PROVIDERS_BEFORE: tuple[str, ...] = ("container", "ec2", "localdev", "personal", "runpod", "ssh")
_PROVIDERS_AFTER: tuple[str, ...] = (
    "container",
    "ec2",
    "localdev",
    "personal",
    "runpod",
    "self_hosted",
    "ssh",
)

TABLE = "compute_machine_types"
NAME = "ck_compute_machine_types_provider"


def _in(values: Sequence[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _swap(values: Sequence[str]) -> None:
    add_check_not_valid(TABLE, NAME, f"provider IN {_in(values)}")


def upgrade() -> None:
    bound_lock_wait()
    _swap(_PROVIDERS_AFTER)


def downgrade() -> None:
    bound_lock_wait()
    _swap(_PROVIDERS_BEFORE)
