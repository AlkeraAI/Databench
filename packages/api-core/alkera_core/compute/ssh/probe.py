"""Reading a host before it is added: its key, and whether it can run a node.

:func:`probe_host` reads the key the host presents without authenticating,
refuses when an expected fingerprint was given and the host presented another,
then signs in pinned to that key and reads the facts a node needs: Linux,
systemd, root (or ``sudo -n``), and the hardware. Over the same session it
asks the host to fetch this deployment's ``/health/live`` at every address a
node would call back on, so a host that could never register is refused before
anything is installed on it.
"""

from __future__ import annotations

import ipaddress
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from alkera_core.compute import host_forward
from alkera_core.compute.ssh.transport import (
    HostKey,
    HostKeyMismatchError,
    SshAuth,
    SshTarget,
    SshTransport,
)

#: Read as the user, not as root: whether root is available is one of the facts.
FACTS_SCRIPT = """\
uname -s 2>/dev/null || echo unknown
uname -m 2>/dev/null || echo unknown
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
then echo yes; else echo no; fi
if [ "$(id -u)" = 0 ] || sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi
nproc 2>/dev/null || echo 0
awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo 2>/dev/null || echo 0
df -BG --output=size / 2>/dev/null | tail -n 1 | tr -dc '0-9'; echo
nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true
if command -v systemd-run >/dev/null 2>&1 && [ -S /run/dbus/system_bus_socket ]
then echo yes; else echo no; fi
"""

#: How a line after the facts names a host firewall the node could not open.
_MISSING_PREFIX = "missing "


def facts_script() -> str:
    """:data:`FACTS_SCRIPT`, then each host firewall's check
    (:func:`~alkera_core.compute.host_forward.host_check_script`): a firewall
    that is active but that the node could not let its links through is
    named in :attr:`HostFacts.missing`."""
    return FACTS_SCRIPT + host_forward.host_check_script()


#: The block a node's chat sandboxes take their addresses from, unless the node
#: is told another (``ALKERA_SANDBOX_NET``; the node's sandbox prerequisites and
#: the harness hold the same default). A chat's egress never reaches into it, so
#: a server the host reaches through an address inside it is unreachable from
#: every chat on that host.
SANDBOX_NET = ipaddress.ip_network("10.200.0.0/14")

#: How long the host waits for this deployment to answer, in seconds.
CALLBACK_TIMEOUT_SECONDS = 10

#: Prints an ``addr <ip>`` line for each IPv4 address the host resolves ``$2``
#: to, then ``ok``, ``fail <why>``, or ``untested`` when the host has no tool to
#: fetch a URL with. ``$1`` is the URL.
_FETCH_SCRIPT = f"""\
u="$1"
getent ahostsv4 "$2" 2>/dev/null | awk '{{print "addr " $1}}' | sort -u
if command -v curl >/dev/null 2>&1; then
  if out=$(curl -fsS -m {CALLBACK_TIMEOUT_SECONDS} -o /dev/null "$u" 2>&1); then echo ok
  else echo "fail $out"; fi
elif command -v wget >/dev/null 2>&1; then
  if out=$(wget -q -T {CALLBACK_TIMEOUT_SECONDS} -O /dev/null "$u" 2>&1); then echo ok
  else echo "fail ${{out:-wget could not fetch it}}"; fi
elif command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout={CALLBACK_TIMEOUT_SECONDS}).read(1)
    print("ok")
except Exception as exc:
    print("fail", exc)' "$u"
else echo untested; fi
"""


def callback_script(url: str) -> str:
    """The script that asks the host to fetch ``url``'s liveness probe."""
    probe = url.rstrip("/") + "/health/live"
    host = urlsplit(url).hostname or ""
    return f"set -- {shlex.quote(probe)} {shlex.quote(host)}\n{_FETCH_SCRIPT}"


def _addresses(url: str, lines: list[str]) -> list[ipaddress.IPv4Address]:
    """The IPv4 addresses the host reaches ``url`` through: the literal one, or
    what the host resolved its name to."""
    host = urlsplit(url).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if isinstance(literal, ipaddress.IPv4Address):
        return [literal]
    found: list[ipaddress.IPv4Address] = []
    for line in lines:
        if not line.startswith("addr "):
            continue
        try:
            address = ipaddress.ip_address(line.removeprefix("addr ").strip())
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            found.append(address)
    return found


