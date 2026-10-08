"""`alkera env`: capture a workspace's Python environment into
``.alkera-environment.json`` and recreate it elsewhere."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from alkera_cli.environment import (
    InvalidSpecError,
    LocalRunner,
    ProbeError,
    RecreateReport,
    SpecSecretError,
    capture,
    host_python,
    load_spec,
    local_python,
    recreate,
    summarize_spec,
    write_spec,
)
from alkera_cli.ui import console

env_app: typer.Typer = typer.Typer(help="Capture and recreate a workspace's Python environment.")

_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    "-p",
    exists=True,
    file_okay=False,
    dir_okay=True,
    help="Workspace root (defaults to cwd).",
)
_JSON_OPTION = typer.Option(False, "--json", help="Print the result as JSON.")


def _windows() -> bool:
    return os.name == "nt"


@env_app.command("capture")
def capture_env(
    project: Path | None = _PROJECT_OPTION,
    env: Path | None = typer.Option(
        None, "--env", help="The environment to capture (defaults to the workspace's .venv)."
    ),
    as_json: bool = _JSON_OPTION,
) -> None:
    """Write the workspace's environment to .alkera-environment.json."""
    root = (project or Path.cwd()).resolve()
    python = local_python(root, env, windows=_windows())
    try:
        spec = asyncio.run(capture(LocalRunner(), root=str(root), python=python))
        path = write_spec(root, spec)
    except (ProbeError, SpecSecretError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(spec.model_dump_json(indent=2))
        return
    console.print(f"Wrote {path}")
    console.print(summarize_spec(spec), markup=False)


def _print_report(report: RecreateReport) -> None:
    if report.already_satisfied:
        console.print(f"{report.target_env} already matches the spec.", markup=False)
        return
    verb = "Would run" if report.dry_run else "Ran"
    for step in report.steps:
        status = "" if not step.ran else (" ok" if step.exit_code == 0 else " failed")
        console.print(f"{verb}{status}: {step.command}", markup=False)
    for note in report.notes:
        console.print(f"Note: {note}", markup=False)
    for gap in report.not_recreated:
        console.print(f"Not recreated ({gap.kind}): {gap.name} {gap.detail}".rstrip(), markup=False)


@env_app.command("recreate")
def recreate_env(
    target: Path = typer.Option(..., "--target", help="Where the environment goes."),
    project: Path | None = _PROJECT_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; change nothing."),
    as_json: bool = _JSON_OPTION,
) -> None:
    """Build or update an environment to match the workspace's spec."""
    root = (project or Path.cwd()).resolve()
    try:
        spec = load_spec(root)
    except InvalidSpecError as exc:
        console.print(f"✗ {exc} ({root})", style="red", markup=False)
        raise typer.Exit(code=2) from exc
    try:
        report = asyncio.run(
            recreate(
                spec,
                LocalRunner(),
                target_env=str(target.resolve()),
                target_root=str(root),
                dry_run=dry_run,
                fallback_python=host_python(),
                windows=_windows(),
            )
        )
    except ProbeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        _print_report(report)
    failed = [g for g in report.not_recreated if g.kind in ("step_failed", "not_installed")]
    if failed and not dry_run:
        raise typer.Exit(code=1)
