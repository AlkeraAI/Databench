"""The real transport against a real SSH server: an in-process ``asyncssh``
server on an ephemeral loopback port. It proves the host key read, pinning
before authentication, password and key sign-in, the refusals, and that a
command's stdin reaches the host."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh
import pytest
from alkera_core.compute.ssh import (
    AsyncsshTransport,
    HostKeyMismatchError,
    SshAuth,
    SshError,
    SshTarget,
    host_key_type,
    probe_host,
)

PASSWORD = "correct horse battery staple"
USER = "deploy"


@dataclass
class ServerLog:
    """What the server saw: every auth attempt and every command."""

    auth_attempts: list[str] = field(default_factory=list)
    commands: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Rig:
    target: SshTarget
    host_key: asyncssh.SSHKey
    client_key: asyncssh.SSHKey
    log: ServerLog


FACTS = "Linux\nx86_64\nyes\nyes\n8\n31\n200\n0\nyes\n"


def _server_class(log: ServerLog, authorized: asyncssh.SSHKey) -> type[asyncssh.SSHServer]:
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            log.auth_attempts.append(f"password:{username}")
            return username == USER and password == PASSWORD

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            log.auth_attempts.append(f"key:{username}")
            return username == USER and key.public_data == authorized.public_data

    return Server


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    log = ServerLog()

    async def handle(process: asyncssh.SSHServerProcess[str]) -> None:
        stdin = await process.stdin.read()
        log.commands.append((str(process.command), stdin))
        process.stdout.write(FACTS if stdin.startswith("uname") else f"echo:{stdin}")
        process.exit(0)

    server = await asyncssh.create_server(
        lambda: _server_class(log, client_key)(),
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        process_factory=handle,
        # The home an upload lands in: a directory of this test's own.
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(tmp_path / "home")),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield Rig(SshTarget("127.0.0.1", "127.0.0.1", port), host_key, client_key, log)
    finally:
        server.close()
        await server.wait_closed()


def _public(key: asyncssh.SSHKey) -> str:
    return key.export_public_key().decode("ascii").strip()


async def test_the_host_key_is_read_without_signing_in(rig: Rig) -> None:
    key = await AsyncsshTransport().host_key(rig.target)
    assert key.fingerprint == rig.host_key.get_fingerprint("sha256")
    assert key.public_key == _public(rig.host_key)
    assert rig.log.auth_attempts == []


@pytest.mark.parametrize("kind", ["password", "private_key"])
async def test_a_pinned_session_signs_in_and_runs_with_stdin(rig: Rig, kind: str) -> None:
    auth = (
        SshAuth(kind="password", secret=PASSWORD)
        if kind == "password"
        else SshAuth(
            kind="private_key",
            secret=rig.client_key.export_private_key("openssh").decode("ascii"),
        )
    )
    async with AsyncsshTransport().session(
        rig.target, username=USER, auth=auth, pinned_key=_public(rig.host_key)
    ) as session:
        done = await session.run("sudo -n bash -s", stdin="the script\n")
    assert (done.exit_status, done.stdout) == (0, "echo:the script\n")
    assert rig.log.commands == [("sudo -n bash -s", "the script\n")]


async def test_an_encrypted_key_signs_in_with_its_passphrase(rig: Rig) -> None:
    sealed = rig.client_key.export_private_key("openssh", passphrase="pp").decode("ascii")
    async with AsyncsshTransport().session(
        rig.target,
        username=USER,
        auth=SshAuth(kind="private_key", secret=sealed, passphrase="pp"),
        pinned_key=_public(rig.host_key),
    ) as session:
        done = await session.run("true", stdin="")
    assert done.exit_status == 0


async def test_another_host_key_is_refused_before_any_credential_is_sent(rig: Rig) -> None:
    impostor = asyncssh.generate_private_key("ssh-ed25519")
    transport = AsyncsshTransport()
    with pytest.raises(HostKeyMismatchError) as raised:
        async with transport.session(
            rig.target,
            username=USER,
            auth=SshAuth(kind="password", secret=PASSWORD),
            pinned_key=_public(impostor),
        ):
            pass
    assert raised.value.presented == rig.host_key.get_fingerprint("sha256")
    assert f"ED25519 {raised.value.presented}" in str(raised.value)
    assert rig.log.auth_attempts == []
    assert PASSWORD not in str(raised.value)


async def test_a_wrong_password_is_an_auth_refusal_that_does_not_echo_it(rig: Rig) -> None:
    with pytest.raises(SshError) as raised:
        async with AsyncsshTransport().session(
            rig.target,
            username=USER,
            auth=SshAuth(kind="password", secret="wrong-secret-value"),
            pinned_key=_public(rig.host_key),
        ):
            pass
    assert (raised.value.code, raised.value.kind) == ("auth_failed", "auth")
    assert "wrong-secret-value" not in str(raised.value)


async def test_an_unreadable_key_is_refused_before_connecting(rig: Rig) -> None:
    with pytest.raises(SshError) as raised:
        async with AsyncsshTransport().session(
            rig.target,
            username=USER,
            auth=SshAuth(kind="private_key", secret="not a key"),
            pinned_key=_public(rig.host_key),
        ):
            pass
    assert raised.value.code == "invalid_private_key"
    assert rig.log.auth_attempts == []


async def test_a_closed_port_is_unreachable() -> None:
    transport = AsyncsshTransport(connect_timeout=2.0)
    with pytest.raises(SshError) as raised:
        await transport.host_key(SshTarget("127.0.0.1", "127.0.0.1", 1))
    assert raised.value.code == "unreachable"


async def test_a_probe_reads_the_facts_a_node_needs(rig: Rig) -> None:
    found = await probe_host(
        AsyncsshTransport(),
        rig.target,
        username=USER,
        auth=SshAuth(kind="password", secret=PASSWORD),
        expected_fingerprint=rig.host_key.get_fingerprint("sha256"),
    )
    facts = found.facts
    assert (facts.os, facts.arch, facts.systemd, facts.root) == ("Linux", "x86_64", True, True)
    assert (facts.vcpu, facts.memory_gb, facts.disk_gb, facts.gpu_count) == (8, 31, 200, 0)
    assert facts.missing == []


async def test_an_upload_lands_in_the_home_over_the_signed_in_session(
    rig: Rig, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir()
    local = tmp_path / "bundle.tar.gz"
    local.write_bytes(b"x" * 200_000)
    async with AsyncsshTransport().session(
        rig.target,
        username=USER,
        auth=SshAuth(kind="password", secret=PASSWORD),
        pinned_key=_public(rig.host_key),
    ) as session:
        where = await session.upload(local, "alkera-node-bundle.tar.gz")
    assert where.endswith("/alkera-node-bundle.tar.gz")
    assert (tmp_path / "home" / "alkera-node-bundle.tar.gz").read_bytes() == b"x" * 200_000


async def test_an_upload_refuses_a_name_that_leaves_the_home(rig: Rig, tmp_path: Path) -> None:
    async with AsyncsshTransport().session(
        rig.target,
        username=USER,
        auth=SshAuth(kind="password", secret=PASSWORD),
        pinned_key=_public(rig.host_key),
    ) as session:
        with pytest.raises(ValueError):
            await session.upload(tmp_path, "../etc/passwd")


# --- a host serving several keys ------------------------------------------------


@asynccontextmanager
async def _multi_key_server(keys: list[asyncssh.SSHKey]) -> AsyncIterator[SshTarget]:
    """A server offering ``keys`` (in this order) that accepts the password."""
    log = ServerLog()

    async def handle(process: asyncssh.SSHServerProcess[str]) -> None:
        await process.stdin.read()
        process.exit(0)

    server = await asyncssh.create_server(
        lambda: _server_class(log, keys[0])(),
        "127.0.0.1",
        0,
        server_host_keys=keys,
        process_factory=handle,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield SshTarget("127.0.0.1", "127.0.0.1", port)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        pytest.param(["ssh-rsa", "ecdsa-sha2-nistp256", "ssh-ed25519"], 2, id="ed25519-over-all"),
        pytest.param(["ssh-rsa", "ecdsa-sha2-nistp256"], 1, id="ecdsa-over-rsa"),
        pytest.param(["ssh-rsa"], 0, id="rsa-alone"),
    ],
)
async def test_the_key_read_is_ed25519_then_ecdsa_then_rsa(
    served: list[str], expected: int
) -> None:
    keys = [asyncssh.generate_private_key(alg) for alg in served]
    async with _multi_key_server(keys) as target:
        found = await AsyncsshTransport().host_key(target)
    want = keys[expected]
    assert found.fingerprint == want.get_fingerprint("sha256")
    assert found.public_key == _public(want)
    assert (
        found.key_type
        == {"ssh-rsa": "RSA", "ecdsa-sha2-nistp256": "ECDSA", "ssh-ed25519": "ED25519"}[
            served[expected]
        ]
    )


async def test_a_pin_to_another_of_the_hosts_keys_still_signs_in() -> None:
    # A machine added before the preference was pinned to the RSA key; the
    # session negotiates the pinned key's algorithm, so it keeps working.
    rsa = asyncssh.generate_private_key("ssh-rsa")
    ed = asyncssh.generate_private_key("ssh-ed25519")
    async with _multi_key_server([rsa, ed]) as target:
        async with AsyncsshTransport().session(
            target,
            username=USER,
            auth=SshAuth(kind="password", secret=PASSWORD),
            pinned_key=_public(rsa),
        ) as session:
            done = await session.run("true", stdin="")
    assert done.exit_status == 0


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("ssh-ed25519 AAAAC3Nz", "ED25519", id="ed25519"),
        pytest.param("ecdsa-sha2-nistp256 AAAAE2Vj", "ECDSA", id="ecdsa-256"),
        pytest.param("ecdsa-sha2-nistp521 AAAAE2Vj", "ECDSA", id="ecdsa-521"),
        pytest.param("ssh-rsa AAAAB3Nz", "RSA", id="rsa"),
        pytest.param("ssh-ed448 AAAA", "ED448", id="ed448"),
        pytest.param("sk-ssh-ed25519@openssh.com AAAA", "ED25519-SK", id="sk-ed25519"),
        pytest.param("ssh-ed25519-cert-v01@openssh.com AAAA", "ED25519", id="cert"),
        pytest.param("  ssh-rsa AAAA comment  ", "RSA", id="padded-with-comment"),
        pytest.param("rsa-sha2-256 AAAA", "", id="a-signature-alg-is-not-a-key-type"),
        pytest.param("", "", id="empty"),
        pytest.param("not a key", "", id="garbage"),
    ],
)
def test_the_key_type_is_named_as_ssh_keygen_names_it(line: str, expected: str) -> None:
    assert host_key_type(line) == expected
