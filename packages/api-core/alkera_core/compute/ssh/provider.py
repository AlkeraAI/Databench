"""The ``ssh`` provider: a Linux host an org attached by its SSH details.

The plane never buys, prices or powers the host. Its lifecycle is the node
daemon's on that host:

- ``run`` writes the rendered bootstrap and a root-only file with the node's
  secrets under ``/opt/alkera`` and starts the bootstrap detached, as root
  (directly, or through ``sudo -n``). The machine id is the endpoint's id.
- ``describe`` reads one word off the host: the ``alkera-node`` unit active
  (``running``), installed and inactive (``stopped``), the bootstrap still
  installing (``running``: the node reconcile's boot timeout bounds it), or
  nothing of ours there (``gone``). A host that cannot be reached raises a
  transient error and is never read as gone, so an outage does not release it.
- ``stop`` / ``start`` stop and start the unit; ``terminate`` uninstalls what
  the bootstrap installed (the data directory stays) and, once the org machine
  was removed, forgets the stored credential.

Every script goes to the host on stdin, never on argv.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery, unknown
from alkera_core.compute.bootstrap import BOX_HOME, SSH_MACHINE_ID_ENV
from alkera_core.compute.box_egress import NFT_TABLE
from alkera_core.compute.disk import DiskBounds, DiskRules, DiskRulesTable, GrowRule
from alkera_core.compute.host_forward import host_firewall_removal_script
from alkera_core.compute.node_bundle import (
    BundleFile,
    NodeBundleError,
    bundle_file,
    install_refusal,
    target_for_machine,
)
from alkera_core.compute.org_machines import (
    UNLISTED_STORAGE_MAX_GB,
    ProviderTimings,
    register_timings,
)
from alkera_core.compute.provider import (
    GONE,
    INVALID_FAILURE,
    RUNNING,
    SSH,
    STOPPED,
    TRANSIENT_FAILURE,
    UNKNOWN,
    ComputeProvider,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    NodeDescription,
    NodeLaunch,
    PodPhase,
    PodStatus,
    ProviderPod,
    ProviderTraits,
    register_provider,
)
from alkera_core.compute.ssh.endpoints import DbEndpointStore, Endpoint, EndpointStore
from alkera_core.compute.ssh.transport import (
    AsyncsshTransport,
    SshError,
    SshSession,
    SshTransport,
    vet_host,
)

if TYPE_CHECKING:
    from alkera_core.config import Settings
    from alkera_core.models.compute import ComputeMachineType

#: What the provider installs on a host and removes on terminate.
BOX_ROOT = "/opt/alkera"
UNIT = "alkera-node.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT}"
#: The file naming the allocation the host was last launched for.
LAUNCH_MARKER = f"{BOX_ROOT}/ssh-launch"
SECRETS_FILE = f"{BOX_ROOT}/ssh-node.env"
BOOTSTRAP_FILE = f"{BOX_ROOT}/ssh-bootstrap.sh"
#: The heredoc delimiters the launch embeds the bootstrap and secrets under.
BOOTSTRAP_DELIMITER = "ALKERA_SSH_BOOTSTRAP"
SECRETS_DELIMITER = "ALKERA_SSH_SECRETS"

#: The one-word answers the state probe prints, and what each one means.
STATE_PHASES: dict[str, PodPhase] = {
    "active": RUNNING,
    "installing": RUNNING,
    "inactive": STOPPED,
    "absent": GONE,
}

#: The prefix of the state probe's line carrying the node's last logged error.
NODE_ERROR_PREFIX = "node-error:"

STATE_SCRIPT = f"""\
if systemctl is-active --quiet {UNIT} 2>/dev/null; then echo active
elif [ -f {UNIT_PATH} ]; then echo inactive
elif [ -f {LAUNCH_MARKER} ]; then echo installing
else echo absent; fi
[ -f {LAUNCH_MARKER} ] && cat {LAUNCH_MARKER} || true
journalctl -u {UNIT} -n 400 --no-pager -o cat 2>/dev/null \\
  | grep -E '[A-Za-z]+(Error|Exception): ' | tail -n 1 | sed 's/^/{NODE_ERROR_PREFIX} /' || true
"""


def parse_state(stdout: str) -> tuple[str, str, str]:
    """The state probe's word, the allocation the host was launched for, and
    the node's last logged error (each empty when the host printed none)."""
    word, launched_for, node_error = "", "", ""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(NODE_ERROR_PREFIX):
            node_error = line.removeprefix(NODE_ERROR_PREFIX).strip()[:500]
        elif not word:
            word = line.split()[0]
        elif not launched_for:
            launched_for = line.split()[0]
    return word, launched_for, node_error


