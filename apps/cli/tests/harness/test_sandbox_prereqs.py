"""The box prerequisite script carries the rules the launcher relies on. CI has
no root, so the script's shape is what is pinned: it installs runsc and stages
the digest-verified rootfs for a gVisor node, installs uv, micromamba and the
managed Python on every node, keeps the uid tools and the metadata firewall for
both modes, loads the chat-network rules the launch's per-chat steps depend on,
and writes the two-state ``ALKERA_SANDBOX_MODE`` environment. bash parses it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.supervisor import slots

# The script is rendered once per module; every case reads that one rendering.
pytestmark = pytest.mark.xdist_group("sandbox_prereqs")

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "ops" / "box" / "sandbox-prereqs.sh"
API_CORE_COPY = (
    REPO_ROOT / "packages" / "api-core" / "alkera_core" / "compute" / "sandbox_prereqs.sh"
)


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def rendered(script: str) -> str:
    """The script with its own shell variables for the uid range, the metadata
    addresses and the chat block substituted, so the rule lines can be compared
    to the launcher's text byte for byte."""
    values = dict(re.findall(r"^([A-Z_0-9]+)=([0-9a-z.:/]+)$", script, flags=re.MULTILINE))
    # NAME="${ENV:-default}" carries its default the same way.
    values.update(
        re.findall(r'^([A-Z_0-9]+)="\$\{[A-Z_0-9]+:-([^}"]+)\}"$', script, flags=re.MULTILINE)
    )
    out = script
    for name, value in values.items():
        out = out.replace("${" + name + "}", value)
    return out


def _section(script: str, start: str, end: str) -> str:
    return script[script.index(start) : script.index(end)]


def test_the_script_is_executable_and_strict(script: str) -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert 'if [ "$(id -u)" -ne 0 ]' in script


def test_the_two_prereqs_copies_are_byte_identical() -> None:
    """The backend embeds its own copy in the bootstrap; a shape test pins them
    together so a change to one is a change to both."""
    assert API_CORE_COPY.read_bytes() == SCRIPT.read_bytes()


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs a POSIX bash; the Windows runner's bash is the WSL launcher with no distribution",
)
def test_bash_parses_the_script() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# -- the firewall ------------------------------------------------------------------


def test_the_metadata_block_rules_are_the_launchers_text(script: str) -> None:
    body = rendered(script)
    for rule in sb.metadata_block_rules():
        assert rule in body, rule
    assert f"CHAT_UID_MIN={sb.UID_MIN}\n" in script
    assert f"CHAT_UID_MAX={sb.UID_MAX}\n" in script
    assert f"METADATA_IPV4={sb.METADATA_IPV4}\n" in script
    assert f"METADATA_IPV6={sb.METADATA_IPV6}\n" in script


def test_the_chat_network_rules_are_the_launchers_text_in_the_launchers_order(script: str) -> None:
    """The per-chat steps only add an element to ``chat_ports``; everything else
    a chat's namespace may or may not reach is this ruleset, so its lines are
    pinned to the launcher's, in order."""
    body = rendered(script)
    rules = sb.chat_network_rules()
    positions = [body.index(rule) for rule in rules]
    assert positions == sorted(positions), rules
    assert f'SANDBOX_NET="${{ALKERA_SANDBOX_NET:-{sb.DEFAULT_CHAT_NET}}}"' in script
    assert "type ifname . inet_service" in _section(script, "set chat_ports {", "chain output {")


def test_the_metadata_rules_sit_in_an_output_chain_that_otherwise_accepts(script: str) -> None:
    output = _section(script, "chain output {", "chain input {")
    assert "type filter hook output priority filter; policy accept;" in output
    assert "meta skuid" in output and "counter drop" in output


