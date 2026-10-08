"""Shared slash-command vocabulary for the chat clients.

A small declarative command registry, kept separate from any UI so the
same commands back both the terminal client and the daemon's
`harness.list_commands` / `run_command` (which the VS Code editor calls) —
one source of truth, no drift. Commands are unit-testable in isolation;
each parses its own arguments and returns a `CommandResult`:

- `OK`        — ran (including handled errors like a bad value); continue.
- `BAD_USAGE` — args were the wrong shape; the dispatcher auto-prints the
                command's `usage`, so handlers don't repeat that boilerplate.
- `EXIT`      — leave the chat.

Usage strings use `()` for a required arg and `[]` for an optional one,
e.g. `/preferences [set (key) (value)]`.

The dispatcher is pure: it takes a `SlashContext` (session + the mutable
display state + injected preference accessors + the output console), so
tests construct one with a stub session and a `Console(file=StringIO())`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal

import httpx
from alkera_core.money import usd_display, usd_to_credits
from alkera_core.schemas.preferences import Preferences
from rich.console import Console
from rich.table import Table

from alkera_cli.account.auth_file import Profile, ProfileResolutionError
from alkera_cli.account.binding import acting_profile
from alkera_cli.chat.usage import WINDOWS, fetch_usage, render_usage
from alkera_cli.harness import MODE_LABELS, ChatSession, parse_mode
from alkera_cli.preferences.user import settable_fields


class CommandResult(Enum):
    OK = auto()
    BAD_USAGE = auto()
    EXIT = auto()


@dataclass
class DisplayState:
    """Mutable display state shared by the REPL and slash commands."""


@dataclass
class SlashContext:
    session: ChatSession
    display: DisplayState
    console: Console
    load_prefs: Callable[[], Preferences]
    set_pref: Callable[[str, str], Preferences]
    compact: Callable[[], None]
    """Trigger a context compaction as a turn. Wired by the REPL to
    begin a turn + schedule `session.compact()`; the dispatcher stays
    sync (the actual await happens on the event loop)."""
    clear: Callable[[], None]
    """Reset the conversation context to empty. Wired by the REPL to
    schedule `session.clear()`; like `compact`, the dispatcher stays sync
    and the actual await happens on the event loop."""
    get_title: Callable[[], str | None]
    """Read the chat's current title (`None` when untitled)."""
    set_title: Callable[[str], None]
    """Set the chat's title. Wired by the REPL to schedule
    `session.set_title(...)`; the dispatcher stays sync."""


Handler = Callable[["SlashContext", str], CommandResult]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    summary: str
    usage: str
    handler: Handler
    hidden: bool = False
    cli_only: bool = False
    """The command's interaction model only makes sense in the REPL (e.g. the
    editor has a native mode picker / preferences surface). Editors exclude it
    from menus and answer a typed invocation with `ui_hint`."""
    ui_hint: str | None = None
    """For `cli_only` commands: where the editor's equivalent lives."""
    editor_hidden: bool = False
    """Stronger than `cli_only`: the editor surface does not know this command
    exists AT ALL — it is dropped from `harness.list_commands` and a typed
    invocation answers `unknown` (NOT a `cli_only` hint card). For commands whose
    only editor path is a dedicated native control (the mode picker, the
    Preferences tab, the Cost activity tab), where a redirect card is just noise.
    The CLI still dispatches them normally."""


@dataclass(frozen=True)
class UiCommandOutcome:
    """Structured result of a slash command for EDITOR rendering — same
    registry + argument entry point as the CLI, different renderer: the CLI
    prints via the console handler, editors get data and draw native UI."""

    kind: Literal["ok", "bad_usage", "exit", "cli_only", "unknown"]
    command: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    message: str | None = None


# --- handlers --------------------------------------------------------------


def _cmd_help(ctx: SlashContext, _args: str) -> CommandResult:
    table = Table(title="Commands", show_lines=False, title_justify="left")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    for spec in COMMANDS:
        if spec.hidden:
            continue
        cmd = f"/{spec.name} {spec.usage}".rstrip()
        if spec.aliases:
            cmd += f"  ({', '.join('/' + a for a in spec.aliases)})"
        table.add_row(cmd, spec.summary)
    ctx.console.print(table)
    # Spell out how to change a setting — the command row alone is easy to
    # miss. Example key is derived so it can't drift from the schema.
    fields = settable_fields()
    keys = ", ".join(fields) or "(none)"
    example_key = next(iter(fields), "<key>")
    ctx.console.print(
        f"[dim]Set a preference: [bold]/prefs set <key> <value>[/bold] "
        f"— e.g. [bold]/prefs set {example_key} true[/bold]. Keys: {keys}.[/dim]"
    )
    return CommandResult.OK


