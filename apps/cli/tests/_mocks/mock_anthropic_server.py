"""Minimal async HTTP/1.1 server speaking the Anthropic Messages streaming wire.

The opencode↔gateway e2e for the **anthropic wire** points the gateway's
AnthropicDirectTransport at this server (via ``settings.anthropic_base_url``), so a
real opencode subprocess drives a fully-scripted Anthropic provider — deterministic,
no remote LLM, no real network. Parallel to ``mock_openai_server.MockOpenAIServer``
(which serves the OpenAI Chat Completions wire).

Honored surface::

    POST /v1/messages   → text/event-stream of Anthropic Messages events
    GET  /v1/models     → minimal model list (opencode validates on startup)

Scripting: a ``MockScript`` maps the last user-message text → a list of Anthropic
SSE event dicts (built via ``text_events`` / ``tool_use_events`` / ``thinking_events``).
Unmatched prompts fall through to ``"*"``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Sequence
from typing import Any, TypeAlias

AnthropicEvent: TypeAlias = dict[str, Any]
MockScript: TypeAlias = dict[str, Sequence[AnthropicEvent]]

# opencode's compaction agent appends this SUMMARY_TEMPLATE substring as the last
# user message of a summarization call (same marker the OpenAI mock keys on).
COMPACTION_MARKER = "single-sentence task summary"

_USAGE_START = {"input_tokens": 12, "output_tokens": 1}


def _message_start() -> AnthropicEvent:
    return {
        "type": "message_start",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "mock-model",
            "stop_reason": None,
            "usage": dict(_USAGE_START),
        },
    }


def _message_delta(stop_reason: str, *, output_tokens: int = 8) -> list[AnthropicEvent]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


def text_events(text: str, *, output_tokens: int = 8) -> list[AnthropicEvent]:
    """A streamed text response: message_start → text content block (one delta per
    char) → message_delta(end_turn)."""
    events: list[AnthropicEvent] = [_message_start()]
    events.append(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    )
    for ch in text:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": ch},
            }
        )
    events.append({"type": "content_block_stop", "index": 0})
    events.extend(_message_delta("end_turn", output_tokens=output_tokens))
    return events


def thinking_events(thinking: str, text: str) -> list[AnthropicEvent]:
    """A streamed response with an extended-thinking block then a text block —
    exercises the harness reasoning path (AgentThoughtChunk / ReasoningPart)."""
    events: list[AnthropicEvent] = [_message_start()]
    events.append(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )
    for ch in thinking:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": ch},
            }
        )
    events.append(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "mock-sig"},
        }
    )
    events.append({"type": "content_block_stop", "index": 0})
    events.append(
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}
    )
    for ch in text:
        events.append(
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": ch}}
        )
    events.append({"type": "content_block_stop", "index": 1})
    events.extend(_message_delta("end_turn"))
    return events


def tool_use_events(
    tool_name: str, tool_input: dict[str, Any], *, tool_id: str | None = None
) -> list[AnthropicEvent]:
    """A streamed tool_use response: message_start → tool_use content block (input
    streamed as input_json_delta) → message_delta(tool_use)."""
    tid = tool_id or f"toolu_{secrets.token_hex(6)}"
    events: list[AnthropicEvent] = [_message_start()]
    events.append(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": tid, "name": tool_name, "input": {}},
        }
    )
    events.append(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
        }
    )
    events.append({"type": "content_block_stop", "index": 0})
    events.extend(_message_delta("tool_use"))
    return events


class MockAnthropicServer:
    """Async HTTP/1.1 server serving scripted Anthropic Messages SSE.

    Usage mirrors ``MockOpenAIServer``::

        async with MockAnthropicServer({"*": text_events("Hi!")}) as server:
            base_url = server.base_url  # "http://127.0.0.1:<port>"  (no /v1)
    """

    def __init__(
        self,
        script: MockScript,
        *,
        compaction_summary: str | None = None,
        error_on_marker: str | None = None,
        error_status: int = 400,
        error_message: str = "mock injected error",
    ) -> None:
        self._script: MockScript = dict(script)
        self._compaction_summary = compaction_summary
        self._error_on_marker = error_on_marker
        self._error_status = error_status
        self._error_message = error_message
        self._server: asyncio.Server | None = None
        self._port = 0
        self.requests: list[dict[str, Any]] = []
        self.paths: list[tuple[str, str]] = []

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        # The gateway's AnthropicDirectTransport posts `{base_url}/v1/messages`.
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, host="127.0.0.1", port=0)
        sockets = self._server.sockets
        assert sockets, "asyncio.start_server returned no sockets"
        self._port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def __aenter__(self) -> MockAnthropicServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode("ascii", "replace").strip().split(" ", 2)
            except ValueError:
                await self._write_simple(writer, 400, b"")
                return
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("ascii", "replace").rstrip("\r\n")
                if ":" in decoded:
                    k, _, v = decoded.partition(":")
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or "0")
            body = await reader.readexactly(length) if length else b""
            self.paths.append((method, path))
            # The claude CLI posts `/v1/messages?beta=true`; route on the path.
            route = path.split("?", 1)[0].rstrip("/")
            if route == "/v1/messages" and method == "POST":
                await self._serve_messages(writer, body)
            elif path == "/v1/models" and method == "GET":
                payload = json.dumps({"data": [{"id": "mock-model", "type": "model"}]}).encode()
                await self._write_simple(writer, 200, payload, content_type="application/json")
            else:
                await self._write_simple(writer, 404, b"")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _serve_messages(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        try:
            request = json.loads(body) if body else {}
        except json.JSONDecodeError:
            await self._write_simple(writer, 400, b"invalid JSON body")
            return
        self.requests.append(request)
        last_user = _last_user_message(request)
        if self._error_on_marker is not None and self._error_on_marker in last_user:
            err = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": self._error_message},
            }
            await self._write_simple(
                writer,
                self._error_status,
                json.dumps(err).encode(),
                content_type="application/json",
            )
            return
        events = self._select_events(last_user)
        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        writer.write(head)
        await writer.drain()
        try:
            for ev in events:
                frame = f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
                await self._write_chunked(writer, frame)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass

    async def _write_chunked(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(f"{len(payload):x}\r\n".encode())
        writer.write(payload)
        writer.write(b"\r\n")
        await writer.drain()

    async def _write_simple(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/plain",
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()

    def _select_events(self, last_user: str) -> Sequence[AnthropicEvent]:
        if self._compaction_summary is not None and COMPACTION_MARKER in last_user:
            return text_events(self._compaction_summary)
        normalized = last_user.strip()
        if normalized in self._script:
            return self._script[normalized]
        if "*" in self._script:
            return self._script["*"]
        return text_events("(mock default)")


def _last_user_message(request: dict[str, Any]) -> str:
    """Most recent role==user message text from an Anthropic Messages body."""
    messages = request.get("messages") or []
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            )
    return ""


__all__ = [
    "COMPACTION_MARKER",
    "AnthropicEvent",
    "MockAnthropicServer",
    "MockScript",
    "text_events",
    "thinking_events",
    "tool_use_events",
]
