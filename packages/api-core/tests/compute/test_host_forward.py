"""The host firewalls a node lets its links through: ufw and firewalld beside
Docker, each one probed, opened without a second copy of anything, and taken
back out leaving the operator's own rules as they were.

ufw is a stateful stand-in for its iptables chains and its two before-rules
files; firewalld a stand-in that keeps a permanent and a runtime
configuration and copies one over the other on ``--reload``. What is asserted
is the firewall a host ends up with, not which commands ran. The removal and
the Test connection checks run under a real shell with the host's commands
on shims and its files under ``tmp_path``."""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alkera_core.compute import box_egress as egress
from alkera_core.compute import host_forward as hf
from alkera_core.compute.ssh.probe import FACTS_SCRIPT, facts_script, parse_facts

ON_A_BOX = {egress.ENV_SANDBOX_MODE: "gvisor"}
COMMENT = ("-m", "comment", "--comment", "alkera-sandbox")

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs a POSIX bash; the Windows runner's bash is the WSL launcher with no distribution",
)

#: ufw's before.rules as Ubuntu ships it, cut to its shape: a nat table an
#: operator added, the filter table's declarations, its rules, COMMIT.
BEFORE_RULES = """\
#
# rules.before
#
*nat
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s 192.168.50.0/24 -o eth0 -j MASQUERADE
COMMIT

# Don't delete these required lines, otherwise there will be errors
*filter
:ufw-before-input - [0:0]
:ufw-before-output - [0:0]
:ufw-before-forward - [0:0]
:ufw-not-local - [0:0]
# End required lines


# allow all on loopback
-A ufw-before-input -i lo -j ACCEPT
-A ufw-before-output -o lo -j ACCEPT

# quickly process packets for which we already have a connection
-A ufw-before-input -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A ufw-before-forward -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A ufw-before-input -m conntrack --ctstate INVALID -j DROP

# don't delete the 'COMMIT' line or these rules won't be processed
COMMIT
"""
BEFORE6_RULES = (
    BEFORE_RULES.replace("ufw-", "ufw6-")
    .replace("*nat\n:POSTROUTING ACCEPT [0:0]\n", "")
    .replace("-A POSTROUTING -s 192.168.50.0/24 -o eth0 -j MASQUERADE\nCOMMIT\n", "")
)
V4, V6 = hf.UFW_FAMILIES
OPERATORS_INPUT = ("-p", "tcp", "--dport", "22", "-j", "ACCEPT")


