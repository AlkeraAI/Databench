"""What an org worker sets up inside its own namespaces before it serves.

The supervisor starts the worker as uid 0 of a user namespace that owns its
mount, network and cgroup namespaces (``alkera_cli/supervisor/launch.py``).
Here the worker, with no authority outside them, makes them usable:

* a private ``/run`` (gVisor's state, the chats' network namespaces);
* a fresh read-only ``sysfs`` and a ``cgroup2`` view rooted at the org's
  delegated cgroup, the worker itself moved into a leaf so the cpu, memory and
  pids controllers can be handed down to ``alkera.slice``, where every chat's
  cgroup is made;
* its loopback, its link to the host (addressed from its slot's /30), a route
  out, forwarding for its chats, and the same chat firewall a box keeps on the
  host, kept here in the org's own namespace: a chat's namespace reaches this
  worker's tool server on the granted ports and nothing else of it, never the
  metadata service, never another chat, and goes out through NAT;
* the org root's subdirectories.

Composed as argv and file contents, run through one injectable runner, so the
tests pin what runs without a namespace.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alkera_core.compute import box_egress
from alkera_core.process import SpawnSpec, run
from alkera_core.process import spawn as spawn_child

from alkera_cli.harness.sandbox import (
    DEFAULT_CHAT_NET,
    METADATA_IPV4,
    METADATA_IPV6,
    chat_network_rules,
)
from alkera_cli.harness.sandbox_probe import host_resolvers

logger = logging.getLogger(__name__)

#: The controllers handed down to the chats.
CHAT_CONTROLLERS: Final = "+cpu +cpuset +io +memory +pids"
#: The slice every chat cgroup is made under (``harness/sandbox_layout.py``).
CHAT_SLICE: Final = "alkera.slice"
#: How long the worker waits for the supervisor to hand it its link.
LINK_WAIT_SECONDS: Final = 30.0
#: Where systemd-resolved keeps the resolver files the host's /etc/resolv.conf
#: points at, beneath the /run the worker replaces with its own.
RESOLVED_DIR: Final = Path("/run/systemd/resolve")
RESOLVED_FILES: Final = ("resolv.conf", "stub-resolv.conf")

Runner = Callable[[Sequence[str]], int]


class OrgNamespaceError(RuntimeError):
    """A step the worker cannot serve without failed; the worker exits and the
    supervisor's backoff owns the retry."""


