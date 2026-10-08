"""Reading the Files item wire, in whichever spelling a field arrives in.

`Item` camel-cases its own keys, but every facet hanging off it is a
`VersionedModel` with no alias generator — so a facet field reads back in
snake_case while its parent reads back in camelCase. One module owns that
asymmetry so a push and a pull cannot disagree about it, and so a facet that
grows an alias later does not silently turn a fast path off in one of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


def facet_content_hash(facet: Mapping[str, Any]) -> str | None:
    """The file facet's whole-file BLAKE3, or ``None`` when it carries none.

    ``None`` is not "the file is empty": it means the read this facet came from
    did not carry the hash at all, and the caller has to go ask the version
    list for it rather than conclude the local bytes differ.
    """
    for key in ("content_hash", "contentHash"):
        value = facet.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def item_etag(item: Mapping[str, Any]) -> str:
    """The item's etag, or ``""`` when the read carried none.

    ``etag`` is one word, so the camelCase alias generator leaves it as it is:
    the server spells it ``etag`` and never anything else. ``""`` is also what
    the server's own ``Item`` defaults it to, so an empty etag and a missing
    one are the same fact.
    """
    value = item.get("etag")
    return value if isinstance(value, str) else ""


def conflict_code(exc: BaseException) -> str | None:
    """The API's own error code behind a refused call, when the body names one.

    A Files refusal says what it is (``files.leased``, ``files.exists``, …) in
    ``code`` — at the top of the body or under ``error`` — and the SDK's error
    carries the same field. Read here once, so a status is never the only
    thing a caller reads: two 409s with different codes are different facts.
    """
    direct = getattr(exc, "code", None)
    if isinstance(direct, str) and direct:
        return direct
    response = getattr(exc, "response", None)
    if not isinstance(response, httpx.Response) or not response.content:
        return None
    try:
        body: Any = response.json()
    except ValueError:
        return None
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    source = nested if isinstance(nested, Mapping) else body
    code = source.get("code")
    return code if isinstance(code, str) and code else None