def test_an_owned_host_drops_new_inbound_except_loopback_established_icmp_and_the_named_ports(
    script: str,
) -> None:
    """The lockdown is its own chain, which the input chain jumps to only on a
    host the plane owns; on a shared host nothing but the node's links is
    filtered (see the rendered rulesets below)."""
    inbound = _section(script, "chain input {", "chain host_inbound {")
    assert "type filter hook input priority filter; policy accept;" in inbound
    assert 'iif "lo" accept' in inbound
    assert "ct state established,related accept" in inbound
    assert inbound.rstrip().rstrip("}").rstrip().endswith("${host_inbound}")
    lockdown = _section(script, "chain host_inbound {", "chain forward {")
    lockdown = lockdown[: lockdown.rindex("}")]
    assert "ct state invalid drop" in lockdown
    assert "ip protocol icmp accept" in lockdown and "ip6 nexthdr ipv6-icmp accept" in lockdown
    assert "tcp dport { ${ports} } accept" in lockdown
    assert lockdown.rstrip().endswith("counter drop")


def test_a_chat_reaches_the_host_only_on_its_granted_ports_and_never_ssh(script: str) -> None:
    """The veth rules come before the box's own inbound ports: a chat's host end
    is admitted to the (interface, port) pairs the launch granted and dropped
    otherwise — so the ssh port the box admits from the world is not admitted
    from a chat."""
    inbound = _section(script, "chain input {", "chain forward {")
    grant = inbound.index("iifname . tcp dport @chat_ports accept")
    drop = inbound.index('iifname "vc*" counter drop')
    ssh = inbound.index("tcp dport { ${ports} } accept")
    assert inbound.index("ct state established,related accept") < grant < drop < ssh


def test_the_forward_path_drops_metadata_and_other_chats_and_nats_the_rest_out(
    script: str,
) -> None:
    forward = _section(script, "chain forward {", "chain postrouting {")
    assert "type filter hook forward priority filter; policy accept;" in forward
    metadata = forward.index('iifname "vc*" ip daddr ${METADATA_IPV4} counter drop')
    peers = forward.index('iifname "vc*" ip daddr ${SANDBOX_NET} counter drop')
    out = forward.index('iifname "vc*" oifname != "vc*" accept')
    rest = forward.index('iifname "vc*" counter drop')
    inbound = forward.index('oifname "vc*" counter drop')
    assert metadata < peers < out < rest < inbound
    nat = script[script.index("chain postrouting {") :]
    assert "type nat hook postrouting priority srcnat; policy accept;" in nat
    assert 'ip saddr ${SANDBOX_NET} oifname != "vc*" masquerade' in nat


def test_an_org_worker_reaches_nothing_on_the_host(script: str) -> None:
    """An org worker's link ends on the host, and its egress in production is
    the API and the gateway, both off the box: the host admits nothing from a
    worker's link, before the box's own public ports are admitted, and the
    only earlier accepts are loopback, replies and a chat's granted pairs."""
    inbound = _section(script, "chain input {", "chain forward {")
    drop = inbound.index('iifname "vo*" counter drop')
    public = inbound.index("tcp dport { ${ports} } accept")
    icmp = inbound.index("ip protocol icmp accept")
    assert drop < icmp < public
    accepts = [
        line.strip() for line in inbound[:drop].splitlines() if line.strip().endswith("accept")
    ]
    assert accepts == [
        'iif "lo" accept',
        "ct state established,related accept",
        "iifname . tcp dport @chat_ports accept",
    ]
    assert inbound.count('"vo*"') == 1


def test_an_org_worker_goes_out_through_nat_and_never_to_metadata_or_a_peer(
    script: str,
) -> None:
    forward = _section(script, "chain forward {", "chain postrouting {")
    metadata = forward.index('iifname "vo*" ip daddr ${METADATA_IPV4} counter drop')
    metadata6 = forward.index('iifname "vo*" ip6 daddr ${METADATA_IPV6} counter drop')
    orgs = forward.index('iifname "vo*" ip daddr ${ORG_NET} counter drop')
    chats = forward.index('iifname "vo*" ip daddr ${SANDBOX_NET} counter drop')
    out = forward.index('iifname "vo*" oifname != "vo*" accept')
    assert max(metadata, metadata6, orgs, chats) < out
    nat = script[script.index("chain postrouting {") :]
    assert 'ip saddr ${ORG_NET} oifname != "vo*" masquerade' in nat


