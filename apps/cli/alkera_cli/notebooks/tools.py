"""The notebook tools as harness tools: thin façades over ``alkera_notebook.tools``.

Each class here is one ``notebook.*`` tool. Its ``Input`` and ``Output`` are the
open core's models, so the schema a model sees and the shape the core returns
cannot drift. A call resolves three things from the chat and hands them to
:func:`alkera_notebook.tools.call_tool`:

- the **host**: the chat's workspace engine bound to the chat's agent as the
  actor, from the runtime's :class:`NotebookService` (``ToolRegistry.notebooks``);
- the **gatekeeper**: :class:`HarnessGatekeeper`, which turns the core's gate
  subject into an ``ActionDescriptor`` and decides it through the same
  ``DecisionEngine`` every other tool uses, so a notebook run is asked, judged,
  refused and audited exactly as the equivalent shell or edit action would be;
- the **cursor**: the chat starts following a notebook's activity the first
  time it touches it, which is what the per-turn digest reports from.

The decisions, by gate effect (the stance contract in
``apps/cli/tests/notebooks/test_nbagt_notebook_gates.py`` drives each one in
each mode):

- ``file_edit``: a ``fs`` write of the notebook file (every ``notebook.cells``
  action, clearing outputs included, is one). A notebook in the place
  the mode writes freely (the chat's sandbox, or the workspace's shared folder
  where the mode allows it) is admitted with no ask, as the harness admits the
  edit tool there; anywhere else the engine decides as for any file edit.
- ``run_cells``: every cell the plan executes is classified as the agent's own
  command would be (a Python cell as ``python -c`` through the shell, a SQL
  cell by ``sql.query``'s classifier on its connection), the most severe effect
  wins and the mode's row decides: a plan of reads runs unasked where a read
  does, a plan with a write asks where a write asks. A cell that cannot be
  classified is a write. A run that restarts the kernel while someone else's
  work runs is a ``destroy``.
- ``code_exec``: the effect the shell classifier gives ``python -c``, so an
  interrupt or restart is asked in ``default``, judged in ``auto``, refused in
  ``read_only`` and ``plan``. Ending or interrupting work someone else started
  is a ``destroy``: asked even in ``auto``.
- ``env_write``: an ``egress`` (it changes the environment and fetches
  packages).
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from alkera_core.permission_presentation.model import (
    AskNotebook,
    AskNotebookCell,
    NotebookCellRole,
)
from alkera_notebook.cell_names import cell_display_name
from alkera_notebook.digest import CursorStore, follow
from alkera_notebook.tools import (
    SKILL_BODY,
    SKILL_DESCRIPTION,
    SKILL_NAME,
    TOOLS,
    GateEffect,
    GateSubject,
    GateVerdict,
    NotebookHost,
    NotebookToolError,
    call_tool,
)
from alkera_notebook.tools.port import PlanStepRecord
from pydantic import BaseModel

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.plugins.plugin_base.delivery import StoreSpill
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_cli.plugins.plugin_base.permissions.classifier import descriptor_from_sql
from alkera_cli.plugins.plugin_base.permissions.commands import effect_max
from alkera_cli.plugins.plugin_base.permissions.consent_scope import standing_allow_scope
from alkera_cli.plugins.plugin_base.permissions.gate import (
    NO_SINK_REASON,
    binding_from_context,
    denied_error,
)
from alkera_cli.plugins.plugin_base.skill_tool import UseSkillTool
from alkera_cli.plugins.plugin_base.surfaces import SkillDef
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

#: The tool app every notebook tool belongs to.
NOTEBOOK_APP = "notebook"

#: Running notebook code is gated as running ``python -c`` through the shell:
#: the effect is read off the shell classifier itself, so the two cannot drift.
CODE_EXEC_EFFECT: Effect = classify_command("python -c pass").effect

#: How much of a run a permission card is sent: the cells, and each cell's code
#: (the card shows only the first lines; the rest bounds the event).
_PREVIEW_CELLS = 200
_CELL_CODE_CHARS = 2_000


@runtime_checkable
class NotebookService(Protocol):
    """What the runtime provides the notebook tools, per chat.

    One service per runtime, handed to the tool registry as
    ``ToolRegistry.notebooks``. It owns the workspace engines (one per
    workspace root) and the per-chat digest cursors."""

    def host_for(self, ctx: ToolContext) -> NotebookHost | None:
        """The chat's workspace notebooks with the chat's agent as the actor, or
        ``None`` when this chat has no workspace that can hold notebooks."""
        ...

    def notebook_path(self, ctx: ToolContext, path: str) -> str:
        """The notebook path the engine reads for one the agent typed, as its
        sandbox spells the chat's folder (the one mapping every notebook tool
        takes its ``path`` through)."""
        ...

    def file_path(self, ctx: ToolContext, path: str) -> Path:
        """The file a canonical notebook path names, as the file tools see it."""
        ...

    def edit_is_free(self, ctx: ToolContext, file: Path) -> bool:
        """Whether a file edit at ``file`` is admitted with no ask in the chat's
        current mode, as the harness admits its edit tool there."""
        ...

    def cursors_for(self, ctx: ToolContext) -> CursorStore | None:
        """The chat's digest cursors, or ``None`` for a session that keeps none
        (a subagent reports through its parent)."""
        ...

    def with_guide(self, ctx: ToolContext, result: BaseModel) -> BaseModel:
        """``result`` carrying the notebook guide when it is the first notebook
        tool result in this conversation (a subagent is a conversation of its
        own); unchanged otherwise."""
        ...


def notebook_service(ctx: ToolContext) -> NotebookService:
    service = getattr(ctx.registry, "notebooks", None)
    if service is None:
        raise ToolError("Notebooks are not available in this chat.")
    return service  # type: ignore[no-any-return]


def _descriptor(subject: GateSubject, file: Path) -> ActionDescriptor:
    """The action a notebook gate subject is, as the permission engine reads it.

    The notebook is named by its path in the workspace, which is what a person
    knows it by; only a file edit carries the path on this machine's disk,
    because that is what the edit rules match."""
    preview_target = ResourceRef(kind="notebook", name=subject.path)
    if subject.effect is GateEffect.FILE_EDIT:
        return ActionDescriptor(
            capability="fs",
            effect=Effect.WRITE,
            operation="edit",
            targets=[ResourceRef(kind="file", name=str(file))],
            raw=str(file),
            classifier="notebook",
        )
    if subject.effect is GateEffect.ENV_WRITE:
        return ActionDescriptor(
            capability="notebook",
            effect=Effect.EGRESS,
            operation="notebook_install",
            targets=[preview_target],
            raw=subject.title,
            reasons=["changes the notebook's environment, which can reach the network"],
            classifier="notebook",
        )
    verb = subject.tool.removeprefix("notebook.")
    reasons = ["runs code in the notebook's shared kernel"]
    effect = CODE_EXEC_EFFECT
    if subject.affects_others:
        effect = Effect.DESTROY
        reasons.append("ends or interrupts work started by " + ", ".join(subject.affects_others))
    return ActionDescriptor(
        capability="notebook",
        effect=effect,
        operation=f"notebook_{verb}",
        targets=[preview_target],
        raw=subject.title,
        reasons=reasons,
        classifier="notebook",
    )


@dataclass(frozen=True)
class StepClassification:
    """One planned cell as the permission engine reads it."""

    step: PlanStepRecord
    descriptor: ActionDescriptor

    @property
    def label(self) -> str:
        sure = "" if self.descriptor.confidence == "exact" else ", not certain"
        return f"{self.descriptor.effect}{sure}"

    @property
    def cell_name(self) -> str:
        """The cell as a person sees it: its name, else its position."""
        return cell_display_name(self.step.name, self.step.index)


def classify_step(step: PlanStepRecord, ctx: ToolContext) -> StepClassification:
    """A planned cell classified as the agent's own command would be: a SQL
    cell's statement by ``sql.query``'s classifier on its connection, any other
    cell's code as running it with ``python -c``. A statement that cannot be
    known before the run, or that does not parse, counts as a write."""
    if step.kind == "sql":
        if step.sql is None or step.interpolated:
            # Interpolated values exist only in the kernel: the statement that
            # will be sent is not known here, so it cannot be cleared as a read.
            descriptor = ActionDescriptor(
                capability="sql",
                effect=Effect.WRITE,
                operation="unknown",
                raw=step.sql or step.code,
                reasons=["the statement is completed in the kernel and cannot be read first"],
                classifier="notebook",
                confidence="unknown",
            )
        else:
            conn = ctx.registry.connection_named(step.connection) if step.connection else None
            if step.connection and conn is None:
                # A name this session cannot resolve to one connection has no
                # dialect to read the statement in: it is not cleared as a read.
                descriptor = ActionDescriptor(
                    capability="sql",
                    effect=Effect.WRITE,
                    operation="unknown",
                    raw=step.sql,
                    reasons=[f"no single connection named {step.connection!r} to read it for"],
                    classifier="notebook",
                    confidence="unknown",
                )
            else:
                dialect = (conn.dialect or "") if conn is not None else "duckdb"
                descriptor = descriptor_from_sql(
                    step.sql, dialect=dialect, capability="sql", connection=step.connection
                )
    else:
        descriptor = classify_command("python -c " + shlex.quote(step.code))
    if descriptor.confidence == "unknown" and descriptor.effect == Effect.READ:
        # Fail closed: what the classifier cannot read is a write, never a read.
        descriptor = descriptor.model_copy(update={"effect": Effect.WRITE})
    return StepClassification(step, descriptor)


def plan_descriptor(
    subject: GateSubject, file: Path, ctx: ToolContext
) -> tuple[ActionDescriptor, list[StepClassification]]:
    """A run's plan as one action: the most severe effect of any cell it
    executes, with every cell's own classification kept for the prompt."""
    plan = subject.plan
    steps = [classify_step(step, ctx) for step in (plan.steps if plan is not None else [])]
    effect = Effect.READ
    confidence: Literal["exact", "heuristic", "unknown"] = "exact"
    reasons: list[str] = []
    targets = [ResourceRef(kind="notebook", name=subject.path)]
    for item in steps:
        effect = effect_max(effect, item.descriptor.effect)
        if item.descriptor.confidence != "exact" and item.descriptor.effect == effect:
            confidence = "unknown" if item.descriptor.confidence == "unknown" else "heuristic"
        reasons.append(f"{item.cell_name}: {item.label}")
        targets.extend(item.descriptor.targets)
    if effect == Effect.READ:
        confidence = "exact"
    if subject.affects_others:
        # A run that restarts the kernel first ends whatever else is running.
        effect = Effect.DESTROY
        reasons.append("ends or interrupts work started by " + ", ".join(subject.affects_others))
    descriptor = ActionDescriptor(
        capability="notebook",
        effect=effect,
        operation=f"notebook_{subject.tool.removeprefix('notebook.')}",
        targets=targets,
        raw=subject.title,
        reasons=reasons,
        classifier="notebook",
        confidence=confidence,
        scope="command",
    )
    return descriptor, steps


