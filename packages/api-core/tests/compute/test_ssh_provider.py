"""The ``ssh`` provider against a scripted host: what each lifecycle call does on
the host, what it reports, and that the node's secrets and the admin's
credential stay off argv and out of errors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.provider import (
    GONE,
    INVALID_FAILURE,
    RUNNING,
    STOPPED,
    TRANSIENT_FAILURE,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    NodeLaunch,
    provider_traits,
)
from alkera_core.compute.ssh import (
    CommandResult,
    Endpoint,
    HostKey,
    HostKeyMismatchError,
    SshAuth,
    SshError,
    SshTarget,
    open_credential,
    probe_host,
    seal_credential,
    vet_host,
)
from alkera_core.compute.ssh.provider import (
    SECRETS_DELIMITER,
    STATE_SCRIPT,
    UNINSTALL_SCRIPT,
    SshProvider,
    StagedBundle,
    launch_script,
    parse_state,
    staging_lines,
)
from alkera_core.compute.ssh.transport import unreachable

HOST_KEY = HostKey(public_key="ssh-ed25519 AAAAhost", fingerprint="SHA256:host")
CREDENTIAL = "hunter2-is-the-password"
NODE_SECRET = "alkm_node-secret-value"


@dataclass
class FakeHost:
    """A host that answers the provider's scripts the way a real one would."""

    state: str = "absent"
    launched_for: str = ""
    #: The node's last logged error, as the state probe prints it.
    node_error: str = ""
    reachable: bool = True
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    uploads: dict[str, bytes] = field(default_factory=dict)

    def answer(self, command: str, stdin: str) -> CommandResult:
        if stdin == STATE_SCRIPT:
            logged = f"node-error: {self.node_error}\n" if self.node_error else ""
            return CommandResult(0, f"{self.state}\n{self.launched_for}\n{logged}", "")
        if stdin.startswith("set -euo pipefail"):
            self.state = "installing"
            self.launched_for = stdin.split("printf '%s\\n' ", 1)[1].split(" ", 1)[0]
            return CommandResult(0, "started\n", "")
        if stdin == UNINSTALL_SCRIPT:
            self.state, self.launched_for = "absent", ""
            return CommandResult(0, "removed\n", "")
        if stdin.startswith("systemctl stop"):
            self.state = "inactive"
            return CommandResult(0, "", "")
        if stdin.startswith("systemctl start"):
            self.state = "active"
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unknown script")


class FakeSession:
    def __init__(self, host: FakeHost, username: str) -> None:
        self._host = host
        self._username = username

    async def run(
        self, command: str, *, stdin: str = "", limit_seconds: float = 60.0
    ) -> CommandResult:
        self._host.calls.append((self._username, command, stdin))
        return self._host.answer(command, stdin)

    async def upload(self, local: Path, remote_name: str) -> str:
        self._host.uploads[remote_name] = await asyncio.to_thread(local.read_bytes)
        return f"/home/{self._username}/{remote_name}"


class FakeTransport:
    def __init__(self, host: FakeHost, *, key: HostKey = HOST_KEY) -> None:
        self.host = host
        self.key = key
        self.pinned: list[str] = []

    async def host_key(self, target: SshTarget) -> HostKey:
        if not self.host.reachable:
            raise unreachable()
        return self.key

    def session(
        self, target: SshTarget, *, username: str, auth: SshAuth, pinned_key: str
    ) -> AbstractAsyncContextManager[FakeSession]:
        @asynccontextmanager
        async def opened() -> AsyncIterator[FakeSession]:
            if not self.host.reachable:
                raise unreachable()
            if pinned_key != self.key.public_key:
                raise HostKeyMismatchError(self.key.fingerprint)
            self.pinned.append(pinned_key)
            yield FakeSession(self.host, username)

        return opened()


class MemoryStore:
    def __init__(self, endpoint: Endpoint, allocation_id: UUID) -> None:
        self.endpoint = endpoint
        self.allocation_id = allocation_id
        self.forgotten = False
        self.abandoned: list[str] = []

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        return self.endpoint if endpoint_id == self.endpoint.id else None

    async def for_allocation(self, allocation_id: UUID) -> Endpoint | None:
        return self.endpoint if allocation_id == self.allocation_id else None

    async def forget(self, endpoint_id: UUID) -> None:
        self.forgotten = True

    async def abandon(self, endpoint: Endpoint, note: str) -> None:
        self.forgotten = True
        self.abandoned.append(note)


