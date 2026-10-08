"""The host firewalls a node's links have to get through, besides its own table.

A chat's link (``vc*``) and an org worker's (``vo*``) go out through the box
table (:mod:`alkera_core.compute.box_egress`), which decides what each may
reach. A host its operator shares can also run a firewall of its own that
filters forwarding (and input) whatever the box table accepts: a packet one
table drops is dropped whatever another accepts. Each such firewall is a
:class:`HostForwardFilter` in this module's registry, with a probe (is it
active on this host), an idempotent apply that lets the node's links through
it, a removal the uninstall runs, and a check Test connection runs to name a
firewall that is active but cannot be opened.

Every filter only lifts its own drop for the node's links: the box table still
decides what a link may reach, and the operator's policies and rules are
never changed. Every rule a filter adds is marked :data:`RULE_COMMENT`, which
is how it is found again, by the next apply and by the removal.

The filters:

- ``docker``: Docker's iptables firewall sets the ``FORWARD`` policy to DROP
  and accepts only its own bridges. The node accepts its links in
  ``DOCKER-USER``, the chain Docker evaluates first and leaves to the operator.
  Docker's nftables firewall keeps no such chain and drops only what heads for
  its own bridges, so it needs nothing; its operator is told once.
- ``ufw``: its ``DEFAULT_FORWARD_POLICY`` is DROP and its incoming policy is
  deny, so neither a link's traffic out nor a chat's call to the node's tool
  server on the host gets through. The node accepts its links at the top of
  ``ufw-before-input`` and ``ufw-before-forward`` (and their IPv6 twins), live
  with iptables, and writes the same lines into ``/etc/ufw/before.rules`` and
  ``before6.rules`` so they survive a ``ufw reload`` and a reboot. ufw's own
  rule commands (``ufw route allow in on ...``) are not used: they would show
  as the operator's rules in ``ufw status``, and ufw takes no interface
  wildcard there.
- ``firewalld``: an interface in no zone falls to the default zone, which
  rejects input to anything it does not name and, from firewalld 1.0, every
  forward to another zone. The node binds its links (``vc+``, ``vo+``) to a
  zone of its own whose target accepts (input), and adds a policy from that
  zone to ``ANY`` that accepts (forwarding). Both are permanent; the first
  apply reloads firewalld once, the only way a new zone goes live (it also
  drops any runtime-only change the operator had not made permanent, as a
  reboot would). Policies need firewalld 0.9 or later.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from alkera_core.compute.box_egress import (
    ENV_SANDBOX_MODE,
    NFT_FILE,
    NFT_TABLE,
    NFTABLES_CONF,
    SYSCTL_FILE,
)
from alkera_core.process import SpawnSpec, run

logger = logging.getLogger(__name__)

#: The comment every rule a filter adds carries, so it is found again.
RULE_COMMENT: Final = "alkera-sandbox"
_COMMENT: Final = ("-m", "comment", "--comment", RULE_COMMENT)

#: Runs one argv and returns its exit status.
Runner = Callable[[Sequence[str]], int]
#: Reads a host file, or ``None`` when there is none (or it cannot be read).
Reader = Callable[[str], "str | None"]
#: Replaces a host file's text, keeping its owner and mode. Raises ``OSError``.
Writer = Callable[[str, str], None]


def _quiet_run(argv: Sequence[str]) -> int:
    spec = SpawnSpec(argv=list(argv), env=os.environ, stdout="devnull", stderr="pipe")
    try:
        done = run(spec, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("could not run %s: %s", argv[0], exc)
        return 127
    return done.returncode


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _write(path: str, text: str) -> None:
    """Replace ``path`` whole (a reader sees the old file or the new one, never
    half of it), with the old file's owner and mode."""
    st = os.stat(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".alkera-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class Host:
    """What a filter may do to the host: run a command, read a file, replace
    one. A test substitutes all three."""

    run: Runner = field(default=_quiet_run)
    read: Reader = field(default=_read)
    write: Writer = field(default=_write)


class HostForwardFilter(Protocol):
    """One firewall a host may run that would drop the node's links."""

    @property
    def name(self) -> str:
        """The filter's name, in logs and in what :func:`open_links` returns."""
        ...

    def active(self, host: Host) -> bool:
        """Whether this firewall filters this host's traffic now."""
        ...

    def open(self, host: Host, link_prefixes: Sequence[str]) -> bool:
        """Let links named ``<prefix>*`` through, adding only what is missing.
        ``False`` when the firewall refused part of it."""
        ...

    def removal_script(self) -> str:
        """The shell that takes back everything :meth:`open` added, safe on a
        host without this firewall and on a second run."""
        ...

    def host_check(self) -> str:
        """Shell, run as the login user, that prints ``missing <what>`` for
        each thing the host lacks for :meth:`open` to work while this firewall
        is active; empty when it always works."""
        ...


_FILTERS: dict[str, HostForwardFilter] = {}


def register(entry: HostForwardFilter) -> HostForwardFilter:
    """Add a filter; a second one under the same name replaces the first."""
    _FILTERS[entry.name] = entry
    return entry


def registered() -> tuple[HostForwardFilter, ...]:
    return tuple(_FILTERS.values())


def open_links(
    env: Mapping[str, str], link_prefixes: Sequence[str], *, host: Host | None = None
) -> tuple[str, ...]:
    """Let the node's links through every host firewall that is active, and
    return the names of those that took it. Only on a box (its environment
    names a sandbox mode): a developer's machine is never touched. Idempotent,
    so it runs at every start of the node, of every worker, and at every
    heartbeat (a firewall that restarted rebuilds its chains). Never raises:
    a node starts whatever the host's firewall does."""
    if not env.get(ENV_SANDBOX_MODE, "").strip():
        return ()
    on = host or Host()
    done: list[str] = []
    for entry in registered():
        try:
            if not entry.active(on):
                continue
            if entry.open(on, link_prefixes):
                done.append(entry.name)
            else:
                logger.warning("%s refused part of the node's link rules", entry.name)
        except Exception:
            logger.exception("could not open the node's links in %s", entry.name)
    return tuple(done)


def removal_script() -> str:
    """The shell that takes back what every filter added."""
    return "".join(entry.removal_script() for entry in registered())


def host_check_script() -> str:
    """The shell Test connection runs to name an active firewall the node
    could not open."""
    return "".join(entry.host_check() for entry in registered())


# -- iptables chains ----------------------------------------------------------------


def link_accepts(link_prefixes: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """What a forward chain accepts for the node's links: whatever a link sends
    (the box table has already decided it may), and the replies coming back
    to it. Nothing new reaches a link through them."""
    rules: list[tuple[str, ...]] = []
    for prefix in link_prefixes:
        rules.append(("-i", f"{prefix}+", *_COMMENT, "-j", "ACCEPT"))
        rules.append(
            (
                "-o",
                f"{prefix}+",
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                *_COMMENT,
                "-j",
                "ACCEPT",
            )
        )
    return tuple(rules)


def link_input_accepts(link_prefixes: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """What an input chain accepts for the node's links: anything from one
    (the box table keeps a link to the ports it was granted)."""
    return tuple(("-i", f"{prefix}+", *_COMMENT, "-j", "ACCEPT") for prefix in link_prefixes)


def _has_chain(host: Host, binary: str, chain: str) -> bool:
    return host.run((binary, "-w", "-S", chain)) == 0


def _ensure_at_top(host: Host, binary: str, chain: str, rules: Sequence[Sequence[str]]) -> bool:
    """Insert each missing rule at the top of ``chain``, in ``rules``' order."""
    ok = True
    for rule in reversed(rules):
        if host.run((binary, "-w", "-C", chain, *rule)) == 0:
            continue
        if host.run((binary, "-w", "-I", chain, "1", *rule)) != 0:
            ok = False
    return ok


def _chain_removal(binaries: Sequence[str], chains: Sequence[str]) -> str:
    """Delete every rule marked :data:`RULE_COMMENT` from ``chains``."""
    return (
        f"for ipt in {' '.join(binaries)}; do\n"
        '  command -v "$ipt" >/dev/null 2>&1 || continue\n'
        f"  for chain in {' '.join(chains)}; do\n"
        '    { "$ipt" -w -S "$chain" 2>/dev/null || true; }'
        f" | {{ grep -F -- '--comment {RULE_COMMENT}' || true; }} | sed 's/^-A /-D /'"
        " | while read -r rule; do\n"
        "      # The rule's own words, split as iptables printed them.\n"
        "      # shellcheck disable=SC2086\n"
        '      "$ipt" -w $rule || true\n'
        "    done\n"
        "  done\n"
        "done\n"
    )


# -- docker -------------------------------------------------------------------------

#: The chain Docker evaluates before its own forward rules and never writes to.
DOCKER_USER_CHAIN: Final = "DOCKER-USER"
#: The iptables binaries a Docker host may keep that chain in (v4, then v6).
IPTABLES: Final = ("iptables", "ip6tables")
#: The table Docker's nftables firewall (``firewall-backend: nftables``) keeps
#: its bridges' rules in. It has no ``DOCKER-USER`` chain, and its forward
#: chain drops only what heads for Docker's own bridges.
DOCKER_NFT_TABLE: Final = ("ip", "docker-bridges")
#: What the operator of such a host is told, once per process, should a link
#: still be cut off there.
DOCKER_NFT_ADVICE: Final = (
    "Docker runs its nftables firewall on this host, which keeps no DOCKER-USER "
    "chain; it drops only traffic bound for its own bridges, so the node's links "
    "need no rule there. If a chat or an org worker still cannot reach the network, "
    "accept 'iifname \"vo*\"' and 'iifname \"vc*\"' (and their replies) in the "
    "forward chain of whatever else filters forwarding on this host."
)


@dataclass(slots=True)
class DockerFilter:
    """Docker's iptables forward drop, lifted in ``DOCKER-USER``."""

    name: str = "docker"
    told_nft: bool = False

    def active(self, host: Host) -> bool:
        if any(_has_chain(host, binary, DOCKER_USER_CHAIN) for binary in IPTABLES):
            return True
        self._tell_about_nftables_docker(host)
        return False

    def _tell_about_nftables_docker(self, host: Host) -> None:
        # Logged once per process, not at every heartbeat.
        if self.told_nft or host.run(("nft", "list", "table", *DOCKER_NFT_TABLE)) != 0:
            return
        self.told_nft = True
        logger.info(DOCKER_NFT_ADVICE)

    def open(self, host: Host, link_prefixes: Sequence[str]) -> bool:
        ok = True
        rules = link_accepts(link_prefixes)
        for binary in IPTABLES:
            if _has_chain(host, binary, DOCKER_USER_CHAIN):
                ok = _ensure_at_top(host, binary, DOCKER_USER_CHAIN, rules) and ok
        return ok

    def removal_script(self) -> str:
        return _chain_removal(IPTABLES, (DOCKER_USER_CHAIN,))

    def host_check(self) -> str:
        return ""


# -- ufw ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UfwFamily:
    """One IP family of ufw: its iptables binary, its before-rules file and
    the two chains of that file the node's lines go in."""

    binary: str
    rules_file: str
    input_chain: str
    forward_chain: str

    def lines(self, link_prefixes: Sequence[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Each (chain, rule) the node needs in this family, in order."""
        return (
            *((self.input_chain, rule) for rule in link_input_accepts(link_prefixes)),
            *((self.forward_chain, rule) for rule in link_accepts(link_prefixes)),
        )


UFW_FAMILIES: Final = (
    UfwFamily("iptables", "/etc/ufw/before.rules", "ufw-before-input", "ufw-before-forward"),
    UfwFamily("ip6tables", "/etc/ufw/before6.rules", "ufw6-before-input", "ufw6-before-forward"),
)


def _saved(chain: str, rule: Sequence[str]) -> str:
    return " ".join(("-A", chain, *rule))


def ufw_rules_with(text: str, lines: Sequence[str]) -> str:
    """``text`` (a ufw before-rules file) with each of ``lines`` it lacks put
    right after the ``*filter`` table's chain declarations, so they come first
    in their chains, as the live inserts do. Every other byte is kept, so
    taking the marked lines out again gives back the file as it was. A file
    with no ``*filter`` table is returned as it is."""
    kept = text.splitlines(keepends=True)
    missing = [line for line in lines if f"{line}\n" not in kept and line not in kept]
    if not missing:
        return text
    try:
        start = next(i for i, line in enumerate(kept) if line.strip() == "*filter")
    except StopIteration:
        return text
    at = start + 1
    for i in range(start + 1, len(kept)):
        stripped = kept[i].strip()
        if stripped.startswith(":"):
            at = i + 1
        elif stripped and not stripped.startswith("#"):
            break
    if at > 0 and not kept[at - 1].endswith("\n"):
        kept[at - 1] += "\n"
    return "".join([*kept[:at], *(f"{line}\n" for line in missing), *kept[at:]])


@dataclass(frozen=True, slots=True)
class UfwFilter:
    """ufw's forward and incoming drops, lifted at the top of its before
    chains, live and in its before-rules files."""

    name: str = "ufw"
    families: tuple[UfwFamily, ...] = UFW_FAMILIES

    def active(self, host: Host) -> bool:
        # ufw's chains exist only while it is enabled.
        first = self.families[0]
        return _has_chain(host, first.binary, first.forward_chain)

    def open(self, host: Host, link_prefixes: Sequence[str]) -> bool:
        ok = True
        for family in self.families:
            wanted = family.lines(link_prefixes)
            if _has_chain(host, family.binary, family.forward_chain):
                for chain in (family.input_chain, family.forward_chain):
                    rules = [rule for c, rule in wanted if c == chain]
                    ok = _ensure_at_top(host, family.binary, chain, rules) and ok
            ok = self._persist(host, family, [_saved(c, r) for c, r in wanted]) and ok
        return ok

    def _persist(self, host: Host, family: UfwFamily, lines: Sequence[str]) -> bool:
        text = host.read(family.rules_file)
        if text is None:
            return True  # no such family on this ufw (an IPv4-only build, say)
        updated = ufw_rules_with(text, lines)
        if updated == text:
            return True
        try:
            host.write(family.rules_file, updated)
        except OSError as exc:
            logger.warning("could not write %s: %s", family.rules_file, exc)
            return False
        return True

    def removal_script(self) -> str:
        binaries = [f.binary for f in self.families]
        chains = [c for f in self.families for c in (f.input_chain, f.forward_chain)]
        files = " ".join(f.rules_file for f in self.families)
        return _chain_removal(binaries, chains) + (
            f"for rules in {files}; do\n"
            f"  grep -qF -- '--comment {RULE_COMMENT}' \"$rules\" 2>/dev/null || continue\n"
            f'  grep -vF -- \'--comment {RULE_COMMENT}\' "$rules" > "$rules.alkera" || true\n'
            # Written back in place, so the file keeps its owner and mode.
            '  cat "$rules.alkera" > "$rules"\n'
            '  rm -f "$rules.alkera"\n'
            "done\n"
        )

    def host_check(self) -> str:
        # The live inserts need nothing but the iptables ufw itself runs on.
        return ""


# -- firewalld ----------------------------------------------------------------------

FIREWALL_CMD: Final = "firewall-cmd"
#: The zone the node binds its links to, and the policy from it to ANY.
FIREWALLD_ZONE: Final = "alkera-links"
FIREWALLD_POLICY: Final = "alkera-links-out"
#: Where firewalld keeps the two, should the uninstall find it stopped.
FIREWALLD_ZONE_FILE: Final = f"/etc/firewalld/zones/{FIREWALLD_ZONE}.xml"
FIREWALLD_POLICY_FILE: Final = f"/etc/firewalld/policies/{FIREWALLD_POLICY}.xml"


@dataclass(frozen=True, slots=True)
class FirewalldFilter:
    """firewalld's default-zone rejects, lifted by a zone of the node's own and
    a policy from it to ANY."""

    name: str = "firewalld"

    def active(self, host: Host) -> bool:
        return host.run((FIREWALL_CMD, "--state")) == 0

    def open(self, host: Host, link_prefixes: Sequence[str]) -> bool:
        fc = FIREWALL_CMD
        perm = (fc, "--permanent")
        ok = True
        created = False
        if host.run((*perm, f"--info-zone={FIREWALLD_ZONE}")) != 0:
            ok = host.run((*perm, f"--new-zone={FIREWALLD_ZONE}")) == 0 and ok
            ok = host.run((*perm, f"--zone={FIREWALLD_ZONE}", "--set-target=ACCEPT")) == 0 and ok
            created = True
        if host.run((*perm, f"--info-policy={FIREWALLD_POLICY}")) != 0:
            policy = (*perm, f"--policy={FIREWALLD_POLICY}")
            steps = (
                (*perm, f"--new-policy={FIREWALLD_POLICY}"),
                (*policy, f"--add-ingress-zone={FIREWALLD_ZONE}"),
                (*policy, "--add-egress-zone=ANY"),
                (*policy, "--set-target=ACCEPT"),
            )
            for step in steps:
                if host.run(step) != 0:
                    ok = False
                    break
            created = True
        interfaces = [f"{prefix}+" for prefix in link_prefixes]
        for iface in interfaces:
            zone = f"--zone={FIREWALLD_ZONE}"
            if host.run((*perm, zone, f"--query-interface={iface}")) != 0:
                ok = host.run((*perm, zone, f"--add-interface={iface}")) == 0 and ok
        if created:
            # A new zone or policy goes live only by a reload.
            ok = host.run((fc, "--reload")) == 0 and ok
        for iface in interfaces:
            zone = f"--zone={FIREWALLD_ZONE}"
            if host.run((fc, zone, f"--query-interface={iface}")) != 0:
                ok = host.run((fc, zone, f"--add-interface={iface}")) == 0 and ok
        return ok

    def removal_script(self) -> str:
        fc = FIREWALL_CMD
        zone = FIREWALLD_ZONE
        # The links leave the zone live, so nothing more is let through now;
        # the emptied zone and its policy stay in the running firewall until
        # its next reload, matching nothing, rather than reloading it here.
        return (
            f"if command -v {fc} >/dev/null 2>&1 && {fc} --state >/dev/null 2>&1; then\n"
            f"  for iface in $({fc} --zone={zone} --list-interfaces 2>/dev/null); do\n"
            f'    {fc} --zone={zone} --remove-interface="$iface" >/dev/null 2>&1 || true\n'
            "  done\n"
            f"  {fc} --permanent --delete-policy={FIREWALLD_POLICY} >/dev/null 2>&1 || true\n"
            f"  {fc} --permanent --delete-zone={zone} >/dev/null 2>&1 || true\n"
            "fi\n"
            f"rm -f {FIREWALLD_ZONE_FILE} {FIREWALLD_ZONE_FILE}.old"
            f" {FIREWALLD_POLICY_FILE} {FIREWALLD_POLICY_FILE}.old\n"
        )

    def host_check(self) -> str:
        return (
            "if systemctl is-active --quiet firewalld 2>/dev/null; then\n"
            f"  v=$({FIREWALL_CMD} --version 2>/dev/null)\n"
            '  case "$v" in\n'
            '    0.[0-8]|0.[0-8].*) echo "missing firewalld 0.9 or later'
            ' (firewalld $v is running)" ;;\n'
            "  esac\n"
            "fi\n"
        )


register(DockerFilter())
register(UfwFilter())
register(FirewalldFilter())


def host_firewall_removal_script() -> str:
    """The shell that takes everything the box put into a host's firewall back
    out: the live table, the line that reloads it at boot (left behind, it
    would name a file the uninstall deletes and fail the host's nftables at
    its next boot), the persisted forwarding, and every host filter's rules.
    Live forwarding is left on: something else on the host may rely on it."""
    family, table = NFT_TABLE
    return (
        f"nft delete table {family} {table} 2>/dev/null || true\n"
        f"if [ -f {NFTABLES_CONF} ]; then\n"
        f'  kept="$(grep -vxF \'include "{NFT_FILE}"\' {NFTABLES_CONF} || true)"\n'
        # Written back in place, so the file keeps its owner and mode.
        f"  printf '%s\\n' \"$kept\" > {NFTABLES_CONF}\n"
        "fi\n"
        f"rm -f {SYSCTL_FILE}\n"
        f"{removal_script()}"
    )


__all__ = [
    "DOCKER_NFT_ADVICE",
    "DOCKER_NFT_TABLE",
    "DOCKER_USER_CHAIN",
    "FIREWALLD_POLICY",
    "FIREWALLD_POLICY_FILE",
    "FIREWALLD_ZONE",
    "FIREWALLD_ZONE_FILE",
    "FIREWALL_CMD",
    "IPTABLES",
    "RULE_COMMENT",
    "UFW_FAMILIES",
    "DockerFilter",
    "FirewalldFilter",
    "Host",
    "HostForwardFilter",
    "UfwFamily",
    "UfwFilter",
    "host_check_script",
    "host_firewall_removal_script",
    "link_accepts",
    "link_input_accepts",
    "open_links",
    "register",
    "registered",
    "removal_script",
    "ufw_rules_with",
]
