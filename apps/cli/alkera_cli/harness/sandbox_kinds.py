"""Sandboxes a box starts for a workspace, by kind, beside its chats' agents.

A chat's agent server is the one sandbox every box starts today
(``adapters/opencode_sandbox.plan_sandbox``). A workspace may need others of
its own, one of each kind for the whole workspace: a notebook kernel that
every member's chat talks to is the first. Each kind registers here
(:func:`register_workspace_sandbox`) and says only what is its own: the spec
its container is made from, and what runs in it. Everything a box must get
right for every kind is decided here, once:

* **who it runs as**: the workspace's tree identity, the uid that owns the
  workspace's root, so the sandbox reads and writes the files every member
  reads and writes, and no other uid gains anything;
* **what it is named after**: a container, slice, cgroup and network
  namespace of its own (:func:`workspace_container`), never a chat's and never
  another kind's, so a kernel and an agent in one workspace never share a
  veth or a scope;
* **whether the box can run it**: the runtime the box's mode selects, which
  refuses a gVisor box that cannot run ``runsc`` rather than downgrade.

Inside an org worker all of this already holds per org: the uids come from
the org's own ledger, and the namespaces sit inside the worker's.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, TypedDict

from alkera_cli.harness.sandbox import (
    SandboxLaunch,
    SandboxRefusedError,
    SandboxRuntime,
    SandboxSettings,
    SandboxSpec,
    ensure_chat_uid,
    select_runtime,
)
from alkera_cli.harness.sandbox_probe import SandboxCapability

#: The kind every chat's agent server is; reserved, since it is planned from
#: the chat's session (``plan_sandbox``), not registered here.
AGENT_KIND: Final = "agent"
_KIND_NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


@dataclass(frozen=True, slots=True)
class SandboxIdentity:
    """Who a sandbox runs as and what it is named after: the ``tree`` whose
    uid owns its root and runs its processes, the ``container`` its scope and
    network are named after, and the uids both resolve to (``net_uid`` only
    when the container is not the tree's own, so two containers under one
    identity never ask for one network slot)."""

    tree: str
    container: str
    uid: int
    net_uid: int | None


def sandbox_identity(
    tree: str, container: str, *, ensure: Callable[[str], int] = ensure_chat_uid
) -> SandboxIdentity:
    """The identity of a sandbox rooted at ``tree``'s files and named after
    ``container``. Every sandbox kind, the chat's agent included, takes its
    uids from here."""
    uid = ensure(tree)
    net_uid = ensure(container) if container != tree else None
    return SandboxIdentity(tree=tree, container=container, uid=uid, net_uid=net_uid)


class BoxTools(TypedDict):
    runsc: str
    setpriv: str
    uv: str
    ip: str
    nft: str
    unshare: str


def box_tools(cap: SandboxCapability) -> BoxTools:
    """The binaries the probe found, as a spec names them: the same for every
    kind of sandbox on this box."""
    return BoxTools(
        runsc=cap.runsc or "runsc",
        setpriv=cap.setpriv or "setpriv",
        uv=cap.uv or "uv",
        ip=cap.ip or "ip",
        nft=cap.nft or "nft",
        unshare=cap.unshare or "unshare",
    )


def workspace_container(workspace: str, kind: str) -> str:
    """The name a workspace's sandbox of ``kind`` is made under: one per
    workspace and kind, never a session id (which has no ``:``)."""
    if not workspace.strip():
        raise SandboxRefusedError("a workspace sandbox needs its workspace")
    return f"{workspace}:{kind}"


@dataclass(frozen=True, slots=True)
class WorkspaceSandboxRequest:
    """One workspace sandbox to start."""

    workspace: str
    """The workspace's custody key: the tree identity its members run as."""
    folder: Path
    """The workspace's root: the one writable tree, shared with its members."""
    state_dir: Path
    """A daemon-owned directory for this sandbox alone (its OCI bundle, its
    overlay, its own state), which the container never sees whole."""
    vcpu: int | None = None
    memory_mb: int | None = None
    options: Mapping[str, object] = field(default_factory=dict)
    """What the kind itself reads (a kernel's language, say); opaque here."""


