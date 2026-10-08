"""The generated cross-tenant route matrix: every operation, aimed at org A from org B.

One person belongs to two orgs. Their credential for org B must reach nothing of
org A's, on every route the app serves, including the route someone adds next
week. This module builds everything that sentence needs, and
``test_cross_tenant_routes.py`` asserts it:

* **The operations** come from the app's own OpenAPI document, so a new route
  joins the matrix the moment it is served. Excluded: the platform-staff
  surfaces (``/api/v1/admin``, ``/admin/v1``), ``/health``, and the
  ``/api/v1/auth`` routes that take no credential (sign-in, sign-up, the device
  grant's public half). Routes left out of the document (``include_in_schema``
  off) authenticate with something other than a member's credential (SCIM
  bearer, Slack and webhook signatures) and are pinned by name in
  :data:`OUTSIDE_THE_DOCUMENT`, so a new hidden route is a decision, not an
  accident.
* **The world** (:class:`TwoOrgWorld`) is the two-org identity from the suite's
  factory with one object of each kind seeded in A and in B. Seeds live one
  module per area, ``seed_<area>.py``, each exporting
  ``async def seed(session, world, side) -> dict[str, str]`` (kind -> id). They
  run in module-name order; a seed may read kinds an earlier module seeded from
  ``side.objects``.
* **The parameter table** maps a path or query parameter to org A's object of
  that kind. It is split one module per area, ``params_<area>.py``, each
  exporting any of ``PARAM_OBJECTS`` (bare parameter name -> factory),
  ``SCOPED_PARAM_OBJECTS`` (``(path prefix, name)`` -> factory, for a name two
  areas use for different kinds; the longest matching prefix wins),
  ``NOT_TENANT_PARAMS`` (name -> why it names no tenant object) and
  ``CALLER_SCOPED_OPERATIONS`` (operation -> why a 2xx on a foreign id is
  correct: the id keys a record in the caller's own org, never a lookup). The
  merge refuses a name two modules claim. An id parameter in none of them
  fails its operation by name.

There is no list of operations allowed to fail: every operation must hold, so a
new route that crosses orgs fails the matrix outright.

Each area owns its own two files, so the changes that extend the matrix never
edit the same one.
"""

from __future__ import annotations

import importlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from alkera_core.db.base import Base
from backend.api.extension_points import HTTP_ROUTERS
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests._tenancy_architecture_scan import AREA_NAMES, area_of
from tests.conftest import TwoOrg

HERE: Final = Path(__file__).parent

#: Prefixes the matrix never drives: platform staff and liveness probes.
EXCLUDED_PREFIXES: Final = ("/api/v1/admin/", "/admin/v1/", "/health/")

#: The prefix whose credential-free routes (signing in, signing up, resetting a
#: password) are excluded: there is no org to cross before a credential exists.
AUTH_PREFIX: Final = "/api/v1/auth/"

#: The dependencies that resolve a member's credential. An ``/api/v1/auth``
#: route whose dependency tree holds none of them takes no credential.
CREDENTIAL_DEPENDENCIES: Final = frozenset(
    {
        "current_user",
        "current_principal",
        "current_member",
        "current_org",
        "principal_user",
        "require_browser_session",
    }
)

#: Routes the platform serves outside the OpenAPI document, with what
#: authenticates them instead of a member's credential. Pinned so a hidden route
#: is a choice. An installed extension's hidden routes are its own to pin
#: (:func:`extension_hidden_operations`).
OUTSIDE_THE_DOCUMENT: Final[Mapping[str, str]] = {
    "POST /api/v1/compute/shutdown-notices": "the provider's signed notice; no member credential",
    "GET /api/v1/scim/v2/Groups": "SCIM bearer token; the identity suites cover it",
    "GET /api/v1/scim/v2/ResourceTypes": "SCIM bearer token; the identity suites cover it",
    "GET /api/v1/scim/v2/Schemas": "SCIM bearer token; the identity suites cover it",
    "GET /api/v1/scim/v2/ServiceProviderConfig": "SCIM bearer token; the identity suites cover it",
    "GET /api/v1/scim/v2/Users": "SCIM bearer token; the identity suites cover it",
    "POST /api/v1/scim/v2/Users": "SCIM bearer token; the identity suites cover it",
    "GET /api/v1/scim/v2/Users/{raw_id}": "SCIM bearer token; the identity suites cover it",
    "PUT /api/v1/scim/v2/Users/{raw_id}": "SCIM bearer token; the identity suites cover it",
    "PATCH /api/v1/scim/v2/Users/{raw_id}": "SCIM bearer token; the identity suites cover it",
    "DELETE /api/v1/scim/v2/Users/{raw_id}": "SCIM bearer token; the identity suites cover it",
}

