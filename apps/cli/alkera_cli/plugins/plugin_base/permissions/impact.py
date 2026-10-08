"""What a write reaches, resolved before it runs.

The gate turns a statement's parsed targets into graph URNs, classifies the
change against the frozen lineage graph, and folds the verdict together with
the knowledge filed against the affected assets. One typed object comes out:
the policy keys on it, the permission card renders it, the audit record keeps
it, and a refusal quotes it back to the agent.

A graph that cannot answer produces a DEGRADED assessment, never an empty one.
"Nothing breaks" and "nothing was read" are different sentences and only one of
them is safe to act on.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import ActionDescriptor

logger = logging.getLogger(__name__)

#: The taxonomy value a pre-taxonomy or untypeable edge carries. It stays
#: VISIBLE: an edge whose transformation is unknown is the reason a destructive
#: change escalates instead of resolving itself.
UNKNOWN_TRANSFORMATION = "unknown"

#: How many affected assets and knowledge bodies a refusal quotes before it
#: summarizes the rest. The reason is read by a model mid-turn, so it is a
#: paragraph, not a report.
_QUOTED_ASSETS = 5
_QUOTED_CONCEPTS = 2


class ImpactStatus(StrEnum):
    """Whether the affected set is the whole answer."""

    RESOLVED = "resolved"
    DEGRADED = "degraded"
    """Part of the read gave out (no graph, an unresolvable target, an
    unreadable KB), so the affected set may be short."""


class AffectedAsset(BaseModel):
    """One downstream asset the change reaches."""

    urn: str
    category: str = "non_breaking"
    """``breaking`` | ``potentially_breaking`` | ``non_breaking`` -- the
    classifier's ``ChangeCategory``, never the CI gate's severity words."""
    transformation: str = ""
    """The edge taxonomy value that carried the change here (``direct.identity``,
    ``indirect.filter``, ``unknown``)."""
    reason: str = ""


class BoundConcept(BaseModel):
    """A knowledge item filed against an affected asset -- what the gate quotes
    when it says why this asset matters."""

    urn: str
    item_id: str = ""
    title: str = ""
    meaning: str = ""
    owner_teams: list[str] = Field(default_factory=list)
    """The teams accountable for this concept, as the item carries them. Empty is
    the norm, and an unowned concept escalates nothing."""


class ImpactAssessment(BaseModel):
    """What one action changes, what that reaches, and who has said something
    about it. Serialized onto ``PermissionRequest.impact``."""

    status: ImpactStatus = ImpactStatus.DEGRADED
    category: str = "non_breaking"
    """The worst category across the affected set."""
    targets: list[str] = Field(default_factory=list)
    """The resolved URNs of what the action changes, at column grain when the
    statement named a column."""
    changes: dict[str, str] = Field(default_factory=dict)
    """``{target urn: ChangeKind}`` -- what was classified, so a reader can tell a
    drop from a logic change."""
    unresolved: list[str] = Field(default_factory=list)
    """Target names with no matching graph node."""
    affected: list[AffectedAsset] = Field(default_factory=list)
    model_rollup: dict[str, str] = Field(default_factory=dict)
    concepts: list[BoundConcept] = Field(default_factory=list)
    owner_teams: list[str] = Field(default_factory=list)
    """Every team owning something in the affected set, sorted."""
    unsure: bool = False
    """The policy cannot rule on this alone: an ambiguous target, an
    unknown-severity edge under a destructive change, or a confirmed owner
    outside the acting user's teams."""
    reason: str = ""
    """Why the read is degraded or unsure, in a sentence a person reads."""

    @property
    def has_impact(self) -> bool:
        return bool(self.affected)

    def asset_urns(self) -> list[str]:
        """Everything this decision is about: what changes plus what it reaches.
        The knowledge join runs over this set."""
        return list(dict.fromkeys([*self.targets, *(a.urn for a in self.affected)]))

    def broken_urns(self) -> list[str]:
        """What an intent has to name to cover this write. The target is not on the
        list: the agent wrote the statement, so declaring the thing it is changing
        proves nothing. Naming the assets it BREAKS is the acknowledgment."""
        return list(dict.fromkeys(a.urn for a in self.affected))


def degraded(reason: str) -> ImpactAssessment:
    """An assessment that read nothing. The policy falls back to effect tiers and
    the audit says it did."""
    return ImpactAssessment(status=ImpactStatus.DEGRADED, reason=reason)


# ---------------------------------------------------------------------------
# The agent-facing refusal
# ---------------------------------------------------------------------------

#: The two ways a refusal ends, named so a caller or a test reads the sentence
#: from here instead of copying it.
RETRY_WITH_INTENT_REASON = (
    "To proceed, retry with intent declaring the affected assets named above and why "
    "breaking them is right; naming a table covers its columns. Otherwise narrow the "
    "statement, or run lineage_impact and fix the consumers before this write."
)
INTENT_CANNOT_HELP_REASON = (
    "An intent declaration cannot change this outcome. Narrow the statement, or run "
    "lineage_impact and fix the consumers before this write."
)


def refusal_reason(
    assessment: ImpactAssessment, descriptor: ActionDescriptor, *, offer_intent: bool = True
) -> str:
    """The model-visible refusal: where it breaks, what governs it, and the ways
    forward. This text IS the retrieval mechanism, so it names assets and quotes
    the notes filed against them rather than saying "not approved". ``offer_intent``
    is False where no declaration can change the decision (a mode that refuses
    mutations, an explicit deny rule), so the agent is never sent to file one that
    cannot work."""
    action = descriptor.operation or str(descriptor.effect)
    changed = ", ".join(assessment.targets[:2]) or "this statement's target"
    lines = [
        f"Refused: {action} on {changed} reaches {len(assessment.affected)} downstream "
        f"asset(s), and the worst is {assessment.category.replace('_', ' ')}."
    ]
    paths = [
        f"{a.urn} ({a.category.replace('_', ' ')}, via {a.transformation})"
        for a in assessment.affected[:_QUOTED_ASSETS]
    ]
    if paths:
        more = len(assessment.affected) - len(paths)
        tail = f", and {more} more" if more > 0 else ""
        lines.append(f"Impact path: {'; '.join(paths)}{tail}.")
    for concept in assessment.concepts[:_QUOTED_CONCEPTS]:
        label = concept.title or concept.urn
        lines.append(f"Filed against {concept.urn}: {label}. {concept.meaning}")
    lines.append(RETRY_WITH_INTENT_REASON if offer_intent else INTENT_CANNOT_HELP_REASON)
    return " ".join(lines)


__all__ = [
    "INTENT_CANNOT_HELP_REASON",
    "RETRY_WITH_INTENT_REASON",
    "UNKNOWN_TRANSFORMATION",
    "AffectedAsset",
    "BoundConcept",
    "ImpactAssessment",
    "ImpactStatus",
    "degraded",
    "refusal_reason",
]
