"""The harness-owned ``present_question`` SDK-MCP tool (``mcp__ask__present_question``)
— the replacement for Claude Code's disallowed native ``AskUserQuestion``. It drives
the existing ``QuestionRequest(kind="question")`` surface: the handler publishes the
question, blocks, and ``answer_question`` / ``reject_question`` resolve it, returning
the user's selection to the model. Covers the schema/steering the model receives, the
defensive coercion of model input, the answer formatting, and the full round-trip.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import (
    _ASK_SERVER,
    _ASK_TOOL,
    _ASK_TOOL_FQN,
    _PLAN_SERVER,
    ClaudeAgentAdapter,
    _coerce_question_prompts,
    _format_question_answers,
)
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import QuestionPrompt, QuestionRequest
from mcp.types import ListToolsRequest


def _adapter(tmp_path: Path) -> ClaudeAgentAdapter:
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    return ClaudeAgentAdapter(
        config,
        binary=ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path"),
        event_bus=EventBus(),
    )


async def _next_question(sub: object, *, timeout_s: float = 1.0) -> QuestionRequest:
    async def _pull() -> QuestionRequest:
        async for ev in sub:  # type: ignore[attr-defined]
            if isinstance(ev, QuestionRequest):
                return ev
        raise AssertionError("stream ended before a QuestionRequest")

    return await asyncio.wait_for(_pull(), timeout=timeout_s)


_SAMPLE: list[dict[str, Any]] = [
    {
        "question": "Which framework?",
        "header": "Framework",
        "options": [
            {"label": "React", "description": "SPA"},
            {"label": "Vue", "description": "progressive"},
        ],
        "multiSelect": False,
    }
]


# ---------------------------------------------------------------------------
# Coercion of model-supplied input (defensive — never raise on bad input)
# ---------------------------------------------------------------------------


def test_coerce_basic() -> None:
    prompts = _coerce_question_prompts(_SAMPLE)
    assert len(prompts) == 1
    p = prompts[0]
    assert p.question == "Which framework?"
    assert p.header == "Framework"
    assert [o.label for o in p.options] == ["React", "Vue"]
    assert p.options[0].description == "SPA"
    assert p.multiple is False
    assert p.custom is True  # always allow a free-form answer


def test_coerce_multiselect_and_freeform() -> None:
    prompts = _coerce_question_prompts(
        [{"question": "Pick languages", "multiSelect": True}]  # no options → free-form
    )
    assert prompts[0].multiple is True
    assert prompts[0].options == []
    assert prompts[0].header is None


def test_coerce_is_defensive() -> None:
    assert _coerce_question_prompts("not-a-list") == []
    assert _coerce_question_prompts(None) == []
    prompts = _coerce_question_prompts(
        [
            "not-a-dict",
            {"header": "no question key"},  # missing question → skipped
            {"question": ""},  # empty question → skipped
            {"question": "ok", "header": 5, "options": "bad", "multiSelect": "yes"},
            {"question": "q2", "options": [{"no_label": 1}, {"label": "L"}]},
        ]
    )
    assert [p.question for p in prompts] == ["ok", "q2"]
    assert prompts[0].header is None  # non-str header → None
    assert prompts[0].options == []  # non-list options → []
    assert prompts[0].multiple is False  # non-bool multiSelect → False
    assert [o.label for o in prompts[1].options] == ["L"]  # option w/o label dropped


# ---------------------------------------------------------------------------
# Answer formatting (pair each question with the chosen label(s))
# ---------------------------------------------------------------------------


def test_format_answers_pairs_and_joins() -> None:
    prompts = [
        QuestionPrompt(question="Q1", header="Framework", options=[], multiple=False, custom=True),
        QuestionPrompt(question="Pick langs", options=[], multiple=True, custom=True),
    ]
    text = _format_question_answers(prompts, [["React"], ["Python", "Rust"]])
    assert "Framework: React" in text  # keyed by header when present
    assert "Pick langs: Python, Rust" in text  # keyed by question text otherwise


def test_format_answers_missing_or_empty_selection() -> None:
    prompts = [QuestionPrompt(question="Q1", options=[], multiple=False, custom=True)]
    assert "(no answer)" in _format_question_answers(prompts, [[]])
    assert "(no answer)" in _format_question_answers(prompts, [])  # fewer answers than questions


# ---------------------------------------------------------------------------
# Handler round-trip: emit → answer/reject/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_question_kind_and_returns_answer(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_question(_SAMPLE))
    req = await _next_question(sub)
    assert req.kind == "question"  # NOT plan_approval
    assert req.questions[0].question == "Which framework?"
    assert req.questions[0].custom is True
    await adapter.answer_question(req.request_id, [["React"]])
    result: dict[str, Any] = await asyncio.wait_for(task, timeout=1.0)
    text = result["content"][0]["text"]
    assert "answered" in text.lower()
    assert "React" in text
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_multi_question_roundtrip(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    qs = [
        {"question": "A?", "header": "HA"},
        {"question": "B?", "header": "HB", "multiSelect": True},
    ]
    task = asyncio.create_task(adapter._handle_present_question(qs))
    req = await _next_question(sub)
    assert len(req.questions) == 2
    await adapter.answer_question(req.request_id, [["x"], ["y", "z"]])
    text = (await asyncio.wait_for(task, timeout=1.0))["content"][0]["text"]
    assert "HA: x" in text
    assert "HB: y, z" in text


@pytest.mark.asyncio
async def test_reject_returns_dismissed_with_reason(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_question(_SAMPLE))
    req = await _next_question(sub)
    await adapter.reject_question(req.request_id, "not now")
    text = (await asyncio.wait_for(task, timeout=1.0))["content"][0]["text"].lower()
    assert "dismiss" in text
    assert "not now" in text
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_cancel_returns_dismissed(tmp_path: Path) -> None:
    """A cancelled turn (the parked future is cancelled) resolves to a clean
    'dismissed' result for the model — never an unhandled error."""
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_question(_SAMPLE))
    req = await _next_question(sub)
    adapter._pending_questions[req.request_id].cancel()
    text = (await asyncio.wait_for(task, timeout=1.0))["content"][0]["text"].lower()
    assert "dismiss" in text
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_empty_questions_short_circuits(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    result = await adapter._handle_present_question([])
    assert "no question" in result["content"][0]["text"].lower()
    assert adapter._pending_questions == {}  # nothing parked → never blocks/prompts


# ---------------------------------------------------------------------------
# Options wiring: server registered, allow-listed, steering appended
# ---------------------------------------------------------------------------


def test_build_options_registers_ask_server_and_steering(tmp_path: Path) -> None:
    opts = _adapter(tmp_path)._build_options(resume=False)
    assert _ASK_SERVER in opts.mcp_servers
    assert _PLAN_SERVER in opts.mcp_servers  # both tools coexist
    perms = json.loads(opts.settings)["permissions"]
    assert _ASK_TOOL_FQN in perms["allow"]  # auto-approved → never hits can_use_tool
    sp = opts.system_prompt
    assert isinstance(sp, dict)
    assert sp.get("preset") == "claude_code"  # APPEND to the default, don't replace
    assert _ASK_TOOL in sp.get("append", "")  # steers the model to our tool


# ---------------------------------------------------------------------------
# The tool spec the model actually receives (clear description + correct schema)
# ---------------------------------------------------------------------------


async def test_present_question_tool_is_well_described(tmp_path: Path) -> None:
    srv = _adapter(tmp_path)._make_ask_server()
    handler = srv["instance"].request_handlers[ListToolsRequest]
    res = await handler(ListToolsRequest(method="tools/list"))
    tools = {t.name: t for t in res.root.tools}
    assert _ASK_TOOL in tools
    pq = tools[_ASK_TOOL]
    desc = (pq.description or "").lower()
    assert "block" in desc and "answer" in desc  # spells out the blocking + return

    schema = pq.inputSchema
    assert schema["required"] == ["questions"]
    item = schema["properties"]["questions"]["items"]
    # only `question` is required — header/options/multiSelect are optional
    assert item["required"] == ["question"]
    assert "header" in item["properties"] and "header" not in item["required"]
    assert "multiSelect" in item["properties"] and "multiSelect" not in item["required"]
    # nested option: only `label` required, `description` optional, both documented
    opt = item["properties"]["options"]["items"]
    assert opt["required"] == ["label"]
    assert opt["properties"]["description"]["description"]  # the field IS documented
    # every leaf field carries a description
    assert item["properties"]["question"]["description"]
    assert item["properties"]["multiSelect"]["description"]
    assert schema["properties"]["questions"]["description"]