#: Why a planned step runs, as the permission card groups it.
_CELL_ROLES: dict[str, NotebookCellRole] = {
    "target": "target",
    "upstream": "dependency",
    "descendant": "dependent",
}


def _ask_cell(step: PlanStepRecord) -> AskNotebookCell:
    code = (step.sql if step.kind == "sql" and step.sql is not None else step.code) or ""
    return AskNotebookCell(
        name=cell_display_name(step.name, step.index),
        code=code[:_CELL_CODE_CHARS],
        role=_CELL_ROLES.get(step.reason, "target"),
    )


def notebook_ask(subject: GateSubject) -> AskNotebook:
    """The ask a person answers, in the notebook's words: the action, the file
    by its workspace path, and each cell the plan runs by the name a person
    knows it by, in notebook order. Never a cell's id or a path on this disk."""
    steps = list(subject.plan.steps if subject.plan is not None else [])
    unplaced = len(steps)
    steps.sort(key=lambda step: step.index if step.index is not None else unplaced)
    return AskNotebook(
        lead=subject.lead,
        file_name=PurePosixPath(subject.path).name or subject.path,
        file_path=subject.path,
        tail=subject.tail,
        cells=[_ask_cell(step) for step in steps[:_PREVIEW_CELLS]],
        packages=list(subject.packages),
    )