def _endpoint(
    *, username: str = "ubuntu", deleted: bool = False, key: str = HOST_KEY.public_key
) -> Endpoint:
    return Endpoint(
        id=uuid4(),
        org_id=uuid4(),
        org_machine_id=uuid4(),
        host="127.0.0.1",
        port=22,
        username=username,
        host_key=key,
        auth=SshAuth(kind="password", secret=CREDENTIAL),
        machine_deleted=deleted,
        arch="x86_64",
    )


def _launch(allocation_id: UUID) -> NodeLaunch:
    return NodeLaunch(
        allocation_id=allocation_id,
        name="node",
        type_code="host",
        storage_gb=10,
        script="#!/usr/bin/env bash\necho boot\n",
        secrets={"ALKERA_MACHINE_CREDENTIAL": NODE_SECRET},
    )


def _rig(**endpoint: object) -> tuple[SshProvider, FakeHost, MemoryStore, UUID]:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(_endpoint(**endpoint), allocation_id)  # type: ignore[arg-type]
    provider = SshProvider(
        enabled=True, allow_private=True, transport=FakeTransport(host), store=store
    )
    return provider, host, store, allocation_id


async def test_a_launch_installs_the_node_and_reads_running_while_it_installs() -> None:
    provider, _host, store, allocation_id = _rig()
    machine_id = await provider.run(_launch(allocation_id))
    assert machine_id == str(store.endpoint.id)
    described = await provider.describe(machine_id)
    assert (described.phase, described.raw_status) == (RUNNING, "installing")
    found = await provider.find(allocation_id)
    assert found is not None and found.machine_id == machine_id
    # Another allocation's lookup does not adopt this launch.
    assert await provider.find(uuid4()) is None


async def test_secrets_ride_on_stdin_never_on_the_command() -> None:
    provider, host, _store, allocation_id = _rig()
    await provider.run(_launch(allocation_id))
    for _user, command, _stdin in host.calls:
        assert NODE_SECRET not in command
        assert CREDENTIAL not in command
    launch_stdin = host.calls[0][2]
    assert f"ALKERA_MACHINE_CREDENTIAL={NODE_SECRET}" in launch_stdin


@pytest.mark.parametrize(
    ("username", "command"),
    [
        pytest.param("root", "bash -s", id="root-runs-directly"),
        pytest.param("ubuntu", "sudo -n bash -s", id="others-go-through-sudo"),
    ],
)
async def test_every_script_runs_as_root(username: str, command: str) -> None:
    provider, host, store, allocation_id = _rig(username=username)
    await provider.run(_launch(allocation_id))
    await provider.describe(str(store.endpoint.id))
    assert {call[1] for call in host.calls} == {command}


async def test_stop_and_start_drive_the_unit_and_never_the_host() -> None:
    provider, host, _store, allocation_id = _rig()
    machine_id = await provider.run(_launch(allocation_id))
    await provider.stop(machine_id)
    assert (await provider.describe(machine_id)).phase == STOPPED
    await provider.start(machine_id)
    assert (await provider.describe(machine_id)).phase == RUNNING
    assert not any("poweroff" in s or "shutdown" in s for _u, _c, s in host.calls)


async def test_terminate_uninstalls_and_keeps_the_credential_while_the_machine_lives() -> None:
    provider, _host, store, allocation_id = _rig()
    machine_id = await provider.run(_launch(allocation_id))
    await provider.terminate(machine_id)
    assert (await provider.describe(machine_id)).phase == GONE
    assert store.forgotten is False


async def test_terminate_of_a_removed_machine_forgets_the_credential() -> None:
    provider, host, store, _allocation_id = _rig(deleted=True)
    await provider.terminate(str(store.endpoint.id))
    assert host.state == "absent"
    assert store.forgotten is True


