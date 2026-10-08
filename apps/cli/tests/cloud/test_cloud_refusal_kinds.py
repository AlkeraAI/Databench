"""Every refusal the box puts on a chat names its kind, which is what a reader
is shown; the box's own sentence never is."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alkera_cli.cloud.start_failures import gateway_refusal, start_failure_kind
from alkera_cli.harness.adapter import HarnessSandboxRefusedError
from alkera_core.chat_refusals import CHAT_REFUSAL_KINDS, refusal_is_final

CLOUD = Path(__file__).resolve().parents[2] / "alkera_cli" / "cloud"


def _refusal_reports_without_a_kind() -> list[str]:
    """Each call in the box's cloud package that reports ``"refused"`` and
    names no ``kind``."""
    missing: list[str] = []
    for path in sorted(CLOUD.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            says_refused = any(
                isinstance(arg, ast.Constant) and arg.value == "refused" for arg in node.args
            )
            if says_refused and not any(kw.arg == "kind" for kw in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    return missing


def test_every_refusal_the_box_reports_names_its_kind() -> None:
    assert _refusal_reports_without_a_kind() == []


def test_the_gate_sees_the_refusal_reports() -> None:
    """The scan is not vacuous: the package does report refusals."""
    count = 0
    for path in sorted(CLOUD.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(isinstance(a, ast.Constant) and a.value == "refused" for a in node.args)
        )
    assert count >= 6


@pytest.mark.parametrize(
    ("code", "kind", "final"),
    [
        pytest.param("not_publisher", "moving", True, id="moved-to-another-machine"),
        pytest.param("forbidden", "not_allowed", True, id="forbidden"),
        pytest.param("not_found", "transcript_unopened", False, id="not-found-is-a-wait"),
        pytest.param("timeout", "transcript_unopened", False, id="timeout-is-a-wait"),
        pytest.param("something_new", "not_allowed", True, id="unknown-code-is-a-verdict"),
    ],
)
def test_a_gateway_refusal_reports_its_kind(code: str, kind: str, final: bool) -> None:
    _sentence, said_kind = gateway_refusal(code)
    assert said_kind == kind
    assert refusal_is_final(said_kind) is final


@pytest.mark.parametrize(
    ("exc", "never_ran", "kind"),
    [
        pytest.param(RuntimeError("boom"), True, "start_failed", id="never-ran"),
        pytest.param(RuntimeError("boom"), False, "resume_failed", id="ran-before"),
        pytest.param(
            HarnessSandboxRefusedError("no sandbox here"), False, "sandbox_refused", id="sandbox"
        ),
    ],
)
def test_a_start_that_failed_reports_its_kind(
    exc: BaseException, never_ran: bool, kind: str
) -> None:
    assert start_failure_kind(exc, never_ran=never_ran) == kind
    assert kind in CHAT_REFUSAL_KINDS