def _forward(prefix: str) -> list[tuple[str, ...]]:
    return [
        ("-i", f"{prefix}+", *COMMENT, "-j", "ACCEPT"),
        (
            *("-o", f"{prefix}+", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED"),
            *COMMENT,
            "-j",
            "ACCEPT",
        ),
    ]


def _input(prefix: str) -> tuple[str, ...]:
    return ("-i", f"{prefix}+", *COMMENT, "-j", "ACCEPT")


@dataclass
class UfwHost:
    """A host whose ufw is enabled (its chains exist) or not, with its files."""

    chains: dict[tuple[str, str], list[tuple[str, ...]]]
    files: dict[str, str]
    writes: list[str] = field(default_factory=list)
    inserts: int = 0

    @classmethod
    def enabled(cls, *, ipv6: bool = True) -> UfwHost:
        chains: dict[tuple[str, str], list[tuple[str, ...]]] = {
            ("iptables", V4.input_chain): [OPERATORS_INPUT],
            ("iptables", V4.forward_chain): [],
        }
        files = {V4.rules_file: BEFORE_RULES}
        if ipv6:
            chains[("ip6tables", V6.input_chain)] = [OPERATORS_INPUT]
            chains[("ip6tables", V6.forward_chain)] = []
            files[V6.rules_file] = BEFORE6_RULES
        return cls(chains, files)

    def run(self, argv: Sequence[str]) -> int:
        if argv[0] not in hf.IPTABLES:
            return 1  # no nftables Docker, no firewalld
        binary, wait, verb, chain, *rest = argv
        assert wait == "-w"
        rules = self.chains.get((binary, chain))
        if rules is None:
            return 1
        if verb == "-S":
            return 0
        if verb == "-C":
            return 0 if tuple(rest) in rules else 1
        if verb == "-I":
            position, *body = rest
            rules.insert(int(position) - 1, tuple(body))
            self.inserts += 1
            return 0
        raise AssertionError(f"unexpected iptables call {argv}")

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def write(self, path: str, text: str) -> None:
        self.writes.append(path)
        self.files[path] = text

    def host(self) -> hf.Host:
        return hf.Host(run=self.run, read=self.read, write=self.write)


def test_ufw_lets_the_links_in_to_the_host_and_out_through_forwarding_ahead_of_the_rest() -> None:
    """ufw's incoming deny would cut a chat off from the node's tool server on
    the host, its forward drop every link from the network: both are lifted,
    first in their chains, the operator's own rules kept after them."""
    ufw = UfwHost.enabled()

    assert hf.open_links(ON_A_BOX, ("vc", "vo"), host=ufw.host()) == ("ufw",)

    for binary, family in (("iptables", V4), ("ip6tables", V6)):
        assert ufw.chains[(binary, family.input_chain)] == [
            _input("vc"),
            _input("vo"),
            OPERATORS_INPUT,
        ]
        assert ufw.chains[(binary, family.forward_chain)] == [*_forward("vc"), *_forward("vo")]


def test_ufw_keeps_the_lines_in_its_before_rules_where_its_reload_reads_them_first() -> None:
    ufw = UfwHost.enabled()
    hf.open_links(ON_A_BOX, ("vo",), host=ufw.host())

    lines = ufw.files[V4.rules_file].splitlines()
    declarations_end = lines.index(":ufw-not-local - [0:0]")
    assert lines[declarations_end + 1 : declarations_end + 4] == [
        "-A ufw-before-input -i vo+ -m comment --comment alkera-sandbox -j ACCEPT",
        "-A ufw-before-forward -i vo+ -m comment --comment alkera-sandbox -j ACCEPT",
        "-A ufw-before-forward -o vo+ -m conntrack --ctstate RELATED,ESTABLISHED"
        " -m comment --comment alkera-sandbox -j ACCEPT",
    ]
    assert lines[declarations_end + 4] == "# End required lines"
    six = ufw.files[V6.rules_file]
    assert "-A ufw6-before-forward -i vo+ -m comment --comment alkera-sandbox -j ACCEPT\n" in six


def test_opening_ufw_again_changes_nothing() -> None:
    ufw = UfwHost.enabled()
    hf.open_links(ON_A_BOX, ("vc", "vo"), host=ufw.host())
    chains, files = copy.deepcopy(ufw.chains), dict(ufw.files)
    ufw.writes.clear()
    ufw.inserts = 0

    for _ in range(3):
        assert hf.open_links(ON_A_BOX, ("vc", "vo"), host=ufw.host()) == ("ufw",)

    assert (ufw.chains, ufw.files, ufw.writes, ufw.inserts) == (chains, files, [], 0)


def test_a_ufw_reload_that_dropped_the_live_rules_gets_them_back_once() -> None:
    """A ``ufw reload`` rebuilds the chains from the files, which carry the
    lines; a hand flush does not, and the next heartbeat puts them back."""
    ufw = UfwHost.enabled()
    hf.open_links(ON_A_BOX, ("vo",), host=ufw.host())
    forward = ufw.chains[("iptables", V4.forward_chain)]
    expected = list(forward)
    forward.clear()

    hf.open_links(ON_A_BOX, ("vo",), host=ufw.host())
    hf.open_links(ON_A_BOX, ("vo",), host=ufw.host())

    assert forward == expected


def test_a_disabled_ufw_is_left_alone() -> None:
    """ufw installed but off keeps no chains; its files are not touched."""
    ufw = UfwHost(chains={}, files={V4.rules_file: BEFORE_RULES})
    assert hf.open_links(ON_A_BOX, ("vo",), host=ufw.host()) == ()
    assert (ufw.files, ufw.writes) == ({V4.rules_file: BEFORE_RULES}, [])


def test_an_ipv4_only_ufw_gets_only_its_ipv4_side() -> None:
    ufw = UfwHost.enabled(ipv6=False)
    assert hf.open_links(ON_A_BOX, ("vo",), host=ufw.host()) == ("ufw",)
    assert ufw.writes == [V4.rules_file]
    assert set(ufw.chains) == {("iptables", V4.input_chain), ("iptables", V4.forward_chain)}


def test_a_before_rules_that_cannot_be_written_is_reported_and_the_live_rules_stay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ufw = UfwHost.enabled(ipv6=False)

    def refuse(path: str, text: str) -> None:
        raise PermissionError(path)

    host = hf.Host(run=ufw.run, read=ufw.read, write=refuse)
    with caplog.at_level("WARNING", logger=hf.__name__):
        assert hf.open_links(ON_A_BOX, ("vo",), host=host) == ()
    assert ufw.chains[("iptables", V4.forward_chain)] == _forward("vo")
    assert f"could not write {V4.rules_file}" in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("# an operator emptied it\n", id="no-filter-table"),
        pytest.param("", id="empty"),
    ],
)
def test_a_before_rules_with_no_filter_table_is_kept_as_it_is(text: str) -> None:
    assert hf.ufw_rules_with(text, ["-A ufw-before-forward -i vo+ -j ACCEPT"]) == text