async def test_an_unreachable_host_is_a_transient_error_never_gone() -> None:
    provider, host, _store, allocation_id = _rig()
    machine_id = await provider.run(_launch(allocation_id))
    host.reachable = False
    with pytest.raises(SshError) as raised:
        await provider.describe(machine_id)
    assert raised.value.kind == TRANSIENT_FAILURE


@pytest.mark.parametrize(
    ("state", "note"),
    [
        pytest.param("inactive", "ApiError: unreachable: Connection refused", id="crash-looping"),
        pytest.param("active", "ApiError: unreachable: Connection refused", id="between-restarts"),
        pytest.param("absent", "", id="nothing-installed"),
    ],
)
async def test_describe_carries_the_nodes_last_logged_error(state: str, note: str) -> None:
    provider, host, _store, allocation_id = _rig()
    machine_id = await provider.run(_launch(allocation_id))
    host.state = state
    host.node_error = "ApiError: unreachable: Connection refused"
    assert (await provider.describe(machine_id)).note == note


def test_the_state_probe_reads_its_error_line_wherever_it_falls() -> None:
    assert parse_state("inactive\nnode-error: ApiError: boom\n") == (
        "inactive",
        "",
        "ApiError: boom",
    )
    assert parse_state("active\nabc-123\nnode-error: KeyError: x\n") == (
        "active",
        "abc-123",
        "KeyError: x",
    )
    assert parse_state("") == ("", "", "")


async def test_a_machine_whose_endpoint_is_gone_reads_gone() -> None:
    provider, _host, _store, _ = _rig()
    assert (await provider.describe(str(uuid4()))).phase == GONE
    assert (await provider.describe("not-a-uuid")).phase == GONE


async def test_a_host_presenting_another_key_is_refused_before_anything_runs() -> None:
    provider, host, _store, allocation_id = _rig(key="ssh-ed25519 AAAAsomeone-else")
    with pytest.raises(HostKeyMismatchError) as raised:
        await provider.run(_launch(allocation_id))
    assert raised.value.kind == INVALID_FAILURE
    assert host.calls == []


async def test_a_removed_machine_is_never_launched() -> None:
    provider, host, _store, allocation_id = _rig(deleted=True)
    with pytest.raises(ComputeProviderError):
        await provider.run(_launch(allocation_id))
    assert host.calls == []


async def test_a_switched_off_deployment_acts_on_nothing() -> None:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(_endpoint(), allocation_id)
    provider = SshProvider(
        enabled=False, allow_private=True, transport=FakeTransport(host), store=store
    )
    assert provider.configured() is False
    with pytest.raises(ComputeProviderUnavailableError):
        await provider.run(_launch(allocation_id))
    with pytest.raises(ComputeProviderUnavailableError):
        await provider.list_pods()


def test_ssh_is_never_offered_as_a_catalog_size_and_attaches_hosts() -> None:
    traits = provider_traits("ssh")
    assert (traits.catalog_provisioned, traits.attaches_hosts) == (False, True)
    assert provider_traits("ec2").attaches_hosts is False


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("value'; rm -rf /", id="quote"),
        pytest.param(f"x\n{SECRETS_DELIMITER}\n", id="heredoc-end"),
    ],
)
def test_a_launch_refuses_a_secret_that_could_escape_its_file(secret: str) -> None:
    launch = _launch(uuid4())
    hostile = NodeLaunch(**{**launch.__dict__, "secrets": {"ALKERA_MACHINE_CREDENTIAL": secret}})
    with pytest.raises(ComputeProviderError):
        launch_script(hostile, machine_id="m")


def test_a_launch_refuses_a_bootstrap_that_would_end_its_heredoc() -> None:
    launch = _launch(uuid4())
    hostile = NodeLaunch(
        **{**launch.__dict__, "script": "echo a\nALKERA_SSH_BOOTSTRAP\nrm -rf /\n"}
    )
    with pytest.raises(ComputeProviderError):
        launch_script(hostile, machine_id="m")


