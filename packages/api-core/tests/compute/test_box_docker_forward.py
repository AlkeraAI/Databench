"""A box on a host that also runs Docker: its links get through Docker's
forward drop, at every start, without a second copy of a rule; and an
uninstall takes back every firewall change the box made, and only those.

The iptables side is a small stateful stand-in (chains holding rules), so
what is asserted is the chain a host ends up with, not which commands ran.
The removal shells run under bash with ``iptables``, ``nft`` and the host
files on shims."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from alkera_core.compute import box_egress as egress
from alkera_core.compute import host_forward as hf

ON_A_BOX = {egress.ENV_SANDBOX_MODE: "gvisor"}
COMMENT = ("-m", "comment", "--comment", "alkera-sandbox")

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs a POSIX bash; the Windows runner's bash is the WSL launcher with no distribution",
)


class Host:
    """The iptables chains a host keeps, per binary; a missing chain is a host
    whose Docker (or whose IP family) has none."""

    def __init__(self, chains: dict[str, list[tuple[str, ...]]]) -> None:
        self.chains = chains

    def run(self, argv: Sequence[str]) -> int:
        if argv[0] not in hf.IPTABLES:
            return 1  # no nftables Docker, no firewalld on this host
        binary, wait, verb, chain, *rest = argv
        assert wait == "-w"
        rules = self.chains.get(binary)
        if rules is None or chain != "DOCKER-USER":  # no ufw either
            return 1
        rule = tuple(rest)
        if verb == "-S":
            return 0
        if verb == "-C":
            return 0 if rule in rules else 1
        if verb == "-I":
            position, *body = rule
            rules.insert(int(position) - 1, tuple(body))
            return 0
        raise AssertionError(f"unexpected iptables call {argv}")


def test_the_rules_let_out_what_a_link_sends_and_only_replies_back_in() -> None:
    assert hf.link_accepts(("vo",)) == (
        ("-i", "vo+", *COMMENT, "-j", "ACCEPT"),
        (
            "-o",
            "vo+",
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            *COMMENT,
            "-j",
            "ACCEPT",
        ),
    )


def test_a_docker_host_gets_the_box_links_ahead_of_its_own_rules() -> None:
    """Docker's own ``RETURN`` (or an operator's drop) stays last: the box's
    accepts go first, or a drop above them would still cut the links off."""
    operators = ("-s", "203.0.113.0/24", "-j", "DROP")
    host = Host({"iptables": [operators]})
    applied = hf.open_links(ON_A_BOX, ("vc", "vo"), host=hf.Host(run=host.run))
    assert applied == ("docker",)
    assert host.chains["iptables"] == [*hf.link_accepts(("vc", "vo")), operators]


def test_starting_again_adds_no_second_copy() -> None:
    host = Host({"iptables": [], "ip6tables": []})
    for _ in range(3):
        hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=host.run))
    expected = list(hf.link_accepts(("vo",)))
    assert host.chains == {"iptables": expected, "ip6tables": expected}


def test_a_rule_lost_since_is_put_back_and_the_rest_left() -> None:
    first, second = hf.link_accepts(("vo",))
    host = Host({"iptables": [second]})
    hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=host.run))
    assert sorted(host.chains["iptables"]) == sorted([first, second])


def test_a_host_without_docker_is_left_alone() -> None:
    host = Host({})
    assert hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=host.run)) == ()
    assert host.chains == {}


def test_off_a_box_docker_is_never_touched() -> None:
    host = Host({"iptables": []})
    assert hf.open_links({}, ("vo",), host=hf.Host(run=host.run)) == ()
    assert host.chains == {"iptables": []}


def test_a_refused_insert_is_survived(caplog: pytest.LogCaptureFixture) -> None:
    def refuses(argv: Sequence[str]) -> int:
        return 0 if argv[0] in hf.IPTABLES and tuple(argv[2:4]) == ("-S", "DOCKER-USER") else 1

    with caplog.at_level("WARNING", logger=hf.__name__):
        assert hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=refuses)) == ()
    assert "docker refused part of the node's link rules" in caplog.text


# -- the removal -------------------------------------------------------------------

_IPTABLES_SHIM = r"""#!/usr/bin/env bash
# A stand-in iptables: DOCKER-USER is the file "$STATE/$(basename "$0")", one
# rule per line in -S form.
state="$STATE/$(basename "$0")"
[ "$1" = -w ] && shift
verb="$1"; chain="$2"; shift 2
[ -f "$state" ] && [ "$chain" = DOCKER-USER ] || exit 1
case "$verb" in
  -S) cat "$state" ;;
  -D) line="-A $chain $*"
      grep -qxF -- "$line" "$state" || exit 1
      grep -vxF -- "$line" "$state" > "$state.new" || true
      mv "$state.new" "$state" ;;
  *) exit 2 ;;