def test_forwarding_is_turned_on_and_persisted(script: str) -> None:
    assert "net.ipv4.ip_forward = 1" in script
    assert 'sysctl -q -p "$SYSCTL_FILE"' in script
    assert "SYSCTL_FILE=/etc/sysctl.d/90-alkera-sandbox.conf" in script


def test_ssh_is_always_admitted_whatever_its_port(script: str) -> None:
    assert 'INBOUND_TCP_PORTS="${SANDBOX_INBOUND_TCP_PORTS:-22}"' in script
    assert "sshd -T" in script and 'ports="$ports,$ssh_port"' in script


def test_the_ruleset_is_replaced_whole_on_a_rerun_and_loaded_at_boot(script: str) -> None:
    assert "table inet alkera_sandbox\nflush table inet alkera_sandbox\n" in script
    assert 'nft -f "$NFT_FILE"' in script
    assert '! grep -qF "$NFT_FILE" /etc/nftables.conf' in script
    assert "systemctl enable nftables" in script


# -- the tools -------------------------------------------------------------------


def test_the_uid_and_network_tools_are_installed_on_both_families(script: str) -> None:
    # The uid tools (util-linux/shadow-utils), acl, iproute2 and nftables —
    # needed in BOTH modes for the per-chat uid, the chat network and the
    # metadata firewall.
    assert (
        "apt-get install -y --no-install-recommends uidmap nftables util-linux acl iproute2 bzip2"
        in script
    )
    assert "dnf install -y shadow-utils nftables util-linux acl iproute bzip2" in script
    # bubblewrap is gone: the old model's boundary, replaced by gVisor.
    assert "bubblewrap" not in script and "bwrap" not in script


def test_every_download_is_verified_before_it_is_used(script: str) -> None:
    """Each fetched artifact is checked: uv and micromamba against their
    release sidecars, the Ubuntu base against a pinned sha256, gVisor against
    its published sha512 (or installed from its signed apt repository)."""
    assert "verify_sha256()" in script and "sidecar_digest()" in script
    uv = _section(script, "if ! command -v uv", 'log "uv ')
    assert 'curl -fsSL --retry 5 "$UV_URL.sha256"' in uv
    assert 'verify_sha256 "$uv_tmp/uv.tar.gz"' in uv
    mm = _section(script, "if ! command -v micromamba", 'log "micromamba ')
    assert 'curl -fsSL --retry 5 "$MM_URL.sha256"' in mm
    assert 'verify_sha256 "$mm_tmp/micromamba"' in mm
    assert re.search(r"^ROOTFS_BASE_SHA256_AMD64=[0-9a-f]{64}$", script, flags=re.MULTILINE)
    assert re.search(r"^ROOTFS_BASE_SHA256_ARM64=[0-9a-f]{64}$", script, flags=re.MULTILINE)
    assert 'verify_sha256 "$base_tar" "$ROOTFS_BASE_SHA256"' in script
    # A tarball that fails its check is never extracted: the check comes first.
    assert script.index('verify_sha256 "$base_tar"') < script.index('tar -xzf "$base_tar"')


