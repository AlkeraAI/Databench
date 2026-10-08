"""Tests for `alkera run` — the headless one-shot command.

The command is a thin wrapper over `alkera_cli.chat.headless.run_headless`; these
tests stub that function (the library has its own suite in
`apps/cli/tests/harness/test_headless.py`) and pin the CLI contract: argument
validation, what gets threaded through, exit codes, and `--json` output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.chat.headless import HeadlessError, HeadlessResult, HeadlessTurn
from alkera_cli.commands import run as run_command
from alkera_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _result(stop_reason: str = "completed", **overrides: Any) -> HeadlessResult:
    base = HeadlessResult(
        session_id="sid-1",
        stop_reason=stop_reason,
        turns=[HeadlessTurn(prompt="p", final_text="the answer", stop_reason=stop_reason)],
        tokens={"input": 10, "output": 5},
        cost_usd=0.0123,
        duration_seconds=1.5,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _stub_run_headless(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: HeadlessResult | None = None,
    error: Exception | None = None,
) -> list[dict[str, Any]]:
    """Replace run_headless with a recorder; returns the recorded call kwargs."""
    calls: list[dict[str, Any]] = []

    async def _fake(project_dir: Path, prompts: Any, **kwargs: Any) -> HeadlessResult:
        calls.append({"project_dir": project_dir, "prompts": list(prompts), **kwargs})
        if error is not None:
            raise error
        assert result is not None
        return result

    monkeypatch.setattr(run_command, "run_headless", _fake)
    return calls


# --- Argument validation (exit 2, run_headless never called)


@pytest.mark.parametrize(
    ("args", "expected_snippet"),
    [
        pytest.param(["run"], "exactly one of PROMPT", id="no-prompt"),
        pytest.param(["run", "hi", "--mode", "yolo"], "Unknown mode", id="bad-mode"),
        pytest.param(
            ["run", "hi", "--on-permission", "maybe"], "--on-permission", id="bad-on-permission"
        ),
    ],
)
def test_bad_arguments_exit_2(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected_snippet: str
) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert expected_snippet in result.output
    assert calls == []


def test_unknown_analysis_exits_2_naming_both_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    result = runner.invoke(app, ["run", "hi", "--analysis", "deep"])
    assert result.exit_code == 2
    assert "off" in result.output and "analyst" in result.output
    assert calls == []


def test_prompt_and_prompt_file_together_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("from file", encoding="utf-8")
    result = runner.invoke(app, ["run", "inline", "--prompt-file", str(prompt_file)])
    assert result.exit_code == 2
    assert calls == []


def test_empty_prompt_file_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    prompt_file = tmp_path / "empty.md"
    prompt_file.write_text("  \n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--prompt-file", str(prompt_file)])
    assert result.exit_code == 2
    assert calls == []


# --- Threading + exit codes


@pytest.mark.parametrize(
    ("extra", "analysis", "verification_timeout"),
    [
        pytest.param(
            ["--analysis", "analyst", "--verification-timeout", "45"], "analyst", 45.0, id="given"
        ),
        # Unset keeps the chat's persisted selector (None, the library reads it
        # back) and the library's own verification budget.
        pytest.param([], None, None, id="unset"),
    ],
)
def test_arguments_thread_through_to_the_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra: list[str],
    analysis: str | None,
    verification_timeout: float | None,
) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    args = "--model opus-4-8 --effort xhigh --harness claude --mode bypass --on-permission reject"
    args += " --resume sid-9 --timeout 60 --wait-seed 30"
    result = runner.invoke(app, ["run", "do the thing", "-p", str(tmp_path), *args.split(), *extra])

    assert result.exit_code == 0
    (call,) = calls
    expected = {
        "project_dir": tmp_path.resolve(),
        "prompts": ["do the thing"],
        "model": "opus-4-8",
        "effort": "xhigh",
        "harness": "claude",
        "permission_mode": "bypass",
        "analysis_pipeline": analysis,
        "on_permission": "reject",
        "resume_session_id": "sid-9",
        "timeout_seconds": 60.0,
        "wait_for_seed_seconds": 30.0,
        "verification_timeout_seconds": verification_timeout,
    }
    assert {k: call[k] for k in expected} == expected


def test_show_thinking_option_is_removed_and_effort_none_is_off_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a CLI that retains the redundant boolean or rejects the replacement
    literal ``none`` effort instead of threading it to the headless API."""
    calls = _stub_run_headless(monkeypatch, result=_result())

    removed = runner.invoke(app, ["run", "hi", "--show-thinking"])
    assert removed.exit_code == 2
    assert "No such option" in removed.output
    assert calls == []

    off = runner.invoke(
        app,
        ["run", "hi", "--model", "opus-4-8", "--effort", "none"],
    )
    assert off.exit_code == 0
    assert calls[0]["effort"] == "none"


