"""The sandbox plan for an opencode session: what its agent server runs in.

The adapter decides WHETHER a session is sandboxed (a bounded session is, a
local one is not, a bounded session with no root is refused); this module
composes WHAT the sandbox is once it is: the runtime for the box's mode, the
chat's uid and limits, every tree the container sees and where
(:func:`~alkera_cli.harness.sandbox_layout.agent_binds`), the spec the launch
is made from, the container network the agent's config is written for, and
the probe that says what else runs in the sandbox once the agent server is
up. Pure beside the uid lookup, which creates the chat's user on first use.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.sandbox import (
    AGENT_DATA_SUBDIR,
    DEFAULT_ENV_NAME,
    SandboxNetwork,
    SandboxRefusedError,
    SandboxRuntime,
    SandboxSettings,
    SandboxSpec,
    ensure_chat_uid,
    select_runtime,
)
from alkera_cli.harness.sandbox_env import session_envs_dir
from alkera_cli.harness.sandbox_kinds import box_tools, sandbox_identity
from alkera_cli.harness.sandbox_layout import agent_binds, host_path
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_processes import SandboxProcessProbe, probe_for
from alkera_cli.harness.sandbox_scope import TOPOLOGY_ENV, register_scope, scope_for, scope_of
from alkera_cli.harness.sandbox_uid import member_identity, owner_session

logger = logging.getLogger(__name__)


def optional_int(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def at_host(url: str, host: str) -> str:
    """``url`` with its host replaced by ``host`` (the port kept)."""
    parts = urlsplit(url)
    if parts.port is None:
        return url
    return urlunsplit(parts._replace(netloc=f"{host}:{parts.port}"))


def mcp_ports(alkera_mcp: object) -> tuple[int, ...]:
    """The daemon ports the tool-server block points the agent at."""
    ports: list[int] = []
    if isinstance(alkera_mcp, Mapping):
        for entry in alkera_mcp.values():
            if not isinstance(entry, Mapping) or not isinstance(entry.get("url"), str):
                continue
            port = urlsplit(entry["url"]).port
            if port is not None and port not in ports:
                ports.append(port)
    return tuple(sorted(ports))


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    """The sandbox a session's agent server will run in, decided before its
    environment is built. Under gVisor the container has a network of its own,
    so the environment (the loopback tool server's address in the agent's
    config, the address the server binds) has to be composed for it, and the
    plan is what both are composed from. The spec carries no environment yet;
    the launch is composed from it once the environment exists."""

    runtime: SandboxRuntime
    spec: SandboxSpec
    capability: SandboxCapability
    """What the probe found on this host: the reason line a refusal or a
    failed start quotes, so the operator reads the box's verdict on itself."""

    def agent_path(self, host: Path) -> str:
        """``host`` spelled as the agent sees it inside this sandbox: the one
        spelling the environment, the config and the launch all use, so a path
        the daemon composes names what the agent can open."""
        return self.spec.agent_path(host)

    def data_home(self, harness_dir: Path) -> str:
        """opencode keeps its data in ``<XDG_DATA_HOME>/agent``, and that
        directory is the one a sandbox binds, so the variable names its parent
        as the agent sees it; the host directory itself where nothing moves,
        spelled for the Linux host like every other path the plan composes."""
        if not self.spec.relocates:
            return host_path(harness_dir)
        return str(PurePosixPath(self.agent_path(harness_dir / AGENT_DATA_SUBDIR)).parent)

    @property
    def network(self) -> SandboxNetwork | None:
        """The container's network under gVisor; ``None`` when the agent shares
        the daemon's (a ``none`` box)."""
        return self.spec.network if self.runtime.mode == "gvisor" else None

    @staticmethod
    def probe_of(plan: SandboxPlan | None, agent_pid: int) -> SandboxProcessProbe | None:
        """What reads the processes in ``plan``'s sandbox once its agent server
        runs as ``agent_pid``; ``None`` for no plan (an unsandboxed session) or
        a sandbox with nothing to read."""
        return None if plan is None else probe_for(plan.runtime.mode, plan.spec, agent_pid)


def agent_spelling(plan: SandboxPlan | None, host: Path) -> str:
    """``host`` as the agent will see it: through ``plan`` when the session
    has one, the host path itself otherwise."""
    return plan.agent_path(host) if plan is not None else str(host)


def agent_directory(plan: SandboxPlan | None, config: SessionConfig) -> str:
    """Where opencode runs: what its tools resolve a relative path against
    and the working directory its prompt names. A cloud chat points it at the
    chat's own folder (the only place it may write), so "create a file" lands
    inside the write fence. Spelled where the agent sees it: under gVisor the
    folder is mounted only at the sandbox's home, so its host path names
    nothing in the container and a write there cannot make its directory."""
    return agent_spelling(plan, config.cwd)


