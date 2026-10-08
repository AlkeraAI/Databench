"""SQL across the kernel boundary: the providers that resolve a workspace's
connections, the statement policy, and the broker that serves the kernel's
``sql.execute``.

Importing this package needs no Arrow; the broker and the providers do
(``alkera-notebook[arrow,duckdb]``).
"""

from alkera_notebook.sql.errors import (
    ConnectionNotInWorkspaceError,
    OutsideRunError,
    QueryCancelledError,
    QueryFailedError,
    ResultTooLargeError,
    SqlError,
    SqlUnavailableError,
    StatementRefusedError,
    UnknownConnectionError,
)
from alkera_notebook.sql.policy import (
    Confirmer,
    DefaultStatementPolicy,
    NoConfirmer,
    PolicyDecision,
    StatementPolicy,
    keyword_classifier,
)
from alkera_notebook.sql.provider import (
    ArrowResult,
    Requester,
    SqlEngineProvider,
    SqlProviderRegistry,
    SqlRequest,
    SqlWorkspace,
)

__all__ = [
    "ArrowResult",
    "Confirmer",
    "ConnectionNotInWorkspaceError",
    "DefaultStatementPolicy",
    "NoConfirmer",
    "OutsideRunError",
    "PolicyDecision",
    "QueryCancelledError",
    "QueryFailedError",
    "Requester",
    "ResultTooLargeError",
    "SqlEngineProvider",
    "SqlError",
    "SqlProviderRegistry",
    "SqlRequest",
    "SqlUnavailableError",
    "SqlWorkspace",
    "StatementPolicy",
    "StatementRefusedError",
    "UnknownConnectionError",
    "keyword_classifier",
]
