"""Every notebook setting, in one place.

A notebook's settings live in its header block. Each is a
:class:`SettingSpec`: its type and the values it admits, its default, what a
person reads about it, the control that edits it and what changing it does
beyond the file. The header reader and writer, ``set_setting`` (on the engine
and in the platform's live document), the engine's settings model, the agent's
``notebook.settings`` tool and the portal's settings form all read this table;
nothing else lists a setting's name or values. ``export_schema`` is what the
portal gets (``packages/shared-openapi/notebook-settings.json``).

A cell kind's own settings (a SQL cell's connection, result name and whether
it shows its result; a Markdown cell's interpolation) are specs too, keyed by
kind, changed by ``set_meta``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

SettingType = Literal["enum", "bool", "int", "env", "text"]
#: The control a form draws for a setting; ``none`` needs a reason.
Control = Literal["enum", "bool", "int-range", "env-ref", "connection-ref", "identifier", "none"]
#: What a change does beyond the file.
Effect = Literal["none", "restart_kernel", "next_run"]
#: Where an effective value came from.
Source = Literal["notebook", "workspace", "detected", "default"]


#: What a notebook's ``env`` may say: the workspace default, its own PEP 723
#: block, or a relative path (no NUL, at most 1,024 characters in all).
ENV_PATTERN = r"(default|script|\.\.?/[^\x00]{0,1020})"


@dataclass(frozen=True)
class SettingSpec:
    name: str
    type: SettingType
    default: Any
    label: str
    help: str
    control: Control
    effect: Effect = "none"
    choices: tuple[tuple[Any, str], ...] = ()
    """``(value, what a person reads)`` for an enum."""
    minimum: int | None = None
    maximum: int | None = None
    reason: str = ""
    """Why a setting has no control (``control == "none"``)."""
    pattern: str = ""
    """For ``env`` and ``text``: the whole value must match it (exported, so a
    client checks exactly what the server checks)."""

    def valid(self, value: object) -> bool:
        if self.type == "enum":
            return any(value == v for v, _ in self.choices)
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            low = self.minimum if self.minimum is not None else value
            high = self.maximum if self.maximum is not None else value
            return low <= value <= high
        if not isinstance(value, str):
            return False
        return not self.pattern or re.fullmatch(self.pattern, value) is not None

    @property
    def expected(self) -> str:
        """What a valid value is, for a violation's message."""
        if self.type == "enum":
            names = [f'"{v}"' for v, _ in self.choices]
            return ", ".join(names[:-1]) + " or " + names[-1]
        if self.type == "bool":
            return "true or false"
        if self.type == "int":
            return f"a whole number from {self.minimum:,} to {self.maximum:,}"
        if self.type == "env":
            return '"default", "script", or a path starting with ./ or ../'
        return "text"


NOTEBOOK_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "reactivity",
        "enum",
        "autorun",
        "When a cell runs",
        "What happens to the cells that read what a cell defines.",
        "enum",
        choices=(
            ("autorun", "Cells that read a changed value re-run"),
            ("lazy", "They are marked stale instead"),
        ),
    ),
    SettingSpec(
        "dataframe",
        "enum",
        "auto",
        "Frames as",
        "The frame type a SQL cell's result is.",
        "enum",
        effect="next_run",
        choices=(("auto", "Whichever is installed"), ("polars", "Polars"), ("pandas", "pandas")),
    ),
    SettingSpec(
        "env",
        "env",
        None,
        "Environment",
        "The environment the notebook runs in.",
        "env-ref",
        effect="restart_kernel",
        pattern=ENV_PATTERN,
    ),
    SettingSpec(
        "outputs_in_git",
        "bool",
        False,
        "Save outputs with the file",
        "Keep the saved outputs beside the notebook out of .gitignore.",
        "bool",
    ),
    SettingSpec(
        "autoreload",
        "enum",
        "off",
        "Reload changed modules",
        "Reload the workspace's changed Python modules before each run.",
        "enum",
        effect="next_run",
        choices=(("off", "Off"), ("on", "On")),
    ),
    SettingSpec(
        "sql_row_limit",
        "int",
        None,
        "Rows per SQL result",
        "The most rows kept from a query that has no LIMIT of its own. Empty keeps them all.",
        "int-range",
        effect="next_run",
        minimum=1,
        maximum=10_000_000,
    ),
)
"""Known keys after ``format``, in the order writers emit them."""

