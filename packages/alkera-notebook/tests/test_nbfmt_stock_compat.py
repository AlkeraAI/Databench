"""Canonical files against stock marimo.

Stock marimo is pinned at the fork's base version as a dev dependency (the fork
is the private ``alkera_notebook._marimo``, so the two never collide). Every
corpus case must parse in stock marimo to the same cell codes in the same
order, run as a script where it needs no external system, and survive a stock
save: reading the saved file with the previous version as the known state keeps
the id of every cell whose code is unchanged and never gives a cell an id whose
known code is less than 60% alike.

Older marimo releases run under the opt-in ``marimo_compat`` marker, each in a
throwaway environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_notebook import _marimo as fork
from alkera_notebook.format import CellIR, NotebookIR, normalize_code, read
from alkera_notebook.format.ids import SIMILARITY_THRESHOLD, similarity

marimo = pytest.importorskip("marimo", reason="stock marimo is a dev dependency")

CORPUS = Path(__file__).resolve().parent / "format_corpus"
CASES = sorted(p for p in CORPUS.glob("v1.*/*") if (p / "notebook.alknb.py").is_file())


def _case_id(case: Path) -> str:
    return f"{case.parent.name}/{case.name}"


def _text(case: Path) -> str:
    # What stock marimo sees when it opens the file: universal newlines.
    return (case / "notebook.alknb.py").read_text(encoding="utf-8")


def _expected(case: Path) -> dict[str, object]:
    return json.loads((case / "expected.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _is_marimo_cell(cell: CellIR) -> bool:
    """A cell stock marimo also sees as a cell (not a kept stray statement)."""
    return not cell.meta.get("stray", False)


def _applicable(case: Path) -> bool:
    limit = _expected(case).get("max_python")
    return limit is None or sys.version_info[:2] <= tuple(int(p) for p in str(limit).split("."))


def test_stock_marimo_is_the_forks_base_version() -> None:
    assert marimo.__version__ == fork.__version__


def test_the_fork_does_not_shadow_stock_marimo() -> None:
    assert Path(marimo.__file__).resolve().parent != Path(fork.__file__).resolve().parent
    assert fork.__name__ == "alkera_notebook._marimo"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_stock_marimo_parses_the_same_codes_in_order(case: Path) -> None:
    from marimo._ast.parse import parse_notebook

    if not _applicable(case):
        pytest.skip("case pins an older interpreter's behaviour")
    text = _text(case)
    ours = [c.code for c in read(text).cells if _is_marimo_cell(c)]
    stock = parse_notebook(text.removeprefix("\ufeff"))
    stock_codes = [c.code for c in stock.cells] if stock is not None else []
    divergence = STOCK_DIVERGENCES.get(case.name)
    if divergence is None:
        assert stock_codes == ours
    else:
        # The fork fixes a stock marimo bug here: the codes differ, and only by it.
        assert stock_codes != ours
        assert stock_codes == [divergence(code) for code in ours]


def _without_last_line_comment(code: str) -> str:
    head, _, last = code.rpartition("\n")
    last = re.sub(r"\s+#[^'\"]*$", "", last)
    return f"{head}\n{last}" if head else last


# Corpus cases where stock marimo reads a different code than the fork, because
# the fork fixes a stock bug (see vendor/marimo/README.alkera.md), mapped to
# what stock marimo makes of each of the fork's codes.
STOCK_DIVERGENCES: dict[str, Callable[[str], str]] = {
    # trailing-comment: stock cuts a comment after a block's last statement,
    # except in a cell with a return value.
    "trailing-comments": lambda code: (
        code if code.startswith("y = ") else _without_last_line_comment(code)
    ),
    # empty-setup: stock reads the `pass` an empty setup cell is written with.
    "empty-setup": lambda code: code or "pass",
}


# Cases that need nothing outside the standard library and the notebook itself,
# and contain no deliberate errors.
SELF_CONTAINED = {
    "cycle": 1,  # a cycle is a marimo error at run time
    "multiple-definitions": 1,
    "setup-absent": 0,
    "ids-identical-unkeyed": 1,  # both cells define x
    "ids-duplicate-no-known": 0,
    "ids-duplicate-with-known": 1,  # both copies define x
    "ids-malformed": 0,
    "fence-misplaced": 0,
    "settings-missing": 0,
    "settings-invalid": 0,
    "settings-not-toml": 0,
    "newer-major": 0,
    "newer-minor": 0,
    "tabs": 0,
    "crlf": 0,
    "no-trailing-newline": 0,
    "marimo-alias-and-cell-forms": 0,
    "alkera-keywords": 0,
}


@pytest.mark.parametrize(
    ("name", "exit_code"),
    sorted(SELF_CONTAINED.items()),
    ids=sorted(SELF_CONTAINED),
)
def test_canonical_files_run_as_scripts(name: str, exit_code: int, tmp_path: Path) -> None:
    source = CORPUS / "v1.0" / name / "notebook.alknb.py"
    target = tmp_path / "notebook.alknb.py"
    shutil.copyfile(source, target)
    result = subprocess.run(
        [sys.executable, str(target)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        env={**os.environ, "MARIMO_SKIP_UPDATE_CHECK": "1"},
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stderr[-2000:]


def _stock_save(path: Path) -> str:
    """What stock marimo writes back when its editor saves ``path``."""
    from marimo._ast.codegen import generate_filecontents
    from marimo._ast.load import load_app

    app = load_app(str(path))
    assert app is not None
    manager = app._cell_manager
    return generate_filecontents(
        list(manager.codes()),
        list(manager.names()),
        list(manager.configs()),
        app._config,
        app._header,
    )


def _stock_savable(case: Path) -> bool:
    ir = read(_text(case))
    return (
        _applicable(case)
        # Stock marimo reads files as plain UTF-8 and cannot load one that
        # starts with a byte order mark (Python itself runs it).
        and not _text(case).startswith("\ufeff")
        and bool(ir.cells)
        and ir.read_only_reason is None
        and all(c.kind != "unparsable" for c in ir.cells)
    )


SAVABLE = [c for c in CASES if _stock_savable(c)]


@pytest.mark.parametrize("case", SAVABLE, ids=_case_id)
def test_a_stock_save_keeps_ids_through_the_known_state(case: Path, tmp_path: Path) -> None:
    path = tmp_path / "notebook.alknb.py"
    path.write_text(_text(case), encoding="utf-8")
    before: NotebookIR = read(_text(case))
    known = {c.id: normalize_code(c.code) for c in before.cells}
    saved = _stock_save(path)
    assert "alkera_id" not in saved  # stock marimo drops the keywords
    after = read(saved, known=known)
    unchanged = {normalize_code(c.code): c.id for c in before.cells}
    for cell in after.cells:
        code = normalize_code(cell.code)
        if cell.id in known:
            assert similarity(code, known[cell.id]) >= SIMILARITY_THRESHOLD
        if code in unchanged and list(known.values()).count(code) == 1:
            assert cell.id == unchanged[code], cell


def test_a_stock_save_keeps_the_settings_fence_verbatim(tmp_path: Path) -> None:
    case = CORPUS / "v1.0" / "settings-every-key"
    path = tmp_path / "notebook.alknb.py"
    path.write_text(_text(case), encoding="utf-8")
    fence = _text(case).split("\n\n")[0]
    assert fence in _stock_save(path)
    assert read(_stock_save(path)).settings == read(_text(case)).settings


def test_a_stock_save_never_reassigns_a_rewritten_cell(tmp_path: Path) -> None:
    ir = read(_text(CORPUS / "v1.0" / "setup-absent"))
    known = {c.id: normalize_code(c.code) for c in ir.cells}
    path = tmp_path / "nb.py"
    rewritten = _text(CORPUS / "v1.0" / "setup-absent").replace(
        "y = x + 1\n    y", "import json\n    payload = json.dumps({'k': [1, 2, 3]})"
    )
    path.write_text(rewritten, encoding="utf-8")
    after = read(_stock_save(path), known=known)
    assert after.cells[0].id == ir.cells[0].id
    assert after.cells[1].id not in known
    assert after.cells[1].resolution == "minted"


# ---- older releases (opt in) ---------------------------------------------------------

# The previous two minor releases (format spec, compatibility matrix).
OLDER = ["0.24.2", "0.23.16"]

_PARSE = """
import json, sys
from pathlib import Path
from marimo._ast.parse import parse_notebook
out = {}
for path in sys.argv[1:]:
    nb = parse_notebook(Path(path).read_text(encoding="utf-8").removeprefix("\\ufeff"))
    out[path] = [c.code for c in nb.cells] if nb is not None else []
