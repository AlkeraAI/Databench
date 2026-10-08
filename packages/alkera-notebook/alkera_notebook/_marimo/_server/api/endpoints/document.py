# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from alkera_notebook._marimo._messaging.notebook.changes import Transaction
from alkera_notebook._marimo._messaging.notebook.reconcile import reconcile_transaction
from alkera_notebook._marimo._messaging.notification import (
    NotebookDocumentTransactionNotification,
)
from alkera_notebook._marimo._server.api.deps import AppState
from alkera_notebook._marimo._server.api.utils import parse_request
from alkera_notebook._marimo._server.models.models import (
    BaseResponse,
    NotebookDocumentTransactionRequest,
    SuccessResponse,
)
from alkera_notebook._marimo._server.router import APIRouter
from alkera_notebook._marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from starlette.requests import Request

router = APIRouter()


@router.post("/transaction")
@requires("edit")
async def document_transaction(request: Request) -> BaseResponse:
    """
    parameters:
        - in: header
          name: Marimo-Session-Id
          schema:
            type: string
          required: true
    requestBody:
        content:
            application/json:
                schema:
                    $ref: "#/components/schemas/NotebookDocumentTransactionRequest"
    responses:
        200:
            description: Apply a document transaction
            content:
                application/json:
                    schema:
                        $ref: "#/components/schemas/SuccessResponse"
    """
    app_state = AppState(request)
    body = await parse_request(request, cls=NotebookDocumentTransactionRequest)
    session = app_state.require_current_session()
    session_id = app_state.require_current_session_id()

    sanitized = reconcile_transaction(tuple(body.changes), session.document)
    transaction = Transaction(changes=sanitized, source="frontend")
    applied = session.document.apply(transaction)
    session.notify(
        NotebookDocumentTransactionNotification(transaction=applied),
        from_consumer_id=ConsumerId(session_id),
    )

    return SuccessResponse()