def plan_sandbox(
    *,
    config: SessionConfig,
    folder: Path,
    cap: SandboxCapability,
    settings: SandboxSettings,
    binary: ResolvedOpencodeBinary,
    harness_dir: Path,
    agent_config_root: Path,
) -> SandboxPlan:
    """The plan for a bounded session whose root is ``folder``.

    The box's mode picks the runtime (gVisor or the no-boundary uid+cgroup
    launch; a box set to ``gvisor`` that cannot run runsc refuses rather than
    downgrades), the chat's user is made or found, the chat row's own limits
    (``sandbox_vcpu`` / ``sandbox_memory_mb`` in ``harness_native``, the box
    default where absent) bound it, and the container's view is composed from
    the bind table. Raises :class:`~alkera_cli.harness.sandbox.SandboxRefusedError`
    for what the box cannot provide.
    """
    runtime = select_runtime(settings.mode, gvisor_ready=cap.gvisor)
    # A subagent child shares its parent's root: it runs as the parent's uid,
    # or its spawn would hand the parent's tree to the child.
    scope = scope_for(config.session_id, config.harness_native)
    if scope.shared:
        raise SandboxRefusedError(
            "this box was asked to run a workspace's chats in one container, which this build "
            f"cannot do; set {TOPOLOGY_ENV}=per_chat"
        )
    register_scope(scope)
    # The tree's identity owns the root and runs every process: the
    # workspace's for a member (so every member's processes read and write
    # every shared file), the parent's for a subagent child (which shares its
    # parent's root), the chat's own otherwise. A container that is not the
    # tree's own takes its network slot from its own user, so two containers
    # under one identity never ask for one veth.
    tree = (
        scope.tree
        if scope.member
        else scope_of(owner_session(config.session_id, config.parent_session_id)).tree
    )
    identity = sandbox_identity(tree, scope.container, ensure=ensure_chat_uid)
    # With no container, a member runs as its own uid and shares only the tree.
    uid, share_gid = member_identity(
        identity.uid,
        owner_session(config.session_id, config.parent_session_id),
        member=scope.member,
        mode=settings.mode,
        ensure=ensure_chat_uid,
    )
    vcpu, memory_mb = settings.limits_for(
        optional_int(config.harness_native.get("sandbox_vcpu")),
        optional_int(config.harness_native.get("sandbox_memory_mb")),
    )
    rg = binary.ripgrep_path
    # A member's environments are its workspace's, bound into every member's
    # container and the workspace's kernel sandbox; a chat's own otherwise.
    envs = session_envs_dir(config.session_id, harness_dir)
    spec = SandboxSpec(
        chat_id=identity.container,
        folder=folder,
        uid=uid,
        net_uid=identity.net_uid,
        share_gid=share_gid,
        vcpu=vcpu,
        memory_mb=memory_mb,
        home=settings.home,
        mode=settings.mode,
        cgroup=cap.cgroup,
        binds=agent_binds(
            runtime_dir=harness_dir,
            agent_config_root=agent_config_root,
            default_env=cap.default_env,
            binary_dir=binary.path.parent,
            ripgrep_dir=rg.parent if rg is not None else None,
            prefix_dirs=[Path(arg).parent for arg in binary.prefix_args if arg.startswith("/")],
            envs_dir=envs,
        ),
        # The chat's records beside its folder, and its agent root beside
        # every other chat's under ALKERA_HOME: the config tree is readable
        # by whoever can reach it (gVisor honours mode bits, not ACLs), so
        # the root that holds it is closed here, by the steps, not left to
        # the umask the daemon happened to start with.
        private_dirs=(config.chat_dir, agent_config_root),
        bundle=agent_config_root / "runsc",
        rootfs=Path(cap.rootfs) if cap.rootfs else None,
        # The container's root overlay lives beside the chat's records,
        # where the container never looks and the data volume has room.
        overlay_dir=config.chat_dir / ".overlay",
        default_env=envs / DEFAULT_ENV_NAME if cap.default_env else None,
        python_home=cap.python_home or settings.python_home,
        daemon_ports=mcp_ports(config.harness_native.get("alkera_mcp")),
        resolvers=cap.resolvers,
        chat_net=settings.chat_net,
        mount_alias=settings.mode == "none" and cap.mount_ns,
        **box_tools(cap),
    )
    if rg is not None:
        # ``rg`` goes on the container's PATH where the container sees it.
        spec = replace(spec, path_dirs=(spec.agent_path(rg.parent),))
    logger.info(
        "chat %s sandbox: mode=%s cgroup=%s uid=%d alias=%s default_env=%s (%s)",
        config.session_id,
        settings.mode,
        spec.cgroup,
        spec.uid,
        spec.mount_alias or settings.mode == "gvisor",
        spec.default_env is not None,
        cap.reason,
    )
    return SandboxPlan(runtime, spec, cap)


__all__ = [
    "SandboxPlan",
    "agent_directory",
    "agent_spelling",
    "at_host",
    "mcp_ports",
    "optional_int",
    "plan_sandbox",
]
