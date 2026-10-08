"""Every notebook route that can take more than the edge's 8 KB body rule is
declared where the edge's exemptions are generated from, with a cap no
smaller than the largest body its schema admits.

The box posts kernel events and document operations to these routes through
the same edge every person's browser goes through; one that is not declared
is refused at the edge with a 403 nobody sees, and the box resends it
forever. A route whose schema bounds nothing (a list or a text with no
maximum) must be declared all the same, and its cap is then what bounds it.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from alkera_core.notebooks import limits
from backend.api.body_limit import GUARDED_PATHS, GUARDED_PREFIXES
from backend.api.routes.notebooks import router as notebooks_router
from fastapi import FastAPI

#: The notebook routes, mounted as the product mounts them.
app = FastAPI()
app.include_router(notebooks_router)

EDGE_BODY_RULE_BYTES = 8 * 1024
NOTEBOOKS = "/api/v1/notebooks/"


def _bound(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> float:
    """The most bytes a JSON body valid for ``schema`` can weigh (``inf`` when
    nothing bounds it). Text counts six bytes a character (a ``\\uXXXX``)."""
    if depth > 40:
        return math.inf
    if "$ref" in schema:
        return _bound(defs[schema["$ref"].split("/")[-1]], defs, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return max(_bound(s, defs, depth + 1) for s in schema[key])
    if "allOf" in schema:
        return sum(_bound(s, defs, depth + 1) for s in schema["allOf"])
    kind = schema.get("type")
    if kind == "string":
        if "enum" in schema:
            return max(len(str(e)) + 2 for e in schema["enum"])
        if "maxLength" in schema:
            return 2 + 6 * schema["maxLength"]
        return math.inf
    if kind in ("integer", "number"):
        return 25
    if kind in ("boolean", "null"):
        return 5
    if kind == "array":
        if "maxItems" not in schema:
            return math.inf
        return 2 + schema["maxItems"] * (_bound(schema.get("items", {}), defs, depth + 1) + 2)
    if kind == "object" or "properties" in schema:
        if schema.get("additionalProperties") not in (None, False):
            return math.inf
        props = schema.get("properties", {})
        return 2 + sum(len(k) + 6 + _bound(v, defs, depth + 1) for k, v in props.items())
    return math.inf


def _notebook_bodies() -> list[tuple[str, str, float]]:
    spec = app.openapi()
    defs = spec.get("components", {}).get("schemas", {})
    found = []
    for path, operations in spec["paths"].items():
        if not path.startswith(NOTEBOOKS):
            continue
        for method, operation in operations.items():
            body = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if body is None:
                continue
            found.append((method.upper(), path, _bound(body["schema"], defs)))
    return found


def _concrete(path: str) -> str:
    """A request path for a route template (each parameter one segment)."""
    return "/".join("p" if part.startswith("{") else part for part in path.split("/"))


def _cap(method: str, path: str) -> int | None:
    concrete = _concrete(path)
    if concrete in GUARDED_PATHS:
        return 2**62
    caps = [g.max_bytes for g in GUARDED_PREFIXES if method in g.methods and g.matches(concrete)]
    return max(caps) if caps else None


@pytest.mark.parametrize(
    ("method", "path", "bound"),
    [
        pytest.param(m, p, b, id=f"{m} {p.removeprefix(NOTEBOOKS)}")
        for m, p, b in _notebook_bodies()
        if b > EDGE_BODY_RULE_BYTES
    ],
)
def test_a_notebook_route_that_can_pass_the_edge_rule_is_declared(
    method: str, path: str, bound: float
) -> None:
    cap = _cap(method, path)
    assert cap is not None, f"{method} {path} can take {bound} bytes and is not declared"
    if math.isfinite(bound):
        assert cap >= bound, f"{method} {path} admits {bound} bytes; its cap is {cap}"


def test_the_box_s_event_post_is_declared_at_the_notebooks_one_budget() -> None:
    assert _cap("POST", "/api/v1/notebooks/{drive_id}/{item_id}/events") == (
        limits.POST_BODY_MAX_BYTES
    )


def test_the_gate_reads_the_real_route_list() -> None:
    """Not vacuous: the routes a box posts large bodies to are among those
    it checks."""
    checked = {(m, p.removeprefix(NOTEBOOKS)) for m, p, b in _notebook_bodies() if b > 8192}
    assert ("POST", "{drive_id}/{item_id}/events") in checked
    assert ("POST", "{drive_id}/{item_id}/ops") in checked
