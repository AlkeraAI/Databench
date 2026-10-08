"""Model mode: a real (or scripted) model working notebooks through the tools.

:func:`run_episode` gives a model the notebook tools' schemas, the prompt
block, ``use_skill`` with the ``notebooks`` skill, and two plain file tools
(``read_file``, ``write_file``) so an attempt to edit a notebook as a file can
be seen and refused. Every notebook call goes through the simulator's driver,
so every invariant is checked after every step exactly as in the random
modes. The episode reports hard checks (outcomes and invariants, which fail
an evaluation) and rubric counts (reported, never failing).

The model sits behind :class:`ModelClient`. In Alkera's suite it is the
gateway (an Anthropic-compatible messages endpoint, :class:`MessagesApiClient`);
outside it any provider SDK can implement the protocol. :class:`ScriptedModel`
plays a fixed script for the loop's own tests.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from alkera_notebook.sim.driver import SimDriver
from alkera_notebook.sim.invariants import InvariantViolationError
from alkera_notebook.tools import PROMPT_BLOCK, SKILL_BODY, SKILL_NAME, TOOLS, GuideLedger
from alkera_notebook.tools.models import NotebookEditInput, NotebookRunOutput, NotebookToolResult
from alkera_notebook.tools.port import NotebookToolError

#: Tool names as a model API accepts them (no dots).
_WIRE = {name: name.replace(".", "_") for name in TOOLS}
_FROM_WIRE = {wire: name for name, wire in _WIRE.items()}


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ModelClient(Protocol):
    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


def tool_schemas() -> list[dict[str, Any]]:
    """The tools a model is offered, in the messages API's shape."""
    out = [
        {
            "name": _WIRE[name],
            "description": f"{d.description} (Alkera tool {name}.)",
            "input_schema": d.input.model_json_schema(),
        }
        for name, d in TOOLS.items()
    ]
    out.append(
        {
            "name": "use_skill",
            "description": "Load a skill's guide by name.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
    )
    for name, props in (
        ("read_file", {"path": {"type": "string"}}),
        ("write_file", {"path": {"type": "string"}, "content": {"type": "string"}}),
    ):
        out.append(
            {
                "name": name,
                "description": f"{name.replace('_', ' ').capitalize()} in the workspace.",
                "input_schema": {"type": "object", "properties": props, "required": list(props)},
            }
        )
    return out


@dataclass
class Rubric:
    tool_calls: int = 0
    runs: int = 0
    reruns_of_fresh_cells: int = 0
    whole_cell_replacements: int = 0
    duplicate_definitions_introduced: int = 0
    mo_calls_written: int = 0
    file_tool_notebook_edits: int = 0
    skill_loaded: bool = False
    guide_delivered: bool = False
    """The first notebook tool result carried the skill's essentials."""


@dataclass
class EpisodeReport:
    scenario: str
    hard: dict[str, bool] = field(default_factory=dict)
    rubric: Rubric = field(default_factory=Rubric)
    violations: list[str] = field(default_factory=list)
    final_text: str = ""
    turns: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations and all(self.hard.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "passed": self.passed,
            "hard": self.hard,
            "rubric": self.rubric.__dict__,
            "violations": self.violations,
            "turns": self.turns,
            "final_text": self.final_text[:2_000],
        }


HardCheck = Callable[[SimDriver, EpisodeReport], Awaitable[bool]]


@dataclass
class Scenario:
    name: str
    task: str
    setup: Callable[[SimDriver], Awaitable[None]]
    hard_checks: dict[str, HardCheck]
    max_turns: int = 30
    after_call: Callable[[SimDriver, str, Any], Awaitable[None]] | None = None
    """Runs after each agent tool call: what a person does meanwhile."""


async def _fresh_cells(driver: SimDriver, path: str) -> set[str]:
    port = await driver.agent_host.open(path)
    view = await port.read(None, include_source=False, include_outputs=False)
    return {c.id for c in view.cells if c.status == "fresh"}


async def _graph_errors(driver: SimDriver, path: str) -> int:
    port = await driver.agent_host.open(path)
    view = await port.read(None, include_source=False, include_outputs=False)
    return sum(
        any(e.split(":", 1)[0] == "multiple_definitions" for e in c.graph_errors)
        for c in view.cells
    )


async def _call(
    driver: SimDriver, report: EpisodeReport, call: ToolCall, guides: GuideLedger
) -> str:
    """One tool call, its result as the text the model reads, and the rubric."""
    rubric = report.rubric
    rubric.tool_calls += 1
    if call.name == "use_skill":
        if call.arguments.get("name") == SKILL_NAME:
            rubric.skill_loaded = True
            return SKILL_BODY
        return f"No skill named {call.arguments.get('name')!r}; available: {SKILL_NAME}."
    if call.name in ("read_file", "write_file"):
        path = str(call.arguments.get("path", ""))
        if path.endswith(".alknb.py"):
            if call.name == "write_file":
                rubric.file_tool_notebook_edits += 1
                return "Refused: edit notebooks with notebook.edit, never as files."
            return "Read notebooks with notebook.read."
        return "No such file."
    tool = _FROM_WIRE.get(call.name)
    if tool is None:
        return f"Unknown tool {call.name}."
    args = dict(call.arguments)
    path = str(args.get("path", ""))
    before_fresh: set[str] = set()
    before_dups = 0
    if tool == "notebook.run" and path:
        try:
            before_fresh = await _fresh_cells(driver, driver.agent_host.resolve(path))
        except NotebookToolError:
            pass
    if tool == "notebook.edit" and path:
        try:
            before_dups = await _graph_errors(driver, driver.agent_host.resolve(path))
        except NotebookToolError:
            pass
        ops = args.get("ops") or []
        rubric.whole_cell_replacements += sum(
            1 for op in ops if isinstance(op, dict) and op.get("op") == "replace"
        )
        written = json.dumps(ops)
        rubric.mo_calls_written += written.count("mo.")
    result = await driver.agent(tool, args)
    if isinstance(result, Exception):
        return f"Error: {result}"
    result = guides.attach(result, report.scenario)
    if isinstance(result, NotebookToolResult) and result.notebook_guide is not None:
        rubric.guide_delivered = True
    if isinstance(result, NotebookRunOutput):
        rubric.runs += 1
        rubric.reruns_of_fresh_cells += sum(
            1 for step in result.plan if step.reason == "target" and step.cell_id in before_fresh
        )
    if tool == "notebook.edit" and isinstance(result, BaseModel):
        parsed = TOOLS[tool].input.model_validate(args)
        assert isinstance(parsed, NotebookEditInput)
        after = await _graph_errors(driver, driver.agent_host.resolve(parsed.path))
        rubric.duplicate_definitions_introduced += max(0, after - before_dups)
    return result.model_dump_json()


async def run_episode(client: ModelClient, scenario: Scenario, driver: SimDriver) -> EpisodeReport:
    """Run one scenario to the model's final answer, checking invariants after
    every step, then the scenario's hard checks."""
    report = EpisodeReport(scenario.name)
    await scenario.setup(driver)
    messages: list[dict[str, Any]] = [{"role": "user", "content": scenario.task}]
    tools = tool_schemas()
    # One episode is one conversation: its first notebook result carries the guide.
    guides = GuideLedger()
    try:
        for _ in range(scenario.max_turns):
            report.turns += 1
            turn = await client.complete(system=PROMPT_BLOCK, messages=messages, tools=tools)
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [c.model_dump() for c in turn.tool_calls],
                }
            )
            if not turn.tool_calls:
                report.final_text = turn.text
                break
            for call in turn.tool_calls:
                content = await _call(driver, report, call, guides)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if scenario.after_call is not None:
                    await scenario.after_call(driver, _FROM_WIRE.get(call.name, call.name), content)
            await driver.check()
        await driver.check(settle=True)
    except InvariantViolationError as exc:
        report.violations = [f"[{v.invariant}] {v.path}: {v.detail}" for v in exc.violations]
    for name, check in scenario.hard_checks.items():
        try:
            report.hard[name] = bool(await check(driver, report))
        except Exception:
            report.hard[name] = False
    return report


