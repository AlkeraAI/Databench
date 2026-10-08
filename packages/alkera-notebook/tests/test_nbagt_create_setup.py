"""What ``notebook.create`` makes a notebook from: the cells asked for, with a
setup cell importing the runtime module."""

from __future__ import annotations

import pytest
from alkera_notebook.tools.functions import new_notebook_cells
from alkera_notebook.tools.models import InsertCellOp


def _shape(cells: list[InsertCellOp]) -> list[tuple[str, str]]:
    return [(cell.kind, cell.source) for cell in cells]


@pytest.mark.parametrize(
    ("given", "made"),
    [
        pytest.param([], [("setup", "import alkera")], id="no-cells"),
        pytest.param(
            [("python", "x = 1"), ("markdown", "# Title")],
            [("setup", "import alkera"), ("python", "x = 1"), ("markdown", "# Title")],
            id="no-setup-cell-given",
        ),
        pytest.param(
            [("setup", "import os"), ("python", "x = 1")],
            [("setup", "import alkera\nimport os"), ("python", "x = 1")],
            id="the-import-goes-first-in-the-setup-given",
        ),
        pytest.param(
            [("setup", ""), ("python", "x = 1")],
            [("setup", "import alkera"), ("python", "x = 1")],
            id="an-empty-setup-given",
        ),
        pytest.param(
            [("setup", "import os\nimport alkera"), ("python", "x = 1")],
            [("setup", "import os\nimport alkera"), ("python", "x = 1")],
            id="a-setup-that-imports-it-is-kept",
        ),
        pytest.param(
            [("setup", "import alkera as ak")],
            [("setup", "import alkera\nimport alkera as ak")],
            id="another-name-for-it-does-not-bind-alkera",
        ),
        pytest.param(
            [("setup", "import alkera.ui")],
            [("setup", "import alkera\nimport alkera.ui")],
            id="a-submodule-import-is-not-the-module-line",
        ),
        pytest.param(
            [("python", "import alkera"), ("setup", "import os")],
            [("python", "import alkera"), ("setup", "import alkera\nimport os")],
            id="only-the-setup-cell-is-looked-at",
        ),
    ],
)
def test_a_created_notebook_has_a_setup_cell_importing_the_runtime_module(
    given: list[tuple[str, str]], made: list[tuple[str, str]]
) -> None:
    cells = [InsertCellOp(kind=kind, source=source) for kind, source in given]
    assert _shape(new_notebook_cells(cells)) == made


def test_the_cells_given_are_not_changed_in_place() -> None:
    given = [InsertCellOp(kind="setup", source="import os", name="setup")]
    made = new_notebook_cells(given)
    assert given[0].source == "import os"
    assert (made[0].name, made[0].source) == ("setup", "import alkera\nimport os")


def test_the_kernel_and_the_format_name_the_same_runtime_module() -> None:
    """The kernel cannot import the format (it runs in the person's
    environment on the standard library alone), so each spells the name; this
    holds them to the one value."""
    from _alkera_kernel import runtime
    from alkera_notebook.format import RUNTIME_IMPORT, RUNTIME_MODULE

    assert runtime.RUNTIME_MODULE == RUNTIME_MODULE == "alkera"
    assert RUNTIME_IMPORT == "import alkera"
