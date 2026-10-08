"""alkera-connectors — driver-free data-connector building blocks.

The shared layer under the CLI plugin framework, the backend's team-connection
CRUD/OAuth relay, and the worker's server-side probe. Importing this package
(or its descriptor catalog) NEVER imports a warehouse driver — driver imports
are function-local inside each descriptor's ``connect()``, and the ``drivers``
extra opts a consumer (the worker) into installing them.
"""

from alkera_core.connectors.connection import (
    AnyCredential,
    Connection,
    Credential,
    CredentialManager,
    KeyPairCredential,
    OAuthCredential,
    RawCredential,
    TokenCredential,
    UserPassCredential,
)
from alkera_core.connectors.connection_form import (
    AuthMethodSchema,
    ConnectionFormSchema,
    FormField,
    OAuthSpec,
)
from alkera_core.connectors.primitives import (
    ConnectionOrigin,
    CredentialMode,
    CredentialRef,
    Effect,
    Environment,
    environment_for,
)

__all__ = [
    "AnyCredential",
    "AuthMethodSchema",
    "Connection",
    "ConnectionFormSchema",
    "ConnectionOrigin",
    "Credential",
    "CredentialManager",
    "CredentialMode",
    "CredentialRef",
    "Effect",
    "Environment",
    "FormField",
    "KeyPairCredential",
    "OAuthCredential",
    "OAuthSpec",
    "RawCredential",
    "TokenCredential",
    "UserPassCredential",
    "environment_for",
]
