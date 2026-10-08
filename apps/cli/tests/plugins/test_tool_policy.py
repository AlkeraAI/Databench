"""The consolidation-policy CI gate.

Sweeps EVERY bundled tool — the built-ins assembled the way production
assembles them, plus whatever each registered plugin (``CLI_PLUGINS``) registers via
``r.tool(...)`` — so a new tool or plugin lands in this sweep automatically.
A violation here means: consolidate the tool surface (the policy: few
parameterized tools, crisp enums, no fat catch-alls, descriptions that work as
ranking signal), or add a WRITTEN exemption below. Rule unit tests live in
``test_tool_lint.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _search_eval import build_real_registry
from _tool_lint import (
    RULE_APP_BUDGET,
    RULE_CATCH_ALL,
    RULE_ENUM_CRISPNESS,
    RULE_PARAM_BUDGET,
    lint_catalog,
)
from alkera_cli.plugins.plugin_base import Tool
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_cli.plugins.plugin_base.registration import RecordingRegistrar

# Every exemption carries its justification; test_no_stale_exemptions retires
# entries that stop firing, so this list can only shrink or stay honest.
EXEMPTIONS: dict[tuple[str, str], str] = {
    ("call_tool", RULE_CATCH_ALL): (
        "the dispatcher's `args` ARE the discovered tool's args — they're validated "
        "against that tool's own Input at dispatch, so a typed "
        "model here is impossible by design"
    ),
    ("elasticsearch.search", RULE_CATCH_ALL): (
        "the query DSL is an open recursive grammar owned by the Elasticsearch "
        "server — typing it would fork the DSL into our manifest and drift every "
        "ES release; effect classification runs on the (method, route), never the body"
    ),
    ("elasticsearch.count", RULE_CATCH_ALL): (
        "same open query-DSL body as elasticsearch.search, on the _count route"
    ),
    ("context_search", RULE_ENUM_CRISPNESS): (
        "ContextKind is a deliberately-open set — `x:` extension kinds ride as "
        "strings (context_types.py), so a closed Literal would make extension "
        "items unsearchable"
    ),
    ("context_note", RULE_PARAM_BUDGET): (
        "the nine knobs are one knowledge item's own facets — what it says (text, title, "
        "kind), the assets it governs (attachments), how it is filed (filing), where it "
        "came from (file_sources), and its trust, visibility and repo scope. One meaning "
        "is ONE item, which the description enforces against repeating the call per asset; "
        "splitting the facets across two tools would make two writes of one item, and every "
        "knob but text has a default, so a call names only the facets it sets"
    ),
    ("context_edit", RULE_PARAM_BUDGET): (
        "the same facets as context_note keyed by item_id instead of kind, every one "
        "omit-to-keep: a correction names the one facet it changes, and a second tool per "
        "facet group would turn one edit into two writes of one item"
    ),
    ("lineage_find", RULE_ENUM_CRISPNESS): (
        "NodeType is a deliberately-open set — `x:<plugin>:<kind>` extension kinds "
        "ride as strings (lineage_types.py), so a closed Literal would make "
        "extension nodes unfilterable"
    ),
    ("app:databricks", RULE_APP_BUDGET): (
        "the Databricks control-plane surface is deliberately comprehensive — jobs, "
        "Lakeflow pipelines, warehouses, clusters, and full Unity Catalog governance "
        "(browse/describe/grant/create) are distinct, typed operations a production data "
        "team expects. Every one is NON-HOT (discovered via search_tools, never in the hot "
        "prefix), so the model-attention sprawl the app budget guards against doesn't apply; "
        "the lifecycle pairs (start/stop, grant/revoke) are clearer + safer as separate typed "
        "tools than one action-parameterized tool."
    ),
    ("app:aws", RULE_APP_BUDGET): (
        "one AWS plugin spans TWO unrelated services — object storage (S3: buckets, "
        "objects, tabular previews, storage summaries) and managed databases (RDS: "
        "instances, clusters, snapshots, parameters, logs, CloudWatch metrics). Splitting "
        'them into separate apps would be worse, not better: `search_tools(app="aws")` is '
        "how the model narrows to this account's surface, and fragmenting it into aws_s3 / "
        "aws_rds makes that filter return half the answer. Every tool is NON-HOT (reached "
        "via search_tools, never in the hot prefix), so the model-attention sprawl the app "
        "budget guards against doesn't apply — the same reasoning as the databricks entry "
        "above. Collapsing distinct typed operations behind one action parameter would also "
        "erase the per-operation Effect declarations that gate S3/RDS mutations."
    ),
}


def _bundled_tools(tmp_path: Path) -> list[type[Tool[Any, Any]]]:
    registry = build_real_registry(tmp_path)
    tools: list[type[Tool[Any, Any]]] = []
    for spec in registry.all_specs():
        tool_cls = registry.tool_for(spec.name)
        assert tool_cls is not None
        tools.append(tool_cls)
    for plugin_cls in CLI_PLUGINS.items():
        rec = RecordingRegistrar()
        plugin_cls().register(rec)
        tools.extend(rec.tools)
    return tools


def test_bundled_catalog_is_policy_clean(tmp_path: Path) -> None:
    violations = lint_catalog(_bundled_tools(tmp_path), exemptions=EXEMPTIONS)
    assert not violations, "consolidation-policy violations:\n" + "\n".join(
        str(v) for v in violations
    )


# Every built-in tool family the production assembly registers. If a family is
# added to the assembly but not here, this guard fails — forcing the new family
# into the sweep instead of letting it pass the gate silently (the
# green-because-vacuous failure this whole gate exists to avoid). The catalog
# the sweep walks is assembled by `_search_eval.real_builtin_registrars`, which
# mirrors `PluginRegistry.tool_registry`; this set is the cross-check on that
# mirror.
EXPECTED_BUILTIN_TOOLS = {
    "search_tools",
    "call_tool",
    "list_plugins",
    "fetch_result",
    "spawn_agent",
    "list_agent_types",
    "context_search",
    "lineage_find",
    "lineage_traverse",
    "lineage_impact",
    "lineage_classify_change",
    "use_skill",
    "sql.query",
    "sql.schema",
    "graph.edit",
    "graph.query",
    "call_graph_python",
}


def test_sweep_actually_saw_the_catalog(tmp_path: Path) -> None:
    """Guard against a silently-empty OR silently-incomplete sweep —
    green-because-vacuous is the failure mode this gate must never have."""
    names = {t.spec.name for t in _bundled_tools(tmp_path)}
    missing = EXPECTED_BUILTIN_TOOLS - names
    assert not missing, (
        f"built-in tools missing from the policy sweep: {sorted(missing)} — a family was "
        "added to PluginRegistry.tool_registry but not to real_builtin_registrars, so it "
        "would ship past this gate unchecked"
    )


def test_no_stale_exemptions(tmp_path: Path) -> None:
    """Every exemption must still be needed — when a waived violation stops
    firing, its entry is dead policy and must be deleted."""
    raw = lint_catalog(_bundled_tools(tmp_path))
    fired = {(v.tool, v.rule) for v in raw}
    stale = set(EXEMPTIONS) - fired
    assert not stale, f"exemptions no longer needed (delete them): {sorted(stale)}"
