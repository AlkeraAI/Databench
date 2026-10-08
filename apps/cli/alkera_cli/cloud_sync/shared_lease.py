"""Lease a preconfigured connection's shared credential bundle.

The backend keeps the admin's role-keyed bundle and hands out time-bounded
copies. The daemon holds it in memory for the lease window and never writes it
to disk, so a connection the admin disabled, deleted, or flipped to per-user
stops working within one lease instead of living on as a file on every laptop
that ever synced.

Two things this does NOT claim. Refusing the next lease only reaches clients
that ask for one, so rotating a role stays the way an admin cuts off access
this second. A leased bundle contains working credentials for its window. The
bound is on how long, not on what the holder can do with it.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog
from pydantic import SecretStr

from alkera_cli.cloud_sync.lease_cache import (
    LeaseEntry,
    LeasePolicy,
    invalidate_record,
    lease_through_cache,
)
from alkera_cli.cloud_sync.lease_scope import lease_scope, lease_scope_key
from alkera_cli.plugins.plugin_base.connection import Credential, TokenCredential
from alkera_cli.plugins.plugin_base.credential_manager import (
    CredentialResolutionError,
    register_credential_resolver,
)

logger = structlog.get_logger(__name__)


class SharedLeaseError(RuntimeError):
    """A lease couldn't be obtained — the message is user-facing."""


#: Reuse for at most 5 minutes — a single introspection makes many sequential
#: requests, and a backend round-trip per request is a pathology. The 30-second
#: expiry margin covers one clock: the backend minted this lease's expiry
#: itself (the relay lane's provider-reported expiry crosses one clock more and
#: carries a wider margin).
_POLICY = LeasePolicy(reuse_seconds=300.0, expiry_skew_seconds=30.0)

_cache: dict[str, LeaseEntry[dict[str, str]]] = {}
"""``"<caller-identity-digest>:<chat:id | workspace:id | *>:<record_id>@<credential_version>"``
maps to a role-keyed bundle and its serve-until epoch. Identity isolates
account sessions; the chat or workspace isolates what one box serves, so a
lease one may hold is never served to another; the version prevents rotation
from serving a retired bundle."""


def invalidate_shared_lease(record_id: str) -> None:
    """Drop every cached lease for this record, across identities and
    generations. Called when the member removes or dismisses the connection,
    and when sync removes it."""
    invalidate_record(_cache, record_id)


def lease_shared_secret_sync(locator: str) -> str:
    """Return one role from a preconfigured connection's leased credential bundle.

    Called from a connector's sync connect path — usually a worker thread, but
    the connection probe reaches it on the daemon's event-loop thread, which is
    what ``run_coro_blocking`` handles."""
    from alkera_core.utils.blocking import run_coro_blocking

    from alkera_cli.cloud_sync.client import resolve_connections_client

    client = resolve_connections_client()
    if client is None:
        raise SharedLeaseError("sign in to Alkera to use this preconfigured connection")
    generation, separator, role = locator.partition("#")
    if not separator or not role:
        raise SharedLeaseError("the credential lease locator has no role")
    record_id = generation.partition("@")[0]
    # Read on this thread: the tool call's context, copied by ``to_thread``.
    chat_id, workspace_id = lease_scope()

    def fetch() -> tuple[dict[str, str], float | None]:
        try:
            if workspace_id is not None and chat_id is None:
                lease = client.lease_credential_bundle(record_id, workspace_id=workspace_id)
            else:
                lease = client.lease_credential_bundle(record_id, chat_id=chat_id)
            return run_coro_blocking(lease)
        except Exception as exc:
            raise SharedLeaseError(
                f"couldn't get the credential for this connection ({exc})"
            ) from exc

    key = f"{client.identity_key}:{lease_scope_key() or '*'}:{generation}"
    bundle = lease_through_cache(_cache, key, fetch, _POLICY)
    try:
        return bundle[role]
    except KeyError:
        raise SharedLeaseError(f"the leased credential bundle has no {role!r} role") from None


def resolve_team_lease(locator: str, env: Mapping[str, str]) -> Credential:
    """The ``team_lease`` credential scheme: one role of a team connection's
    credential, leased from the server a few minutes at a time."""
    try:
        leased = lease_shared_secret_sync(locator)
    except SharedLeaseError as exc:
        raise CredentialResolutionError(str(exc)) from exc
    return TokenCredential(token=SecretStr(leased))


register_credential_resolver("team_lease", resolve_team_lease)

__all__ = [
    "SharedLeaseError",
    "invalidate_shared_lease",
    "lease_shared_secret_sync",
    "resolve_team_lease",
]
