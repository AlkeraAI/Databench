"""No route 5xxes on text Postgres cannot store — proven by walking the schema.

A NUL byte in any user-controlled string used to reach the driver and raise
`CharacterNotInRepertoireError`, which escaped onto the catch-all as an opaque
500 on twenty-eight routes at once. The fix is at the boundary, so the proof has
to be at the boundary too: this test discovers the surface from the app's own
OpenAPI document rather than from a list someone has to remember to extend. A
route added tomorrow is covered the day it is added.

The walk runs three times, once per surface — a poisoned path, a poisoned query
string, a poisoned body — and poisons ONLY that surface each time. Driving all
three at once proves only whichever scan answers first: with a poisoned query
parameter on every request, the body could stop being scanned at all and the
walk would still be green. Kept apart, each pass fails on its own when its own
scan is removed.

Every pass expects the coded refusal BEFORE the endpoint runs, which is also
why this is safe to run against every mutating route in the API: nothing past
the boundary executes.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.models import WorkspaceObject
from alkera_core.observability.asgi import _untranslatable_text
from alkera_core.validation.storable_text import SelfValidated
from backend.api.extension_points import SELF_VALIDATED
from httpx import AsyncClient
from sqlalchemy import select, text
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.anyio

NUL = "\x00"
POISON = f"nul{NUL}value"
#: The same poison for a path segment. httpx refuses a raw control character in
#: a URL, so the NUL rides percent-encoded — which is how a real client would
#: have to send it, and what the server decodes back before routing.
PATH_POISON = "nul%00value"

#: Methods driven. HEAD/OPTIONS carry nothing the scan is about.
_METHODS = ("get", "post", "put", "patch", "delete")


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    """Every (method, path, operation) in the app's own schema."""
    schema = fastapi_app.openapi()
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in schema.get("paths", {}).items():
        for method in _METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                found.append((method, path, operation))
    return sorted(found, key=lambda entry: (entry[1], entry[0]))


#: What a templated segment gets when this pass is NOT about the path.
BENIGN_SEGMENT = "0e6f7a1c-0000-4000-8000-00000000abcd"


def _filled_path(path: str, segment: str) -> str:
    """The path with every templated segment replaced by ``segment``."""
    out = path
    while "{" in out:
        head, _, rest = out.partition("{")
        _, _, tail = rest.partition("}")
        out = f"{head}{segment}{tail}"
    return out


