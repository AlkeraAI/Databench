"""Self-test for the MockOpenAIServer.

Before the opencode e2e suite leans on this mock, prove it
actually speaks the OpenAI SSE wire format we promised by hitting it
with httpx.AsyncClient.
"""

from __future__ import annotations

import json

import httpx
import pytest
from httpx_sse import aconnect_sse

from _mocks.mock_openai_server import (
    MockOpenAIServer,
    responses_text,
    responses_thinking,
    responses_tool_use,
    text_chunks,
    tool_call_chunks,
)


def _resp_events(body: str) -> list[dict]:
    """Parse a Responses SSE body into its `data:` JSON events."""
    out: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


@pytest.mark.asyncio
async def test_streams_text_response() -> None:
    script = {"say hi": text_chunks("Hi!")}
    async with MockOpenAIServer(script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with aconnect_sse(
                client,
                "POST",
                "/chat/completions",
                json={
                    "model": "mock-model",
                    "stream": True,
                    "messages": [{"role": "user", "content": "say hi"}],
                },
            ) as event_source:
                content_pieces: list[str] = []
                done_seen = False
                async for sse in event_source.aiter_sse():
                    if sse.data == "[DONE]":
                        done_seen = True
                        break
                    payload = json.loads(sse.data)
                    delta = payload["choices"][0]["delta"]
                    if "content" in delta:
                        content_pieces.append(delta["content"])
                assert done_seen
                assert "".join(content_pieces) == "Hi!"


@pytest.mark.asyncio
async def test_default_script_used_when_no_match() -> None:
    """An unmatched prompt falls through to the ``"*"`` default."""
    script = {"*": text_chunks("default!")}
    async with MockOpenAIServer(script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/chat/completions",
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "anything"}],
                },
            )
            body = resp.text  # the SSE stream body, terminated by [DONE]
            assert "default!" in body or all(ch in body for ch in "default!")


@pytest.mark.asyncio
async def test_tool_call_chunks_carry_function_args() -> None:
    """The mock must produce a finish_reason="tool_calls" envelope so
    opencode treats the response as a tool call, not text."""
    script = {"edit foo": tool_call_chunks("edit", {"path": "foo.txt", "content": "x"})}
    async with MockOpenAIServer(script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with aconnect_sse(
                client,
                "POST",
                "/chat/completions",
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "edit foo"}],
                },
            ) as event_source:
                final_reason: str | None = None
                args_acc = ""
                async for sse in event_source.aiter_sse():
                    if sse.data == "[DONE]":
                        break
                    payload = json.loads(sse.data)
                    choice = payload["choices"][0]
                    if "tool_calls" in choice.get("delta", {}):
                        for tc in choice["delta"]["tool_calls"]:
                            args_acc += tc.get("function", {}).get("arguments", "")
                    if choice.get("finish_reason"):
                        final_reason = choice["finish_reason"]
                assert final_reason == "tool_calls"
                assert json.loads(args_acc) == {
                    "path": "foo.txt",
                    "content": "x",
                }


@pytest.mark.asyncio
async def test_requests_are_recorded() -> None:
    """The server keeps every JSON body so the test can assert what
    opencode actually sent (system prompt, tools, etc.)."""
    script = {"*": text_chunks("ok")}
    async with MockOpenAIServer(script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            await client.post(
                "/chat/completions",
                json={
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "be helpful"},
                        {"role": "user", "content": "go"},
                    ],
                },
            )
        assert len(server.requests) == 1
        sent = server.requests[0]
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]


# --------------------------------------------------------------------------- #
# Responses API wire (the gateway-proxied path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_responses_streams_text_and_usage() -> None:
    script = {"say hi": responses_text("Hi!", input_tokens=42, output_tokens=7)}
    async with MockOpenAIServer(responses_script=script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/responses",
                json={"model": "m", "input": [{"role": "user", "content": "say hi"}]},
            )
    events = _resp_events(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "[DONE]" not in resp.text  # Responses never sends [DONE]
    text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert text == "Hi!"
    usage = events[-1]["response"]["usage"]
    assert usage["input_tokens"] == 42 and usage["output_tokens"] == 7


@pytest.mark.asyncio
async def test_responses_reasoning_carries_encrypted_content() -> None:
    script = {"*": responses_thinking("pondering", "answer", encrypted="ENC-XYZ")}
    async with MockOpenAIServer(responses_script=script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/responses", json={"input": [{"role": "user", "content": "go"}]}
            )
    events = _resp_events(resp.text)
    # The reasoning summary streams, and the encrypted blob rides in the done item.
    summary = "".join(
        e["delta"] for e in events if e["type"] == "response.reasoning_summary_text.delta"
    )
    assert summary == "pondering"
    done_items = [
        e["item"]
        for e in events
        if e["type"] == "response.output_item.done" and e["item"]["type"] == "reasoning"
    ]
    assert done_items and done_items[0]["encrypted_content"] == "ENC-XYZ"


@pytest.mark.asyncio
async def test_responses_tool_call_carries_arguments() -> None:
    script = {"edit foo": responses_tool_use("edit", {"path": "foo.txt"})}
    async with MockOpenAIServer(responses_script=script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/responses", json={"input": [{"role": "user", "content": "edit foo"}]}
            )
    events = _resp_events(resp.text)
    fc = next(
        e["item"]
        for e in events
        if e["type"] == "response.output_item.done" and e["item"]["type"] == "function_call"
    )
    assert fc["name"] == "edit"
    assert json.loads(fc["arguments"]) == {"path": "foo.txt"}


@pytest.mark.asyncio
async def test_responses_tool_result_followup_routes_to_default() -> None:
    """A trailing function_call_output (tool-result turn) routes to `*`, not the
    turn-1 key — so the mock doesn't loop on a tool round-trip."""
    script = {
        "run it": responses_tool_use("glob", {"pattern": "*"}),
        "*": responses_text("all done"),
    }
    async with MockOpenAIServer(responses_script=script) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/responses",
                json={
                    "input": [
                        {"role": "user", "content": "run it"},
                        {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "glob",
                            "arguments": "{}",
                        },
                        {"type": "function_call_output", "call_id": "c1", "output": "[]"},
                    ]
                },
            )
    text = "".join(
        e["delta"] for e in _resp_events(resp.text) if e["type"] == "response.output_text.delta"
    )
    assert text == "all done"


@pytest.mark.asyncio
async def test_responses_error_injection_returns_non_200() -> None:
    script = {"*": responses_text("unused")}
    async with MockOpenAIServer(responses_script=script, error_on_marker="BOOM") as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            resp = await client.post(
                "/responses", json={"input": [{"role": "user", "content": "please BOOM"}]}
            )
    assert resp.status_code == 400
    assert "mock injected error" in resp.text
