"""The notebook tools on a huge notebook: every result stays under the size
budget, pages walk the whole notebook exactly once, and the rest is always
one stated call away.

The notebook is generated (``nbagt_huge``): 5,000 cells with ten 2,000-line
cells, a 1,000,000-row frame, megabytes of output text, a 20,000-line
traceback, 3,000 kernel variables, 2,000 widgets and 3,000 packages. Sizes are
measured here as the JSON a transport sends, independently of the tools' own
measure.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from alkera_notebook.tools import NotebookToolError
from alkera_notebook.tools.paging import RESULT_BUDGET_BYTES
from nbagt_huge import (
    CELLS,
    ERROR_CELL,
    FRAME_CELL,
    FRAME_ROWS,
    HUGE_LINES,
    IMAGE_CELL,
    NEEDLE,
    NEEDLE_CELL,
    NEEDLE_LINE,
    PACKAGES,
    TEXT_CELL,
    VARIABLES,
    WIDGETS,
    Huge,
    build,
    huge_source,
)
from pydantic import BaseModel, ValidationError

#: Edges in the generated notebook: the first and last chains hold 99 cells
#: (98 links each), the 48 chains between them 100 cells (99 links each), and
#: the SQL cell uses ``alkera`` from the setup cell.
EDGES = 98 + 48 * 99 + 98 + 1

#: What the budget gate measures against: 48 KiB, written out so a change to
#: the tools' constant is a change this test notices.
BUDGET = 48 * 1024


def wire_bytes(result: BaseModel) -> int:
    return len(json.dumps(result.model_dump(mode="json"), ensure_ascii=False).encode())


def dump(result: BaseModel) -> dict[str, Any]:
    return result.model_dump(mode="json")


@pytest.fixture
async def huge() -> Huge:
    return await build()


def test_the_budget_is_the_one_the_tools_use() -> None:
    assert RESULT_BUDGET_BYTES == BUDGET


# Every tool's default call (and the calls that reach the biggest things in the
# notebook) on the huge notebook.
DEFAULT_CALLS = [
    pytest.param("notebook.read", {}, id="read"),
    pytest.param("notebook.read", {"cells": ["c507"]}, id="read_huge_cell"),
    pytest.param("notebook.read", {"cells": ["c507", "c1007", "c1507"]}, id="read_three_huge"),
    pytest.param("notebook.read", {"search": "x"}, id="read_search_everything"),
    pytest.param("notebook.graph", {}, id="graph"),
    pytest.param("notebook.graph", {"cell": "setup", "direction": "down"}, id="graph_cell"),
    pytest.param("notebook.output", {"cell": TEXT_CELL}, id="output_text"),
    pytest.param("notebook.output", {"cell": ERROR_CELL}, id="output_error"),
    pytest.param("notebook.output", {"cell": FRAME_CELL}, id="output_table"),
    pytest.param("notebook.output", {"cell": IMAGE_CELL}, id="output_images"),
    pytest.param("notebook.inspect", {"what": "variables"}, id="inspect_variables"),
    pytest.param("notebook.inspect", {"what": "frame", "name": FRAME_CELL}, id="inspect_frame"),
    pytest.param("notebook.inspect", {"what": "value", "name": FRAME_CELL}, id="inspect_value"),
    pytest.param("notebook.widget", {}, id="widget_list"),
    pytest.param("notebook.env", {"action": "packages"}, id="env_packages"),
    pytest.param("notebook.kernel", {}, id="kernel"),
    pytest.param("notebook.settings", {}, id="settings"),
    pytest.param(
        "notebook.edit",
        {"ops": [{"op": "replace", "cell_id": "c3", "source": "x3 = x2 + 2"}]},
        id="edit",
    ),
]


@pytest.mark.parametrize(("tool", "args"), DEFAULT_CALLS)
async def test_every_tool_stays_under_the_budget(
    huge: Huge, tool: str, args: dict[str, Any]
) -> None:
    result = await huge.call(tool, **args)
    assert wire_bytes(result) <= BUDGET


async def test_a_run_of_every_cell_stays_under_the_budget(huge: Huge) -> None:
    result = await huge.call("notebook.run", target={"kind": "all"}, confirm_expensive=True)
    assert wire_bytes(result) <= BUDGET


# ---------------------------------------------------------------------------
# notebook.read
# ---------------------------------------------------------------------------


async def test_read_defaults_to_an_outline_without_sources(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read"))
    assert out["total_cells"] == CELLS
    assert out["page"]["offset"] == 0
    assert out["page"]["total"] == CELLS
    assert out["page"]["next_offset"] == out["page"]["returned"]
    first = out["cells"][1]
    assert first["name"] == "c1"
    assert first["source"] is None
    assert first["head"]["content"] == "# step 1: an ordinary cell"
    assert first["lines"] == 3
    assert first["defs"] == ["x1"]


async def test_paging_walks_every_cell_exactly_once(huge: Huge) -> None:
    seen: list[str] = []
    offset: int | None = 0
    calls = 0
    while offset is not None:
        out = dump(await huge.call("notebook.read", offset=offset, limit=500))
        assert wire_bytes(await huge.call("notebook.read", offset=offset, limit=500)) <= BUDGET
        seen.extend(c["id"] for c in out["cells"])
        assert [c["index"] for c in out["cells"]] == list(range(offset, offset + len(out["cells"])))
        nxt = out["page"]["next_offset"]
        if nxt is not None:
            assert out["page"]["more"]["args"] == {
                "path": "huge.alknb.py",
                "offset": nxt,
                "limit": 500,
            }
        offset = nxt
        calls += 1
        assert calls < 200
    assert seen == huge.ids


async def test_a_page_the_budget_cuts_says_so_and_names_the_next_call(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", limit=500))
    page = out["page"]
    assert 0 < page["returned"] < 500
    assert page["cut_by_budget"] is True
    assert page["more"] == {
        "tool": "notebook.read",
        "args": {"path": "huge.alknb.py", "limit": 500, "offset": page["returned"]},
        "note": page["more"]["note"],
    }


async def test_search_finds_the_one_cell_and_the_line(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", search=NEEDLE))
    assert [c["name"] for c in out["cells"]] == [NEEDLE_CELL]
    assert out["matched"] == 1
    assert out["cells"][0]["matches"] == [NEEDLE_LINE]


async def test_search_is_case_insensitive_and_matches_names(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", search="C4998"))
    assert [c["name"] for c in out["cells"]] == ["c4998"]


async def test_search_by_regex(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", search=r"^x49(8|9)\d = x", regex=True))
    assert [c["name"] for c in out["cells"]] == [f"c{i}" for i in range(4980, 4999)]


async def test_an_invalid_regex_is_refused_by_name(huge: Huge) -> None:
    with pytest.raises(NotebookToolError) as caught:
        await huge.call("notebook.read", search="(", regex=True)
    assert caught.value.code == "invalid_search"


async def test_read_by_status(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", status=["error"]))
    assert [c["name"] for c in out["cells"]] == [ERROR_CELL]


async def test_naming_cells_returns_their_full_source(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", cells=["c3", "c4"]))
    assert [c["source"]["content"] for c in out["cells"]] == [
        "# step 3: an ordinary cell\n_tmp = [n * n for n in range(10)]\nx3 = x2 + 1",
        "# step 4: an ordinary cell\n_tmp = [n * n for n in range(10)]\nx4 = x3 + 1",
    ]


async def test_a_source_line_range_returns_exactly_those_lines(huge: Huge) -> None:
    out = dump(await huge.call("notebook.read", cells=["c507"], source_offset=100, source_limit=50))
    cell = out["cells"][0]
    lines = huge_source(507).splitlines(keepends=True)
    assert cell["source"]["content"] == "".join(lines[100:150])
    assert cell["source_page"]["offset"] == 100
    assert cell["source_page"]["returned"] == 50
    assert cell["source_page"]["total"] == HUGE_LINES
    assert cell["source_page"]["next_offset"] == 150


async def test_reading_a_huge_cell_on_walks_every_line_once(huge: Huge) -> None:
    lines: list[str] = []
    offset: int | None = 0
    while offset is not None:
        out = dump(await huge.call("notebook.read", cells=["c1007"], source_offset=offset))
        cell = out["cells"][0]
        lines.append(cell["source"]["content"])
        offset = cell["source_page"]["next_offset"]
        if offset is not None:
            assert cell["source_page"]["more"]["args"]["source_offset"] == offset
    assert "".join(lines) == huge_source(1007)


async def test_an_unknown_cell_is_refused(huge: Huge) -> None:
    with pytest.raises(NotebookToolError) as caught:
        await huge.call("notebook.read", cells=["nope"])
    assert caught.value.code == "cell_not_found"


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"limit": 501}, id="limit"),
        pytest.param({"limit": 0}, id="limit_zero"),
        pytest.param({"offset": -1}, id="offset"),
        pytest.param({"source_limit": 2001}, id="source_limit"),
    ],
)
def test_read_page_sizes_have_a_hard_cap(args: dict[str, Any]) -> None:
    from alkera_notebook.tools import validate

    with pytest.raises(ValidationError):
        validate("notebook.read", {"path": "x.alknb.py", **args})


# ---------------------------------------------------------------------------
# notebook.graph
# ---------------------------------------------------------------------------


async def test_graph_defaults_to_a_summary(huge: Huge) -> None:
    out = dump(await huge.call("notebook.graph"))
    summary = out["summary"]
    assert summary["cells"] == CELLS
    assert summary["edges"] == EDGES
    assert out["edges"] == []


async def test_graph_depth_scopes_upstream_and_downstream(huge: Huge) -> None:
    by = huge.by_name
    one = dump(await huge.call("notebook.graph", cell="c250", depth=1))
    assert one["upstream"] == [by["c249"]]
    assert one["downstream"] == [by["c251"]]
    three = dump(await huge.call("notebook.graph", cell="c250", direction="up", depth=3))
    assert three["upstream"] == [by["c249"], by["c248"], by["c247"]]
    assert three["downstream"] == []
    assert {three["cells"][cid]["distance"] for cid in three["upstream"]} == {1, 2, 3}
    assert sorted(three["edges"]) == sorted(
        [
            [by["c247"], by["c248"], ["x247"]],
            [by["c248"], by["c249"], ["x248"]],
            [by["c249"], by["c250"], ["x249"]],
        ]
    )
    assert three["upstream_direct"] == [by["c249"]]
    assert three["upstream_transitive"] == [by["c248"], by["c247"]]


async def test_graph_edges_page_walks_every_edge_once(huge: Huge) -> None:
    seen: list[tuple[str, str]] = []
    offset: int | None = 0
    while offset is not None:
        out = dump(await huge.call("notebook.graph", summary=False, offset=offset, limit=1000))
        seen.extend((a, b) for a, b, _via in out["edges"])
        offset = out["page"]["next_offset"]
    assert len(seen) == len(set(seen)) == EDGES


# ---------------------------------------------------------------------------
# notebook.output
# ---------------------------------------------------------------------------


async def test_output_text_pages_by_character_and_walks_it_whole(huge: Huge) -> None:
    parts: list[str] = []
    offset: int | None = 0
    calls = 0
    while offset is not None:
        out = dump(
            await huge.call("notebook.output", cell=TEXT_CELL, text_offset=offset, max_chars=40_000)
        )
        parts.append(out["text"]["content"])
        offset = out["text_page"]["next_offset"]
        calls += 1
    assert "".join(parts) == huge.text
    assert calls == -(-len(huge.text) // 40_000)


async def test_output_text_by_line_range(huge: Huge) -> None:
    out = dump(await huge.call("notebook.output", cell=TEXT_CELL, line_offset=1000, line_limit=3))
    assert out["text"]["content"] == "".join(f"output line {n:07d}\n" for n in (1000, 1001, 1002))
    assert out["text_page"]["unit"] == "line"
    assert out["text_page"]["total"] == len(huge.text.splitlines())


async def test_output_table_rows_come_from_the_frame_by_row_range(huge: Huge) -> None:
    out = dump(
        await huge.call(
            "notebook.output", cell=FRAME_CELL, part="table", row_offset=500_000, row_limit=3
        )
    )
    table = out["table"]["content"]
    assert table["columns"] == ["row", "label"]
    assert table["rows"] == [[n, f"r{n}"] for n in (500_000, 500_001, 500_002)]
    assert out["table_page"]["total"] == FRAME_ROWS
    assert out["table_page"]["next_offset"] == 500_003


async def test_large_images_are_referenced_not_inlined(huge: Huge) -> None:
    out = dump(await huge.call("notebook.output", cell=IMAGE_CELL, part="image"))
    small, medium, large = out["images"]
    assert small["data_base64"] is not None
    assert (medium["data_base64"], large["data_base64"]) == (None, None)
    assert (medium["bytes"], large["bytes"]) == (4 + 100 * 1024, 4 + 400 * 1024)


async def test_images_are_inlined_only_when_asked_for(huge: Huge) -> None:
    out = dump(await huge.call("notebook.output", cell=IMAGE_CELL))
    assert [image["data_base64"] for image in out["images"]] == [None, None, None]
    assert [image["index"] for image in out["images"]] == [0, 1, 2]


# ---------------------------------------------------------------------------
# notebook.run
# ---------------------------------------------------------------------------


async def test_a_large_run_is_summarized_with_its_failures(huge: Huge) -> None:
    out = dump(await huge.call("notebook.run", target={"kind": "all"}, confirm_expensive=True))
    assert out["plan_total"] == CELLS
    assert len(out["plan"]) <= 50
    assert out["cells"] == []
    assert sum(out["counts"].values()) == CELLS
    failed = [c["name"] for c in out["failures"]]
    assert failed, out["counts"]
    assert all(c["status"] in ("error", "interrupted") for c in out["failures"])
    assert {m["tool"] for m in out["see"]} == {"notebook.read", "notebook.output"}


async def test_a_small_run_still_lists_its_cells(huge: Huge) -> None:
    out = dump(await huge.call("notebook.run", target={"kind": "cells", "ids": ["c3101"]}))
    assert [c["name"] for c in out["cells"]] == ["c3100", "c3101"]
    assert out["plan_total"] == 2


# ---------------------------------------------------------------------------
# notebook.edit
# ---------------------------------------------------------------------------


async def test_edit_by_exact_text_in_a_huge_cell_by_name(huge: Huge) -> None:
    old = "v1507_1000 = 1000  # line 1000 of a long cell"
    out = await huge.call(
        "notebook.edit",
        ops=[{"op": "edit", "cell_id": "c1507", "edits": [{"old": old, "new": "v1507_1000 = -1"}]}],
    )
    assert wire_bytes(out) <= BUDGET
    back = dump(
        await huge.call("notebook.read", cells=["c1507"], source_offset=1000, source_limit=1)
    )
    assert back["cells"][0]["source"]["content"] == "v1507_1000 = -1\n"
    source = huge.nb.doc.cells[huge.by_name["c1507"]].source
    assert source == huge_source(1507).replace(old, "v1507_1000 = -1")


async def test_ops_address_cells_and_anchors_by_name(huge: Huge) -> None:
    await huge.call("notebook.edit", ops=[{"op": "move", "cell_id": "c4000", "after": "c10"}])
    assert huge.nb.doc.live()[11].id == huge.by_name["c4000"]


async def test_an_ambiguous_name_is_refused(huge: Huge) -> None:
    await huge.call("notebook.edit", ops=[{"op": "rename", "cell_id": "c11", "name": "c10"}])
    with pytest.raises(NotebookToolError) as caught:
        await huge.call("notebook.edit", ops=[{"op": "delete", "cell_id": "c10"}])
    assert caught.value.code == "ambiguous_cell"


def test_a_batch_over_the_cap_is_refused_with_its_limit() -> None:
    from alkera_notebook.tools import validate

    ops = [{"op": "delete", "cell_id": "x"}] * 201
    with pytest.raises(ValidationError) as caught:
        validate("notebook.edit", {"path": "x.alknb.py", "ops": ops})
    assert "200" in str(caught.value)


def test_a_batch_over_the_text_cap_is_refused() -> None:
    from alkera_notebook.tools import validate

    ops = [{"op": "replace", "cell_id": "x", "source": "a" * (1024 * 1024)}] * 3
    with pytest.raises(ValidationError) as caught:
        validate("notebook.edit", {"path": "x.alknb.py", "ops": ops})
    assert "2 MiB" in str(caught.value)


async def test_an_edit_reports_stale_cells_as_a_count_and_a_page(huge: Huge) -> None:
    from alkera_notebook.sim.reference import CellKernelState

    nb = huge.nb
    for seq, i in enumerate(range(301, 400), start=1):
        cid = huge.by_name[f"c{i}"]
        nb.kstate[cid] = CellKernelState(
            submitted=nb.doc.cells[cid].source, has_value=True, result="ok", seq=seq
        )
    out = dump(
        await huge.call(
            "notebook.edit",
            ops=[
                {
                    "op": "edit",
                    "cell_id": "c300",
                    "edits": [{"old": "x300 = 300", "new": "x300 = 7"}],
                }
            ],
        )
    )
    stale = [cid for cid, status in nb.statuses().items() if status == "stale"]
    assert {huge.by_name[f"c{i}"] for i in range(301, 400)} <= set(stale)
    assert out["stale_total"] == len(stale)
    assert out["stale"] == stale[:50]
    assert set(out["graph"]["cells"]) == {huge.by_name["c300"]}
    assert out["graph"]["edges"] == [[huge.by_name["c300"], huge.by_name["c301"]]]


# ---------------------------------------------------------------------------
# notebook.inspect, notebook.widget, notebook.env
# ---------------------------------------------------------------------------


async def test_variables_page_walks_every_variable_once(huge: Huge) -> None:
    names: list[str] = []
    offset: int | None = 0
    while offset is not None:
        out = dump(await huge.call("notebook.inspect", what="variables", offset=offset, limit=500))
        names.extend(v["name"] for v in out["variables"])
        offset = out["page"]["next_offset"]
    assert [n for n in names if n.startswith("var")] == [f"var{n:05d}" for n in range(VARIABLES)]


async def test_frame_pages_columns_and_rows_of_a_million_row_frame(huge: Huge) -> None:
    out = dump(
        await huge.call(
            "notebook.inspect", what="frame", name=FRAME_CELL, offset=FRAME_ROWS - 2, limit=10
        )
    )
    assert out["frame"]["total_rows"] == FRAME_ROWS
    assert out["frame"]["rows"]["content"] == [
        [FRAME_ROWS - 2, f"r{FRAME_ROWS - 2}"],
        [FRAME_ROWS - 1, f"r{FRAME_ROWS - 1}"],
    ]
    assert out["page"]["next_offset"] is None


async def test_widgets_page_walks_every_widget_once(huge: Huge) -> None:
    ids: list[str] = []
    offset: int | None = 0
    while offset is not None:
        out = dump(await huge.call("notebook.widget", offset=offset, limit=200))
        ids.extend(w["model_id"] for w in out["widgets"])
        offset = out["page"]["next_offset"]
    assert ids == [f"w-{n:05d}" for n in range(WIDGETS)]


async def test_packages_page_and_search(huge: Huge) -> None:
    out = dump(await huge.call("notebook.env", action="packages", search="pkg0299"))
    assert [p["name"] for p in out["packages"]] == [f"pkg0299{n}" for n in range(10)]
    first = dump(await huge.call("notebook.env", action="packages"))
    assert first["page"]["total"] == PACKAGES + 2


async def test_creating_the_largest_notebook_stays_under_the_budget(huge: Huge) -> None:
    """A create of the most cells one call takes, each with a long name,
    answers within the budget and says how to read the rest."""
    long = "n" * 200
    cells = [{"source": f"v{i} = {i}", "name": f"{long}{i}"} for i in range(500)]
    out = await huge.call("notebook.create", path="big.alknb.py", cells=cells)
    assert wire_bytes(out) <= BUDGET
    body = out.model_dump(mode="json")
    # The 500 cells, after the setup block every new notebook starts with.
    assert body["total_cells"] == 501
    listed = [c["name"] for c in body["cells"] if c["kind"] != "setup"]
    assert 0 < len(listed) < 500
    assert listed == [f"{long}{i}" for i in range(len(listed))]
    assert body["more"]["tool"] == "notebook.read"
    assert body["more"]["args"] == {"path": "big.alknb.py", "offset": len(body["cells"])}
