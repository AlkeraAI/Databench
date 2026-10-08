"""A notebook tool's ask reads as the notebook: the action on the file, the
cells it would run by the names a person knows, why it needs approval in plain
words, once. Never the cells' internal ids, never the machine's own paths.

The web card and the Slack card both render what ``present`` returns, so these
are the contract for both surfaces.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from alkera_core.permission_presentation import PermissionAsk, ask_from_event, present
from alkera_core.permission_presentation.model import AskSubject
from alkera_core.permission_presentation.present import (
    code_preview,
    effect_reason,
    related_line,
)

BOX_FILE = "/opt/alkera-work/.alkera/workspaces/115d3feb/files/analysis.alknb.py"
IDS = ("a7yg9x7evz", "007ferqvhc", "twtt7g67kt")

ALWAYS = [
    {"optionId": "allow_once", "name": "Allow once"},
    {"optionId": "allow_always", "name": "Always allow"},
    {"optionId": "reject_once", "name": "Reject once"},
]


def _run_event(**overrides: Any) -> dict[str, Any]:
    """A ``permission.request`` for a run of three cells, two of them nobody
    named, as the machine holding the notebook raises it (snake_case)."""
    event: dict[str, Any] = {
        "permission_kind": "notebook",
        "canonical_kind": "other",
        "patterns": ["Run 2 cells in analysis.alknb.py"],
        "subject": {
            "capability": "notebook",
            "effect": "write",
            "confidence": "unknown",
            "operation": "notebook_run",
            "scope": "command",
            "targets": [{"kind": "notebook", "name": BOX_FILE}],
            "reasons": [f"Cell 2 ({IDS[1]}): write"],
        },
        "preview": {
            "kind": "notebook",
            "title": "Run 2 cells in analysis.alknb.py",
            "notebook": {
                "lead": "Run 2 cells in",
                "file_name": "analysis.alknb.py",
                "file_path": "analysis.alknb.py",
                "cells": [
                    {"name": "setup", "code": "import alkera", "role": "dependency"},
                    {
                        "name": "Cell 2",
                        "code": "import alkera\n\nimport polars as pl\ndf = pl.read_csv('a.csv')",
                        "role": "target",
                    },
                    {"name": "Cell 3", "code": "df.head()", "role": "target"},
                ],
            },
        },
        "options": [{"option_id": "allow_once", "name": "Allow once"}],
    }
    event.update(overrides)
    return event


def _shown_text(raw: dict[str, Any]) -> str:
    return json.dumps(present(ask_from_event(raw)).to_json())


def test_a_run_reads_as_its_cells_by_name_with_one_plain_reason() -> None:
    shown = present(ask_from_event(_run_event()))
    assert shown.title == "Run 2 cells in analysis.alknb.py"
    nb = shown.notebook
    assert nb is not None
    assert (nb.lead, nb.file_name, nb.file_path, nb.tail) == (
        "Run 2 cells in",
        "analysis.alknb.py",
        "analysis.alknb.py",
        "",
    )
    # The targets first, in the order the machine listed them; the cell that
    # runs only because they need it is one quiet line, not a row of its own.
    assert [(c.name, c.preview_lines) for c in nb.cells] == [
        ("Cell 2", ["import alkera", "import polars as pl …"]),
        ("Cell 3", ["df.head()"]),
    ]
    assert [c.name for c in nb.related] == ["setup"]
    assert nb.related_line == "Also runs 1 cell they depend on"
    assert nb.reason == "Can't tell if it changes anything"
    # The notebook stands in for the subject: no sentence, no facts, no
    # "Requested by" line under it.
    assert shown.subject is None and shown.facts == [] and shown.missing_subject is None
    assert shown.primary == ["title", "notebook", "decisions"]


@pytest.mark.parametrize("needle", [BOX_FILE, "/opt/", *IDS, "write, not certain", "# "])
def test_a_run_never_shows_an_id_a_machine_path_or_classifier_jargon(needle: str) -> None:
    assert needle not in _shown_text(_run_event())


def test_the_classifier_targets_still_name_tables_a_cell_reaches() -> None:
    event = _run_event()
    event["subject"]["targets"].append({"kind": "table", "name": "orders"})
    assert present(ask_from_event(event)).facts == ["orders"]


@pytest.mark.parametrize(
    ("subject", "reason"),
    [
        pytest.param(
            {"effect": "write", "confidence": "unknown"},
            "Can't tell if it changes anything",
            id="unsure",
        ),
        pytest.param(
            {"effect": "write", "confidence": "heuristic"}, "May change files or data", id="write"
        ),
        pytest.param(
            {"effect": "destroy", "confidence": "exact"}, "May delete files or data", id="destroy"
        ),
        pytest.param(
            {"effect": "egress", "operation": "notebook_install"},
            "Downloads packages from the internet",
            id="install",
        ),
        # Ending someone's run is classed as a destroy, but deletes nothing.
        pytest.param(
            {"effect": "destroy", "operation": "notebook_kernel"},
            "Stops code running in the notebook",
            id="kernel",
        ),
        pytest.param({"effect": "teleport"}, None, id="unknown-effect"),
    ],
)
def test_effect_reason(subject: dict[str, Any], reason: str | None) -> None:
    assert effect_reason(AskSubject.model_validate(subject)) == reason


def test_no_subject_gives_no_reason() -> None:
    assert effect_reason(None) is None


@pytest.mark.parametrize(
    ("code", "lines"),
    [
        pytest.param("x = 1", ["x = 1"], id="one-line"),
        pytest.param("\n\n  \nx = 1\n\t\ny = 2  \n", ["x = 1", "y = 2"], id="blank-lines-skipped"),
        pytest.param("a\nb\nc", ["a", "b …"], id="more-follow"),
        pytest.param("x" * 81, ["x" * 79 + "…"], id="long-line-cut"),
        pytest.param("x" * 80, ["x" * 80], id="exactly-the-width"),
        pytest.param(
            "a\n" + "y" * 90 + "\nc", ["a", "y" * 79 + "…"], id="cut-line-is-the-ellipsis"
        ),
        pytest.param("PASSWORD = 'hunter2'", ["PASSWORD = [redacted]"], id="secret-redacted"),
        pytest.param("", [], id="empty"),
    ],
)
def test_code_preview(code: str, lines: list[str]) -> None:
    assert code_preview(code) == lines


@pytest.mark.parametrize(
    ("targets", "dependencies", "dependents", "line"),
    [
        (1, 0, 0, None),
        (1, 2, 0, "Also runs 2 cells it depends on"),
        (3, 1, 0, "Also runs 1 cell they depend on"),
        (1, 0, 1, "Also runs 1 cell that depends on it"),
        (2, 0, 3, "Also runs 3 cells that depend on them"),
        (1, 1, 2, "Also runs 1 cell it depends on and 2 cells that depend on it"),
    ],
)
def test_related_line(targets: int, dependencies: int, dependents: int, line: str | None) -> None:
    assert related_line(targets, dependencies, dependents) == line


def test_a_run_with_no_named_target_lists_every_cell_as_the_run() -> None:
    """A widget change runs the cells that read it: none was asked for by name,
    so they are the run itself rather than a line about the run."""
    event = _run_event()
    for cell in event["preview"]["notebook"]["cells"]:
        cell["role"] = "dependent"
    nb = present(ask_from_event(event)).notebook
    assert nb is not None
    assert [c.name for c in nb.cells] == ["setup", "Cell 2", "Cell 3"]
    assert nb.related == [] and nb.related_line is None


def test_always_says_what_it_remembers_in_plain_words() -> None:
    event = _run_event(options=[{"option_id": o["optionId"], "name": o["name"]} for o in ALWAYS])
    event["subject"]["confidence"] = "exact"
    shown = present(ask_from_event(event))
    assert shown.always_scope == (
        "Always allow skips this question whenever the agent asks to run 2 cells in "
        "analysis.alknb.py."
    )


def test_no_always_option_means_no_always_line() -> None:
    assert present(ask_from_event(_run_event())).always_scope is None


def test_a_package_install_lists_its_packages() -> None:
    event = _run_event(
        patterns=["Install polars into the environment of analysis.alknb.py"],
        subject={"capability": "notebook", "effect": "egress", "operation": "notebook_install"},
        preview={
            "kind": "notebook",
            "notebook": {
                "lead": "Install polars into the environment of",
                "file_name": "analysis.alknb.py",
                "file_path": "analysis.alknb.py",
                "packages": ["polars"],
            },
        },
    )
    shown = present(ask_from_event(event))
    assert shown.title == "Install polars into the environment of analysis.alknb.py"
    assert shown.notebook is not None and shown.notebook.packages == ["polars"]
    assert shown.notebook.reason == "Downloads packages from the internet"


@pytest.mark.parametrize(
    "notebook",
    [
        pytest.param(None, id="absent"),
        pytest.param({"file_name": "a.alknb.py"}, id="no-lead"),
        pytest.param({"lead": "Run 1 cell in"}, id="no-file"),
        pytest.param("junk", id="wrong-type"),
    ],
)
def test_a_malformed_notebook_ask_falls_back_to_the_generic_card(notebook: Any) -> None:
    event = _run_event()
    event["preview"] = {"kind": "notebook", "title": "t", "notebook": notebook}
    shown = present(ask_from_event(event))
    assert shown.notebook is None
    assert shown.title == "Allow this notebook action?"


def test_a_cell_with_no_name_is_dropped_and_an_unknown_role_is_a_target() -> None:
    event = _run_event()
    event["preview"]["notebook"]["cells"] = [
        {"name": "", "code": "x"},
        {"name": "Cell 1", "code": "y", "role": "martian"},
    ]
    nb = present(ask_from_event(event)).notebook
    assert nb is not None
    assert [(c.name, c.role) for c in nb.cells] == [("Cell 1", "target")]


def test_the_ask_round_trips_through_its_camel_case_json() -> None:
    """The conformance vectors carry the ask as camelCase JSON; reading one
    back gives the same presentation, so the TS twin is held to this one."""
    parsed = ask_from_event(_run_event())
    again = PermissionAsk.model_validate(parsed.to_json())
    assert present(again) == present(parsed)
