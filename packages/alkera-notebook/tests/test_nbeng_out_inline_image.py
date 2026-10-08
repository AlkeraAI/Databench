"""Finding a raster image a cell carries inline by the hash a stored copy of
it would have: the hash ``notebook.show_output`` names an image by."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from alkera_notebook.outputs import InlineImage, inline_image, stored_output

PNG = b"\x89PNG\r\n\x1a\nfirst"
JPEG = b"\xff\xd8\xff\xe0second"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


BUNDLES: list[dict[str, Any]] = [
    {"text/plain": "<Figure>", "image/png": _b64(PNG)},
    {"image/jpeg": _b64(JPEG)},
]


@pytest.mark.parametrize(
    ("data", "mime"),
    [pytest.param(PNG, "image/png", id="png"), pytest.param(JPEG, "image/jpeg", id="jpeg")],
)
def test_an_image_is_found_by_the_hash_of_its_bytes(data: bytes, mime: str) -> None:
    assert inline_image(BUNDLES, _sha(data)) == InlineImage(mime=mime, data=data)


def test_the_hash_is_the_one_a_stored_copy_is_named_by() -> None:
    assert stored_output("image/png", _b64(PNG)).sha256 == _sha(PNG)


@pytest.mark.parametrize(
    "bundles",
    [
        pytest.param([{"image/svg+xml": "<svg/>"}], id="markup-is-never-an-image"),
        pytest.param(
            [{"image/png": {"application/vnd.alkera.ref+json": {"sha256": _sha(PNG)}}}],
            id="a-reference-is-not-the-bytes",
        ),
        pytest.param([{"text/plain": _b64(PNG)}], id="text-that-looks-like-an-image"),
        pytest.param([], id="no-outputs"),
    ],
)
def test_nothing_but_an_inline_raster_is_found(bundles: list[dict[str, Any]]) -> None:
    sha = _sha(b"<svg/>") if "image/svg+xml" in (bundles[0] if bundles else {}) else _sha(PNG)
    assert inline_image(bundles, sha) is None


def test_an_image_no_bundle_holds_is_not_found() -> None:
    assert inline_image(BUNDLES, _sha(b"another image")) is None
