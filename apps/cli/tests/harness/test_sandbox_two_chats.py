"""Two chats' launches, composed side by side on one box: each control the
launch composes — the uid, the cgroup, the network namespace and its addresses,
the ownership of the trees and the traverse grants — is the chat's own, and
names nothing of the other chat's.

The pool box runs chats of different orgs in one process. The live ownership
probe proves what the steps do to a real filesystem; these pin, as data, that
the two launches a box composes for two chats are disjoint in every control,
under both cgroup drivers and both runtimes, so a regression that shares a
slice, a namespace, a uid or a grant between chats fails here before it needs
a Linux root to be seen.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness import sandbox as sb

pytestmark = [pytest.mark.spread]

WORK = Path("/opt/alkera-work/.alkera/chats")
HOME = Path("/root/.alkera/harness")
ROOTFS = Path("/opt/alkera/rootfs/current")
AGENT_ARGV = ("/opt/agent/alkera-agent", "serve")
DAEMON_PORT = 41234


def chat_spec(
    tag: str,
    uid: int,
    *,
    mode: sb.SandboxMode = "gvisor",
    cgroup: sb.CgroupDriver = "systemd",
) -> sb.SandboxSpec:
    """A chat's spec as the box lays it out: its records and folder under the
    work root, its config and state under the daemon's home, both parents
    shared with every other chat on the box."""
    chat_dir = WORK / tag
    config_root = HOME / tag
    return sb.SandboxSpec(
        chat_id=tag,
        folder=chat_dir / "sandbox",
        uid=uid,
        vcpu=1,
        memory_mb=1024,
        home="/home/alkera",
        mode=mode,
        cgroup=cgroup,
        binds=sb.chat_binds(
            runtime_dir=chat_dir / sb.RUNTIME_STATE_SUBDIR,
            agent_config_root=config_root,
            default_env=False,
        ),
        private_dirs=(chat_dir,),
        bundle=config_root / "runsc",
        rootfs=ROOTFS,
        overlay_dir=chat_dir / ".overlay",
        daemon_ports=(DAEMON_PORT,),
        resolvers=("172.31.0.2",),
    )


A = chat_spec("chat-a-0f3c2e1d9b8a7654", sb.UID_MIN + 17)
B = chat_spec("chat-b-9a8b7c6d5e4f3210", sb.UID_MIN + 18)


def _launch(spec: sb.SandboxSpec) -> sb.SandboxLaunch:
    if spec.mode == "gvisor":
        return sb.GvisorRuntime().compose_launch(spec, AGENT_ARGV)
    return sb.NoneRuntime().compose_launch(spec)


def _argv_tokens(launch: sb.SandboxLaunch) -> list[str]:
    tokens: list[str] = [*launch.prefix, *(launch.command or ()), *launch.tail]
    for step in (*launch.before, *launch.after_exit):
        if isinstance(step, sb.ShellStep):
            tokens.extend(step.argv)
        elif isinstance(step, sb.RepairStep):
            tokens.append(sb.host_path(step.tree))
        else:
            tokens.extend((sb.host_path(step.path), step.content))
    return tokens


def _mentions(tokens: Sequence[str], needle: str) -> bool:
    return any(needle in token for token in tokens)


def _specs(mode: sb.SandboxMode, cgroup: sb.CgroupDriver) -> tuple[sb.SandboxSpec, sb.SandboxSpec]:
    return (
        chat_spec(A.chat_id, A.uid, mode=mode, cgroup=cgroup),
        chat_spec(B.chat_id, B.uid, mode=mode, cgroup=cgroup),
    )


BOTH_RUNTIMES = pytest.mark.parametrize("mode", ["none", "gvisor"])
BOTH_DRIVERS = pytest.mark.parametrize("cgroup", ["systemd", "cgroupfs"])


# --- identity: uid, slice, container, namespace ------------------------------------


def test_the_names_a_launch_derives_from_the_chat_are_distinct_for_two_chats() -> None:
    for derive in (sb.chat_user, sb.chat_slice, sb.chat_container, sb.chat_netns):
        assert derive(A.chat_id) != derive(B.chat_id), derive.__name__
    assert sb.chat_cgroup_path(A.chat_id) != sb.chat_cgroup_path(B.chat_id)
    assert sb.chat_cgroup_path(A.chat_id).parent == sb.chat_cgroup_path(B.chat_id).parent


@BOTH_RUNTIMES
@BOTH_DRIVERS
def test_each_launch_drops_to_its_own_uid_and_never_names_the_others(
    mode: sb.SandboxMode, cgroup: sb.CgroupDriver
) -> None:
    a, b = _specs(mode, cgroup)
    for mine, theirs in ((a, b), (b, a)):
        tokens = _argv_tokens(_launch(mine))
        if mode == "gvisor":
            # The OCI config is the one JSON object among the tokens; the
            # ownership step's ``find ... -exec chmod {} +`` carries a bare ``{}``.
            oci = json.loads(next(t for t in tokens if t.startswith("{") and t != "{}"))
            assert oci["process"]["user"] == {"uid": mine.uid, "gid": mine.uid}
        else:
            assert f"--reuid={mine.uid}" in tokens and f"--regid={mine.uid}" in tokens
        # The other chat's uid appears in no ownership, grant or drop of this launch.
        assert not _mentions(tokens, f"{theirs.uid}:{theirs.uid}")
        assert not _mentions(tokens, f"u:{theirs.uid}:")
        assert f"--reuid={theirs.uid}" not in tokens
        assert not _mentions(tokens, f'"uid": {theirs.uid}')


@BOTH_RUNTIMES
@BOTH_DRIVERS
def test_each_launch_makes_and_removes_its_own_cgroup_only(
    mode: sb.SandboxMode, cgroup: sb.CgroupDriver
) -> None:
    a, b = _specs(mode, cgroup)
    for mine, theirs in ((a, b), (b, a)):
        launch = _launch(mine)
        made = _argv_tokens(launch)
        own = (
            sb.chat_slice(mine.chat_id)
            if cgroup == "systemd"
            else sb.host_path(sb.chat_cgroup_path(mine.chat_id))
        )
        other = (
            sb.chat_slice(theirs.chat_id)
            if cgroup == "systemd"
            else sb.host_path(sb.chat_cgroup_path(theirs.chat_id))
        )
        assert _mentions(made, own), "the launch names its own cgroup"
        assert not _mentions(made, other), "and never the other chat's"
        if cgroup == "cgroupfs":
            limits = {
                step.path.name: step.content
                for step in launch.before
                if isinstance(step, sb.WriteStep)
                and step.path.parent == sb.chat_cgroup_path(mine.chat_id)
            }
            assert {"memory.max", "cpu.max", "pids.max"} <= set(limits)
        # Teardown removes this chat's group; the other chat's outlives it.
        after = [
            t for step in launch.after_exit if isinstance(step, sb.ShellStep) for t in step.argv
        ]
        assert _mentions(after, own) and not _mentions(after, other)


# --- network: one /30, one namespace, one veth per chat ---------------------------


def test_two_chats_get_disjoint_networks_from_their_uids() -> None:
    net_a, net_b = A.network, B.network
    assert net_a.namespace != net_b.namespace
    assert net_a.host_if != net_b.host_if
    block_a = ipaddress.ip_network(f"{net_a.host_ip}/{net_a.prefix}", strict=False)
    block_b = ipaddress.ip_network(f"{net_b.host_ip}/{net_b.prefix}", strict=False)
    assert not block_a.overlaps(block_b)
    assert ipaddress.ip_address(net_b.container_ip) not in block_a
    assert ipaddress.ip_address(net_a.container_ip) not in block_b
    # The same chat id with the other chat's uid is another network entirely:
    # the address follows the uid, the namespace follows the chat.
    swapped = sb.plan_network(A.chat_id, B.uid)
    assert swapped.host_ip == net_b.host_ip and swapped.namespace == net_a.namespace


@BOTH_DRIVERS
def test_each_gvisor_launch_joins_its_own_namespace_and_grants_its_own_interface(
    cgroup: sb.CgroupDriver,
) -> None:
    a, b = _specs("gvisor", cgroup)
    for mine, theirs in ((a, b), (b, a)):
        launch = _launch(mine)
        assert launch.network == mine.network
        oci = sb.gvisor_oci_spec(mine, AGENT_ARGV)
        namespaces = {n["type"]: n for n in oci["linux"]["namespaces"]}
        assert namespaces["network"]["path"] == mine.network.namespace_path
        assert namespaces["network"]["path"] != theirs.network.namespace_path
        steps = [step.argv for step in launch.before if isinstance(step, sb.ShellStep)]
        made = [argv for argv in steps if argv[:3] == (mine.ip, "netns", "add")]
        assert made == [(mine.ip, "netns", "add", mine.network.namespace)]
        grants = [argv for argv in steps if argv[0] == mine.nft and argv[1] == "add"]
        assert grants == [sb._nft_element(mine, "add", mine.network.host_if, DAEMON_PORT)]
        assert not _mentions(_argv_tokens(launch), theirs.network.host_if)
        assert not _mentions(_argv_tokens(launch), theirs.network.namespace)
        assert not _mentions(_argv_tokens(launch), theirs.network.container_ip)


def test_the_box_ruleset_keeps_one_chats_block_from_the_others() -> None:
    """The per-chat networks are disjoint blocks inside one chat net, and the
    ruleset the box loads once drops anything a chat's interface sends toward
    that whole net — so no chat's address is reachable from another chat, and
    the grant set admits a chat's interface to the daemon's port alone."""
    rules = sb.chat_network_rules()
    chat_net = ipaddress.ip_network(sb.DEFAULT_CHAT_NET)
    assert f'iifname "{sb.VETH_PREFIX}*" ip daddr {chat_net} counter drop' in rules
    for net in (A.network, B.network):
        assert ipaddress.ip_address(net.container_ip) in chat_net
        assert net.host_if.startswith(sb.VETH_PREFIX)
    accept = rules.index(f"iifname . tcp dport @{sb.NFT_CHAT_PORTS_SET} accept")
    assert accept < rules.index(f'iifname "{sb.VETH_PREFIX}*" counter drop')


# --- the trees: owned, closed, traversable by one uid ------------------------------


def _shell(launch: sb.SandboxLaunch) -> list[tuple[str, ...]]:
    return [step.argv for step in launch.before if isinstance(step, sb.ShellStep)]


@BOTH_RUNTIMES
def test_each_launch_owns_and_closes_its_own_trees_and_grants_traverse_to_its_uid_alone(
    mode: sb.SandboxMode,
) -> None:
    a, b = _specs(mode, "systemd")
    for mine, theirs in ((a, b), (b, a)):
        steps = _shell(_launch(mine))
        # The shared trees by the reviewed repair, the rest walked every time:
        # between them, exactly this chat's owned trees, to this chat's uid.
        repairs = [s for s in _launch(mine).before if isinstance(s, sb.RepairStep)]
        assert {s.owner for s in repairs} == {TreeIdentity(mine.uid, mine.uid)}
        shared = [sb.host_path(s.tree) for s in repairs if s.when_wrong]
        every = [sb.host_path(s.tree) for s in repairs if not s.when_wrong]
        assert set(shared) | set(every) == {sb.host_path(p) for p in mine.folder_and_binds()}
        assert not set(shared) & set(every)
        private = next(s for s in steps if s[:2] == ("chmod", "go-rwx"))
        assert set(private[2:]) == {sb.host_path(p) for p in mine.private_dirs}
        grants = [s for s in steps if s[0] == "setfacl"]
        assert len(grants) == 1
        assert grants[0][1:3] == ("-m", f"u:{mine.uid}:x")
        granted = set(grants[0][3:])
        # Traverse on the shared parents and on this chat's own directories;
        # nothing under the other chat, and nothing of the other's uid.
        assert sb.host_path(WORK) in granted and sb.host_path(HOME) in granted
        assert not any(path.startswith(sb.host_path(theirs.private_dirs[0])) for path in granted)
        assert not any(path.startswith(sb.host_path(HOME / theirs.chat_id)) for path in granted)
        # No step of this launch touches a tree of the other chat.
        for argv in steps:
            for token in argv[1:]:
                assert not token.startswith(sb.host_path(theirs.private_dirs[0])), argv
                assert not token.startswith(sb.host_path(HOME / theirs.chat_id)), argv


def test_a_second_chats_launch_composes_the_same_ownership_steps_as_the_first() -> None:
    """What differs between two chats' ownership steps is the chat — its uid
    and its paths — and nothing else: substituting one chat for the other in
    the first chat's steps yields the second chat's steps exactly. A step that
    named a fixed path or uid for one chat would show up here."""
    steps_a = [" ".join(s.argv) for s in sb.uid_steps(A) if isinstance(s, sb.ShellStep)]
    steps_b = [" ".join(s.argv) for s in sb.uid_steps(B) if isinstance(s, sb.ShellStep)]
    substituted = [
        s.replace(A.chat_id, B.chat_id)
        .replace(sb.chat_slug(A.chat_id), sb.chat_slug(B.chat_id))
        .replace(str(A.uid), str(B.uid))
        for s in steps_a
    ]
    assert substituted == steps_b


def test_a_shell_for_one_chat_execs_into_that_chats_container_as_its_uid() -> None:
    """A command run on chat A's behalf enters A's container as A's uid and
    reads A's environment; nothing in it names B."""
    shell_a = sb.GvisorRuntime().compose_launch(sb.shell_spec(A))
    argv = shell_a.wrap(["/bin/sh", "-c", "id"], env={"PROBE": "x"})
    assert sb.chat_container(A.chat_id) in argv
    assert f"--user={A.uid}:{A.uid}" in argv
    assert sb.chat_container(B.chat_id) not in argv
    assert not _mentions(argv, str(B.uid))
