"""Errors the SQL broker answers ``sql.execute`` with.

Each is an ``RpcError`` with the code and ``data.name`` of the kernel RPC's
error table, so a handler raising one is answered with it as it is.
"""

from __future__ import annotations

from typing import Any

from alkera_notebook.rpc.frames import RpcError

INVALID_PARAMS = -32602
FORBIDDEN = -32002
TOO_LARGE = -32003
CANCELLED = -32004
UNAVAILABLE = -32005
UNKNOWN_CONNECTION = -32010
CONNECTION_NOT_IN_WORKSPACE = -32011
STATEMENT_REFUSED = -32012
QUERY_FAILED = -32013


class SqlError(RpcError):
    """A refusal or failure of one ``sql.execute`` call."""

    error_code: int = -32603
    error_name: str = "internal_error"

    def __init__(self, message: str, **data: Any) -> None:
        super().__init__(self.error_code, message, {"name": self.error_name, **data})

    def to_error(self) -> dict[str, Any]:
        """The JSON-RPC ``error`` member."""
        return self.to_json()


class InvalidParamsError(SqlError):
    error_code = INVALID_PARAMS
    error_name = "invalid_params"


class OutsideRunError(SqlError):
    """The named run is not executing on this kernel (any more)."""

    error_code = FORBIDDEN
    error_name = "forbidden"

    def __init__(
        self, message: str = "sql.execute is accepted only while a run is executing"
    ) -> None:
        super().__init__(message, reason="outside_run")


class ResultTooLargeError(SqlError):
    error_code = TOO_LARGE
    error_name = "too_large"

    def __init__(self, limit: int, hint: str) -> None:
        super().__init__(f"the result is larger than {limit} bytes", limit=limit, hint=hint)


class QueryCancelledError(SqlError):
    error_code = CANCELLED
    error_name = "cancelled"


class SqlUnavailableError(SqlError):
    """Retryable: the engine is out of room for results in flight, or a
    provider is temporarily unreachable."""

    error_code = UNAVAILABLE
    error_name = "unavailable"


class UnknownConnectionError(SqlError):
    error_code = UNKNOWN_CONNECTION
    error_name = "sql.unknown_connection"

    def __init__(self, connection: str) -> None:
        super().__init__(
            f"no connection named {connection!r} in this workspace", connection=connection
        )


class ConnectionNotInWorkspaceError(SqlError):
    error_code = CONNECTION_NOT_IN_WORKSPACE
    error_name = "sql.connection_not_in_workspace"

    def __init__(self, connection: str, reason: str) -> None:
        super().__init__(
            f"connection {connection!r} is not shared with this workspace's notebooks",
            connection=connection,
            reason=reason,
        )


class StatementRefusedError(SqlError):
    error_code = STATEMENT_REFUSED
    error_name = "sql.statement_refused"

    def __init__(self, reason: str, statement_kind: str) -> None:
        super().__init__(reason, kind=statement_kind)


class QueryFailedError(SqlError):
    """The data system rejected the statement (syntax, permissions, ...)."""

    error_code = QUERY_FAILED
    error_name = "sql.query_failed"
