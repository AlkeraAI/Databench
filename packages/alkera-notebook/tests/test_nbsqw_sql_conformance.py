"""What the engine writes, read back by ``alkera._sql`` in real environments:
Polars with no pyarrow, pandas with pyarrow, and the oldest supported
versions of each, on the oldest supported Python.

Each environment is made with uv; one that cannot be made here (offline,
no wheel) is skipped and says why."""

from __future__ import annotations

import decimal
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from alkera_notebook.sql.encode import ArrowSink, encode_rows_json

pytestmark = pytest.mark.xdist_group("nbsqw_sql_conformance")

HERE = Path(__file__).resolve().parent
ALKERA_PY = HERE.parents[1] / "alkera-py"
sys.path.insert(0, str(ALKERA_PY / "tests"))

from alkera_py_floor import floor_version  # noqa: E402

FLOOR = floor_version()

ENVIRONMENTS = [
    pytest.param("polars", "3.13", ["polars"], id="polars-latest-no-pyarrow"),
    pytest.param("polars", FLOOR, ["polars==1.0.0"], id="polars-oldest-no-pyarrow"),
    pytest.param("pandas", "3.13", ["pandas", "pyarrow"], id="pandas-pyarrow-latest"),
    pytest.param(
        "pandas", FLOOR, ["pandas<2.2", "numpy<2", "pyarrow==14.0.1"], id="pandas-pyarrow-oldest"
    ),
]


def table() -> pa.Table:
    ree = pa.RunEndEncodedArray.from_arrays(
        pa.array([2, 3], pa.int32()), pa.array(["east", "west"])
    )
    return pa.table(
        {
            "id": pa.array([1, 2, None], pa.int64()),
            "amount": pa.array([1.5, None, -2.25]),
            "name": pa.array(["a", None, "ü"], pa.string_view()),
            "region": ree,
            "price": pa.array(
                [decimal.Decimal("1.00"), decimal.Decimal("2.00"), decimal.Decimal("3.00")],
                pa.decimal128(10, 2),
            ),
            "day": pa.array([18000, 18001, None], pa.date32()),
        }
    )


EXPECTED_ROWS = [
    [1, 1.5, "a", "east", "1.00", "2019-04-14"],
    [2, None, None, "east", "2.00", "2019-04-15"],
    [None, -2.25, "ü", "west", "3.00", None],
]


@pytest.fixture(scope="module")
def vectors(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("vectors")
    t = table()
    stream = ArrowSink(t.schema, data_dir=None)
    for batch in t.to_batches():
        stream.write(batch)
    inline = stream.finish()
    assert inline.inline is not None
    (out / "inline.stream").write_bytes(inline.inline)
    spill = ArrowSink(t.schema, data_dir=out, inline_limit=1, file_stem="spilled")
    for batch in t.to_batches():
        spill.write(batch)
    assert spill.finish().file_name == "spilled.arrow"
    rows = encode_rows_json(t.schema, t.to_batches())
    assert rows.inline is not None
    (out / "rows.json").write_bytes(rows.inline)
    return out


def make_env(root: Path, python: str, requirements: list[str]) -> Path:
    env = root / "venv"
    made = subprocess.run(
        ["uv", "venv", "-q", "--python", python, str(env)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip(f"no Python {python} here: {made.stderr.strip()[:200]}")
    installed = subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(env / "bin" / "python"), *requirements],
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        pytest.skip(f"could not install {requirements}: {installed.stderr.strip()[:200]}")
    return env / "bin" / "python"


@pytest.mark.parametrize(("library", "python", "requirements"), ENVIRONMENTS)
def test_every_codec_decodes(
    tmp_path: Path, vectors: Path, library: str, python: str, requirements: list[str]
) -> None:
    interpreter = make_env(tmp_path, python, requirements)
    ran = subprocess.run(
        [
            str(interpreter),
            str(HERE / "nbsqw_decode_vectors.py"),
            library,
            str(vectors),
            str(ALKERA_PY),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    got: dict[str, Any] = json.loads(ran.stdout)
    for name in ("inline.stream", "spilled.arrow", "rows.json"):
        assert got[name]["columns"] == ["id", "amount", "name", "region", "price", "day"], name
        rows = [
            [
                r[0],
                r[1],
                r[2],
                r[3],
                str(r[4]) if r[4] is not None else None,
                (r[5] or "")[:10] or None,
            ]
            for r in got[name]["rows"]
        ]
        assert rows == EXPECTED_ROWS, name
    assert got["_left_in_data_dir"] == []
    if library == "polars":
        assert got["_pyarrow_imported"] is False
