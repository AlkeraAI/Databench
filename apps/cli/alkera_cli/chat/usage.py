"""Shared usage/credits fetch + Rich rendering for the CLI.

The `/usage` chat slash command and the daemon's `usage.get` method both go
through `fetch_usage`, which returns the same api-core shapes the web Usage page
consumes (`MyCreditsResponse` / `MyUsageResponse`).

OBFUSCATION: the plan's included allotment is never shown as an absolute number —
only the percentage used + the reset date + the EXACT non-allotment balances
(prepaid, promotional, on-demand). The activity breakdown is request COUNTS,
never credit spend.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from alkera_core.schemas.my_usage import MyCreditsResponse, MyUsageResponse
from rich.console import Console
from rich.table import Table

from alkera_cli.account.auth_file import org_headers

WINDOWS: tuple[str, ...] = ("7d", "30d", "90d", "all")


def fetch_usage(
    api_url: str,
    token: str,
    *,
    window: str = "30d",
    org_id: str = "",
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[MyCreditsResponse, MyUsageResponse]:
    """GET `/api/v1/me/credits` + `/api/v1/me/usage` and parse into the canonical
    api-core shapes. `org_id` is asserted (`X-Alkera-Org`) when known.
    `transport` is injectable for tests.

    Raises `httpx.HTTPStatusError` on a non-2xx (401 → re-auth) and other
    `httpx.HTTPError`s on transport failure — callers map these to messages.
    """
    headers = org_headers(token, org_id)
    with httpx.Client(
        base_url=api_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
    ) as client:
        credits_resp = client.get("/api/v1/me/credits")
        credits_resp.raise_for_status()
        usage_resp = client.get("/api/v1/me/usage", params={"window": window})
        usage_resp.raise_for_status()
    return (
        MyCreditsResponse.model_validate(credits_resp.json()),
        MyUsageResponse.model_validate(usage_resp.json()),
    )


def _fmt(n: int) -> str:
    return f"{n:,}"


def _pct(value: float) -> str:
    """Render a 0.5-rounded percentage without a trailing ``.0`` (``12.5`` / ``40``)."""
    return f"{value:g}%"


def _reset_in_days(reset_at: datetime | None) -> int | None:
    return None if reset_at is None else max(0, (reset_at - datetime.now(UTC)).days)


def credit_rows(credits: MyCreditsResponse) -> list[tuple[str, str, str]]:
    """The /usage rows as ``(value, label, detail)`` triples — the plan name, this
    cycle's % used, the reset countdown, and the EXACT non-allotment balances
    (prepaid always; promotional / on-demand only when the seat holds any).

    Pure (no I/O) so any client can render it without the network — the TUI maps the
    triples to its own row type. OBFUSCATION: no absolute allotment, no
    granted/consumed split, no pool balances.
    """
    rows: list[tuple[str, str, str]] = [
        ("plan", "Plan", credits.tier_name),
        ("usage", "Plan usage", f"{_pct(credits.pct_used)} of this cycle used"),
    ]
    days = _reset_in_days(credits.reset_at)
    if days is not None:
        rows.append(("reset", "Resets in", f"{days}d"))
    rows.append(("prepaid", "Prepaid credits", _fmt(credits.prepaid_credits)))
    if credits.promotional_credits:
        rows.append(("promotional", "Bonus credits", _fmt(credits.promotional_credits)))
    if credits.on_demand_credits:
        rows.append(("on_demand", "On-demand credits", _fmt(credits.on_demand_credits)))
    return rows


def render_usage(console: Console, credits: MyCreditsResponse, usage: MyUsageResponse) -> None:
    """Render the obfuscated plan summary + request activity to `console`."""
    days = _reset_in_days(credits.reset_at)
    reset = f" · resets in {days}d" if days is not None else ""
    balances = "".join(
        f" · [green]{_fmt(amount)}[/green] {label}"
        for amount, label in (
            (credits.prepaid_credits, "prepaid credits"),
            (credits.promotional_credits, "bonus credits"),
            (credits.on_demand_credits, "on-demand credits"),
        )
        if amount
    )
    console.print(
        f"[bold]Plan[/bold] [cyan]{credits.tier_name}[/cyan] — "
        f"[bold green]{_pct(credits.pct_used)}[/bold green] of this cycle used{reset}{balances}"
    )

    console.print()
    console.print(
        f"[bold]Activity[/bold] [dim]({usage.window})[/dim] — {usage.total_requests} requests"
    )
    if usage.by_model:
        by_model = Table(title="By model", title_justify="left", show_lines=False)
        by_model.add_column("Model", style="cyan")
        by_model.add_column("Requests", style="bold", justify="right")
        for model in usage.by_model:
            by_model.add_row(model.model_id, str(model.request_count))
        console.print(by_model)