def test_the_managed_python_uv_and_micromamba_are_installed_on_every_node(script: str) -> None:
    assert 'PYTHON_HOME="$PYTHON_ROOT/current"' in script
    assert 'PYTHON_ROOT="$TOOLS_ROOT/python"' in script and "TOOLS_ROOT=/opt/alkera" in script
    assert f"{sb.DEFAULT_PYTHON_HOME}" == "/opt/alkera/python/current"
    assert 'UV_PYTHON_INSTALL_DIR="$PYTHON_ROOT" uv python install "$PYTHON_VERSION"' in script
    assert 'PYTHON_VERSION="${ALKERA_SANDBOX_PYTHON_VERSION:-3.12}"' in script
    assert 'ln -sfn "$(dirname "$(dirname "$found")")" "$PYTHON_HOME"' in script
    assert (
        'install -m 0755 -o root -g root "$mm_tmp/micromamba" /usr/local/bin/micromamba' in script
    )
    # Installed before the mode split: a "none" box needs them for the default
    # environment as much as a gvisor box does.
    assert script.index("uv python install") < script.index(
        'if [ "$SANDBOX_MODE" = "gvisor" ]; then'
    )
    # The mount point a "none" box binds a chat's folder at.
    assert 'mkdir -p "$SANDBOX_HOME"' in script


def test_every_tree_the_chat_uid_traverses_is_opened_after_its_mkdir(script: str) -> None:
    """The bootstrap runs the script under ``umask 077``; a bare ``mkdir`` there
    leaves ``/opt/alkera`` (and the rootfs's copy of it) ``0700``, and the chat's
    uid cannot reach the Python below it — no shell tool finds ``python3``."""
    host_mkdir = script.index('mkdir -p "$TOOLS_ROOT" "$PYTHON_ROOT"')
    host_open = script.index('chmod 0755 "$TOOLS_ROOT" "$PYTHON_ROOT"')
    assert host_mkdir < host_open < script.index("uv python install")
    section = _section(script, "# --- the rootfs", "# --- the parent cgroup slice")
    trees = '"$work/opt/alkera" "$work/home/alkera" "$work/run/alkera"'
    rootfs_mkdir = section.index(f"mkdir -p {trees}")
    rootfs_open = section.index(f"chmod 0755 {trees}")
    python_copied = section.index('cp -a "$PYTHON_ROOT" "$work/opt/alkera/python"')
    assert rootfs_mkdir < rootfs_open < python_copied
    assert 'chmod 0755 "$SANDBOX_HOME"' in script


def test_gvisor_runsc_is_installed_only_for_a_gvisor_node(script: str) -> None:
    assert 'if [ "$SANDBOX_MODE" = "gvisor" ]; then' in script
    section = _section(script, "# --- gVisor (runsc)", "# --- the rootfs")
    # The signed apt repository where there is apt...
    assert "https://gvisor.dev/archive.key" in section
    assert "signed-by=/etc/apt/keyrings/gvisor-archive-keyring.gpg" in section
    assert "https://storage.googleapis.com/gvisor/releases release main" in section
    assert "apt-get install -y --no-install-recommends runsc" in section
    # ...and the release tarball, checksum-verified, elsewhere. The bare
    # ``runsc`` object the old script fetched no longer exists in the bucket.
    assert "gvisor/releases/release" in section
    assert 'curl -fsSL --retry 5 "${RUNSC_URL}/gvisor.tar.bz2"' in section
    assert "sha512sum -c gvisor.tar.bz2.sha512" in section
    assert '"${RUNSC_URL}/runsc"' not in section
    assert "install -m 0755" in section and "runsc" in section


