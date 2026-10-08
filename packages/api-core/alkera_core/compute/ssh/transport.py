"""How the plane talks to a host an org attached over SSH.

:class:`SshTransport` is the one seam: it reads a host's key without
authenticating, and opens an authenticated session pinned to a known key.
:class:`AsyncsshTransport` is the real one; tests substitute a fake.

Three rules hold for every connection:

- **The host is vetted first** (:func:`vet_host`): the name is resolved once,
  every answer is classified with :mod:`alkera_core.egress`, and the connection
  is made to the vetted address, so a second DNS answer cannot move it. The
  metadata range is refused in every configuration; private and loopback
  addresses only when the deployment allows them.
- **Credentials are sent only to the pinned key.** A session refuses before
  authentication when the host presents another key
  (:class:`HostKeyMismatchError`), so a host that took over the address never
  sees the password.
- **Secrets never ride on argv.** A command's input goes on stdin, and every
  error is reduced to a fixed sentence before it leaves this module.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import asyncssh

from alkera_core.compute.provider import (
    AUTH_FAILURE,
    INVALID_FAILURE,
    TRANSIENT_FAILURE,
    ComputeProviderError,
    FailureKind,
)
from alkera_core.egress import EgressPolicy, IPAddress, address_refusal, parse_address

#: How long a connection (TCP, key exchange and authentication) may take.
CONNECT_TIMEOUT_SECONDS = 15.0
#: How long one command may run.
COMMAND_TIMEOUT_SECONDS = 60.0
#: The SSH port when none is given.
DEFAULT_PORT = 22

SshAuthKind = Literal["password", "private_key"]
SSH_AUTH_KINDS: tuple[SshAuthKind, ...] = ("password", "private_key")


class SshError(ComputeProviderError):
    """A host refused or could not be reached. ``code`` is stable for routes;
    the message is a fixed sentence that never carries a secret."""

    def __init__(self, code: str, message: str, *, kind: FailureKind) -> None:
        super().__init__(message, kind=kind)
        self.code = code


class HostKeyMismatchError(SshError):
    """The host presented a key other than the one recorded for it."""

    def __init__(self, presented: str, *, key_type: str = "") -> None:
        shown = f"{key_type} {presented}" if key_type else presented
        super().__init__(
            "host_key_mismatch",
            "The host presented a different host key than the one recorded "
            f"({shown}). Check the host, then remove the machine and add it again.",
            kind=INVALID_FAILURE,
        )
        self.presented = presented


def unreachable(detail: str = "") -> SshError:
    suffix = f" ({detail})" if detail else ""
    return SshError("unreachable", f"Couldn't connect to the host{suffix}.", kind=TRANSIENT_FAILURE)


def auth_refused() -> SshError:
    return SshError(
        "auth_failed", "The host refused the username or the credential.", kind=AUTH_FAILURE
    )


def bad_key() -> SshError:
    return SshError(
        "invalid_private_key",
        "The private key couldn't be read. Check the key and its passphrase.",
        kind=INVALID_FAILURE,
    )


def address_refused(message: str) -> SshError:
    return SshError("address_not_allowed", message, kind=INVALID_FAILURE)


@dataclass(frozen=True)
class SshAuth:
    """A credential. ``repr`` never shows its values."""

    kind: SshAuthKind
    secret: str = field(repr=False)
    passphrase: str = field(default="", repr=False)

    def values(self) -> tuple[str, ...]:
        """Every secret value, for striking out of anything that leaves."""
        return tuple(v for v in (self.secret, self.passphrase) if v)


@dataclass(frozen=True)
class SshTarget:
    """Where to connect: the name an admin gave, the vetted address it
    resolved to, and the port."""

    host: str
    address: str
    port: int


#: The host key algorithms asked for when reading a host's key, most preferred
#: first: ed25519, then ECDSA, then RSA. A host serving several keys is then
#: shown (and pinned) by the key an admin is most likely to check with
#: ``ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub``. A session pinned to a
#: key negotiates that key's own algorithm, so this order never moves a pin.
PREFERRED_HOST_KEY_ALGS: tuple[str, ...] = (
    "ssh-ed25519",
    "ssh-ed448",
    "ecdsa-sha2-nistp521",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp256",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
)

#: OpenSSH key-type names, as ``ssh-keygen -l`` prints them, by the algorithm
#: token that starts a public key line.
_KEY_TYPE_NAMES: dict[str, str] = {
    "ssh-ed25519": "ED25519",
    "ssh-ed448": "ED448",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
    "ssh-rsa": "RSA",
    "ssh-dss": "DSA",
    "sk-ssh-ed25519@openssh.com": "ED25519-SK",
    "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK",
}
_CERT_SUFFIX = "-cert-v01@openssh.com"


def host_key_type(public_key: str) -> str:
    """The OpenSSH name of the key type of ``public_key`` (an OpenSSH public
    key line), e.g. ``ED25519``; ``""`` when the line names no known type."""
    token = public_key.strip().split(" ", 1)[0]
    token = token.removesuffix(_CERT_SUFFIX)
    return _KEY_TYPE_NAMES.get(token, "")


@dataclass(frozen=True)
class HostKey:
    """A host's public key as OpenSSH writes it, and its SHA-256 fingerprint."""

    public_key: str
    fingerprint: str

    @property
    def key_type(self) -> str:
        """The key's OpenSSH type name (``ED25519``, ``ECDSA``, ``RSA``)."""
        return host_key_type(self.public_key)


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class SshSession(Protocol):
    async def run(
        self, command: str, *, stdin: str = "", limit_seconds: float = COMMAND_TIMEOUT_SECONDS
    ) -> CommandResult:
        """Run ``command`` on the host with ``stdin``; never raises for a
        nonzero exit (the caller reads :attr:`CommandResult.exit_status`)."""
        ...

    async def upload(self, local: Path, remote_name: str) -> str:
        """Copy ``local`` into the signed-in user's home as ``remote_name``
        (sftp); returns its absolute path on the host."""
        ...


