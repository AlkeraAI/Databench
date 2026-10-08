"""Canonical result-blob envelope + pagination + the ``fetch_result`` tool.

Pins the contract every spilled handle relies on:
both producers write one envelope; `paginate` pages it by row or char with the
standard offset/has_more/next_offset metadata; a non-envelope blob still pages
as a raw char window so no handle is a dead end; and `fetch_result` surfaces a
clean error for a bad handle instead of crashing dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.result_blob import (
    DEFAULT_ROW_PAGE,
    DEFAULT_TEXT_PAGE,
    MAX_ROW_PAGE,
    MAX_TEXT_PAGE,
    ResultBlobEnvelope,
    paginate,
    write_rows_blob,
    write_text_blob,
)
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_core.project.chats.blobs import BlobStore
from alkera_core.project.directory import ProjectDirectory


def _blobs(tmp_path: Path) -> BlobStore:
    return ProjectDirectory(tmp_path / ".alkera").blobs()


def test_rows_blob_with_non_finite_floats_is_strict_json(tmp_path: Path) -> None:
    # sql.query writes its rows-blob BEFORE Tool.invoke's sanitize, so the blob
    # writer must wrap non-finite floats itself — otherwise the stored bytes hold
    # invalid NaN/Infinity tokens that break any strict re-reader of the blob.
    import json

    from alkera_core.json_safe import NONFINITE_KEY

    blobs = _blobs(tmp_path)
    handle = write_rows_blob(blobs, columns=["x"], rows=[[float("nan")], [float("inf")], [2.0]])

    raw = blobs.read(handle.sha256).decode()
    json.loads(raw)  # parses
    json.dumps(json.loads(raw), allow_nan=False)  # ...and is strict (no NaN token)

    page = paginate(blobs, handle.sha256, offset=0, limit=10)
    assert page["rows"] == [[{NONFINITE_KEY: "nan"}], [{NONFINITE_KEY: "inf"}], [2.0]]


# --- envelope round-trip + pagination --------------------------------------


def test_rows_blob_pages_by_row(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path)
    rows = [[i, f"r{i}"] for i in range(120)]
    handle = write_rows_blob(blobs, columns=["id", "name"], rows=rows)

    p0 = paginate(blobs, handle.sha256, offset=0, limit=50)
    assert p0["kind"] == "rows"
    assert p0["columns"] == ["id", "name"]
    assert p0["rows"] == rows[:50]
    assert p0["total"] == 120
    assert p0["returned"] == 50
    assert p0["has_more"] is True
    assert p0["next_offset"] == 50

    p2 = paginate(blobs, handle.sha256, offset=100, limit=50)
    assert p2["rows"] == rows[100:120]
    assert p2["returned"] == 20
    assert p2["has_more"] is False
    assert p2["next_offset"] is None


def test_text_blob_pages_by_char(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path)
    text = "abcdefghij" * 10  # 100 chars
    handle = write_text_blob(blobs, text=text)

    p0 = paginate(blobs, handle.sha256, offset=0, limit=40)
    assert p0["kind"] == "text"
    assert p0["text"] == text[:40]
    assert p0["total"] == 100
    assert p0["has_more"] is True and p0["next_offset"] == 40

    p_last = paginate(blobs, handle.sha256, offset=80, limit=40)
    assert p_last["text"] == text[80:]
    assert p_last["has_more"] is False and p_last["next_offset"] is None


def test_offset_past_end_is_empty_not_an_error(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path)
    handle = write_rows_blob(blobs, columns=["id"], rows=[[1], [2]])
    page = paginate(blobs, handle.sha256, offset=999, limit=50)
    assert page["rows"] == [] and page["returned"] == 0
    assert page["has_more"] is False and page["next_offset"] is None


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(None, DEFAULT_TEXT_PAGE, id="none->text-default"),
        pytest.param(0, DEFAULT_TEXT_PAGE, id="zero->text-default"),
        pytest.param(-5, DEFAULT_TEXT_PAGE, id="negative->text-default"),
        pytest.param(MAX_TEXT_PAGE + 100, MAX_TEXT_PAGE, id="over-cap->text-clamped"),
    ],
)
def test_text_limit_is_clamped(tmp_path: Path, limit: int | None, expected: int) -> None:
    blobs = _blobs(tmp_path)
    handle = write_text_blob(blobs, text="z" * (MAX_TEXT_PAGE * 2))
    page = paginate(blobs, handle.sha256, offset=0, limit=limit)
    assert page["limit"] == expected


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(None, DEFAULT_ROW_PAGE, id="none->row-default"),
        pytest.param(0, DEFAULT_ROW_PAGE, id="zero->row-default"),
        pytest.param(MAX_ROW_PAGE + 100, MAX_ROW_PAGE, id="over-cap->row-clamped"),
    ],
)
def test_row_limit_is_clamped(tmp_path: Path, limit: int | None, expected: int) -> None:
    blobs = _blobs(tmp_path)
    handle = write_rows_blob(blobs, columns=["i"], rows=[[i] for i in range(MAX_ROW_PAGE * 2)])
    page = paginate(blobs, handle.sha256, offset=0, limit=limit)
    assert page["limit"] == expected


def test_default_text_page_is_usable_not_50_chars(tmp_path: Path) -> None:
    # Regression: the generic dispatch spill is a TEXT envelope; a 50-char
    # default page would make a 64KiB result need >1300 fetches. The default
    # must page in a usable chunk.
    blobs = _blobs(tmp_path)
    handle = write_text_blob(blobs, text="z" * 20_000)
    assert paginate(blobs, handle.sha256)["returned"] == DEFAULT_TEXT_PAGE
    assert DEFAULT_TEXT_PAGE >= 1000


def test_opaque_blob_falls_back_to_char_window(tmp_path: Path) -> None:
    # A blob that is NOT a result envelope (a raw legacy/opaque payload) must
    # still be fetchable — every handle pages, nothing is a dead end.
    blobs = _blobs(tmp_path)
    sha, _ = blobs.write(b"just some opaque bytes, not an envelope")
    page = paginate(blobs, sha, offset=0, limit=10)
    assert page["kind"] == "text"
    assert page["text"] == "just some "
    assert page["total"] == 39 and page["has_more"] is True


def test_envelope_is_versioned() -> None:
    assert ResultBlobEnvelope(kind="text", text="x", total=1).schema_version == "1.0.0"


# --- the fetch_result tool -------------------------------------------------


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(_blobs(tmp_path))
    register_meta_tools(registry)
    register_blob_tools(registry)
    return registry


def test_fetch_result_is_hot() -> None:
    # It must be reachable without a search (handles come from any tool), so it
    # rides the always-on hot prefix, not the searchable long tail.
    from alkera_cli.plugins.plugin_base.blob_tool import FetchResultTool

    assert FetchResultTool.spec.hot is True


async def test_fetch_result_pages_a_rows_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["id"], rows=[[i] for i in range(60)])
    out = await registry.dispatch(
        "fetch_result", {"handle": handle.sha256, "offset": 50, "limit": 50}
    )
    assert out["kind"] == "rows"
    assert out["rows"] == [[i] for i in range(50, 60)]
    assert out["has_more"] is False


@pytest.mark.parametrize(
    ("char", "fills_the_limit"),
    [pytest.param("z", True, id="ascii"), pytest.param("あ", False, id="cjk")],
)
async def test_a_max_limit_page_comes_back_as_a_page_not_another_handle(
    tmp_path: Path, char: str, fills_the_limit: bool
) -> None:
    # A page is delivered inline, so it is bounded in bytes as well as characters: a full
    # limit of Japanese weighs ~150 KB, which the dispatch spill blobbed in turn, so the
    # model dereferenced a handle and was handed another handle. An ASCII page still runs
    # to the whole character limit, and either way paging keeps advancing.
    registry = _registry(tmp_path)
    text = char * (MAX_TEXT_PAGE * 2)
    handle = write_text_blob(registry._blobs, text=text)

    page = await registry.dispatch(
        "fetch_result", {"handle": handle.sha256, "limit": MAX_TEXT_PAGE}
    )
    assert page["kind"] == "text" and "blob" not in page
    assert (page["returned"] == MAX_TEXT_PAGE) is fills_the_limit
    assert page["text"] == text[: page["returned"]]
    assert page["has_more"] is True and page["next_offset"] == page["returned"]

    nxt = await registry.dispatch(
        "fetch_result",
        {"handle": handle.sha256, "offset": page["next_offset"], "limit": MAX_TEXT_PAGE},
    )
    assert nxt["returned"] > 0
    assert nxt["text"] == text[page["next_offset"] : page["next_offset"] + nxt["returned"]]


async def test_fetch_result_unknown_handle_is_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    valid_but_absent = "0" * 64
    out = await registry.dispatch("fetch_result", {"handle": valid_but_absent})
    assert "no result blob" in out["error"]


async def test_fetch_result_malformed_handle_is_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("fetch_result", {"handle": "not-a-sha"})
    assert "invalid blob handle" in out["error"]
