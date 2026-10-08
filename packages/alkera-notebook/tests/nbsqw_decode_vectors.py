"""Decodes the SQL result vectors with ``alkera._sql`` in whatever
environment runs it, and prints what it read as JSON.

Usage: python decode.py <library> <vectors dir> <alkera-py dir>
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 6)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pandas Timestamp
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalars
        return plain(value.item())
    return value


def rows_of(frame: Any, library: str) -> list[list[Any]]:
    if library == "polars":
        return [[plain(v) for v in row] for row in frame.rows()]
    return [
        [plain(None if v is None or (isinstance(v, float) and math.isnan(v)) else v) for v in row]
        for row in frame.astype(object).values.tolist()
    ]


def main() -> None:
    library, vectors, alkera_py = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    sys.path.insert(0, alkera_py)
    from alkera._sql import to_frame

    work = Path(tempfile.mkdtemp())
    out: dict[str, Any] = {}
    for vector in sorted(vectors.iterdir()):
        if vector.suffix not in (".stream", ".arrow", ".json"):
            continue
        if vector.suffix == ".arrow":
            shutil.copy(vector, work / vector.name)
            table = SimpleNamespace(
                codec="arrow.ipc.file", path_in=lambda d, n=vector.name: f"{d}/{n}"
            )
        else:
            codec = "arrow.ipc.stream" if vector.suffix == ".stream" else "rows.json"
            table = SimpleNamespace(codec=codec, data=vector.read_bytes())
        frame = to_frame(table, library, str(work))
        out[vector.name] = {
            "columns": [str(c) for c in frame.columns],
            "rows": rows_of(frame, library),
        }
    out["_left_in_data_dir"] = sorted(p.name for p in work.iterdir())
    out["_pyarrow_imported"] = "pyarrow" in sys.modules
    print(json.dumps(out))


if __name__ == "__main__":
    main()