def _switch_mode(ctx: SlashContext, raw: str) -> CommandResult:
    mode = parse_mode(raw)
    if mode is None:
        ctx.console.print(
            f"[red]unknown mode '{raw}'[/red] [dim](read-only, default, auto, plan, bypass)[/dim]"
        )
        return CommandResult.OK
    ctx.session.set_permission_mode(mode)
    ctx.console.print(f"[dim]mode → [bold]{MODE_LABELS[mode]}[/bold][/dim]")
    return CommandResult.OK


def _cmd_mode(ctx: SlashContext, args: str) -> CommandResult:
    arg = args.strip()
    if not arg:
        current = MODE_LABELS[ctx.session.permission_mode]
        options = " ".join(MODE_LABELS.values())
        ctx.console.print(f"[dim]mode: [bold]{current}[/bold]  (options: {options})[/dim]")
        return CommandResult.OK
    return _switch_mode(ctx, arg)


def _cmd_cost(ctx: SlashContext, args: str) -> CommandResult:
    """Show this chat's tool-execution spend vs caps, or set per-window caps.

    ``/cost``                       → the chat/day/week spend table
    ``/cost set chat=50 day=200``   → write caps to permissions.local.yml
    """
    from datetime import UTC, datetime

    from alkera_cli.plugins.plugin_base.cost import (
        CostLedger,
        CostLimits,
        cost_overview,
        unknown_cost_keys,
    )
    from alkera_cli.plugins.plugin_base.permissions import load_permissions
    from alkera_cli.plugins.plugin_base.permissions.config import update_local_cost_caps

    project = ctx.session.project
    arg = args.strip()
    if arg.startswith("set"):
        rest = arg[len("set") :].strip()
        if not rest:
            return CommandResult.BAD_USAGE
        caps: dict[str, float] = {}
        for pair in rest.split():
            window, sep, value = pair.partition("=")
            if not sep:
                return CommandResult.BAD_USAGE
            try:
                caps[window.strip()] = float(value)
            except ValueError:
                ctx.console.print(f"[red]not a number: {value!r}[/red]")
                return CommandResult.OK
        try:
            update_local_cost_caps(project.path, caps)
        except ValueError as exc:
            ctx.console.print(f"[red]{exc}[/red]")
            return CommandResult.OK
        pretty = ", ".join(f"{k}={usd_display(v)}" for k, v in caps.items())
        ctx.console.print(f"[dim]cost caps set in permissions.local.yml: {pretty}[/dim]")
        return CommandResult.OK

    perms = load_permissions(project.path)
    limits = CostLimits.from_config(perms.cost)
    ov = cost_overview(CostLedger(project), ctx.session.session_id, limits, now=datetime.now(UTC))
    table = Table(title="Tool-execution cost", show_lines=False, title_justify="left")
    table.add_column("Window", style="cyan")
    table.add_column("Spent", justify="right")
    table.add_column("Cap", justify="right")
    for window in ("chat", "day", "week"):
        table.add_row(window, usd_display(ov["spent"][window]), usd_display(ov["caps"][window]))
    table.add_row("per-query", "—", usd_display(ov["caps"]["per_query"]))
    ctx.console.print(table)
    if ov["org_managed"]:
        ctx.console.print("[dim]caps are org-managed (overages reject, not prompt-to-raise)[/dim]")
    # Surface a hand-edit typo that silently did nothing (e.g. `chat_usd_cpa`).
    stray = unknown_cost_keys(perms.cost)
    if stray:
        ctx.console.print(
            f"[yellow]⚠ ignored unrecognized cost key(s): {', '.join(stray)} (typo?)[/yellow]"
        )
    ctx.console.print("[dim]set caps with: /cost set chat=50 day=200[/dim]")
    return CommandResult.OK


