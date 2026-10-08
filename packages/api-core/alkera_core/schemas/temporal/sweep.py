"""The input of a single-pass sweep workflow: the clock it runs against.

The billing maintenance sweeps (grant expiry, pool-window reset, funnel prune,
the free-cycle roll and backfill) run exactly one activity per run, and the one
thing a caller may need to choose is the instant that activity treats as
"now". The cores took ``now`` as an argument for the same reason — a global
sweep pinned to a clock only reaches the rows that clock selects, which is how
a test scopes it to its own seeds on a shared database — and a workflow needs
the seam spelled out because a frozen clock does not cross the server round
trip. A schedule starts the workflow with no input at all: the wall clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import field_validator

from alkera_core.versioning import VersionedModel


class SweepInput(VersionedModel):
    """What a single-pass sweep workflow is started with. Every field defaults
    so a scheduled run can start the workflow with no argument."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    now: datetime | None = None
    """The instant the sweep runs against; ``None`` means the activity's wall
    clock. Must be timezone-aware: the rows it is compared with are."""

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("a sweep clock must be timezone-aware")
        return value