def test_the_rootfs_is_staged_once_per_recipe_verified_and_never_used_half_built(
    script: str,
) -> None:
    section = _section(script, "# --- the rootfs", "# --- the parent cgroup slice")
    assert 'ROOTFS_ROOT="$TOOLS_ROOT/rootfs"' in script
    assert 'ROOTFS_CURRENT="$ROOTFS_ROOT/current"' in script
    assert f"ROOTFS_STAMP={sb.ROOTFS_STAMP}" in script
    assert sb.DEFAULT_ROOTFS == "/opt/alkera/rootfs/current"
    # The recipe digest names the build from every input.
    for token in ("$ROOTFS_RECIPE", "$ROOTFS_BASE_SHA256", "$ROOTFS_PACKAGES", "$python_build"):
        assert token in section, token
    assert 'target="$ROOTFS_ROOT/$recipe_digest"' in section
    # Reused when the stamp matches; rebuilt beside the old one otherwise.
    assert '[ "$(cat "$target/$ROOTFS_STAMP")" = "$recipe_digest" ]' in section
    assert 'mktemp -d -p "$ROOTFS_ROOT" .build.XXXXXX' in section
    # The stamp is written last, before the atomic move and the pointer flip.
    stamp = section.index('printf \'%s\\n\' "$recipe_digest" >"$work/$ROOTFS_STAMP"')
    move = section.index('mv "$work" "$target"')
    flip = section.index('mv -T "$ROOTFS_CURRENT.tmp" "$ROOTFS_CURRENT"')
    assert section.index("chroot") < stamp < move < flip
    # The host's Python, uv and micromamba go in at the same paths.
    assert 'cp -a "$PYTHON_ROOT" "$work/opt/alkera/python"' in section
    assert "/usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/micromamba" in section
    assert '"$work/home/alkera"' in section
    # A failed build leaves nothing mounted or half-made.
    assert "trap cleanup_rootfs_build EXIT" in section


def test_the_parent_slice_is_made_under_systemd_or_cgroupfs(script: str) -> None:
    assert "systemctl set-property alkera.slice" in script
    assert "mkdir -p /sys/fs/cgroup/alkera.slice" in script
    assert "cgroup.subtree_control" in script


def test_the_bwrap_user_namespace_scaffolding_is_gone(script: str) -> None:
    # gVisor's runsc (as root) needs no unprivileged user namespaces, so the
    # old apparmor/userns sysctl block that only served bwrap is removed.
    for gone in (
        "kernel.unprivileged_userns_clone",
        "user.max_user_namespaces",
        "apparmor_restrict_unprivileged_userns",
    ):
        assert gone not in script, gone


def test_the_settings_file_names_the_mode_the_paths_and_the_defaults_and_is_written_once(
    script: str,
) -> None:
    assert 'if [ ! -f "$ENV_FILE" ]; then' in script
    for line in (
        "ALKERA_SANDBOX_MODE=${SANDBOX_MODE}",
        "ALKERA_SANDBOX_HOME=${SANDBOX_HOME}",
        "ALKERA_SANDBOX_ROOTFS=${ROOTFS_CURRENT}",
        "ALKERA_SANDBOX_PYTHON=${PYTHON_HOME}",
        "ALKERA_SANDBOX_NET=${SANDBOX_NET}",
        f"SANDBOX_POOL_VCPU={sb.DEFAULT_POOL_VCPU}",
        f"SANDBOX_POOL_MEMORY_MB={sb.DEFAULT_POOL_MEMORY_MB}",
        f"SANDBOX_DEDICATED_VCPU={sb.DEFAULT_DEDICATED_VCPU}",
        f"SANDBOX_DEDICATED_MEMORY_MB={sb.DEFAULT_DEDICATED_MEMORY_MB}",
    ):
        assert line in script, line
    # The old level settings are gone.
    assert "ALKERA_SANDBOX_LEVEL" not in script
    assert "ALKERA_SANDBOX_REQUIRED" not in script


def test_the_closing_check_reports_runsc_and_rootfs_readiness_for_a_gvisor_node(
    script: str,
) -> None:
    """The script ends by saying the mode and, for a gVisor node, whether runsc
    is runnable and a rootfs is staged — so the operator sees before the daemon
    does whether the box can actually sandbox."""
    closing = script[script.rindex("# --- the readiness check") :]
    assert "runsc --version" in closing
    assert '[ -f "$ROOTFS_CURRENT/$ROOTFS_STAMP" ]' in closing
    assert "refuses every chat until it is fixed" in closing


# -- the host firewall policy -------------------------------------------------


