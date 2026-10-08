"""The Files wire readers and the whole-file hash, pinned against the server's
own item shape and against fixed BLAKE3 values."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.hashing import STREAM_CHUNK, content_hash, file_hash, stream_hash
from alkera_cli.files.wire import facet_content_hash, item_etag
from alkera_core.schemas.files.item import FileFacet, Item

#: The BLAKE3 reference vectors for the empty input and for ``abc``.
EMPTY = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
ABC = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"

#: Two and a half reads plus a few bytes, so the stream crosses read
#: boundaries and ends on a short one.
LONG = bytes(i % 251 for i in range(STREAM_CHUNK * 5 // 2 + 7))
LONG_HASH = "88aec6118d3eb8bf68e79e1f991aa4ecd723e6896e2615d968ea136c2302d303"


def _served(**fields: Any) -> dict[str, Any]:
    """An item exactly as the server writes it on the wire."""
    return Item(id="n1", **fields).model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# The content hash a facet carries
# ---------------------------------------------------------------------------


def test_a_served_facet_with_a_hash_reads_as_that_hash() -> None:
    item = _served(file=FileFacet(content_hash=ABC, size=3))
    assert facet_content_hash(item["file"]) == ABC


@pytest.mark.parametrize(
    "facet",
    [
        pytest.param(_served(file=FileFacet(size=3))["file"], id="served-empty-hash"),
        pytest.param({"size": 3}, id="no-hash-key"),
        pytest.param({"content_hash": None}, id="null-hash"),
        pytest.param({"contentHash": ""}, id="empty-camel-hash"),
    ],
)
def test_a_facet_with_no_hash_is_unknown_not_empty(facet: dict[str, Any]) -> None:
    """No hash is ``None``, never ``""``: the caller asks the version list
    rather than conclude the bytes differ (or match)."""
    assert facet_content_hash(facet) is None


@pytest.mark.parametrize(
    "key", [pytest.param("content_hash", id="snake"), pytest.param("contentHash", id="camel")]
)
def test_a_facet_hash_reads_in_either_spelling(key: str) -> None:
    assert facet_content_hash({key: ABC}) == ABC


# ---------------------------------------------------------------------------
# The etag an item carries
# ---------------------------------------------------------------------------


def test_the_served_etag_is_read_under_the_key_the_server_writes() -> None:
    """``etag`` is one word, so the camel alias generator leaves it alone; if
    the server ever renamed it, this reader would read nothing and this fails."""
    item = _served(etag="7")
    assert item_etag(item) == "7"


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(_served(), id="served-default"),
        pytest.param({"id": "n1"}, id="missing"),
        pytest.param({"id": "n1", "etag": None}, id="null"),
        pytest.param({"id": "n1", "eTag": "7"}, id="a-spelling-the-server-never-sends"),
    ],
)
def test_an_item_with_no_etag_reads_as_empty(item: dict[str, Any]) -> None:
    assert item_etag(item) == ""


# ---------------------------------------------------------------------------
# The whole-file hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(b"", EMPTY, id="empty"),
        pytest.param(b"abc", ABC, id="abc"),
        pytest.param(LONG, LONG_HASH, id="many-reads"),
    ],
)
def test_the_hash_is_blake3_whether_held_streamed_or_read_from_disk(
    data: bytes, expected: str, tmp_path: Path
) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(data)

    assert content_hash(data) == expected
    assert stream_hash(io.BytesIO(data)) == (expected, len(data))
    assert file_hash(path) == (expected, len(data))


def test_a_stream_is_hashed_from_where_it_stands() -> None:
    handle = io.BytesIO(b"xxabc")
    handle.seek(2)
    assert stream_hash(handle) == (ABC, 3)
