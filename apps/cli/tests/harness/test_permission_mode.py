"""Tests for `alkera_cli.harness.permission_mode`."""

from __future__ import annotations

import pytest
from alkera_cli.harness.permission_mode import (
    CYCLE_MODES,
    MODE_MENU_ORDER,
    PLAN_ACCEPT_OPTIONS,
    PermissionMode,
    denial_message,
    mode_auto_decision,
    mode_to_agent,
    next_mode,
    parse_mode,
    plan_label_to_mode,
)
from alkera_core.schemas.chat import CanonicalPermissionKind

# ---------------------------------------------------------------------------
# mode_auto_decision — the policy matrix (canonical kinds only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "kind", "expected"),
    [
        # default: everything mutating prompts.
        ("default", "edit", "prompt"),
        ("default", "shell", "prompt"),
        ("default", "network", "prompt"),
        ("default", "task", "prompt"),
        ("default", "external", "prompt"),
        ("default", "other", "prompt"),
        # plan: edits auto-rejected (planning is read-only), else prompt.
        ("plan", "edit", "reject"),
        ("plan", "shell", "prompt"),
        ("plan", "network", "prompt"),
        ("plan", "task", "prompt"),
        ("plan", "external", "prompt"),
        ("plan", "other", "prompt"),
        # bypass: everything auto-allowed.
        ("bypass", "edit", "allow"),
        ("bypass", "shell", "allow"),
        ("bypass", "network", "allow"),
        ("bypass", "task", "allow"),
        ("bypass", "external", "allow"),
        ("bypass", "other", "allow"),
    ],
)
def test_mode_auto_decision(
    mode: PermissionMode, kind: CanonicalPermissionKind, expected: str
) -> None:
    assert mode_auto_decision(mode, kind) == expected


def test_mode_auto_decision_other_kind_defaults_to_prompt() -> None:
    """The catch-all "other" canonical kind isn't an edit, so it prompts
    in default/plan and allows only in bypass. (Unknown native opencode keys
    are mapped to "other" by the adapter.)"""
    assert mode_auto_decision("default", "other") == "prompt"
    assert mode_auto_decision("plan", "other") == "prompt"
    assert mode_auto_decision("bypass", "other") == "allow"


def test_read_only_rejects_every_mutation() -> None:
    # A permission ask only fires for a mutation (reads run freely), so
    # read_only rejects every canonical kind that reaches the broker.
    for kind in ("edit", "shell", "network", "task", "external", "other"):
        assert mode_auto_decision("read_only", kind) == "reject"


def test_auto_allows_canonical_mutations() -> None:
    # MVP auto = allow the recoverable middle; the destroy floor (and the auto-mode
    # judge that grounds the write/egress middle) live at the effect-aware policy
    # layer (policy.decide / DecisionEngine.resolve), not in this coarse kind-only
    # fallback.
    for kind in ("edit", "shell", "other"):
        assert mode_auto_decision("auto", kind) == "allow"


# ---------------------------------------------------------------------------
# mode_to_agent
# ---------------------------------------------------------------------------


def test_mode_to_agent() -> None:
    assert mode_to_agent("plan") == "plan"
    assert mode_to_agent("default") is None
    assert mode_to_agent("auto") is None
    assert mode_to_agent("bypass") is None


# ---------------------------------------------------------------------------
# parse_mode (CLI input → canonical mode)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("default", "default"),
        ("normal", "default"),
        ("accept-edits", "default"),  # legacy alias → default (mode retired)
        ("accept_edits", "default"),
        ("auto", "auto"),  # `auto` is its own mode (grounded write-middle)
        ("read-only", "read_only"),
        ("ro", "read_only"),
        ("plan", "plan"),
        ("bypass", "bypass"),
        ("yolo", "bypass"),
        ("  AUTO  ", "auto"),  # case + whitespace insensitive
    ],
)
def test_parse_mode(raw: str, expected: PermissionMode) -> None:
    assert parse_mode(raw) == expected


def test_parse_mode_unknown_returns_none() -> None:
    assert parse_mode("nonsense") is None
    assert parse_mode("") is None


# ---------------------------------------------------------------------------
# next_mode (cycling)
# ---------------------------------------------------------------------------


def test_next_mode_cycles_through_the_cycle_set() -> None:
    # Shift+Tab walks CYCLE_MODES in order and wraps; read_only is now ON it.
    seen = []
    mode: PermissionMode = CYCLE_MODES[0]
    for _ in range(len(CYCLE_MODES)):
        seen.append(mode)
        mode = next_mode(mode)
    assert tuple(seen) == CYCLE_MODES  # visits each mode, in order
    assert mode == CYCLE_MODES[0]  # wraps back to start
    assert "read_only" in CYCLE_MODES


def test_cycle_order_is_the_menu_order() -> None:
    # Shift+Tab steps through exactly the sequence the /mode picker shows —
    # one canonical UI ordering (default → plan → read-only → auto → bypass),
    # never two that disagree.
    assert CYCLE_MODES == MODE_MENU_ORDER
    assert CYCLE_MODES == ("default", "plan", "read_only", "auto", "bypass")


