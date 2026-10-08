"""`alkera run` — one-shot headless prompt against a project.

Thin Typer wrapper over `alkera_cli.chat.headless.run_headless` (the library an
evaluation rig imports directly). Progress streams to STDERR so `--json`
output on STDOUT stays machine-readable:

    alkera run -p scratch "add a README summarizing this repo" --json > result.json

Exit codes: 0 = the run completed; 1 = it errored / timed out / was canceled /
stopped at the model's output cap (`max_tokens`, so the text is not an answer;
details in the output); 2 = a setup problem the caller must fix (not signed
in, gateway down, unknown model/harness/mode).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast, get_args

import typer
from alkera_core.money import usd_display
from alkera_core.schemas.chat import (
    Event,
    MessageCreated,
    PartCreated,
    PermissionRequest,
    TextPart,
    ToolCall,
    TurnFinished,
)
from rich.console import Console

from alkera_cli.chat.headless import HeadlessError, HeadlessResult, run_headless
from alkera_cli.harness.permission_mode import ALL_MODES
from alkera_cli.harness.registry import HARNESS_CHOICES
from alkera_cli.harness.runtime import AnalysisPipeline

_err_console = Console(stderr=True)

_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    "-p",
    exists=True,
    file_okay=False,
    dir_okay=True,
    help="Project root (defaults to cwd).",
)


def _progress_printer(quiet: bool) -> Any:
    """An on_event callback that narrates the run on stderr. Tracks message
    roles so the echoed user prompt isn't reprinted as agent output."""
    roles: dict[str, str] = {}

    def _print(event: Event) -> None:
        if isinstance(event, MessageCreated):
            roles[event.message_id] = event.role
            return
        if quiet:
            return
        if isinstance(event, ToolCall):
            preview = json.dumps(event.input, default=str)
            if len(preview) > 120:
                preview = preview[:119] + "…"
            _err_console.print(f"[dim]→ {event.tool_name or event.tool_kind} {preview}[/dim]")
        elif isinstance(event, PermissionRequest):
            _err_console.print(f"[yellow]? permission: {event.canonical_kind}[/yellow]")
        elif isinstance(event, PartCreated):
            part = event.part
            if (
                isinstance(part, TextPart)
                and part.text
                and not part.synthetic
                and roles.get(part.message_id) != "user"
            ):
                _err_console.print(part.text)
        elif isinstance(event, TurnFinished):
            _err_console.print(f"[dim]✓ turn finished ({event.stop_reason})[/dim]")

    return _print


def _summary(result: HeadlessResult) -> None:
    """Human summary on stderr (the final text already streamed)."""
    tokens = result.tokens or {}
    _err_console.print(
        f"[dim]session {result.session_id} · {result.stop_reason} · "
        f"{len(result.tool_calls)} tool calls · "
        f"{len(result.permission_prompts)} permission prompts · "
        f"tokens {tokens.get('input', 0)}in/{tokens.get('output', 0)}out · "
        f"{usd_display(result.cost_usd)} · {result.duration_seconds:.0f}s[/dim]"
    )
    if result.error_detail:
        _err_console.print(f"[red]{result.error_detail}[/red]")
    if result.verification == "verified":
        _err_console.print("[dim]Delivered: the re-verified answer above.[/dim]")
    elif result.verification:
        _err_console.print(
            f"[yellow]Delivered: the first answer above; {result.verification}.[/yellow]"
        )


def run_command(
    prompt: str | None = typer.Argument(None, help="The prompt text (or use --prompt-file)."),
    project: Path | None = _PROJECT_OPTION,
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", exists=True, dir_okay=False, help="Read the prompt from a file."
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Gateway model id (defaults to the saved chat default)."
    ),
    effort: str | None = typer.Option(
        None, "--effort", help="Reasoning-effort variant for --model (e.g. low/high/xhigh)."
    ),
    harness: str = typer.Option(
        "alkera", "--harness", help="Agent harness: " + " | ".join(HARNESS_CHOICES) + "."
    ),
    mode: str = typer.Option(
        "auto", "--mode", help="Permission mode: " + " | ".join(sorted(ALL_MODES)) + "."
    ),
    analysis: str | None = typer.Option(
        None,
        "--analysis",
        help=(
            "Analysis mode: off | analyst (analyst adds a verification turn after a data "
            "answer). Unset keeps the chat's persisted mode."
        ),
    ),
    on_permission: str = typer.Option(
        "allow",
        "--on-permission",
        help="What to do when a permission would prompt a human: allow | reject.",
    ),
    resume: str | None = typer.Option(
        None, "--resume", help="Session id to continue instead of creating a new chat."
    ),
    timeout: float = typer.Option(1800.0, "--timeout", help="Whole-run budget in seconds."),
    verification_timeout: float | None = typer.Option(
        None,
        "--verification-timeout",
        help="Seconds for an analyst-mode verification turn (default 900, capped by --timeout).",
    ),
    wait_seed: float | None = typer.Option(
        None,
        "--wait-seed",
        help="Wait up to N seconds for the initial lineage/KB seed before prompting.",
    ),
    events_file: Path | None = typer.Option(
        None, "--events-file", help="Append every event as JSONL to this file."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the structured result as JSON on stdout."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="No progress narration (result output only)."
    ),
) -> None:
    """Run one prompt against a project headlessly and exit when it settles."""
    if (prompt is None) == (prompt_file is None):
        _err_console.print("[red]✗ Give exactly one of PROMPT or --prompt-file.[/red]")
        raise typer.Exit(code=2)
    if prompt_file is not None:
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt:
            _err_console.print(f"[red]✗ {prompt_file} is empty.[/red]")
            raise typer.Exit(code=2)
    if mode not in ALL_MODES:
        _err_console.print(f"[red]✗ Unknown mode '{mode}'.[/red] Choose: {', '.join(ALL_MODES)}")
        raise typer.Exit(code=2)
    if analysis is not None and analysis not in get_args(AnalysisPipeline):
        _err_console.print("[red]✗ --analysis must be 'off' or 'analyst'.[/red]")
        raise typer.Exit(code=2)
    if on_permission not in ("allow", "reject"):
        _err_console.print("[red]✗ --on-permission must be 'allow' or 'reject'.[/red]")
        raise typer.Exit(code=2)

    assert prompt is not None
    try:
        result = asyncio.run(
            run_headless(
                (project or Path.cwd()).resolve(),
                [prompt],
                model=model,
                effort=effort,
                harness=harness,
                permission_mode=mode,
                analysis_pipeline=None if analysis is None else cast(AnalysisPipeline, analysis),
                on_permission=cast(Any, on_permission),
                resume_session_id=resume,
                timeout_seconds=timeout,
                verification_timeout_seconds=verification_timeout,
                wait_for_seed_seconds=wait_seed,
                on_event=_progress_printer(quiet),
                events_path=events_file,
            )
        )
    except HeadlessError as exc:
        _err_console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=2) from None
    except KeyboardInterrupt:
        _err_console.print("[yellow]Interrupted.[/yellow]")
        raise typer.Exit(code=130) from None

    _summary(result)
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
    raise typer.Exit(code=0 if result.stop_reason == "completed" else 1)


__all__ = ["run_command"]