def test_the_lines_go_in_even_when_the_last_declaration_has_no_newline() -> None:
    text = "*filter\n:ufw-before-forward - [0:0]"
    assert hf.ufw_rules_with(text, ["-A x"]) == "*filter\n:ufw-before-forward - [0:0]\n-A x\n"


# -- the ufw removal ------------------------------------------------------------------

_IPTABLES_SHIM = r"""#!/usr/bin/env bash
# A stand-in iptables: each chain is the file "$STATE/<binary>.<chain>", one
# rule per line in -S form; a chain with no file does not exist.
[ "$1" = -w ] && shift
verb="$1"; chain="$2"; shift 2
state="$STATE/$(basename "$0").$chain"
[ -f "$state" ] || exit 1
case "$verb" in
  -S) cat "$state" ;;
  -D) line="-A $chain $*"
      grep -qxF -- "$line" "$state" || exit 1
      grep -vxF -- "$line" "$state" > "$state.new" || true
      mv "$state.new" "$state" ;;
  *) exit 2 ;;
esac
"""


def _bin(tmp_path: Path, shims: dict[str, str]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in shims.items():
        shim = bin_dir / name
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)
    return bin_dir


def _relocated(script: str, tmp_path: Path) -> str:
    return script.replace("/etc/ufw", str(tmp_path / "ufw")).replace(
        "/etc/firewalld", str(tmp_path / "firewalld")
    )


def _bash(script: str, tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _relocated(script, tmp_path)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STATE": str(tmp_path)},
        capture_output=True,
        text=True,
    )


