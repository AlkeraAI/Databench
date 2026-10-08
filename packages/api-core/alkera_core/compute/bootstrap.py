"""The first-boot script of a machine the plane provisions, one render for
every provider.

It installs the released daemon the backend advertises from the release host
(``<release base>/cli/v<version>/linux-x64/alkera-linux-x64.tar.gz``, verified
against its ``.sha256`` sidecar), mounts the data volume at
``/opt/alkera-work``, writes the node's environment file (0600) and a systemd
unit that keeps the box up, and starts it. Every box starts with
``alkera cloud-mirror run``, which every build has. How it runs comes from the
:class:`~alkera_core.compute.box_contract.BootstrapPlan` resolved from the
build's manifest and the provider's boot profile, written as
``ALKERA_BOX_START_MODE``: a build with the per-org supervisor hands over to
it, any other runs the single daemon. So the unit never names a command a
later (or rolled-back) build might lack. The box then claims the machine its
credential names, which moves the row to ``ready``.

The node's secrets are ``ALKERA_MACHINE_CREDENTIAL`` (the machine credential
it claims with) and ``ALKERA_BOX_TOKEN`` / ``ALKERA_BOX_TOKEN_EXPIRES`` (the CLI
login it runs under, written to ``auth.yml``). Where they come from is the only
provider difference:

- ``ec2``: one JSON Secrets Manager secret (``<prefix><allocation id>``,
  written before the instance was launched);
- ``runpod``: the pod's own env;
- ``localdev``: the container's own env (``localdev.py`` starts it);
- ``ssh``: the environment the plane starts the bootstrap with over SSH
  (``compute/ssh/provider.py``), read from a root-only file it removes first.

A ``localdev`` box is a privileged Docker container on a developer's machine,
so it also runs the daemon from the developer's source tree (mounted read-only
at :data:`LOCALDEV_SOURCE`), delegates the container's cgroup controllers so
``runsc`` can make its cgroups, and forwards the ports the backend names in
``localhost`` URLs to the developer's machine.

The release is resolved before rendering
(:func:`~alkera_core.compute.box_contract.resolve_release`), so the script
installs exactly the build whose manifest chose its command.

``sandbox_prereqs.sh`` (beside this module) is embedded whole and run as root
before the daemon starts. It installs gVisor and its rootfs on a ``gvisor``
node, and on every node the uid tools, Python, uv and micromamba, the parent
cgroup slice, and the firewall that keeps chat uids and namespaces off the
metadata service. The daemon starts only behind a marker
(:data:`SANDBOX_READY`) written once the prerequisites applied and the sandbox
works (for ``gvisor``, ``runsc`` runs a command in the rootfs), with any
earlier boot's marker removed first. A node whose sandbox is not ready never
claims, so it never reports a mode it cannot enforce.

Pure: plain data in, a string out. Nothing secret is ever in the rendered text.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from alkera_core.compute.box_contract import (
    START_COMMAND,
    START_MODE_ENV,
    BootstrapPlan,
    StartMode,
)
from alkera_core.compute.box_layout import (
    ORGS_DIR,
    ORGS_ROOT_MODE,
    WORK_ROOT,
    WORK_ROOT_MODE,
)
from alkera_core.compute.daemon_source import NodeDaemonSource, daemon_source
from alkera_core.compute.disk import ENV_MACHINE_TENANCY
from alkera_core.compute.liveness import (
    CHAT_IDLE_MINUTES,
    CHAT_MEMORY_PRESSURE_PERCENT,
    DRAIN_CEILING_SECONDS,
    ENV_CHAT_IDLE_MINUTES,
    ENV_CHAT_MEMORY_PRESSURE_PERCENT,
    ENV_DRAIN_CEILING_SECONDS,
    unit_stop_timeout_seconds,
)

DATA_MOUNT = WORK_ROOT


def work_root_lines() -> list[str]:
    """Lines that give the work root and the orgs root their modes, whatever
    the umask: each org's worker, as its own uid, walks through both."""
    return [
        "# Every org's worker walks through these as its own uid (box_layout).",
        f'chmod {WORK_ROOT_MODE:04o} "$DATA_MOUNT"',
        f'install -d -m {ORGS_ROOT_MODE:04o} "$DATA_MOUNT/{ORGS_DIR}"',
        f'chmod {ORGS_ROOT_MODE:04o} "$DATA_MOUNT/{ORGS_DIR}"',
    ]


#: How many times an EC2 node asks for its secret, and the longest wait
#: between asks. Waits double from two seconds to the cap: 2+4+8+16 and then
#: the cap, about five minutes in all before the boot gives up.
SECRET_READ_ATTEMPTS = 13
SECRET_READ_MAX_DELAY = 30
BOX_HOME = "/opt/alkera-home"
#: The file that tells the node's daemon a stop is final. The unit runs the
#: daemon supervised (``Restart=always``), and a supervised daemon treats a
#: stop signal as a restart in place: it keeps its leases, hands no chat back,
#: and the platform keeps the chats bound to the box, which the next process
#: takes back with their owed turns. A stop that is NOT followed by a start (a
#: shutdown, a sleep, ``systemctl stop``) must hand every chat on instead, so
#: the unit's stop step writes this file unless the job stopping the unit is a
#: restart. Unsupervised, every restart read as a departure: the platform moved
#: the box's chats to another box while this one still held their folders, the
#: hand-back was refused because the chat was no longer this box's, and the new
#: box could not take a folder whose lease had not lapsed — the prompt sat
#: unanswered.
FINAL_STOP_FILE = f"{BOX_HOME}/daemon.final-stop"
#: The unit's stop step. It runs before systemd signals the daemon, and it
#: writes the final-stop file unless systemd lists a ``restart`` job for the
#: unit. Every failure (no ``systemctl``, no job listed) falls to the final
#: stop — the full hand-back, which is safe everywhere, only slower to return.
#: It holds no ``$`` or ``%``: the unit is written through an expanding heredoc
#: and systemd expands both in command lines.
NODE_STOP_COMMAND = (
    '/bin/sh -c "systemctl list-jobs --no-legend --plain alkera-node.service 2>/dev/null'
    " | grep -Eq '[[:space:]]restart[[:space:]]'"
    f' || touch {FINAL_STOP_FILE}"'
)
_VERSION_RE = re.compile(r"^[0-9A-Za-z.-]+$")
#: The characters CloudWatch Logs allows in a log group name.
_LOG_GROUP_RE = re.compile(r"^[A-Za-z0-9._/#-]{1,512}$")

