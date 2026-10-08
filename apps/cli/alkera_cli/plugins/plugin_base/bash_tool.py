"""The parent-hosted ``bash`` tool, served over the loopback MCP to both adapters
so native bash can be disabled on each.

Foreground: gate the command (our classifier + the sensitive-path escalation), run
it via :mod:`bash_exec`, return the output. Background (``background=true``): gate
synchronously (the human is attached during the live turn), then detach the run as
a supervised job and return a job-id stub; the native result is delivered via the
completion notification. Background bash is available only in a root chat (a
subagent's flag is ignored — it runs foreground, like ``sql.query``).

cwd: an optional ``workdir`` resolved LEXICALLY under the workspace root (escapes
refused); the command itself is the security boundary (the gate), the workdir is
confined as defense in depth.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar

from alkera_core.project.directory import CHATS_SUBDIR
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness.sandbox import RUNTIME_STATE_SUBDIR, relative_to_container
from alkera_cli.plugins.plugin_base.agent_paths import agent_path
from alkera_cli.plugins.plugin_base.agent_tree import tool_output
from alkera_cli.plugins.plugin_base.bash_exec import (
    ExecResult,
    build_child_env,
    confine_under_root,
    run_command,
    run_with_abort,
    select_shell,
    session_sandbox,
)
from alkera_cli.plugins.plugin_base.bash_ids import BASH_TOOL_NAME, DEFAULT_TIMEOUT_MS
from alkera_cli.plugins.plugin_base.environment_tool import ENVIRONMENT_TOOLS, chat_launch
from alkera_cli.plugins.plugin_base.permissions import (
    binding_from_context,
    denied_error,
    gate_shell_action,
)
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

_BASH_DESCRIPTION = (
    "Execute a bash command and return its output, with an optional timeout.\n\n"
    "Each command runs in a FRESH, stateless shell — no state (environment "
    "variables, shell functions, the working directory) carries over between "
    "commands. Because nothing persists, don't rely on a prior command's `cd` or "
    "exports; chain dependent steps in a SINGLE call with `&&` (e.g. "
    "`cd … && …` becomes `pytest tests` with workdir set). To run in a different "
    "directory, set the `workdir` parameter instead of using `cd`.\n\n"
    "Notes:\n"
    "- If the output is very large it is truncated and the full text written to a "
    "file (the path is shown); Read with offset/limit or Grep that file rather than "
    "re-running.\n"
    "- Prefer the dedicated tools over shell where they exist: Glob (not find/ls), "
    "Grep (not grep/rg), Read (not cat/head/tail), Edit (not sed/awk).\n"
    "- The shell is non-interactive: a command that waits for input is stopped once "
    "it has printed nothing and used no CPU for a long while — pass non-interactive "
    "flags or pipe input in.\n"
    "- There is no time limit by default: a long build, test run or job runs until it "
    "finishes.\n"
    "- For a slow, independent command (a build, a test run, a dev server) set "
    "`background: true` to return immediately and be notified when it finishes."
)


class BashInput(BaseModel):
    command: str = Field(
        description=(
            "The command to execute. Runs in a fresh, stateless shell — chain "
            "dependent steps in one call with `&&`; set `workdir` instead of `cd`."
        )
    )
    timeout: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional timeout in milliseconds. The command is killed if it exceeds "
            "this. Leave it unset to let the command run as long as it needs; it is "
            "then stopped only if it sits idle, printing nothing and using no CPU."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "The working directory to run the command in (relative to the workspace "
            "root, or absolute under it). Defaults to the workspace root. Use this "
            "instead of a `cd` in the command — `cd` does not persist."
        ),
    )
    description: str = Field(
        description="Clear, concise description of what this command does in 5-10 words."
    )
    background: bool = Field(
        default=False,
        description=(
            "Run this command in the BACKGROUND: return a job id immediately instead "
            "of blocking, and notify you with the command's full output when it "
            "finishes. Use for a slow, independent command (a build, a full test run, "
            "a long-running script, a dev server). Permissions are still checked up "
            "front. Don't poll; you'll be notified. Leave false when you need the "
            "output for your next step."
        ),
    )


class BashResult(BaseModel):
    output: str = ""
    """The command's output (tail + truncation banner + ``<shell_metadata>``)."""
    exit_code: int | None = None
    """The exit code, or ``None`` if the command timed out (was killed)."""
    truncated: bool = False
    output_path: str | None = None
    """Path to the FULL spilled output when truncated, else ``None``."""
    job_id: str = ""
    """Set only on a BACKGROUNDED command's immediate return — the job id to inspect
    via ``background_status`` / stop via ``background_cancel``. Empty otherwise."""
    note: str = ""
    """Set only on a backgrounded command's immediate return — the "started" message."""


#: How much of a running background command's latest output its job keeps for
#: ``background_status`` (characters, oldest dropped first).
PREVIEW_TAIL_BYTES = 8 * 1024