#: Operations the matrix serves the schema but cannot call in-process, and why.
NOT_CALLABLE: Final[Mapping[str, str]] = {
    "GET /api/v1/events": (
        "an event stream answers by never ending; the realtime suites drive it per event"
    ),
}

#: Operations that answer the person's own list of orgs by design, so an answer
#: to B may name org A's id (the two-org person is a member of A), and never any
#: other id of A's. Each with why.
NAMES_THE_CALLERS_ORGS: Final[Mapping[str, str]] = {
    "GET /api/v1/auth/memberships": (
        "the org chooser and switcher: the identity's own memberships, each org once"
    ),
}

#: Statuses a refused cross-tenant call may answer. Anything else on a route
#: whose path names org A's object is a finding.
REFUSALS: Final = frozenset({401, 403, 404, 422})

#: Tables whose org-A rows a refused call may legitimately add, with why. The
#: authorization decision row is filed under the org whose resource was probed,
#: and it is platform-visible only (``visibility='platform'``), so it is
#: excluded by predicate rather than by table.
_UNCOUNTED_TABLES: Final[Mapping[str, str]] = {}

_PARAM = re.compile(r"{([^}]+)}")


# --------------------------------------------------------------------------
# the world
# --------------------------------------------------------------------------


@dataclass
class Side:
    """One org of the world, as the seeds see it."""

    org_id: uuid.UUID
    #: The org's admin (not the two-org person).
    admin: Any
    #: The two-org person: a member of both orgs.
    member: Any
    token: str
    objects: dict[str, str] = field(default_factory=dict)


@dataclass
class TwoOrgWorld(TwoOrg):
    """The two-org identity with one object of each kind in each org.

    ``a`` and ``b`` map a kind (``"kb_item"``, ``"object"``, ``"file"``, ...)
    to that org's object id, as the string a path carries."""

    a: dict[str, str] = field(default_factory=dict)
    b: dict[str, str] = field(default_factory=dict)

    def side_a(self) -> Side:
        return Side(self.org_a, self.admin_a, self.user, self.token_a, self.a)

    def side_b(self) -> Side:
        return Side(self.org_b, self.admin_b, self.user, self.token_b, self.b)

    def needles(self, *, names_callers_orgs: bool = False) -> set[str]:
        """Every string that would show org A's data in an answer to B: A's
        object ids (dashed and bare hex), A's org id and A's admin. The two-org
        person's own id is no needle: B knows its own member. With
        ``names_callers_orgs`` (an operation in :data:`NAMES_THE_CALLERS_ORGS`)
        A's org id is no needle either, and everything else of A's still is."""
        ids = {*self.a.values(), str(self.admin_a.id)}
        if not names_callers_orgs:
            ids.add(str(self.org_a))
        found: set[str] = set()
        for value in ids:
            found.add(value)
            try:
                found.add(uuid.UUID(value).hex)
            except ValueError:
                pass
        return found


Factory = Callable[[TwoOrgWorld], str]
Seeder = Callable[[AsyncSession, TwoOrgWorld, Side], Any]


def _area_modules(prefix: str) -> list[ModuleType]:
    """``<prefix><area>.py`` beside this file, imported in name order. Only area
    names count, so a stray file cannot join the matrix by its name alone."""
    found = []
    for path in sorted(HERE.glob(f"{prefix}*.py")):
        area = path.stem.removeprefix(prefix)
        if area not in AREA_NAMES:
            raise AssertionError(f"{path.name}: {area!r} is not a tenancy area")
        found.append(importlib.import_module(path.stem))
    return found


