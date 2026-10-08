"""The Files router, assembled by discovery rather than by a list.

A new route family is a new module in this package (``items.py``,
``uploads.py``): this module walks the package, imports every public module,
and includes any module-level ``router``. Nothing here needs editing to add a
family, so two changes adding two families never collide in this file.

Two rules make the walk safe to trust:

* modules whose name starts with ``_`` are infrastructure (``_testhooks``)
  and are skipped, so a helper that happens to define a ``router`` cannot be
  mounted by accident;
* a module that exposes ``content_router`` is collected separately rather than
  mounted here — content bytes are served from the content domain's own ASGI
  app with its own middleware and header recipe, and mounting one under the API host
  would put user bytes on an origin that holds a session cookie.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Final

from alkera_core.validation.storable_text import CHARACTER_REASONS, Reason, SelfValidated
from fastapi import APIRouter, Depends

from backend.api.deps.files import require_files_enabled
from backend.api.deps.files_errors import register_files_error_handlers
from backend.api.routes.files._testhooks import build_test_hooks_router

#: Everything under this prefix answers the opaque 404 while Files is dark.
PREFIX = "/api/v1/files"

#: What Files refuses in its own words, so the request-wide scan for text
#: Postgres cannot store defers there and checks the rest of every Files
#: request (the query string, the headers, every other body field):
#:   - the path: every id in it is held to a declared shape before the route
#:     runs, and a mangled one is a ``validation_error``;
#:   - a NUL in a name: a create, a rename, a copy, a batch item and an upload
#:     each answer ``files.invalid_name.nul``, the code a client's sentence is
#:     keyed off. A lone surrogate in a name is not Files' to answer: a name is
#:     UTF-8 bytes, so the scan refuses it;
#:   - what a box reports from its own filesystem: paths arrive with bytes that
#:     are not UTF-8 carried as lone surrogates, are judged entry by entry and
#:     are compared as bytes, so one odd file name must not refuse the batch.
#:     ``paths[]`` is also a folder skeleton's names, whose decoder refuses a
#:     surrogate itself;
#:   - a NUL in a document's text, which is merged as content.
_NAME: Final = frozenset({Reason.NUL})
_BOX_PATH: Final = CHARACTER_REASONS
SELF_VALIDATED: Final = SelfValidated(
    prefix=PREFIX,
    path=True,
    body_fields={
        "body.name": _NAME,
        "body.items[].name": _NAME,
        "body.symlinkTarget": _NAME,
        "body.text": _NAME,
        "body.paths[]": _BOX_PATH,
        "body.entries[].path": _BOX_PATH,
        "body.entries[].from": _BOX_PATH,
        "body.entries[].displaced": _BOX_PATH,
        "body.names[]": _BOX_PATH,
        "body.unsyncedPaths[]": _BOX_PATH,
        "body.final": _BOX_PATH,
    },
)


def family_modules(package: ModuleType | None = None) -> list[ModuleType]:
    """Every public module in ``package`` (this one by default), alphabetically.

    ``package`` is a parameter so the discovery rules can be proven against a
    planted package rather than by adding a decoy module to the real one.
    """
    target = package if package is not None else _self()
    found: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(target.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        found.append(importlib.import_module(f"{target.__name__}.{info.name}"))
    return found


def _self() -> ModuleType:
    return importlib.import_module(__name__)


def discovered_routers(package: ModuleType | None = None) -> list[APIRouter]:
    """The API routers the walk found."""
    return [
        module.router
        for module in family_modules(package)
        if isinstance(getattr(module, "router", None), APIRouter)
    ]


def discovered_content_routers(package: ModuleType | None = None) -> list[APIRouter]:
    """The content-domain routers the walk found, for the separate mount."""
    return [
        module.content_router
        for module in family_modules(package)
        if isinstance(getattr(module, "content_router", None), APIRouter)
    ]


def build_files_router() -> APIRouter:
    """The one parent router the app includes.

    ``require_files_enabled`` sits on the parent, so a family module cannot
    ship a route that stays reachable when the flag is off — the gate is not
    something a new module has to remember.
    """
    parent = APIRouter(prefix=PREFIX, dependencies=[Depends(require_files_enabled)])
    for child in discovered_routers():
        parent.include_router(child)
    # The one family the walk cannot find (its name starts with ``_``): the
    # crash hook, mounted only when the local test-hook gate is open.
    hooks = build_test_hooks_router()
    if hooks is not None:
        parent.include_router(hooks)
    return parent


__all__ = [
    "PREFIX",
    "SELF_VALIDATED",
    "build_files_router",
    "build_test_hooks_router",
    "discovered_content_routers",
    "discovered_routers",
    "family_modules",
    "register_files_error_handlers",
    "require_files_enabled",
]