def test_next_mode_advances_in_menu_order() -> None:
    # Each Shift+Tab advances one step along the menu order and wraps at the end.
    assert next_mode("default") == "plan"
    assert next_mode("plan") == "read_only"
    assert next_mode("read_only") == "auto"
    assert next_mode("auto") == "bypass"
    assert next_mode("bypass") == "default"  # wraps


# ---------------------------------------------------------------------------
# Plan-approval label → mode mapping
# ---------------------------------------------------------------------------


def test_plan_accept_options_map_to_distinct_modes() -> None:
    """The three accept options cover default / auto / bypass."""
    modes = [mode for _, mode in PLAN_ACCEPT_OPTIONS]
    assert modes == ["default", "auto", "bypass"]


def test_plan_label_to_mode_for_each_accept_option() -> None:
    for label, mode in PLAN_ACCEPT_OPTIONS:
        assert plan_label_to_mode(label) == mode


def test_plan_label_to_mode_free_form_is_rejection() -> None:
    """Anything that isn't a known accept label is a rejection reason,
    so it maps to None (stay in plan mode)."""
    assert plan_label_to_mode("please add tests first") is None
    assert plan_label_to_mode("") is None
    assert plan_label_to_mode("Accept") is None  # partial, not exact


# ---------------------------------------------------------------------------
# Unified plan-mode steering (parity across both backends)
# ---------------------------------------------------------------------------


def test_plan_mode_steering_is_unified_contract() -> None:
    from alkera_cli.harness.permission_mode import plan_mode_steering

    text = plan_mode_steering("plan_present", "question")
    # Names the backend's plan + question tools and the read-only / either-or loop.
    assert "plan_present" in text and "question" in text
    assert "READ-ONLY" in text
    assert "do NOT edit" in text or "do not edit" in text.lower()
    assert "EITHER" in text  # plan OR questions — prose is acceptable
    # Planning leans HEAVILY on Explore, and the plan-mode reminder says so
    # (this lands on BOTH backends via the shared source).
    assert "Explore" in text
    assert "spawn_agent" in text
    # The model must know the agent tools are ALWAYS available (don't do it inline)
    # and that `list_agent_types` is the discovery path.
    assert "ALWAYS have it" in text
    assert "list_agent_types" in text
    # Planning is exactly when recorded team knowledge pays off — the steering points the
    # planner at the KB via context_search. (same-author-ok: comment-only wording fix)
    assert "context_search" in text
    assert "knowledge base" in text.lower()


def test_both_backends_share_the_plan_contract() -> None:
    # opencode (PLAN_MODE_SYSTEM_PROMPT) and Claude (_CLAUDE_PLAN_STEERING) derive
    # from the SAME template — only the tool names differ per backend.
    from alkera_cli.harness.adapters.claude_agent import _CLAUDE_PLAN_STEERING
    from alkera_cli.harness.permission_mode import PLAN_MODE_SYSTEM_PROMPT

    for shared in (
        "Plan mode is READ-ONLY",
        "End EVERY plan-mode turn",
        "neither a plan nor questions",
    ):
        assert shared in PLAN_MODE_SYSTEM_PROMPT
        assert shared in _CLAUDE_PLAN_STEERING
    assert "plan_present" in PLAN_MODE_SYSTEM_PROMPT
    assert "mcp__plan__present_plan" in _CLAUDE_PLAN_STEERING


# ---------------------------------------------------------------------------
# denial_message — the model-visible "why" on a denied tool call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decided_by", "must_contain", "must_not_contain"),
    [
        # A mode reject names the mode AND that the user set it.
        pytest.param("mode", ["read-only mode", "the user set", "Don't retry"], [], id="mode"),
        # A rule reject names the user rule — and must NOT claim it's a mode.
        pytest.param(
            "rule", ["permission rule", "Don't retry"], ["mode, which the user set"], id="rule"
        ),
        pytest.param("floor", ["destructive", "explicit approval"], [], id="floor"),
        pytest.param("judge", ["safety check", "Don't retry"], [], id="judge"),
        # A human reject is a short hand-back (the turn ends anyway) — no "don't retry".
        pytest.param(
            "human", ["declined", "what they'd like instead"], ["Don't retry"], id="human"
        ),
        pytest.param("fail_closed", ["no approver"], [], id="fail-closed"),
        # An unknown/coarse provenance is still actionable, never silent/empty.
        pytest.param("confidence", ["Refused", "Don't retry"], [], id="fallback"),
    ],
)
def test_denial_message_names_its_source(
    decided_by: str, must_contain: list[str], must_not_contain: list[str]
) -> None:
    msg = denial_message(
        mode="read_only",
        decided_by=decided_by,
        classifier_reason="writes a file via redirect",
        effect="write",
    )
    assert msg  # never empty
    for needle in must_contain:
        assert needle in msg, f"{decided_by!r} message missing {needle!r}: {msg!r}"
    for needle in must_not_contain:
        assert needle not in msg, f"{decided_by!r} message should not contain {needle!r}: {msg!r}"


