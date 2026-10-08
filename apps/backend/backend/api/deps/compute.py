"""How a compute refusal answers over HTTP."""

from __future__ import annotations

from fastapi import HTTPException

from backend.services.compute import grants


def refused(exc: grants.ComputeRefusedError) -> HTTPException:
    """The HTTP shape of a refusal: the class's status and ``{code, message}``,
    plus the figures a refusal names (a quota refusal's ``quota``)."""
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, **exc.detail_extra()},
    )
