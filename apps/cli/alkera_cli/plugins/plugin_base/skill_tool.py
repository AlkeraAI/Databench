"""``use_skill`` — load a plugin-provided skill on demand.

A ``SkillProvider`` ships markdown guidance (a ``SkillDef``: name + description +
body). This tool is how the agent CONSUMES them, progressive-disclosure style:
call it with no ``name`` to list the available skills (name + one-line
description), then with a ``name`` to load that skill's full body into context.
Keeping the body out of the always-on prompt is the whole point — it loads only
when the agent decides it's relevant. Reads ``ctx.registry.skills``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec


class UseSkillInput(BaseModel):
    name: str | None = None
    """Omit to LIST available skills; pass a skill name to LOAD its body."""


class SkillCard(BaseModel):
    name: str
    description: str = ""


class UseSkillResult(BaseModel):
    skills: list[SkillCard] = Field(default_factory=list)
    """Populated on a LIST call (no name) — what's available to load."""
    name: str | None = None
    body: str | None = None
    """Populated on a LOAD call — the loaded skill's full instructions."""


class UseSkillTool(Tool[UseSkillInput, UseSkillResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="use_skill",
        title="Load a skill",
        description=(
            "List the project's available skills, or load one by name to bring its "
            "full instructions into context. Call with no name to see what exists, "
            "then with a name to load it."
        ),
        app="skill",
        hot=False,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = UseSkillInput
    Output: ClassVar[type[BaseModel]] = UseSkillResult

    async def run(self, args: UseSkillInput, ctx: ToolContext) -> UseSkillResult:
        skills: list[Any] = ctx.registry.skills
        if args.name is None:
            return UseSkillResult(
                skills=[SkillCard(name=s.name, description=s.description) for s in skills]
            )
        for s in skills:
            if s.name == args.name:
                return UseSkillResult(name=s.name, body=s.body)
        available = ", ".join(s.name for s in skills) or "(none)"
        raise ToolError(f"unknown skill {args.name!r}; available: {available}")


def register_skill_tools(registry: ToolRegistry) -> None:
    """Register ``use_skill`` IFF the project has any skills — no skills, no tool
    (don't surface a tool that can only ever return an empty list)."""
    if registry.skills:
        registry.register(UseSkillTool)


__all__ = [
    "SkillCard",
    "UseSkillInput",
    "UseSkillResult",
    "UseSkillTool",
    "register_skill_tools",
]
