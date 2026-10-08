"""What the notebook routes answer is the notebook engine's own models,
imported from ``alkera_notebook`` by the backend (``alkera_core`` never
depends on the engine), and those models round-trip as the routes send them."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.engine import models as engine_models
from alkera_notebook.engine.models import GraphSummary, NotebookOpsResult, NotebookView
from backend.api.routes.notebooks import KernelResult, parse_sort
from backend.api.routes.notebooks import router as notebooks_router
from fastapi.routing import APIRoute

CELL = "a1b2c3d4e5"
OTHER = "f6g7h8j9k0"


def _response_models() -> dict[str, Any]:
    return {
        route.name: route.response_model
        for route in notebooks_router.routes
        if isinstance(route, APIRoute) and route.response_model is not None
    }


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(name, id=name)
        for name in ("NotebookView", "NotebookOpsResult", "EnvListing", "EnvPackages")
    ],
)
def test_a_route_answers_the_engine_s_own_model(name: str) -> None:
    """One definition: a route answering one of the engine's shapes answers
    the engine's class itself, never a copy that could drift from it."""
    by_name = [model for model in _response_models().values() if model.__name__ == name]
    assert by_name, f"no notebook route answers {name}"
    assert all(model is getattr(engine_models, name) for model in by_name)


def test_the_kernel_result_carries_the_engine_s_kernel_info() -> None:
    assert KernelResult.model_fields["kernel"].annotation is engine_models.KernelInfo


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, [], id="absent"),
        pytest.param("a", [("a", False)], id="bare-column"),
        pytest.param("a:desc,b:asc", [("a", True), ("b", False)], id="directions"),
    ],
)
def test_a_sort_parameter_reads_as_sort_keys(
    raw: str | None, expected: list[tuple[str, bool]]
) -> None:
    assert [(key.column, key.descending) for key in parse_sort(raw)] == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(":asc", id="empty-column"),
        pytest.param("a:sideways", id="bad-direction"),
        pytest.param(",".join(f"c{i}" for i in range(17)), id="too-many-keys"),
    ],
)
def test_a_sort_parameter_that_does_not_read_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_sort(raw)


def test_an_ops_result_round_trips() -> None:
    result = NotebookOpsResult.model_validate(
        {
            "token": "4.AQID",
            "repeat": False,
            "cells": [{"id": CELL, "index": None, "kind": "python", "name": "_", "deleted": True}],
            "created": [],
            "notices": [{"cell_id": CELL, "kind": "concurrent_edit", "message": "Bob is typing"}],
            "graph": {
                "computed": True,
                "cells": {
                    CELL: {
                        "defs": ["x"],
                        "refs": [],
                        "errors": [
                            {"code": "multiple_definitions", "name": "x", "cells": [CELL, OTHER]}
                        ],
                    }
                },
                "edges": [[OTHER, CELL]],
            },
        }
    )
    assert NotebookOpsResult.model_validate(result.model_dump(mode="json")) == result
    (error,) = result.graph.cells[CELL].errors
    assert (error.code, error.name, error.cells) == ("multiple_definitions", "x", [CELL, OTHER])


OTHER = "f6g7h8j9k0"


def test_a_view_round_trips_with_the_document_s_cell_settings_and_presence() -> None:
    """What the box's document store reads back from the view: each cell's
    config, meta and extra, and when and as whom someone was in a cell."""
    view = NotebookView.model_validate(
        {
            "path": "orders.alknb.py",
            "token": "4.AQID",
            "settings": {"format": "1.0", "reactivity": "lazy"},
            "kernel": {"state": "absent", "env": None, "reactivity": "lazy", "seq": 7},
            "cells": [
                {
                    "id": CELL,
                    "name": "orders",
                    "kind": "sql",
                    "index": 0,
                    "status": "not_run",
                    "source": "SELECT 1",
                    "config": {"hide_code": True},
                    "meta": {"connection": "wh", "output_var": "orders"},
                    "extra": {"alkera_note": "x"},
                }
            ],
            "presence": [
                {"who": "Ann", "cell_id": CELL, "kind": "person", "at": "2026-10-05T12:00:00Z"}
            ],
        }
    )
    again = NotebookView.model_validate(view.model_dump(mode="json"))
    assert again == view
    (cell,) = again.cells
    assert (cell.config, cell.meta, cell.extra) == (
        {"hide_code": True},
        {"connection": "wh", "output_var": "orders"},
        {"alkera_note": "x"},
    )
    assert (again.presence[0].kind, again.presence[0].at is not None) == ("person", True)
    assert (again.settings.reactivity, again.kernel.seq) == ("lazy", 7)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"code": "cycle", "cells": ["b", "a"]},
            {"code": "cycle", "name": None, "cells": ["b", "a"]},
            id="cycle-without-name",
        ),
        pytest.param(
            {"code": "multiple_definitions", "name": "x", "cells": ["a", "b"]},
            {"code": "multiple_definitions", "name": "x", "cells": ["a", "b"]},
            id="named",
        ),
        pytest.param("syntax", {"code": "syntax", "name": None, "cells": []}, id="bare-code"),
        pytest.param({}, {"code": "error", "name": None, "cells": []}, id="empty-object"),
    ],
)
def test_a_graph_error_reads_what_the_format_reports(raw: Any, expected: dict[str, Any]) -> None:
    summary = GraphSummary.of_analysis(
        {"cells": {CELL: {"defs": [], "refs": [], "errors": [raw]}}, "edges": [["a", "b"]]}
    )
    assert summary.computed is True
    assert summary.cells[CELL].errors[0].model_dump() == expected
    assert summary.edges == [("a", "b")]


def test_a_pending_graph_says_it_is_not_computed() -> None:
    assert GraphSummary.pending().model_dump() == {"computed": False, "cells": {}, "edges": []}