@dataclass(frozen=True)
class ParamTable:
    """Every area's parameter declarations, merged."""

    bare: Mapping[str, Factory]
    scoped: Mapping[tuple[str, str], Factory]
    not_tenant: Mapping[str, str]
    caller_scoped: Mapping[str, str]
    conflicts: tuple[str, ...]

    def factory_for(self, path: str, name: str) -> Factory | None:
        scoped = [
            (prefix, factory)
            for (prefix, param), factory in self.scoped.items()
            if param == name and path.startswith(prefix)
        ]
        if scoped:
            return max(scoped, key=lambda pair: len(pair[0]))[1]
        return self.bare.get(name)

    def is_tenant(self, name: str) -> bool:
        return name not in self.not_tenant


def _merge(modules: Iterable[ModuleType]) -> ParamTable:
    bare: dict[str, Factory] = {}
    scoped: dict[tuple[str, str], Factory] = {}
    not_tenant: dict[str, str] = {}
    caller_scoped: dict[str, str] = {}
    owners: dict[tuple[str, object], str] = {}
    conflicts: list[str] = []

    def claim(kind: str, key: object, module: ModuleType) -> bool:
        holder = owners.setdefault((kind, key), module.__name__)
        if holder != module.__name__:
            conflicts.append(f"{kind} {key!r} is claimed by {holder} and {module.__name__}")
            return False
        return True

    for module in modules:
        for name, factory in getattr(module, "PARAM_OBJECTS", {}).items():
            if claim("param", name, module) and claim("name", name, module):
                bare[name] = factory
        for key, factory in getattr(module, "SCOPED_PARAM_OBJECTS", {}).items():
            if claim("scoped", key, module):
                scoped[key] = factory
        for name, reason in getattr(module, "NOT_TENANT_PARAMS", {}).items():
            if claim("name", name, module):
                not_tenant[name] = reason
        for operation, reason in getattr(module, "CALLER_SCOPED_OPERATIONS", {}).items():
            if claim("caller_scoped", operation, module):
                caller_scoped[operation] = reason
    return ParamTable(bare, scoped, not_tenant, caller_scoped, tuple(conflicts))


PARAMS: Final = _merge(_area_modules("params_"))
SEEDS: Final[tuple[Seeder, ...]] = tuple(module.seed for module in _area_modules("seed_"))


async def build_world(session: AsyncSession, base: TwoOrg) -> TwoOrgWorld:
    """``base`` with every area's seeds run for org A, then org B."""
    world = TwoOrgWorld(**vars(base))
    for side in (world.side_a(), world.side_b()):
        for seed in SEEDS:
            made = await seed(session, world, side)
            side.objects.update({kind: str(value) for kind, value in made.items()})
    await session.commit()
    return world


# --------------------------------------------------------------------------
# the operations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Parameter:
    name: str
    where: str
    required: bool
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class Operation:
    """One method on one path, as the OpenAPI document describes it."""

    method: str
    path: str
    parameters: tuple[Parameter, ...]
    body_schema: Mapping[str, Any] | None
    body_kind: str
    area: str

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def path_params(self) -> tuple[str, ...]:
        return tuple(_PARAM.findall(self.path))

    def __str__(self) -> str:
        return self.name


def _walk_calls(dependant: Any, found: set[str]) -> None:
    call = getattr(dependant, "call", None)
    if call is not None:
        found.add(getattr(call, "__name__", ""))
    for sub in dependant.dependencies:
        _walk_calls(sub, found)


def takes_credential(route: APIRoute) -> bool:
    calls: set[str] = set()
    _walk_calls(route.dependant, calls)
    return bool(calls & CREDENTIAL_DEPENDENCIES)


