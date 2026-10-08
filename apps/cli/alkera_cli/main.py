"""Alkera CLI entrypoint.

Library-first architecture: every meaningful operation here delegates
to a Python library importable both from the CLI (in-process) AND from
the daemon (`alkera serve`'s JSON-RPC methods). The daemon is a thin
JSON-RPC façade over the same library. See `CLAUDE.md` for the rule.

The daemon's chat surface lives in `alkera_cli.daemon.methods.harness`, over
the `alkera_cli.harness` package. A distribution adds commands, and what bare
`alkera` opens, through `alkera_cli.commands.extension_points`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import typer
from alkera_sdk import AlkeraClient
from click import Context as _ClickContext
from click import HelpFormatter as _HelpFormatter
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from alkera_cli.account import auth_file, orgs
from alkera_cli.host.config import get_settings, no_server_refusal
from alkera_cli.ui import activate as _activate_theme
from alkera_cli.ui import apply_typer_brand, console, render_banner

# Brand every --help screen with the detected palette. The OSC 11 probe
# inside activate() only runs when stdio is a tty, so the daemon path
# (`alkera serve` spawned over pipes) never sees a byte of it.
apply_typer_brand(_activate_theme())


class _BannerGroup(TyperGroup):
    """Root group whose help screen leads with the Alkera wordmark.

    `alkera --help` (and any usage/help render of the root) prints the
    banner first, then Typer's standard branded help. Bare `alkera` runs the
    registered default command instead, when a distribution names one."""

    def format_help(self, ctx: _ClickContext, formatter: _HelpFormatter) -> None:
        render_banner(console)
        super().format_help(ctx, formatter)


app: typer.Typer = typer.Typer(
    name="alkera",
    help="Alkera developer CLI.",
    cls=_BannerGroup,
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
    # We own the unhandled-exception path (`main()` below) so we can offer an
    # opt-in crash report + capture to Sentry, instead of Typer's Rich traceback.
    pretty_exceptions_enable=False,
)

# `alkera cloud-mirror run` — serve cloud chats from this machine's daemon (the
# publisher peer of `doc:chat:<id>`). The service itself is imported lazily
# inside the command so `alkera --help` never pays for the harness stack.
from alkera_cli.commands import box as _cloud_mirror_command  # noqa: E402

app.add_typer(_cloud_mirror_command.cloud_mirror_app, name="cloud-mirror", hidden=True)

# `alkera whoami` / `alkera org ...` / `alkera project unpin` — which sign-in a
# command acts as, and switching between orgs.
from alkera_cli.commands import account as _account_command  # noqa: E402

# `alkera files push ...` — the local half of the Files round trip.
from alkera_cli.commands import files as _files_command  # noqa: E402

app.add_typer(_files_command.files_app, name="files", hidden=True)

# `alkera env capture|recreate`: the workspace's Python environment spec.
from alkera_cli.commands import env as _env_command  # noqa: E402

app.add_typer(_env_command.env_app, name="env", hidden=True)


# `alkera run ...` — one-shot headless prompt (scripting / the experiments rig).
from alkera_cli.commands import run as _run_command  # noqa: E402

app.command("run", hidden=True)(_run_command.run_command)


def _version_callback(value: bool) -> None:
    if value:
        from alkera_cli import __version__

        console.print(f"alkera-cli [bold]{__version__}[/bold]")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Project root for the default command (defaults to cwd).",
    ),
    # Eager so `alkera --version` prints and exits before any subcommand/housekeeping.
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        help=(
            "Act as your sign-in for this organization (id or name) for this command. "
            f"Also read from {auth_file.ORG_ENV}."
        ),
    ),
) -> None:
    """Alkera developer CLI."""
    # Every invocation sets it, so a value never outlives the command it was
    # given to. The env var is read by the resolver itself, so it is named as
    # its own source.
    auth_file.set_invocation_org(org)
    # A process that starts with SIGCHLD ignored — an ancestor's disposition,
    # or a build plugin's C call after the interpreter is up, which Python's
    # own handler table never sees — reads every child it runs as exit 0 to
    # subprocess and 255 to asyncio. Every alkera process reaps its own
    # children from here on.
    from alkera_core.process import reclaim_children

    reclaim_children()
    for hook in CLI_STARTUP.items():
        hook(ctx.invoked_subcommand)

    if ctx.invoked_subcommand is not None:
        return
    # Bare `alkera` runs the distribution's default command; with none, it
    # shows the banner and help, as `alkera --help` does.
    default = default_command(CLI_DEFAULT.items())
    if default is None:
        typer.echo(ctx.get_help(), color=ctx.color)
        raise typer.Exit()
    default(project)


@app.command(hidden=True)
def health(
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help="Override the API base URL (default: $ALKERA_API_URL).",
    ),
) -> None:
    """Check the API health endpoints and print a friendly report."""
    settings = get_settings()
    base = (url or settings.alkera_api_url).rstrip("/")

    table = Table(title=f"Alkera health · {base}", show_lines=False)
    table.add_column("Endpoint", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Detail", style="dim")

    any_failed = False
    with AlkeraClient(base_url=base, timeout=settings.request_timeout_seconds) as api:
        probes: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("/health/live", api.health.live),
            ("/health/ready", api.health.ready),
        )
        for path, probe in probes:
            state, detail = _probe(probe)
            table.add_row(path, _render_state(state), detail)
            if state != "ok":
                any_failed = True

    console.print(table)
    raise typer.Exit(code=1 if any_failed else 0)


def _probe(call: Callable[[], Any]) -> tuple[str, str]:
    """Run a probe via the SDK; return (state, detail) ∈ {ok, degraded, down}."""
    try:
        result = call()
    except httpx.HTTPError as exc:
        return ("down", f"{type(exc).__name__}: {exc}")
    status_code = int(result.status_code)
    if status_code >= 500:
        body = result.content.decode("utf-8", errors="replace")[:80]
        return ("down", f"HTTP {status_code} · {body}")
    if status_code >= 400:
        return ("degraded", f"HTTP {status_code}")
    return ("ok", f"HTTP {status_code}")


def _render_state(state: str) -> str:
    colors = {"ok": "green", "degraded": "yellow", "down": "red"}
    return f"[{colors.get(state, 'white')}]{state}[/{colors.get(state, 'white')}]"


@app.command()
def login(
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-login even if already authenticated."
    ),
    org: str | None = typer.Option(
        None, "--org", help="Sign in to this organization (id or name)."
    ),
) -> None:
    """Authenticate via web OAuth flow."""
    settings = get_settings()
    api_url = settings.alkera_api_url.rstrip("/")
    _account_command.run_device_login(api_url, org=org, force=force)


app.command("whoami")(_account_command.whoami)
app.add_typer(_account_command.org_app, name="org")
app.add_typer(_account_command.project_app, name="project", hidden=True)


@app.command()
def logout(
    org: str | None = typer.Option(
        None, "--org", help="Sign out of this organization only (id or name)."
    ),
    all_profiles: bool = typer.Option(False, "--all", help="Sign out of every organization."),
) -> None:
    """Log out of your Alkera account (the current organization by default)."""
    auth = auth_file.load_profiles()
    if auth is None or not auth.profiles:
        console.print("[dim]No saved auth to remove.[/dim]")
        return
    try:
        done = orgs.sign_out(org=None if all_profiles else org, every=all_profiles)
    except auth_file.ProfileResolutionError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if done.revoke_unreachable:
        console.print("[dim]Couldn't reach the server to revoke the token; clearing locally.[/dim]")
    labels = ", ".join(p.org_label or p.email for p in done.removed)
    suffix = f" of {labels}" if labels else ""
    console.print(f"[green]✓[/green] Logged out{suffix}.")
    if done.current_changed and done.now_current is not None:
        console.print(f"Current organization: [bold]{done.now_current.org_label}[/bold]")


@app.command()
def version(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the version, the build this binary was compiled from and the daemon report "
        "as JSON.",
    ),
) -> None:
    """Print the version."""
    from alkera_cli import __version__

    if as_json:
        import json

        from alkera_cli.host.version_info import build_id, daemon_version

        typer.echo(
            json.dumps(
                {"version": __version__, "build": build_id(), "daemon_version": daemon_version()}
            )
        )
        return
    console.print(f"alkera-cli [bold]{__version__}[/bold]")


@app.command(hidden=True)
def serve(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="Wire transport. Only 'stdio' is supported today; '--transport ws' is reserved.",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="Daemon log verbosity (debug|info|warning|error).",
    ),
    cloud_mirror: bool = typer.Option(
        False,
        "--cloud-mirror",
        help="Also serve the cloud chats bound to this machine (the publisher peer); "
        "the workspace is --project (default: the current directory).",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Workspace root for --cloud-mirror (default: the current directory).",
    ),
    api_url: str | None = typer.Option(
        None, "--api-url", help="Backend base URL for --cloud-mirror (default: the login's)."
    ),
) -> None:
    """Run the Alkera daemon (JSON-RPC over stdio).

    Spawned by editor extensions (VS Code, JetBrains). Reads
    LSP-style Content-Length-framed JSON-RPC from stdin, writes
    responses + notifications to stdout. Logs go to stderr and to
    ~/.alkera/logs/daemon.log; never to stdout.
    """
    if transport != "stdio":
        console.print(f"[red]✗[/red] Unsupported transport: {transport!r} (use 'stdio').")
        raise typer.Exit(code=2)

    from alkera_cli.daemon.server import run_stdio
    from alkera_cli.host.onefile_cache import prune_stale_onefile_caches_in_background

    # Sweep extraction caches of versions we've upgraded away from (no-op
    # outside a compiled binary; runs on a daemon thread so startup isn't
    # delayed by deleting a multi-hundred-MB directory).
    prune_stale_onefile_caches_in_background()

    if cloud_mirror:
        from alkera_cli.commands.box import (
            SandboxProbes,
            build_mirror_runtime,
            mirror_settings_from_auth,
            run_mirror,
        )
        from alkera_cli.harness import HarnessUnavailableError

        # The registration facts come from the environment here
        # (ALKERA_MACHINE_PROVIDER_POD_ID / ALKERA_MACHINE_TYPE_CODE); a box
        # missing them is refused before the daemon starts.
        settings = mirror_settings_from_auth(
            api_url=api_url, project_dir=project or Path.cwd(), machine_name=None
        )
        probes = SandboxProbes()
        runtime = build_mirror_runtime(
            settings.project_dir,
            on_machine_credential=bool(settings.machine_credential),
            sandbox_probes=probes,
        )

        async def _both() -> None:
            # The mirror lives as long as the stdio daemon: when the editor (or
            # whoever holds stdin) goes away the service stops with it.
            stop = asyncio.Event()
            mirror = asyncio.create_task(
                run_mirror(settings, runtime, stop=stop, sandbox_probes=probes)
            )
            try:
                await run_stdio(log_level=log_level)
            finally:
                stop.set()
                await mirror

        try:
            asyncio.run(_both())
        except HarnessUnavailableError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from exc
        except KeyboardInterrupt:
            pass
        return

    try:
        asyncio.run(run_stdio(log_level=log_level))
    except KeyboardInterrupt:
        # Graceful — the signal handler already requested shutdown.
        pass


def _report_unexpected(exc: BaseException) -> None:
    """Print an error no command handled, then hand it to the installed crash
    hooks (a distribution may report it). A refused connection to the local
    default, with no API URL configured, is no crash: it says what to set."""
    refusal = no_server_refusal(exc, get_settings())
    if refusal is not None:
        console.print(f"[red]✗[/red] {refusal}")
        return
    console.print("\n[red]✗ alkera hit an unexpected error.[/red]")
    console.print(Text(f"  {type(exc).__name__}: {exc}", style="dim"))
    for hook in CLI_CRASH.items():
        hook(exc)


# Every command an installed distribution contributes, after the open ones so
# none can take an open command's name.
from alkera_cli.commands.extension_points import (  # noqa: E402
    CLI_COMMANDS,
    CLI_CRASH,
    CLI_DEFAULT,
    CLI_STARTUP,
    default_command,
    mount_commands,
)

mount_commands(app, CLI_COMMANDS.items())


def main() -> None:
    """Console-script entrypoint. Owns the unhandled-exception path so a crash
    yields a friendly message + opt-in report instead of a raw traceback."""
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        _report_unexpected(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
