"""Which routes are listings, and whether the visibility matrix covers each.

Pure reads of the app's route table: no database, no request. The matrix test
(``test_listing_visibility_matrix.py``) runs every covered listing against
seeded callers; this module is the part that says what "every" is, so a
listing added tomorrow is found by discovery rather than by someone
remembering to add it.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable, Mapping

from fastapi.routing import APIRoute
from pydantic import BaseModel

#: The field a page model carries its rows in.
PAGE_FIELDS = frozenset({"items", "entries", "value", "rows"})

#: The tenant surface. ``/admin/v1`` is the platform's own staff surface: its
#: callers are platform staff reading across orgs, not members of one org, so
#: "each member sees only what they may read" is not the question it answers.
TENANT_PREFIX = "/api/v1/"


def is_listing_model(model: object) -> bool:
    """A response model that is a list of rows, or a page carrying them."""
    if typing.get_origin(model) is list:
        return True
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return False
    for name, info in model.model_fields.items():
        if name not in PAGE_FIELDS or typing.get_origin(info.annotation) is not list:
            continue
        (row,) = typing.get_args(info.annotation) or (None,)
        if isinstance(row, type) and issubclass(row, BaseModel):
            return True
    return False


def route_key(method: str, path: str) -> str:
    return f"{method} {path}"


def listing_routes(routes: Iterable[object]) -> set[str]:
    """Every tenant GET route whose response model is a listing."""
    found = set()
    for route in routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if route.path.startswith(TENANT_PREFIX) and is_listing_model(route.response_model):
            found.add(route_key("GET", route.path))
    return found


def get_routes(routes: Iterable[object]) -> set[str]:
    """Every GET route, listing or not."""
    return {
        route_key("GET", route.path)
        for route in routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    }


def coverage_problems(
    *,
    discovered: set[str],
    routes: set[str],
    covered: set[str],
    extra: Mapping[str, str],
    pending: Mapping[str, str],
) -> list[str]:
    """What is wrong with the matrix's coverage, as messages a reader acts on.

    ``discovered`` are the listings found by their response model; ``extra``
    are listings declared by hand because their response carries no model
    (each with the reason). Every one must be covered by an adapter or pending;
    a pending entry may only shrink, so one that gained an adapter or names a
    route that no longer exists fails until it is removed.
    """
    problems = []
    listings = discovered | set(extra)
    for key in sorted(listings - covered - set(pending)):
        problems.append(f"add a seed adapter for {key}")
    for key in sorted(set(pending) & covered):
        problems.append(f"{key} has a seed adapter now; remove it from PENDING")
    for key in sorted(set(pending) - listings):
        problems.append(f"{key} is no longer a listing route; remove it from PENDING")
    for key in sorted(covered - listings):
        problems.append(f"{key} has an adapter but is not a listing route")
    for key in sorted(set(extra) - routes):
        problems.append(f"{key} is declared a listing but no such GET route exists")
    return problems


__all__ = [
    "PAGE_FIELDS",
    "TENANT_PREFIX",
    "coverage_problems",
    "get_routes",
    "is_listing_model",
    "listing_routes",
    "route_key",
]
