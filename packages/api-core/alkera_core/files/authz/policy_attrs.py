"""The one builder of the ``attrs`` dict handed to ``enforce()``.

Two rules make this module small and worth having. First: **no role name
crosses this boundary** — the policy is a table over ``allowed_actions``, so the
day the platform engine replaces the ladder the policy body does not change.
Second: **ids only** — ``name``, ``path`` and ``symlink_target`` are in
``NEVER_AUDITED_KEYS`` because a decision row is written for denied requests
too, and a denial that quoted the filename would answer the question the denial
exists to refuse.

``facts`` is an open bag on purpose: ABAC conditions the later engine evaluates
read from it, so adding a fact is a dict key rather than a signature change.
"""

from __future__ import annotations

from typing import Any

from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.decider import (
    SEAL_SELF_ONLY_BIT,
    AccessFacts,
    EffectiveAccess,
    NodeFlag,
)
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode


def policy_attrs(
    access: EffectiveAccess,
    node: FileNode,
    drive: FileDrive,
    ctx: ActingContext,
    facts: AccessFacts,
    *,
    grant_within_rung: bool = True,
) -> dict[str, object]:
    """The attributes the ``files.access`` policy decides over.

    ``grant_within_rung`` is the share ceiling, resolved by the caller from the
    ladder: whether the rung being granted or withdrawn is at or below the one
    the caller holds. ``True`` for every action that grants nothing.
    """
    bag: dict[str, Any] = {
        "node_id": str(node.id),
        "drive_id": str(drive.id),
        "trust": node.trust,
        "state": node.state,
        "kind": node.kind,
        "traversal_only": node.traversal_only,
        "leased_subtree": (str(facts.leased_subtree) if facts.leased_subtree is not None else None),
        "held_leases": sorted(str(root) for root in facts.held_leases),
    }
    bag.update(facts.extra)
    return {
        "org_id": str(node.org_team_id),
        "allowed_actions": access.allowed_actions,
        "flags": access.flags,
        "is_agent": facts.is_agent,
        "agent_machine_verified": facts.agent_machine_id is not None,
        "chat_subtree": access.in_chat_subtree,
        "chat_bound_elsewhere": access.chat_bound_elsewhere,
        "workspace_subtree": access.in_workspace_subtree,
        "workspace_bound_elsewhere": access.workspace_bound_elsewhere,
        # The one writer a chat's record admits, resolved by the decider from
        # the leases the route verified: a fact about the caller AND the
        # chain, so the policy reads it rather than re-deriving it.
        "holds_lease": access.holds_lease,
        # What admits a box on its own credential: the node's chat or
        # workspace runs on its machine, or it holds the live lease over it.
        "machine_runs_it": access.machine_runs_it,
        "is_link": facts.is_link,
        "in_org": access.in_org,
        "drive_kind": drive.kind,
        "held": NodeFlag.HELD.value in access.flags,
        "locked": NodeFlag.LOCKED.value in access.flags,
        # A copy's two source facts. Read off the row rather than the access
        # result: the decider says what a caller may do, not where the node is
        # or how far its seal reaches.
        "trashed": node.trashed_at is not None,
        "seal_self_only": bool(node.flags & SEAL_SELF_ONLY_BIT),
        # A copy's two reach facts, which the decider resolves because both are
        # about the chain and the grants rather than about the row: whether
        # anything above this node forbids passing it on, and whether the only
        # thing reading it for this caller is a grant that runs out.
        "no_reshare_chain": access.no_reshare_chain,
        "read_via_conditional_grant": access.read_via_conditional_grant,
        "grant_within_rung": grant_within_rung,
        "facts": bag,
    }


__all__ = ["policy_attrs"]
