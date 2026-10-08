"""Connections + credentials.

A plugin is the integration *type*; a ``Connection`` is an *instance* — and
one plugin almost always manages MANY (snow_prod / snow_dev / snow_analytics
under one Snowflake plugin; a Connection per dbt project root; …). Every
other surface operates per-connection.

``Connection`` is persisted (``VersionedModel``) with secrets BY REFERENCE
only. ``Credential`` is a transient discriminated union — Pydantic (for
``SecretStr`` redaction + the union), never ``abc.ABC`` (its metaclass
clashes with Pydantic's). Credentials are never serialized in plaintext and
never returned into agent context — the ``CredentialManager`` resolves a
``CredentialRef`` at the I/O boundary inside the connector.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, PrivateAttr, SecretStr, Tag

from alkera_core.connectors.primitives import (
    CredentialMode,
    CredentialRef,
    Environment,
)
from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _migrate_connection_drop_max_effect(doc: dict[str, Any]) -> dict[str, Any]:
    """→ 2.0.0: drop the retired ``max_effect`` per-connection effect ceiling. ``max_effect``
    existed in BOTH 1.0.0 and 1.1.0, so it's stripped from either — otherwise a 1.0.0 document's
    field would linger forever in the ``extra="allow"`` bag. Stamps 2.0.0 so the ladder ends."""
    doc.pop("max_effect", None)
    doc["schema_version"] = "2.0.0"
    return doc


class Connection(VersionedModel):
    """The instance unit. Persisted under
    ``.alkera/plugins/<plugin>/connections/<handle>/connection.json`` —
    secrets by primary or named ``CredentialRef`` values, never inline.
    """

    # 1.1.0: added ``credential_mode`` + ``enabled`` (additive, defaulted — a
    # 1.0.0 document reads as a shared-credential, enabled connection).
    # 2.0.0: removed ``max_effect`` (the per-connection effect ceiling). Writes are
    # governed solely by the permission gate + modes now. The migration strips the field
    # from a 1.0.0 OR 1.1.0 document (both carried it), mirroring TeamConnectionRecord.
    SCHEMA_VERSION = "2.0.0"
    MIGRATIONS: ClassVar[dict[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
        "1.0.0": _migrate_connection_drop_max_effect,
        "1.1.0": _migrate_connection_drop_max_effect,
    }

    handle: str
    """Stable id used by tools, ACL, cost, URNs — e.g. "snow_prod"."""
    plugin: str
    """The owning plugin name — e.g. "snowflake"."""
    dialect: str | None = None
    environment: Environment = Environment.DEV
    """Vestigial deployment-tier label — retained for on-disk/wire
    compatibility; not consulted by permissions or enforcement."""
    urn_namespace: str = ""
    """How this connection mints URNs (lineage/context identity join)."""
    credential_ref: CredentialRef | None = None
    """A POINTER to the secret, never the secret."""
    credential_mode: CredentialMode = CredentialMode.SHARED
    """One service secret for every caller (``shared``, today's default) vs.
    each acting user's own delegated token (``per_user`` — OAuth RBAC
    pass-through; resolution picks the ACTING user's credential)."""
    enabled: bool = True
    """Per-connection mute. A disabled connection stays in the allow-list
    (config + secret preserved) but is filtered out of tool registries, so
    one suspect connection can be silenced without removing it or disabling
    its whole plugin."""
    attributes: dict[str, Any] = Field(default_factory=dict)
    _runtime_bindings: dict[str, str] = PrivateAttr(default_factory=dict)
    """Process-local ownership bindings excluded from every serialized connection."""

    # Identity is (plugin, handle) — the stable key the registry, allow-list, ACL,
    # cost and URNs all address a connection by. Two Connection objects for the same
    # logical connection (e.g. the same DuckDB file re-discovered) compare + hash
    # equal so they can never be added twice (set/dict dedup), even if a transient
    # field like ``attributes`` differs between detections.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Connection):
            return NotImplemented
        return (self.plugin, self.handle) == (other.plugin, other.handle)

    def __hash__(self) -> int:
        return hash((self.plugin, self.handle))

    def is_offline_seed(self) -> bool:
        """Whether THIS connection is a fully-offline resource — parsed from local
        files with no network or warehouse cost (a ``.twb``/``.twbx`` workbook, a
        static ``dags/`` folder). Such connections are safe to ADD automatically,
        the same carve-out as a local DuckDB file or a dbt project: no prod-by-
        accident, no metered query. A provider answers ``seeds_on_activation()`` for
        the WHOLE plugin, but some plugins are offline only for SOME connections —
        e.g. Tableau is a live Metadata-API source by default, yet a workbook FILE
        connection carries ``offline_seed`` and rides this carve-out."""
        return str(self.attributes.get("offline_seed", "")).lower() in {"1", "true", "yes"}


NAMED_CREDENTIAL_REFS_ATTRIBUTE = "credential_refs"
"""Connection attribute mapping credential roles to independent references."""


def named_credential_refs(conn: Connection) -> dict[str, CredentialRef]:
    """Return every valid named credential reference recorded on ``conn``."""
    raw = conn.attributes.get(NAMED_CREDENTIAL_REFS_ATTRIBUTE)
    if not isinstance(raw, Mapping):
        return {}
    refs: dict[str, CredentialRef] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        try:
            refs[name] = CredentialRef.model_validate(value)
        except ValueError:
            continue
    return refs


def with_named_credential_refs(conn: Connection, refs: Mapping[str, CredentialRef]) -> Connection:
    """Copy ``conn`` with the canonical serialized named-reference attribute."""
    attributes = dict(conn.attributes)
    if refs:
        attributes[NAMED_CREDENTIAL_REFS_ATTRIBUTE] = {
            name: ref.model_dump(mode="json") for name, ref in refs.items()
        }
    else:
        attributes.pop(NAMED_CREDENTIAL_REFS_ATTRIBUTE, None)
    return conn.model_copy(update={"attributes": attributes})


def named_credential_ref(conn: Connection, name: str) -> CredentialRef | None:
    """Return the independent credential reference for ``name``, when configured."""
    return named_credential_refs(conn).get(name)


# ---------------------------------------------------------------------------
# Credentials (transient discriminated union; never persisted in plaintext)
# ---------------------------------------------------------------------------


class Credential(BaseModel):
    """Base for the credential variants. Transient; ``SecretStr`` fields
    redact on repr + are excluded from ordinary serialization."""

    model_config = ConfigDict(frozen=True)

    kind: str


class TokenCredential(Credential):
    kind: Literal["token"] = "token"
    token: SecretStr


class UserPassCredential(Credential):
    kind: Literal["user_pass"] = "user_pass"
    user: str
    password: SecretStr
    host: str
    port: int


class OAuthCredential(Credential):
    kind: Literal["oauth"] = "oauth"
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: int = 0

    async def refresh(self) -> OAuthCredential:
        """Refresh the access token. Concrete managers override; the base
        is a no-op identity so callers don't special-case absence."""
        return self


class KeyPairCredential(Credential):
    """Snowflake keypair-JWT — the PKCS#8 private key (rip Altimate decrypt)."""

    kind: Literal["key_pair"] = "key_pair"
    private_key: SecretStr
    user: str | None = None


class RawCredential(Credential):
    """Unknown-tag fallback so a newer writer's credential kind round-trips."""

    kind: str = "__unknown__"


_credential_tag = make_unknown_tag_discriminator(
    {"token", "user_pass", "oauth", "key_pair"}, field="kind"
)

AnyCredential = Annotated[
    (
        Annotated[TokenCredential, Tag("token")]
        | Annotated[UserPassCredential, Tag("user_pass")]
        | Annotated[OAuthCredential, Tag("oauth")]
        | Annotated[KeyPairCredential, Tag("key_pair")]
        | Annotated[RawCredential, Tag("__unknown__")]
    ),
    Discriminator(_credential_tag),
]
"""Parse-time discriminated union over the credential variants."""


@dataclass(frozen=True, slots=True)
class ConnectionBuildResult:
    """A built connection plus primary and independently stored named credentials.

    ``named_credentials=None`` means a legacy builder that does not manage named
    credentials. A mapping lets an update distinguish an omitted name (remove its
    ref) from a present name with ``None`` (preserve its current secret).
    """

    connection: Connection
    credential: Credential | None = None
    named_credentials: Mapping[str, Credential | None] | None = None


ConnectionBuild = tuple[Connection, Credential | None] | ConnectionBuildResult


def normalize_connection_build(result: ConnectionBuild) -> ConnectionBuildResult:
    """Normalize the legacy tuple and named-credential result to one shape."""
    if isinstance(result, ConnectionBuildResult):
        return result
    connection, credential = result
    return ConnectionBuildResult(connection=connection, credential=credential)


def credential_secret_value(credential: Credential) -> str | None:
    """Extract the single secret held by a transient credential variant."""
    for attribute in ("token", "password", "access_token", "private_key"):
        value = getattr(credential, attribute, None)
        if isinstance(value, SecretStr):
            return value.get_secret_value()
    return None


# ---------------------------------------------------------------------------
# CredentialManager
# ---------------------------------------------------------------------------


class CredentialManager(ABC):
    """Resolves a ``CredentialRef`` → live ``Credential`` at the I/O boundary.

    Concrete managers implement keychain / profile / env / file resolution and
    the per-dialect auth niceties. The manager NEVER returns plaintext into
    agent context — tools get a ``Connection`` handle; the connector resolves
    the secret only when it actually opens a session.
    """

    @abstractmethod
    async def resolve(self, ref: CredentialRef) -> Credential: ...

    @abstractmethod
    async def store(self, ref: CredentialRef, cred: Credential) -> None: ...


__all__ = [
    "NAMED_CREDENTIAL_REFS_ATTRIBUTE",
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
