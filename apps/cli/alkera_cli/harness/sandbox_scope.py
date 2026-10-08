"""Whose sandbox a session runs in: its own, or its workspace's.

A chat on its own owns everything its sandbox is named after: the uid that
owns its root and runs its commands, and the container (or slice) its agent
server and commands share. A chat that is a member of a workspace shares the
workspace's root with every other member, so the uid that owns that root, the
TREE identity, is the workspace's: every process in every member's sandbox can
read and write every file. Where its agent server runs is the TOPOLOGY's
choice, set by :data:`TOPOLOGY_ENV`:

* ``shared``: one container per workspace, created once, with a small init
  as PID 1, and every member's agent server exec'd into it;
* ``per_chat``: each member keeps a container of its own (as a chat on its
  own does), rooted at the shared tree under the workspace's identity.

:data:`DEFAULT_TOPOLOGY` says which a box that names none runs.

The box's cloud layer names a member's workspace on the session's harness bag
(:data:`SCOPE_KEY`); the adapter reads it through :func:`scope_for` when it
plans the sandbox and registers the answer (:func:`register_scope`), and every
other part of the box that names a sandbox after a session (the commands run
on its behalf, the daemon's writes into the tree, the process probe) asks
:func:`scope_of` rather than assuming the session's id.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

#: The harness bag key a member chat's workspace custody key rides on.
SCOPE_KEY: Final = "sandbox_scope"
#: The setting that picks the topology for a workspace's chats.
TOPOLOGY_ENV: Final = "ALKERA_SANDBOX_TOPOLOGY"

Topology = Literal["shared", "per_chat"]
TOPOLOGIES: Final[tuple[Topology, ...]] = ("shared", "per_chat")


#: The topology a box that names none runs workspaces in.
DEFAULT_TOPOLOGY: Final[Topology] = "per_chat"


def topology_from_env(environ: Mapping[str, str] | None = None) -> Topology:
    """The topology this box runs workspaces in: what the deployment names,
    else :data:`DEFAULT_TOPOLOGY`. An unknown word is the default, never a
    refusal: the choice changes cost, not whether a chat is served."""
    raw = (environ if environ is not None else os.environ).get(TOPOLOGY_ENV, "").strip().lower()
    for topology in TOPOLOGIES:
        if raw == topology:
            return topology
    return DEFAULT_TOPOLOGY


@dataclass(frozen=True, slots=True)
class SandboxScope:
    """The names one session's sandbox is made from."""

    session_id: str
    #: Whose uid owns the root and runs every command: the workspace's custody
    #: key for a member, the session id for a chat on its own.
    tree: str
    #: Whose container, slice and network namespace the agent server runs in.
    container: str

    @property
    def member(self) -> bool:
        """Whether the session is a member of a workspace."""
        return self.tree != self.session_id

    @property
    def shared(self) -> bool:
        """Whether the agent server runs in a container other sessions share."""
        return self.container != self.session_id


def own_scope(session_id: str) -> SandboxScope:
    """A chat on its own: every name is the session's."""
    return SandboxScope(session_id=session_id, tree=session_id, container=session_id)


def scope_for(
    session_id: str, harness_native: Mapping[str, object], *, topology: Topology | None = None
) -> SandboxScope:
    """The scope a session's harness bag asks for. A bag that names no
    workspace (every chat on its own, every older manifest) is the session's
    own scope, exactly as before."""
    raw = harness_native.get(SCOPE_KEY)
    workspace = raw.strip() if isinstance(raw, str) else ""
    if not workspace or workspace == session_id:
        return own_scope(session_id)
    chosen = topology if topology is not None else topology_from_env()
    container = workspace if chosen == "shared" else session_id
    return SandboxScope(session_id=session_id, tree=workspace, container=container)


_LOCK = threading.Lock()
_SCOPES: dict[str, SandboxScope] = {}


def register_scope(scope: SandboxScope) -> None:
    """Record ``scope`` as the session's for as long as it runs on this box.
    A session's own scope is not recorded: it is what :func:`scope_of`
    answers for a session it knows nothing about."""
    with _LOCK:
        if scope.member:
            _SCOPES[scope.session_id] = scope
        else:
            _SCOPES.pop(scope.session_id, None)


def forget_scope(session_id: str) -> None:
    with _LOCK:
        _SCOPES.pop(session_id, None)


def scope_of(session_id: str) -> SandboxScope:
    """The scope the session runs in on this box: the one registered for it,
    else its own."""
    with _LOCK:
        return _SCOPES.get(session_id) or own_scope(session_id)


def members_of(tree: str) -> tuple[str, ...]:
    """The sessions running on this box under ``tree``'s identity."""
    with _LOCK:
        return tuple(sorted(s.session_id for s in _SCOPES.values() if s.tree == tree))


__all__ = [
    "SCOPE_KEY",
    "TOPOLOGIES",
    "TOPOLOGY_ENV",
    "SandboxScope",
    "Topology",
    "forget_scope",
    "members_of",
    "own_scope",
    "register_scope",
    "scope_for",
    "scope_of",
    "topology_from_env",
]
