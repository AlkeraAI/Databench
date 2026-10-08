"""`alkera whoami`, `alkera org list|switch` and `alkera project unpin`.

Thin façades over :mod:`alkera_cli.account`: which sign-in a command acts as,
the orgs the person belongs to, switching the current profile, and releasing a
project's org pin for a deliberate move.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.table import Table

from alkera_cli.account import auth_file, device_flow
from alkera_cli.account import login as login_flow
from alkera_cli.account import memberships as memberships_api
from alkera_cli.account import orgs as orgs_api
from alkera_cli.account.binding import unpin_project
from alkera_cli.host.config import get_settings, no_server_refusal
from alkera_cli.host.paths import existing_project_directory
from alkera_cli.ui import console

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

org_app = typer.Typer(help="See and switch the organizations you are signed in to.")
project_app = typer.Typer(help="The project's link to an organization.")

_SOURCE_LABELS: dict[auth_file.ProfileSource, str] = {
    "token_env": f"env ({auth_file.TOKEN_ENV})",
    "flag": "flag (--org)",
    "env": f"env ({auth_file.ORG_ENV})",
    "pin": "pin (.alkera/cloud.json)",
    "current": "current",
}


def _project_or_none(project: Path | None) -> ProjectDirectory | None:
    return existing_project_directory((project or Path.cwd()).resolve())


def whoami(
    project: Path | None = typer.Option(
        None, "--project", "-p", help="Project root (defaults to cwd)."
    ),
) -> None:
    """Show which sign-in commands here act as."""
    status = login_flow.auth_status(project=_project_or_none(project))
    if status.reason == "refused":
        console.print(f"[red]✗[/red] {status.detail}")
        raise typer.Exit(code=1)
    if status.reason == "missing" or status.stored is None:
        console.print("Not logged in. Run [bold]alkera login[/bold].")
        raise typer.Exit(code=1)
    profile = status.stored
    console.print(f"Email: {status.email or profile.email or 'unknown'}")
    console.print(f"Organization: {profile.org_label or 'unknown'}")
    console.print(f"API: {profile.api_url}")
    console.print(f"Expires: {profile.expires_at:%Y-%m-%d %H:%M %Z}".rstrip())
    source = status.source or "current"
    console.print(f"Source: {_SOURCE_LABELS[source]}")
    if status.reason == "invalid":
        console.print("[yellow]This sign-in was refused. Run [bold]alkera login[/bold].[/yellow]")
        raise typer.Exit(code=1)
    if status.reason == "email_verification_required":
        console.print("[yellow]Verify your email address to use the agent.[/yellow]")


@org_app.command("list")
def org_list() -> None:
    """List your organizations, marking the current one and the ones signed in."""
    auth = auth_file.load_profiles()
    current = auth.current_profile if auth is not None else None
    if current is None:
        console.print("Not logged in. Run [bold]alkera login[/bold].")
        raise typer.Exit(code=1)
    try:
        listed = orgs_api.list_orgs(current)
    except memberships_api.MembershipsError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Organization")
    table.add_column("Role")
    table.add_column("Signed in")
    table.add_column("Id", style="dim")
    for row in listed:
        table.add_row(
            "*" if row.current else "",
            row.org_name or row.org_team_id,
            row.role,
            "yes" if row.stored else "no",
            row.org_team_id,
        )
    console.print(table)


@org_app.command("switch")
def org_switch(org: str = typer.Argument(..., help="Organization id or name.")) -> None:
    """Make another organization current. Signs in to it when needed."""
    try:
        switched = orgs_api.switch_org(org)
    except orgs_api.AmbiguousOrgNameError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if switched is not None:
        console.print(f"[green]✓[/green] Current organization: [bold]{switched.org_label}[/bold]")
        return
    api_url = get_settings().alkera_api_url.rstrip("/")
    run_device_login(api_url, org=org, force=True)


@project_app.command("unpin")
def project_unpin(
    project: Path | None = typer.Option(
        None, "--project", "-p", help="Project root (defaults to cwd)."
    ),
) -> None:
    """Release this project from its organization so it can sync to another."""
    directory = _project_or_none(project)
    if directory is None or not unpin_project(directory):
        console.print("This project is not linked to an organization.")
        return
    console.print("[green]✓[/green] Unlinked. The next sync links it to the organization in use.")


def _verification_page_url() -> str:
    """The web app's email-verification page — where a login refused for an
    unverified email sends the user (resend + live status live there)."""
    return get_settings().alkera_frontend_url.rstrip("/") + "/verify-email"


def _refuse_unverified_login(email: str) -> None:
    """Refuse a CLI sign-in for an account that hasn't proven its email, with
    the fix in hand: the verification page, opened best-effort and printed for
    headless sessions. The web portal stays open to unverified accounts — it's
    where verifying happens — but the agent surfaces require a proven email."""
    url = _verification_page_url()
    console.print(
        f"[red]✗[/red] [bold]{email}[/bold] hasn't verified its email address, "
        "so it can't use the Alkera agent yet."
    )
    console.print(f"\n  Verify it here: [cyan]{url}[/cyan]\n")
    console.print("Then run [bold]alkera login[/bold] again.")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: S110 — headless sessions fall back to the printed URL
        pass
    raise typer.Exit(code=1)


def _org_for_approval(api_url: str, org: str) -> str:
    """The org id the approval page pre-selects for ``org``. A name is looked up
    in the memberships a stored sign-in for this API can read; without one it is
    passed as given and chosen on the page."""
    auth = auth_file.load_profiles()
    reader = next(
        (p for p in (auth.profiles if auth else []) if p.api_url.rstrip("/") == api_url),
        None,
    )
    if reader is None:
        return org
    try:
        found = memberships_api.find_membership(memberships_api.fetch_memberships(reader), org)
    except memberships_api.MembershipsError:
        return org
    return found.org_team_id if found is not None else org


def run_device_login(api_url: str, *, org: str | None = None, force: bool = False) -> None:
    """The device sign-in, saving the token as the profile its own claims name
    and making it current. ``org`` pre-selects the org on the approval page."""
    if not force and org is None:
        _check_existing_login()

    try:
        device = device_flow.request_device_code(api_url, client_id=device_flow.CLIENT_ID_CLI)
    except device_flow.DeviceCodeRequestError as exc:
        console.print(f"[red]✗[/red] {no_server_refusal(exc, get_settings()) or exc}")
        raise typer.Exit(code=1) from exc

    approval_url = device_flow.approval_url_for_org(
        device.verification_uri_complete, _org_for_approval(api_url, org) if org else None
    )
    console.print("\nTo sign in, open this URL in your browser:")
    console.print(f"\n  [cyan]{approval_url}[/cyan]\n")
    console.print("and check the code matches:")
    console.print(f"\n  [bold green]{device.user_code}[/bold green]\n")
    console.print("[dim]You can approve on any device — it doesn't need to be this one.[/dim]")

    # Best-effort: pop the browser on this machine to the code-prefilled URL.
    # Swallow every failure so headless / SSH sessions fall back to the printed
    # URL above without a traceback — there's nothing actionable to log.
    try:
        webbrowser.open(approval_url)
    except Exception:  # noqa: S110
        pass

    console.print("[dim]Waiting for you to approve in the browser… (Ctrl-C to cancel)[/dim]")

    def _on_slow_down(new_interval: int) -> None:
        console.print(f"[dim]Server asked us to slow down; polling every {new_interval}s.[/dim]")

    try:
        token = device_flow.poll_for_token(
            api_url,
            device.device_code,
            client_id=device_flow.CLIENT_ID_CLI,
            interval=device.interval,
            expires_in=device.expires_in,
            on_slow_down=_on_slow_down,
        )
    except device_flow.AuthorizationDeniedError:
        console.print("[red]✗[/red] Login was denied.")
        raise typer.Exit(code=1) from None
    except device_flow.DeviceCodeExpiredError:
        console.print("[red]✗[/red] The login code expired. Run `alkera login` again.")
        raise typer.Exit(code=1) from None
    except device_flow.DeviceFlowError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Login canceled.[/yellow]")
        raise typer.Exit(code=1) from None

    outcome = login_flow.complete_login(api_url, token)
    if outcome.refusal == "api_rejected":
        console.print(
            "[red]✗[/red] Received a token but it didn't authenticate against "
            f"{api_url}. Check ALKERA_API_URL and try again."
        )
        raise typer.Exit(code=1)
    if outcome.refusal == "email_verification_required":
        # Refused on the /auth/me identity, so it holds even when the gateway is
        # unreachable. Nothing is saved: an unverified account gets no session.
        _refuse_unverified_login(outcome.email or "")
    gateway = outcome.gateway
    if outcome.refusal == "gateway_rejected":
        detail = gateway.detail if gateway is not None else None
        console.print(
            f"[red]✗[/red] The gateway at {get_settings().alkera_gateway_url} refused the "
            f"new sign-in ({detail}). Check ALKERA_GATEWAY_URL and try again."
        )
        raise typer.Exit(code=1)
    if gateway is not None and gateway.accepted is None:
        console.print(
            "[yellow]Couldn't validate the token against the gateway; saving API "
            f"authentication only ({gateway.detail}).[/yellow]"
        )
    profile = outcome.profile
    where = f" in [bold]{profile.org_label}[/bold]" if profile and profile.org_label else ""
    console.print(f"[green]✓[/green] Logged in as [bold]{outcome.email}[/bold]{where}.")


def _check_existing_login() -> None:
    """Before a new device login, say what the saved one is worth.

    Only a sign-in the API accepts and the gateway does not reject asks
    "Re-login?". Any failure falls through to the device flow, so a token
    whose user was removed can always sign in again. An unverified account
    stops here: signing in again as the same account cannot help."""
    status = login_flow.auth_status()
    if status.reason == "missing":
        return
    if status.reason == "email_verification_required":
        _refuse_unverified_login(status.email or "")
    if status.reason == "invalid" or status.stored is None:
        console.print("[yellow]Existing token is invalid or expired; re-authenticating.[/yellow]")
        return
    gateway = login_flow.check_gateway(status.stored.token)
    if gateway.accepted is False:
        console.print(
            "[yellow]Existing token authenticates with the API but the gateway "
            f"rejected it ({gateway.detail}); re-authenticating.[/yellow]"
        )
        return
    console.print(f"[green]✓[/green] Already logged in as [bold]{status.email}[/bold].")
    if gateway.accepted is None:
        console.print(
            "[yellow]Couldn't validate the token against the gateway; "
            "continuing with API authentication only "
            f"({gateway.detail}).[/yellow]"
        )
    if not typer.confirm("Re-login?", default=False):
        raise typer.Exit(0)


__all__ = ["org_app", "project_app", "run_device_login", "whoami"]