print(json.dumps(out))
"""


@pytest.mark.marimo_compat
@pytest.mark.parametrize("version", OLDER)
def test_older_marimo_parses_the_same_codes(version: str, tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env_dir = tmp_path / "venv"
    subprocess.run(
        [uv, "venv", "-q", "-p", f"{sys.version_info[0]}.{sys.version_info[1]}", str(env_dir)],
        check=True,
    )
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(python), f"marimo=={version}"], check=True
    )
    cases = [c for c in CASES if _applicable(c)]
    result = subprocess.run(
        [str(python), "-c", _PARSE, *[str(c / "notebook.alknb.py") for c in cases]],
        capture_output=True,
        text=True,
        check=True,
    )
    codes = json.loads(result.stdout)
    for case in cases:
        ours = [c.code for c in read(_text(case)).cells if _is_marimo_cell(c)]
        assert codes[str(case / "notebook.alknb.py")] == ours, case.name


# ---- the version floor (opt in) -----------------------------------------------------

_FLOORS = [
    pytest.param("0.14.17", "kinds-all", True, id="setup-floor"),
    pytest.param("0.14.16", "kinds-all", False, id="below-setup-floor"),
    pytest.param("0.11.3", "setup-absent", True, id="cell-floor"),
]


@pytest.mark.marimo_compat
@pytest.mark.parametrize(("version", "case", "runs"), _FLOORS)
def test_the_stock_marimo_version_floor(
    version: str, case: str, runs: bool, tmp_path: Path
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env_dir = tmp_path / "venv"
    subprocess.run([uv, "venv", "-q", "-p", "3.12", str(env_dir)], check=True)
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(python), f"marimo=={version}"], check=True
    )
    target = tmp_path / "notebook.alknb.py"
    text = _text(CORPUS / "v1.0" / case)
    # The floor is about marimo, not about `alkera`, which old environments lack:
    # cells that call it are replaced by an equivalent that needs nothing.
    target.write_text(text.replace("import alkera\n", "import os\n"), encoding="utf-8")
    result = subprocess.run(
        [str(python), str(target)], capture_output=True, text=True, cwd=tmp_path, timeout=300
    )
    if runs:
        assert "_SetupContext" not in result.stderr
        assert "has no attribute" not in result.stderr
    else:
        assert "'_SetupContext' object is not callable" in result.stderr
