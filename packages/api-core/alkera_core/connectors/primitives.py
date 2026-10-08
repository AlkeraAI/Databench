"""Connection primitives shared by every surface that touches a data connection.

These are the axes a connection is governed by — extracted from the CLI's
plugin framework so the backend (team-connection CRUD, OAuth relay) and the
worker (server-side probe) can reason about connections without importing the
CLI runtime:

- ``Effect`` — the read/write/destroy/egress taxonomy every gated action
  reduces to. ``DESTROY``/``EGRESS`` are the irreversible / exfiltration tiers.
- ``Environment`` — a connection's deployment tier (a descriptive label).
- ``CredentialMode`` — one shared service secret vs. each acting user's own
  delegated credential.
- ``ConnectionOrigin`` — where a live connection is defined (a runtime axis).
- ``CredentialRef`` — a POINTER to a secret, never the secret itself.

The agent/policy machinery (action descriptors, capability tokens, transient
query results) deliberately stays in the CLI — it is meaningless without the
harness around it.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Effect(StrEnum):
    """What an action does to the world.

    The single load-bearing axis: a SQL ``DROP``, a bash ``rm -rf`` and a
    dbt-Cloud "delete job" all reduce to ``DESTROY`` and hit the same rule.
    Persisted via ``native_enum=False`` (VARCHAR) when it ever reaches the
    ORM — Pydantic models just serialize the ``.value`` string.
    """

    READ = "read"
    """Observes only — never mutates."""
    WRITE = "write"
    """Recoverable mutation (INSERT, guarded UPDATE/DELETE, CREATE, dbt run)."""
    DESTROY = "destroy"
    """Irreversible / wide-blast (DROP DB/SCHEMA, TRUNCATE, unguarded DML, rm -rf)."""
    EGRESS = "egress"
    """Moves data OUT (COPY INTO s3, export, exfil)."""
    EXEC = "exec"
    """Runs a program or reaches the host filesystem on the DATA server itself:
    ``COPY … FROM/TO PROGRAM`` (RCE), ``COPY … FROM/TO '<file>'`` and
    ``pg_read_file`` / ``lo_export`` (server filesystem), ``CREATE EXTENSION`` /
    ``load_extension`` (loads native code), ``LOAD DATA INFILE`` /
    ``SELECT … INTO OUTFILE`` (server filesystem), ``sys_exec`` / ``xp_cmdshell``
    (shell), ClickHouse ``file()`` / ``executable()`` / ``SYSTEM``. The most
    dangerous tier — a program on the database host, not just its data. NEVER
    auto-allowed in any stance, and refused even under ``bypass`` unless an
    explicit rule grants it (the one floor ``bypass`` does not waive)."""
    MEMORY = "memory"
    """Records what the agent learned (a knowledge item on this machine). The
    agent's memory, not a change to the workspace, the user's files or any data
    system — so every permission mode admits it, the read-only ones included."""


class Environment(StrEnum):
    """A connection's deployment tier — a descriptive label persisted on the
    connection; not a permission input."""

    PROD = "prod"
    STAGING = "staging"
    DEV = "dev"
    LOCAL = "local"


_PROD_RE = re.compile(r"\bprod", re.IGNORECASE)
_STAGING_RE = re.compile(r"\bstag", re.IGNORECASE)


def environment_for(*hints: str) -> Environment:
    """Classify a connection's deployment tier from name/url/db hints — the ONE
    shared heuristic every plugin's discovery uses. Prod wins over staging.
    Word-boundary matching avoids false positives like 'reproduction' → prod."""
    blob = " ".join(h for h in hints if h)
    if _PROD_RE.search(blob):
        return Environment.PROD
    if _STAGING_RE.search(blob):
        return Environment.STAGING
    return Environment.DEV


class CredentialMode(StrEnum):
    """How a connection's credential binds to users.

    ``SHARED`` is one service credential every caller uses — the classic
    connection secret. ``PER_USER`` means each acting user holds their OWN
    credential (a user-delegated OAuth token), so the warehouse's native
    RBAC and audit attribution apply per person; resolving the credential
    picks the acting user's token, and a user without one gets a
    sign-in-required error instead of someone else's identity.
    """

    SHARED = "shared"
    PER_USER = "per_user"


class ConnectionOrigin(StrEnum):
    """Where a live connection is defined — a runtime axis, not persisted on
    the ``Connection`` document itself.

    ``LOCAL`` connections live in this workspace's ``.alkera/`` store (added
    by form or promoted from detection). ``ORG`` connections are defined
    centrally in the org's backend registry and distributed to every member's
    daemon; they carry org-level ACL/policy and a local handle collision
    shadows them.
    """

    LOCAL = "local"
    ORG = "org"


class CredentialRef(BaseModel):
    """A POINTER to a secret — never the secret itself.

    The actual material lives in the OS keychain / a chmod-600 file / env;
    only this reference is persisted into a ``Connection``. The
    ``CredentialManager`` resolves it at the I/O boundary inside the
    connector, never into agent context.
    """

    model_config = ConfigDict(frozen=True)

    scheme: str
    """How to resolve it — e.g. "keychain" | "env" | "profile" | "file"."""
    locator: str
    """The scheme-specific key/path (keychain service name, env var, …)."""


__all__ = [
    "ConnectionOrigin",
    "CredentialMode",
    "CredentialRef",
    "Effect",
    "Environment",
    "environment_for",
]