CELL_SETTINGS: dict[str, tuple[SettingSpec, ...]] = {
    "sql": (
        SettingSpec(
            "connection",
            "text",
            None,
            "Connection",
            "Where the query runs. Empty runs it on the notebook's own frames.",
            "connection-ref",
        ),
        SettingSpec(
            "output_var",
            "text",
            "_df",
            "Result name",
            "The name later cells read the result by.",
            "identifier",
        ),
        SettingSpec(
            "show_output",
            "bool",
            True,
            "Show result",
            "Show the result under the cell.",
            "bool",
        ),
    ),
    "markdown": (
        SettingSpec(
            "quote",
            "enum",
            "r",
            "Interpolate values",
            "Fill {name} in the text with the value it names.",
            "enum",
            choices=(("r", "Off"), ("rf", "On")),
        ),
    ),
}

BY_NAME: dict[str, SettingSpec] = {s.name: s for s in NOTEBOOK_SETTINGS}


def default_settings() -> dict[str, Any]:
    return {s.name: s.default for s in NOTEBOOK_SETTINGS if s.default is not None}


def setting_sources(
    stored: Mapping[str, Any], workspace: Mapping[str, Any] | None = None
) -> dict[str, Source]:
    """Where each setting's effective value comes from: the file, the
    workspace's defaults, detection (an ``env`` the file does not name) or
    the setting's own default."""
    found: dict[str, Source] = {}
    for spec in NOTEBOOK_SETTINGS:
        if stored.get(spec.name) is not None:
            found[spec.name] = "notebook"
        elif workspace is not None and workspace.get(spec.name) is not None:
            found[spec.name] = "workspace"
        elif spec.name == "env":
            found[spec.name] = "detected"
        else:
            found[spec.name] = "default"
    return found


def stored_settings(read: Mapping[str, Any]) -> dict[str, Any]:
    """What the file itself sets, from a settings read (each effective value
    plus ``sources``): only the values whose source is ``notebook``. A read
    with no ``sources`` (a reader that predates them) keeps every setting it
    names. ``sources`` and the inherited values never come back as settings
    of the file."""
    sources = read.get("sources")
    stored: dict[str, Any] = {}
    for spec in NOTEBOOK_SETTINGS:
        value = read.get(spec.name)
        if value is None:
            continue
        if isinstance(sources, Mapping) and sources.get(spec.name) != "notebook":
            continue
        stored[spec.name] = value
    return stored


def _spec_json(spec: SettingSpec) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": spec.name,
        "type": spec.type,
        "default": spec.default,
        "label": spec.label,
        "help": spec.help,
        "control": spec.control,
        "effect": spec.effect,
    }
    if spec.choices:
        out["choices"] = [{"value": v, "label": label} for v, label in spec.choices]
    if spec.minimum is not None:
        out["minimum"] = spec.minimum
    if spec.maximum is not None:
        out["maximum"] = spec.maximum
    if spec.reason:
        out["reason"] = spec.reason
    if spec.pattern:
        out["pattern"] = spec.pattern
    return out


def export_schema() -> dict[str, Any]:
    """The schema a form renders: the notebook's settings and each cell
    kind's."""
    return {
        "notebook": [_spec_json(s) for s in NOTEBOOK_SETTINGS],
        "cells": {kind: [_spec_json(s) for s in specs] for kind, specs in CELL_SETTINGS.items()},
    }


__all__ = [
    "BY_NAME",
    "CELL_SETTINGS",
    "NOTEBOOK_SETTINGS",
    "Control",
    "Effect",
    "SettingSpec",
    "Source",
    "default_settings",
    "export_schema",
    "setting_sources",
    "stored_settings",
]