def _preview(subject: GateSubject) -> dict[str, Any]:
    return {
        "kind": "notebook",
        "title": subject.title,
        "notebook": notebook_ask(subject).model_dump(mode="json"),
    }


class HarnessGatekeeper:
    """Decides a notebook gate subject through the session's permission engine."""

    def __init__(self, ctx: ToolContext, service: NotebookService) -> None:
        self._ctx = ctx
        self._service = service

    async def __call__(self, subject: GateSubject) -> GateVerdict:
        ctx = self._ctx
        mode = ctx.permission_mode
        file = self._service.file_path(ctx, subject.path)
        binding = binding_from_context(ctx)
        engine = binding.engine(source="notebook_gate")
        if engine is None:
            return GateVerdict(False, NO_SINK_REASON)
        if subject.effect is GateEffect.RUN_CELLS:
            descriptor, _steps = plan_descriptor(subject, file, ctx)
        else:
            descriptor = _descriptor(subject, file)
        if subject.effect is GateEffect.FILE_EDIT and self._service.edit_is_free(ctx, file):
            await engine.record_confined(descriptor, mode=mode)
            return GateVerdict(True)
        request = engine.build_request(descriptor, preview=_preview(subject))
        if standing_allow_scope(descriptor) is None:
            # "Always allow" on an action nothing could classify records no rule
            # (the answer is clamped to once), so it is not offered. "Always
            # reject" stays: a refusal is always recorded.
            options = [o for o in request.options if o.option_id != "allow_always"]
            request = request.model_copy(update={"options": options})
        res = await engine.resolve(descriptor, mode=mode, task_goal=ctx.task_goal, request=request)
        if res.allowed:
            return GateVerdict(True)
        return GateVerdict(False, denied_error(res.reason or "not approved"))


