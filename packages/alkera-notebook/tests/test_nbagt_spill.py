"""A page or window the budget cuts is stored whole through the host's spill.

``call_tool`` takes the host's :class:`ResultSpill` (Alkera's harness passes
the blob store large SQL results spill to). A cut list is stored one whole
item per row, a cut text whole; the page or window names the handle, the
result names the first one at its top level, and the reply still fits its
budget. A host whose store fails, or a host with none, gets the paging
arguments for the rest instead.
"""

from __future__ import annotations

import json
from typing import Any

from alkera_notebook.tools import call_tool, validate
from alkera_notebook.tools.paging import RESULT_BUDGET_BYTES
from nbagt_huge import AGENT, Huge, build, huge_source


class MemorySpill:
    """Stores in a list; the handle is the position."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.stored: list[tuple[str, Any]] = []

    def rows(self, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
        if self.fail:
            return None
        self.stored.append(("rows", [dict(zip(columns, row, strict=True)) for row in rows]))
        return {"sha256": f"h{len(self.stored) - 1}", "size": 1}

    def text(self, text: str) -> dict[str, Any] | None:
        if self.fail:
            return None
        self.stored.append(("text", text))
        return {"sha256": f"h{len(self.stored) - 1}", "size": 1}


async def _call(huge: Huge, spill: MemorySpill | None, tool: str, **args: Any) -> dict[str, Any]:
    args.setdefault("path", "huge.alknb.py")
    result = await call_tool(
        tool, validate(tool, args), host=huge.ws.host(AGENT), gatekeeper=huge.gate, spill=spill
    )
    body: dict[str, Any] = result.model_dump(mode="json")
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= RESULT_BUDGET_BYTES
    return body


async def test_a_cut_page_is_stored_one_whole_cell_per_row() -> None:
    huge = await build()
    spill = MemorySpill()
    out = await _call(huge, spill, "notebook.read", offset=10, limit=500)
    page = out["page"]
    assert page["cut_by_budget"] and page["blob"] == {"sha256": "h0", "size": 1}
    assert (out["blob"], out["ref_type"]) == (page["blob"], "rows") and out["result_name"]
    assert page["more"]["args"]["offset"] == 510 and page["next_offset"] == 510
    kind, cells = spill.stored[0]
    assert kind == "rows" and len(cells) == 500
    assert [c["id"] for c in cells] == huge.ids[10:510]
    assert cells[: len(out["cells"])] == out["cells"]


async def test_a_cut_window_is_stored_as_the_whole_window() -> None:
    huge = await build()
    spill = MemorySpill()
    out = await _call(huge, spill, "notebook.read", cells=["c507"], source_limit=2_000)
    window = out["cells"][0]["source_page"]
    assert window["cut_by_budget"] and window["blob"] == out["blob"]
    assert out["ref_type"] == "text"
    assert spill.stored == [("text", huge_source(507))]


async def test_a_reply_that_fits_stores_nothing() -> None:
    huge = await build()
    spill = MemorySpill()
    out = await _call(huge, spill, "notebook.read", limit=5)
    assert spill.stored == [] and out["blob"] is None and out["page"]["blob"] is None


async def test_without_a_store_the_page_names_the_call_for_the_rest() -> None:
    huge = await build()
    for spill in (None, MemorySpill(fail=True)):
        out = await _call(huge, spill, "notebook.read", offset=10, limit=500)
        page = out["page"]
        assert page["blob"] is None and out["blob"] is None
        assert page["more"]["args"]["offset"] == page["next_offset"] == 10 + page["returned"]
