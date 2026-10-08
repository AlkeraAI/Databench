"""Machines an org attaches by their SSH details: the transport seam, the
endpoint store, the reading of a host before it is added, and the ``ssh``
provider (registered by :mod:`alkera_core.compute.ssh.provider`)."""

from alkera_core.compute.ssh.endpoints import (
    DbEndpointStore,
    Endpoint,
    EndpointStore,
    open_credential,
    seal_credential,
)
from alkera_core.compute.ssh.probe import HostFacts, ProbeResult, probe_host
from alkera_core.compute.ssh.transport import (
    DEFAULT_PORT,
    SSH_AUTH_KINDS,
    AsyncsshTransport,
    CommandResult,
    HostKey,
    HostKeyMismatchError,
    SshAuth,
    SshAuthKind,
    SshError,
    SshSession,
    SshTarget,
    SshTransport,
    host_key_type,
    vet_host,
)

__all__ = [
    "DEFAULT_PORT",
    "SSH_AUTH_KINDS",
    "AsyncsshTransport",
    "CommandResult",
    "DbEndpointStore",
    "Endpoint",
    "EndpointStore",
    "HostFacts",
    "HostKey",
    "HostKeyMismatchError",
    "ProbeResult",
    "SshAuth",
    "SshAuthKind",
    "SshError",
    "SshSession",
    "SshTarget",
    "SshTransport",
    "host_key_type",
    "open_credential",
    "probe_host",
    "seal_credential",
    "vet_host",
]
