"""Effect-aware permission decision at the harness request seam.

The runtime permission loop calls :func:`request_auto_decision` for every
``PermissionRequest`` BEFORE it would prompt the human. When the request carries
a typed ``subject`` (an :class:`ActionDescriptor` — bash via the classifier, SQL
via the gate, fs/network via the adapter static map) the full policy engine
(``evaluate_action``: reclassify → rules → floor → ``decide``) rules on it. When
it doesn't (legacy / fake events), it falls back to the coarse kind-only
``mode_auto_decision``.

This is the ONE place the policy runs for harness-native tools, so it gives
daemon parity for free (the editor only sees genuine prompts) and is the
chokepoint the audit sink + the auto-mode grounded judge hang off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from alkera_core.schemas.chat import PermissionRequest

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.harness.permission_mode import PermissionMode, mode_auto_decision
from alkera_cli.plugins.plugin_base.permissions import (
    AutoDecision,
    evaluate_action,
    needs_auto_grounding,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.permissions import PermissionsConfig

Decision = Literal["allow", "prompt", "reject"]

_AUTO_TO_STR: dict[AutoDecision, Decision] = {
    AutoDecision.ALLOW: "allow",
    AutoDecision.PROMPT: "prompt",
    AutoDecision.REJECT: "reject",
}


@dataclass(frozen=True)
class RequestDecision:
    """The policy outcome for one ``PermissionRequest`` — the decision plus the
    provenance + the rebuilt descriptor (so the auto-mode judge + the audit sink
    don't re-parse the subject)."""

    decision: Decision
    decided_by: str
    """``read`` | ``rule`` | ``mode`` | ``floor`` | ``confidence`` | ``kind``."""
    mode: str = ""
    reasons: list[str] = field(default_factory=list)
    descriptor: ActionDescriptor | None = None
    effect: Effect | None = None

    @property
    def is_auto_write_middle(self) -> bool:
        """The auto-mode case handed to the grounded safety judge: a typed ``write``
        OR ``egress`` action resolved by the MODE base (a known write / a data
        egress the mode would auto-allow) OR by UNKNOWN CONFIDENCE (an ambiguous
        write like ``./build.sh`` / ``python3 …`` the policy would otherwise prompt
        on). The judge — not a human prompt — grounds these in auto. The DESTROY /
        floor-op floor (``decided_by='floor'``) and explicit rule allow/deny
        (``'rule'``) never reach the judge; reads never do either."""
        return (
            self.descriptor is not None
            and self.effect is not None
            and needs_auto_grounding(mode=self.mode, effect=self.effect, decided_by=self.decided_by)
        )


def request_auto_decision(
    mode: PermissionMode,
    request: PermissionRequest,
    permissions: PermissionsConfig | None = None,
    *,
    shared_host: bool = False,
) -> RequestDecision:
    """Decide a ``PermissionRequest`` against the active mode + project policy.

    Prefers the typed ``subject`` (full effect-aware engine); falls back to the
    coarse ``canonical_kind`` when there's no descriptor."""
    subject = request.subject
    if subject:
        try:
            descriptor = ActionDescriptor.model_validate(subject)
        except Exception:  # a malformed subject must never wedge the gate
            descriptor = None
        # A subject that validates but carries no capability is a junk/empty
        # descriptor (VersionedModel defaults everything → effect=read); don't
        # TRUST it as a read — fall through to the kind-only path instead.
        if descriptor is not None and descriptor.capability:
            result = evaluate_action(
                descriptor, mode=mode, permissions=permissions, shared_host=shared_host
            )
            return RequestDecision(
                decision=_AUTO_TO_STR[result.decision],
                decided_by=result.decided_by,
                mode=mode,
                reasons=result.reasons,
                descriptor=descriptor,
                effect=result.effective_effect,
            )
    # No usable descriptor → an UNMODELED gating kind we can't classify or judge.
    # FAIL SAFE for ``auto``: never blanket-allow an unmodeled kind there — PROMPT
    # the human instead. ``bypass`` is a total override and runs even these without
    # asking. Modeled kinds (every real opencode/claude kind) carry a descriptor and
    # take the path above; this only catches a brand-new kind. (read_only/plan still
    # reject; default already prompts.)
    fallback = mode_auto_decision(mode, request.canonical_kind)
    if fallback == "allow" and mode == "auto":
        fallback = "prompt"
    return RequestDecision(decision=fallback, decided_by="kind", mode=mode)


__all__ = ["Decision", "RequestDecision", "request_auto_decision"]
