"""Reproducibility: identical plans for identical inputs, and
``alkera-notebook run --clean --compare`` telling nondeterminism apart from
changed external data."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.cli.main import main_async
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.ops import InsertCell
from alkera_notebook.engine import AllTarget, CellsTarget
from nbeng_fakes import FAKE_MOUNT, duck_provider, write_duck
from nbeng_harness import ANN, engine_for


async def write_nb(path: Path, cells: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    store = FileDocumentStore(path.parent, fmt=ModuleFormat())
    await store.create(path.name, [InsertCell(source=c) for c in cells], {}, ANN)
    await store.close()


async def cli(*argv: str, sql: Any = (), **kw: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = await main_async(
        list(argv), fmt=ModuleFormat(), mount=FAKE_MOUNT, out=out, sql=sql, **kw
    )
    return code, out.getvalue()


def verdicts(text: str) -> dict[int, tuple[str, bool]]:
    """Per compare line in order: (verdict, external)."""
    rows = [line.split() for line in text.splitlines() if line.startswith("compare ")]
    return {i: (r[1], r[-1] == "(external)") for i, r in enumerate(rows)}


CELLS = ["x = 3", "y = x * 2\ny", "z = [y] * 2\nz"]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(CellsTarget(ids=["$2"]), id="repro.identical_plans"),
        pytest.param(AllTarget(), id="repro.identical_plans_run_all"),
    ],
)
async def test_identical_inputs_give_identical_plans(tmp_path: Path, target: Any) -> None:
    nb = tmp_path / "a" / "nb.alknb.py"
    await write_nb(nb, CELLS)
    (tmp_path / "b").mkdir()
    shutil.copy(nb, tmp_path / "b" / "nb.alknb.py")
    plans = []
    codes = []
    for name in ("a", "b"):
        async with engine_for(tmp_path, name=name) as engine:
            session = await engine.open("nb.alknb.py")
            client = session.attach(ANN)
            ids = [c.id for c in (await client.read()).cells]
            tgt = CellsTarget(ids=[ids[2]]) if isinstance(target, CellsTarget) else target
            record = await (await client.run(tgt)).wait(30)
            assert record.status == "ok", record
            plans.append([(p.cell_id, p.name, p.reason) for p in record.plan])
            codes.append({cid: rt.submitted for cid, rt in session.runtime.cells.items()})
    assert plans[0] == plans[1]
    assert codes[0] == codes[1]
    assert all(code is not None for code in codes[0].values())


async def test_compare_passes_on_a_deterministic_notebook(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, [*CELLS, "print('hello')"])
    code, text = await cli("run", str(nb))
    assert code == 0, text
    code, text = await cli("run", str(nb), "--clean", "--compare")
    assert code == 0, text
    assert [v for v, _ in verdicts(text).values()] == ["same"] * 4


async def test_compare_reports_a_nondeterministic_cell(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "import random\nprint(random.random())"])
    assert (await cli("run", str(nb)))[0] == 0
    code, text = await cli("run", str(nb), "--clean", "--compare")
    assert code == 1, text
    assert verdicts(text) == {0: ("same", False), 1: ("changed", False)}


async def test_compare_marks_a_sql_cell_external_and_passes_when_only_it_changed(
    tmp_path: Path,
) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "rows = sql('select v from t')\nrows"])
    provider = duck_provider(tmp_path / "db", [1])
    assert (await cli("run", str(nb), sql=[provider]))[0] == 0
    write_duck(tmp_path / "db" / "warehouse.duckdb", [2])  # the data changed between runs
    code, text = await cli("run", str(nb), "--clean", "--compare", sql=[provider])
    assert code == 0, text
    assert verdicts(text) == {0: ("same", False), 1: ("changed", True)}
    assert [conn for conn, _, _ in provider.executed] == ["Warehouse", "Warehouse"]


async def test_compare_fails_when_a_deterministic_cell_changed_beside_sql(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["rows = sql('select v from t')\nrows", "import random\nrandom.random()"])
    provider = duck_provider(tmp_path / "db", [1])
    assert (await cli("run", str(nb), sql=[provider]))[0] == 0
    code, text = await cli("run", str(nb), "--clean", "--compare", sql=[provider])
    assert code == 1, text
    assert verdicts(text)[1] == ("changed", False)


async def test_compare_reports_cells_new_since_the_snapshot(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1"])
    code, text = await cli("run", str(nb), "--clean", "--compare")
    # No snapshot yet: every cell is new, which is not a match.
    assert code == 1, text
    assert verdicts(text) == {0: ("new", False)}
