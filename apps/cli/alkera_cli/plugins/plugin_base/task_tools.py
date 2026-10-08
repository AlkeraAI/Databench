"""``manage_tasks`` — the agent's unified TODO system (one hot, root-only tool).

A single tool over a per-chat task DAG (persisted via
:class:`alkera_core.project.chats.tasks.TaskStore`). It does everything —
add (``upsert`` a new id), edit / mark-done / change-deps (``upsert`` an existing
id), remove (``delete``), wipe (``clear``) — and a no-arg call just reads. Every
call returns the FULL current list with derived ``blocked`` / ``blocked_by`` so
the model is always re-grounded in the live graph (the start-of-turn reminder
shows the same list, so a standalone read is rarely needed).

It is ``effect_hint=READ`` (it mutates only agent-internal ``.alkera`` bookkeeping
— never the user's world — so it must never trigger a permission prompt). It is
withheld from subagents by the ``app="tasks"`` root-only gate (see
``mcp_entry.alkera_tool_descriptors`` + ``ToolRegistry.dispatch``), NOT by effect.

Model-facing time is RELATIVE ("5m ago"), never a raw timestamp — the same
``format_age_ago`` the context tools use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from alkera_core.schemas.chat.tasks import TaskList, TaskStatus, TaskValidationError
from pydantic import BaseModel, Field

from alkera_cli.agefmt import format_age_ago
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

_DESCRIPTION = """\
Your task list / TODO for this session — a DAG of tasks you USE to plan, track, and \
finish large or multi-step work. This ONE tool does everything: add, edit, complete, \
remove, and clear. Every call returns the full current list with each task's derived \
`blocked` status and `blocked_by` ids, so you always see live state.

Parameters (combine freely; applied in order clear → delete → upsert):
- `upsert`: add or edit tasks. A new `id` creates a task (needs a `title`); an existing \
`id` edits ONLY the fields you pass (others are untouched). Use it to add work, edit a \
title/description, mark a task done (`status:"completed"`), or change `depends_on`.
- `delete`: remove tasks by id.
- `clear`: true wipes the WHOLE list — do this when the work is finished or the list is \
no longer relevant, so it doesn't linger.

A task: `id` (a short stable slug you choose, e.g. "create-schema" — referenced by \
other tasks' `depends_on`), `title`, optional `description`, `status` \
(pending/in_progress/completed/cancelled), and `depends_on` (ids of prerequisites). \
A task is `blocked` until every prerequisite is `completed`; the unblocked pending \
tasks are the work you can start now (returned as `ready`). Dependencies must form a \
DAG — cycles, unknown ids, and self-deps are rejected with an explanatory error.

When to use it — proactively, for any non-trivial work:
- 3+ distinct steps, a multi-file change, or interdependent work → lay out the DAG up \
front with one `upsert` call. When in doubt, use it.
- Skip only a single trivial edit or a purely conversational/informational reply.

How to use it well:
- Mark a task `in_progress` BEFORE you start it; mark it `completed` ONLY after the work \
is actually done and verified (never on intent). Keep roughly one task `in_progress`.
- Re-check and update it OFTEN — after finishing each step, and add follow-up tasks as \
you discover them. Treat it as your working memory across the whole task.
- For non-trivial work, include a final task to review the change — re-read your own diff \
and run the checks — and satisfy it before you deliver.
"""


class TaskUpsert(BaseModel):
    """One add-or-edit. A new ``id`` creates (``title`` required); an existing ``id``
    edits only the fields provided here."""

    id: str = Field(description="Stable slug; new id = create, existing id = edit.")
    title: str | None = Field(default=None, description="Required when creating a task.")
    description: str | None = None
    status: TaskStatus | None = Field(
        default=None, description="pending | in_progress | completed | cancelled."
    )
    depends_on: list[str] | None = Field(
        default=None, description="Ids of prerequisite tasks (replaces the current list)."
    )


class ManageTasksInput(BaseModel):
    upsert: list[TaskUpsert] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    clear: bool = Field(default=False, description="Wipe the whole list (work finished).")


class TaskView(BaseModel):
    """A task as the model sees it — full detail + derived blocked state +
    RELATIVE updated time (never a raw timestamp)."""

    id: str
    title: str
    description: str = ""
    status: TaskStatus
    depends_on: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_by: list[str] = Field(default_factory=list)
    updated_ago: str = ""


class TaskSummary(BaseModel):
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    ready: list[str] = Field(default_factory=list)
    """Unblocked pending task ids — the work you can start now."""


class ManageTasksOutput(BaseModel):
    tasks: list[TaskView] = Field(default_factory=list)
    summary: TaskSummary = Field(default_factory=TaskSummary)


def _view(task_list: TaskList, *, now: datetime) -> ManageTasksOutput:
    """Build the model-facing view: every task with derived blocked state and a
    relative ``updated_ago``, plus a summary."""
    now_epoch = now.timestamp()
    views = [
        TaskView(
            id=t.id,
            title=t.title,
            description=t.description,
            status=t.status,
            depends_on=list(t.depends_on),
            blocked=task_list.is_blocked(t.id),
            blocked_by=task_list.blocked_by(t.id),
            updated_ago=format_age_ago(now_epoch, t.updated_at.timestamp()) if t.updated_at else "",
        )
        # Active tasks first, finished ones last — upcoming work on top (the cards +
        # the TUI inherit this order from the JSON they render).
        for t in task_list.display_order()
    ]
    return ManageTasksOutput(
        tasks=views,
        summary=TaskSummary(
            total=len(task_list.tasks),
            counts=task_list.status_counts(),
            ready=[t.id for t in task_list.ready()],
        ),
    )


class ManageTasksTool(Tool[ManageTasksInput, ManageTasksOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="manage_tasks",
        title="Manage tasks",
        description=_DESCRIPTION,
        hot=True,
        # READ so it never prompts — task state is agent-internal, not a world write.
        # Subagent denial is the app="tasks" root-only gate, not the effect.
        effect_hint=Effect.READ,
        app="tasks",
    )
    Input: ClassVar[type[BaseModel]] = ManageTasksInput
    Output: ClassVar[type[BaseModel]] = ManageTasksOutput

    async def run(self, args: ManageTasksInput, ctx: ToolContext) -> ManageTasksOutput:
        store = ctx.task_store
        if store is None:
            raise ToolError("the task system is unavailable in this session")
        now = datetime.now(UTC)
        if not args.upsert and not args.delete and not args.clear:
            return _view(await store.load(), now=now)  # no-op call = read
        try:
            updated = await store.apply(
                upsert=[u.model_dump(exclude_unset=True) for u in args.upsert],
                delete=args.delete,
                clear=args.clear,
                now=now,
            )
        except TaskValidationError as exc:
            raise ToolError(str(exc)) from exc
        return _view(updated, now=now)


def register_task_tools(registry: ToolRegistry) -> None:
    """Register the hot ``manage_tasks`` tool (the unified TODO system). Always
    registered (core); the ``app="tasks"`` gate keeps it root-only at run time."""
    registry.register(ManageTasksTool)


__all__ = [
    "ManageTasksInput",
    "ManageTasksOutput",
    "ManageTasksTool",
    "TaskSummary",
    "TaskUpsert",
    "TaskView",
    "register_task_tools",
]
