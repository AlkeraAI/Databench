"""The daemon's view of saved-query parameter binding.

A re-run arrives on the daemon as ``{"kind": "run_query", "params": {...}}``
beside a query object whose spec carries the SQL template, its typed parameter
declarations and the engine. The template is never rendered: the shared
compiler in ``alkera_core.schemas.objects.query_params`` — the same module the
objects routes validate a re-run with — rewrites each ``{name}`` slot to the
engine's own placeholder and returns the typed values separately, and the
mirror hands both to the read-only SQL tool, whose connector passes the values
to the driver beside the statement. The SQL the driver sees holds placeholders
and never a value; the classifier, the read-only transaction and the
unconditional rollback stand between the same parameterized text and the
warehouse.

This module re-exports the shared names so the cloud package has one import
path for them.
"""

from __future__ import annotations

from alkera_core.schemas.objects.query_params import (
    ENGINE_STYLES,
    SLOT,
    BoundQuery,
    QueryCompileError,
    QueryParamDeclaration,
    compile_query,
    placeholders_of,
)

__all__ = [
    "ENGINE_STYLES",
    "SLOT",
    "BoundQuery",
    "QueryCompileError",
    "QueryParamDeclaration",
    "compile_query",
    "placeholders_of",
]
