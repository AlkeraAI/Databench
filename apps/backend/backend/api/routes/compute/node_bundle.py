"""The node bundle this deployment built from source, served to its nodes.

A node's bootstrap fetches ``{target}.sha256`` and then ``{target}`` on its
machine credential (``X-Alkera-Machine-Credential``). A request without a live
machine credential is a 401 before the deployment says whether it holds a
bundle at all, so the bundle is never a public download. The bundle is the
one in ``NODE_BUNDLE_DIR`` (``alkera_core.compute.node_bundle``), built with
the deployment.
"""

from __future__ import annotations

from alkera_core.auth.machine_token import parse_machine_credential
from alkera_core.compute.daemon_source import BUNDLE_ROUTE
from alkera_core.compute.node_bundle import BundleFile, NodeBundleError, bundle_file
from alkera_core.config import settings
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse

from backend.api.params import PathId
from backend.auth.dependencies import DbSession
from backend.services.credentials import resolve_machine_credential

router = APIRouter(prefix=BUNDLE_ROUTE, tags=["machines"])

#: The answer to a request that carries no live machine credential.
_REFUSED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"code": "machine_credential_required", "message": "A node credential is required."},
)


async def _bundle(request: Request, db: DbSession, target: str) -> BundleFile:
    raw = parse_machine_credential(request.headers)
    if raw is None or await resolve_machine_credential(db, raw) is None:
        raise _REFUSED
    try:
        return bundle_file(settings.node_bundle_dir, target)
    except NodeBundleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "node_bundle_missing", "message": str(exc)},
        ) from exc


@router.get("/{target}.sha256", response_class=PlainTextResponse)
async def node_bundle_digest(request: Request, db: DbSession, target: PathId) -> Response:
    """The bundle's digest, as ``sha256sum`` writes it."""
    found = await _bundle(request, db, target)
    return PlainTextResponse(found.sidecar, headers={"X-Alkera-Bundle-Version": found.version})


@router.get("/{target}")
async def node_bundle(request: Request, db: DbSession, target: PathId) -> Response:
    """The bundle itself."""
    found = await _bundle(request, db, target)
    return FileResponse(
        found.path,
        media_type="application/gzip",
        filename=found.path.name,
        headers={"X-Alkera-Bundle-Version": found.version, "X-Alkera-Bundle-Sha256": found.sha256},
    )


__all__ = ["router"]
