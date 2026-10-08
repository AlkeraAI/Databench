"""Generic-SQL (SQLAlchemy URL) connection form + validation (driver-free).

Parsing a SQLAlchemy URL needs only sqlalchemy's URL machinery; no driver is
imported here. Drivers resolve at connect time, in the CLI's connector and in a
distribution's server-side probe, if it registers one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr
from sqlalchemy import URL
from sqlalchemy.engine import make_url

from alkera_core.connectors.connection import Connection, Credential, TokenCredential
from alkera_core.connectors.connection_form import ConnectionFormSchema, FormField
from alkera_core.connectors.primitives import environment_for
from alkera_core.connectors.reach import host_reach_refusal

PLUGIN = "generic_sql"
URL_ATTR = "url"
#: Query-string keys that carry a secret — stripped OUT of the stored URL so a secret can
#: never land on the Connection in EITHER the userinfo or the query position.
SECRET_QUERY_KEYS = frozenset(
    {"password", "pwd", "passwd", "token", "authtoken", "private_key", "passphrase", "secret"}
)
_PASSWORD_QUERY_KEYS = ("password", "pwd", "passwd")

#: SQLAlchemy backend name → the canonical lineage ``system`` (the URN scheme + the
#: ``warehouse_authority`` key), so a generic connection folds with dbt + a dedicated
#: connector for the same engine. Unknown backends use the bare backend name.
_BACKEND_SYSTEM: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "redshift": "redshift",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowflake": "snowflake",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "mssql": "sqlserver",
    "clickhouse": "clickhouse",
    "oracle": "oracle",
    "trino": "trino",
}


def system_for(backend: str) -> str:
    return _BACKEND_SYSTEM.get(backend.strip().lower(), backend.strip().lower() or "sql")


def form_schema() -> ConnectionFormSchema:
    return ConnectionFormSchema(
        # No auth methods — the secret is a shared field, so team-capability is the
        # form-level implicit slot (an admin filling the whole form covers it).
        implicit_team_capable=True,
        shared_fields=[
            FormField(
                name="url",
                label="Database URL (SQLAlchemy)",
                required=True,
                help="e.g. postgresql+psycopg://user@host:5432/db or "
                "snowflake://user@account/db/schema. You can omit the password and "
                "set it below. The driver must be installed.",
            ),
            FormField(
                name="password",
                label="Password",
                type="password",
                required=False,
                secret=True,
                help="The database password. A password written into the URL above wins and "
                "this field is ignored, so put it in one place or the other.",
            ),
        ],
    )


def sanitize_shared(fields: dict[str, str]) -> dict[str, str]:
    """Take the password back out of the URL before it is distributed.

    This is the one connector whose non-secret field can carry a secret:
    ``postgresql://user:hunter2@host/db`` is a perfectly ordinary thing to
    paste, and a team connection hands that string to every member's machine.
    The password belongs in the password field, which rides the encrypted
    column and is lent out a few minutes at a time. An unparseable URL is left
    alone — ``build_connection`` is where it earns its error message."""
    raw = (fields.get(URL_ATTR) or "").strip()
    if not raw:
        return dict(fields)
    try:
        url = make_url(raw)
    except Exception:
        return dict(fields)
    query = {k: v for k, v in url.query.items() if k.lower() not in SECRET_QUERY_KEYS}
    stripped = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=None,
        host=url.host,
        port=url.port,
        database=url.database,
        query=query,
    )
    return {**fields, URL_ATTR: stripped.render_as_string(hide_password=False)}


def build_connection(
    handle: str, auth_method: str, fields: dict[str, str]
) -> tuple[Connection, Credential | None]:
    raw = (fields.get("url") or "").strip()
    if not raw:
        raise ValueError("a database URL is required")
    try:
        url = make_url(raw)
    except Exception as exc:  # surface any SQLAlchemy parse error to the user
        raise ValueError(f"invalid database URL: {exc}") from exc
    # Split the secret OUT of the URL — from BOTH the userinfo slot AND the query string
    # (e.g. `?password=`, `?token=`) — so the stored Connection never carries a secret;
    # the secret rides a CredentialRef. ``URL.set(password=None)`` is a no-op (None =
    # "no change"), so rebuild explicitly with a cleaned query.
    query = dict(url.query)
    secret_query = {k: query.pop(k) for k in list(query) if k.lower() in SECRET_QUERY_KEYS}
    # A non-password secret in the query (token / private_key / passphrase) can't be
    # re-expressed as a userinfo password at connect, so refuse rather than silently drop
    # it (which would break auth) or persist it (which would leak). Use a dedicated
    # connector, or the password field, for those.
    unsupported = sorted(k for k in secret_query if k.lower() not in _PASSWORD_QUERY_KEYS)
    if unsupported:
        raise ValueError(
            f"secret URL query parameter(s) {unsupported} aren't supported by the generic "
            "connector. Remove them from the URL and use a dedicated connector, or the "
            "password field"
        )

    # A SQLAlchemy URL query value is str | tuple[str, ...] (repeated keys) — take the
    # first for a password.
    def _first(value: object) -> str:
        return str(value[0]) if isinstance(value, tuple) and value else str(value)

    query_password = next(
        (_first(secret_query[k]) for k in _PASSWORD_QUERY_KEYS if k in secret_query), ""
    )
    password = url.password or query_password or (fields.get("password") or "").strip()
    url_no_pw = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=None,
        host=url.host,
        port=url.port,
        database=url.database,
        query=query,  # secrets removed
    )
    backend = url.get_backend_name()
    system = system_for(backend)
    env = environment_for(handle, raw, url.database or "")
    attrs: dict[str, Any] = {
        URL_ATTR: url_no_pw.render_as_string(hide_password=False),
        "system": system,
        "backend": backend,
        "host": url.host or "",
        "port": str(url.port or ""),
        "database": url.database or "",
    }
    conn = Connection(
        handle=handle,
        plugin=PLUGIN,
        dialect=system,
        environment=env,
        urn_namespace=f"{system}://{url.host or url.database or handle}",
        attributes=attrs,
    )
    cred = TokenCredential(token=SecretStr(password)) if password else None
    return conn, cred


# ---------------------------------------------------------------------------
# What a distributed connection may reach
# ---------------------------------------------------------------------------
#
# A team or personal connection saved on the server is opened by other machines: a
# member's laptop, and a cloud box that runs chats for many people and many orgs. A
# URL that names a FILE (``sqlite:////…``, ``duckdb:///…``) or the machine itself
# (``localhost``, a loopback or link-local address, the cloud metadata endpoint, a
# local socket) does not name a database server; on a box it opens another org's
# files, the box's own stores, or a service listening there. Such a URL is refused
# when a record is saved and again where a machine would open one; the host rules are
# the shared ones in :mod:`alkera_core.connectors.reach`, and these add what only a URL can
# say (a file backend, a parameter that moves the endpoint).

#: Backends whose URL names a database server reached over the network. Anything
#: else (``sqlite``, ``duckdb``, an engine this list does not know) may open files on
#: the machine that runs it, so it is not distributed.
NETWORK_BACKENDS = frozenset(
    {
        "postgresql",
        "redshift",
        "mysql",
        "mariadb",
        "clickhouse",
        "clickhousedb",
        "snowflake",
        "trino",
        "mssql",
        "oracle",
    }
)

#: Query keys that point the driver somewhere other than the URL's host (a socket
#: path, a second host, a service file) or have it read a file on the machine that
#: opens it (a client key it would then present to the server, a defaults file).
LOCAL_QUERY_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "unix_socket",
        "unix_sock",
        "socket",
        "service",
        "servicefile",
        "passfile",
        "sslkey",
        "sslcert",
        "sslcrl",
        "sslcrldir",
        "read_default_file",
        "read_default_group",
        "local_infile",
    }
)


def _url_hosts(url: URL) -> list[str]:
    """Every host the URL names (libpq takes a comma-separated list); ``[""]`` for none."""
    return (url.host or "").split(",")


def distribution_refusal(attributes: Mapping[str, Any]) -> str | None:
    """Why a connection with these built attributes must not be opened by another
    machine, or ``None`` when its URL names a database server over the network."""
    try:
        url = make_url(str(attributes.get(URL_ATTR) or "").strip())
    except Exception:
        return "the database URL could not be parsed"
    backend = url.get_backend_name().lower()
    if backend not in NETWORK_BACKENDS:
        return (
            f"a shared connection must name a database server; the {backend!r} backend "
            "opens files on the machine that runs it"
        )
    local_keys = sorted(k for k in url.query if k.lower() in LOCAL_QUERY_KEYS)
    if local_keys:
        return (
            f"the database URL parameter(s) {local_keys} point the driver at the machine "
            "itself or at its files; name the server in the URL's host"
        )
    for host in _url_hosts(url):
        refusal = host_reach_refusal(host)
        if refusal is not None:
            return refusal
    return None


__all__ = [
    "LOCAL_QUERY_KEYS",
    "NETWORK_BACKENDS",
    "PLUGIN",
    "SECRET_QUERY_KEYS",
    "URL_ATTR",
    "build_connection",
    "distribution_refusal",
    "form_schema",
    "sanitize_shared",
    "system_for",
]
