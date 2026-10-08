"""Where the impact gate arms.

The SQL gate and the harness file-edit seam each make one call here. What a
write reaches is measured by the distribution that owns the graph it is
measured against: it registers an :class:`ImpactMeasure` in
``IMPACT_MEASURES``. With none registered, a SQL action's impact is degraded
(nothing was measured, so the effect tiers decide alone) and a file edit
carries no evidence. The mode fold (``decide_gate``) stays in policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint

from alkera_cli.contracts.tool_types import ActionDescriptor
from alkera_cli.plugins.plugin_base.permissions.actor import ActingPrincipal
from alkera_cli.plugins.plugin_base.permissions.impact import ImpactAssessment, degraded
from alkera_cli.plugins.plugin_base.permissions.resolve import GateEvidence

#: The reason an unmeasured SQL action carries when no measure is installed.
NO_MEASURE_REASON = "No impact measurement is installed, so nothing was checked."


class ImpactMeasure(Protocol):
    """Measures what an action reaches before the gate decides it."""

    async def sql_gate_impact(
        self,
        descriptor: ActionDescriptor,
        *,
        lineage_store: Any,
        context_store: Any = None,
        actor: ActingPrincipal | None = None,
    ) -> ImpactAssessment:
        """The impact of a SQL or warehouse action's parsed targets."""
        ...

    async def fs_write_evidence(
        self,
        descriptor: ActionDescriptor,
        *,
        registry: Any,
        workspace_root: Path,
        alkera_dir: Path | None = None,
    ) -> GateEvidence | None:
        """Evidence for a file edit, or ``None`` when the edit reaches nothing
        measured. Never raises: this seam must not block an edit on its own
        machinery."""
        ...


IMPACT_MEASURES: ExtensionPoint[ImpactMeasure] = ExtensionPoint("impact_measures")


def the_measure(measures: Sequence[ImpactMeasure]) -> ImpactMeasure | None:
    """The one registered measure, or ``None``. Two would answer the same
    question twice with no rule for which one the gate believes, so that is a
    composition error."""
    if len(measures) > 1:
        raise ExtensionError(f"{len(measures)} impact measures are registered; the gate takes one")
    return measures[0] if measures else None


async def sql_gate_impact(
    descriptor: ActionDescriptor,
    *,
    lineage_store: Any,
    context_store: Any = None,
    actor: ActingPrincipal | None = None,
) -> ImpactAssessment:
    """Measure an action's parsed warehouse targets, or explain why nothing was."""
    measure = the_measure(IMPACT_MEASURES.items())
    if measure is None:
        return degraded(NO_MEASURE_REASON)
    return await measure.sql_gate_impact(
        descriptor, lineage_store=lineage_store, context_store=context_store, actor=actor
    )


async def fs_write_evidence(
    descriptor: ActionDescriptor,
    *,
    registry: Any,
    workspace_root: Path,
    alkera_dir: Path | None = None,
) -> GateEvidence | None:
    """The harness file-edit seam's evidence, or ``None``."""
    measure = the_measure(IMPACT_MEASURES.items())
    if measure is None:
        return None
    return await measure.fs_write_evidence(
        descriptor, registry=registry, workspace_root=workspace_root, alkera_dir=alkera_dir
    )


__all__ = [
    "IMPACT_MEASURES",
    "NO_MEASURE_REASON",
    "ImpactMeasure",
    "fs_write_evidence",
    "sql_gate_impact",
    "the_measure",
]