class SshTransport(Protocol):
    async def host_key(self, target: SshTarget) -> HostKey:
        """The key the host presents, read without authenticating."""
        ...

    def session(
        self, target: SshTarget, *, username: str, auth: SshAuth, pinned_key: str
    ) -> AbstractAsyncContextManager[SshSession]:
        """An authenticated session, refused with :class:`HostKeyMismatchError`
        before any credential is sent when the host presents another key."""
        ...


# --- vetting ------------------------------------------------------------------


Resolver = Callable[[str, int], Sequence[str]]
"""``(host, port) -> [address]``: how a name is resolved."""


def _system_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def vet_host(
    host: str,
    port: int,
    *,
    allow_private: bool,
    resolve: Resolver | None = None,
) -> SshTarget:
    """Resolve ``host`` once and refuse unless every answer may be dialled
    (module docstring). ``resolve`` is ``(host, port) -> [address]`` for tests."""
    name = host.strip()
    if not name or any(ch.isspace() for ch in name) or name.startswith("-"):
        raise address_refused("Enter a host name or an IP address.")
    if not 1 <= port <= 65535:
        raise address_refused("The port must be between 1 and 65535.")
    resolver = resolve or _system_resolve
    try:
        answers: Sequence[str] = await asyncio.wait_for(
            asyncio.to_thread(resolver, name.strip("[]"), port), CONNECT_TIMEOUT_SECONDS
        )
        addresses: list[IPAddress] = list(dict.fromkeys(parse_address(a) for a in answers))
    except (OSError, ValueError, TimeoutError) as exc:
        raise unreachable("the name did not resolve") from exc
    if not addresses:
        raise unreachable("the name did not resolve")
    policy = EgressPolicy.from_settings(allow_private=allow_private)
    for address in addresses:
        reason = address_refusal(address)
        if reason is not None and not policy.admits(name, address):
            raise address_refused(
                f"{name} resolves to an address this deployment doesn't connect to ({reason})."
            )
    return SshTarget(host=name, address=str(addresses[0]), port=port)