class ScriptedModel:
    """A model that plays a script: each entry sees the conversation so far and
    returns the next turn."""

    def __init__(self, script: Sequence[Callable[[list[dict[str, Any]]], ModelTurn]]) -> None:
        self._script = list(script)
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        del system, tools
        self.seen.append(list(messages))
        if not self._script:
            return ModelTurn(text="Done.")
        return self._script.pop(0)(messages)


class MessagesApiClient:
    """An Anthropic-compatible messages endpoint (the gateway, or the provider)."""

    def __init__(self, base_url: str, api_key: str, model: str, *, max_tokens: int = 4096) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens

    @staticmethod
    def _wire_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for call in m.get("tool_calls", []):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["arguments"],
                        }
                    )
                out.append(
                    {"role": "assistant", "content": blocks or [{"type": "text", "text": "."}]}
                )
            else:
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        import httpx

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._wire_messages(messages),
            "tools": tools,
        }
        headers = {
            "x-api-key": self.api_key,
            "authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=300) as http:
            response = await http.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in data.get("content", [])
            if b.get("type") == "tool_use"
        ]
        return ModelTurn(text=text, tool_calls=calls)


__all__ = [
    "EpisodeReport",
    "HardCheck",
    "MessagesApiClient",
    "ModelClient",
    "ModelTurn",
    "Rubric",
    "Scenario",
    "ScriptedModel",
    "ToolCall",
    "run_episode",
    "tool_schemas",
]