class _NotebookTool(Tool[BaseModel, BaseModel]):
    """The one body every notebook tool shares: resolve, gate, call, translate errors."""

    async def run(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        service = notebook_service(ctx)
        host = service.host_for(ctx)
        if host is None:
            raise ToolError("This chat has no workspace that holds notebooks.")
        name = type(self).spec.name
        typed = getattr(args, "path", None)
        if isinstance(typed, str):
            # Every notebook tool names its notebook by ``path``: read it as
            # the engine does, whatever spelling the agent saw it under.
            args = args.model_copy(update={"path": service.notebook_path(ctx, typed)})
        try:
            result = await call_tool(
                name,
                args,
                host=host,
                gatekeeper=HarnessGatekeeper(ctx, service),
                spill=StoreSpill(ctx.blobs),
            )
        except NotebookToolError as exc:
            raise ToolError(_tool_error_text(exc)) from exc
        result = service.with_guide(ctx, result)
        cursors = service.cursors_for(ctx)
        path = getattr(result, "path", None)
        if cursors is not None and isinstance(path, str):
            try:
                await follow(cursors, host, path)
            except Exception:
                logger.warning("notebook digest cursor for %s not saved", path, exc_info=True)
        return result


def _tool_error_text(exc: NotebookToolError) -> str:
    if exc.code == "permission_denied":
        return str(exc)
    where = f" (op {exc.op_index})" if exc.op_index is not None else ""
    return f"{exc.code}{where}: {exc}"


def _spec(name: str, effect: Effect) -> ToolSpec:
    definition = TOOLS[name]
    return ToolSpec(
        name=name,
        title=definition.title,
        description=definition.description,
        app=NOTEBOOK_APP,
        effect_hint=effect,
    )


class NotebookReadTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.read", Effect.READ)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.read"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.read"].output


class NotebookCreateTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.create", Effect.WRITE)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.create"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.create"].output


class NotebookEditTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.edit", Effect.WRITE)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.edit"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.edit"].output


class NotebookRunTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.run", CODE_EXEC_EFFECT)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.run"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.run"].output


class NotebookCellsTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.cells", Effect.WRITE)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.cells"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.cells"].output


class NotebookKernelTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.kernel", CODE_EXEC_EFFECT)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.kernel"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.kernel"].output


class NotebookOutputTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.output", Effect.READ)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.output"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.output"].output


class NotebookShowOutputTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.show_output", Effect.READ)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.show_output"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.show_output"].output


class NotebookInspectTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.inspect", CODE_EXEC_EFFECT)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.inspect"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.inspect"].output


class NotebookGraphTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.graph", Effect.READ)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.graph"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.graph"].output


class NotebookWidgetTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.widget", CODE_EXEC_EFFECT)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.widget"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.widget"].output


class NotebookEnvTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.env", Effect.EGRESS)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.env"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.env"].output


class NotebookSettingsTool(_NotebookTool):
    spec: ClassVar[ToolSpec] = _spec("notebook.settings", Effect.WRITE)
    Input: ClassVar[type[BaseModel]] = TOOLS["notebook.settings"].input
    Output: ClassVar[type[BaseModel]] = TOOLS["notebook.settings"].output


NOTEBOOK_TOOL_CLASSES: tuple[type[_NotebookTool], ...] = (
    NotebookReadTool,
    NotebookCreateTool,
    NotebookEditTool,
    NotebookRunTool,
    NotebookCellsTool,
    NotebookKernelTool,
    NotebookOutputTool,
    NotebookShowOutputTool,
    NotebookInspectTool,
    NotebookGraphTool,
    NotebookWidgetTool,
    NotebookEnvTool,
    NotebookSettingsTool,
)


#: The ``notebooks`` skill the prompt block tells the agent to load first.
NOTEBOOKS_SKILL = SkillDef(name=SKILL_NAME, description=SKILL_DESCRIPTION, body=SKILL_BODY)


def register_notebook_tools(registry: ToolRegistry) -> None:
    """Register the notebook tools and the ``notebooks`` skill, with the
    ``use_skill`` tool that loads it when no plugin skill brought it already."""
    for tool_cls in NOTEBOOK_TOOL_CLASSES:
        registry.register(tool_cls)
    registry.add_skill(NOTEBOOKS_SKILL)
    if registry.tool_for(UseSkillTool.spec.name) is None:
        registry.register(UseSkillTool)


__all__ = [
    "CODE_EXEC_EFFECT",
    "NOTEBOOKS_SKILL",
    "NOTEBOOK_APP",
    "NOTEBOOK_TOOL_CLASSES",
    "HarnessGatekeeper",
    "NotebookCellsTool",
    "NotebookCreateTool",
    "NotebookEditTool",
    "NotebookEnvTool",
    "NotebookGraphTool",
    "NotebookInspectTool",
    "NotebookKernelTool",
    "NotebookOutputTool",
    "NotebookReadTool",
    "NotebookRunTool",
    "NotebookService",
    "NotebookSettingsTool",
    "NotebookShowOutputTool",
    "NotebookWidgetTool",
    "notebook_ask",
    "notebook_service",
    "register_notebook_tools",
]
