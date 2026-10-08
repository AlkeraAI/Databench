"""The declarative connection-form schema, re-exported from ``alkera_core.connectors``.

The schema lives in the shared package so the backend can serve every plugin's
form to the web portal, which renders the same generic form the workspace uses.
"""

from __future__ import annotations

from alkera_core.connectors.connection_form import (
    AuthMethodSchema,
    ConnectionFormSchema,
    FormField,
    OAuthSpec,
)

__all__ = [
    "AuthMethodSchema",
    "ConnectionFormSchema",
    "FormField",
    "OAuthSpec",
]
