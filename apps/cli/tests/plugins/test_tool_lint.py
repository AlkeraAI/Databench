"""Unit tests for the consolidation-policy lint rules.

Each rule gets a deliberately-bad tool that trips exactly that rule, plus a
clean tool that trips none — so a rule can neither silently die nor over-fire.
The sweep applying the lint to the real catalog is ``test_tool_policy.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pytest
from _tool_lint import (
    MAX_HOT_TOOLS,
    MAX_TOOLS_PER_APP,
    RULE_APP_BUDGET,
    RULE_CATCH_ALL,
    RULE_DESCRIPTION,
    RULE_ENUM_CRISPNESS,
    RULE_HOT_BUDGET,
    RULE_NAMING,
    RULE_PARAM_BUDGET,
    lint_catalog,
    lint_tool,
)
from alkera_cli.plugins.plugin_base import Tool, ToolContext, ToolSpec
from pydantic import BaseModel, Field, create_model


class _Out(BaseModel):
    ok: bool = True


def _tool(
    name: str,
    *,
    description: str = "A perfectly reasonable description of what this tool does.",
    app: str | None = "demo",
    hot: bool = False,
    input_model: type[BaseModel] | None = None,
) -> type[Tool[Any, Any]]:
    class _In(BaseModel):
        query: str

    async def _run(self: Any, args: Any, ctx: ToolContext) -> _Out:
        return _Out()

    return type(
        f"LintFixture_{name.replace('.', '_')}",
        (Tool,),
        {
            "spec": ToolSpec(name=name, description=description, app=app, hot=hot),
            "Input": input_model or _In,
            "Output": _Out,
            "run": _run,
        },
    )


def _rules(violations: list[Any]) -> set[str]:
    return {v.rule for v in violations}


# --- clean tool ------------------------------------------------------------


def test_clean_tool_has_no_violations() -> None:
    assert lint_tool(_tool("demo.fetch")) == []


def test_clean_discriminated_union_passes() -> None:
    """The blessed multi-signature idiom is policy-clean: each
    variant budgeted separately, discriminator consts count as crisp."""

    class _BySql(BaseModel):
        mode: Literal["sql"]
        connection: str
        sql: str
        limit: int = 100

    class _ByTable(BaseModel):
        mode: Literal["table"]
        connection: str
        table: str
        columns: list[str] | None = None
        limit: int = 100

    class _UnionIn(BaseModel):
        args: Annotated[_BySql | _ByTable, Field(discriminator="mode")]

    # Two variants of 4-5 params each: fine per-variant, would trip if summed.
    assert lint_tool(_tool("demo.union", input_model=_UnionIn)) == []


# --- per-rule trips ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_rule"),
    [
        pytest.param("DemoFetch", RULE_NAMING, id="camel-case"),
        pytest.param("demo fetch", RULE_NAMING, id="space"),
        pytest.param("demo..fetch", RULE_NAMING, id="double-dot"),
        pytest.param("1demo.fetch", RULE_NAMING, id="leading-digit"),
    ],
)
def test_naming_rule(name: str, expected_rule: str) -> None:
    assert _rules(lint_tool(_tool(name))) == {expected_rule}


@pytest.mark.parametrize(
    "description",
    [pytest.param("", id="empty"), pytest.param("does stuff", id="too-short")],
)
def test_description_rule(description: str) -> None:
    assert _rules(lint_tool(_tool("demo.fetch", description=description))) == {RULE_DESCRIPTION}


def test_catch_all_rule_free_dict() -> None:
    class _In(BaseModel):
        payload: dict[str, Any]

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_CATCH_ALL}


def test_catch_all_rule_bare_any() -> None:
    # pydantic emits a metadata-only schema ({"title": ...}) for an Any field,
    # NOT {} — the textbook fat catch-all the rule must still catch.
    class _In(BaseModel):
        payload: Any

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_CATCH_ALL}


def test_catch_all_rule_optional_free_dict_unwrapped() -> None:
    class _In(BaseModel):
        payload: dict[str, Any] | None = None

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_CATCH_ALL}


def test_typed_nested_model_is_not_a_catch_all() -> None:
    class _Filter(BaseModel):
        column: str
        value: str

    class _In(BaseModel):
        filter: _Filter

    assert lint_tool(_tool("demo.fetch", input_model=_In)) == []


def test_param_budget_rule() -> None:
    fields: dict[str, Any] = {f"param_{i}": (str, ...) for i in range(9)}
    big = create_model("_BigIn", **fields)
    assert _rules(lint_tool(_tool("demo.fetch", input_model=big))) == {RULE_PARAM_BUDGET}


def test_param_budget_counts_nested_union_variants() -> None:
    # A fat arm hidden inside the nested-discriminated-union idiom must still
    # trip the budget — otherwise the rule is escapable by wrapping the union
    # in a field, and the clean-union test passes only vacuously.
    fat_fields: dict[str, Any] = {"mode": (Literal["fat"], ...)}
    fat_fields.update({f"p{i}": (str, ...) for i in range(9)})  # 10 props in this arm
    fat = create_model("_FatArm", **fat_fields)

    class _Thin(BaseModel):
        mode: Literal["thin"]
        x: str

    class _In(BaseModel):
        args: Annotated[fat | _Thin, Field(discriminator="mode")]

    assert RULE_PARAM_BUDGET in _rules(lint_tool(_tool("demo.fetch", input_model=_In)))


def test_enum_crispness_open_switch_param() -> None:
    class _In(BaseModel):
        mode: str

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_ENUM_CRISPNESS}


def test_enum_crispness_optional_open_switch_param() -> None:
    class _In(BaseModel):
        kind: str | None = None

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_ENUM_CRISPNESS}


def test_enum_crispness_literal_switch_is_fine() -> None:
    class _In(BaseModel):
        mode: Literal["list", "describe"]

    assert lint_tool(_tool("demo.fetch", input_model=_In)) == []


def test_enum_crispness_oversized_enum() -> None:
    class _In(BaseModel):
        mode: Literal["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"]  # 13 values

    assert _rules(lint_tool(_tool("demo.fetch", input_model=_In))) == {RULE_ENUM_CRISPNESS}


def test_switch_named_param_of_non_string_type_is_fine() -> None:
    """The switch-name heuristic only bites open STRINGS — an int named `kind`
    isn't an enum-shaped hole (asymmetric case: don't over-fire)."""

    class _In(BaseModel):
        kind: int

    assert lint_tool(_tool("demo.fetch", input_model=_In)) == []


# --- catalog rules ----------------------------------------------------------


def test_app_budget_rule() -> None:
    tools = [
        _tool(f"sprawl.tool_{i}", app="sprawl", description=f"Does distinct sprawl thing #{i}.")
        for i in range(MAX_TOOLS_PER_APP + 1)
    ]
    violations = lint_catalog(tools)
    assert _rules(violations) == {RULE_APP_BUDGET}
    assert violations[0].tool == "app:sprawl"


def test_app_budget_at_limit_is_fine() -> None:
    tools = [
        _tool(f"dense.tool_{i}", app="dense", description=f"Does distinct dense thing #{i}.")
        for i in range(MAX_TOOLS_PER_APP)
    ]
    assert lint_catalog(tools) == []


def test_duplicate_description_rule() -> None:
    same = "Fetch rows from the warehouse and return them as a preview."
    tools = [
        _tool("demo.alpha", description=same),
        _tool("demo.beta", description=same),
    ]
    violations = lint_catalog(tools)
    assert _rules(violations) == {RULE_DESCRIPTION}
    assert "demo.alpha" in violations[0].tool and "demo.beta" in violations[0].tool


def test_hot_budget_rule() -> None:
    tools = [_tool(f"core.tool_{i}", hot=True) for i in range(MAX_HOT_TOOLS + 1)]
    violations = lint_catalog(tools)
    assert RULE_HOT_BUDGET in _rules(violations)


# --- exemptions -------------------------------------------------------------


def test_exemption_suppresses_only_its_rule() -> None:
    class _In(BaseModel):
        payload: dict[str, Any]

    tool = _tool("demo.fetch", description="", input_model=_In)
    violations = lint_catalog(
        [tool], exemptions={("demo.fetch", RULE_CATCH_ALL): "raw passthrough by design"}
    )
    # The catch-all is waived; the bad description still fires.
    assert _rules(violations) == {RULE_DESCRIPTION}


def test_exemption_without_justification_raises() -> None:
    with pytest.raises(ValueError, match="justification"):
        lint_catalog([_tool("demo.fetch")], exemptions={("demo.fetch", RULE_CATCH_ALL): "  "})
