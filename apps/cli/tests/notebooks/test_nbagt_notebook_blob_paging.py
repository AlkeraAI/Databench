"""A notebook reply too large to inline is stored and paged as a SQL result is.

The notebook tools bound every reply to their size budget; what the budget
cuts goes to the same blob store a large SQL result spills to, and the agent
reads it back with the same tool (``fetch_result``): a list one whole cell per
row, in order, the last page saying it is last; a cell's source too long for
its window as text, by character. The reply names the handle at its top level
(``blob``, ``result_name``, ``ref_type``), the shape the chat turns into a
reference, exactly as for SQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP
from alkera_cli.plugins.plugin_base.wire import model_facing_text
from test_nbagt_notebook_scale import _Rig, _source

LONG = "n" * 200


def _rig(tmp_path: Path) -> _Rig:
    rig = _Rig(tmp_path)
    register_blob_tools(rig.registry)
    return rig


async def _page_all(
    rig: _Rig, handle: str
) -> tuple[list[str], list[list[Any]], list[dict[str, Any]]]:
    """Every page of a rows blob through ``fetch_result``, from offset 0."""
    columns: list[str] = []
    rows: list[list[Any]] = []
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await rig.call("fetch_result", {"handle": handle, "offset": offset}, "chat")
        assert "error" not in page, page
        assert page["kind"] == "rows"
        assert len(model_facing_text(page).encode()) <= RESULT_INLINE_BYTE_CAP
        columns = page["columns"]
        rows.extend(page["rows"])
        pages.append(page)
        if not page["has_more"]:
            return columns, rows, pages
        assert page["next_offset"] == offset + page["returned"]
        offset = page["next_offset"]


async def _text(rig: _Rig, handle: str) -> str:
    text, offset = "", 0
    while True:
        page = await rig.call("fetch_result", {"handle": handle, "offset": offset}, "chat")
        assert page["kind"] == "text"
        text += page["text"]
        if not page["has_more"]:
            return text
        offset = page["next_offset"]


async def test_creating_500_cells_answers_a_reference_that_pages_whole_cells_in_order(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    cells = [{"source": f"v{i} = {i}", "name": f"{LONG}{i}"} for i in range(500)]
    out = await rig.call("notebook.create", {"path": "big.alknb.py", "cells": cells}, "chat")
    assert "error" not in out, out
    assert len(model_facing_text(out).encode()) <= RESULT_INLINE_BYTE_CAP
    assert out["ref_type"] == "rows" and out["result_name"]
    handle = out["blob"]["sha256"]
    columns, rows, pages = await _page_all(rig, handle)
    assert len(pages) > 1
    assert pages[-1]["next_offset"] is None
    names = [row[columns.index("name")] for row in rows]
    # The setup block every new notebook starts with, then the 500, in order.
    assert names[0] == "setup"
    assert names[1:] == [f"{LONG}{i}" for i in range(500)]
    assert [row[columns.index("index")] for row in rows] == list(range(501))
    # What the reply carried inline is the head of the same list.
    inline = [c["name"] for c in out["cells"]]
    assert inline == names[: len(inline)]


async def test_a_large_read_answers_a_reference_to_the_whole_page(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    await rig.huge()
    out = await rig.call("notebook.read", {"offset": 1000, "limit": 500}, "chat")
    assert "error" not in out, out
    assert out["page"]["cut_by_budget"] is True
    assert out["blob"] == out["page"]["blob"]
    assert out["page"]["next_offset"] == 1500
    columns, rows, pages = await _page_all(rig, out["blob"]["sha256"])
    assert pages[-1]["has_more"] is False
    assert [row[columns.index("name")] for row in rows] == [f"c{i}" for i in range(1000, 1500)]


async def test_a_cell_longer_than_its_window_pages_its_source_by_character(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    await rig.huge()
    out = await rig.call("notebook.read", {"cells": ["c507"], "source_limit": 2_000}, "chat")
    assert "error" not in out, out
    window = out["cells"][0]["source_page"]
    assert window["cut_by_budget"] is True
    assert out["blob"] == window["blob"] and out["ref_type"] == "text"
    assert window["returned"] < window["total"] == 2_000
    assert await _text(rig, window["blob"]["sha256"]) == _source(507)


async def test_a_shown_table_with_more_rows_than_the_chat_shows_is_a_rows_reference(
    tmp_path: Path,
) -> None:
    """``notebook.show_output`` on a 300-row SQL result: the chat gets the
    first rows, the store gets the table, and ``fetch_result`` pages every row
    of it under the table's own columns, as it does a spilled SQL result."""
    rig = _rig(tmp_path)
    path = "show.alknb.py"
    query = "SELECT i AS n, 'r' || CAST(i AS VARCHAR) AS label FROM range(300) t(i)"
    cell = {"kind": "sql", "source": query, "name": "orders", "meta": {"output_var": "orders"}}
    created = await rig.call("notebook.create", {"path": path, "cells": [cell]}, "chat")
    assert "error" not in created, created
    ran = await rig.call("notebook.run", {"path": path, "target": {"kind": "all"}}, "chat")
    assert "error" not in ran, ran

    out = await rig.call(
        "notebook.show_output", {"path": path, "cell": "orders", "row_limit": 4}, "chat"
    )

    assert "error" not in out, out
    assert out["kind"] == "table"
    assert out["table"]["columns"] == ["n", "label"]
    assert out["table"]["shown_rows"] == 4 and out["table"]["total_rows"] == 300
    assert len(out["table"]["rows"]["content"]) == 4
    assert out["ref_type"] == "rows"
    assert out["result_name"] == "orders, 300 rows"
    columns, rows, _pages = await _page_all(rig, out["blob"]["sha256"])
    assert columns == ["n", "label"]
    assert [str(row[0]) for row in rows] == [str(i) for i in range(300)]
    assert [row[1] for row in rows] == [f"r{i}" for i in range(300)]
