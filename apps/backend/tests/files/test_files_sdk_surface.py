"""What the Files routes promise the generated clients.

Everything asserted here is read off the app's own OpenAPI document, which is
what `make gen-sdk` feeds to `openapi-typescript` and `openapi-python-client`.
Three classes of defect are only ever visible at that layer, and each cost a
real client:

* **A name collision qualifies BOTH schemas.** Two classes called
  `GrantRequest` made `components["schemas"]["GrantRequest"]` stop resolving in
  the browser portal, which failed `tsc` on a billing module that had not
  changed. A qualified name is therefore a defect wherever it appears, not
  only on the Files half of the pair.
* **A duplicate nested-object title silently drops a model.**
  `openapi-python-client` names a nested object's model after its title and
  exits 0 when two collide, so seven facets titled `Metadata` took `Item`,
  `ChildrenPage` and every facet out of `alkera_sdk._generated.models` with no
  failing command anywhere.
* **A parameter or a status the document does not declare cannot be sent or
  typed.** A filter that is only read off `request.query_params` has to be
  smuggled past the typed client, and a 202 that is not declared has no shape
  to poll.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from alkera_core.files.filters import WIRE_KEYS
from tests._suite_app import app

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = pytest.mark.xdist_group("files_sdk_surface")


def _chip_keys() -> set[str]:
    """Every wire key `ListFilters.from_query` accepts, read from the library
    rather than retyped, so a chip added there fails this file until the route
    declares it. The filters the server derives from the principal (`caller`)
    are not wire keys and are deliberately not declared."""
    return set(WIRE_KEYS)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


def _files_paths(spec: dict[str, Any]) -> dict[str, Any]:
    return {p: v for p, v in spec["paths"].items() if p.startswith("/api/v1/files/")}


def test_no_schema_name_is_qualified_by_a_collision(spec: dict[str, Any]) -> None:
    """A qualified name is the document saying two classes share a name.

    FastAPI only prefixes a schema with its module path when a second class
    claims the same name, so an empty list here is the proof that no Files
    schema shadows a platform one — and that the portal's
    `components["schemas"][...]` lookups still resolve.
    """
    qualified = sorted(n for n in spec["components"]["schemas"] if "__" in n or "-" in n)
    assert qualified == []


def test_the_sharing_body_and_the_credit_grant_body_are_both_reachable(
    spec: dict[str, Any],
) -> None:
    """The two grant bodies are different shapes and both keep a plain name."""
    schemas = spec["components"]["schemas"]
    assert "FilesGrantRequest" in schemas
    assert "GrantRequest" in schemas
    files_body = spec["paths"]["/api/v1/files/drives/{drive_id}/items/{item_id}/permissions"][
        "post"
    ]["requestBody"]["content"]["application/json"]["schema"]
    assert files_body["$ref"].endswith("/FilesGrantRequest")
    assert set(schemas["FilesGrantRequest"]["properties"]) == {
        "principal",
        "role",
        "expiresAt",
    }
    assert "credit_class" in schemas["GrantRequest"]["properties"]


@pytest.mark.parametrize(
    "model",
    [
        "AttrsFacet",
        "Capabilities",
        "FileFacet",
        "LeaseFacet",
        "NameFlagsWire",
        "ObjectFacet",
        "SymlinkFacet",
    ],
)
def test_every_facet_titles_its_metadata_bag_for_itself(spec: dict[str, Any], model: str) -> None:
    """No two facets title the inherited bag the same thing.

    `openapi-python-client` turns a nested object's title into a model name; a
    shared title is a duplicate model, and a duplicate model is dropped along
    with the facet that held it.
    """
    bag = spec["components"]["schemas"][model]["properties"]["metadata"]
    assert bag["type"] == "object"
    assert bag["title"] == f"{'NameFlags' if model == 'NameFlagsWire' else model}Metadata"


def test_the_metadata_titles_across_files_schemas_are_all_distinct(
    spec: dict[str, Any],
) -> None:
    titles = [
        schema["properties"]["metadata"]["title"]
        for schema in spec["components"]["schemas"].values()
        if isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        and isinstance(schema["properties"].get("metadata"), dict)
        and schema["properties"]["metadata"].get("type") == "object"
    ]
    assert len(titles) == len(set(titles)), sorted(titles)


@pytest.mark.parametrize(
    "module",
    [
        "item",
        "children_page",
        "capabilities",
        "file_facet",
        "lease_facet",
        "name_flags_wire",
        "object_facet",
        "symlink_facet",
        "trash_page",
        "version_list",
        "files_grant_request",
    ],
)
def test_the_python_sdk_actually_carries_the_files_models(module: str) -> None:
    """The generated package holds the models, not just the document.

    This is the assertion the generator's exit code does not make: it printed
    "duplicate models" and returned 0 while writing a package with no `Item` in
    it.
    """
    importlib.import_module(f"alkera_sdk._generated.models.{module}")


def test_the_children_listing_declares_every_filter_it_accepts(
    spec: dict[str, Any],
) -> None:
    """A chip the library parses is a parameter the document declares.

    Anything missing here has to be appended to the query string behind the
    typed client's back, which is how the browser hook and the py-sdk namespace
    both ended up with their own serialisers.
    """
    op = spec["paths"]["/api/v1/files/drives/{drive_id}/items/{item_id}/children"]["get"]
    declared = {p["name"] for p in op["parameters"] if p["in"] == "query"}
    assert _chip_keys() | {"marker", "orderBy", "limit"} <= declared


@pytest.mark.parametrize(
    ("path", "method", "field"),
    [
        ("/api/v1/files/drives/{drive_id}/trash", "get", "entries"),
        (
            "/api/v1/files/drives/{drive_id}/items/{node_id}/versions",
            "get",
            "versions",
        ),
    ],
)
def test_the_listing_bodies_are_typed_not_unknown(
    spec: dict[str, Any], path: str, method: str, field: str
) -> None:
    """Both routes name a schema, and that schema names the array it returns."""
    body = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]
    ref = body["schema"].get("$ref")
    assert ref is not None, body["schema"]
    named = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert named["properties"][field]["type"] == "array"


def test_a_move_too_large_to_run_inline_has_a_declared_shape(
    spec: dict[str, Any],
) -> None:
    """The 202 branch of PATCH is the operation, and the document says so."""
    op = spec["paths"]["/api/v1/files/drives/{drive_id}/items/{item_id}"]["patch"]
    assert "202" in op["responses"]
    ref = op["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/OperationWire")


def test_the_copy_202_and_the_operation_poll_are_one_shape(spec: dict[str, Any]) -> None:
    """A client writes one progress path, so both must name the same schema."""
    paths = _files_paths(spec)
    copy_ref = paths["/api/v1/files/drives/{drive_id}/items/{item_id}/copy"]["post"]["responses"][
        "202"
    ]["content"]["application/json"]["schema"]["$ref"]
    poll_ref = paths["/api/v1/files/drives/{drive_id}/operations/{operation_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert copy_ref == poll_ref