#: The sandbox prerequisites script, shipped beside this module so the backend
#: image renders it without a checkout.
SANDBOX_PREREQS_PATH = Path(__file__).with_name("sandbox_prereqs.sh")
#: Where a release build carries the prerequisites it was built with, under
#: the box root (the release build puts it in ``alkera.dist``),
#: and where a source box's checkout does. A box applies its build's copy
#: before each start when it differs from the one staged at boot.
BUILD_PREREQS = "alkera.dist/sandbox-prereqs.sh"
#: Where an installed build is linked onto ``PATH``.
PATH_LINK_DIR: Final = "/usr/local/bin"
PATH_LINK: Final = f"{PATH_LINK_DIR}/alkera"
SOURCE_PREREQS = "packages/api-core/alkera_core/compute/sandbox_prereqs.sh"
#: The script, under the box root, that does that before each start.
RESTAGE_PREREQS = "restage-prereqs.sh"
RESTAGE_DELIMITER = "ALKERA_RESTAGE_EOF"
#: The quoted heredoc delimiter the script is embedded under. Quoted, so
#: nothing in the script expands at boot; a script containing this line cannot
#: be embedded and the render refuses.
SANDBOX_PREREQS_DELIMITER = "ALKERA_SANDBOX_PREREQS"
#: The sandbox mode every provisioned node's daemon runs under. ``gvisor`` (an
#: EC2 pool box, the real boundary) or ``none`` (a single-tenant dedicated or
#: dev/RunPod box, no boundary). A ``gvisor`` box that cannot run runsc refuses
#: every chat rather than running it open.
SANDBOX_MODE = "gvisor"
#: The marker the boot writes once the sandbox applied; the daemon unit starts
#: only while it exists (``ConditionPathExists``), and a container with no init
#: checks it before its foreground loop.
SANDBOX_READY = "/etc/alkera/sandbox.ready"
#: Where the prerequisites stage the rootfs a gVisor container roots at, and
#: the stamp they write there last. The daemon reads the same paths
#: (``alkera_cli.harness.sandbox``); a test pins the two spellings together.
SANDBOX_ROOTFS = "/opt/alkera/rootfs/current"
SANDBOX_ROOTFS_STAMP = ".alkera-rootfs"
#: Where a ``localdev`` box sees the developer's source tree (read-only), the
#: venv its daemon runs from, and the daemon inside that venv.
LOCALDEV_SOURCE = "/opt/alkera-src"
LOCALDEV_VENV = "/opt/alkera-venv"
LOCALDEV_DAEMON = f"{LOCALDEV_VENV}/bin/alkera"
#: The Linux build of the opencode harness and its ripgrep, relative to the
#: source root (``ops/scripts/dev/build-localdev-agent.sh`` stages it there),
#: and where the box installs them at boot. A chat's agent runs under gVisor as
#: the chat's own uid, and gVisor will not load a binary straight from the
#: developer's mounted source tree, so the box runs a root-owned copy on its own
#: disk, as an EC2 node runs the agent its release ships.
LOCALDEV_AGENT_SOURCE = "apps/cli/dist/localdev-agent"
LOCALDEV_AGENT_DIR = "/opt/alkera/agent"
#: The umask every supervisor starts the daemon under: what it writes is the
#: owner's alone unless it names a mode (systemd's own default is 0022).
DAEMON_UMASK = "0077"
#: The host name a ``localdev`` box reaches the developer's machine by.
LOCALDEV_HOST = "host.docker.internal"
#: The environment variables ``localdev.py`` hands a box: its container name
#: (the machine id the daemon reports) and the ports to forward to the host.
LOCALDEV_CONTAINER_ENV = "ALKERA_LOCALDEV_CONTAINER"
LOCALDEV_FORWARD_ENV = "ALKERA_LOCALDEV_FORWARD_PORTS"
#: The environment variable an SSH-attached host's bootstrap reads its
#: machine id from (``compute/ssh/provider.py`` hands it over).
SSH_MACHINE_ID_ENV = "ALKERA_SSH_MACHINE_ID"
#: The lines that bracket the gate in the rendered script, so a test can cut
#: it out and run it.
SANDBOX_GATE_BEGIN = "# --- the sandbox gate ---"
SANDBOX_GATE_END = "# --- end of the sandbox gate ---"


def sandbox_prereqs_script() -> str:
    """The prerequisites script, read at render time so the rendered bootstrap
    and the file an operator runs by hand can never drift. Decoded as UTF-8
    whatever the host locale, and with CRLF folded to LF: a checkout that
    rewrote the line endings must not put a carriage return inside the bash
    heredoc the node executes."""
    return SANDBOX_PREREQS_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")


SecretSource = Literal["secrets_manager", "environment"]
DaemonSource = Literal["release", "source"]
Supervisor = Literal["systemd", "container"]
SandboxPolicy = Literal["deployment", "never", "always"]
HostScope = Literal["owned", "shared"]