@needs_bash
def test_the_ufw_removal_gives_back_the_operators_files_byte_for_byte(tmp_path: Path) -> None:
    """Applied by the node, then removed by the uninstall: the before-rules
    files are what the operator had, and the live chains hold only their own."""
    ufw = UfwHost.enabled()
    hf.open_links(ON_A_BOX, ("vc", "vo"), host=ufw.host())
    (tmp_path / "ufw").mkdir()
    for family in (V4, V6):
        target = tmp_path / "ufw" / Path(family.rules_file).name
        target.write_text(ufw.files[family.rules_file], encoding="utf-8")
    for (binary, chain), rules in ufw.chains.items():
        (tmp_path / f"{binary}.{chain}").write_text(
            "".join(" ".join(("-A", chain, *rule)) + "\n" for rule in rules), encoding="utf-8"
        )
    bin_dir = _bin(tmp_path, {"iptables": _IPTABLES_SHIM, "ip6tables": _IPTABLES_SHIM})

    for _ in range(2):  # a second run finds nothing left and still succeeds
        done = _bash(hf.UfwFilter().removal_script(), tmp_path, bin_dir)
        assert done.returncode == 0, done.stderr

    assert (tmp_path / "ufw" / "before.rules").read_bytes() == BEFORE_RULES.encode()
    assert (tmp_path / "ufw" / "before6.rules").read_bytes() == BEFORE6_RULES.encode()
    for binary, family in (("iptables", V4), ("ip6tables", V6)):
        kept = (tmp_path / f"{binary}.{family.input_chain}").read_text().splitlines()
        assert kept == [" ".join(("-A", family.input_chain, *OPERATORS_INPUT))]
        assert (tmp_path / f"{binary}.{family.forward_chain}").read_text() == ""
    assert sorted(p.name for p in (tmp_path / "ufw").iterdir()) == ["before.rules", "before6.rules"]


@needs_bash
def test_the_ufw_removal_leaves_a_file_it_never_wrote_untouched(tmp_path: Path) -> None:
    (tmp_path / "ufw").mkdir()
    rules = tmp_path / "ufw" / "before.rules"
    rules.write_text(BEFORE_RULES, encoding="utf-8")
    rules.chmod(0o640)
    before = rules.stat()
    bin_dir = _bin(tmp_path, {"iptables": _IPTABLES_SHIM})

    done = _bash(hf.UfwFilter().removal_script(), tmp_path, bin_dir)

    assert done.returncode == 0, done.stderr
    after = rules.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )


# -- firewalld ------------------------------------------------------------------------


@dataclass
class FirewalldConfig:
    zones: dict[str, dict[str, object]]
    policies: dict[str, dict[str, object]]


def _stock() -> FirewalldConfig:
    return FirewalldConfig(
        zones={
            "public": {"target": "default", "interfaces": ["eth0"]},
            "trusted": {"target": "ACCEPT", "interfaces": []},
        },
        policies={
            "allow-host-ipv6": {"ingress": ["ANY"], "egress": ["HOST"], "target": "CONTINUE"}
        },
    )


