"""``POST /api/v1/charts/export``: a chart profile spec rendered to SVG or PNG.

The route renders what it is given and stores nothing: the spec carries its
own rows (the inline policy), so there is no object to authorize against and
no org data read. It still requires a credential, through
:data:`~backend.auth.dependencies.CurrentPrincipal` rather than
``CurrentUser``, because the callers that need an image of a chart most are
agents, and an agent in a box acts on the machine's credential. Rendering
costs CPU, so an anonymous caller gets nothing.

The SVG is a document served from the API origin, and its text is the chart's
data (customer content). It goes out as an attachment with ``nosniff``, under
the API's own CSP (``alkera_core.observability.security_headers.API_CSP``:
``default-src 'none'``, not framable), so opening it directly can never run a
script or fetch anything on this origin.
"""

from __future__ import annotations

from typing import Any, Literal

from alkera_core.charts import ChartSpecError
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from backend.auth.dependencies import CurrentPrincipal
from backend.services import chart_export

router = APIRouter(prefix="/api/v1/charts", tags=["charts"])

_MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png"}


class ChartExportRequest(BaseModel):
    """A chart to render. ``spec`` is an Alkera chart profile spec with its
    rows inline; ``scale`` multiplies a PNG's pixel size."""

    spec: dict[str, Any] = Field(title="ChartExportSpec")
    format: Literal["svg", "png"] = "svg"
    scheme: Literal["light", "dark"] = "light"
    scale: float = Field(default=2.0, ge=chart_export.MIN_SCALE, le=chart_export.MAX_SCALE)


@router.post(
    "/export",
    response_class=Response,
    responses={
        200: {"content": {"image/svg+xml": {}, "image/png": {}}},
        422: {"description": "The spec is outside the chart profile."},
    },
)
async def export_chart(body: ChartExportRequest, ctx: CurrentPrincipal) -> Response:
    """Render ``body.spec`` with the Alkera theme. A spec the profile refuses is
    a 422 naming the offending path, never a value from the spec."""
    del ctx  # authentication is the whole of the requirement
    try:
        image = await chart_export.render_async(body.spec, body.format, body.scheme, body.scale)
    except ChartSpecError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "chart_spec_refused", "message": str(exc), "path": exc.path},
        ) from exc
    headers = {
        "Content-Disposition": f'attachment; filename="chart.{body.format}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return Response(content=image, media_type=_MEDIA_TYPES[body.format], headers=headers)
