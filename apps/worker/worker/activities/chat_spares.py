"""The activity behind the chat-spare sweep: a thin wrapper around the core in
``worker.tasks.chat_spares``, under its advisory lock and heartbeating."""

from __future__ import annotations

from functools import partial

from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.activities._sweep import clock, locked_sweep
from worker.tasks.chat_spares import run_reap_chat_spares

LOCK = "chat_reap_spares"


@activity.defn(name=WorkflowType.REAP_CHAT_SPARES.value)
async def reap_chat_spares(input: SweepInput | None = None) -> int:
    """Every minute: reap every warmed-ahead chat nobody claimed in time.
    Returns how many were reaped; ``0`` when another pass held the lock."""
    reaped = await locked_sweep(
        LOCK,
        partial(run_reap_chat_spares, clock(input)),
        skipped_event="chat.spares.skipped_locked",
    )
    if reaped is None:
        return 0
    return reaped
