"""The request path the router routes on, for the ASGI guards that run before it.

The CSRF guard and the body-limit guard match a request against tables spelled
as routes, and the content app decides which of its paths are pages, so each
must judge the path the router will route on and nothing else.
"""

from __future__ import annotations

from starlette.types import Scope


def routed_path(scope: Scope) -> str:
    """``scope["path"]`` with the ``root_path`` prefix stripped on a segment
    boundary, exactly as Starlette's router does.

    Mirrors ``starlette._utils.get_route_path`` as of Starlette 1.3.1, including
    its answer for a request to the root path itself, which is ``""`` (not
    ``"/"``). A path that only shares the prefix's spelling (``/prefixed`` under
    ``/prefix``) is left whole. ``apps/backend/tests/test_route_path.py`` holds
    this to Starlette's own function, so an upgrade that changes the rule fails
    there."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


__all__ = ["routed_path"]