def test_denial_message_includes_the_classifier_reason_when_present() -> None:
    msg = denial_message(
        mode="read_only", decided_by="mode", classifier_reason="writes a file via redirect"
    )
    assert "writes a file via redirect" in msg


def test_denial_message_handles_missing_reason_and_effect() -> None:
    # No classifier_reason / no effect → still a clean, non-empty, no-"Reason:" message.
    msg = denial_message(mode="plan", decided_by="mode")
    assert "Reason:" not in msg
    assert "plan mode" in msg
    assert "mutating actions" in msg  # generic effect phrasing when effect is unknown


def test_denial_message_uses_the_mode_label() -> None:
    assert "read-only mode" in denial_message(mode="read_only", decided_by="mode")
    assert "plan mode" in denial_message(mode="plan", decided_by="mode")


# ---------------------------------------------------------------------------
# Mode awareness: every mode self-describes + the switch reminder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["read_only", "default", "bypass"])
def test_simple_modes_now_self_describe(mode: PermissionMode) -> None:
    from alkera_cli.harness.permission_mode import MODE_LABELS, mode_system_prompt

    text = mode_system_prompt(mode)
    assert text is not None  # used to be None for these three
    assert MODE_LABELS[mode] in text
    assert "set by the user" in text


def test_plan_and_auto_keep_their_rich_steering() -> None:
    from alkera_cli.harness.permission_mode import (
        AUTO_MODE_SYSTEM_PROMPT,
        PLAN_MODE_SYSTEM_PROMPT,
        mode_system_prompt,
    )

    assert mode_system_prompt("plan") == PLAN_MODE_SYSTEM_PROMPT
    assert mode_system_prompt("auto") == AUTO_MODE_SYSTEM_PROMPT


def test_mode_switch_reminder_names_old_new_and_new_rules() -> None:
    from alkera_cli.harness.permission_mode import mode_switch_reminder

    text = mode_switch_reminder("default", "read_only")
    assert "from default to read-only" in text
    # Explains the NEW mode's behavior (refuses writes), not the old one.
    assert "refused" in text
    # The reverse switch explains default's behavior instead.
    back = mode_switch_reminder("read_only", "default")
    assert "from read-only to default" in back
    assert "approval" in back


# ---------------------------------------------------------------------------
# Knowledge is the agent's memory — every restricting mode says so, in the one
# sentence the policy spells, so "read-only" is never read as "don't save what I
# learned". A live read-only chat once refused "write a knowledge item saying
# Robin Lee was here" on the strength of "don't try to persist anything".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["read_only", "plan", "default"])
def test_every_restricting_mode_tells_the_model_knowledge_is_its_memory(
    mode: PermissionMode,
) -> None:
    from alkera_cli.harness.permission_mode import (
        KNOWLEDGE_IS_MEMORY,
        mode_switch_reminder,
        mode_system_prompt,
    )

    text = mode_system_prompt(mode) or ""
    assert KNOWLEDGE_IS_MEMORY in text
    assert KNOWLEDGE_IS_MEMORY in mode_switch_reminder("auto", mode)
    # The unqualified ban the model read as covering its memory is gone; what remains
    # names the workspace as the thing not to persist to.
    assert "persist anything (" not in text
    assert "cannot be modified" not in text


def test_the_memory_sentence_names_the_tools_the_modes_and_the_verdict() -> None:
    from alkera_cli.harness.permission_mode import KNOWLEDGE_IS_MEMORY

    sentence = KNOWLEDGE_IS_MEMORY

    assert "context_note" in sentence and "context_edit" in sentence
    assert "read-only and plan included" in sentence
    assert "always allowed and expected" in sentence and "never refused" in sentence
    assert "not a change to the workspace" in sentence


# ---------------------------------------------------------------------------
# The read-only steering says what the read-only stance does. The table admits a
# write inside the chat's own folder in every stance and refuses the shell in
# read-only; a steering that told the model "no file writes" made it refuse to
# write at all, from the prompt and not from any refusal.
# ---------------------------------------------------------------------------


def test_the_read_only_steering_says_what_the_stance_allows() -> None:
    from alkera_cli.harness.permission_mode import MODE_RULES, mode_system_prompt

    rule = MODE_RULES["read_only"]
    assert rule.chat_folder_change == "allow" and rule.workspace_change == "reject"
    text = mode_system_prompt("read_only") or ""
    # What the stance allows is said as allowed, where it is allowed.
    assert "this chat's own folder" in text and "needs no approval" in text
    # What the stance refuses is said as refused: the shell, and anything outside.
    assert "shell does not run" in text and "refused" in text
    for contradiction in ("no file writes", "no scratch files", "Every file edit"):
        assert contradiction not in text
    # The picker's one line and the steering agree on the folder.
    assert "writes only in this chat" in rule.description
