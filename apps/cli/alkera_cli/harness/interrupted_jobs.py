"""Background jobs a restart cut off, found in a chat's log when it is opened.

A background job (a shell, a query, an integration call) lives in the process
that started it. When that process ends first (a machine restart that outlasted
the drain ceiling, a crash), the job dies with it, and nothing in the next
process would ever say so: the chat's transcript shows the job's START card
(``bgjob:<id>``) and never its FINISH card (``bgdone:<id>``), and the model,
which was told it would hear back, hears nothing.

:func:`interrupted_background_jobs` reads that from the log, purely: a START
card with no FINISH card is a job that was interrupted, because no job of a
process that has ended can still be running. The session that opens the chat
delivers each as a finished job whose error says what happened
(:func:`interruption_notice`), through the same path a job that failed would
take: the reader sees the finish card, and the model is woken with it. Once its
FINISH card is in the log the job is not found again.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from alkera_core.schemas.chat import Event, ToolCall, ToolCallUpdate

from alkera_cli.harness.background import BackgroundJob

START_CARD: Final = "bgjob:"
FINISH_CARD: Final = "bgdone:"

#: The job kind each background tool card names (the reverse of the runtime's
#: model-facing names); a card naming another tool is not a background job.
JOB_KIND_OF_TOOL: Final[Mapping[str, str]] = {
    "bash": "bash",
    "sql.query": "sql",
    "call_integration_sdk": "integration_sdk",
}


@dataclass(frozen=True, slots=True)
class InterruptedJob:
    job_id: str
    kind: str
    started: datetime
    #: The last moment the log heard from the process that ran the job.
    ended: datetime
    input: Mapping[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        for key in ("description", "command", "sql", "name"):
            value = self.input.get(key)
            if isinstance(value, str) and value.strip():
                text = " ".join(value.split())
                return text if len(text) <= 80 else text[:79] + "…"
        return self.kind

    @property
    def minutes(self) -> int:
        """How long the job had run when it was cut off, in whole minutes, at
        least one."""
        return max(1, math.ceil((self.ended - self.started).total_seconds() / 60))


def interrupted_background_jobs(events: Iterable[Event]) -> list[InterruptedJob]:
    """The background jobs ``events`` started and never finished, oldest
    first. A job ended at the last event before the next open closed its START
    card as interrupted (``resume_reconcile``), else at the log's last event."""
    started: dict[str, ToolCall] = {}
    finished: set[str] = set()
    cut: dict[str, datetime] = {}
    last: datetime | None = None
    for event in events:
        if (
            isinstance(event, ToolCallUpdate)
            and event.status == "error"
            and event.tool_call_id.startswith(START_CARD)
            and last is not None
        ):
            cut.setdefault(event.tool_call_id.removeprefix(START_CARD), last)
        last = event.time
        if not isinstance(event, ToolCall):
            continue
        if event.tool_call_id.startswith(START_CARD):
            started.setdefault(event.tool_call_id.removeprefix(START_CARD), event)
        elif event.tool_call_id.startswith(FINISH_CARD):
            finished.add(event.tool_call_id.removeprefix(FINISH_CARD))
    return [
        InterruptedJob(
            job_id=job_id,
            kind=JOB_KIND_OF_TOOL[card.tool_name],
            started=card.time,
            ended=max(card.time, cut.get(job_id) or last or card.time),
            input=dict(card.input or {}),
        )
        for job_id, card in started.items()
        if job_id not in finished and card.tool_name in JOB_KIND_OF_TOOL
    ]


def interruption_notice(job: InterruptedJob) -> str:
    """What the model and the reader are told."""
    unit = "minute" if job.minutes == 1 else "minutes"
    return (
        f"The background job {job.job_id} ({job.title}) was interrupted by a machine restart "
        f"after {job.minutes} {unit}; it did not finish."
    )


def as_finished_job(job: InterruptedJob) -> BackgroundJob:
    """The job as the registry would have finished it: failed, with the
    notice as its error."""
    return BackgroundJob(
        job_id=job.job_id,
        kind=job.kind,
        title=job.title,
        state="error",
        started_at=job.started.timestamp(),
        completed_at=job.ended.timestamp(),
        error=interruption_notice(job),
        input=dict(job.input),
    )


__all__ = [
    "FINISH_CARD",
    "JOB_KIND_OF_TOOL",
    "START_CARD",
    "InterruptedJob",
    "as_finished_job",
    "interrupted_background_jobs",
    "interruption_notice",
]
