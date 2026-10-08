"""``request_auto_decision`` — the effect-aware decision at the harness seam.

Pins that a typed ``subject`` routes through the full policy engine (bash via the
classifier) while a subject-less request falls back to the coarse kind-only
``mode_auto_decision``. This is the single chokepoint the runtime permission loop
calls, so the matrix here mirrors the gate's mode-by-effect truth table.
"""

from __future__ import annotations

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness.permission_mode import PermissionMode
from alkera_cli.harness.permission_policy import request_auto_decision
from alkera_cli.plugins.plugin_base.permissions import (
    PermissionRule,
    PermissionsConfig,
    classify_command,
)
from alkera_core.schemas.chat import PermissionRequest

_T = "2026-06-11T00:00:00Z"


def _req(command: str | None = None, *, canonical: str = "shell") -> PermissionRequest:
    subject = None
    if command is not None:
        subject = classify_command(command).model_dump(mode="json")
    return PermissionRequest(
        event_id="e",
        time=_T,  # type: ignore[arg-type]
        session_id="s",
        request_id="r",
        permission_kind="bash",
        canonical_kind=canonical,  # type: ignore[arg-type]
        subject=subject,
    )


@pytest.mark.parametrize(
    ("command", "mode", "expected"),
    [
        # read auto-allows in every working mode (so the passthrough flip is silent)
        ("ls -la", "default", "allow"),
        ("ls -la", "read_only", "allow"),
        ("ls -la", "plan", "allow"),
        ("ls -la", "auto", "allow"),
        # recoverable write: prompt by default, allow in auto/bypass, reject read_only/plan
        ("touch x", "default", "prompt"),
        ("touch x", "auto", "allow"),
        ("touch x", "bypass", "allow"),
        ("touch x", "read_only", "reject"),
        ("touch x", "plan", "reject"),
        # destroy floor: prompts in auto/default; bypass runs everything
        ("rm -rf x", "bypass", "allow"),
        ("rm -rf x", "auto", "prompt"),
        ("rm -rf x", "default", "prompt"),
        ("rm -rf x", "read_only", "reject"),
        # a DB CLI is classified by its inner SQL: a SELECT is a READ → allows
        ("psql -c 'select 1'", "bypass", "allow"),
        ("psql -c 'select 1'", "default", "allow"),
        # ...a DROP via psql is a destroy floor → prompts in auto/default, runs in bypass
        ("psql -c 'drop table t'", "bypass", "allow"),
        ("psql -c 'drop table t'", "auto", "prompt"),
        # an opaque DB session (bare REPL) stays unknown → prompts everywhere but bypass
        ("psql", "bypass", "allow"),
        ("psql", "auto", "prompt"),
    ],
)
def test_subject_routes_through_policy(command: str, mode: PermissionMode, expected: str) -> None:
    assert request_auto_decision(mode, _req(command)).decision == expected


def test_subjectless_falls_back_to_kind_only() -> None:
    # No descriptor → an UNMODELED gating kind we can't classify or judge. It FAILS
    # SAFE in auto: never blanket-allowed there — prompts. bypass is a total override
    # and runs even an unmodeled kind without asking.
    assert request_auto_decision("default", _req(None, canonical="edit")).decision == "prompt"
    assert request_auto_decision("read_only", _req(None, canonical="edit")).decision == "reject"
    assert request_auto_decision("auto", _req(None, canonical="edit")).decision == "prompt"
    assert request_auto_decision("bypass", _req(None, canonical="shell")).decision == "allow"


def test_empty_capability_subject_is_not_trusted_as_read() -> None:
    # A junk subject that validates to the default (effect=read, capability="")
    # must NOT auto-allow — it falls through to the conservative kind-only path.
    req = PermissionRequest(
        event_id="e",
        time=_T,  # type: ignore[arg-type]
        session_id="s",
        request_id="r",
        permission_kind="bash",
        canonical_kind="shell",
        subject={"effect": "read"},  # no capability → not a real descriptor
    )
    assert request_auto_decision("default", req).decision == "prompt"  # not allow


def test_deny_rule_overrides_auto() -> None:
    perms = PermissionsConfig(
        rules=[PermissionRule(capability="shell", effect=Effect.WRITE, decision="deny")]
    )
    rd = request_auto_decision("auto", _req("touch x"), perms)
    assert rd.decision == "reject"
    assert rd.decided_by == "rule"


def test_allow_rule_clears_default_prompt() -> None:
    perms = PermissionsConfig(
        rules=[PermissionRule(capability="shell", operation="touch", decision="allow")]
    )
    rd = request_auto_decision("default", _req("touch x"), perms)
    assert rd.decision == "allow"
    assert rd.decided_by == "rule"


def test_is_auto_write_middle_only_in_auto() -> None:
    # The judge hand-off fires only for auto + a mode-allowed write/egress middle.
    assert request_auto_decision("auto", _req("touch x")).is_auto_write_middle is True
    # EGRESS is judged in auto too (the judge blocks true exfiltration) ...
    assert request_auto_decision("auto", _req("scp f h:/p")).is_auto_write_middle is True
    assert request_auto_decision("bypass", _req("touch x")).is_auto_write_middle is False
    assert request_auto_decision("auto", _req("ls")).is_auto_write_middle is False
    # ... but a DESTROY never is — it's the human floor, no exceptions.
    assert request_auto_decision("auto", _req("rm -rf x")).is_auto_write_middle is False
    # an allow RULE (not the mode) isn't the judge's middle — it's pre-approved
    perms = PermissionsConfig(
        rules=[PermissionRule(capability="shell", operation="touch", decision="allow")]
    )
    assert request_auto_decision("auto", _req("touch x"), perms).is_auto_write_middle is False


def _memory_req() -> PermissionRequest:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    subject = ActionDescriptor(
        capability="knowledge", effect=Effect.MEMORY, operation="knowledge_note"
    ).model_dump(mode="json")
    return PermissionRequest(
        event_id="e",
        time=_T,  # type: ignore[arg-type]
        session_id="s",
        request_id="r",
        permission_kind="alkera_context",
        canonical_kind="other",
        subject=subject,
    )


@pytest.mark.parametrize(
    ("mode", "write_expected"),
    [
        pytest.param("read_only", "reject", id="read_only"),
        pytest.param("plan", "reject", id="plan"),
        pytest.param("default", "prompt", id="default"),
        pytest.param("auto", "allow", id="auto"),
        pytest.param("bypass", "allow", id="bypass"),
    ],
)
def test_agent_memory_allows_at_the_seam_where_a_file_write_keeps_the_modes_answer(
    mode: PermissionMode, write_expected: str
) -> None:
    """The one chokepoint the local runtime AND the cloud mirror's analyst session run
    through: a knowledge write that stays on the machine is allowed in every mode with
    the mode as its provenance, while `touch x` in the same mode is answered as the
    mode always answered it."""
    memory = request_auto_decision(mode, _memory_req())
    assert memory.decision == "allow", (mode, memory)
    assert memory.decided_by == ("bypass" if mode == "bypass" else "mode")
    assert request_auto_decision(mode, _req("touch x")).decision == write_expected