def test_the_host_firewall_is_required_unless_the_bootstrap_says_otherwise(script: str) -> None:
    """The default is the fail-closed one; only the compute bootstrap names the
    other value, and only for a none node in a container without CAP_NET_ADMIN."""
    assert 'HOST_FIREWALL="${ALKERA_SANDBOX_HOST_FIREWALL:-required}"' in script


def _run_network_steps(
    tmp_path: Path,
    script: str,
    *,
    mode: str,
    policy: str,
    nft_exit: int,
    sysctl_exit: int = 0,
    scope: str = "owned",
) -> subprocess.CompletedProcess[str]:
    """Run the forwarding and firewall steps under bash with ``nft`` and
    ``sysctl`` on shims, together with the helper that decides what a failure
    means, so the policy is exercised and not just read."""
    helpers = _section(script, "log() {", 'if [ "$(id -u)"')
    steps = _section(
        script, "# --- forwarding for the chat namespaces", "# --- the daemon's sandbox environment"
    ).replace("/etc/nftables.conf", str(tmp_path / "nftables.conf"))
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, code in (("nft", nft_exit), ("sysctl", sysctl_exit)):
        (shims / name).write_text(f"#!/bin/sh\nexit {code}\n")
        (shims / name).chmod(0o755)
    etc = tmp_path / "etc"
    prelude = "\n".join(
        [
            "set -euo pipefail",
            f"SANDBOX_MODE={mode}",
            f"HOST_FIREWALL={policy}",
            f"HOST_SCOPE={scope}",
            "CHAT_UID_MIN=200000",
            "CHAT_UID_MAX=200999",
            "METADATA_IPV4=169.254.169.254",
            "METADATA_IPV6=fd00:ec2::254",
            "SANDBOX_NET=10.200.0.0/14",
            "ORG_NET=10.204.0.0/16",
            "INBOUND_TCP_PORTS=22",
            f"ENV_DIR={etc}",
            'NFT_FILE="$ENV_DIR/sandbox.nft"',
            f"SYSCTL_FILE={etc}/90-alkera-sandbox.conf",
        ]
    )
    return subprocess.run(
        ["bash", "-c", f"{prelude}\n{helpers}\n{steps}"],
        env={"PATH": f"{shims}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs a POSIX bash; the Windows runner's bash is the WSL launcher with no distribution",
)
@pytest.mark.parametrize(
    ("mode", "policy", "nft_exit", "sysctl_exit", "exit_code", "says"),
    [
        pytest.param(
            "none", "optional", 1, 0, 0, "ruleset not applied", id="none-container-no-nft"
        ),
        pytest.param(
            "none", "optional", 0, 1, 0, "forwarding not applied", id="none-container-no-sysctl"
        ),
        pytest.param(
            "none", "optional", 0, 0, 0, "metadata blocked", id="none-container-loads-when-it-can"
        ),
        pytest.param(
            "none", "required", 1, 0, 1, "needs the host firewall", id="none-host-refuses"
        ),
        pytest.param("gvisor", "optional", 1, 0, 1, "never optional", id="gvisor-refuses-optional"),
        pytest.param("gvisor", "required", 1, 0, 1, "needs the host firewall", id="gvisor-refuses"),
        pytest.param("gvisor", "required", 0, 0, 0, "metadata blocked", id="gvisor-loads"),
    ],
)
def test_a_firewall_that_cannot_load_is_fatal_unless_declared_optional_for_a_none_container(
    tmp_path: Path,
    script: str,
    mode: str,
    policy: str,
    nft_exit: int,
    sysctl_exit: int,
    exit_code: int,
    says: str,
) -> None:
    done = _run_network_steps(
        tmp_path, script, mode=mode, policy=policy, nft_exit=nft_exit, sysctl_exit=sysctl_exit
    )
    assert done.returncode == exit_code, done.stdout + done.stderr
    assert says in done.stdout + done.stderr


# -- whose host it is -----------------------------------------------------------

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs a POSIX bash; the Windows runner's bash is the WSL launcher with no distribution",
)
#: What marks a rule as the node's own: one of its links, or a chat uid.
NODES_OWN = ('"vc*"', '"vo*"', "meta skuid")


