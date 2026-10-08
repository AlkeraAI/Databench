"""The table sort syntax ``col:asc,col2:desc``: one reader, shared by the
platform's table page route and the agent's ``notebook.inspect``."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.engine.sort_spec import MAX_SORT_KEYS, parse_sort_spec


@pytest.mark.parametrize(
    ("raw", "keys"),
    [
        pytest.param(None, [], id="absent"),
        pytest.param("", [], id="empty"),
        pytest.param("a", [("a", False)], id="direction-defaults-to-asc"),
        pytest.param("a:desc", [("a", True)], id="desc"),
        pytest.param("a:ASC, b:Desc", [("a", False), ("b", True)], id="case-and-spaces"),
        pytest.param("a:desc,b", [("a", True), ("b", False)], id="mixed"),
        pytest.param("t:start:asc", [("t:start", False)], id="colon-inside-a-column"),
        pytest.param("a desc", [("a desc", False)], id="a-space-is-part-of-the-column"),
    ],
)
def test_a_sort_that_reads_becomes_ordered_kernel_keys(
    raw: str | None, keys: list[tuple[str, bool]]
) -> None:
    assert parse_sort_spec(raw) == [{"column": c, "descending": d} for c, d in keys]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("a:up", id="unknown-direction"),
        pytest.param(":desc", id="empty-column"),
        pytest.param("a,,b", id="empty-part"),
        pytest.param("x" * 257, id="column-too-long"),
        pytest.param(",".join(f"c{i}" for i in range(MAX_SORT_KEYS + 1)), id="seventeen-keys"),
    ],
)
def test_a_sort_that_does_not_read_is_refused(raw: Any) -> None:
    with pytest.raises(ValueError):
        parse_sort_spec(raw)


def test_sixteen_keys_are_admitted() -> None:
    raw = ",".join(f"c{i}" for i in range(MAX_SORT_KEYS))
    assert len(parse_sort_spec(raw)) == MAX_SORT_KEYS