def _show_preferences(ctx: SlashContext) -> None:
    prefs = ctx.load_prefs()
    table = Table(title="Preferences", show_lines=False, title_justify="left")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white", no_wrap=True)
    table.add_column("Description", style="dim")
    for name, info in settable_fields().items():
        table.add_row(name, str(getattr(prefs, name)), info.description or "")
    ctx.console.print(table)


def _cmd_preferences(ctx: SlashContext, args: str) -> CommandResult:
    parts = args.split(maxsplit=2)
    if not parts:
        _show_preferences(ctx)
        return CommandResult.OK
    if parts[0] != "set" or len(parts) != 3:
        return CommandResult.BAD_USAGE
    _, key, value = parts
    try:
        new = ctx.set_pref(key, value)
    except ValueError as exc:
        ctx.console.print(f"[red]✗[/red] {exc}")
        return CommandResult.OK
    ctx.console.print(f"[dim]✓ {key} → [bold]{getattr(new, key)}[/bold][/dim]")
    return CommandResult.OK


def _cmd_onboarding(ctx: SlashContext, _args: str) -> CommandResult:
    # The tour is a client-UI surface (the editor replays it in place; the TUI
    # has its own `/onboarding` in `ui.tui.screens.commands`). A bare console
    # can only point the way.
    ctx.console.print("[dim]Replay the welcome tour from the Alkera view.[/dim]")
    return CommandResult.OK


def _cmd_compact(ctx: SlashContext, _args: str) -> CommandResult:
    ctx.compact()
    return CommandResult.OK


def _cmd_clear(ctx: SlashContext, _args: str) -> CommandResult:
    ctx.clear()
    return CommandResult.OK


def _cmd_title(ctx: SlashContext, args: str) -> CommandResult:
    # `args` is the full remainder after `/title`, already trimmed by the
    # dispatcher — internal spaces are preserved, so multi-word titles
    # work as typed. No args → show the current title.
    title = args.strip()
    if not title:
        current = ctx.get_title()
        ctx.console.print(f"[dim]title:[/dim] {current or '(untitled)'}")
        return CommandResult.OK
    ctx.set_title(title)
    return CommandResult.OK


def _chat_profile(ctx: SlashContext) -> tuple[Profile | None, str | None]:
    """The sign-in this chat acts as (the one it bound when it opened), or the
    one-line reason there is none."""
    try:
        profile = acting_profile(ctx.session.project, credential=ctx.session.credential)
    except ProfileResolutionError as exc:
        return None, str(exc)
    if profile is None or not profile.token:
        return None, None
    return profile, None