def _written_ruleset(tmp_path: Path, script: str, scope: str) -> str:
    done = _run_network_steps(
        tmp_path, script, mode="gvisor", policy="required", nft_exit=0, scope=scope
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return (tmp_path / "etc" / "sandbox.nft").read_text(encoding="utf-8")


def _chain(ruleset: str, name: str) -> list[str]:
    """The rules of one chain, its hook declaration and comments left out."""
    body = ruleset[ruleset.index(f"  chain {name} {{\n") :]
    body = body[body.index("\n") + 1 : body.index("\n  }\n")]
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith(("type ", "#"))
    ]


def test_the_host_is_taken_as_owned_unless_the_bootstrap_says_shared(script: str) -> None:
    assert 'HOST_SCOPE="${ALKERA_SANDBOX_HOST_SCOPE:-owned}"' in script


@needs_bash
def test_a_shared_host_filters_only_the_nodes_own_links_and_uids(
    tmp_path: Path, script: str
) -> None:
    """On a host its operator also uses (Docker containers, services on their
    own ports), no chain drops by default and every drop names the node's own
    link or uid: what is not the node's passes as if the box were not there."""
    ruleset = _written_ruleset(tmp_path, script, "shared")
    assert "policy drop" not in ruleset
    assert "jump " not in ruleset
    for chain in ("output", "input", "forward"):
        drops = [rule for rule in _chain(ruleset, chain) if rule.endswith("drop")]
        assert drops, chain
        for rule in drops:
            assert any(own in rule for own in NODES_OWN), f"{chain}: {rule}"


@needs_bash
def test_an_owned_host_still_drops_everything_that_is_not_the_nodes(
    tmp_path: Path, script: str
) -> None:
    """A host the plane provisioned for the node alone keeps the lockdown: past
    the node's own rules, new inbound but ssh is dropped and nothing else is
    forwarded."""
    ruleset = _written_ruleset(tmp_path, script, "owned")
    assert _chain(ruleset, "input")[-1] == "jump host_inbound"
    assert _chain(ruleset, "forward")[-1] == "jump host_forward"
    assert _chain(ruleset, "host_inbound")[-2:] == ["tcp dport { 22 } accept", "counter drop"]
    assert _chain(ruleset, "host_forward") == ["counter drop"]


@needs_bash
def test_a_host_scope_that_is_neither_is_refused(script: str) -> None:
    check = _section(script, 'case "$HOST_SCOPE" in\n  owned|shared) ;;', 'if [ "$HOST_FIREWALL"')
    done = subprocess.run(
        ["bash", "-c", f"HOST_SCOPE=mine\n{check}"], capture_output=True, text=True
    )
    assert done.returncode == 1
    assert "must be owned or shared, not 'mine'" in done.stderr


# -- the org workers ----------------------------------------------------------------


def test_the_org_ranges_and_block_are_the_supervisor_s(script: str) -> None:
    assert f"ORG_UID_BASE={slots.ORG_UID_BASE}\n" in script
    assert f"ORG_UID_TOTAL={slots.MAX_SLOTS * slots.ORG_UID_SPAN}\n" in script
    assert f"ORG_NET={slots.ORG_NET}\n" in script