@dataclass
class Firewalld:
    """firewalld's two configurations; ``--reload`` makes runtime the permanent.
    ``policies=False`` is a firewalld before 0.9, which has none."""

    running: bool = True
    policies: bool = True
    permanent: FirewalldConfig = field(default_factory=_stock)
    runtime: FirewalldConfig = field(default_factory=_stock)
    reloads: int = 0
    changes: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> int:
        if argv[0] != hf.FIREWALL_CMD:
            return 1  # no iptables chains of Docker's or ufw's
        args = list(argv[1:])
        if args == ["--state"]:
            return 0 if self.running else 252
        assert self.running, argv
        if args == ["--reload"]:
            self.reloads += 1
            self.runtime = copy.deepcopy(self.permanent)
            return 0
        config = self.runtime
        if args[0] == "--permanent":
            config, args = self.permanent, args[1:]
        options = dict(arg.removeprefix("--").split("=", 1) for arg in args if "=" in arg)
        flags = [arg.removeprefix("--") for arg in args]
        verb = next(
            f.split("=", 1)[0] for f in flags if f.split("=", 1)[0] not in ("zone", "policy")
        )
        if "polic" in verb and not self.policies:
            return 2  # firewall-cmd: error: unrecognized arguments
        if verb not in ("info-zone", "info-policy", "query-interface"):
            self.changes.append(tuple(argv))
        if verb == "info-zone":
            return 0 if options["info-zone"] in config.zones else 112
        if verb == "info-policy":
            return 0 if options["info-policy"] in config.policies else 112
        if verb == "new-zone":
            assert config is self.permanent, "a zone is made only in the permanent configuration"
            config.zones[options["new-zone"]] = {"target": "default", "interfaces": []}
            return 0
        if verb == "new-policy":
            assert config is self.permanent
            config.policies[options["new-policy"]] = {
                "ingress": [],
                "egress": [],
                "target": "CONTINUE",
            }
            return 0
        if "policy" in options:
            policy = config.policies[options["policy"]]
            if verb == "add-ingress-zone":
                policy["ingress"] = [*policy["ingress"], options[verb]]  # type: ignore[misc]
            elif verb == "add-egress-zone":
                policy["egress"] = [*policy["egress"], options[verb]]  # type: ignore[misc]
            elif verb == "set-target":
                policy["target"] = options[verb]
            else:
                raise AssertionError(argv)
            return 0
        zone = config.zones.get(options.get("zone", ""))
        if zone is None:
            return 112
        interfaces: list[str] = zone["interfaces"]  # type: ignore[assignment]
        if verb == "query-interface":
            return 0 if options[verb] in interfaces else 1
        if verb == "add-interface":
            interfaces.append(options[verb])
            return 0
        if verb == "set-target":
            zone["target"] = options[verb]
            return 0
        raise AssertionError(f"unexpected firewall-cmd call {argv}")

    def host(self) -> hf.Host:
        return hf.Host(run=self.run)


OURS_ZONE = {"target": "ACCEPT", "interfaces": ["vc+", "vo+"]}
OURS_POLICY = {"ingress": [hf.FIREWALLD_ZONE], "egress": ["ANY"], "target": "ACCEPT"}


def test_firewalld_gets_a_zone_for_the_links_and_a_policy_from_it_to_any() -> None:
    """Live and permanent alike, after one reload (the only way a new zone
    goes live); the operator's zones and policies are as they were."""
    fw = Firewalld()

    assert hf.open_links(ON_A_BOX, ("vc", "vo"), host=fw.host()) == ("firewalld",)

    stock = _stock()
    for config in (fw.permanent, fw.runtime):
        assert config.zones == {**stock.zones, hf.FIREWALLD_ZONE: OURS_ZONE}
        assert config.policies == {**stock.policies, hf.FIREWALLD_POLICY: OURS_POLICY}
    assert fw.reloads == 1


def test_opening_firewalld_again_changes_nothing() -> None:
    fw = Firewalld()
    hf.open_links(ON_A_BOX, ("vc", "vo"), host=fw.host())
    fw.changes.clear()

    for _ in range(3):
        assert hf.open_links(ON_A_BOX, ("vc", "vo"), host=fw.host()) == ("firewalld",)

    assert (fw.changes, fw.reloads) == ([], 1)


def test_a_second_kind_of_link_joins_the_zone_without_a_reload() -> None:
    """The supervisor opens ``vo``, a single daemon ``vc``: whichever comes
    second only adds its interface, live and permanent."""
    fw = Firewalld()
    hf.open_links(ON_A_BOX, ("vo",), host=fw.host())
    hf.open_links(ON_A_BOX, ("vc",), host=fw.host())

    assert fw.reloads == 1
    for config in (fw.permanent, fw.runtime):
        assert config.zones[hf.FIREWALLD_ZONE] == {"target": "ACCEPT", "interfaces": ["vo+", "vc+"]}


def test_a_link_lost_from_the_running_zone_is_put_back_without_a_reload() -> None:
    fw = Firewalld()
    hf.open_links(ON_A_BOX, ("vo",), host=fw.host())
    fw.runtime.zones[hf.FIREWALLD_ZONE]["interfaces"] = []

    hf.open_links(ON_A_BOX, ("vo",), host=fw.host())

    assert fw.runtime.zones[hf.FIREWALLD_ZONE]["interfaces"] == ["vo+"]
    assert fw.reloads == 1


