"""A refused tree report names the entries it refused, in either body shape.

A current server answers in the error envelope, with the refused positions in
``error.details``; a server older than the envelope answered flat, with them
in ``detail``. The holder drops exactly the named entries either way, and an
envelope that names none drops nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from alkera_cli.files.live_paths import refused_paths

pytestmark = [pytest.mark.spread]


@dataclass(frozen=True)
class Entry:
    path: str


CHUNK = [Entry("a.txt"), Entry("bad dir/readme.md"), Entry("readme.md")]


def _refusal(body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.test/tree")
    response = httpx.Response(409, json=body, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


def _envelope(details: dict[str, Any] | None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": "urn:alkera:error:files.tree_mismatch",
        "code": "files.tree_mismatch",
        "status": 409,
        "message": "refused",
        "trace_id": "t",
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_envelope({"indexes": [1]}), {"bad dir/readme.md"}, id="envelope"),
        pytest.param(
            {**_envelope({"indexes": [1]}), "detail": {"indexes": [0, 2]}},
            {"bad dir/readme.md"},
            id="envelope-before-the-flat-keys",
        ),
        pytest.param(
            {"code": "files.tree_mismatch", "detail": {"indexes": [0]}},
            {"a.txt"},
            id="flat-from-an-older-server",
        ),
        pytest.param(_envelope(None), set(), id="envelope-naming-nothing"),
    ],
)
def test_the_named_entries_are_the_refused_ones(body: dict[str, Any], expected: set[str]) -> None:
    assert refused_paths(_refusal(body), CHUNK) == expected