esac
"""


def _shims(tmp_path: Path, names: Sequence[str]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        shim = bin_dir / name
        shim.write_text(_IPTABLES_SHIM, encoding="utf-8")
        shim.chmod(0o755)
    nft = bin_dir / "nft"
    nft.write_text('#!/bin/sh\necho "$@" >> "$STATE/nft.calls"\n', encoding="utf-8")
    nft.chmod(0o755)
    return bin_dir


def _as_saved(rule: Sequence[str]) -> str:
    return " ".join(("-A", "DOCKER-USER", *rule))


def _run(script: str, tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STATE": str(tmp_path)},
        capture_output=True,
        text=True,
    )


@needs_bash
def test_the_removal_takes_out_the_box_rules_and_only_those(tmp_path: Path) -> None:
    operators = "-A DOCKER-USER -s 203.0.113.0/24 -j DROP"
    ours = [_as_saved(rule) for rule in hf.link_accepts(("vc", "vo"))]
    (tmp_path / "iptables").write_text("\n".join([ours[0], operators, *ours[1:]]) + "\n")
    (tmp_path / "ip6tables").write_text(ours[2] + "\n")
    bin_dir = _shims(tmp_path, ("iptables", "ip6tables"))

    for _ in range(2):  # a second run finds nothing left and still succeeds
        done = _run(hf.DockerFilter().removal_script(), tmp_path, bin_dir)
        assert done.returncode == 0, done.stderr

    assert (tmp_path / "iptables").read_text().splitlines() == [operators]
    assert (tmp_path / "ip6tables").read_text().splitlines() == []


@needs_bash
def test_the_removal_runs_on_a_host_without_docker(tmp_path: Path) -> None:
    bin_dir = _shims(tmp_path, ("iptables",))  # no chain, and no ip6tables at all
    done = _run(hf.DockerFilter().removal_script(), tmp_path, bin_dir)
    assert done.returncode == 0, done.stderr


@needs_bash
def test_the_host_firewall_removal_leaves_the_host_as_it_was(tmp_path: Path) -> None:
    """The live table goes, the boot include goes (left, it names a file the
    uninstall deletes and the host's nftables fails at its next boot), the
    persisted forwarding goes; the operator's own config lines stay."""
    conf = tmp_path / "nftables.conf"
    conf.write_text(
        '#!/usr/sbin/nft -f\nflush ruleset\ninclude "/etc/nftables.d/mine.nft"\n'
        f'\ninclude "{egress.NFT_FILE}"\n',
        encoding="utf-8",
    )
    sysctl = tmp_path / "90-alkera-sandbox.conf"
    sysctl.write_text("net.ipv4.ip_forward = 1\n", encoding="utf-8")
    (tmp_path / "iptables").write_text(
        _as_saved(hf.link_accepts(("vo",))[0]) + "\n", encoding="utf-8"
    )
    bin_dir = _shims(tmp_path, ("iptables",))
    script = (
        hf.host_firewall_removal_script()
        .replace(egress.NFTABLES_CONF, str(conf))
        .replace(egress.SYSCTL_FILE, str(sysctl))
        .replace("/etc/ufw", str(tmp_path / "ufw"))
        .replace("/etc/firewalld", str(tmp_path / "firewalld"))
    )

    done = _run(script, tmp_path, bin_dir)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "nft.calls").read_text().splitlines() == ["delete table inet alkera_sandbox"]
    assert conf.read_text().splitlines() == [
        "#!/usr/sbin/nft -f",
        "flush ruleset",
        'include "/etc/nftables.d/mine.nft"',
    ]
    assert not sysctl.exists()
    assert (tmp_path / "iptables").read_text() == ""


def test_the_removal_names_the_files_the_prerequisites_write() -> None:
    script = Path(egress.__file__).with_name("sandbox_prereqs.sh").read_text(encoding="utf-8")
    assert 'NFT_FILE="$ENV_DIR/sandbox.nft"' in script and "ENV_DIR=/etc/alkera\n" in script
    assert egress.NFT_FILE == "/etc/alkera/sandbox.nft"
    assert f"SYSCTL_FILE={egress.SYSCTL_FILE}\n" in script
    assert f'printf \'\\ninclude "%s"\\n\' "$NFT_FILE" >> {egress.NFTABLES_CONF}' in script


@pytest.mark.parametrize(
    ("docker_tables", "told"),
    [
        pytest.param({("ip", "docker-bridges")}, 1, id="nftables-docker-is-told-once"),
        pytest.param(set(), 0, id="no-docker-says-nothing"),
    ],
)
def test_a_docker_on_its_nftables_firewall_is_left_alone_and_the_operator_told_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    docker_tables: set[tuple[str, str]],
    told: int,
) -> None:
    """Docker's nftables firewall keeps no DOCKER-USER chain and drops only
    what heads for its own bridges: nothing is added, the node starts, and
    the operator reads once (not at every heartbeat) what to accept should a
    link still be cut off."""
    docker = hf.DockerFilter()
    monkeypatch.setattr(hf, "_FILTERS", {"docker": docker})

    def host(argv: Sequence[str]) -> int:
        if argv[0] == "nft":
            return 0 if tuple(argv[3:]) in docker_tables else 1
        return 1  # no iptables chain anywhere

    with caplog.at_level("INFO", logger=hf.__name__):
        for _ in range(3):
            assert hf.open_links(ON_A_BOX, ("vo",), host=hf.Host(run=host)) == ()

    assert [r.getMessage() for r in caplog.records].count(hf.DOCKER_NFT_ADVICE) == told