#: The node goes first, so nothing re-adds a firewall rule while it is removed;
#: then the firewall the prerequisites added to a host its operator shares.
UNINSTALL_SCRIPT = f"""\
systemctl disable --now {UNIT} 2>/dev/null || true
rm -f {UNIT_PATH}
systemctl daemon-reload 2>/dev/null || true
{host_firewall_removal_script()}rm -rf {BOX_ROOT} {BOX_HOME} /etc/alkera
echo removed
"""

UNAVAILABLE = "an attached host is reached by its machine id; it has no pods to list"

#: How long a removed machine's host may stay unreachable before the removal
#: finishes without the uninstall.
ABANDON_AFTER = timedelta(hours=24)


def abandoned_note(host: str) -> str:
    """What a person reads when a removal finished without reaching the host."""
    return (
        f"{host} could not be reached for 24 hours after the machine was removed, so the "
        "removal finished without it and the credential was deleted. The Alkera node may "
        "still be installed there. To remove it, run as root on the host: "
        f"systemctl disable --now {UNIT}; rm -f {UNIT_PATH}; "
        f"nft delete table {' '.join(NFT_TABLE)}; "
        f"rm -rf {BOX_ROOT} {BOX_HOME} /etc/alkera"
    )


#: Secret values the launch writes must be plain tokens.
_SAFE_SECRET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:+-=/")


def as_root(username: str) -> str:
    """The command that runs a script read from stdin as root on the host."""
    return "bash -s" if username == "root" else "sudo -n bash -s"


@dataclass(frozen=True)
class StagedBundle:
    """A node bundle already copied to the host, and what it must hash to."""

    uploaded: str
    target: str
    sha256: str


def staging_lines(staged: StagedBundle) -> list[str]:
    """Move the uploaded bundle where the bootstrap looks for it, refusing it
    unless it hashes to the deployment's digest."""
    dest = f"{BOX_ROOT}/node-bundle-{staged.target}.tar.gz"
    return [
        f"uploaded={shlex.quote(staged.uploaded)}",
        f'[ "$(sha256sum "$uploaded" | cut -d\' \' -f1)" = {staged.sha256} ]'
        ' || { rm -f "$uploaded"; echo "the uploaded node bundle does not match"; exit 3; }',
        f'mv "$uploaded" {dest}',
        f"chown root:root {dest}",
        f"printf '%s  %s\\n' {staged.sha256} node-bundle.tar.gz >{dest}.sha256",
    ]


def launch_script(
    launch: NodeLaunch, *, machine_id: str, staged: StagedBundle | None = None
) -> str:
    """The script that installs the bootstrap and its secrets and starts it
    detached. Read on the host's stdin; nothing here is ever on argv."""
    script = launch.script
    if BOOTSTRAP_DELIMITER in script.splitlines():
        raise ComputeProviderError(
            "the bootstrap cannot be embedded in the launch", kind=INVALID_FAILURE
        )
    secrets = {**launch.secrets, SSH_MACHINE_ID_ENV: machine_id}
    for key, value in secrets.items():
        if not key.isidentifier() or not set(value) <= _SAFE_SECRET:
            raise ComputeProviderError(
                f"refusing a node secret {key} with unexpected characters", kind=INVALID_FAILURE
            )
    env_lines = "\n".join(f"{key}={shlex.quote(value)}" for key, value in sorted(secrets.items()))
    return "\n".join(
        [
            "set -euo pipefail",
            "umask 077",
            f"mkdir -p {BOX_ROOT}",
            *(staging_lines(staged) if staged is not None else []),
            f"cat >{SECRETS_FILE} <<'{SECRETS_DELIMITER}'",
            env_lines,
            SECRETS_DELIMITER,
            f"cat >{BOOTSTRAP_FILE} <<'{BOOTSTRAP_DELIMITER}'",
            script.rstrip("\n"),
            BOOTSTRAP_DELIMITER,
            f"printf '%s\\n' {shlex.quote(str(launch.allocation_id))} >{LAUNCH_MARKER}",
            f"nohup setsid bash -c 'set -a; . {SECRETS_FILE}; set +a; rm -f {SECRETS_FILE};"
            f" exec bash {BOOTSTRAP_FILE}' </dev/null >/dev/null 2>&1 &",
            "echo started",
        ]
    )


