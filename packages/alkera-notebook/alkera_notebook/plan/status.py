"""A cell's displayed status, relative to the current kernel."""

from __future__ import annotations

from typing import Literal

from alkera_notebook.engine.models import CellStatus

Activity = Literal["queued", "running"] | None


def display_status(
    *,
    runtime: CellStatus,
    activity: Activity,
    disabled: bool,
    current_code: str,
    submitted_code: str | None,
) -> CellStatus:
    """Queued and running win; then disabled; then ``edited`` when the text
    differs from what the kernel ran; then the runtime status."""
    if activity is not None:
        return activity
    if disabled:
        return "disabled"
    if submitted_code is not None and submitted_code != current_code:
        return "edited"
    return runtime
