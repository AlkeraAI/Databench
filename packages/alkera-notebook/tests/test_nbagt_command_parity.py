"""The gate: every notebook action a person has, an agent has too.

The editor's command table (``packages/notebook-ui/src/editor/commands.ts``)
is read as text, so a command added there without an entry in
:data:`~alkera_notebook.tools.parity.COMMAND_PARITY` fails here before it
ships. Each mapped call must be one its tool accepts, and an action that needs
the right to edit or run must never be reachable by a call the gate treats as
a read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alkera_notebook.format.settings import NOTEBOOK_SETTINGS
from alkera_notebook.tools import TOOLS, GateEffect, gate_effect
from alkera_notebook.tools.models import NotebookSettingsInput
from alkera_notebook.tools.parity import COMMAND_PARITY, SURFACE_PARITY, AgentCall, Exempt

COMMANDS_TS = Path(__file__).resolve().parents[2] / "notebook-ui" / "src" / "editor" / "commands.ts"
PATH = "nb.alknb.py"

#: Every person's action an agent has no call for. This set may only shrink:
#: a new exemption is a decision, made here in review, never by default.
ALLOWED_EXEMPTIONS = frozenset(
    {
        "cell.copy_link",
        "output.toggle_collapse",
        "nav.previous",
        "nav.next",
        "mode.edit",
        "mode.command",
        "doc.undo",
        "doc.redo",
        "find.open",
    }
)


def _source() -> str:
    return COMMANDS_TS.read_text(encoding="utf-8")


def command_ids(source: str) -> set[str]:
    """The ``CommandId`` union's members."""
    union = re.search(r"export type CommandId\s*=(.*?);", source, re.DOTALL)
    assert union is not None, "commands.ts has no CommandId union"
    return set(re.findall(r'"([a-z_]+\.[a-z_]+)"', union.group(1)))


def command_rights(source: str) -> dict[str, set[str]]:
    """Each command in the ``COMMANDS`` table, with the rights it needs
    (``run``, ``edit``)."""
    table = re.search(r"export const COMMANDS[^=]*=\s*\[(.*?)\n\];", source, re.DOTALL)
    assert table is not None, "commands.ts has no COMMANDS table"
    rights: dict[str, set[str]] = {}
    for entry in re.findall(r"\{[^{}]*\}", table.group(1)):
        found = re.search(r'id:\s*"([^"]+)"', entry)
        assert found is not None, entry
        rights[found.group(1)] = {r for r in ("run", "edit") if re.search(rf"\b{r}:\s*true", entry)}
    return rights


def _calls() -> list[tuple[str, AgentCall]]:
    out: list[tuple[str, AgentCall]] = []
    for name, parity in {**COMMAND_PARITY, **SURFACE_PARITY}.items():
        if isinstance(parity, tuple):
            out.extend((name, call) for call in parity)
    return out


def test_the_command_table_is_read() -> None:
    # The parsers find what the file holds, so an empty parse cannot pass the
    # gate below by finding nothing to check.
    ids = command_ids(_source())
    rights = command_rights(_source())
    assert {"output.clear", "run.all", "kernel.restart_run_all", "nav.next"} <= ids
    assert rights["output.clear"] == {"run"}
    assert rights["run.insert_below"] == {"run", "edit"}
    assert rights["nav.next"] == set()
    assert set(rights) == ids


def test_every_command_a_person_has_maps_to_an_agent_call_or_an_exemption() -> None:
    ids = command_ids(_source())
    missing = sorted(ids - set(COMMAND_PARITY))
    assert missing == [], f"commands with no agent path and no exemption: {missing}"
    stale = sorted(set(COMMAND_PARITY) - ids)
    assert stale == [], f"parity entries for commands that no longer exist: {stale}"


def test_exemptions_only_shrink() -> None:
    exempt = {name for name, p in COMMAND_PARITY.items() if isinstance(p, Exempt)}
    exempt |= {name for name, p in SURFACE_PARITY.items() if isinstance(p, Exempt)}
    grown = sorted(exempt - ALLOWED_EXEMPTIONS)
    assert grown == [], f"new exemptions need a review decision: {grown}"
    assert all(len(p.reason) > 20 for p in COMMAND_PARITY.values() if isinstance(p, Exempt))


@pytest.mark.parametrize(("name", "call"), _calls(), ids=lambda v: str(getattr(v, "tool", v)))
def test_every_mapped_call_is_one_its_tool_accepts(name: str, call: AgentCall) -> None:
    assert call.tool in TOOLS, f"{name} maps to unknown tool {call.tool}"
    TOOLS[call.tool].input.model_validate({"path": PATH, **call.args})


def test_an_action_that_needs_rights_is_never_an_agent_read() -> None:
    rights = command_rights(_source())
    reads: list[str] = []
    for name, parity in COMMAND_PARITY.items():
        if not rights.get(name) or not isinstance(parity, tuple):
            continue
        for call in parity:
            args = TOOLS[call.tool].input.model_validate({"path": PATH, **call.args})
            if gate_effect(call.tool, args) is GateEffect.READ:
                reads.append(f"{name} -> {call.tool}")
    assert reads == []


def test_every_notebook_setting_a_person_can_change_the_agent_can_change() -> None:
    names = {spec.name for spec in NOTEBOOK_SETTINGS}
    assert names <= set(NotebookSettingsInput.model_fields) - {"path"}
    assert {f"settings.{n}" for n in names} <= set(SURFACE_PARITY)
