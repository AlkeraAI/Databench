"""What the box puts in front of a chat's agent that the person did not type.

Two kinds of words travel beside a person's message without being it:

* The **hidden context** of a turn: the blocks a mirror gathers before it hands
  a prompt over (the machine card, a chat template's brief, the context the
  server recorded on the message, the files the last reply named but did not
  write). They ride the harness's hidden per-turn channel, so every reader's
  bubble stays exactly the person's words.
* The **continuation** after an ask the agent was no longer holding: a reader
  allowed a permission or answered a question after the box restarted, and
  the agent is told what happened and how to carry on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alkera_core.schemas.chat import PermissionRequest, QuestionRequest

from alkera_cli.cloud.replay import ReplayResult


def hidden_turn(*blocks: str | None) -> dict[str, Any]:
    """The turn's hidden ``context``: every non-blank block in order, joined
    by a blank line, or nothing at all when every block is blank."""
    hidden = [block for block in blocks if block and block.strip()]
    return {"context": "\n\n".join(hidden)} if hidden else {}


def _ask_target(request: PermissionRequest) -> str:
    subject = request.subject if isinstance(request.subject, Mapping) else {}
    raw = subject.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if request.patterns:
        return ", ".join(request.patterns)
    return request.permission_kind


def continuation_after_allow(
    request: PermissionRequest, outcome: ReplayResult | None = None
) -> str:
    """What the agent is told when a reader allows an ask it was no longer
    holding. The action is approved either way; what differs is whether the
    workspace already performed it (the agent reports that result and must
    not redo it), tried and failed (retry), or could not run it at all — the
    original call did NOT run, and a claim of success before a tool result
    would be false."""
    head = (
        "This chat was resumed while you were waiting for permission. The reader has now "
        f"ALLOWED the {request.canonical_kind} action: {_ask_target(request)}. "
    )
    if outcome is not None and outcome.ok:
        return head + (
            f"The workspace has ALREADY performed it on your behalf: {outcome.summary}. "
            "Do not perform it again. Report that result truthfully, then continue with "
            "the task from where it stopped."
        )
    if outcome is not None:
        return head + (
            f"The workspace tried to perform it and it FAILED: {outcome.error}. "
            "Retry that action now (it is approved; raise it again if the tool asks) and "
            "then continue with the task. Do not say it was done until a tool result says so."
        )
    return head + (
        "That tool call did NOT run — the workspace restarted before it could — so it must "
        "be retried: perform that action now (it is approved; raise it again if the tool "
        "asks) and then continue with the task. Do not say it was done until a tool result "
        "says so."
    )


def continuation_after_answer(request: QuestionRequest, answers: Sequence[Sequence[str]]) -> str:
    """What the agent is told when a reader answers a question it was no longer
    holding: each question with the answer given, then carry on."""
    lines = ["This chat was resumed while you were waiting for an answer. The reader answered:"]
    for index, prompt in enumerate(request.questions):
        given = answers[index] if index < len(answers) else []
        lines.append(f"- {prompt.question} -> {'; '.join(given) or '(no answer)'}")
    lines.append("Carry on with the turn from where it stopped.")
    return "\n".join(lines)