def _run(argv: Sequence[str]) -> int:
    try:
        done = run(
            SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe"), timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("org namespace step could not run: %s: %s", " ".join(argv), exc)
        return 127
    if done.returncode != 0:
        logger.warning(
            "org namespace step failed (%d): %s: %s",
            done.returncode,
            " ".join(argv),
            done.stderr.decode("utf-8", "replace").strip()[-500:],
        )
    return done.returncode


@dataclass(frozen=True, slots=True)
class OrgNetwork:
    """The worker's end of its link, as the supervisor assigned it."""

    worker_ip: str
    host_ip: str
    chat_net: str = DEFAULT_CHAT_NET


def chat_ruleset(chat_net: str = DEFAULT_CHAT_NET) -> str:
    """The nftables ruleset the worker loads in its own network namespace: the
    box ruleset's chat rules (:func:`chat_network_rules`, the same text the
    host keeps), with an input chain that admits only loopback, replies and
    the granted tool-server ports."""
    rules = chat_network_rules(chat_net)
    granted, dropped, *forward, masquerade = rules
    forward_lines = "\n".join(f"    {line}" for line in forward)
    # Every set the chat rules name is declared here, as the box ruleset
    # declares it: a rule naming a set this table lacks fails the whole load.
    set_lines = "\n".join(f"  {line}" for line in box_egress.deny_set_lines())
    return f"""table inet alkera_sandbox
flush table inet alkera_sandbox
table inet alkera_sandbox {{
  set chat_ports {{
    type ifname . inet_service
  }}
{set_lines}
  chain input {{
    type filter hook input priority filter; policy drop;
    iif "lo" accept
    ct state established,related accept
    ct state invalid drop
    {granted}
    {dropped}
    ip protocol icmp accept
    counter drop
  }}
  chain forward {{
    type filter hook forward priority filter; policy drop;
    ct state established,related accept
    ct state invalid drop
{forward_lines}
  }}
  chain output {{
    type filter hook output priority filter; policy accept;
    ip daddr {METADATA_IPV4} counter drop
    ip6 daddr {METADATA_IPV6} counter drop
  }}
  chain postrouting {{
    type nat hook postrouting priority srcnat; policy accept;
    {masquerade}
  }}
}}
"""


def mount_steps() -> tuple[tuple[str, ...], ...]:
    """The worker's own ``/run``, ``sysfs`` (the one of its own network
    namespace) and ``cgroup2`` view rooted at the org's cgroup."""
    return (
        ("mount", "-t", "tmpfs", "-o", "nosuid,nodev,mode=0755", "tmpfs", "/run"),
        ("mkdir", "-p", "/run/netns"),
        ("mount", "-t", "sysfs", "-o", "nosuid,nodev,noexec", "sysfs", "/sys"),
        ("mount", "-t", "cgroup2", "-o", "nosuid,nodev,noexec", "cgroup2", "/sys/fs/cgroup"),
    )


def cgroup_steps(
    pid: int, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> tuple[tuple[str, ...], ...]:
    """The worker into a leaf, then the controllers down to the chats' slice:
    a cgroup that holds a process cannot hand controllers to children."""
    leaf = cgroup_root / "worker"
    chats = cgroup_root / CHAT_SLICE
    return (
        ("mkdir", "-p", str(leaf), str(chats)),
        ("/bin/sh", "-c", 'echo "$1" > "$2"', "join", str(pid), str(leaf / "cgroup.procs")),
        (
            "/bin/sh",
            "-c",
            'echo "$1" > "$2"',
            "enable",
            CHAT_CONTROLLERS,
            str(cgroup_root / "cgroup.subtree_control"),
        ),
        (
            "/bin/sh",
            "-c",
            'echo "$1" > "$2"',
            "enable",
            CHAT_CONTROLLERS,
            str(chats / "cgroup.subtree_control"),
        ),
    )


def network_steps(net: OrgNetwork) -> tuple[tuple[str, ...], ...]:
    """Loopback up, the link addressed and up, the route out, forwarding on."""
    return (
        ("ip", "link", "set", "lo", "up"),
        ("ip", "addr", "add", f"{net.worker_ip}/30", "dev", "eth0"),
        ("ip", "link", "set", "eth0", "up"),
        ("ip", "route", "add", "default", "via", net.host_ip),
        ("/bin/sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward"),
    )


def _link_present(run: Runner) -> bool:
    return run(("ip", "link", "show", "eth0")) == 0


def prepare(
    net: OrgNetwork,
    *,
    pid: int,
    run: Runner = _run,
    write: Callable[[Path, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    ruleset_path: Path = Path("/run/alkera-chat.nft"),
    resolvers: Callable[[], tuple[str, ...]] = host_resolvers,
    env: Mapping[str, str] | None = None,
    resolve: box_egress.Resolve = box_egress.system_resolve,
) -> None:
    """Every step, in order; the first that fails raises.

    The host's resolvers are read before the worker's own ``/run`` hides
    where systemd-resolved keeps them (the host's ``/etc/resolv.conf`` points
    there), and written back into it: the worker resolves through the
    upstream servers, which its link can reach, never through the host's
    loopback stub, which it cannot."""
    upstream = resolvers()
    for argv in (*mount_steps(), *cgroup_steps(pid)):
        if run(argv) != 0:
            raise OrgNamespaceError(f"could not prepare the org namespace: {' '.join(argv)}")
    if upstream:
        text = "".join(f"nameserver {server}\n" for server in upstream)
        if run(("mkdir", "-p", str(RESOLVED_DIR))) != 0:
            raise OrgNamespaceError("could not make the worker's resolver directory")
        for name in RESOLVED_FILES:
            (write or _write)(RESOLVED_DIR / name, text)
    deadline = clock() + LINK_WAIT_SECONDS
    while not _link_present(run):
        if clock() >= deadline:
            raise OrgNamespaceError("the supervisor never handed this worker its link")
        sleep(0.2)
    for argv in network_steps(net):
        if run(argv) != 0:
            raise OrgNamespaceError(f"could not bring up the org network: {' '.join(argv)}")
    (write or _write)(ruleset_path, chat_ruleset(net.chat_net))
    if run(("nft", "-f", str(ruleset_path))) != 0:
        raise OrgNamespaceError("could not load the org's chat firewall")
    # The chats reach the private addresses the box itself needs (its backend,
    # gateway and resolvers) through the allow sets, filled here as the box
    # fills its own; left empty, every chat would lose its backend.
    entries = box_egress.allow_entries(
        box_egress.box_endpoints(os.environ if env is None else env), upstream, resolve=resolve
    )
    allow_path = ruleset_path.with_name(ruleset_path.stem + "-allow.nft")
    (write or _write)(allow_path, box_egress.allowlist_script(entries))
    if run(("nft", "-f", str(allow_path))) != 0:
        raise OrgNamespaceError("could not fill the org's egress allowlist")


#: The developer machine a ``localdev`` box reaches its stack on.
LOCALDEV_HOST: Final = "host.docker.internal"
ENV_LOCALDEV_FORWARD_PORTS: Final = "ALKERA_LOCALDEV_FORWARD_PORTS"


def localdev_forward_argv(port: int, host: str = LOCALDEV_HOST) -> tuple[str, ...]:
    """A developer box only: the stack hands out ``localhost`` URLs (a
    presigned Files URL names ``localhost:<port>``), so the worker forwards
    each named port on its own loopback to the developer's machine, as the
    box does on its own."""
    return (
        "socat",
        f"TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr",
        f"TCP:{host}:{port}",
    )


def localdev_ports(raw: str) -> tuple[int, ...]:
    """The ports to forward; anything that is not a port is refused whole."""
    ports: list[int] = []
    for word in raw.split():
        if not word.isdigit() or not 0 < int(word) < 65536:
            raise OrgNamespaceError(f"refusing forward port {word!r}")
        ports.append(int(word))
    return tuple(ports)


def start_localdev_forwards(
    raw: str, *, spawn: Callable[[Sequence[str]], object] | None = None
) -> tuple[int, ...]:
    ports = localdev_ports(raw)
    for port in ports:
        argv = localdev_forward_argv(port)
        if spawn is not None:
            spawn(argv)
        else:
            # Lives as long as the worker, and no longer.
            spawn_child(
                SpawnSpec(argv=list(argv), env=os.environ, stdout="devnull", stderr="devnull")
            )
    return ports


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


__all__ = [
    "CHAT_CONTROLLERS",
    "CHAT_SLICE",
    "ENV_LOCALDEV_FORWARD_PORTS",
    "LINK_WAIT_SECONDS",
    "LOCALDEV_HOST",
    "OrgNamespaceError",
    "OrgNetwork",
    "cgroup_steps",
    "chat_ruleset",
    "localdev_forward_argv",
    "localdev_ports",
    "mount_steps",
    "network_steps",
    "prepare",
    "start_localdev_forwards",
]
