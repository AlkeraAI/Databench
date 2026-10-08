"""The auto-mode safety judge + its one-shot gateway completion client.

The completion client is pinned against a stubbed gateway (``httpx.MockTransport``
returning Anthropic Messages SSE / error statuses); the judge's prompt-building,
JSON parsing, and fail-closed semantics are pinned with a stubbed ``complete``.
The real-wire proof (judge vs the live gateway) is a ``live_provider`` test.
"""

from __future__ import annotations

import json

import httpx
import pytest
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.gateway.client import GatewayAuthError, GatewayUnavailableError
from alkera_cli.harness.gateway_completion import (
    GatewayInsufficientCreditError,
    complete,
)
from alkera_cli.harness.safety_judge import (
    GatewaySafetyJudge,
    JudgeVerdict,
    _build_judge_payload,
    _parse_verdict,
    _target_scope,
)
from alkera_cli.plugins.plugin_base.permissions import classify_command


def _sse(*texts: str) -> bytes:
    """Build an Anthropic Messages SSE body that streams ``texts`` as deltas."""
    lines = ['data: {"type":"message_start"}']
    for t in texts:
        frame = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
        lines.append(f"data: {json.dumps(frame)}")
    lines.append('data: {"type":"message_stop"}')
    return ("\n\n".join(lines) + "\n\n").encode()


def _openai_sse(*texts: str) -> bytes:
    """Build an OpenAI Responses SSE body that streams ``texts`` as deltas."""
    lines = ['data: {"type":"response.created"}']
    for t in texts:
        frame = {"type": "response.output_text.delta", "delta": t}
        lines.append(f"data: {json.dumps(frame)}")
    lines.append('data: {"type":"response.completed"}')
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_complete_accumulates_sse_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["system"] == "s"
        assert body["max_tokens"] == 256
        assert body["stream"] is True
        return httpx.Response(200, content=_sse("Hel", "lo ", "world"))

    async with _client(handler) as c:
        out = await complete(
            gateway_url="http://gw",
            token="tok",
            model="claude-haiku-4.5",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            client=c,
        )
    assert out == "Hello world"


async def test_complete_openai_wire_accumulates_sse_text() -> None:
    # The cheapest catalog model can be an OpenAI one — the SAME complete()
    # speaks the Responses dialect: /openai ingress, instructions/input body,
    # response.output_text.delta frames.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/responses"
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["instructions"] == "s"
        assert body["input"] == [{"role": "user", "content": "hi"}]
        assert body["max_output_tokens"] == 256
        assert body["stream"] is True
        assert "system" not in body and "messages" not in body  # no Anthropic leak
        return httpx.Response(200, content=_openai_sse("Hel", "lo ", "world"))

    async with _client(handler) as c:
        out = await complete(
            gateway_url="http://gw",
            token="tok",
            model="gpt-5-nano",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            wire="openai",
            client=c,
        )
    assert out == "Hello world"


async def test_complete_openai_ignores_non_text_frames_and_garbage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        frames = (
            'data: {"type":"response.output_item.added","item":{}}\n\n'
            "data: not-json\n\n"
            'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            'data: {"type":"response.output_text.delta","delta":42}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=frames.encode())

    async with _client(handler) as c:
        out = await complete(
            gateway_url="http://gw",
            token="t",
            model="m",
            system="s",
            messages=[],
            wire="openai",
            client=c,
        )
    assert out == "ok"


async def test_complete_openai_maps_402_and_401_like_anthropic() -> None:
    # The error mapping is wire-agnostic — pin it explicitly on the openai path.
    for status, exc_type in ((402, GatewayInsufficientCreditError), (401, GatewayAuthError)):

        def handler(request: httpx.Request, _s: int = status) -> httpx.Response:
            return httpx.Response(_s, json={"error": "x"})

        async with _client(handler) as c:
            with pytest.raises(exc_type):
                await complete(
                    gateway_url="http://gw",
                    token="t",
                    model="m",
                    system="s",
                    messages=[],
                    wire="openai",
                    client=c,
                )


async def test_complete_402_is_insufficient_credit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "insufficient credit"})

    async with _client(handler) as c:
        with pytest.raises(GatewayInsufficientCreditError):
            await complete(
                gateway_url="http://gw", token="t", model="m", system="s", messages=[], client=c
            )


async def test_complete_401_is_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    async with _client(handler) as c:
        with pytest.raises(GatewayAuthError):
            await complete(
                gateway_url="http://gw", token="t", model="m", system="s", messages=[], client=c
            )


