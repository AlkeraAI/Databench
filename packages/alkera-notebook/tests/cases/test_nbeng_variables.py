"""A cell's variables, from the real kernel to a client: each run's summaries
arrive as one ``cell.variables`` event and answer an inspect, and a summary
the engine cannot read is logged, never dropped unseen."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.engine.models import InspectQuery
from alkera_notebook.engine.session import variables_of
from nbeng_harness import engine_for, notebook, run_cells


async def _variables_events(client: Any) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {}
    while True:
        try:
            event = await client.next_event(0.3)
        except TimeoutError:
            return seen
        if event is None:
            return seen
        if event.type == "cell.variables":
            seen[event.cell_id] = [v.model_dump() for v in event.variables]


async def test_real_kernel_variables_reach_a_client_and_an_inspect(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 1\nlabel = 'hi'", "y = [x, 2]"])
        record = await run_cells(ann, a, b)
        assert record.status == "ok"
        events = await _variables_events(ann)
        assert {
            cid: sorted((v["name"], v["type"], v["repr"], v["cell_id"]) for v in vs)
            for cid, vs in events.items()
        } == {
            a: [("label", "str", "'hi'", a), ("x", "int", "1", a)],
            b: [("y", "list", "[1, 2]", b)],
        }
        listed = await ann.inspect(InspectQuery(what="variables"))
        assert sorted((v.name, v.cell_id) for v in listed.variables or []) == [
            ("label", a),
            ("x", a),
            ("y", b),
        ]


@pytest.mark.parametrize(
    ("bad", "said"),
    [
        pytest.param({"name": "z", "type": "int"}, "'z'", id="missing_repr"),
        pytest.param(
            {"name": "z", "type": "int", "repr": "1", "shape": "3x4"}, "'z'", id="bad_shape"
        ),
        pytest.param({"type": "int", "repr": "1"}, "None", id="missing_name"),
        pytest.param("z", "None", id="not_an_object"),
    ],
)
def test_an_unreadable_summary_is_logged_and_the_rest_kept(
    caplog: pytest.LogCaptureFixture, bad: object, said: str
) -> None:
    good = {"name": "x", "type": "int", "repr": "1", "size_bytes": 28, "extra": "kept"}
    with caplog.at_level(logging.WARNING, logger="alkera_notebook.engine.session"):
        out = variables_of("c1", [good, bad])
    assert [(v.name, v.repr, v.cell_id) for v in out] == [("x", "1", "c1")]
    assert out[0].model_dump()["extra"] == "kept"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert warned[0].startswith(f"cell c1: variable {said} left out of its summary")


@pytest.mark.parametrize("raw", [None, {}, "x"], ids=["none", "object", "string"])
def test_no_list_is_no_variables(raw: object) -> None:
    assert variables_of("c1", raw) == []
