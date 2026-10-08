"""What the UI, the CLI and the façades see: capabilities, never a role.

A role name is an internal fact about the ladder and it stays inside this
package. Everything outside asks "may I write this?" and gets a boolean plus,
when the answer is no, the reason — so a share dialog can say "this folder is
held" instead of a bare greyed-out button.

The capability map is built by iterating :class:`FilesAction`, so an action
added to the enum and the ladder appears here with no edit.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.files.authz.actions import READ_ONLY_ACTIONS, FilesAction
from alkera_core.files.authz.decider import EffectiveAccess, NodeFlag

#: Why an action was refused. Ordered narrowest-cause-first when several apply.
REASON_NOT_IN_ORG = "not_in_org"
REASON_NO_ROLE = "no_role"
REASON_INSUFFICIENT_ROLE = "insufficient_role"
REASON_AGENT = "agent_confined"

_CAMEL: Mapping[FilesAction, str] = {
    action: "can" + "".join(part.capitalize() for part in action.value.split("_"))
    for action in FilesAction
}


class Capabilities:
    """The per-action answer for one caller on one node."""

    __slots__ = ("_allowed", "_refusals")

    def __init__(self, allowed: frozenset[str], refusals: Mapping[str, str]) -> None:
        self._allowed = allowed
        self._refusals = dict(refusals)

    def can(self, action: FilesAction) -> bool:
        return action.value in self._allowed

    @property
    def refusals(self) -> Mapping[str, str]:
        """Action name → why it is refused, for every action that is."""
        return dict(self._refusals)

    def as_dict(self) -> dict[str, bool]:
        """The wire shape: ``{"canRead": True, "canWrite": False, …}``."""
        return {_CAMEL[action]: self.can(action) for action in FilesAction}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Capabilities):
            return NotImplemented
        return self._allowed == other._allowed and self._refusals == other._refusals

    def __hash__(self) -> int:
        return hash((self._allowed, tuple(sorted(self._refusals.items()))))

    def __repr__(self) -> str:
        return f"Capabilities(allowed={sorted(self._allowed)!r})"


def _refusal(action: FilesAction, access: EffectiveAccess) -> str:
    """Why ``action`` is refused — the flag that removed it when one did, so the
    UI can name the cause, and the role otherwise."""
    if not access.in_org:
        return REASON_NOT_IN_ORG
    flags = access.flags
    if NodeFlag.FROZEN.value in flags and action not in READ_ONLY_ACTIONS:
        return NodeFlag.FROZEN.value
    if NodeFlag.NO_DOWNLOAD.value in flags and action is FilesAction.EXPORT:
        return NodeFlag.NO_DOWNLOAD.value
    if NodeFlag.NO_RESHARE.value in flags and action is FilesAction.SHARE:
        return NodeFlag.NO_RESHARE.value
    if NodeFlag.HELD.value in flags and action in (FilesAction.WRITE, FilesAction.DELETE):
        return NodeFlag.HELD.value
    if NodeFlag.LOCKED.value in flags and action is FilesAction.WRITE:
        return NodeFlag.LOCKED.value
    if access.role is None:
        return REASON_NO_ROLE
    return REASON_INSUFFICIENT_ROLE


def capabilities(access: EffectiveAccess) -> Capabilities:
    """The capability map for ``access``.

    Takes only the access result: everything a refusal reason needs — the flags
    in force, whether the caller is in the org at all — is already on it, so
    there is no second source of truth to drift.
    """
    refusals = {
        action.value: _refusal(action, access)
        for action in FilesAction
        if action.value not in access.allowed_actions
    }
    return Capabilities(allowed=access.allowed_actions, refusals=refusals)


__all__ = [
    "REASON_AGENT",
    "REASON_INSUFFICIENT_ROLE",
    "REASON_NOT_IN_ORG",
    "REASON_NO_ROLE",
    "Capabilities",
    "capabilities",
]