# --- the judge --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "decision", "injection"),
    [
        (
            '{"decision":"allow","reason":"ordinary edit","injection_suspected":false}',
            "allow",
            False,
        ),
        (
            '{"decision":"block","reason":"exfiltrates secrets","injection_suspected":false}',
            "block",
            False,
        ),
        # injection suspicion forces a block even if it claimed allow
        ('{"decision":"allow","reason":"x","injection_suspected":true}', "block", True),
        # prose around the JSON is tolerated
        ('Sure! {"decision":"allow","reason":"ok"} done', "allow", False),
        # unparseable / no decision → fail closed to block
        ("not json at all", "block", False),
        ('{"reason":"no decision field"}', "block", False),
    ],
)
def test_parse_verdict(text: str, decision: str, injection: bool) -> None:
    v = _parse_verdict(text)
    assert v.decision == decision
    assert v.injection_suspected == injection


async def test_judge_builds_prompt_and_returns_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def _fake_complete(**kw: object) -> str:
        seen.update(kw)
        return '{"decision":"allow","reason":"normal build"}'

    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _fake_complete)
    # An explicit model skips the catalog fetch (the resolution path is its own test).
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="tok", model="some-cheap-model")
    verdict = await judge.judge(classify_command("touch x"), "create a scratch file")
    assert isinstance(verdict, JudgeVerdict)
    assert verdict.decision == "allow"
    assert seen["model"] == "some-cheap-model"
    user = seen["messages"][0]["content"]  # type: ignore[index]
    assert "create a scratch file" in user
    assert "touch x" in user


async def test_judge_propagates_credit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _broke(**kw: object) -> str:
        raise GatewayInsufficientCreditError("no credit")

    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _broke)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="t", model="m")
    with pytest.raises(GatewayInsufficientCreditError):
        await judge.judge(classify_command("touch x"), "goal")


# --- model selection from the gateway catalog -------------------------------


def _model(model_id: str, tier: str, wire: str = "anthropic") -> object:
    from alkera_cli.contracts.gateway_model import GatewayModel

    return GatewayModel(id=model_id, display_name=model_id, wire=wire, tier=tier)  # type: ignore[arg-type]


def test_pick_judge_model_prefers_cheap_then_alphabetical() -> None:
    from alkera_cli.harness.safety_judge import pick_judge_model

    models = [
        _model("z-frontier", "frontier"),
        _model("b-standard", "standard"),
        _model("m-cheap", "cheap"),
        _model("a-cheap", "cheap"),  # alphabetical within the cheap tier
    ]
    chosen = pick_judge_model(models)  # type: ignore[arg-type]
    assert chosen is not None and chosen.id == "a-cheap"


def test_pick_judge_model_falls_through_tiers() -> None:
    from alkera_cli.harness.safety_judge import pick_judge_model

    # no cheap → standard; no standard → frontier; empty → None
    s = pick_judge_model([_model("s", "standard"), _model("f", "frontier")])  # type: ignore[arg-type]
    assert s is not None and s.id == "s"
    f = pick_judge_model([_model("f2", "frontier"), _model("f1", "frontier")])  # type: ignore[arg-type]
    assert f is not None and f.id == "f1"
    assert pick_judge_model([]) is None


async def test_judge_resolves_model_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_models(**kw: object) -> list[object]:
        return [_model("frontier-x", "frontier"), _model("cheap-y", "cheap")]

    seen: dict[str, object] = {}

    async def _fake_complete(**kw: object) -> str:
        seen.update(kw)
        return '{"decision":"allow"}'

    monkeypatch.setattr("alkera_cli.harness.safety_judge.fetch_models", _fake_models)
    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _fake_complete)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="t")  # no explicit model
    await judge.judge(classify_command("touch x"), "goal")
    assert seen["model"] == "cheap-y"  # the cheapest from the catalog
    assert seen["wire"] == "anthropic"


async def test_concurrent_judge_calls_fetch_the_catalog_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two simultaneous tool-asks in one auto-mode session must resolve the model
    # from the catalog exactly ONCE — the lazy fetch is lock-guarded, so the
    # second caller waits on the first instead of firing a redundant round-trip.
    import asyncio

    fetches = 0

    async def _fake_models(**kw: object) -> list[object]:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.02)  # widen the race window the lock must close
        return [_model("cheap-y", "cheap")]

    async def _fake_complete(**kw: object) -> str:
        return '{"decision":"allow"}'

    monkeypatch.setattr("alkera_cli.harness.safety_judge.fetch_models", _fake_models)
    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _fake_complete)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="t")  # no explicit model
    verdicts = await asyncio.gather(
        judge.judge(classify_command("touch a"), "goal"),
        judge.judge(classify_command("touch b"), "goal"),
        judge.judge(classify_command("touch c"), "goal"),
    )
    assert fetches == 1, "concurrent judge calls each fetched the catalog"
    assert all(v.decision == "allow" for v in verdicts)