@pytest.mark.parametrize(
    ("answer", "allow_private", "refused"),
    [
        pytest.param("169.254.169.254", True, True, id="metadata-always-refused"),
        pytest.param("10.0.0.5", False, True, id="private-refused-when-not-allowed"),
        pytest.param("127.0.0.1", False, True, id="loopback-refused-when-not-allowed"),
        pytest.param("10.0.0.5", True, False, id="private-allowed-when-allowed"),
        pytest.param("93.184.216.34", False, False, id="public-always-allowed"),
    ],
)
async def test_vetting_a_host(answer: str, allow_private: bool, refused: bool) -> None:
    def resolve(host: str, port: int) -> list[str]:
        return [answer]

    if refused:
        with pytest.raises(SshError) as raised:
            await vet_host("box.example", 22, allow_private=allow_private, resolve=resolve)
        assert raised.value.code == "address_not_allowed"
        return
    target = await vet_host("box.example", 22, allow_private=allow_private, resolve=resolve)
    assert (target.host, target.address, target.port) == ("box.example", answer, 22)


async def test_one_private_answer_among_public_ones_refuses_the_name() -> None:
    def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.5"]

    with pytest.raises(SshError):
        await vet_host("box.example", 22, allow_private=False, resolve=resolve)


async def test_a_name_that_does_not_resolve_is_unreachable() -> None:
    def resolve(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(SshError) as raised:
        await vet_host("nowhere.example", 22, allow_private=True, resolve=resolve)
    assert raised.value.code == "unreachable"


async def test_a_probe_with_an_unconfirmed_fingerprint_sends_no_credential() -> None:
    host = FakeHost()
    transport = FakeTransport(host)
    target = SshTarget(host="h", address="127.0.0.1", port=22)
    with pytest.raises(HostKeyMismatchError):
        await probe_host(
            transport,
            target,
            username="root",
            auth=SshAuth(kind="password", secret=CREDENTIAL),
            expected_fingerprint="SHA256:something-else",
        )
    assert transport.pinned == []


def test_a_sealed_credential_opens_to_what_was_sealed_and_hides_it() -> None:
    auth = SshAuth(kind="private_key", secret="-----BEGIN KEY-----", passphrase="pp")
    sealed = seal_credential(auth)
    assert "BEGIN KEY" not in sealed
    assert open_credential(sealed) == auth
    assert open_credential("") is None
    assert open_credential("not-ciphertext") is None
    assert CREDENTIAL not in repr(SshAuth(kind="password", secret=CREDENTIAL))


REMOVED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _removed_rig(
    *, hours_later: float, auth: bool = True
) -> tuple[SshProvider, FakeHost, MemoryStore]:
    host = FakeHost(reachable=False)
    endpoint = replace(
        _endpoint(deleted=True),
        machine_deleted_at=REMOVED_AT,
        auth=SshAuth(kind="password", secret=CREDENTIAL) if auth else None,
    )
    store = MemoryStore(endpoint, uuid4())
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        clock=lambda: REMOVED_AT + timedelta(hours=hours_later),
    )
    return provider, host, store


async def test_a_removed_machine_unreachable_for_a_day_finishes_with_a_note() -> None:
    provider, _host, store = _removed_rig(hours_later=24)
    described = await provider.describe(str(store.endpoint.id))
    assert (described.phase, described.raw_status) == (GONE, "abandoned")
    assert "may still be installed" in described.note
    assert "systemctl disable --now alkera-node.service" in described.note
    assert store.abandoned == [described.note]
    assert CREDENTIAL not in described.note


async def test_a_removed_machine_unreachable_for_less_than_a_day_keeps_trying() -> None:
    provider, _host, store = _removed_rig(hours_later=23.9)
    with pytest.raises(SshError):
        await provider.describe(str(store.endpoint.id))
    assert store.abandoned == [] and store.forgotten is False


async def test_a_live_machine_is_never_abandoned_however_long_it_is_down() -> None:
    host = FakeHost(reachable=False)
    store = MemoryStore(_endpoint(), uuid4())
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        clock=lambda: REMOVED_AT + timedelta(days=30),
    )
    with pytest.raises(SshError):
        await provider.describe(str(store.endpoint.id))
    assert store.abandoned == []


async def test_a_removed_machine_whose_credential_is_gone_reads_gone() -> None:
    provider, host, store = _removed_rig(hours_later=1, auth=False)
    assert (await provider.describe(str(store.endpoint.id))).phase == GONE
    assert host.calls == []


