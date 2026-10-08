"""What a private distribution adds to the `alkera` command tree.

The open CLI mounts its own commands in :mod:`alkera_cli.main`. A distribution
that ships more registers a :class:`CommandMount` into :data:`CLI_COMMANDS`
from its extension's ``install``; the root app mounts every one after its own
(:func:`mount_commands`), so a contributed name can never replace an open
command. A distribution that runs something on every launch registers a
:data:`CliStartupHook` into :data:`CLI_STARTUP`, one that gives bare
``alkera`` something to open registers a :data:`CliDefaultCommand` into
:data:`CLI_DEFAULT`, and one that does more with an unexpected error than print
it registers a :data:`CliCrashHook` into :data:`CLI_CRASH`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from alkera_core.extensions import ExtensionError, ExtensionPoint


@dataclass(frozen=True, slots=True)
class CommandMount:
    """One command: a Typer ``group`` mounted under ``name``, or a single
    ``command`` function. It mounts at the top level, or under the open
    top-level group named ``parent``."""

    name: str
    group: typer.Typer | None = None
    command: Callable[..., Any] | None = None
    hidden: bool = True
    parent: str | None = None

    def __post_init__(self) -> None:
        if (self.group is None) == (self.command is None):
            raise ValueError(f"command mount {self.name!r} needs exactly one of group or command")


CLI_COMMANDS: ExtensionPoint[CommandMount] = ExtensionPoint("cli_commands")

#: Called by the root command on every launch, before the subcommand runs, with
#: the subcommand's name (``None`` for bare ``alkera``).
CliStartupHook = Callable[[str | None], None]

CLI_STARTUP: ExtensionPoint[CliStartupHook] = ExtensionPoint("cli_startup")

#: What bare ``alkera`` runs, called with the root ``--project`` option. With
#: none registered, bare ``alkera`` prints the help.
CliDefaultCommand = Callable[[Path | None], None]

CLI_DEFAULT: ExtensionPoint[CliDefaultCommand] = ExtensionPoint("cli_default")


#: Called with an error no command handled, after the CLI has printed it and
#: before the process exits 1.
CliCrashHook = Callable[[BaseException], None]

CLI_CRASH: ExtensionPoint[CliCrashHook] = ExtensionPoint("cli_crash")


def default_command(registered: tuple[CliDefaultCommand, ...]) -> CliDefaultCommand | None:
    """The one command bare ``alkera`` runs (the root passes
    ``CLI_DEFAULT.items()``), or ``None`` when no distribution names one.

    Refuses two: which would win depends on installation order, silently."""
    if len(registered) > 1:
        raise ExtensionError("more than one default command is registered for bare `alkera`")
    return registered[0] if registered else None


def _mounted_names(app: typer.Typer) -> set[str]:
    """The top-level names ``app`` already answers to."""
    names = {info.name for info in app.registered_groups if info.name}
    for info in app.registered_commands:
        if info.name:
            names.add(info.name)
        elif info.callback is not None:
            names.add(info.callback.__name__.replace("_", "-"))
    return names


def _group_named(app: typer.Typer, name: str) -> typer.Typer:
    for info in app.registered_groups:
        if info.name == name and info.typer_instance is not None:
            return info.typer_instance
    raise ExtensionError(f"the CLI has no command group {name!r} to mount under")


def mount_commands(app: typer.Typer, mounts: tuple[CommandMount, ...]) -> None:
    """Mount ``mounts`` (the root app passes ``CLI_COMMANDS.items()``) on
    ``app``, in order, each at the top level or under its ``parent`` group.

    Refuses a name its target (or an earlier mount there) already answers to:
    Click would let the later one shadow the first, silently."""
    taken: dict[str | None, set[str]] = {None: _mounted_names(app)}
    for mount in mounts:
        target = app if mount.parent is None else _group_named(app, mount.parent)
        names = taken.setdefault(mount.parent, _mounted_names(target))
        if mount.name in names:
            where = "the CLI" if mount.parent is None else f"`{mount.parent}`"
            raise ExtensionError(f"the command {mount.name!r} is already part of {where}")
        names.add(mount.name)
        if mount.group is not None:
            target.add_typer(mount.group, name=mount.name, hidden=mount.hidden)
        elif mount.command is not None:
            target.command(mount.name, hidden=mount.hidden)(mount.command)


__all__ = [
    "CLI_COMMANDS",
    "CLI_CRASH",
    "CLI_DEFAULT",
    "CLI_STARTUP",
    "CliCrashHook",
    "CliDefaultCommand",
    "CliStartupHook",
    "CommandMount",
    "default_command",
    "mount_commands",
]