class SshProvider:
    """Implements the whole ``ComputeProvider`` surface (module docstring)."""

    kind = SSH

    def __init__(
        self,
        *,
        enabled: bool,
        allow_private: bool,
        transport: SshTransport | None = None,
        store: EndpointStore | None = None,
        clock: Callable[[], datetime] | None = None,
        bundle_dir: str = "",
    ) -> None:
        self._bundle_dir = bundle_dir
        self._enabled = enabled
        self._allow_private = allow_private
        self._clock = clock or (lambda: datetime.now(UTC))
        self._transport: SshTransport = transport or AsyncsshTransport()
        self._store: EndpointStore = store or DbEndpointStore()

    def configured(self) -> bool:
        return self._enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ComputeProviderUnavailableError("machines added over SSH are off here")

    async def _endpoint(self, machine_id: str) -> Endpoint | None:
        try:
            endpoint_id = UUID(machine_id)
        except ValueError:
            return None
        return await self._store.get(endpoint_id)

    @asynccontextmanager
    async def _session(self, endpoint: Endpoint) -> AsyncIterator[SshSession]:
        if endpoint.auth is None:
            raise SshError(
                "credential_missing",
                "The machine's credential is no longer stored.",
                kind=INVALID_FAILURE,
            )
        target = await vet_host(endpoint.host, endpoint.port, allow_private=self._allow_private)
        async with self._transport.session(
            target, username=endpoint.username, auth=endpoint.auth, pinned_key=endpoint.host_key
        ) as session:
            yield session

    async def _as_root(self, endpoint: Endpoint, script: str) -> str:
        async with self._session(endpoint) as session:
            done = await session.run(as_root(endpoint.username), stdin=script)
        if done.exit_status != 0:
            raise ComputeProviderError(
                f"the host answered exit status {done.exit_status}", kind=INVALID_FAILURE
            )
        return done.stdout

    async def _state(self, endpoint: Endpoint) -> tuple[str, str, str]:
        return parse_state(await self._as_root(endpoint, STATE_SCRIPT))

    # -- the node lifecycle -------------------------------------------------------

    async def run(self, launch: NodeLaunch) -> str:
        self._require_enabled()
        endpoint = await self._store.for_allocation(launch.allocation_id)
        if endpoint is None or endpoint.machine_deleted:
            raise ComputeProviderError(
                "no attached host backs this allocation", kind=INVALID_FAILURE
            )
        machine_id = str(endpoint.id)
        bundle = self._bundle_for(endpoint)
        async with self._session(endpoint) as session:
            staged = None
            if bundle is not None:
                # Over this connection, so the host needs no route to anything.
                uploaded = await session.upload(
                    bundle.path, f"alkera-node-bundle-{launch.allocation_id}.tar.gz"
                )
                staged = StagedBundle(uploaded=uploaded, target=bundle.target, sha256=bundle.sha256)
            done = await session.run(
                as_root(endpoint.username),
                stdin=launch_script(launch, machine_id=machine_id, staged=staged),
            )
        if done.exit_status != 0:
            raise ComputeProviderError(
                f"the host answered exit status {done.exit_status}", kind=INVALID_FAILURE
            )
        return machine_id

    def _bundle_for(self, endpoint: Endpoint) -> BundleFile | None:
        """The bundle this deployment built for the host's architecture, or
        ``None`` when it holds none (the bootstrap then gets the daemon from
        the source the deployment registered)."""
        if not self._bundle_dir:
            return None
        refusal = install_refusal(self._bundle_dir, endpoint.arch)
        if refusal is not None:
            raise ComputeProviderError(refusal, kind=INVALID_FAILURE)
        try:
            return bundle_file(self._bundle_dir, target_for_machine(endpoint.arch))
        except NodeBundleError as exc:
            raise ComputeProviderError(str(exc), kind=INVALID_FAILURE) from exc

    async def describe(self, machine_id: str) -> NodeDescription:
        self._require_enabled()
        endpoint = await self._endpoint(machine_id)
        if endpoint is None:
            return NodeDescription(machine_id=machine_id, phase=GONE, raw_status="no endpoint")
        if endpoint.machine_deleted and endpoint.auth is None:
            # Removed, and the credential is gone: nothing can reach the host.
            return NodeDescription(machine_id=machine_id, phase=GONE, raw_status="forgotten")
        try:
            word, _, node_error = await self._state(endpoint)
        except SshError as exc:
            if exc.kind != TRANSIENT_FAILURE or not self._past_removal_bound(endpoint):
                raise
            note = abandoned_note(endpoint.host)
            await self._store.abandon(endpoint, note)
            return NodeDescription(
                machine_id=machine_id, phase=GONE, raw_status="abandoned", note=note
            )
        # Why a node on the host has not registered, from its own log. A
        # crash-looping unit reads active between restarts, so the error is
        # carried whatever the word; a host with nothing installed has none.
        return NodeDescription(
            machine_id=machine_id,
            phase=STATE_PHASES.get(word, UNKNOWN),
            raw_status=word,
            public_ip=endpoint.host,
            note=node_error if word != "absent" else "",
        )

    def _past_removal_bound(self, endpoint: Endpoint) -> bool:
        removed = endpoint.machine_deleted_at
        return (
            endpoint.machine_deleted
            and removed is not None
            and self._clock() - removed >= ABANDON_AFTER
        )

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        self._require_enabled()
        endpoint = await self._store.for_allocation(allocation_id)
        if endpoint is None:
            return None
        word, launched_for, _ = await self._state(endpoint)
        if launched_for != str(allocation_id):
            return None
        return NodeDescription(
            machine_id=str(endpoint.id),
            phase=STATE_PHASES.get(word, UNKNOWN),
            raw_status=word,
            public_ip=endpoint.host,
        )

    async def stop(self, machine_id: str) -> None:
        self._require_enabled()
        endpoint = await self._endpoint(machine_id)
        if endpoint is None:
            return
        await self._as_root(endpoint, f"systemctl stop {UNIT}\n")

    async def start(self, machine_id: str) -> None:
        self._require_enabled()
        endpoint = await self._endpoint(machine_id)
        if endpoint is None:
            raise ComputeProviderError("the attached host is gone", kind=INVALID_FAILURE)
        await self._as_root(endpoint, f"systemctl start {UNIT}\n")

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        """An SSH host's disk is its operator's: the plane never grows it."""
        raise ComputeProviderUnavailableError(UNAVAILABLE)

    async def terminate(self, machine_id: str) -> None:
        self._require_enabled()
        endpoint = await self._endpoint(machine_id)
        if endpoint is None:
            return
        if endpoint.auth is not None:
            await self._as_root(endpoint, UNINSTALL_SCRIPT)
        if endpoint.machine_deleted:
            await self._store.forget(endpoint.id)

    async def terminate_pod(self, pod_id: str) -> None:
        await self.terminate(pod_id)

    async def pod_status(self, pod_id: str) -> PodStatus:
        described = await self.describe(pod_id)
        return PodStatus(pod_id=pod_id, phase=described.phase, raw_status=described.raw_status)

    def normalize_status(self, raw_status: str) -> PodPhase:
        return STATE_PHASES.get(raw_status, UNKNOWN)

    # -- what the node carries with it -------------------------------------------

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        """The secrets travel with the launch; nothing is kept here."""
        self._require_enabled()

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        self._require_enabled()

    async def delete_credential(self, allocation_id: UUID) -> None:
        """The node's secret is on the host and goes with ``terminate``."""

    # -- what an attached host does not have -------------------------------------

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        raise ComputeProviderUnavailableError(UNAVAILABLE)

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        raise ComputeProviderUnavailableError(UNAVAILABLE)

    async def catalog_prices(self, sizes: list[SizeQuery] | None = None) -> dict[str, int]:
        raise ComputeProviderUnavailableError(UNAVAILABLE)

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, Any]]:
        raise ComputeProviderUnavailableError(UNAVAILABLE)

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        return {s.code: unknown("an attached host has no stock") for s in sizes}