def _bundle_dir(tmp: Path, body: bytes = b"the bundle") -> Path:
    (tmp / "alkera-linux-x64.tar.gz").write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    (tmp / "manifest.json").write_text(
        json.dumps(
            {
                "version": "1.0.0+abc",
                "targets": {
                    "linux-x64": {
                        "file": "alkera-linux-x64.tar.gz",
                        "sha256": sha,
                        "size": len(body),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp


async def test_a_launch_uploads_the_bundle_and_stages_it_against_its_digest(tmp_path: Path) -> None:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(_endpoint(), allocation_id)
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        bundle_dir=str(_bundle_dir(tmp_path)),
    )
    await provider.run(_launch(allocation_id))
    [(name, body)] = host.uploads.items()
    assert body == b"the bundle"
    launch_stdin = host.calls[0][2]
    uploaded = f"/home/ubuntu/{name}"
    assert f"uploaded={uploaded}" in launch_stdin
    assert hashlib.sha256(b"the bundle").hexdigest() in launch_stdin
    assert "/opt/alkera/node-bundle-linux-x64.tar.gz" in launch_stdin
    # Staged before the bootstrap starts, so it finds the bundle in place.
    assert launch_stdin.index("node-bundle-linux-x64.tar.gz") < launch_stdin.index("nohup setsid")


async def test_without_a_bundle_the_launch_uploads_nothing() -> None:
    provider, host, _store, allocation_id = _rig()
    await provider.run(_launch(allocation_id))
    assert host.uploads == {}
    assert "node-bundle" not in host.calls[0][2]


async def test_a_host_of_an_architecture_with_no_bundle_is_refused(tmp_path: Path) -> None:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(replace(_endpoint(), arch="riscv64"), allocation_id)
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        bundle_dir=str(_bundle_dir(tmp_path)),
    )
    with pytest.raises(ComputeProviderError):
        await provider.run(_launch(allocation_id))
    assert host.uploads == {} and host.calls == []


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sha256sum") is None, reason="runs the staging lines"
)
@pytest.mark.parametrize("matches", [True, False], ids=["digest-matches", "digest-differs"])
def test_the_staging_lines_keep_only_a_bundle_that_hashes_right(
    tmp_path: Path, matches: bool
) -> None:
    uploaded = tmp_path / "upload.tar.gz"
    uploaded.write_bytes(b"the bundle")
    box = tmp_path / "box"
    box.mkdir()
    sha = hashlib.sha256(b"the bundle" if matches else b"something else").hexdigest()
    lines = staging_lines(StagedBundle(uploaded=str(uploaded), target="linux-x64", sha256=sha))
    script = "\n".join(lines).replace("/opt/alkera", str(box)).replace("chown root:root", "true")
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    staged = box / "node-bundle-linux-x64.tar.gz"
    if matches:
        assert done.returncode == 0, done.stderr
        assert staged.read_bytes() == b"the bundle"
        assert (box / "node-bundle-linux-x64.tar.gz.sha256").read_text().startswith(sha)
    else:
        assert done.returncode == 3
        assert not staged.exists() and not uploaded.exists()


def _two_bundles(tmp: Path, held: tuple[str, ...]) -> Path:
    entries = {}
    for target in held:
        body = f"bundle {target}".encode()
        name = f"alkera-{target}.tar.gz"
        (tmp / name).write_bytes(body)
        entries[target] = {"file": name, "sha256": hashlib.sha256(body).hexdigest(), "size": 1}
    (tmp / "manifest.json").write_text(
        json.dumps({"version": "1.0.0+abc", "targets": entries}), encoding="utf-8"
    )
    return tmp


@pytest.mark.parametrize(
    ("arch", "target"),
    [
        pytest.param("x86_64", "linux-x64", id="x86_64"),
        pytest.param("aarch64", "linux-arm64", id="aarch64"),
    ],
)
async def test_a_launch_uploads_the_bundle_of_the_hosts_architecture(
    tmp_path: Path, arch: str, target: str
) -> None:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(replace(_endpoint(), arch=arch), allocation_id)
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        bundle_dir=str(_two_bundles(tmp_path, ("linux-x64", "linux-arm64"))),
    )
    await provider.run(_launch(allocation_id))
    [(_name, body)] = host.uploads.items()
    assert body == f"bundle {target}".encode()
    assert f"/opt/alkera/node-bundle-{target}.tar.gz" in host.calls[0][2]


async def test_a_launch_on_a_host_whose_bundle_was_not_built_says_what_to_rebuild(
    tmp_path: Path,
) -> None:
    host = FakeHost()
    allocation_id = uuid4()
    store = MemoryStore(replace(_endpoint(), arch="x86_64"), allocation_id)
    provider = SshProvider(
        enabled=True,
        allow_private=True,
        transport=FakeTransport(host),
        store=store,
        bundle_dir=str(_two_bundles(tmp_path, ("linux-arm64",))),
    )
    with pytest.raises(ComputeProviderError, match="only has the arm64 machine bundle"):
        await provider.run(_launch(allocation_id))
    assert host.uploads == {} and host.calls == []


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="runs the uninstall under bash"
)
def test_the_uninstall_leaves_the_operators_host_firewall_as_it_found_it(tmp_path: Path) -> None:
    """An attached host is its operator's: the uninstall stops the node first
    (so nothing re-adds a rule), then takes back every firewall change the
    prerequisites and the node made. A boot include left behind would name a
    file the uninstall deletes, and the host's nftables would fail at its next
    boot; a DOCKER-USER accept left behind would let a link the host no longer
    has through Docker. Every host path is moved under ``tmp_path`` and every
    system command is a shim that logs what it was asked."""
    from alkera_core.compute import box_egress as egress
    from alkera_core.compute.bootstrap import BOX_HOME
    from alkera_core.compute.ssh.provider import BOX_ROOT, UNIT_PATH

    log = tmp_path / "calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "nft", "ip6tables"):
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
    (bin_dir / "iptables").write_text(
        "#!/bin/sh\n"
        f'echo "iptables $*" >> "{log}"\n'
        'case "$2" in -S) echo "-A DOCKER-USER -i vo+ -m comment --comment alkera-sandbox'
        ' -j ACCEPT";; esac\n'
    )
    for shim in bin_dir.iterdir():
        shim.chmod(0o755)
    host = {
        BOX_ROOT: tmp_path / "opt-alkera",
        BOX_HOME: tmp_path / "home-alkera",
        "/etc/alkera": tmp_path / "etc-alkera",
        UNIT_PATH: tmp_path / "alkera-node.service",
        egress.NFTABLES_CONF: tmp_path / "nftables.conf",
        egress.SYSCTL_FILE: tmp_path / "90-alkera-sandbox.conf",
        "/etc/ufw": tmp_path / "etc-ufw",
        "/etc/firewalld": tmp_path / "etc-firewalld",
    }
    for directory in (host[BOX_ROOT], host[BOX_HOME], host["/etc/alkera"]):
        directory.mkdir()
    host[UNIT_PATH].write_text("[Unit]\n")
    # The ruleset lives in /etc/alkera, which moves with it.
    included = egress.NFT_FILE.replace("/etc/alkera", str(host["/etc/alkera"]))
    host[egress.NFTABLES_CONF].write_text(
        f'flush ruleset\ninclude "/etc/mine.nft"\n\ninclude "{included}"\n'
    )
    host[egress.SYSCTL_FILE].write_text("net.ipv4.ip_forward = 1\n")
    script = UNINSTALL_SCRIPT
    for real, moved in sorted(host.items(), key=lambda item: -len(item[0])):
        script = script.replace(real, str(moved))

    done = subprocess.run(
        ["bash", "-s"],
        input=script,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("removed")
    calls = log.read_text().splitlines()
    assert calls[0] == "systemctl disable --now alkera-node.service"
    assert "nft delete table inet alkera_sandbox" in calls
    assert (
        "iptables -w -D DOCKER-USER -i vo+ -m comment --comment alkera-sandbox -j ACCEPT" in calls
    )
    assert host[egress.NFTABLES_CONF].read_text().splitlines() == [
        "flush ruleset",
        'include "/etc/mine.nft"',
    ]
    assert not any(path.exists() for path in host.values() if path != host[egress.NFTABLES_CONF])
