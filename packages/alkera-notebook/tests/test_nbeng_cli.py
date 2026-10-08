"""The ``alkera-notebook`` command line: run, check, new, exit codes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.cli.main import main, main_async, parse_script_args
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.ops import DeleteCell, InsertCell
from alkera_notebook.engine import Actor
from alkera_notebook.outputs import snapshot_path
from nbeng_fakes import FAKE_MOUNT

ANN = Actor(kind="person", id="u-ann", display_name="Ann", can_edit=True, can_run=True)


async def write_nb(path: Path, cells: list[str], settings: dict[str, Any] | None = None) -> None:
    store = FileDocumentStore(path.parent, fmt=ModuleFormat())
    await store.create(path.name, [InsertCell(source=c) for c in cells], settings or {}, ANN)
    await store.close()


async def cli(*argv: str, **kw: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = await main_async(list(argv), fmt=ModuleFormat(), mount=FAKE_MOUNT, out=out, **kw)
    return code, out.getvalue()


async def test_run_prints_each_cell_and_writes_the_snapshot(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 40", "x + 2"])
    code, text = await cli("run", str(nb))
    assert code == 0, text
    lines = text.splitlines()
    assert lines[0].startswith("fresh") and lines[1].startswith("fresh")
    assert lines[1].endswith("42")
    saved = json.loads(snapshot_path(nb).read_text())
    assert len(saved["cells"]) == 2


async def test_run_exits_one_when_a_cell_fails(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "raise ValueError('boom')"])
    code, text = await cli("run", str(nb))
    assert code == 1
    assert "error" in text and "ValueError: boom" in text


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["run", "missing.alknb.py"], id="missing-file"),
        pytest.param([], id="no-command"),
        pytest.param(["frobnicate"], id="unknown-command"),
        pytest.param(["run", "{nb}", "--target", "some"], id="bad-target"),
        pytest.param(["run", "{nb}", "--compare"], id="compare-without-clean"),
        pytest.param(["check", "{nb}", "--", "--x", "1"], id="args-for-check"),
        pytest.param(["run", "{nb}", "--python", "/no/such/python"], id="missing-python"),
        pytest.param(["run", "{nb}", "--", "positional"], id="bad-script-args"),
        pytest.param(["run", "{nb}", "--known", "/no/such/known.py"], id="missing-known"),
        pytest.param(["new", "/no/such/folder/nb.alknb.py"], id="new-in-missing-folder"),
    ],
)
async def test_usage_errors_exit_two(tmp_path: Path, argv: list[str]) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1"])
    code, text = await cli(*[a.replace("{nb}", str(nb)) for a in argv])
    assert code == 2, text
    assert text.startswith("alkera-notebook: ")


async def test_run_stale_target_runs_everything_in_a_fresh_kernel(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "x"])
    code, text = await cli("run", str(nb), "--target", "stale")
    assert code == 0, text
    assert text.splitlines()[1].endswith("1")


async def test_script_args_reach_the_kernel(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["sorted(alkera_args.items())"])
    code, text = await cli("run", str(nb), "--", "--region", "eu", "--n=3", "--dry")
    assert code == 0, text
    assert text.splitlines()[0].endswith("[('dry', 'true'), ('n', '3'), ('region', 'eu')]")


async def test_without_script_args_the_kernel_sees_none(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["len(alkera_args)"])
    code, text = await cli("run", str(nb))
    assert code == 0 and text.splitlines()[0].endswith("0")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(["--a", "1", "--b", "two"], {"a": "1", "b": "two"}, id="pairs"),
        pytest.param(["--k=v=w"], {"k": "v=w"}, id="equals"),
        pytest.param(["--flag", "--x", "1"], {"flag": "true", "x": "1"}, id="flag"),
        pytest.param([], {}, id="empty"),
    ],
)
def test_parse_script_args(args: list[str], expected: dict[str, str]) -> None:
    assert parse_script_args(args) == expected


async def test_known_file_keeps_cell_ids_when_keywords_are_lost(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 7", "x * 6"])
    old = tmp_path / "old.alknb.py"
    old.write_text(nb.read_text())
    ids = [c.id for c in ModuleFormat().read(nb.read_text()).cells]
    stripped = nb.read_text()
    for cid in ids:
        stripped = stripped.replace(f'alkera_id="{cid}"', "")
    nb.write_text(stripped.replace("(, ", "(").replace("()", "()"))
    code, text = await cli("run", str(nb), "--known", str(old))
    assert code == 0, text
    saved = json.loads(snapshot_path(nb).read_text())
    assert [c["id"] for c in saved["cells"]] == ids


async def test_check_reports_a_duplicate_definition(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "x = 2"])
    code, text = await cli("check", str(nb))
    assert code == 1
    assert text.count("multiple_definitions") == 2


async def test_check_reports_a_cycle(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["a = b + 1", "b = a + 1"])
    code, text = await cli("check", str(nb))
    assert code == 1
    assert "cycle: " in text


async def test_check_names_cells_by_position_never_by_id(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["a = b + 1", "b = a + 1", "x = 1", "x = 2"])
    ids = list(
        (await FileDocumentStore(nb.parent, fmt=ModuleFormat()).load(nb.name)).document.order
    )
    code, text = await cli("check", str(nb))
    assert code == 1
    assert "Cell 3: " in text and "Cell 4: " in text
    assert any(
        line.startswith("cycle: Cell ") and " -> Cell " in line for line in text.splitlines()
    )
    for cid in ids:
        assert cid not in text


async def test_check_reports_a_file_that_is_not_a_notebook(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    nb.write_text("import marimo\napp = marimo.App(\n")
    code, text = await cli("check", str(nb))
    assert code == 1 and "not_a_notebook" in text


async def test_check_reports_a_cell_with_a_syntax_error(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "y = ("])
    code, text = await cli("check", str(nb))
    assert code == 1 and "syntax_error" in text


async def test_check_passes_a_clean_notebook(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1", "y = x"])
    code, text = await cli("check", str(nb))
    assert code == 0 and "2 cells, no problems" in text


async def test_new_writes_the_setup_import_and_one_empty_cell_with_the_header(
    tmp_path: Path,
) -> None:
    nb = tmp_path / "fresh.alknb.py"
    code, text = await cli("new", str(nb))
    assert code == 0, text
    ir = ModuleFormat().read(nb.read_text())
    # The setup block importing the runtime module, then an empty cell (the
    # stand-in format writes an empty body as `pass`).
    setup, empty = ir.cells
    assert (setup.kind, setup.code) == ("setup", "import alkera")
    assert empty.kind == "python" and empty.code in ("", "pass")
    assert "with app.setup(" in nb.read_text()
    assert "# >>> alkera" in nb.read_text()
    assert ir.settings.get("dataframe") == "polars"


async def test_new_writes_the_text_every_notebook_creator_is_held_to(tmp_path: Path) -> None:
    """The web client writes a new notebook without this package; both are
    held to the one recorded text (ids aside, which are minted per file)."""
    vector = json.loads(
        (Path(__file__).parent / "vectors" / "new_notebook.json").read_text("utf-8")
    )
    nb = tmp_path / "fresh.alknb.py"
    code, text = await cli("new", str(nb))
    assert code == 0, text
    written = nb.read_text(encoding="utf-8")
    setup, empty = ModuleFormat().read(written).cells
    assert setup.id != empty.id
    assert (
        written.replace(setup.id, vector["setup_id"]).replace(empty.id, vector["cell_id"])
        == vector["text"]
    )


async def test_new_refuses_an_existing_file(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    nb.write_text("keep me")
    code, text = await cli("new", str(nb))
    assert code == 2 and "already exists" in text
    assert nb.read_text() == "keep me"


async def test_run_cuts_a_long_output_line(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["'y' * 500"])
    code, text = await cli("run", str(nb))
    assert code == 0, text
    line = text.splitlines()[0]
    assert line.endswith("...") and "y" * 190 in line and "y" * 300 not in line


async def test_compare_reports_a_cell_removed_since_the_snapshot(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 1\nx", "y = 2\ny"])
    assert (await cli("run", str(nb)))[0] == 0
    store = FileDocumentStore(nb.parent, fmt=ModuleFormat())
    gone = (await store.load(nb.name)).document.order[1]
    await store.apply(nb.name, [DeleteCell(cell_id=gone)], None, ANN, None)
    await store.close()
    code, text = await cli("run", str(nb), "--clean", "--compare")
    # Named by the place it had in the snapshot, never
    # by its id: the id means nothing to the person reading the report.
    assert "compare missing  Cell 2" in text
    assert gone not in text
    assert code == 1


async def test_run_falls_back_visibly_when_the_recorded_env_is_not_usable(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    await write_nb(nb, ["x = 3\nx"], settings={"env": "./no-such-env"})
    code, text = await cli("run", str(nb))
    assert code == 0, text
    assert "env './no-such-env'" in text and sys.executable in text
    assert text.splitlines()[-1].endswith("3")


def test_sync_main_runs_check_with_the_default_format(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    nb.write_text("print('not a notebook')\n")
    out = io.StringIO()
    assert main(["check", str(nb)], out=out) == 1
    assert "not_a_notebook" in out.getvalue()


def test_module_entry_point_runs(tmp_path: Path) -> None:
    nb = tmp_path / "fresh.alknb.py"
    done = subprocess.run(
        [sys.executable, "-m", "alkera_notebook.cli", "new", str(nb)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert nb.is_file() and "# >>> alkera" in nb.read_text()