def _cmd_usage(ctx: SlashContext, args: str) -> CommandResult:
    window = args.strip() or "30d"
    if window not in WINDOWS:
        return CommandResult.BAD_USAGE
    auth, refused = _chat_profile(ctx)
    if auth is None:
        if refused:
            ctx.console.print(f"[red]✗[/red] {refused}")
        else:
            ctx.console.print("[red]✗[/red] Not signed in — run [bold]alkera login[/bold].")
        return CommandResult.OK
    try:
        credits, usage = fetch_usage(
            auth.api_url, auth.token, window=window, org_id=auth.org_team_id
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            ctx.console.print("[red]✗[/red] Session expired — run [bold]alkera login[/bold].")
        else:
            ctx.console.print(f"[red]✗[/red] usage request failed ({exc.response.status_code}).")
        return CommandResult.OK
    except httpx.HTTPError as exc:
        ctx.console.print(f"[red]✗[/red] couldn't reach the API: {exc}")
        return CommandResult.OK
    render_usage(ctx.console, credits, usage)
    return CommandResult.OK


def _cmd_exit(_ctx: SlashContext, _args: str) -> CommandResult:
    return CommandResult.EXIT


def _mode_switch_handler(word: str) -> Handler:
    def handler(ctx: SlashContext, _args: str) -> CommandResult:
        return _switch_mode(ctx, word)

    return handler


# --- registry --------------------------------------------------------------

# Direct mode shortcuts (`/plan`, `/bypass`, …) — hidden from `/help` (the
# `mode` row documents them) but still dispatchable, preserving the prior
# REPL behavior.
_MODE_SHORTCUTS = ("normal", "read-only", "auto", "plan", "bypass", "yolo")

COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "help",
        (),
        "List available commands.",
        "",
        _cmd_help,
        cli_only=True,
        ui_hint="Type / in the composer to browse commands.",
    ),
    CommandSpec(
        "preferences",
        ("prefs",),
        "Show preferences, or set one.",
        "[set (key) (value)]",
        _cmd_preferences,
        cli_only=True,
        ui_hint="Opens the Alkera preferences panel.",
    ),
    # `mode` and `cost` are listed for the editor menu (NOT editor_hidden) but
    # stay `cli_only`: the editor drives them through a client-side control (the
    # mode picker / the cost gauge) rather than a `dispatch_ui` handler, so they
    # never round-trip `run_command`. The slash menu offers them; the panel does
    # the work via `harness.set_permission_mode` / `harness.get_cost_state`.
    CommandSpec(
        "mode",
        (),
        "Show, or switch permission mode (read-only/default/auto/plan/bypass).",
        "[(name)]",
        _cmd_mode,
        cli_only=True,
    ),
    CommandSpec(
        "cost",
        (),
        "Show tool-execution spend vs caps, or set caps.",
        "[set (window)=(usd) …]",
        _cmd_cost,
        cli_only=True,
    ),
    CommandSpec(
        "compact",
        (),
        "Summarize the conversation now to reclaim context.",
        "",
        _cmd_compact,
    ),
    CommandSpec(
        "clear",
        (),
        "Clear the conversation; the next turn starts fresh.",
        "",
        _cmd_clear,
    ),
    CommandSpec(
        "title",
        (),
        "Show the chat title, or set it.",
        "[(text)]",
        _cmd_title,
    ),
    CommandSpec(
        "usage",
        (),
        "Show your credit balance and usage.",
        "[(window)]",
        _cmd_usage,
    ),
    CommandSpec("exit", ("quit",), "Leave the chat.", "", _cmd_exit),
    # The welcome tour. `cli_only` here — the editor runs it client-side (the
    # `onboarding` REGISTRY entry posts the `alkera.onboarding` host command);
    # the TUI has its own `/onboarding` in `ui.tui.screens.commands`.
    CommandSpec(
        "onboarding",
        (),
        "Replay the welcome tour.",
        "",
        _cmd_onboarding,
        cli_only=True,
        ui_hint="Replays the Alkera welcome tour.",
    ),
    # The direct mode shortcuts are listed for the editor (so a typed `/plan`
    # resolves) but `hidden` keeps them out of the browsable menu — the `/mode`
    # panel is their front door there. Like `mode`, they're driven client-side
    # (`harness.set_permission_mode`), so they stay `cli_only` with no handler.
    *(
        CommandSpec(
            word,
            (),
            f"Switch to {word} mode.",
            "",
            _mode_switch_handler(word),
            hidden=True,
            cli_only=True,
        )
        for word in _MODE_SHORTCUTS
    ),
)


def _build_registry() -> dict[str, CommandSpec]:
    registry: dict[str, CommandSpec] = {}
    for spec in COMMANDS:
        for key in (spec.name, *spec.aliases):
            registry[key] = spec
    return registry


_REGISTRY = _build_registry()


def dispatch(line: str, ctx: SlashContext) -> CommandResult:
    """Parse and run a slash command. `line` may include the leading `/`.

    On `BAD_USAGE`, auto-prints the command's usage so each handler stays
    a one-liner for the wrong-args case.
    """
    body = line[1:] if line.startswith("/") else line
    parts = body.split(maxsplit=1)
    name = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""

    spec = _REGISTRY.get(name)
    if spec is None:
        ctx.console.print(f"[dim]unknown command `/{name}` — try /help[/dim]")
        return CommandResult.OK

    result = spec.handler(ctx, args)
    if result is CommandResult.BAD_USAGE:
        ctx.console.print(f"[dim]usage: /{spec.name} {spec.usage}".rstrip() + "[/dim]")
    return result


# --- editor (UI) execution ---------------------------------------------------
#
# Same registry, same argument entry point, different renderer: the CLI's
# `dispatch` prints through the console handlers above; editors call
# `dispatch_ui` and get a STRUCTURED outcome to draw native UI from. Only the
# commands whose interaction works in an editor have a UI handler — the rest
# are `cli_only` and answer with their `ui_hint`.


