"""One discovered connection from a host-addressed plugin's conventional env vars.

Postgres (libpq's ``PG*``), MySQL, Redshift, ClickHouse, Trino and Databricks each name
a host, a few attributes and a secret in the environment. A plugin states those names
as an :class:`EnvConnectionSpec`; :func:`connection_from_env` reads them the one way.

The candidate is discovered, never auto-promoted to live. Its secret is never copied
onto the connection: a ``CredentialRef`` points at the secret's variable and is resolved
at connect.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from alkera_cli.contracts.tool_types import CredentialRef, environment_for
from alkera_cli.plugins.plugin_base.connection import Connection


@dataclass(frozen=True)
class EnvAttr:
    """One connection attribute read from one environment variable."""

    attr: str
    var: str
    default: str = ""
    """Used only when the variable is unset; a variable set to ``""`` stays ``""``."""
    required: bool = False
    """An unset or empty variable means the environment names no connection at all."""
    only_when_set: bool = False
    """Left off the attributes unless the variable holds a value."""


@dataclass(frozen=True)
class EnvConnectionSpec:
    """Where one plugin's environment candidate comes from."""

    plugin: str
    handle: str
    host_var: str
    credential_var: str
    attrs: tuple[EnvAttr, ...]
    container_attr: str
    """The attribute (database or catalog) the deployment tier is also read from."""
    urn_with_port: bool = True
    """Whether the URN namespace carries the port (``scheme://host:port``)."""


def connection_from_env(
    spec: EnvConnectionSpec, environ: Mapping[str, str] | None = None
) -> Connection | None:
    """The candidate ``spec`` describes, or ``None`` when its host (or another required
    variable) is unset or empty."""
    env = os.environ if environ is None else environ
    host = env.get(spec.host_var)
    if not host:
        return None
    attributes: dict[str, Any] = {"host": host}
    for attr in spec.attrs:
        value = env.get(attr.var)
        if attr.required and not value:
            return None
        if attr.only_when_set:
            if value:
                attributes[attr.attr] = value
            continue
        attributes[attr.attr] = env.get(attr.var, attr.default)
    ref = (
        CredentialRef(scheme="env", locator=spec.credential_var)
        if env.get(spec.credential_var)
        else None
    )
    authority = f"{host}:{attributes['port']}" if spec.urn_with_port else host
    return Connection(
        handle=spec.handle,
        plugin=spec.plugin,
        dialect=spec.plugin,
        environment=environment_for(spec.handle, host, attributes[spec.container_attr]),
        urn_namespace=f"{spec.plugin}://{authority}",
        credential_ref=ref,
        attributes=attributes,
    )


__all__ = ["EnvAttr", "EnvConnectionSpec", "connection_from_env"]
