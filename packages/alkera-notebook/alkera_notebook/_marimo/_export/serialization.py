# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._ast.app import InternalApp
from alkera_notebook._marimo._schemas.export_options import NotebookExportSnapshot
from alkera_notebook._marimo._session.state.serialize import (
    serialize_notebook,
    serialize_session_view,
)
from alkera_notebook._marimo._session.state.session_view import SessionView


def serialize_notebook_snapshot(
    app: InternalApp,
    session_view: SessionView,
    *,
    drop_virtual_file_outputs: bool,
    include_model_notifications: bool = False,
) -> NotebookExportSnapshot:
    return NotebookExportSnapshot(
        notebook=serialize_notebook(session_view, app.cell_manager),
        session=serialize_session_view(
            session_view,
            cell_ids=app.cell_manager.cell_ids(),
            drop_virtual_file_outputs=drop_virtual_file_outputs,
        ),
        model_notifications=(
            tuple(session_view.get_model_notifications())
            if include_model_notifications
            else ()
        ),
    )