def test_an_org_link_reaches_nothing_on_the_host_and_nothing_of_another_org(script: str) -> None:
    """A worker's link goes out through NAT and nowhere else: not a host port
    (dropped before the named ports are accepted), not the metadata service,
    not another org's link, not a chat block."""
    body = rendered(script)
    prefix = f'"{slots.ORG_VETH_PREFIX}*"'
    inbound = _section(body, "chain input {", "chain forward {")
    assert inbound.index(f"iifname {prefix} counter drop") < inbound.index("tcp dport {")
    forward = _section(body, "chain forward {", "chain postrouting {")
    rules = [
        f"iifname {prefix} ip daddr {sb.METADATA_IPV4} counter drop",
        f"iifname {prefix} ip6 daddr {sb.METADATA_IPV6} counter drop",
        f"iifname {prefix} ip daddr {slots.ORG_NET} counter drop",
        f"iifname {prefix} ip daddr {sb.DEFAULT_CHAT_NET} counter drop",
        f"iifname {prefix} oifname != {prefix} accept",
    ]
    positions = [forward.index(rule) for rule in rules]
    assert positions == sorted(positions)
    assert f"ip saddr {slots.ORG_NET} oifname != {prefix} masquerade" in body


def test_the_supervisor_may_map_the_org_ranges_and_a_worker_reads_what_it_must(script: str) -> None:
    assert 'echo "root:${ORG_UID_BASE}:${ORG_UID_TOTAL}" >> "$ids"' in script
    assert 'chmod 0755 "$ENV_DIR"' in script
    assert 'chmod 0755 "$ROOTFS_ROOT"' in script


def test_every_mountpoint_a_chat_binds_onto_is_made_in_the_rootfs(script: str) -> None:
    """An org worker roots its chats at the shared rootfs read-only, so runsc
    cannot make a missing mountpoint there: the prerequisites make every one
    the launcher binds onto, from the same layout the launch mounts from."""
    made = _section(script, "# --- the chats' mountpoints", "# --- the parent cgroup slice")
    listed = set(re.findall(r"(/opt/alkera/[a-z/]+)", made))
    binds = sb.chat_binds(runtime_dir=Path("/r"), agent_config_root=Path("/c"), default_env=True)
    wanted = {b.destination for b in binds} | {sb.AGENT_MOUNT, sb.RIPGREP_MOUNT}
    assert wanted <= listed, wanted - listed


def test_the_quota_tools_are_installed_and_checked_for(script: str) -> None:
    packages = _section(script, "install_packages() {", "sandbox-prereqs: no apt-get or dnf")
    for tool in ("e2fsprogs", "quota"):
        assert packages.count(f" {tool}") == 2, tool
    assert "command -v setquota" in script and "command -v chattr" in script


@needs_bash
@pytest.mark.skipif(sys.platform == "win32", reason="checks POSIX mode bits")
def test_a_chat_uid_can_walk_to_every_mountpoint_even_under_the_bootstraps_umask(
    tmp_path: Path, script: str
) -> None:
    """The bootstrap runs the prerequisites under umask 077, and ``install -d``
    makes a missing parent under the umask: ``/opt/alkera/harness`` came out
    0700 root, so a chat's agent (another uid) could not reach its state below
    it and died with EACCES on its secrets file. Every directory from the
    rootfs down to each mountpoint is opened to be walked."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    made = _section(script, "# --- the chats' mountpoints", "# --- the parent cgroup slice")
    done = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\numask 077\nROOTFS_CURRENT={rootfs}\n{made}"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    mountpoints = re.findall(r"(/opt/alkera/[a-z/]+)", made)
    assert "/opt/alkera/harness/state" in mountpoints
    for mountpoint in mountpoints:
        path = rootfs
        for part in PurePosixPath(mountpoint).parts[1:]:
            path = path / part
            assert path.stat().st_mode & 0o777 == 0o755, path


def test_a_rootfs_built_before_the_mountpoints_were_opened_is_rebuilt(script: str) -> None:
    """Org workers keep a copy of the rootfs for as long as its stamp matches,
    and the stamp is the recipe's digest: a box whose copy was made while
    ``/opt/alkera/harness`` was 0700 only gets a working one when the recipe
    moves on."""
    assert int(re.findall(r"^ROOTFS_RECIPE=(\d+)$", script, flags=re.MULTILINE)[0]) >= 2
