"""The box ruleset, loaded into a real kernel: a sandbox's link cannot reach any
private range, the metadata service or the box's VPC, and reaches a public
address and an allowlisted private one.

Linux as root with ``nft`` and ``ip`` only (a box, or a privileged container);
skipped elsewhere. The sandbox is a network namespace behind a ``vc*`` link,
as a chat's is; the "network" is a second namespace holding one address in
every range, each with a listener.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_core.compute import box_egress as egress
from alkera_core.compute import host_forward as hf
from alkera_core.compute.box_egress import AllowEntry

SCRIPT = Path(egress.__file__).with_name("sandbox_prereqs.sh")
PORT = 8080

needs_a_linux_kernel_as_root = pytest.mark.skipif(
    sys.platform != "linux"
    or os.geteuid() != 0
    or shutil.which("nft") is None
    or shutil.which("ip") is None,
    reason="loads an nftables ruleset into a real kernel: Linux, root, nft and ip",
)
# One network namespace is built for the module and shared by its tests.
pytestmark = [needs_a_linux_kernel_as_root, pytest.mark.xdist_group("box_egress_live")]

#: One address in each range a sandbox must never reach, by what it stands for.
DENIED_TARGETS = {
    "vpc": "10.0.1.5",
    "rfc1918-172": "172.16.4.4",
    "rfc1918-192": "192.168.7.7",
    "cgnat": "100.64.3.3",
    "metadata": "169.254.169.254",
    "link-local": "169.254.5.5",
    "reserved": "198.18.0.9",
}
PUBLIC_TARGET = "8.8.4.4"
ALLOWED_TARGET = "10.0.9.9"


def _sh(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def _scope_lines(script: str, scope: str) -> dict[str, str]:
    """What the script sets ``host_inbound`` / ``host_forward`` to for ``scope``."""
    arm = script[script.index(f"  {scope})\n    host_inbound=") :]
    arm = arm[: arm.index(";;")]
    return dict(re.findall(r'^    (host_inbound|host_forward)="([^"]*)"$', arm, flags=re.MULTILINE))


def _ruleset(scope: str = "owned") -> str:
    """The table ``sandbox_prereqs.sh`` loads on a host of ``scope``, its shell
    variables filled in."""
    script = SCRIPT.read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_0-9]+)=([0-9a-z.:/]+)$", script, flags=re.MULTILINE))
    values.update(
        re.findall(r'^([A-Z_0-9]+)="\$\{[A-Z_0-9]+:-([^}"]+)\}"$', script, flags=re.MULTILINE)
    )
    values["ports"] = "22"
    values.update(_scope_lines(script, scope))
    body = script[script.index("table inet alkera_sandbox\n") : script.index("\nEOF\n")]
    for name, value in values.items():
        body = body.replace("${" + name + "}", value)
    return body + "\n"


@pytest.fixture(scope="module")
def network() -> Iterator[None]:
    """A sandbox namespace ``alk-sb`` behind ``vc-eg0``, and a network
    namespace ``alk-net`` routed from the host holding every target."""
    _sh("ip", "netns", "add", "alk-sb")
    _sh("ip", "netns", "add", "alk-net")
    try:
        _sh("ip", "link", "add", "vc-eg0", "type", "veth", "peer", "name", "sb0", "netns", "alk-sb")
        _sh("ip", "addr", "add", "10.200.0.1/30", "dev", "vc-eg0")
        _sh("ip", "link", "set", "vc-eg0", "up")
        sb = ("ip", "netns", "exec", "alk-sb")
        _sh(*sb, "ip", "addr", "add", "10.200.0.2/30", "dev", "sb0")
        _sh(*sb, "ip", "link", "set", "sb0", "up")
        _sh(*sb, "ip", "link", "set", "lo", "up")
        _sh(*sb, "ip", "route", "add", "default", "via", "10.200.0.1")
        _sh(
            "ip",
            "link",
            "add",
            "eg-up0",
            "type",
            "veth",
            "peer",
            "name",
            "net0",
            "netns",
            "alk-net",
        )
        _sh("ip", "addr", "add", "192.0.2.1/30", "dev", "eg-up0")
        _sh("ip", "link", "set", "eg-up0", "up")
        net = ("ip", "netns", "exec", "alk-net")
        _sh(*net, "ip", "addr", "add", "192.0.2.2/30", "dev", "net0")
        _sh(*net, "ip", "link", "set", "net0", "up")
        _sh(*net, "ip", "link", "set", "lo", "up")
        _sh(*net, "ip", "route", "add", "default", "via", "192.0.2.1")
        for target in (*DENIED_TARGETS.values(), PUBLIC_TARGET, ALLOWED_TARGET):
            _sh(*net, "ip", "addr", "add", f"{target}/32", "dev", "lo")
            _sh("ip", "route", "add", f"{target}/32", "via", "192.0.2.2")
        listener = subprocess.Popen(
            [
                *net,
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import socket
                    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", {PORT})); s.listen(64)
                    while True:
                        c, _ = s.accept(); c.close()
                    """
                ),
            ]
        )
        _sh("sysctl", "-qw", "net.ipv4.ip_forward=1")
        done = subprocess.run(["nft", "-f", "-"], input=_ruleset(), text=True, capture_output=True)
        assert done.returncode == 0, done.stderr
        allow = egress.allowlist_script([AllowEntry(ALLOWED_TARGET, "tcp", PORT)])
        done = subprocess.run(["nft", "-f", "-"], input=allow, text=True, capture_output=True)
        assert done.returncode == 0, done.stderr
        yield
    finally:
        _sh("nft", "delete", "table", "inet", "alkera_sandbox", check=False)
        if "listener" in locals():
            listener.kill()
        _sh("ip", "link", "delete", "vc-eg0", check=False)
        _sh("ip", "link", "delete", "eg-up0", check=False)
        _sh("ip", "netns", "delete", "alk-sb", check=False)
        _sh("ip", "netns", "delete", "alk-net", check=False)