def _body(operation: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str]:
    content = (operation.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        return content["application/json"].get("schema") or {}, "json"
    if "multipart/form-data" in content:
        return content["multipart/form-data"].get("schema") or {}, "multipart"
    if "application/x-www-form-urlencoded" in content:
        return content["application/x-www-form-urlencoded"].get("schema") or {}, "form"
    if content:
        return None, "bytes"
    return None, "none"


def _routes_by_operation() -> dict[str, APIRoute]:
    found: dict[str, APIRoute] = {}
    for route in fastapi_app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found[f"{method} {route.path_format}"] = route
    return found


def hidden_operations() -> set[str]:
    """Every operation the app serves but leaves out of the document."""
    return {
        name
        for name, route in _routes_by_operation().items()
        if not route.include_in_schema and not route.path.startswith(EXCLUDED_PREFIXES)
    }


def extension_hidden_operations() -> set[str]:
    """The hidden operations an installed extension's routers serve."""
    endpoints = {
        getattr(route, "endpoint", None)
        for mount in HTTP_ROUTERS.items()
        for route in mount.router.routes
    }
    routes = _routes_by_operation()
    return {name for name in hidden_operations() if routes[name].endpoint in endpoints}


def collect_operations() -> tuple[Operation, ...]:
    """Every operation in the app's OpenAPI document the matrix drives."""
    spec = fastapi_app.openapi()
    routes = _routes_by_operation()
    found: list[Operation] = []
    for path, item in spec.get("paths", {}).items():
        if path.startswith(EXCLUDED_PREFIXES):
            continue
        for method, operation in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            name = f"{method.upper()} {path}"
            route = routes[name]
            if path.startswith(AUTH_PREFIX) and not takes_credential(route):
                continue
            params = tuple(
                Parameter(
                    name=raw["name"],
                    where=raw["in"],
                    required=bool(raw.get("required")),
                    schema=raw.get("schema") or {},
                )
                for raw in [*item.get("parameters", []), *operation.get("parameters", [])]
            )
            schema, kind = _body(operation)
            found.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    parameters=params,
                    body_schema=schema,
                    body_kind=kind,
                    area=area_of(route.endpoint.__module__),
                )
            )
    return tuple(sorted(found, key=lambda op: (op.path, op.method)))


OPERATIONS: Final = collect_operations()


# --------------------------------------------------------------------------
# minimal valid requests
# --------------------------------------------------------------------------


def _resolve(schema: Any, defs: Mapping[str, Any]) -> Any:
    seen = 0
    while isinstance(schema, Mapping) and "$ref" in schema and seen < 32:
        schema = defs.get(str(schema["$ref"]).rsplit("/", 1)[-1], {})
        seen += 1
    return schema


def minimal_value(
    schema: Any,
    defs: Mapping[str, Any],
    *,
    name: str,
    ids: Mapping[str, str],
    depth: int = 0,
) -> Any:
    """The smallest value ``schema`` accepts, with org A's id in any field whose
    name is a mapped parameter. Only the JSON-Schema subset FastAPI emits."""
    schema = _resolve(schema, defs)
    if not isinstance(schema, Mapping) or depth > 6:
        return None
    if name in ids and schema.get("type") in (None, "string"):
        return ids[name]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, defs).get("type") != "null"]
            if not options:
                return None
            return minimal_value(options[0], defs, name=name, ids=ids, depth=depth + 1)
    if "allOf" in schema:
        return minimal_value(schema["allOf"][0], defs, name=name, ids=ids, depth=depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            key: minimal_value(sub, defs, name=key, ids=ids, depth=depth + 1)
            for key, sub in (schema.get("properties") or {}).items()
            if key in set(schema.get("required") or ())
        }
    if kind == "array":
        floor = int(schema.get("minItems") or 0)
        item = schema.get("items") or {}
        return [minimal_value(item, defs, name=name, ids=ids, depth=depth + 1)] * floor
    if kind == "integer":
        return int(schema.get("minimum", schema.get("exclusiveMinimum", -1) + 1) or 1)
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return str(uuid.uuid4())
        if fmt == "email":
            return "nobody@example.com"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt in ("uri", "url"):
            return "https://example.com/"
        if fmt == "binary":
            return "x"
        floor = int(schema.get("minLength") or 1)
        return "x" * max(1, floor)
    return None