def _ui_compact(ctx: SlashContext, _args: str) -> UiCommandOutcome:
    ctx.compact()
    return UiCommandOutcome(kind="ok", command="compact")


def _ui_clear(ctx: SlashContext, _args: str) -> UiCommandOutcome:
    ctx.clear()
    return UiCommandOutcome(kind="ok", command="clear")


def _ui_title(ctx: SlashContext, args: str) -> UiCommandOutcome:
    title = args.strip()
    if not title:
        return UiCommandOutcome(
            kind="ok", command="title", payload={"action": "show", "title": ctx.get_title()}
        )
    ctx.set_title(title)
    return UiCommandOutcome(kind="ok", command="title", payload={"action": "set", "title": title})


def _ui_usage(ctx: SlashContext, args: str) -> UiCommandOutcome:
    window = args.strip() or "30d"
    if window not in WINDOWS:
        return UiCommandOutcome(kind="bad_usage", command="usage")
    auth, refused = _chat_profile(ctx)
    if auth is None:
        return UiCommandOutcome(
            kind="ok",
            command="usage",
            payload={"error": refused or "Not signed in — run `alkera login`."},
        )
    try:
        credits, _usage = fetch_usage(
            auth.api_url, auth.token, window=window, org_id=auth.org_team_id
        )
    except httpx.HTTPStatusError as exc:
        error = (
            "Session expired — sign in again."
            if exc.response.status_code == 401
            else f"usage request failed ({exc.response.status_code})."
        )
        return UiCommandOutcome(kind="ok", command="usage", payload={"error": error})
    except httpx.HTTPError as exc:
        return UiCommandOutcome(
            kind="ok", command="usage", payload={"error": f"couldn't reach the API: {exc}"}
        )
    # The editor card shows the account balance + split and THIS chat's spend in
    # credits — not the account 30-day usage. `chat_credits` mirrors the TUI's
    # "Chat used" row, from the manifest's running cost (credits-only surface).
    return UiCommandOutcome(
        kind="ok",
        command="usage",
        payload={
            "credits": credits.model_dump(mode="json"),
            "chat_credits": usd_to_credits(ctx.session.manifest.cost_total),
        },
    )


def _ui_exit(_ctx: SlashContext, _args: str) -> UiCommandOutcome:
    return UiCommandOutcome(kind="exit", command="exit")


_UI_HANDLERS: dict[str, Callable[[SlashContext, str], UiCommandOutcome]] = {
    "compact": _ui_compact,
    "clear": _ui_clear,
    "title": _ui_title,
    "usage": _ui_usage,
    "exit": _ui_exit,
}


def dispatch_ui(line: str, ctx: SlashContext) -> UiCommandOutcome:
    """Editor-side dispatch: identical parsing to `dispatch`, structured
    results instead of console output. BAD_USAGE carries the usage string in
    `message` (same auto-render contract the CLI dispatcher provides)."""
    body = line[1:] if line.startswith("/") else line
    parts = body.split(maxsplit=1)
    name = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""

    spec = _REGISTRY.get(name)
    if spec is None or spec.editor_hidden:
        # `editor_hidden` commands don't exist as far as the editor is concerned:
        # answer exactly like an unknown command so a typed `/mode` / `/cost` /
        # `/preferences` nudges the user to the native control, not a hint card.
        return UiCommandOutcome(
            kind="unknown", message=f"unknown command `/{name}` — type / to browse commands"
        )
    if spec.cli_only:
        return UiCommandOutcome(
            kind="cli_only",
            command=spec.name,
            message=spec.ui_hint or f"/{spec.name} is available in the CLI.",
        )
    handler = _UI_HANDLERS.get(spec.name)
    if handler is None:  # a UI-capable spec must register a handler
        return UiCommandOutcome(
            kind="cli_only", command=spec.name, message=f"/{spec.name} is available in the CLI."
        )
    outcome = handler(ctx, args)
    if outcome.kind == "bad_usage" and outcome.message is None:
        return UiCommandOutcome(
            kind="bad_usage",
            command=spec.name,
            message=f"usage: /{spec.name} {spec.usage}".rstrip(),
        )
    return outcome


__all__ = [
    "COMMANDS",
    "CommandResult",
    "CommandSpec",
    "DisplayState",
    "SlashContext",
    "UiCommandOutcome",
    "dispatch",
    "dispatch_ui",
]