def test_a_stopped_firewalld_is_left_alone() -> None:
    fw = Firewalld(running=False)
    assert hf.open_links(ON_A_BOX, ("vo",), host=fw.host()) == ()
    assert (fw.changes, fw.permanent, fw.runtime) == ([], _stock(), _stock())


def test_a_firewalld_without_policies_is_reported_not_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fw = Firewalld(policies=False)
    with caplog.at_level("WARNING", logger=hf.__name__):
        assert hf.open_links(ON_A_BOX, ("vo",), host=fw.host()) == ()
    assert "firewalld refused part of the node's link rules" in caplog.text


_FIREWALL_CMD_SHIM = r"""#!/bin/sh
echo "firewall-cmd $*" >> "$STATE/calls"
case "$1" in
  --state) [ -f "$STATE/running" ] ;;
  --zone=alkera-links)
    [ "$2" = --list-interfaces ] && echo "vc+ vo+"
    exit 0 ;;
esac
"""


@needs_bash
@pytest.mark.parametrize("running", [True, False], ids=["running", "stopped"])
def test_the_firewalld_removal_takes_the_links_out_live_and_the_zone_and_policy_for_good(
    tmp_path: Path, running: bool
) -> None:
    """The links leave the running zone, so nothing more gets through now,
    without a reload that would drop the operator's runtime changes; the
    zone and its policy go from the permanent configuration, and their files
    go even when firewalld is stopped."""
    for path in (hf.FIREWALLD_ZONE_FILE, hf.FIREWALLD_POLICY_FILE):
        moved = Path(_relocated(path, tmp_path))
        moved.parent.mkdir(parents=True, exist_ok=True)
        moved.write_text("<zone/>", encoding="utf-8")
    theirs = tmp_path / "firewalld" / "zones" / "public.xml"
    theirs.write_text("<zone/>", encoding="utf-8")
    if running:
        (tmp_path / "running").touch()
    bin_dir = _bin(tmp_path, {"firewall-cmd": _FIREWALL_CMD_SHIM})

    done = _bash(hf.FirewalldFilter().removal_script(), tmp_path, bin_dir)

    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    mutations = [c for c in calls if c not in ("firewall-cmd --state",) and "--list" not in c]
    if running:
        assert mutations == [
            "firewall-cmd --zone=alkera-links --remove-interface=vc+",
            "firewall-cmd --zone=alkera-links --remove-interface=vo+",
            "firewall-cmd --permanent --delete-policy=alkera-links-out",
            "firewall-cmd --permanent --delete-zone=alkera-links",
        ]
    else:
        assert mutations == []
    assert not any("--reload" in c for c in calls)
    assert sorted(p.name for p in (tmp_path / "firewalld").rglob("*.xml")) == ["public.xml"]


# -- Test connection --------------------------------------------------------------------

_SYSTEMCTL_SHIM = (
    '#!/bin/sh\n[ "$1 $2 $3" = "is-active --quiet firewalld" ] && [ -f "$STATE/running" ]\n'
)
_FIREWALL_CMD_VERSION_SHIM = '#!/bin/sh\n[ "$1" = --version ] && cat "$STATE/version"\n'