def _resolve(schema: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
    """Follow `$ref` / `anyOf` far enough to see a body's properties."""
    if depth > 6 or not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        target = schema.get("components", {}).get("schemas", {}).get(name, {})
        return _resolve(schema, target, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, list) and options:
            for option in options:
                resolved = _resolve(schema, option, depth + 1)
                if resolved.get("type") not in (None, "null"):
                    return {**resolved, **{k: v for k, v in node.items() if k != key}}
    return node


def _fill(schema: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """A value for `node` that carries the poison wherever a string is allowed."""
    resolved = _resolve(schema, node)
    if depth > 4:
        return POISON
    kind = resolved.get("type")
    if kind == "object" or "properties" in resolved:
        properties = resolved.get("properties")
        if not isinstance(properties, dict) or not properties:
            return {POISON: POISON}
        required = set(resolved.get("required", []))
        out: dict[str, Any] = {}
        for name, prop in properties.items():
            child = _resolve(schema, prop)
            if child.get("type") == "string" or "enum" in child or name in required:
                out[name] = _fill(schema, prop, depth + 1)
        return out or {POISON: POISON}
    if kind == "array":
        return [_fill(schema, resolved.get("items", {"type": "string"}), depth + 1)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return POISON


def _carries_poison(value: Any) -> bool:
    """Whether the poison actually made it into this body."""
    if isinstance(value, str):
        return NUL in value
    if isinstance(value, dict):
        return any(_carries_poison(k) or _carries_poison(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_carries_poison(item) for item in value)
    return False


def _poisoned_body(schema: dict[str, Any], operation: dict[str, Any]) -> Any:
    """This operation's body with the NUL somewhere in it, or ``None`` when it
    takes no JSON body at all.

    A schema with no string field anywhere (all integers, all booleans) would
    otherwise produce a clean body — which the boundary would rightly admit,
    and the route would then EXECUTE. The poisoned key is the fallback that
    keeps every body-carrying operation a genuine test of the body scan.
    """
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    media = body.get("content", {}).get("application/json")
    if not isinstance(media, dict):
        return None
    filled = _fill(schema, media.get("schema", {}))
    if _carries_poison(filled):
        return filled
    if isinstance(filled, dict):
        return {**filled, POISON: POISON}
    if isinstance(filled, list):
        return [*filled, POISON]
    return POISON


def _poisoned_query(operation: dict[str, Any]) -> dict[str, str]:
    """Every declared query parameter, poisoned — or one the schema does not
    declare, so an operation that takes no argument at all is still driven."""
    out: dict[str, str] = {}
    for parameter in operation.get("parameters", []) or []:
        if isinstance(parameter, dict) and parameter.get("in") == "query":
            name = parameter.get("name")
            if isinstance(name, str):
                out[name] = POISON
    return out or {"probe": POISON}


#: Statuses a boundary OUTSIDE the scan may answer with first. The gate ingest
#: limiter turns an unauthenticated CI request away before anything buffers its
#: body — that is the point of it, and the scan is not owed the first word.
#: What matters is that the answer is a coded 4xx and never a 5xx.
_EARLIER_BOUNDARY = frozenset({401, 411, 413})


def _self_validated() -> tuple[SelfValidated, ...]:
    """What the APP declares its surfaces validate themselves, read off its
    own wiring.

    Read rather than re-spelled, so the walk covers a surface the day it is
    declared instead of the day somebody remembers this list. Declaring the
    whole API to make the walk pass is not a way out: the floors below count
    only operations the scan itself refused.
    """
    for middleware in fastapi_app.user_middleware:
        declared = middleware.kwargs.get("self_validated")
        if declared is not None:
            return tuple(declared)
    return ()


#: Surfaces the app declares as answering hostile text in their own vocabulary
#: on some part of a request, which the scan therefore leaves to them. On that
#: part they are held to the finding itself (a null byte must not 5xx) rather
#: than to the scan's particular refusal, because their own answer is the
#: better one: Files names the rule that refused the name, the content plane
#: answers one opaque 404, the visit beacon cleans the string and records the
#: click. Every other part of their requests is held to the scan like any
#: other route's.
_SELF_VALIDATED: tuple[SelfValidated, ...] = _self_validated()


def _left_to_itself(path: str, surface: str) -> bool:
    """Whether the surface ``path`` is under may answer this pass itself.

    A surface that declares only some body fields may answer a poisoned body
    either way: the scan refuses unless the poison sits in declared fields
    alone.
    """
    for own in _SELF_VALIDATED:
        if not own.covers(path):
            continue
        if surface == "path":
            return own.path
        if surface == "query":
            return own.query
        return own.body or bool(own.body_fields)
    return False


def _code_of(response: Any) -> str | None:
    body = response.json()
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


#: The floor of operations each pass must see REFUSED BY THE SCAN. A pass that
#: stopped matching operations — or whose scan stopped answering — would
#: otherwise be green having proven nothing.
_LEAST_SCANNED = {"path": 100, "query": 200, "body": 60}


@pytest.mark.parametrize(
    "surface",
    [pytest.param(name, id=name) for name in ("path", "query", "body")],
)
async def test_no_operation_in_the_schema_5xxes_on_a_null_byte(
    client: AsyncClient, surface: str
) -> None:
    """The whole API surface, discovered from the schema, driven with a NUL in
    ONE place at a time so each scan is pinned by a pass of its own."""
    schema = fastapi_app.openapi()
    operations = _operations()
    assert len(operations) > 200, "the walk found no API to walk"
    assert any(path == "/api/v1/chats" for _, path, _ in operations)

    offenders: list[str] = []
    scanned = 0
    for method, path, operation in operations:
        kwargs: dict[str, Any] = {}
        if surface == "path":
            if "{" not in path:
                continue
            target = _filled_path(path, PATH_POISON)
        elif surface == "query":
            target = _filled_path(path, BENIGN_SEGMENT)
            kwargs["params"] = _poisoned_query(operation)
        else:
            body = _poisoned_body(schema, operation)
            if body is None:
                continue
            target = _filled_path(path, BENIGN_SEGMENT)
            kwargs["json"] = body
        response = await client.request(method.upper(), target, **kwargs)
        code = _code_of(response)
        if response.status_code == 422 and code == "unstorable_text":
            scanned += 1
        elif _left_to_itself(path, surface):
            # Held to the finding, not to the scan's wording: whatever this
            # surface answers, it must not be the 500 the sweep was about.
            if response.status_code >= 500:
                offenders.append(f"{method.upper()} {path} -> {response.status_code}")
        elif not (response.status_code in _EARLIER_BOUNDARY and code):
            offenders.append(
                f"{method.upper()} {path} -> {response.status_code} {response.text[:160]}"
            )
    assert not offenders, "operations that did not refuse a null byte:\n" + "\n".join(offenders)
    assert scanned >= _LEAST_SCANNED[surface], f"{surface}: only {scanned} operations refused"


@pytest.mark.parametrize(
    ("label", "send"),
    [
        pytest.param(
            "query",
            lambda c: c.get("/api/v1/objects", params={"type": f"chat{NUL}"}),
            id="query-parameter",
        ),
        pytest.param(
            "body",
            lambda c: c.post("/api/v1/chats", json={"title": f"a{NUL}b"}),
            id="body-field",
        ),
        pytest.param(
            "nested",
            lambda c: c.put("/api/v1/org/sync-settings", json={"paths": [f"a{NUL}"]}),
            id="nested-body-field",
        ),
        pytest.param(
            "escape",
            lambda c: c.request(
                "POST",
                "/api/v1/chats",
                content=b'{"title": "a\\u0000b"}',
                headers={"content-type": "application/json"},
            ),
            id="escaped-null-in-raw-json",
        ),
        pytest.param(
            "surrogate",
            lambda c: c.request(
                "POST",
                "/api/v1/chats",
                content=b'{"title": "a\\ud800b"}',
                headers={"content-type": "application/json"},
            ),
            id="lone-surrogate-in-raw-json",
        ),
        pytest.param(
            "header",
            lambda c: c.request(
                "POST",
                "/api/v1/chats",
                json={"title": "ok"},
                headers=[(b"idempotency-key", b"a\x00b"), (b"content-type", b"application/json")],
            ),
            id="persisted-header",
        ),
    ],
)
async def test_a_null_byte_is_a_coded_422_that_never_echoes_the_value(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    label: str,
    send: Any,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await send(client)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unstorable_text"
    assert error["details"]["field"], error
    assert NUL not in response.text
    assert "\\u0000" not in response.text


@pytest.mark.parametrize(
    ("value", "caught"),
    [
        pytest.param("a\x00b", True, id="a-nul-postgres-refuses"),
        pytest.param("a\ud800b", True, id="a-surrogate-the-driver-cannot-encode"),
        pytest.param("ordinary", False, id="a-value-that-is-simply-not-there"),
    ],
)
async def test_the_net_behind_the_boundary_catches_what_the_database_actually_raises(
    real_session: Any, value: str, caught: bool
) -> None:
    """The safety net is matched against the real driver, not against a guess.

    It exists for the values the boundary cannot see — assembled by a service,
    carried in a body too big to scan, read back off a queue — and the failures
    it has to recognise are whatever THIS stack raises: SQLAlchemy over asyncpg
    over Postgres. A NUL surfaces as SQLSTATE 22021; a surrogate never reaches
    the server at all, because the driver's own UTF-8 encode of the parameter
    fails first and the SQLSTATE that carries it is the generic `22000` that
    half the data errors share — so matching on it would swallow them all.
    """
    statement = select(WorkspaceObject).where(WorkspaceObject.title == value)
    try:
        await real_session.execute(statement)
    except Exception as exc:
        raised: Exception | None = exc
    else:
        raised = None
    await real_session.rollback()

    if not caught:
        assert raised is None
        return
    assert raised is not None, "the database accepted a value it cannot store"
    assert _untranslatable_text(raised), (
        f"the net does not recognise what the driver raised: {raised!r}"
    )


async def test_the_net_catches_a_null_byte_inside_a_jsonb_document(real_session: Any) -> None:
    """A NUL riding a ``jsonb`` document is refused under its own SQLSTATE.

    ``jsonb`` rejects the escaped ``\\u0000`` as ``22P05`` (untranslatable
    character), not the ``22021`` a text column raises — the shape an audit
    row carrying a caller's path met, and the net let it through as a 500.
    """
    try:
        await real_session.execute(text("SELECT CAST(:doc AS jsonb)"), {"doc": '{"p": "\\u0000"}'})
    except Exception as exc:
        raised: Exception | None = exc
    else:
        raised = None
    await real_session.rollback()
    assert raised is not None, "jsonb accepted an escaped NUL"
    assert _untranslatable_text(raised), f"the net does not recognise {raised!r}"


async def test_the_net_leaves_an_unrelated_database_failure_alone(real_session: Any) -> None:
    """Asymmetry the net has to keep: a real server-side bug must still be a
    500 with a Sentry event, not a 422 telling the caller to fix their text."""
    try:
        await real_session.execute(text("SELECT 1 / 0"))
    except Exception as exc:
        assert not _untranslatable_text(exc)
    else:  # pragma: no cover - Postgres raises 22012 here
        raise AssertionError("expected a division-by-zero failure")
    finally:
        await real_session.rollback()


async def test_three_surfaces_answer_for_themselves_and_files_only_in_part() -> None:
    """Deferring to a surface is a decision, not a default.

    The content plane answers one opaque 404, so the scan stands aside for it,
    and for what an extension registers in ``SELF_VALIDATED``. Files keeps its
    own answer for its path ids and the fields it declares
    (`files.invalid_name.nul`, proven in the Files suite); its query strings,
    headers and every other body field are scanned. A fourth surface, or a
    wider Files declaration, should not appear without the reason written
    beside it.
    """
    registered = [(own.prefix, own.everything) for own in SELF_VALIDATED.items()]
    assert [(own.prefix, own.everything) for own in _SELF_VALIDATED] == [
        ("/api/v1/files", False),
        ("/c", True),
        *registered,
    ]
    files = _SELF_VALIDATED[0]
    assert (files.path, files.query, files.headers, files.body) == (True, False, False, False)


def _body_locations(schema: dict[str, Any], node: Any, where: str, depth: int = 0) -> set[str]:
    """Every location a request body schema can hold a value at, spelled the
    way a declaration spells it (list indexes blank)."""
    resolved = _resolve(schema, node)
    found = {where}
    if depth > 8:
        return found
    for union in ("anyOf", "oneOf", "allOf"):
        for option in resolved.get(union, []) or []:
            found |= _body_locations(schema, option, where, depth + 1)
    properties = resolved.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            found |= _body_locations(schema, prop, f"{where}.{name}", depth + 1)
    if resolved.get("type") == "array":
        found |= _body_locations(schema, resolved.get("items", {}), f"{where}[]", depth + 1)
    return found


async def test_every_field_a_surface_declares_is_one_its_requests_carry() -> None:
    """A declared field no request has is a hole waiting for a route to grow
    into it: the scan would stand aside for a field nobody decided about. A
    renamed or removed field takes its declaration with it."""
    schema = fastapi_app.openapi()
    for own in _SELF_VALIDATED:
        carried: set[str] = set()
        for _, path, operation in _operations():
            media = (operation.get("requestBody") or {}).get("content", {})
            if own.covers(path) and "application/json" in media:
                carried |= _body_locations(
                    schema, media["application/json"].get("schema", {}), "body"
                )
        declared = set(own.body_fields)
        assert declared <= carried, sorted(declared - carried)


async def test_the_real_app_refuses_in_a_way_the_browser_can_read(client: AsyncClient) -> None:
    """The SPA is a cross-origin caller in dev and any API client is one in
    production: a refusal rendered outside the app's CORS layer reaches the
    browser as an opaque network failure, so the client cannot tell a refused
    field from a dead server."""
    origin = settings.cors_origins_list[0]
    preflight = await client.options(
        "/api/v1/chats",
        headers={
            "origin": origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers.get("access-control-allow-origin") == origin

    refused = await client.post(
        "/api/v1/chats", json={"title": f"a{NUL}b"}, headers={"origin": origin}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "unstorable_text"
    assert refused.headers.get("access-control-allow-origin") == origin


async def test_storable_text_still_reaches_the_route(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The boundary refuses the unstorable and nothing else: an emoji, an accent
    and an ordinary control character all still create a chat."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    title = "naïve 🙂 \u0001 done"
    response = await client.post("/api/v1/chats", json={"title": title})
    assert response.status_code in {200, 201}, response.text
    assert response.json()["title"] == title
