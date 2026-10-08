"""The two HOT background-job management tools (root-only).

Backgrounding a tool call (``bash`` / ``sql.query`` / ``spawn_agent`` with
``background: true``) registers a job the model is NOTIFIED about on completion.
These tools cover what the push notification can't:

- ``background_status`` — an on-demand look at what's still running (and a
  never-finishing daemon's latest output). NOT for polling — every job notifies
  automatically when it finishes.
- ``background_cancel`` — stop a job you no longer need (a dev server, a runaway
  command, superseded work).

Both are ``app="background"`` + READ-hinted (so they never prompt) + root-only
(a subagent never sees them, gated at the descriptor list AND the dispatch
backstop). They reach the chat's ``BackgroundJobRegistry`` through ``ctx.background``
— typed loosely (duck-typed) so this module stays decoupled from the harness layer
that defines the registry.
"""

from __future__ import annotations

import os
import time
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from alkera_cli.agefmt import format_age_ago
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.host.limits import env_count
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

#: Cap on a job's output preview in ``background_status`` (chars). Keeps the status
#: payload small — the full result arrives via the completion notification, so this
#: only decides how much a polling model sees early. 0 previews the whole output.
ENV_BACKGROUND_PREVIEW_CHARS = "ALKERA_BACKGROUND_PREVIEW_CHARS"
_PREVIEW_CAP = env_count(os.environ.get(ENV_BACKGROUND_PREVIEW_CHARS), default=2000)


class BackgroundJobView(BaseModel):
    """One job's state for ``background_status`` — relative times only (the repo
    convention, via ``format_age_ago``), never raw timestamps."""

    job_id: str
    kind: str
    """``bash`` | ``sql`` | ``agent``."""
    title: str = ""
    state: str
    """``running`` | ``completed`` | ``error`` | ``cancelled``."""
    started_ago: str = ""
    completed_ago: str | None = None
    child_session_id: str | None = None
    """For an ``agent`` job — the child chat to drill into."""
    output_preview: str | None = None
    error: str | None = None


class BackgroundStatusInput(BaseModel):
    job_id: str | None = None
    """A specific background job id to inspect. Omit to list every background job in
    this session with its state."""


class BackgroundStatusResult(BaseModel):
    jobs: list[BackgroundJobView] = Field(default_factory=list)


def _view(job: Any, now: float) -> BackgroundJobView:
    """Build a status view from a registry ``BackgroundJob`` snapshot (duck-typed)."""
    result = getattr(job, "result", None)
    child_sid = getattr(result, "child_session_id", None) if result is not None else None
    preview = (getattr(job, "preview", "") or "").strip()
    if _PREVIEW_CAP is not None and len(preview) > _PREVIEW_CAP:
        preview = "…\n" + preview[-_PREVIEW_CAP:]
    completed_at = getattr(job, "completed_at", None)
    return BackgroundJobView(
        job_id=str(job.job_id),
        kind=str(job.kind),
        title=str(getattr(job, "title", "") or ""),
        state=str(job.state),
        started_ago=format_age_ago(now, float(job.started_at)),
        completed_ago=(format_age_ago(now, float(completed_at)) if completed_at else None),
        child_session_id=str(child_sid) if child_sid else None,
        output_preview=preview or None,
        error=getattr(job, "error", None),
    )


class BackgroundStatusTool(Tool[BackgroundStatusInput, BackgroundStatusResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="background_status",
        title="Background job status",
        description=(
            "List your background jobs and their state — running, completed, or "
            "failed — for an on-demand check of what's still in flight. Pass a job_id "
            "for just that one job's state + latest output preview; omit it to list "
            "them all. You do NOT need this to wait — every job notifies you "
            "automatically when it finishes. Never call this in a loop to poll."
        ),
        app="background",
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BackgroundStatusInput
    Output: ClassVar[type[BaseModel]] = BackgroundStatusResult

    async def run(self, args: BackgroundStatusInput, ctx: ToolContext) -> BackgroundStatusResult:
        registry = ctx.background
        if registry is None:
            raise ToolError("background jobs are only available in a root chat")
        now = time.time()
        if args.job_id is not None:
            job = registry.get(args.job_id)
            if job is None:
                raise ToolError(f"no background job {args.job_id!r}")
            return BackgroundStatusResult(jobs=[_view(job, now)])
        return BackgroundStatusResult(jobs=[_view(j, now) for j in registry.list()])


class BackgroundCancelInput(BaseModel):
    job_id: str
    """The id of the background job to cancel (from background_status or the job's
    start result)."""


class BackgroundCancelResult(BaseModel):
    job_id: str
    state: str
    """The job's state after the cancel (``cancelled``, or its terminal state if it
    had already finished)."""
    cancelled: bool
    """True iff this call transitioned a running job to cancelled."""


class BackgroundCancelTool(Tool[BackgroundCancelInput, BackgroundCancelResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="background_cancel",
        title="Cancel a background job",
        description=(
            "Cancel a running background job by id — terminate whatever it is "
            "running and stop its work. Use when you started a job you no "
            "longer need (wrong command, superseded work, or it's hanging). "
            "Cancelling a job that already finished is a no-op; the cancelled job "
            "still reports back as cancelled."
        ),
        app="background",
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BackgroundCancelInput
    Output: ClassVar[type[BaseModel]] = BackgroundCancelResult

    async def run(self, args: BackgroundCancelInput, ctx: ToolContext) -> BackgroundCancelResult:
        registry = ctx.background
        if registry is None:
            raise ToolError("background jobs are only available in a root chat")
        # Capture the state BEFORE cancelling so `cancelled` reflects an actual
        # transition (its contract), not a no-op double-cancel of an already-terminal job.
        before = registry.get(args.job_id)
        if before is None:
            raise ToolError(f"no background job {args.job_id!r}")
        was_running = str(before.state) == "running"
        job = await registry.cancel(args.job_id)
        if job is None:
            raise ToolError(f"no background job {args.job_id!r}")
        return BackgroundCancelResult(
            job_id=str(job.job_id),
            state=str(job.state),
            cancelled=was_running and str(job.state) == "cancelled",
        )


def register_background_tools(registry: ToolRegistry) -> None:
    """Register the two hot background-management tools. Always present; a subagent
    never SEES them (withheld via ``allow_background_tools``) and a child dispatch is
    refused (the ``app=="background"`` + ``spawn is None`` backstop)."""
    registry.register(BackgroundStatusTool)
    registry.register(BackgroundCancelTool)


__all__ = [
    "BackgroundCancelInput",
    "BackgroundCancelResult",
    "BackgroundCancelTool",
    "BackgroundJobView",
    "BackgroundStatusInput",
    "BackgroundStatusResult",
    "BackgroundStatusTool",
    "register_background_tools",
]