@dataclass(frozen=True)
class NodeBootProfile:
    """How one provider's machines boot: every way a node differs by provider,
    declared once. The renderer and the provisioning read the profile and never
    branch on a provider's name, so a new provider is a profile here plus its
    module in the registry.
    """

    #: Where the node secrets come from: one Secrets Manager secret the node
    #: reads by name, or the machine's own environment.
    secrets: SecretSource
    #: For ``environment`` secrets: the variable holding the machine's id.
    machine_id_env: str = ""
    #: The released daemon, or the developer's source tree.
    daemon: DaemonSource = "release"
    #: A systemd unit (falling back to a foreground loop where there is no
    #: init), or a supervised foreground loop as a container's command.
    supervisor: Supervisor = "systemd"
    #: Format and mount the one blank attached disk at :data:`DATA_MOUNT`.
    formats_data_volume: bool = False
    #: The machine starts without curl and jq and installs them with apt.
    installs_base_packages: bool = True
    #: A privileged container standing in for a host: delegate its cgroups so
    #: ``runsc`` can make its own, and forward the developer machine's
    #: ``localhost`` ports into it.
    nested_container: bool = False
    #: Which sandbox mode its chats run under: what the deployment requires,
    #: never gVisor (a container that cannot nest a kernel), or always gVisor.
    sandbox: SandboxPolicy = "deployment"
    #: Whether the host firewall may be absent when chats run with no sandbox
    #: (a container with no CAP_NET_ADMIN and no metadata service to fence).
    firewall_optional_without_sandbox: bool = False
    #: The secret lives in a regional service, so the node needs the region.
    needs_region: bool = False
    #: The longest drain its supervisor waits for, when that is shorter than
    #: the deployment's: a container whose ``docker stop`` kills it after a
    #: minute must hand its chats back inside that minute, or none of the
    #: hand-backs (and none of their log lines) ever happen.
    drain_ceiling_cap_seconds: int | None = None
    #: How the box runs: a worker per org where the machine can give each its
    #: own namespaces and cgroup, else the single daemon.
    start_mode: StartMode = StartMode.SUPERVISE
    #: How the node reaches this deployment's API and gateway: from outside its
    #: network (``public``: the deployment needs an address a cloud machine can
    #: call back on), or over the developer machine's own host network
    #: (``host``: a container on this machine, which needs none).
    callback: Literal["public", "host"] = "public"
    #: Whose host the node runs on: one the plane provisioned for it alone
    #: (``owned``: the box firewall also drops whatever comes in or would be
    #: forwarded that is not the node's), or one its operator also uses for
    #: other things (``shared``: the box firewall touches only the node's own
    #: links and uids, and leaves every other service and container alone).
    host: HostScope = "owned"

    @property
    def needs_public_callback(self) -> bool:
        """Whether a node of this kind can claim and heartbeat only when the
        deployment has an address reachable from outside its own machine."""
        return self.callback == "public"

    def drain_ceiling(self, requested: int) -> int:
        """The drain ceiling a node of this kind is told, given the deployment's."""
        cap = self.drain_ceiling_cap_seconds
        return requested if cap is None else min(requested, cap)

    def sandbox_mode(self, requested: str) -> str:
        """The mode a node runs, given what the deployment requires."""
        if self.sandbox == "never":
            return "none"
        if self.sandbox == "always":
            return "gvisor"
        return requested


BOOT_PROFILES: dict[str, NodeBootProfile] = {
    "ec2": NodeBootProfile(
        secrets="secrets_manager",
        formats_data_volume=True,
        needs_region=True,
    ),
    # gVisor needs a real host; a RunPod pod is a container that cannot nest a
    # kernel, so it only ever runs mode "none" and is a single-tenant / dev box
    # the server keeps off the shared pool.
    "runpod": NodeBootProfile(
        secrets="environment",
        machine_id_env="RUNPOD_POD_ID",
        sandbox="never",
        firewall_optional_without_sandbox=True,
        # An unprivileged pod with no init cannot delegate cgroups or make the
        # namespaces an org worker runs in, so every worker would crash-loop.
        # The server already keeps a RunPod box to one tenant.
        start_mode=StartMode.SINGLE,
    ),
    # A local box exists to exercise gVisor's quirks before they reach a pool
    # box, so it never runs a chat open.
    "localdev": NodeBootProfile(
        secrets="environment",
        machine_id_env=LOCALDEV_CONTAINER_ENV,
        daemon="source",
        supervisor="container",
        installs_base_packages=False,
        nested_container=True,
        sandbox="always",
        # ``docker stop`` waits a minute (``localdev.STOP_TIMEOUT_SECONDS``);
        # half of it leaves the stop room to put what is left to sleep.
        drain_ceiling_cap_seconds=30,
        # It calls the API at ``host.docker.internal``.
        callback="host",
    ),
    # A Linux host an org attached over SSH: the plane writes the node's
    # secrets into the bootstrap's environment and starts it detached.
    "ssh": NodeBootProfile(
        secrets="environment",
        machine_id_env=SSH_MACHINE_ID_ENV,
        # The operator's own machine, which may run anything else (Docker
        # containers, services listening on their own ports).
        host="shared",
    ),
}
"""Every provider a node bootstrap can be rendered for, and how its nodes boot."""

#: The providers a bootstrap can be rendered for.
BOOTSTRAP_PROVIDERS: tuple[str, ...] = tuple(sorted(BOOT_PROFILES))


def boot_profile(provider: str) -> NodeBootProfile:
    """The boot profile for ``provider``; refuses a provider with none."""
    profile = BOOT_PROFILES.get(provider)
    if profile is None:
        raise BootstrapError(f"no bootstrap for provider {provider!r}")
    return profile


@dataclass(frozen=True)
class BootstrapSpec:
    provider: str
    allocation_id: str
    machine_name: str
    type_code: str
    tenancy: str
    api_url: str
    release_base_url: str
    #: EC2 only: the Secrets Manager id holding the machine credential.
    credential_secret: str = ""
    region: str = ""
    #: The model gateway the node's chats go through. Empty leaves the daemon
    #: its own default, which is production's gateway: a stack that is not
    #: production names its own here or its nodes' turns are refused there.
    gateway_url: str = ""
    #: The sandbox mode the node runs chats under: ``gvisor`` (the pool boundary)
    #: or ``none`` (a single-tenant dedicated or dev/RunPod box). A RunPod
    #: container can only ever be ``none`` and is kept off the shared pool by the
    #: server. Defaults to the module default.
    sandbox_mode: str = SANDBOX_MODE
    #: How long the node's daemon, told to stop, waits for the turns it holds.
    #: Written into the node's environment for the daemon and used to size the
    #: service unit's stop timeout, so the two can never disagree.
    drain_ceiling_seconds: int = DRAIN_CEILING_SECONDS
    #: How long a chat with nothing running and nobody using it stays awake
    #: on the node, in minutes; written into the node's environment for the
    #: daemon's idle sweep.
    chat_idle_minutes: int = CHAT_IDLE_MINUTES
    #: How full the chats' memory may get, in percent of its limit, before the
    #: daemon sleeps the least recently used idle chats.
    chat_memory_pressure_percent: int = CHAT_MEMORY_PRESSURE_PERCENT
    #: EC2 only: the CloudWatch Logs group the node ships its supervisor's
    #: events to on its instance role (the node log group the alarms filter
    #: on). Empty, the node ships them through the backend instead.
    log_group: str = ""


class BootstrapError(ValueError):
    """A spec the script cannot be rendered from."""


def _q(value: str) -> str:
    return shlex.quote(value)