def strike(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every secret value struck out."""
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return text


# --- the real transport ------------------------------------------------------------


class _AsyncsshSession:
    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn

    async def run(
        self, command: str, *, stdin: str = "", limit_seconds: float = COMMAND_TIMEOUT_SECONDS
    ) -> CommandResult:
        conn = self._conn
        try:
            # An empty input still closes the command's stdin, so a script
            # reader on the host is never left waiting for more.
            source = {"input": stdin} if stdin else {"stdin": asyncssh.DEVNULL}
            done = await conn.run(command, check=False, timeout=limit_seconds, **source)
        except (asyncssh.TimeoutError, TimeoutError) as exc:
            raise unreachable("a command timed out") from exc
        except (asyncssh.Error, OSError) as exc:
            raise unreachable("the connection dropped") from exc
        status = done.exit_status if done.exit_status is not None else 255
        return CommandResult(
            exit_status=int(status), stdout=str(done.stdout or ""), stderr=str(done.stderr or "")
        )

    async def upload(self, local: Path, remote_name: str) -> str:
        if "/" in remote_name or remote_name.startswith("."):
            raise ValueError("an upload lands in the home directory under a plain name")
        try:
            async with self._conn.start_sftp_client() as sftp:
                await sftp.put(str(local), remote_name)
                return str(await sftp.realpath(remote_name))
        except (asyncssh.Error, OSError) as exc:
            raise unreachable("the upload did not finish") from exc


class AsyncsshTransport:
    """:class:`SshTransport` over ``asyncssh``. Reads no SSH config, no agent
    and no known-hosts file of the process it runs in: everything it uses is
    passed in."""

    def __init__(self, *, connect_timeout: float = CONNECT_TIMEOUT_SECONDS) -> None:
        self._connect_timeout = connect_timeout

    async def host_key(self, target: SshTarget) -> HostKey:
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(
                    target.address,
                    target.port,
                    config=None,
                    server_host_key_algs=list(PREFERRED_HOST_KEY_ALGS),
                ),
                self._connect_timeout,
            )
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise unreachable() from exc
        if key is None:
            raise unreachable("the host offered no key")
        return HostKey(
            public_key=key.export_public_key().decode("ascii").strip(),
            fingerprint=key.get_fingerprint("sha256"),
        )

    @asynccontextmanager
    async def _session(
        self, target: SshTarget, *, username: str, auth: SshAuth, pinned_key: str
    ) -> AsyncIterator[SshSession]:
        try:
            pinned = asyncssh.import_public_key(pinned_key)
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise SshError(
                "host_key_invalid", "The recorded host key can't be read.", kind=INVALID_FAILURE
            ) from exc
        options: dict[str, object] = {
            "username": username,
            "known_hosts": ([pinned], [], []),
            "config": None,
            "agent_path": None,
            "client_keys": None,
            "password": None,
            "connect_timeout": self._connect_timeout,
            "login_timeout": self._connect_timeout,
        }
        if auth.kind == "password":
            options["password"] = auth.secret
            options["preferred_auth"] = "password,keyboard-interactive"
        else:
            try:
                key = asyncssh.import_private_key(auth.secret, auth.passphrase or None)
            except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError) as exc:
                raise bad_key() from exc
            options["client_keys"] = [key]
            options["preferred_auth"] = "publickey"
        try:
            conn = await asyncssh.connect(target.address, target.port, **options)
        except asyncssh.HostKeyNotVerifiable as exc:
            presented = await self.host_key(target)
            raise HostKeyMismatchError(presented.fingerprint, key_type=presented.key_type) from exc
        except asyncssh.PermissionDenied as exc:
            raise auth_refused() from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise unreachable() from exc
        try:
            yield _AsyncsshSession(conn)
        finally:
            conn.close()
            await conn.wait_closed()

    def session(
        self, target: SshTarget, *, username: str, auth: SshAuth, pinned_key: str
    ) -> AbstractAsyncContextManager[SshSession]:
        return self._session(target, username=username, auth=auth, pinned_key=pinned_key)


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_PORT",
    "PREFERRED_HOST_KEY_ALGS",
    "SSH_AUTH_KINDS",
    "AsyncsshTransport",
    "CommandResult",
    "HostKey",
    "HostKeyMismatchError",
    "Resolver",
    "SshAuth",
    "SshAuthKind",
    "SshError",
    "SshSession",
    "SshTarget",
    "SshTransport",
    "address_refused",
    "auth_refused",
    "bad_key",
    "host_key_type",
    "strike",
    "unreachable",
    "vet_host",
]
