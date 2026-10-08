"""Consolidation-policy lint for the tool surface.

Dev/CI tooling, deliberately NOT product code: the policy is enforced by
``test_tool_policy.py`` sweeping every bundled tool, and unit-tested in
``test_tool_lint.py``. The rules encode the consolidation policy the spec sets
for plugin authors — few parameterized tools, crisp enums, no fat catch-alls,
descriptions good enough to be BM25 ranking signal, plus the hot-core
budget. All checks read the tool's *observable contract* (its ``spec`` +
the derived LLM-facing JSON-Schema), never its implementation.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alkera_cli.plugins.plugin_base import Tool

MAX_TOOLS_PER_APP = 20
MAX_PARAMS_PER_VARIANT = 8
MAX_ENUM_VALUES = 12
MIN_DESCRIPTION_CHARS = 20
MAX_HOT_TOOLS = 25

RULE_APP_BUDGET = "app_budget"
RULE_CATCH_ALL = "catch_all"
RULE_PARAM_BUDGET = "param_budget"
RULE_ENUM_CRISPNESS = "enum_crispness"
RULE_DESCRIPTION = "description"
RULE_NAMING = "naming"
RULE_HOT_BUDGET = "hot_budget"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
# Param names that read as a mode/variant switch — these must be a closed enum
# (Literal / discriminator const) so the model can't invent values.
_SWITCH_PARAM_NAMES = {"mode", "kind", "type", "action"}


@dataclass(frozen=True)
class PolicyViolation:
    tool: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.tool}: {self.message}"


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        resolved = defs.get(node["$ref"].split("/")[-1], {})
        return resolved if isinstance(resolved, dict) else {}
    return node


def _oneof_arms(node: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The arms of a discriminated union (pydantic emits ``oneOf``), or None.
    ``anyOf`` is deliberately NOT treated as a variant marker — that's how
    ``T | None`` (Optional) serializes, which ``_unwrap_optional`` handles."""
    arms = node.get("oneOf")
    return arms if isinstance(arms, list) else None