class _OutputTail:
    """The last ``limit`` characters of a stream, for a live preview."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._text = ""

    def add(self, text: str) -> str:
        self._text = (self._text + text)[-self._limit :]
        return self._text


def _confine_cwd(workdir: str | None, root: Path, fence: Any = None) -> Path:
    """The command's working directory, confined under the workspace root
    (default: root). A bounded session's model spells it the way it sees the
    root (the sandbox's home), so the spelling is mapped to the host path the
    fence judges before it is confined."""
    if not workdir:
        return root
    canonical = getattr(fence, "canonical", None)
    if callable(canonical):
        workdir = str(canonical(workdir))
    return confine_under_root(workdir, root, label="workdir")


class BashTool(Tool[BashInput, BashResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name=BASH_TOOL_NAME,
        title="Run a bash command",
        description=_BASH_DESCRIPTION,
        app="bash",
        hot=True,
        # READ-hinted like sql.query: the MCP layer doesn't pre-gate; the command's
        # real effect is classified + gated IN the tool body (gate_shell_action).
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BashInput
    Output: ClassVar[type[BaseModel]] = BashResult

    async def run(self, args: BashInput, ctx: ToolContext) -> BashResult:
        # A bounded (cloud) session runs its shell where the agent runs — the
        # chat's own working directory, which is also all its shell may read —
        # with the box's secrets taken out of the environment. The fence judges
        # the command against that same cwd and that same environment, so what
        # it vouches for is what runs.
        fenced = ctx.fence is not None
        root = (
            Path(ctx.fence.working_dir)
            if fenced
            else Path(ctx.alkera_dir).parent
            if ctx.alkera_dir is not None
            else Path.cwd()
        )
        cwd = _confine_cwd(args.workdir, root, ctx.fence)
        if not await asyncio.to_thread(cwd.is_dir):
            raise ToolError(f"workdir does not exist or is not a directory: {agent_path(ctx, cwd)}")
        env = build_child_env(dict(os.environ), cwd=str(cwd), home=str(root) if fenced else None)

        # Gate the command — synchronously, while a human is attached (even for a
        # backgrounded command: "background" means it returns before it COMPLETES,
        # not before it's APPROVED).
        gate = await gate_shell_action(
            args.command,
            mode=ctx.permission_mode,
            binding=binding_from_context(ctx),
            cwd=cwd,
            env=env,
        )
        if not gate.allowed:
            raise ToolError(denied_error(gate.reason or "command not approved"))

        timeout_ms = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT_MS
        shell = select_shell()
        # Where an output too long to hand back goes: a file under the session's
        # tool-output directory, made only when one spills, handed to the chat.
        output = tool_output(ctx, fallback="alkera-bash")
        # The command runs inside the chat's own sandbox — the same uid, cgroup,
        # view of the disk and default Python environment as its agent server.
        # A local session gets none.
        state_dir = (
            Path(ctx.alkera_dir) / CHATS_SUBDIR / ctx.session_id / RUNTIME_STATE_SUBDIR
            if fenced and ctx.alkera_dir is not None and ctx.session_id
            else None
        )
        sandbox = session_sandbox(
            ctx.session_id,
            folder=root if fenced else None,
            fenced=fenced,
            state_dir=state_dir,
            owner_session_id=ctx.owner_session_id,
        )
        # Where the command runs and what it is told, spelled the way the agent
        # sees its root: a sandbox that mounts the working directory at its own
        # home is entered under that home, and a spilled output is named there.
        agent_cwd: str | None = None
        if sandbox.agent_home is not None:
            agent_cwd = relative_to_container(cwd, folder=root, home=sandbox.agent_home)
        spell = (lambda path: agent_path(ctx, path)) if fenced else None

        do_background = args.background and ctx.background is not None
        if do_background:
            registry = ctx.background
            if not registry.can_accept():
                raise ToolError(
                    "too many background jobs already running; cancel one with "
                    "background_cancel or run this command in the foreground"
                )

            # The job's id exists only once it is submitted, and the job's
            # coroutine first runs after that, so the output callback reads it
            # from this cell.
            submitted: list[str] = []
            tail = _OutputTail(PREVIEW_TAIL_BYTES)

            def _on_output(text: str) -> None:
                if submitted:
                    registry.set_preview(submitted[0], tail.add(text))

            async def _job() -> BashResult:
                ex = await run_command(
                    args.command,
                    cwd=str(cwd),
                    env=env,
                    shell=shell,
                    timeout_ms=timeout_ms,
                    make_spill=lambda: output.spill("bash"),
                    on_output=_on_output,
                    sandbox=sandbox,
                    agent_cwd=agent_cwd,
                    spell=spell,
                )
                return _to_result(ex)

            job = registry.submit(
                _job,
                kind="bash",
                title=args.description or "background command",
                input={"command": args.command, "cwd": agent_cwd or str(cwd)},
            )
            submitted.append(str(job.job_id))
            return BashResult(
                job_id=str(job.job_id),
                note=(
                    "Running in the background; you'll be notified with the output "
                    "when it finishes. Do not poll."
                ),
            )

        async def _run() -> ExecResult:
            return await run_command(
                args.command,
                cwd=str(cwd),
                env=env,
                shell=shell,
                timeout_ms=timeout_ms,
                make_spill=lambda: output.spill("bash"),
                sandbox=sandbox,
                agent_cwd=agent_cwd,
                spell=spell,
            )

        ex = await run_with_abort(_run, ctx.abort)
        return _to_result(ex)


def _to_result(ex: ExecResult) -> BashResult:
    return BashResult(
        output=ex.output,
        exit_code=ex.exit_code,
        truncated=ex.truncated,
        output_path=ex.output_path,
    )


def register_bash_tools(registry: ToolRegistry) -> None:
    """Register the parent-hosted ``bash`` tool (POSIX only — the executor relies on
    process groups / signals). A subagent SEES it (read-only investigation) but the
    gate refuses its writes; background is root-only (ignored for a subagent).

    The environment tools ride with it: they run their commands through the same
    executor and the same chat sandbox, and the registry carries the launch the
    runtime's environment service restores a woken chat's environment through."""
    registry.register(BashTool)
    for tool in ENVIRONMENT_TOOLS:
        registry.register(tool)
    registry.chat_launch_factory = chat_launch


__all__ = [
    "BashInput",
    "BashResult",
    "BashTool",
    "register_bash_tools",
]