class WorkspaceSandboxKind(Protocol):
    """One kind of workspace sandbox. ``spec`` composes the container from the
    identity and the box this module decided; ``argv`` is what runs in it."""

    @property
    def name(self) -> str: ...

    def spec(
        self,
        request: WorkspaceSandboxRequest,
        *,
        identity: SandboxIdentity,
        settings: SandboxSettings,
        cap: SandboxCapability,
    ) -> SandboxSpec: ...

    def argv(self, request: WorkspaceSandboxRequest, spec: SandboxSpec) -> tuple[str, ...]: ...


_LOCK = threading.Lock()
_KINDS: dict[str, WorkspaceSandboxKind] = {}


def register_workspace_sandbox(kind: WorkspaceSandboxKind) -> None:
    """Make ``kind`` startable on this box. A name is registered once."""
    if not _KIND_NAME.match(kind.name) or kind.name == AGENT_KIND:
        raise ValueError(f"{kind.name!r} cannot name a workspace sandbox kind")
    with _LOCK:
        if kind.name in _KINDS:
            raise ValueError(f"a workspace sandbox kind {kind.name!r} is already registered")
        _KINDS[kind.name] = kind


def unregister_workspace_sandbox(name: str) -> None:
    with _LOCK:
        _KINDS.pop(name, None)


def workspace_sandbox_kinds() -> tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_KINDS))


@dataclass(frozen=True, slots=True)
class WorkspaceSandboxPlan:
    kind: str
    runtime: SandboxRuntime
    spec: SandboxSpec
    argv: tuple[str, ...]

    def launch(self, env: Mapping[str, str]) -> SandboxLaunch:
        """The launch, composed once the sandbox's environment is known."""
        from dataclasses import replace

        return self.runtime.compose_launch(replace(self.spec, agent_env=dict(env)), self.argv)


def plan_workspace_sandbox(
    kind: str,
    request: WorkspaceSandboxRequest,
    *,
    settings: SandboxSettings,
    cap: SandboxCapability,
    ensure: Callable[[str], int] = ensure_chat_uid,
) -> WorkspaceSandboxPlan:
    """The plan for ``request``'s sandbox of ``kind``. Raises
    :class:`~alkera_cli.harness.sandbox.SandboxRefusedError` for a kind this
    box does not know, or a box that cannot run the mode it is set to."""
    with _LOCK:
        chosen = _KINDS.get(kind)
    if chosen is None:
        raise SandboxRefusedError(f"this box starts no workspace sandbox of kind {kind!r}")
    runtime = select_runtime(settings.mode, gvisor_ready=cap.gvisor)
    identity = sandbox_identity(
        request.workspace, workspace_container(request.workspace, kind), ensure=ensure
    )
    spec = chosen.spec(request, identity=identity, settings=settings, cap=cap)
    if (spec.chat_id, spec.uid, spec.net_uid) != (
        identity.container,
        identity.uid,
        identity.net_uid,
    ):
        raise SandboxRefusedError(
            f"the {kind} sandbox kind composed a spec that is not its own identity's"
        )
    return WorkspaceSandboxPlan(kind, runtime, spec, chosen.argv(request, spec))


__all__ = [
    "AGENT_KIND",
    "BoxTools",
    "SandboxIdentity",
    "WorkspaceSandboxKind",
    "WorkspaceSandboxPlan",
    "WorkspaceSandboxRequest",
    "box_tools",
    "plan_workspace_sandbox",
    "register_workspace_sandbox",
    "sandbox_identity",
    "unregister_workspace_sandbox",
    "workspace_container",
    "workspace_sandbox_kinds",
]
