"""Every Files route whose valid body can cross the edge's 8 KB rule is guarded.

The production WAF re-blocks the CRS ``SizeRestrictions_BODY`` label (8 KB) on
every backend path the generated exempt list does not name, and the exempt list
is :data:`backend.api.body_limit.GUARDED_PREFIXES` / ``GUARDED_PATHS``. A route
missing from it works on every stack without the WAF -- local, CI, self-hosted --
and 403s opaquely in production once a body passes 8 KB. These tests derive the
largest body each route's request schema admits and hold the guard table to it,
and pin the lease plane's caps to the bounds they were computed from.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import uuid
from typing import Annotated, Any

import pytest
from alkera_core.config import Settings
from alkera_core.files.lease_live import SETTLED_STATES
from alkera_core.models.files.leases import LIVE_ENTRY_STATES, FileLease
from alkera_core.schemas.files.lease_tree import TREE_MAX_BODY_BYTES
from backend.api.body_limit import (
    _BIGINT_DIGITS,
    _DIGEST_MAX_BYTES,
    _HEARTBEAT_BATCH_ENTRIES,
    _HEARTBEAT_BATCH_MAX_BYTES,
    _LEASE_INSTANCE_ID_CHARS,
    _LIVE_BATCH_ENTRIES,
    _LIVE_BATCH_MAX_BYTES,
    _LIVE_DISPLACED_CHARS,
    _LIVE_STATE_CHARS,
    GUARDED_PREFIXES,
    edge_exempt_paths,
)
from backend.api.extension_points import signed_bodies
from backend.api.routes.files.leases import (
    HEARTBEAT_BATCH_MAX,
    HeartbeatBatchBody,
    LiveBatchBody,
    LiveReportBody,
)
from pydantic import BaseModel, Field
from tests._suite_app import app

#: The CRS body rule the edge re-blocks off the exempt list.
EDGE_BODY_LIMIT = 8 * 1024

#: The most bytes any JSON encoder spends on one character of free text: a code
#: point outside the BMP escaped as a surrogate pair.
_TEXT_CHAR_BYTES = 12
#: A character outside the BMP: a Python ``str`` of length one that
#: ``json.dumps`` spells as twelve bytes.
_WIDEST_CHAR = "\U0001f600"
_BIGINT_MAX = 2**63 - 1


# ---------------------------------------------------------------------------
# The largest body a JSON schema admits
# ---------------------------------------------------------------------------


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _max_bytes(schema: dict[str, Any], defs: dict[str, Any]) -> int | None:
    """The most bytes a value valid against ``schema`` can serialize to, or
    ``None`` when the schema sets no bound (a string with no ``maxLength``, a
    list with no ``maxItems``, a free-form object). Every member of an object is
    counted as present, free text at the widest escape, separators at the wider
    ``", "`` / ``": "`` spelling."""
    schema = _resolve(schema, defs)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            sizes = [_max_bytes(branch, defs) for branch in schema[key]]
            return None if None in sizes else max(s for s in sizes if s is not None)
    if "const" in schema:
        return len(json.dumps(schema["const"]))
    if "enum" in schema:
        return max(len(json.dumps(value)) for value in schema["enum"])
    kind = schema.get("type")
    if kind == "string":
        if schema.get("format") == "uuid":
            return 38
        if "maxLength" in schema:
            return 2 + _TEXT_CHAR_BYTES * int(schema["maxLength"])
        return None
    if kind == "integer":
        # A sign only where the schema admits a negative value.
        signed = float(schema.get("minimum", -1)) < 0
        return int(signed) + _BIGINT_DIGITS
    if kind == "boolean":
        return len("false")
    if kind == "null":
        return len("null")
    if kind == "array":
        count = schema.get("maxItems")
        item = _max_bytes(schema.get("items", {}), defs)
        if count is None or item is None:
            return None
        return 2 + count * item + 2 * max(count - 1, 0)
    properties = schema.get("properties")
    if kind == "object" and properties:
        total = 2 + 2 * (len(properties) - 1)
        for name, member in properties.items():
            size = _max_bytes(member, defs)
            if size is None:
                return None
            total += len(json.dumps(name)) + 2 + size
        return total
    return None


def _carries_a_list(schema: dict[str, Any], defs: dict[str, Any], seen: frozenset[str]) -> bool:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return name not in seen and _carries_a_list(defs[name], defs, seen | {name})
    if schema.get("type") == "array":
        return True
    branches = [b for key in ("anyOf", "oneOf", "allOf") for b in schema.get(key, [])]
    branches += list(schema.get("properties", {}).values())
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        branches.append(extra)
    return any(_carries_a_list(branch, defs, seen) for branch in branches)


class _Bounded(BaseModel):
    names: list[Annotated[str, Field(max_length=4)]] = Field(max_length=3)
    label: str = Field(max_length=5)
    count: int
    on: bool


class _Unbounded(BaseModel):
    names: list[str]


def test_the_derived_bound_covers_the_widest_valid_body() -> None:
    """The walker the contract rests on: a value at every schema maximum, free
    text in the widest escape, must serialize within the derived bound -- and a
    list with no length bound derives none, rather than a number that looks safe."""
    schema = _Bounded.model_json_schema()
    bound = _max_bytes(schema, schema.get("$defs", {}))
    widest = _Bounded(
        names=[_WIDEST_CHAR * 4] * 3, label=_WIDEST_CHAR * 5, count=-_BIGINT_MAX, on=False
    )
    encoded = json.dumps(widest.model_dump(), ensure_ascii=True)
    assert bound is not None
    assert len(encoded) == bound
    unbounded = _Unbounded.model_json_schema()
    assert _max_bytes(unbounded, {}) is None


def _files_json_bodies() -> list[Any]:
    spec = app.openapi()
    defs = spec["components"]["schemas"]
    cases = []
    for path, operations in spec["paths"].items():
        if not path.startswith("/api/v1/files/"):
            continue
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            schema = content.get("application/json", {}).get("schema")
            if schema is None:
                continue
            cases.append(
                pytest.param(method.upper(), path, schema, defs, id=f"{method.upper()} {path}")
            )
    return cases


def _guard_cap(method: str, route_path: str) -> int | None:
    """The cap the edge exemption carries for this route, or ``None`` when the
    edge does not exempt it (and so 403s its body past 8 KB)."""
    sample = re.sub(r"\{[^}]+\}", "x1", route_path)
    if sample in edge_exempt_paths(signed_bodies()):
        return 0
    caps = [p.max_bytes for p in GUARDED_PREFIXES if p.matches(sample) and method in p.methods]
    return min(caps) if caps else None


FILES_JSON_BODIES = _files_json_bodies()


def test_the_contract_sees_the_lease_plane() -> None:
    """The enumeration reaches the routes it exists for; an OpenAPI change that
    hid them would otherwise make every case below vacuous."""
    ids = {case.id for case in FILES_JSON_BODIES}
    for route in (
        "POST /api/v1/files/drives/{drive_id}/items/{item_id}/lease/live",
        "POST /api/v1/files/drives/{drive_id}/items/{item_id}/lease/tree/digests",
        "POST /api/v1/files/drives/{drive_id}/leases/heartbeat",
    ):
        assert route in ids


@pytest.mark.parametrize(("method", "path", "schema", "defs"), FILES_JSON_BODIES)
def test_a_files_body_that_can_cross_the_edge_rule_is_exempt_and_capped(
    method: str, path: str, schema: dict[str, Any], defs: dict[str, Any]
) -> None:
    """A route whose schema admits a body past 8 KB -- a derived bound above it,
    or a list the schema does not bound -- must be exempt at the edge, and its
    cap must admit the largest body the schema admits. Scalar-only bodies with an
    unbounded string are left to the edge: nothing a client sends there
    legitimately runs to 8 KB."""
    bound = _max_bytes(schema, defs)
    crosses = (bound is None and _carries_a_list(schema, defs, frozenset())) or (
        bound is not None and bound > EDGE_BODY_LIMIT
    )
    cap = _guard_cap(method, path)
    if crosses:
        assert cap is not None, (
            f"{method} {path} admits a body past the edge's 8 KB rule but is not in "
            "GUARDED_PREFIXES, so production 403s it: add a pattern capped at its "
            "largest valid body and run `make gen-waf-paths`"
        )
    if cap and bound is not None:
        assert bound <= cap, f"{method} {path}: cap {cap} refuses a valid {bound}-byte body"


# ---------------------------------------------------------------------------
# The lease plane's caps against the bounds they were computed from
# ---------------------------------------------------------------------------


def test_the_restated_bounds_match_their_sources() -> None:
    """The caps are computed from literals so the generated WAF artifact never
    depends on the environment; each literal is the bound its route enforces."""
    assert _HEARTBEAT_BATCH_ENTRIES == HEARTBEAT_BATCH_MAX
    assert _LIVE_BATCH_ENTRIES == Settings.model_fields["files_live_max_batch_entries"].default
    assert _LEASE_INSTANCE_ID_CHARS == FileLease.__table__.c.holder_instance_id.type.length
    displaced = LiveReportBody.model_json_schema()["properties"]["displaced"]["anyOf"][0]
    assert _LIVE_DISPLACED_CHARS == displaced["maxLength"]
    assert _LIVE_STATE_CHARS == max(len(s) for s in (*LIVE_ENTRY_STATES, *SETTLED_STATES))
    assert _DIGEST_MAX_BYTES > TREE_MAX_BODY_BYTES


def _encoded(body: BaseModel) -> int:
    return len(json.dumps(body.model_dump(mode="json", by_alias=True), ensure_ascii=True))


def test_the_widest_batched_beat_is_exactly_the_cap() -> None:
    """A full batch of beats with every field at its widest validates, and the
    cap is its exact size: no valid beat is refused, nothing past one is admitted."""
    entry = {
        "nodeId": str(uuid.uuid4()),
        "epoch": _BIGINT_MAX,
        "instanceId": _WIDEST_CHAR * _LEASE_INSTANCE_ID_CHARS,
        "synced": False,
    }
    body = HeartbeatBatchBody.model_validate({"leases": [entry] * HEARTBEAT_BATCH_MAX})
    assert _encoded(body) == _HEARTBEAT_BATCH_MAX_BYTES


def test_a_full_live_batch_at_its_widest_fits_the_cap() -> None:
    entry = {
        "nodeId": str(uuid.uuid4()),
        "state": "inbound_rename",
        "boxSize": _BIGINT_MAX,
        "boxMtime": "2026-09-25T12:34:56.123456+14:00",
        "displaced": _WIDEST_CHAR * _LIVE_DISPLACED_CHARS,
    }
    body = LiveBatchBody.model_validate({"entries": [entry] * _LIVE_BATCH_ENTRIES})
    assert EDGE_BODY_LIMIT < _encoded(body) <= _LIVE_BATCH_MAX_BYTES


@pytest.mark.parametrize(
    "entries",
    [pytest.param(70, id="seventy-chats"), pytest.param(HEARTBEAT_BATCH_MAX, id="full")],
)
def test_an_ordinary_batched_beat_crosses_the_edge_rule(entries: int) -> None:
    """Not a pathological body: the beat a box with seventy chats sends, ids as
    the CLI mints them, already passes 8 KB -- which is why it needs the exemption."""
    entry = {
        "nodeId": str(uuid.uuid4()),
        "epoch": 4_294_967_297,
        "instanceId": uuid.uuid4().hex,
        "synced": True,
    }
    body = HeartbeatBatchBody.model_validate({"leases": [entry] * entries})
    assert EDGE_BODY_LIMIT < _encoded(body) <= _HEARTBEAT_BATCH_MAX_BYTES


def test_an_incompressible_digest_walk_at_the_decoded_ceiling_fits_the_cap() -> None:
    """The digest route holds the DECODED body to the tree ceiling; gzip can only
    add to an incompressible one, and the cap admits that worst case."""
    raw = os.urandom(TREE_MAX_BODY_BYTES)
    assert TREE_MAX_BODY_BYTES < len(gzip.compress(raw, compresslevel=9)) <= _DIGEST_MAX_BYTES
