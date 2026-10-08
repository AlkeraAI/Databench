"""Connections and credentials, re-exported from ``alkera_core.connectors``.

The models live in the shared package so the backend and the worker can use
them without the CLI runtime. These are the same class objects, so
``isinstance`` checks and the discriminated union agree across both.
"""

from __future__ import annotations

from alkera_core.connectors.connection import (
    AnyCredential,
    Connection,
    ConnectionBuild,
    ConnectionBuildResult,
    Credential,
    CredentialManager,
    CredentialRef,
    KeyPairCredential,
    OAuthCredential,
    RawCredential,
    TokenCredential,
    UserPassCredential,
    credential_secret_value,
    named_credential_ref,
    named_credential_refs,
    normalize_connection_build,
    with_named_credential_refs,
)

__all__ = [
    "AnyCredential",
    "Connection",
    "ConnectionBuild",
    "ConnectionBuildResult",
    "Credential",
    "CredentialManager",
    "CredentialRef",
    "KeyPairCredential",
    "OAuthCredential",
    "RawCredential",
    "TokenCredential",
    "UserPassCredential",
    "credential_secret_value",
    "named_credential_ref",
    "named_credential_refs",
    "normalize_connection_build",
    "with_named_credential_refs",
]