def callback_failure(setting: str, url: str, stdout: str) -> str | None:
    """Why the host could not reach ``url``, from what :func:`callback_script`
    printed, or ``None`` when it could (or had no tool to try with: the node's
    own claim is then the test)."""
    lines = [line.strip() for line in stdout.strip().splitlines()]
    inside = [a for a in _addresses(url, lines) if a in SANDBOX_NET]
    if inside:
        return (
            f"The host reaches this server at {url} through {inside[0]}, inside the block "
            f"chats on a machine use ({SANDBOX_NET}), so no chat there could reach it. "
            f"Set {setting} to an address outside {SANDBOX_NET}."
        )
    answer = lines[-1] if lines else ""
    if answer in ("ok", "untested"):
        return None
    why = answer.removeprefix("fail").strip() or "no answer"
    return (
        f"The host can't reach this server at {url} ({why[:200]}). "
        f"Set {setting} to an address the host can reach."
    )


@dataclass(frozen=True)
class HostFacts:
    os: str
    arch: str
    systemd: bool
    root: bool
    vcpu: int
    memory_gb: int
    disk_gb: int
    gpu_count: int
    #: ``systemd-run`` and a D-Bus system bus: each org's worker runs as a
    #: transient unit of its own.
    org_units: bool = False
    #: What an active host firewall needs before the node's links get through it.
    host_firewalls: tuple[str, ...] = ()

    @property
    def missing(self) -> list[str]:
        """What the host lacks to run a node, in words for the admin."""
        out: list[str] = []
        if self.os != "Linux":
            out.append("Linux")
        if not self.systemd:
            out.append("systemd")
        if not self.root:
            out.append("root or passwordless sudo")
        if self.systemd and not self.org_units:
            out.append("systemd-run with a running D-Bus system bus")
        out.extend(self.host_firewalls)
        return out


@dataclass(frozen=True)
class ProbeResult:
    host_key: HostKey
    facts: HostFacts
    #: Why the host could not reach this deployment back, or ``None``.
    callback_error: str | None = None


def _int(value: str) -> int:
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else 0


def parse_facts(stdout: str) -> HostFacts:
    lines = [*stdout.splitlines(), *([""] * 9)]
    return HostFacts(
        os=lines[0].strip()[:64],
        arch=lines[1].strip()[:32],
        systemd=lines[2].strip() == "yes",
        root=lines[3].strip() == "yes",
        vcpu=_int(lines[4]),
        memory_gb=_int(lines[5]),
        disk_gb=_int(lines[6]),
        gpu_count=_int(lines[7]),
        org_units=lines[8].strip() == "yes",
        host_firewalls=tuple(
            line.strip().removeprefix(_MISSING_PREFIX).strip()[:200]
            for line in lines[9:]
            if line.startswith(_MISSING_PREFIX)
        ),
    )


async def probe_host(
    transport: SshTransport,
    target: SshTarget,
    *,
    username: str,
    auth: SshAuth,
    expected_fingerprint: str | None = None,
    callbacks: Mapping[str, str] | None = None,
) -> ProbeResult:
    """Read the host. ``callbacks`` maps each setting to the address a node
    calls this deployment back on; the first the host cannot fetch is the
    result's ``callback_error``."""
    key = await transport.host_key(target)
    if expected_fingerprint is not None and key.fingerprint != expected_fingerprint:
        raise HostKeyMismatchError(key.fingerprint, key_type=key.key_type)
    callback_error: str | None = None
    async with transport.session(
        target, username=username, auth=auth, pinned_key=key.public_key
    ) as session:
        done = await session.run("sh -s", stdin=facts_script())
        for setting, url in (callbacks or {}).items():
            fetched = await session.run(
                "sh -s", stdin=callback_script(url), limit_seconds=CALLBACK_TIMEOUT_SECONDS + 15
            )
            callback_error = callback_failure(setting, url, fetched.stdout)
            if callback_error is not None:
                break
    return ProbeResult(host_key=key, facts=parse_facts(done.stdout), callback_error=callback_error)


__all__ = [
    "FACTS_SCRIPT",
    "HostFacts",
    "ProbeResult",
    "callback_failure",
    "callback_script",
    "facts_script",
    "parse_facts",
    "probe_host",
]