#: Any character that could end a line of the script (and so a heredoc) or
#: that no config value needs.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: What the pod id and the machine credential may hold when the boot script
#: appends them to ``node.env`` (an instance id, a RunPod pod id, a urlsafe
#: token). Anything else is refused rather than written into a file that is
#: sourced by a root shell.
_RUNTIME_VALUE_CHECK = (
    'case "$POD_ID$MACHINE_CREDENTIAL" in *[!A-Za-z0-9._-]*)'
    ' echo "refusing a pod id or credential with unexpected characters"; exit 1 ;; esac'
)


def host_firewall_policy(provider: str, mode: str) -> str:
    """Whether the prerequisites may boot the node without the host firewall.

    The nftables ruleset is what keeps a chat uid away from the instance
    metadata service and other chats; loading it needs CAP_NET_ADMIN. A RunPod
    pod is a container that has neither the capability nor a metadata service
    to fence, and a ``none`` pod is single-tenant by construction (the server
    keeps it off the shared pool), so there the ruleset is ``optional``: the
    prerequisites log that it did not load and go on. Everywhere else —
    an EC2 host, where the firewall IS a ``none`` box's metadata boundary, and
    every ``gvisor`` node, whose per-chat network is built on it — it stays
    ``required`` and a boot that cannot load it refuses to start the daemon.
    """
    profile = boot_profile(provider)
    return (
        "optional" if profile.firewall_optional_without_sandbox and mode == "none" else "required"
    )


def sandbox_gate_lines(mode: str, provider: str) -> list[str]:
    """The lines that decide whether the daemon may start: the prerequisites
    must exit 0, and — for ``gvisor`` — ``runsc`` must actually run a command
    inside the staged rootfs; only then is the ready marker written. A marker
    an earlier boot left goes first, so a node whose sandbox broke since does
    not start on last boot's word. The prerequisites are told the mode and the
    host-firewall policy on their own environment — nothing else on a fresh
    node carries them yet. Pure: the block reads ``BOX_ROOT`` and its own
    variables, which is what lets a test cut it out and run it."""
    return [
        SANDBOX_GATE_BEGIN,
        "# The daemon starts only behind this marker: written when the prerequisites",
        "# applied AND the sandbox they installed actually works. A stale marker from",
        "# an earlier boot goes first.",
        f"SANDBOX_MODE={_q(mode)}",
        f"SANDBOX_HOST_FIREWALL={_q(host_firewall_policy(provider, mode))}",
        f"SANDBOX_HOST_SCOPE={_q(boot_profile(provider).host)}",
        f"SANDBOX_READY={SANDBOX_READY}",
        f"SANDBOX_ROOTFS={SANDBOX_ROOTFS}",
        'rm -f "$SANDBOX_READY"',
        "sandbox_applied=0",
        'if ALKERA_SANDBOX_MODE="$SANDBOX_MODE"'
        ' ALKERA_SANDBOX_HOST_FIREWALL="$SANDBOX_HOST_FIREWALL"'
        ' ALKERA_SANDBOX_HOST_SCOPE="$SANDBOX_HOST_SCOPE"'
        ' bash "$BOX_ROOT/sandbox-prereqs.sh"; then',
        '  if [ "$SANDBOX_MODE" = "gvisor" ]; then',
        "    if runsc --version >/dev/null 2>&1"
        f' && [ -f "$SANDBOX_ROOTFS/{SANDBOX_ROOTFS_STAMP}" ]'
        ' && runsc --network=none --platform=systrap do --root="$SANDBOX_ROOTFS" --cwd=/'
        " /bin/true; then",
        "      sandbox_applied=1",
        "    else",
        '      echo "the gVisor sandbox does not run on this node: runsc or the staged rootfs"',
        "    fi",
        "  else",
        "    sandbox_applied=1",
        "  fi",
        "else",
        '  echo "sandbox prerequisites failed"',
        "fi",
        'if [ "$sandbox_applied" = 1 ]; then',
        '  touch "$SANDBOX_READY"',
        "else",
        '  echo "the sandbox is not ready; the daemon will not start until'
        ' sandbox-prereqs.sh applies (ALKERA_SANDBOX_MODE=$SANDBOX_MODE)"',
        "fi",
        SANDBOX_GATE_END,
    ]


def restage_prereqs_lines(mode: str, provider: str) -> list[str]:
    """The script a box runs before each start of its daemon.

    The prerequisites staged at the first boot are the ones the backend
    rendered then; a box whose build has since moved on (an upgrade in place, a
    source box after a pull) would otherwise run that build on a host prepared
    for an older one. So when the build being started carries prerequisites
    that differ from the staged copy, they replace it and the sandbox gate runs
    again, and the start goes ahead only behind a fresh ready marker. A build
    from before builds carried them leaves everything as it was."""
    profile = boot_profile(provider)
    current = (
        f"{LOCALDEV_SOURCE}/{SOURCE_PREREQS}"
        if profile.daemon == "source"
        else f"$BOX_ROOT/{BUILD_PREREQS}"
    )
    return [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "BOX_ROOT=/opt/alkera",
        f'current="{current}"',
        '[ -f "$current" ] || exit 0',
        'cmp -s "$current" "$BOX_ROOT/sandbox-prereqs.sh" && exit 0',
        'echo "the build carries other sandbox prerequisites than the staged ones; applying them"',
        'install -m 700 "$current" "$BOX_ROOT/sandbox-prereqs.sh"',
        *sandbox_gate_lines(mode, provider),
        '[ -f "$SANDBOX_READY" ]',
    ]


def daemon_install_lines(source: NodeDaemonSource, *, api_url: str) -> list[str]:
    """Fetch the daemon from ``source``, then verify it and install it the same
    way whichever source it came from."""
    return [
        *source.fetch_lines(api_url=api_url),
        '[ "$(sha256sum "$work/release.tar.gz" | cut -d\' \' -f1)" = "$expected" ]'
        ' || { echo "sha256 mismatch; refusing to install"; exit 1; }',
        'tar -xzf "$work/release.tar.gz" -C "$work"',
        'rm -rf "$BOX_ROOT/alkera.dist" && mv "$work/alkera.dist" "$BOX_ROOT/alkera.dist"',
        "# Installed unpacked, root's, readable by every uid and writable by none but",
        "# root: every org's worker runs this same build, the harness included, from",
        "# here, and nothing is extracted into a cache at run time.",
        'chown -R root:root "$BOX_ROOT/alkera.dist"',
        'chmod -R u=rwX,go=rX "$BOX_ROOT/alkera.dist"',
        "# On PATH as well, for a person on the host. What the node spawns names the",
        "# build by its absolute path and never relies on this, so a host that",
        "# refuses the link still boots.",
        f'{{ mkdir -p {PATH_LINK_DIR} && ln -sfn "$BOX_ROOT/alkera.dist/alkera" {PATH_LINK}; }}'
        " 2>/dev/null || true",
        'rm -rf "$work"',
        *source.after_install_lines(),
    ]


