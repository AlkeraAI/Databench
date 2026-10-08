"""``notebook.show_output`` against the real engine: each kind of output a cell
holds comes back in the shape the chat draws, from outputs a real kernel made.

One notebook is built and run once per test session (a real kernel with the
display libraries); each test reads it. Nothing in these tests runs a cell
after that, which is the tool's own contract: it shows what is there.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "alkera-kernel" / "tests" / "kernel"))
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.engine_target import EngineTarget
from alkera_notebook.tools import NotebookToolError, RecordingGatekeeper, call_tool, validate
from alkera_notebook.tools.paging import RESULT_BUDGET_BYTES
from nbkrn_harness import rich_python

# The module-scoped fixture is built once per worker; keep its tests together.
pytestmark = pytest.mark.xdist_group("nbagt_show_output")

__all__ = ["rich_python"]

PATH = "analysis.alknb.py"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

SETUP = "import alkera\nimport pandas as pd\nimport matplotlib.pyplot as plt"
CELLS: list[dict[str, Any]] = [
    {"kind": "setup", "source": SETUP},
    {"kind": "markdown", "source": "# Revenue\n\nBy *region*.", "name": "intro"},
    {
        "source": (
            "orders = pd.DataFrame({'n': range(500), 'label': [f'r{i}' for i in range(500)]})\n"
            "orders"
        ),
        "name": "orders_table",
    },
    {
        "source": (
            "sales = pd.DataFrame({'region': ['north', 'south'], 'revenue': [3, 4]})\n"
            "alkera.chart(sales).bar(x='region', y='revenue')"
        ),
        "name": "by_region",
    },
    {"source": "fig, ax = plt.subplots()\nax.plot([1, 2, 3])\nfig", "name": "trend"},
    {
        "source": (
            "daily = pd.DataFrame("
            "{'day': [f'd{i:04d}' for i in range(2000)], 'sov_daily': range(2000)})\n"
            "alkera.chart(daily).line(x='day', y='sov_daily').title('Share of voice')"
        ),
        "name": "big_chart",
    },
    {"source": "print('42 rows checked')", "name": "check"},
    {
        "source": "small = pd.DataFrame({'k': ['a', 'b'], 'v': [1, 2]})\nsmall",
        "name": "small_table",
    },
    {"source": "1 / 0", "name": "broken"},
    {"source": "quiet = 1", "name": "quiet"},
]


class MemorySpill:
    """A host store that keeps what it is given; the handle is the position."""

    def __init__(self) -> None:
        self.tables: list[tuple[list[str], list[list[Any]]]] = []

    def rows(self, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
        self.tables.append((columns, rows))
        return {"sha256": f"h{len(self.tables) - 1}", "size": len(rows), "media_type": "rows"}

    def text(self, text: str) -> dict[str, Any] | None:
        raise AssertionError("a shown output never stores text")


class Shown:
    """The run notebook, and the one way these tests call the tool."""

    def __init__(self, driver: SimDriver, images: dict[str, bytes]) -> None:
        self.driver = driver
        self.images = images
        self.gate = RecordingGatekeeper(allow=frozenset())

    async def show(self, spill: MemorySpill | None = None, **args: Any) -> dict[str, Any]:
        tool = "notebook.show_output"
        result = await call_tool(
            tool,
            validate(tool, {"path": PATH, **args}),
            host=self.driver.agent_host,
            gatekeeper=self.gate,
            spill=spill,
        )
        return result.model_dump(mode="json")


@pytest.fixture(scope="module")
async def shown(tmp_path_factory: pytest.TempPathFactory, rich_python: str) -> AsyncIterator[Shown]:
    root = tmp_path_factory.mktemp("show_output")
    target = EngineTarget(root / "ws", envs=StaticEnvRegistry(interpreter=rich_python))
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        created = await driver.agent("notebook.create", {"path": PATH, "cells": CELLS})
        assert not isinstance(created, Exception), created
        for cell in created.cells[1:]:  # type: ignore[union-attr]
            # One cell at a time: the cell that divides by zero must not stop
            # the cells after it.
            await driver.agent(
                "notebook.run",
                {"path": PATH, "target": {"kind": "cells", "ids": [cell.id]}, "timeout_s": 180},
            )
        raw = await driver.agent_host.open(driver.agent_host.resolve(PATH))
        [trend] = [c.id for c in created.cells if c.name == "trend"]  # type: ignore[union-attr]
        detail = await raw.output(trend, "image")
        yield Shown(driver, {"trend": detail.images[0].data})
    finally:
        await driver.close()


async def test_a_table_is_columns_and_the_first_rows_with_the_true_total(shown: Shown) -> None:
    out = await shown.show(cell="small_table")
    assert out["kind"] == "table"
    assert out["cell_name"] == "small_table"
    assert out["table"]["columns"] == ["k", "v"]
    assert out["table"]["rows"]["untrusted"] is True
    assert out["table"]["rows"]["content"] == [["a", 1], ["b", 2]]
    assert (out["table"]["shown_rows"], out["table"]["total_rows"]) == (2, 2)
    # All of it is in the chat, so nothing is stored and nothing is noted.
    assert out["blob"] is None and out["note"] == ""


async def test_a_big_table_shows_its_first_rows_and_is_stored_whole(shown: Shown) -> None:
    spill = MemorySpill()
    out = await shown.show(spill, cell="orders_table", row_limit=3)
    assert out["table"]["rows"]["content"] == [[0, "r0"], [1, "r1"], [2, "r2"]]
    assert (out["table"]["shown_rows"], out["table"]["total_rows"]) == (3, 500)
    # The whole table went to the host's store, columns as they are, and the
    # result names that handle the way a spilled SQL result does.
    [(columns, rows)] = spill.tables
    assert columns == ["n", "label"]
    assert len(rows) == 500 and rows[0] == [0, "r0"] and rows[499] == [499, "r499"]
    assert out["blob"] == {"sha256": "h0", "size": 500, "media_type": "rows"}
    assert out["ref_type"] == "rows"
    assert out["result_name"] == "orders_table, 500 rows"
    assert out["note"] == "Showing 3 of 500 rows. The whole table is stored with this result."
    assert len(json.dumps(out)) <= RESULT_BUDGET_BYTES


async def test_a_big_table_without_a_store_says_how_much_is_shown(shown: Shown) -> None:
    out = await shown.show(cell="orders_table", row_limit=5)
    assert out["table"]["shown_rows"] == 5
    assert out["blob"] is None
    assert out["note"] == "Showing 5 of 500 rows."


async def test_a_chart_is_its_spec_with_the_rows_it_draws(shown: Shown) -> None:
    out = await shown.show(cell="by_region")
    assert out["kind"] == "chart"
    spec = out["chart_spec"]["content"]
    assert out["chart_spec"]["untrusted"] is True
    assert spec["mark"] == "bar"
    assert spec["encoding"]["x"]["field"] == "region"
    assert spec["encoding"]["y"]["field"] == "revenue"
    [rows] = spec["datasets"].values()
    assert rows == [{"region": "north", "revenue": 3}, {"region": "south", "revenue": 4}]
    # The chart's repr is text the cell also holds; auto still picks the chart.
    assert out["available"] == ["chart", "text"]


async def test_an_image_is_named_by_the_hash_of_its_bytes(shown: Shown) -> None:
    out = await shown.show(cell="trend")
    assert out["kind"] == "image"
    png = shown.images["trend"]
    assert png.startswith(PNG_SIGNATURE)
    assert out["image"] == {
        "index": 0,
        "total": 1,
        "mime": "image/png",
        "bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    # The bytes themselves stay out of the reply: a model cannot read them.
    assert png.hex()[:64] not in json.dumps(out)


async def test_markdown_is_the_cells_markdown(shown: Shown) -> None:
    out = await shown.show(cell="intro")
    assert out["kind"] == "markdown"
    assert out["markdown"]["content"].strip() == "# Revenue\n\nBy *region*."
    assert out["text"] is None
    assert out["available"] == ["markdown"]


async def test_printed_text_is_text(shown: Shown) -> None:
    out = await shown.show(cell="check")
    assert out["kind"] == "text"
    assert out["text"]["content"] == "42 rows checked\n"
    assert out["markdown"] is None


async def test_an_error_is_shown_before_anything_else(shown: Shown) -> None:
    out = await shown.show(cell="broken")
    assert out["kind"] == "error"
    # The call succeeded: the cell's error is its own field, never a top-level
    # ``error``, which a harness reads as the call having failed.
    assert "error" not in out
    assert out["cell_error"]["ename"] == "ZeroDivisionError"
    assert out["cell_error"]["evalue"]["content"] == "division by zero"


async def test_a_part_the_cell_holds_can_be_asked_for_by_name(shown: Shown) -> None:
    out = await shown.show(cell="by_region", part="text")
    assert out["kind"] == "text"
    assert "alkera chart: bar" in out["text"]["content"]
    assert out["chart_spec"] is None


@pytest.mark.parametrize(
    ("args", "code", "says"),
    [
        pytest.param(
            {"cell": "check", "part": "chart"},
            "output_not_found",
            "check has no chart output. It has: text.",
            id="a_part_it_does_not_hold",
        ),
        pytest.param(
            {"cell": "quiet"},
            "no_output",
            "quiet has no output to show. Run it with notebook.run first.",
            id="a_cell_with_no_output",
        ),
        pytest.param(
            {"cell": "trend", "image": 1},
            "image_not_found",
            "trend has 1 image; image 1 is not one of them (the first is 0).",
            id="an_image_past_the_last",
        ),
        pytest.param({"cell": "nowhere"}, "cell_not_found", "nowhere", id="no_such_cell"),
    ],
)
async def test_what_cannot_be_shown_is_refused_with_what_can(
    shown: Shown, args: dict[str, Any], code: str, says: str
) -> None:
    with pytest.raises(NotebookToolError) as raised:
        await shown.show(**args)
    assert raised.value.code == code
    assert says in str(raised.value)


async def _last_runs(shown: Shown) -> dict[str, str | None]:
    """Each cell's latest run, by cell id."""
    port = await shown.driver.agent_host.open(shown.driver.agent_host.resolve(PATH))
    view = await port.read(None, include_source=False, include_outputs=False)
    return {c.id: c.last_run.run_id if c.last_run else None for c in view.cells}