@needs_bash
@pytest.mark.parametrize(
    ("running", "version", "missing"),
    [
        pytest.param(
            True, "0.8.2", ["firewalld 0.9 or later (firewalld 0.8.2 is running)"], id="0.8"
        ),
        pytest.param(
            True, "0.6.3", ["firewalld 0.9 or later (firewalld 0.6.3 is running)"], id="0.6"
        ),
        pytest.param(True, "0.9.0", [], id="0.9-has-policies"),
        pytest.param(True, "0.10.3", [], id="0.10-is-not-0.1"),
        pytest.param(True, "1.3.4", [], id="1.x"),
        pytest.param(False, "0.8.2", [], id="stopped-old-firewalld-is-no-matter"),
    ],
)
def test_test_connection_names_a_running_firewalld_the_node_cannot_open(
    tmp_path: Path, running: bool, version: str, missing: list[str]
) -> None:
    (tmp_path / "version").write_text(version + "\n", encoding="utf-8")
    if running:
        (tmp_path / "running").touch()
    bin_dir = _bin(
        tmp_path,
        {"systemctl": _SYSTEMCTL_SHIM, "firewall-cmd": _FIREWALL_CMD_VERSION_SHIM},
    )
    facts = "Linux\nx86_64\nyes\nyes\n8\n31\n200\n0\nyes\n"

    done = subprocess.run(
        ["sh", "-c", hf.host_check_script()],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STATE": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    assert parse_facts(facts + done.stdout).missing == missing


def test_the_probe_runs_the_facts_then_every_filters_check() -> None:
    assert facts_script() == FACTS_SCRIPT + hf.host_check_script()
    assert hf.FirewalldFilter().host_check() in facts_script()


def test_a_check_line_never_shifts_the_facts_read_by_position() -> None:
    facts = parse_facts(
        "Linux\nx86_64\nyes\nno\n8\n31\n200\n0\nyes\nmissing firewalld 0.9 or later\n"
    )
    assert facts.missing == ["root or passwordless sudo", "firewalld 0.9 or later"]
    assert (facts.vcpu, facts.org_units) == (8, True)


# -- the registry -------------------------------------------------------------------------


def test_the_registry_holds_docker_ufw_and_firewalld() -> None:
    assert [f.name for f in hf.registered()] == ["docker", "ufw", "firewalld"]


@dataclass
class Recorder:
    name: str
    is_active: bool = True
    opened: list[tuple[str, ...]] = field(default_factory=list)
    fails: bool = False

    def active(self, host: hf.Host) -> bool:
        if self.fails:
            raise RuntimeError("the firewall's tool crashed")
        return self.is_active

    def open(self, host: hf.Host, link_prefixes: Sequence[str]) -> bool:
        self.opened.append(tuple(link_prefixes))
        return True

    def removal_script(self) -> str:
        return f"remove-{self.name}\n"

    def host_check(self) -> str:
        return f"check-{self.name}\n"


def test_a_registered_filter_is_opened_removed_and_checked_with_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hf, "_FILTERS", {})
    crashing, idle, extra = Recorder("a", fails=True), Recorder("b", is_active=False), Recorder("c")
    for entry in (crashing, idle, extra):
        hf.register(entry)

    assert hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=lambda argv: 1)) == ("c",)
    assert (idle.opened, extra.opened) == ([], [("vo",)])
    assert hf.removal_script() == "remove-a\nremove-b\nremove-c\n"
    assert hf.host_check_script() == "check-a\ncheck-b\ncheck-c\n"
    assert hf.removal_script() in hf.host_firewall_removal_script()


def test_off_a_box_no_firewall_is_even_probed() -> None:
    ran: list[Sequence[str]] = []
    assert hf.open_links({}, ("vo",), host=hf.Host(run=lambda argv: ran.append(argv) or 0)) == ()
    assert ran == []


def test_a_host_with_no_firewall_of_its_own_is_only_looked_at() -> None:
    """Every probe answers no: nothing is inserted, written or created."""
    ran: list[tuple[str, ...]] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 1

    def write(path: str, text: str) -> None:
        raise AssertionError(f"wrote {path}")

    host = hf.Host(run=run, read=lambda path: None, write=write)
    assert hf.open_links(ON_A_BOX, ("vc", "vo"), host=host) == ()
    assert {argv[:3] for argv in ran} <= {
        ("iptables", "-w", "-S"),
        ("ip6tables", "-w", "-S"),
        ("nft", "list", "table"),
        ("firewall-cmd", "--state"),
    }