def _reaches(namespace: str, address: str, port: int = PORT) -> bool:
    probe = (
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(1.5)\n"
        f"sys.exit(0 if s.connect_ex(({address!r}, {port})) == 0 else 1)\n"
    )
    done = subprocess.run(
        ["ip", "netns", "exec", namespace, sys.executable, "-c", probe], capture_output=True
    )
    return done.returncode == 0


def _sandbox_reaches(address: str) -> bool:
    return _reaches("alk-sb", address)


@pytest.mark.parametrize("address", list(DENIED_TARGETS.values()), ids=list(DENIED_TARGETS))
def test_a_sandbox_cannot_reach_a_private_range(network: None, address: str) -> None:
    assert not _sandbox_reaches(address)


def test_a_sandbox_reaches_a_public_address(network: None) -> None:
    """The drops above are the policy, not a dead link."""
    assert _sandbox_reaches(PUBLIC_TARGET)


def test_a_sandbox_reaches_a_private_address_the_box_allowlisted(network: None) -> None:
    assert _sandbox_reaches(ALLOWED_TARGET)


# -- a host the node shares with others, and Docker's forward drop -----------------

#: A container of the host's own (a Docker bridge's, say), behind a link that is
#: not the node's, and a service of the host's own on a port it never named.
FOREIGN_LINK = "dk-fx0"
FOREIGN_HOST_END = "10.88.0.1"
HOST_SERVICE_PORT = 9099
needs_iptables = pytest.mark.skipif(
    shutil.which("iptables") is None, reason="emulates Docker's forward chain with iptables"
)


def _load(scope: str) -> None:
    for text in (
        _ruleset(scope),
        egress.allowlist_script([AllowEntry(ALLOWED_TARGET, "tcp", PORT)]),
    ):
        done = subprocess.run(["nft", "-f", "-"], input=text, text=True, capture_output=True)
        assert done.returncode == 0, done.stderr