async def test_last_is_the_default_cell_and_showing_never_asks_or_runs(shown: Shown) -> None:
    runs_before = await _last_runs(shown)
    with pytest.raises(NotebookToolError) as raised:
        await shown.show()
    # The last cell is the one with no output; the refusal names it.
    assert raised.value.code == "no_output" and "quiet" in str(raised.value)
    await shown.show(cell="small_table")
    # The gate here allows nothing, so a call that asked would have been refused.
    assert shown.gate.asked == []
    assert await _last_runs(shown) == runs_before


async def _stored_copy(shown: Shown, sha256: str, timeout_s: float = 15.0) -> bytes:
    """The file the notebook keeps beside itself under ``sha256``, once the
    debounced snapshot has written it."""
    target = shown.driver.target
    folder = Path(target.root) / "__marimo__" / "session" / f"{PATH}.d"  # type: ignore[attr-defined]
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        found = folder / f"{sha256}.json"
        if found.exists():
            return found.read_bytes()
        if asyncio.get_running_loop().time() > deadline:
            present = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
            raise AssertionError(f"no stored chart {sha256}.json in {present}")
        await asyncio.sleep(0.2)


async def test_a_chart_of_any_size_is_shown_by_the_file_its_spec_is_stored_in(
    shown: Shown,
) -> None:
    out = await shown.show(cell="big_chart")
    assert out["kind"] == "chart"
    # Nothing says it is too large: the reply names where the spec is instead
    # of carrying it, and tells the agent what the chart is.
    assert out["note"] == ""
    assert out["chart_spec"] is None
    assert out["chart_summary"]["content"] == (
        "line chart titled 'Share of voice' (x=day, y=sov_daily) drawing 2000 rows"
    )
    assert len(json.dumps(out)) < 4_000
    ref = out["chart_ref"]
    stored = await _stored_copy(shown, ref["sha256"])
    assert hashlib.sha256(stored).hexdigest() == ref["sha256"]
    assert len(stored) == ref["bytes"] > 16 * 1024
    spec = json.loads(stored)
    [rows] = spec["datasets"].values()
    assert len(rows) == 2000 and rows[1999] == {"day": "d1999", "sov_daily": 1999}
    assert spec["mark"] in ("line", {"type": "line"})