def test_prompt_file_content_becomes_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_run_headless(monkeypatch, result=_result())
    prompt_file = tmp_path / "task.md"
    prompt_file.write_text("  add a model\nand build it  \n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--prompt-file", str(prompt_file)])
    assert result.exit_code == 0
    assert calls[0]["prompts"] == ["add a model\nand build it"]


@pytest.mark.parametrize(
    ("stop_reason", "expected_code"),
    [
        pytest.param("completed", 0, id="completed"),
        pytest.param("error", 1, id="error"),
        pytest.param("timeout", 1, id="timeout"),
        pytest.param("cancelled", 1, id="cancelled"),
        # A truncated or refused answer is not a successful run: a script that
        # reads exit 0 would ship the half-answer as the whole one.
        pytest.param("max_tokens", 1, id="max_tokens"),
        pytest.param("refusal", 1, id="refusal"),
    ],
)
def test_exit_code_reflects_stop_reason(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, expected_code: int
) -> None:
    _stub_run_headless(monkeypatch, result=_result(stop_reason))
    result = runner.invoke(app, ["run", "hi"])
    assert result.exit_code == expected_code


def test_headless_error_exits_2_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run_headless(monkeypatch, error=HeadlessError("Not signed in. Run `alkera login`."))
    result = runner.invoke(app, ["run", "hi"])
    assert result.exit_code == 2
    assert "Not signed in" in result.output


# --- Output


def test_json_output_is_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run_headless(monkeypatch, result=_result())
    result = runner.invoke(app, ["run", "hi", "--json", "--quiet"])
    assert result.exit_code == 0
    # CliRunner merges stdout+stderr; the JSON object is the stdout tail.
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["session_id"] == "sid-1"
    assert payload["stop_reason"] == "completed"
    assert payload["turns"][0]["final_text"] == "the answer"
    assert payload["tokens"] == {"input": 10, "output": 5}


@pytest.mark.parametrize(
    "verification",
    [
        pytest.param("verified", id="verified"),
        pytest.param("not re-verified: max_tokens", id="not-verified"),
    ],
)
def test_summary_tells_the_user_the_verdict_without_reprinting_the_answer(
    monkeypatch: pytest.MonkeyPatch, verification: str
) -> None:
    """The verdict reaches the user once, with its reason when the answer was not
    re-verified, and the answer text is not printed again."""
    _stub_run_headless(monkeypatch, result=_result(verification=verification))
    result = runner.invoke(app, ["run", "hi"])
    assert result.exit_code == 0
    if verification == "verified":
        assert "re-verified" in result.output
        assert "not re-verified" not in result.output
    else:
        assert verification in result.output
    assert "the answer" not in result.output


def test_off_mode_summary_has_no_delivery_label(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run_headless(monkeypatch, result=_result())
    result = runner.invoke(app, ["run", "hi"])
    assert result.exit_code == 0
    assert "Delivered:" not in result.output


def test_summary_reports_cost_and_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run_headless(monkeypatch, result=_result("error", error_detail="harness crashed hard"))
    result = runner.invoke(app, ["run", "hi"])
    assert result.exit_code == 1
    assert "harness crashed hard" in result.output
    # Cost reads like every other amount: cents, not four decimals.
    assert "$0.01 ·" in result.output
    assert "$0.0123" not in result.output