@pytest.fixture
def foreign(network: None) -> Iterator[None]:
    """A namespace ``alk-fx`` behind ``dk-fx0`` routed out through the host, and
    a listener of the host's own; the owned ruleset is put back afterwards."""
    _sh("ip", "netns", "add", "alk-fx")
    service = None
    try:
        _sh(
            "ip",
            "link",
            "add",
            FOREIGN_LINK,
            "type",
            "veth",
            "peer",
            "name",
            "fx0",
            "netns",
            "alk-fx",
        )
        _sh("ip", "addr", "add", f"{FOREIGN_HOST_END}/30", "dev", FOREIGN_LINK)
        _sh("ip", "link", "set", FOREIGN_LINK, "up")
        fx = ("ip", "netns", "exec", "alk-fx")
        _sh(*fx, "ip", "addr", "add", "10.88.0.2/30", "dev", "fx0")
        _sh(*fx, "ip", "link", "set", "fx0", "up")
        _sh(*fx, "ip", "route", "add", "default", "via", FOREIGN_HOST_END)
        service = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import socket
                    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", {HOST_SERVICE_PORT})); s.listen(64)
                    while True:
                        c, _ = s.accept(); c.close()
                    """
                ),
            ]
        )
        yield
    finally:
        if service is not None:
            service.kill()
        _sh("ip", "link", "delete", FOREIGN_LINK, check=False)
        _sh("ip", "netns", "delete", "alk-fx", check=False)
        _load("owned")


def test_a_shared_host_leaves_its_own_containers_and_services_alone(foreign: None) -> None:
    _load("shared")
    assert _reaches("alk-fx", PUBLIC_TARGET)
    assert _reaches("alk-fx", FOREIGN_HOST_END, HOST_SERVICE_PORT)


def test_a_shared_host_still_keeps_the_sandbox_to_its_policy(foreign: None) -> None:
    _load("shared")
    assert _reaches("alk-sb", PUBLIC_TARGET)
    assert not _reaches("alk-sb", DENIED_TARGETS["vpc"])
    assert not _reaches("alk-sb", DENIED_TARGETS["metadata"])
    assert not _reaches("alk-sb", "10.200.0.1", HOST_SERVICE_PORT)


def test_an_owned_host_drops_what_is_not_the_nodes(foreign: None) -> None:
    _load("owned")
    assert not _reaches("alk-fx", PUBLIC_TARGET)
    assert not _reaches("alk-fx", FOREIGN_HOST_END, HOST_SERVICE_PORT)


@pytest.fixture
def docker_forward(foreign: None) -> Iterator[None]:
    """Docker's forward chain as Docker writes it: DOCKER-USER first, its own
    bridge accepted, the policy DROP."""
    _sh("iptables", "-w", "-N", "DOCKER-USER")
    try:
        _sh("iptables", "-w", "-I", "FORWARD", "1", "-j", "DOCKER-USER")
        _sh("iptables", "-w", "-A", "FORWARD", "-i", FOREIGN_LINK, "-j", "ACCEPT")
        _sh(
            "iptables",
            "-w",
            "-A",
            "FORWARD",
            "-o",
            FOREIGN_LINK,
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-j",
            "ACCEPT",
        )
        _sh("iptables", "-w", "-P", "FORWARD", "DROP")
        yield
    finally:
        _sh("iptables", "-w", "-P", "FORWARD", "ACCEPT", check=False)
        _sh("iptables", "-w", "-F", "FORWARD", check=False)
        _sh("iptables", "-w", "-F", "DOCKER-USER", check=False)
        _sh("iptables", "-w", "-X", "DOCKER-USER", check=False)


@needs_iptables
def test_docker_cuts_the_sandbox_off_until_the_node_opens_its_links(docker_forward: None) -> None:
    """The box table alone cannot let a link through: Docker's drop wins. The
    box's DOCKER-USER accepts do, they still leave the box table in charge,
    the host's own container keeps working, and the uninstall takes them out."""
    _load("shared")
    assert not _reaches("alk-sb", PUBLIC_TARGET)

    applied = hf.open_links({egress.ENV_SANDBOX_MODE: "gvisor"}, ("vc",))

    assert "docker" in applied
    assert _reaches("alk-sb", PUBLIC_TARGET)
    assert not _reaches("alk-sb", DENIED_TARGETS["vpc"])
    assert _reaches("alk-fx", PUBLIC_TARGET)

    done = subprocess.run(
        ["bash", "-c", hf.DockerFilter().removal_script()], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert not _reaches("alk-sb", PUBLIC_TARGET)
    assert _reaches("alk-fx", PUBLIC_TARGET)