def make_ssh_provider(settings: Settings) -> ComputeProvider:
    """Registry factory, annotated as the Protocol so the typechecker proves the
    class still implements the surface."""
    return SshProvider(
        enabled=settings.ssh_machines_on,
        allow_private=settings.ssh_machines_private_ok,
        bundle_dir=settings.node_bundle_dir,
    )


register_provider(
    SSH,
    make_ssh_provider,
    traits=ProviderTraits(catalog_provisioned=False, attaches_hosts=True),
)
# The host is already up: starting is the install, then the first heartbeat.
register_timings(SSH, "cpu", ProviderTimings(reserving=0, booting=10, installing=240))
register_timings(SSH, "gpu", ProviderTimings(reserving=0, booting=10, installing=300))

#: The operator's own disk: whatever size the host has, never billed by the
#: plane and never grown by it. The volume bounds match the unlisted offering an
#: added host gets, so the host's measured size is always admitted.
_SSH_DISKS = DiskRules(
    container=DiskBounds(0, 0, 0),
    volume=DiskBounds(min_gb=1, max_gb=UNLISTED_STORAGE_MAX_GB, default_gb=1),
    volume_billed_while_stopped=False,
    grow=GrowRule.NEVER,
)
DISK_RULES: DiskRulesTable = {(SSH, c): _SSH_DISKS for c in ("cpu", "gpu")}

__all__ = [
    "ABANDON_AFTER",
    "BOOTSTRAP_DELIMITER",
    "LAUNCH_MARKER",
    "SECRETS_DELIMITER",
    "STATE_PHASES",
    "STATE_SCRIPT",
    "UNINSTALL_SCRIPT",
    "SshProvider",
    "abandoned_note",
    "as_root",
    "launch_script",
    "make_ssh_provider",
]
