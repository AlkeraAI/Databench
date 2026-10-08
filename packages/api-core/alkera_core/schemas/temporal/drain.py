"""The generic drain-loop shapes shared by every inbox / sweep workflow.

A drain workflow runs an activity in passes until the work is gone: the Stripe
event inbox, the GitHub delivery inbox and the enterprise-enrollment sweep all
share the loop, and these three models are what the loop reads and returns
across the workflow / activity boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, model_validator

from alkera_core.versioning import VersionedModel


class DrainInput(VersionedModel):
    """What a drain workflow is started with. Every field has a default so a
    signal-with-start nudge can start the workflow with no argument at all."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    now: datetime | None = None
    """The clock the pass runs against; ``None`` means the activity's wall clock.
    Tests pin it here because a frozen clock cannot cross a server round trip."""
    limit: int = Field(default=100, ge=1)
    """Rows one pass applies at most. A pass that fills the page is followed by
    another pass even without a nudge — a full page means more may be waiting."""
    max_passes: int = Field(default=8, ge=1)
    """Upper bound on passes per run; the schedule is the net past it."""
    locked_retries: int = Field(default=5, ge=0)
    """How many times a pass that lost the advisory lock is retried before the
    run gives up (a scheduled drain and a nudged one raced)."""
    locked_retry_seconds: float = Field(default=2.0, ge=0)
    """The pause before each locked retry."""


class DrainOutcome(VersionedModel):
    """What one pass reports back to the loop.

    The page is reported on two sides. The loop goes again on a full page of
    ``applied`` rows and never on ``failed`` ones: a page of rows that all
    failed (a Stripe incident, a poison batch) is not progress, and re-running
    it back to back only hammers the dependency that is down. ``processed`` is
    the total of both, kept for readers of the earlier shape.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    processed: int = Field(default=0, ge=0)
    """Rows applied or marked failed in this pass: ``applied + failed``."""
    applied: int = Field(default=0, ge=0)
    """Rows whose effect committed."""
    failed: int = Field(default=0, ge=0)
    """Rows whose apply raised and were recorded as failed (an attempt burned,
    or a transient blip noted); they stay in the inbox for a later pass."""
    skipped_locked: bool = False
    """Another run held the advisory lock; nothing was attempted."""
    skipped_unconfigured: bool = False
    """The integration is not configured (no Stripe / GitHub credentials); there
    can be no work, so the loop stops without retrying."""

    @model_validator(mode="before")
    @classmethod
    def _keep_the_total_and_the_split_in_step(cls, data: Any) -> Any:
        """A writer of the earlier shape reported only the total, and the loop
        went again on it — so it reads as applied, exactly as it was treated. A
        writer of this shape may give only the split; the total is derived.
        Anything else is left to field validation."""
        if not isinstance(data, dict):
            return data
        processed = data.get("processed")
        applied = data.get("applied")
        failed = data.get("failed")
        if applied is None and failed is None and isinstance(processed, int):
            return {**data, "applied": processed}
        if processed is None and (applied is not None or failed is not None):
            halves = (applied if applied is not None else 0, failed if failed is not None else 0)
            if all(isinstance(half, int) for half in halves):
                return {**data, "processed": sum(halves)}
        return data


class DrainReport(VersionedModel):
    """The run's result: the workflow's return value, visible in the UI."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    passes: int = Field(default=0, ge=0)
    processed: int = Field(default=0, ge=0)
    emails: int = Field(default=0, ge=0)
    """Billing emails the run's dispatch sent: the email dispatch workflow fills
    it; a drain that hands its emails to that workflow leaves it at zero."""
    locked_retries: int = Field(default=0, ge=0)