def _source_install_lines() -> list[str]:
    """Install the daemon from the developer's source tree, mounted read-only:
    ``uv sync`` of the CLI package into a venv kept on its own volume, so a
    restart is a no-op sync and a source change takes effect on the next start.
    The lock is used as committed (``--frozen``); nothing is written to the
    source tree."""
    return [
        "# The daemon from the developer's source tree, synced into a cached venv.",
        f"SOURCE_ROOT={LOCALDEV_SOURCE}",
        f"export UV_PROJECT_ENVIRONMENT={LOCALDEV_VENV}",
        "export UV_LINK_MODE=copy",
        '[ -f "$SOURCE_ROOT/pyproject.toml" ] ||'
        ' { echo "no source tree at $SOURCE_ROOT"; exit 1; }',
        "# Installed readable by every uid: an org worker runs this same daemon. uv",
        "# copies files with the modes its cache holds, so the umask alone is not enough.",
        '(umask 022 && uv sync --frozen --no-dev --package alkera-cli --project "$SOURCE_ROOT")',
        'chmod -R go+rX "$UV_PROJECT_ENVIRONMENT"',
        'if [ -n "${UV_PYTHON_INSTALL_DIR:-}" ]; then chmod -R go+rX "$UV_PYTHON_INSTALL_DIR"; fi',
        f'[ -x {LOCALDEV_DAEMON} ] || {{ echo "the daemon did not install from source"; exit 1; }}',
        "# The Linux harness, installed root-owned on the box's own disk.",
        f'AGENT_SOURCE="$SOURCE_ROOT/{LOCALDEV_AGENT_SOURCE}"',
        '[ -x "$AGENT_SOURCE/opencode" ] && [ -x "$AGENT_SOURCE/rg" ] || {'
        ' echo "no Linux harness at $AGENT_SOURCE;'
        ' run ops/scripts/dev/build-localdev-agent.sh"; exit 1; }',
        f"install -d -m 0755 {LOCALDEV_AGENT_DIR}",
        f'install -m 0755 "$AGENT_SOURCE/opencode" "$AGENT_SOURCE/rg" {LOCALDEV_AGENT_DIR}/',
    ]


#: Where the released build carries the harness and ripgrep (its ``runtime/``
#: data files), named to the daemon so it runs them in place rather than
#: extracting a copy into each org's cache.
INSTALLED_HARNESS: Final = "/opt/alkera/alkera.dist/runtime/alkera-agent"
INSTALLED_RIPGREP: Final = "/opt/alkera/alkera.dist/runtime/rg"


def _installed_harness_lines() -> list[str]:
    return [f"ALKERA_OPENCODE_BIN={INSTALLED_HARNESS}", f"ALKERA_RIPGREP_BIN={INSTALLED_RIPGREP}"]


def _nested_container_lines() -> list[str]:
    """What a privileged container needs before the sandbox prerequisites run.

    cgroup v2 lets a cgroup hand controllers to its children only while it holds
    no process itself, and a container starts with every process in its root
    cgroup: ``runsc`` then cannot make the cgroups it runs a chat in. Moving the
    container's processes into a leaf and enabling every controller at the root
    is what a nested container runtime needs. The backend hands out ``localhost``
    URLs (a presigned Files URL names the store at ``localhost:<port>``), which
    inside the container would point back at itself, so each named port is
    forwarded to the developer's machine."""
    return [
        "# cgroup v2 delegation, so runsc can create the per-chat cgroups.",
        "if [ -f /sys/fs/cgroup/cgroup.controllers ]; then",
        "  mkdir -p /sys/fs/cgroup/init",
        "  # Read every pid first: the file shrinks as each one moves, and a",
        "  # line-by-line read skips the entries behind the one just moved.",
        "  for pid in $(cat /sys/fs/cgroup/cgroup.procs); do",
        '    echo "$pid" >/sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true',
        "  done",
        "  sed -e 's/ / +/g' -e 's/^/+/' /sys/fs/cgroup/cgroup.controllers"
        " >/sys/fs/cgroup/cgroup.subtree_control",
        "fi",
        "# The developer machine's localhost ports, reachable from inside the box.",
        f"for port in ${{{LOCALDEV_FORWARD_ENV}:-}}; do",
        '  case "$port" in ""|*[!0-9]*) echo "refusing forward port $port"; exit 1 ;; esac',
        f"  socat TCP-LISTEN:$port,bind=127.0.0.1,fork,reuseaddr TCP:{LOCALDEV_HOST}:$port &",
        "done",
    ]


def _container_supervisor_lines() -> list[str]:
    """Keep the daemon up as the container's foreground process, behind the
    sandbox gate, with the systemd unit's stop semantics: a crash restarts it in
    place (supervised, leases kept), and ``docker stop`` writes the final-stop
    file before signalling it, so every chat is handed on."""
    return [
        '[ -f "$SANDBOX_READY" ] || { echo "the sandbox is not ready; not starting"; exit 1; }',
        "set -a; [ -f /etc/alkera/sandbox.env ] && . /etc/alkera/sandbox.env;"
        ' . "$BOX_ROOT/node.env"; set +a',
        "export ALKERA_DAEMON_SUPERVISED=1",
        f"export ALKERA_DAEMON_FINAL_STOP_FILE={FINAL_STOP_FILE}",
        'cd "$DATA_MOUNT"',
        # Everything the daemon writes is its own unless it says otherwise: it
        # holds every org's state beside agents that run as other uids. What a
        # chat's uid must read is given a mode by hand, and runsc, whose
        # mountpoints take its umask, is started under 022 by the launch.
        f"umask {DAEMON_UMASK}",
        'child=""',
        'trap \'touch "$ALKERA_DAEMON_FINAL_STOP_FILE";'
        ' [ -n "$child" ] && kill -TERM "$child" 2>/dev/null; wait "$child"; exit 0\' TERM INT',
        "while true; do",
        '  rm -f "$ALKERA_DAEMON_FINAL_STOP_FILE"',
        # The box supervisor: one worker per org, each in its own namespaces.
        f'  bash "$BOX_ROOT/{RESTAGE_PREREQS}" || {{ sleep 5; continue; }}',
        f"  {LOCALDEV_DAEMON} {START_COMMAND} &",
        "  child=$!",
        '  wait "$child" || true',
        "  sleep 5",
        "done",
    ]