async def test_judge_uses_openai_wire_when_cheapest_is_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A catalog whose cheapest model is an OpenAI one must drive the judge down
    # the /openai Responses path — not post a GPT id to the Anthropic ingress.
    async def _fake_models(**kw: object) -> list[object]:
        return [
            _model("claude-sonnet", "standard", wire="anthropic"),
            _model("gpt-5-nano", "cheap", wire="openai"),
        ]

    seen: dict[str, object] = {}

    async def _fake_complete(**kw: object) -> str:
        seen.update(kw)
        return '{"decision":"allow"}'

    monkeypatch.setattr("alkera_cli.harness.safety_judge.fetch_models", _fake_models)
    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _fake_complete)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="t")
    verdict = await judge.judge(classify_command("touch x"), "goal")
    assert verdict.decision == "allow"
    assert seen["model"] == "gpt-5-nano"
    assert seen["wire"] == "openai"


async def test_judge_stops_turn_when_catalog_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(**kw: object) -> list[object]:
        return []

    monkeypatch.setattr("alkera_cli.harness.safety_judge.fetch_models", _empty)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="t")
    with pytest.raises(GatewayUnavailableError):
        await judge.judge(classify_command("touch x"), "goal")


# --- enriched judge context: workspace scope ("what it's given") ------------


@pytest.mark.parametrize(
    ("name", "kind", "root", "expected"),
    [
        pytest.param("src/app.py", "file", "/ws", "inside", id="relative-inside"),
        pytest.param("/ws/sub/x.txt", "file", "/ws", "inside", id="absolute-inside"),
        pytest.param("/etc/passwd", "file", "/ws", "outside", id="absolute-outside"),
        pytest.param("../escape", "file", "/ws", "outside", id="dotdot-escapes-workspace"),
        pytest.param("analytics.events", "table", "/ws", "unknown", id="sql-table-not-a-path"),
        pytest.param("x", "file", None, "unknown", id="no-workspace-root"),
        pytest.param("", "file", "/ws", "unknown", id="empty-name"),
    ],
)
def test_target_scope(name: str, kind: str, root: str | None, expected: str) -> None:
    assert _target_scope(ResourceRef(kind=kind, name=name), root) == expected


def test_build_judge_payload_includes_scope_and_context() -> None:
    d = ActionDescriptor(
        capability="fs",
        effect=Effect.WRITE,
        operation="edit",
        raw="write the file",
        targets=[
            ResourceRef(kind="file", name="src/app.py"),
            ResourceRef(kind="file", name="/etc/hosts"),
        ],
        reasons=["edit"],
    )
    payload = _build_judge_payload(d, "build the app", workspace_root="/ws")
    assert payload["workspace_root"] == "/ws"
    action = payload["action"]
    assert isinstance(action, dict)
    assert action["effect"] == "write"
    scopes = {t["name"]: t["scope"] for t in action["targets"]}
    assert scopes == {"src/app.py": "inside", "/etc/hosts": "outside"}


async def test_judge_threads_workspace_and_scope_into_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end at the judge level: the workspace root + per-target scope reach
    # the metered call's user message, so the model can see an out-of-workspace
    # write for what it is.
    seen: dict[str, object] = {}

    async def _fake_complete(**kw: object) -> str:
        seen.update(kw)
        return '{"decision":"allow","reason":"ok"}'

    monkeypatch.setattr("alkera_cli.harness.safety_judge.complete", _fake_complete)
    judge = GatewaySafetyJudge(gateway_url="http://gw", token="tok", model="m")
    d = ActionDescriptor(
        capability="fs",
        effect=Effect.WRITE,
        operation="edit",
        raw="overwrite /etc/shadow",
        targets=[ResourceRef(kind="file", name="/etc/shadow")],
    )
    await judge.judge(d, "goal", workspace_root="/ws")
    user = json.loads(seen["messages"][0]["content"])  # type: ignore[index]
    assert user["workspace_root"] == "/ws"
    assert user["action"]["targets"][0]["scope"] == "outside"
