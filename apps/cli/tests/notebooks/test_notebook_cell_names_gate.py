"""The source gate: no module a person reads names a cell by its internal id.

``alkera_notebook.cell_names.cell_display_name`` is the one owner of how a cell
is named for a person (its name, else "Cell N"). This fails when a
person-facing module formats a cell id into a string itself, or rebuilds the
"name, else the id" fallback the owner replaced. The behaviour behind it is
pinned in ``test_nbagt_notebook_gates.py``: no notebook prompt shows an id.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]

#: The modules whose strings a person reads: permission prompts, run reports,
#: the notebook command line, and the box's notebook gate. Agent-facing
#: payloads and errors about an id the agent itself passed keep the id.
PERSON_FACING = (
    "packages/alkera-notebook/alkera_notebook/tools/gates.py",
    "packages/alkera-notebook/alkera_notebook/tools/catalog.py",
    "packages/alkera-notebook/alkera_notebook/cli/main.py",
    "apps/cli/alkera_cli/notebooks/tools.py",
    "packages/api-core/alkera_core/permission_presentation/present.py",
)

#: A cell id interpolated into an f-string (``{step.cell_id}``, ``{cell.id}``,
#: ``{cid}``), or the "its name, else its id" fallback
#: (``name if name != "_" else cell_id``).
_ID_FORMAT = re.compile(
    r"\{(?:\w+\.)*(?:cell_id|cid)(?:![rsa])?(?::[^}\s]*)?\}"
    r"|\{(?:cell|c|step|s)\.id(?:![rsa])?(?::[^}\s]*)?\}"
)
_NAME_ELSE_ID = re.compile(r"""!=\s*["']_["']\s+else\b""")


@pytest.mark.parametrize("relative", PERSON_FACING)
def test_no_person_facing_module_formats_a_cell_id(relative: str) -> None:
    source = (_REPO / relative).read_text(encoding="utf-8")
    offenders = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), 1)
        if _ID_FORMAT.search(line) or _NAME_ELSE_ID.search(line)
    ]
    assert offenders == [], "name the cell with cell_display_name:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "line",
    [
        'lines.append(f"# {step.name} ({step.cell_id}) runs as {step.reason}")',
        'label = cell.name if cell.name != "_" else cell.id',
        'print(f"cell {cid}: broken")',
        'f"{c.id!r} failed"',
    ],
)
def test_the_gate_recognises_the_shapes_it_forbids(line: str) -> None:
    assert _ID_FORMAT.search(line) or _NAME_ELSE_ID.search(line)


@pytest.mark.parametrize(
    "line",
    [
        'print(f"{labels[cid]}: {err.label()}", file=out)',
        'f"{actor.id} ran it"',
        "label = cell_display_name(cell.name, cell.index)",
    ],
)
def test_the_gate_lets_a_display_name_through(line: str) -> None:
    assert not (_ID_FORMAT.search(line) or _NAME_ELSE_ID.search(line))
