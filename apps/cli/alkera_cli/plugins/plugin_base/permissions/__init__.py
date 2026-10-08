"""The permission engine.

The sqlglot-AST classifier, the impact the lineage graph measures for a write,
the policy decision (`decide`) plus the destroy/egress floor, the constructed
`DecisionEngine` that resolves and audits every decision, and the persisted
`.alkera/permissions.yml` config. The connector chokepoint (cap-token
enforcement) lives on `Connector`; this package supplies what it gates on.
"""

from __future__ import annotations

from alkera_cli.plugins.plugin_base.permissions.actor import (
    ActingPrincipal,
    current_acting_principal,
    resolve_acting_principal,
    set_acting_principal,
)
from alkera_cli.plugins.plugin_base.permissions.audit import (
    AuditUnavailableError,
    DecisionRecord,
    DecisionSink,
    DeclaredIntent,
    IntentLedger,
    draft_intent,
)
from alkera_cli.plugins.plugin_base.permissions.bash import (
    CREDENTIAL_PATH_GATE_ENV,
    classify_command,
    command_touches_sensitive_path,
    credential_path_gate_enabled,
    raise_effect,
)
from alkera_cli.plugins.plugin_base.permissions.classifier import (
    classify_statements,
    descriptor_from_sql,
)
from alkera_cli.plugins.plugin_base.permissions.config import (
    EffectRule,
    PermissionRule,
    PermissionsConfig,
    load_permissions,
    load_permissions_cached,
    save_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.gate import (
    GateBinding,
    ShellGateResult,
    SqlGateResult,
    binding_from_context,
    denied_error,
    gate_shell_action,
    gate_sql_action,
)
from alkera_cli.plugins.plugin_base.permissions.impact import (
    AffectedAsset,
    BoundConcept,
    ImpactAssessment,
    ImpactStatus,
)
from alkera_cli.plugins.plugin_base.permissions.policy import (
    AutoDecision,
    PolicyResult,
    decide,
    evaluate_action,
    is_exec,
    is_floor,
    needs_auto_grounding,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import (
    STANDING_OPTIONS,
    ActionResolution,
    DecisionEngine,
    GateEvidence,
    as_answered_by,
    bind_to_this_ask,
    clamp_always,
    without_standing_grant,
)

__all__ = [
    "CREDENTIAL_PATH_GATE_ENV",
    "STANDING_OPTIONS",
    "ActingPrincipal",
    "ActionResolution",
    "AffectedAsset",
    "AuditUnavailableError",
    "AutoDecision",
    "BoundConcept",
    "DecisionEngine",
    "DecisionRecord",
    "DecisionSink",
    "DeclaredIntent",
    "EffectRule",
    "GateBinding",
    "GateEvidence",
    "ImpactAssessment",
    "ImpactStatus",
    "IntentLedger",
    "PermissionRule",
    "PermissionsConfig",
    "PolicyResult",
    "ShellGateResult",
    "SqlGateResult",
    "as_answered_by",
    "bind_to_this_ask",
    "binding_from_context",
    "clamp_always",
    "classify_command",
    "classify_statements",
    "command_touches_sensitive_path",
    "credential_path_gate_enabled",
    "current_acting_principal",
    "decide",
    "denied_error",
    "descriptor_from_sql",
    "draft_intent",
    "evaluate_action",
    "gate_shell_action",
    "gate_sql_action",
    "is_exec",
    "is_floor",
    "load_permissions",
    "load_permissions_cached",
    "needs_auto_grounding",
    "raise_effect",
    "resolve_acting_principal",
    "save_permissions",
    "set_acting_principal",
    "without_standing_grant",
]