@dataclass(frozen=True)
class Request:
    """One call, ready to send, and the org-A ids it carried."""

    method: str
    url: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: Any
    body_kind: str
    carried: frozenset[str]
    #: Whether a path parameter names org A's object.
    aimed: bool
    unmapped: tuple[str, ...]


def _id_table(op: Operation, world: TwoOrgWorld) -> dict[str, str]:
    """Every parameter name this operation's path makes resolvable, to A's id."""
    names = {p.name for p in op.parameters}
    defs_names: set[str] = set(names)
    defs_names |= set(PARAMS.bare)
    defs_names |= {param for (_prefix, param) in PARAMS.scoped}
    table: dict[str, str] = {}
    for name in defs_names:
        factory = PARAMS.factory_for(op.path, name)
        if factory is not None:
            table[name] = factory(world)
    return table


def build_request(op: Operation, world: TwoOrgWorld) -> Request:
    defs = fastapi_app.openapi().get("components", {}).get("schemas", {})
    ids = _id_table(op, world)
    carried: set[str] = set()
    unmapped: list[str] = []
    url = op.path
    aimed = False
    for name in op.path_params:
        if name in ids:
            value = ids[name]
            carried.add(value)
            aimed = True
        elif PARAMS.is_tenant(name):
            unmapped.append(name)
            value = str(uuid.uuid4())
        else:
            param = next((p for p in op.parameters if p.name == name), None)
            value = str(
                minimal_value(param.schema if param else {}, defs, name=name, ids={}) or "x"
            )
        url = url.replace("{" + name + "}", value)
    query: dict[str, str] = {}
    headers: dict[str, str] = {
        # Preconditions some routes check before anything else; a refusal for a
        # missing header never reaches the org boundary under test.
        "Idempotency-Key": uuid.uuid4().hex,
        "X-Alkera-Lease-Epoch": "1",
        "X-Alkera-Lease-Instance": uuid.uuid4().hex,
    }
    if op.method != "GET":
        headers["If-Match"] = '"1"'
    for param in op.parameters:
        if param.where == "query":
            if param.name in ids:
                query[param.name] = ids[param.name]
                carried.add(ids[param.name])
            elif param.required:
                value = minimal_value(param.schema, defs, name=param.name, ids={})
                query[param.name] = "x" if value is None else str(value).lower()
        elif param.where == "header" and param.required and param.name not in headers:
            value = minimal_value(param.schema, defs, name=param.name, ids={})
            headers[param.name] = "x" if value is None else str(value)
    body: Any = None
    if op.body_schema is not None:
        body = minimal_value(op.body_schema, defs, name="", ids=ids)
        carried |= {v for v in ids.values() if v in json.dumps(body, default=str)}
    elif op.body_kind == "bytes":
        body = b"x"
    return Request(
        method=op.method,
        url=url,
        query=query,
        headers=headers,
        body=body,
        body_kind=op.body_kind,
        carried=frozenset(carried),
        aimed=aimed,
        unmapped=tuple(unmapped),
    )


# --------------------------------------------------------------------------
# what org A holds
# --------------------------------------------------------------------------


def _org_tables() -> list[tuple[str, str]]:
    """``(table, org column)`` for every table that files rows under an org."""
    found = []
    for table in Base.metadata.sorted_tables:
        if table.name in _UNCOUNTED_TABLES:
            continue
        for column in ("org_team_id", "org_id"):
            if column in table.columns:
                found.append((table.name, column))
                break
    return found


async def org_row_counts(session: AsyncSession, org_id: uuid.UUID) -> dict[str, int]:
    """Rows per table filed under ``org_id``. The authorization decision rows a
    refusal files under the probed org are platform-only and not counted."""
    parts = []
    for table, column in _org_tables():
        extra = " AND visibility <> 'platform'" if table == "event_outbox" else ""
        parts.append(
            f"SELECT '{table}' AS t, count(*) AS n FROM {table} WHERE {column} = :org{extra}"
        )
    rows = await session.execute(text(" UNION ALL ".join(parts)), {"org": org_id})
    return {name: int(count) for name, count in rows.all()}