def _variants(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The call-signature variants of a schema. Two blessed shapes:
    a top-level discriminated union (the ``RootModel`` idiom — ``sql.query``), and
    a union nested under a field (``args: Annotated[A | B, discriminator]``), which
    pydantic emits as a property with its own ``oneOf``. Each arm is a variant,
    merged with the wrapper's other params so per-variant budgets see real fields.
    A plain object with no union is its own single variant."""
    defs = schema.get("$defs", {})
    for key in ("oneOf", "anyOf"):
        arms = schema.get(key)
        if isinstance(arms, list):
            return [_resolve(a, defs) for a in arms if isinstance(a, dict)]
    props = schema.get("properties")
    if isinstance(props, dict):
        base = {k: v for k, v in props.items() if not (isinstance(v, dict) and _oneof_arms(v))}
        expanded: list[dict[str, Any]] = []
        for v in props.values():
            arms = _oneof_arms(v) if isinstance(v, dict) else None
            for arm in arms or []:
                resolved = _resolve(arm, defs) if isinstance(arm, dict) else {}
                expanded.append(
                    {"type": "object", "properties": {**base, **resolved.get("properties", {})}}
                )
        if expanded:
            return expanded
    return [schema]


def _unwrap_optional(prop: dict[str, Any]) -> dict[str, Any]:
    """``T | None`` serializes as ``anyOf: [T, null]`` — unwrap to T so the
    checks see the real shape."""
    arms = prop.get("anyOf")
    if isinstance(arms, list):
        non_null = [a for a in arms if isinstance(a, dict) and a.get("type") != "null"]
        if len(non_null) == 1:
            return non_null[0]
    return prop


# JSON-Schema keywords that actually constrain a value. A property carrying
# NONE of these gives the model no shape to fill — that's a bare ``Any``.
_SCHEMA_CONSTRAINT_KEYS = frozenset(
    {"type", "$ref", "enum", "const", "oneOf", "anyOf", "allOf", "not"}
)


def _is_free_form_object(prop: dict[str, Any]) -> bool:
    """A fat catch-all arg: a value with no schema to validate against — a bare
    ``Any`` (pydantic emits a metadata-only schema like ``{"title": "X"}``, NOT
    ``{}``) or a ``dict[str, Any]`` (an object with open ``additionalProperties``)."""
    if not (_SCHEMA_CONSTRAINT_KEYS & prop.keys()):
        return True  # bare Any — only title/description/default metadata, no constraint
    if prop.get("type") != "object":
        return False
    extra = prop.get("additionalProperties", True if "properties" not in prop else None)
    return extra is True or extra == {}


def lint_tool(tool_cls: type[Tool[Any, Any]]) -> list[PolicyViolation]:
    """Per-tool rules. Catalog-level rules (app budget, duplicate descriptions,
    hot budget) live in :func:`lint_catalog`."""
    spec = tool_cls.spec
    name = spec.name
    out: list[PolicyViolation] = []

    if not _NAME_RE.fullmatch(name):
        out.append(
            PolicyViolation(
                name, RULE_NAMING, "tool names are lowercase dotted/underscore (e.g. 'app.verb')"
            )
        )

    if len(spec.description.strip()) < MIN_DESCRIPTION_CHARS:
        out.append(
            PolicyViolation(
                name,
                RULE_DESCRIPTION,
                f"description under {MIN_DESCRIPTION_CHARS} chars — descriptions are the "
                "search index's ranking signal, write what the tool does and returns",
            )
        )

    schema = tool_cls.input_schema()
    for variant in _variants(schema):
        props = variant.get("properties", {})
        if len(props) > MAX_PARAMS_PER_VARIANT:
            out.append(
                PolicyViolation(
                    name,
                    RULE_PARAM_BUDGET,
                    f"{len(props)} params in one call signature (max {MAX_PARAMS_PER_VARIANT}) "
                    "— split the tool or drop knobs",
                )
            )
        for pname, raw_prop in props.items():
            if not isinstance(raw_prop, dict):
                continue
            prop = _unwrap_optional(raw_prop)
            if _is_free_form_object(prop):
                out.append(
                    PolicyViolation(
                        name,
                        RULE_CATCH_ALL,
                        f"param {pname!r} is a free-form object — give it a typed model "
                        "(no fat catch-alls)",
                    )
                )
            enum = prop.get("enum")
            if isinstance(enum, list) and len(enum) > MAX_ENUM_VALUES:
                out.append(
                    PolicyViolation(
                        name,
                        RULE_ENUM_CRISPNESS,
                        f"param {pname!r} has {len(enum)} enum values (max {MAX_ENUM_VALUES}) "
                        "— that's a lookup table, not a crisp switch",
                    )
                )
            if (
                pname in _SWITCH_PARAM_NAMES
                and prop.get("type") == "string"
                and "enum" not in prop
                and "const" not in prop
            ):
                out.append(
                    PolicyViolation(
                        name,
                        RULE_ENUM_CRISPNESS,
                        f"param {pname!r} reads as a variant switch but is an open string — "
                        "type it Literal/enum so the model can't invent values",
                    )
                )
    return out


def lint_catalog(
    tools: Sequence[type[Tool[Any, Any]]],
    *,
    exemptions: Mapping[tuple[str, str], str] | None = None,
) -> list[PolicyViolation]:
    """Lint a whole catalog: every per-tool rule plus the cross-tool budgets.
    ``exemptions`` maps ``(tool_name, rule)`` to a REQUIRED justification —
    an empty reason raises, so every exception stays documented."""
    exempt = dict(exemptions or {})
    for key, reason in exempt.items():
        if not reason.strip():
            raise ValueError(f"exemption {key} needs a written justification")

    out: list[PolicyViolation] = []
    for tool_cls in tools:
        out.extend(lint_tool(tool_cls))

    per_app: dict[str, list[str]] = defaultdict(list)
    for tool_cls in tools:
        if tool_cls.spec.app:
            per_app[tool_cls.spec.app].append(tool_cls.spec.name)
    for app, names in sorted(per_app.items()):
        if len(names) > MAX_TOOLS_PER_APP:
            out.append(
                PolicyViolation(
                    f"app:{app}",
                    RULE_APP_BUDGET,
                    f"{len(names)} tools in app {app!r} (max {MAX_TOOLS_PER_APP}) — "
                    "consolidate into fewer parameterized tools",
                )
            )

    descriptions = Counter(t.spec.description.strip() for t in tools if t.spec.description.strip())
    for desc, count in descriptions.items():
        if count > 1:
            dupes = sorted(t.spec.name for t in tools if t.spec.description.strip() == desc)
            out.append(
                PolicyViolation(
                    ", ".join(dupes),
                    RULE_DESCRIPTION,
                    "identical descriptions — the search index can't tell these tools apart",
                )
            )

    hot = sorted(t.spec.name for t in tools if t.spec.hot)
    if len(hot) > MAX_HOT_TOOLS:
        out.append(
            PolicyViolation(
                "hot-prefix",
                RULE_HOT_BUDGET,
                f"{len(hot)} hot tools (max {MAX_HOT_TOOLS}) — the always-on prefix must stay "
                f"small: {', '.join(hot)}",
            )
        )

    return [v for v in out if (v.tool, v.rule) not in exempt]