def _log_shipping_lines(spec: BootstrapSpec) -> list[str]:
    """Where an EC2 node ships its supervisor's events on its instance role
    (``alkera_cli.supervisor.log_shipping``); nothing when no group is named."""
    if not (spec.log_group and spec.region):
        return []
    return [
        f"ALKERA_BOX_LOG_GROUP={_q(spec.log_group)}",
        f"ALKERA_BOX_LOG_REGION={_q(spec.region)}",
    ]


def render_bootstrap(
    spec: BootstrapSpec, plan: BootstrapPlan, *, source: NodeDaemonSource | None = None
) -> str:
    """The first-boot script for ``spec``'s machine, installing and starting
    what ``plan`` names. The daemon comes from ``source``, by default the one
    this process registered (:func:`daemon_source`)."""
    fetch = source or daemon_source()
    if not _VERSION_RE.match(plan.version):
        raise BootstrapError(f"refusing daemon version {plan.version!r}")
    if not isinstance(plan.start_mode, StartMode):
        raise BootstrapError(f"refusing start mode {plan.start_mode!r}")
    if spec.log_group and not _LOG_GROUP_RE.match(spec.log_group):
        raise BootstrapError(f"refusing log group {spec.log_group!r}")
    if spec.sandbox_mode not in ("gvisor", "none"):
        raise BootstrapError(f"refusing sandbox mode {spec.sandbox_mode!r}")
    profile = boot_profile(spec.provider)
    if profile.daemon == "release" and fetch.needs_release_base and not spec.release_base_url:
        raise BootstrapError("no release base URL to install the daemon from")
    if profile.sandbox == "always" and spec.sandbox_mode != "gvisor":
        raise BootstrapError(f"{spec.provider} nodes run every chat under gVisor")
    if profile.secrets == "secrets_manager" and not (spec.credential_secret and spec.region):
        raise BootstrapError(f"{spec.provider} nodes read their credential from a named secret")
    for field_name, value in (
        ("machine_name", spec.machine_name),
        ("allocation_id", spec.allocation_id),
        ("type_code", spec.type_code),
        ("tenancy", spec.tenancy),
        ("api_url", spec.api_url),
        ("release_base_url", spec.release_base_url),
        ("credential_secret", spec.credential_secret),
        ("region", spec.region),
    ):
        if _CONTROL.search(value):
            raise BootstrapError(f"refusing a {field_name} with a control character")
    if isinstance(spec.drain_ceiling_seconds, bool) or not (
        isinstance(spec.drain_ceiling_seconds, int) and spec.drain_ceiling_seconds >= 0
    ):
        raise BootstrapError(f"refusing drain ceiling {spec.drain_ceiling_seconds!r}")
    if isinstance(spec.chat_idle_minutes, bool) or not (
        isinstance(spec.chat_idle_minutes, int) and spec.chat_idle_minutes >= 1
    ):
        raise BootstrapError(f"refusing chat idle window {spec.chat_idle_minutes!r}")
    if isinstance(spec.chat_memory_pressure_percent, bool) or not (
        isinstance(spec.chat_memory_pressure_percent, int)
        and 1 <= spec.chat_memory_pressure_percent <= 100
    ):
        raise BootstrapError(
            f"refusing chat memory pressure line {spec.chat_memory_pressure_percent!r}"
        )
    prereqs = sandbox_prereqs_script()
    if SANDBOX_PREREQS_DELIMITER in prereqs.splitlines():
        raise BootstrapError("the sandbox prerequisites script cannot be embedded")
    if RESTAGE_DELIMITER in restage_prereqs_lines(spec.sandbox_mode, spec.provider):
        raise BootstrapError("the restage script cannot be embedded")
    base = spec.release_base_url.rstrip("/")
    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# First boot of an Alkera node; rendered by the backend, runs once.",
        "set -euo pipefail",
        "exec >>/var/log/alkera-node-bootstrap.log 2>&1",
        "BOX_ROOT=/opt/alkera",
        f"BOX_HOME={BOX_HOME}",
        f"DATA_MOUNT={DATA_MOUNT}",
        f"RELEASE_BASE={_q(base)}",
        f"DAEMON_VERSION={_q(plan.version)}",
        f"AWS_REGION={_q(spec.region)}",
        f"CREDENTIAL_SECRET={_q(spec.credential_secret)}",
        'mkdir -p "$BOX_ROOT" "$BOX_HOME/logs" "$DATA_MOUNT"',
        'chmod 700 "$BOX_HOME"',
    ]
    if profile.installs_base_packages:
        lines += [
            "if command -v apt-get >/dev/null 2>&1; then",
            "  export DEBIAN_FRONTEND=noninteractive",
            "  apt-get update",
            "  apt-get install -y --no-install-recommends ca-certificates curl jq unzip",
            "fi",
        ]
    if profile.formats_data_volume:
        lines += [
            "# The data volume: the one whole disk with no filesystem and no mount.",
            'if ! mountpoint -q "$DATA_MOUNT"; then',
            '  DATA_DEV="$(lsblk -dpno NAME,TYPE | awk \'$2=="disk"{print $1}\''
            " | while read -r d; do"
            ' [ -z "$(lsblk -no FSTYPE,MOUNTPOINT "$d" | tr -d "[:space:]")" ]'
            ' && echo "$d" && break;'
            ' done || true)"',
            '  if [ -n "$DATA_DEV" ]; then',
            "    # Project quotas on: each org's root is a project with its own cap.",
            '    mkfs.ext4 -q -O quota,project "$DATA_DEV"',
            "    # Mounted by UUID, never by device name: NVMe names follow probe order",
            "    # at each boot, so after a stop and start the data volume and the root",
            "    # disk can trade names, the mount fails, and the daemon, which works on",
            "    # this volume, never starts.",
            '    DATA_UUID="$(blkid -p -s UUID -o value "$DATA_DEV")"',
            '    [ -n "$DATA_UUID" ]',
            '    echo "UUID=$DATA_UUID $DATA_MOUNT ext4 defaults,nofail,prjquota 0 2" >>/etc/fstab',
            '    mount "$DATA_MOUNT"',
            "  fi",
            "fi",
        ]
    if profile.secrets == "secrets_manager":
        lines += [
            "if ! command -v aws >/dev/null 2>&1; then",
            '  curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip"'
            " -o /tmp/awscli.zip",
            "  (cd /tmp && unzip -q awscli.zip && ./aws/install)",
            "  rm -rf /tmp/aws /tmp/awscli.zip",
            "fi",
            'TOKEN="$(curl -sS -X PUT http://169.254.169.254/latest/api/token'
            " -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')\"",
            'POD_ID="$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN"'
            ' http://169.254.169.254/latest/meta-data/instance-id)"',
            "umask 077",
            "# The node may read only the secret tagged with its own instance ARN, and",
            "# the tag lands just after RunInstances returns: the first reads can be",
            "# refused. Retried with backoff for about five minutes, then the boot",
            "# fails closed and the reconcile ends the node.",
            'NODE_SECRET=""',
            'secret_err="$(mktemp)"',
            "delay=2",
            f"for attempt in $(seq 1 {SECRET_READ_ATTEMPTS}); do",
            '  if NODE_SECRET="$(aws secretsmanager get-secret-value'
            ' --secret-id "$CREDENTIAL_SECRET" --region "$AWS_REGION"'
            ' --query SecretString --output text 2>"$secret_err")"; then',
            "    break",
            "  fi",
            '  NODE_SECRET=""',
            '  echo "reading the node secret was refused (attempt $attempt):'
            ' $(head -c 400 "$secret_err" | tr -d "\\n")"',
            f'  [ "$attempt" -lt {SECRET_READ_ATTEMPTS} ] && sleep "$delay"',
            f"  delay=$(( delay * 2 > {SECRET_READ_MAX_DELAY} ? {SECRET_READ_MAX_DELAY}"
            " : delay * 2 ))",
            "done",
            'rm -f "$secret_err"',
            'if [ -z "$NODE_SECRET" ]; then',
            '  echo "the node secret could not be read; refusing to boot"',
            "  exit 1",
            "fi",
            *(
                f"{var}=\"$(printf '%s' \"$NODE_SECRET\" | jq -r '.{key}')\""
                for var, key in (
                    ("MACHINE_CREDENTIAL", "ALKERA_MACHINE_CREDENTIAL"),
                    ("BOX_TOKEN", "ALKERA_BOX_TOKEN"),
                    ("BOX_TOKEN_EXPIRES", "ALKERA_BOX_TOKEN_EXPIRES"),
                )
            ),
            "unset NODE_SECRET",
        ]
    else:
        lines += [
            f'POD_ID="${{{profile.machine_id_env}:-}}"',
            "umask 077",
            'MACHINE_CREDENTIAL="${ALKERA_MACHINE_CREDENTIAL:-}"',
            'BOX_TOKEN="${ALKERA_BOX_TOKEN:-}"',
            'BOX_TOKEN_EXPIRES="${ALKERA_BOX_TOKEN_EXPIRES:-}"',
            "unset ALKERA_MACHINE_CREDENTIAL ALKERA_BOX_TOKEN ALKERA_BOX_TOKEN_EXPIRES",
        ]
    lines += [
        "# The box login the daemon runs under.",
        'cat >"$BOX_HOME/auth.yml" <<AUTH',
        f"api_url: {spec.api_url}",
        "token: $BOX_TOKEN",
        "expires_at: '$BOX_TOKEN_EXPIRES'",
        "AUTH",
        'chmod 600 "$BOX_HOME/auth.yml"',
        "unset BOX_TOKEN BOX_TOKEN_EXPIRES",
    ]
    # After any mount: a fresh filesystem's root is not the directory made above.
    lines += work_root_lines()
    lines += (
        _source_install_lines()
        if profile.daemon == "source"
        else daemon_install_lines(fetch, api_url=spec.api_url)
    )
    lines += [
        "# The chat sandbox's prerequisites, run before the daemon starts; the gate",
        "# below starts the daemon only once they applied and the sandbox works.",
        f"cat >\"$BOX_ROOT/sandbox-prereqs.sh\" <<'{SANDBOX_PREREQS_DELIMITER}'",
        prereqs.rstrip("\n"),
        SANDBOX_PREREQS_DELIMITER,
        'chmod 700 "$BOX_ROOT/sandbox-prereqs.sh"',
        *(_nested_container_lines() if profile.nested_container else []),
        *sandbox_gate_lines(spec.sandbox_mode, spec.provider),
        "# Before each start: the build's own prerequisites, when they changed.",
        f"cat >\"$BOX_ROOT/{RESTAGE_PREREQS}\" <<'{RESTAGE_DELIMITER}'",
        *restage_prereqs_lines(spec.sandbox_mode, spec.provider),
        RESTAGE_DELIMITER,
        f'chmod 700 "$BOX_ROOT/{RESTAGE_PREREQS}"',
        "# The node's environment: 0600, the credential never on argv or in a log.",
        "# Read by systemd (EnvironmentFile) and sourced by bash on RunPod, so",
        "# every value is shell-quoted and the heredoc expands nothing.",
        "cat >\"$BOX_ROOT/node.env\" <<'ENV'",
        f"ALKERA_API_URL={_q(spec.api_url)}",
        *([f"ALKERA_GATEWAY_URL={_q(spec.gateway_url)}"] if spec.gateway_url else []),
        f"ALKERA_MACHINE_NAME={_q(spec.machine_name)}",
        f"ALKERA_MACHINE_PROVIDER={_q(spec.provider)}",
        f"ALKERA_MACHINE_TYPE_CODE={_q(spec.type_code)}",
        f"{ENV_MACHINE_TENANCY}={_q(spec.tenancy)}",
        f"ALKERA_ALLOCATION_ID={_q(spec.allocation_id)}",
        f"ALKERA_HOME={BOX_HOME}",
        f"ALKERA_SANDBOX_MODE={_q(spec.sandbox_mode)}",
        f"{START_MODE_ENV}={plan.start_mode.value}",
        *(_installed_harness_lines() if profile.daemon == "release" else []),
        f"{ENV_DRAIN_CEILING_SECONDS}={spec.drain_ceiling_seconds}",
        f"{ENV_CHAT_IDLE_MINUTES}={spec.chat_idle_minutes}",
        f"{ENV_CHAT_MEMORY_PRESSURE_PERCENT}={spec.chat_memory_pressure_percent}",
        *(["ALKERA_SANDBOX_DEDICATED=1"] if spec.tenancy == "dedicated" else []),
        *(_log_shipping_lines(spec) if profile.secrets == "secrets_manager" else []),
        # A box that serves the web portal's chats offers no subagents: the
        # spawning tools are withheld from every harness backend, the
        # delegation guidance leaves the system prompt, and a call that arrives
        # anyway is refused. Delegation on a shared box runs a second agent on
        # the same tenant's credential and budget with no card of its own; it
        # returns when that has a policy, not by default.
        "ALKERA_SUBAGENTS_ENABLED=false",
        "ENV",
        _RUNTIME_VALUE_CHECK,
        # The release the node installed rides its heartbeat as part of the
        # daemon version, so the console shows which build a node runs — a
        # node once ran a build three labels old while every proof was read
        # against the label the rig had staged. DAEMON_VERSION was validated
        # above to the same characters the runtime check admits.
        "printf 'ALKERA_MACHINE_PROVIDER_POD_ID=%s\\nALKERA_MACHINE_CREDENTIAL=%s\\n"
        "ALKERA_RELEASE_VERSION=%s\\n'"
        ' "$POD_ID" "$MACHINE_CREDENTIAL" "$DAEMON_VERSION" >>"$BOX_ROOT/node.env"',
        "unset MACHINE_CREDENTIAL",
        'chmod 600 "$BOX_ROOT/node.env"',
    ]
    if profile.supervisor == "container":
        lines += _container_supervisor_lines()
        return "\n".join(lines) + "\n"
    lines += [
        "cat >/etc/systemd/system/alkera-node.service <<UNIT",
        "[Unit]",
        "Description=Alkera node (cloud mirror)",
        "After=network-online.target",
        # On a host that also runs Docker, Docker's forward chain exists by
        # the time the node lets its links through it; ordering only, so a
        # host without Docker is unaffected.
        "After=docker.service",
        "Wants=network-online.target",
        f"ConditionPathExists={SANDBOX_READY}",
        "",
        "[Service]",
        "Type=simple",
        "EnvironmentFile=-/etc/alkera/sandbox.env",
        "EnvironmentFile=$BOX_ROOT/node.env",
        "WorkingDirectory=$DATA_MOUNT",
        f"UMask={DAEMON_UMASK}",
        "Environment=ALKERA_DAEMON_SUPERVISED=1",
        f"Environment=ALKERA_DAEMON_FINAL_STOP_FILE={FINAL_STOP_FILE}",
        f"ExecStartPre=/bin/rm -f {FINAL_STOP_FILE}",
        f"ExecStartPre=/bin/bash $BOX_ROOT/{RESTAGE_PREREQS}",
        # Applying changed prerequisites can download; the default 90 s would
        # kill a start that is only slow.
        "TimeoutStartSec=900",
        f"ExecStart=$BOX_ROOT/alkera.dist/alkera {START_COMMAND}",
        f"ExecStop={NODE_STOP_COMMAND}",
        "Restart=always",
        "RestartSec=5",
        # A stop is bounded: the daemon drains for its ceiling, puts what it
        # still holds to sleep, and past a margin ends itself. The timeout is
        # past all of that, so the kill below is only ever the backstop; an
        # unbounded one let a stop that never ended hold every chat on the
        # node, served by nobody and handed to no other box.
        "KillMode=mixed",
        f"TimeoutStopSec={unit_stop_timeout_seconds(spec.drain_ceiling_seconds)}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "UNIT",
        "if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then",
        "  systemctl daemon-reload",
        "  systemctl enable --now alkera-node.service",
        "else",
        "  # A container with no init (RunPod): keep the daemon up in the foreground,",
        "  # behind the same gate the unit has.",
        '  [ -f "$SANDBOX_READY" ] || { echo "the sandbox is not ready; not starting"; exit 1; }',
        "  set -a; [ -f /etc/alkera/sandbox.env ] && . /etc/alkera/sandbox.env;"
        ' . "$BOX_ROOT/node.env"; set +a',
        '  cd "$DATA_MOUNT"',
        f"  umask {DAEMON_UMASK}",
        f'  while true; do bash "$BOX_ROOT/{RESTAGE_PREREQS}"'
        f' && "$BOX_ROOT/alkera.dist/alkera" {START_COMMAND} || true; sleep 5; done',
        "fi",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "BOOTSTRAP_PROVIDERS",
    "BOOT_PROFILES",
    "DAEMON_UMASK",
    "DATA_MOUNT",
    "FINAL_STOP_FILE",
    "INSTALLED_HARNESS",
    "INSTALLED_RIPGREP",
    "LOCALDEV_AGENT_DIR",
    "LOCALDEV_AGENT_SOURCE",
    "LOCALDEV_CONTAINER_ENV",
    "LOCALDEV_DAEMON",
    "LOCALDEV_FORWARD_ENV",
    "LOCALDEV_HOST",
    "LOCALDEV_SOURCE",
    "LOCALDEV_VENV",
    "NODE_STOP_COMMAND",
    "SANDBOX_GATE_BEGIN",
    "SANDBOX_GATE_END",
    "SANDBOX_MODE",
    "SANDBOX_PREREQS_DELIMITER",
    "SANDBOX_PREREQS_PATH",
    "SANDBOX_READY",
    "SANDBOX_ROOTFS",
    "SANDBOX_ROOTFS_STAMP",
    "SECRET_READ_ATTEMPTS",
    "SECRET_READ_MAX_DELAY",
    "SSH_MACHINE_ID_ENV",
    "BootstrapError",
    "BootstrapSpec",
    "NodeBootProfile",
    "boot_profile",
    "daemon_install_lines",
    "host_firewall_policy",
    "render_bootstrap",
    "sandbox_gate_lines",
    "sandbox_prereqs_script",
]
