"""The editor's model switch over the daemon's JSON-RPC: the same rule as the cloud.

A real daemon server over a real runtime with a FakeAdapter; the gateway catalog
is the faked boundary (``REMEMBERED_CATALOG`` is what ``harness.list_models``
leaves behind, and the read itself is stubbed). The chat's reasoning history is
the transcript's own stamped replies.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
import test_daemon_harness
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.methods import harness as harness_methods
from alkera_cli.daemon.methods.harness import HarnessListModelsResponse
from alkera_cli.gateway.client import REMEMBERED_CATALOG
from alkera_core.schemas.chat import MessageCreated, PartCreated, ReasoningPart
from test_daemon_harness import _drain_until, _recv, _send

pytestmark = pytest.mark.asyncio

# The harness suite's daemon-over-a-fake-adapter fixture, shared rather than copied.
daemon_with_fake = test_daemon_harness.daemon_with_fake

OPUS_55 = GatewayModel(
    id="claude-opus-5.5",
    display_name="Claude Opus 5.5",
    wire="anthropic",
    efforts=("low", "high"),
    default_effort="high",
    reasoning_format="anthropic:claude-opus-5-5",
    reads_reasoning_formats=("anthropic:claude-opus-5",),
)
FABLE_51 = GatewayModel(
    id="claude-fable-5.1",
    display_name="Claude Fable 5.1",
    wire="anthropic",
    reasoning_format="anthropic:claude-fable-5-1",
    reads_reasoning_formats=("anthropic:claude-opus-5-5",),
)
GPT = GatewayModel(
    id="gpt-5.5", display_name="GPT-5.5", wire="openai", reasoning_format="openai:gpt-5.5"
)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def listed(server: Any, params: Any) -> HarnessListModelsResponse:
        REMEMBERED_CATALOG.remember([OPUS_55, FABLE_51, GPT])
        return HarnessListModelsResponse()

    monkeypatch.setattr(harness_methods, "harness_list_models", listed)
    import alkera_cli.daemon.methods.model_switch as switch

    monkeypatch.setattr(switch, "harness_list_models", listed)


async def _call(cw: Any, cr: Any, rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await _send(cw, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return await _drain_until(cr, lambda f: f.get("id") == rid)


async def _open(cw: Any, cr: Any, project_path: Any) -> str:
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "harness.open_chat",
            "params": {
                "project_path": str(project_path),
                "create": True,
                "harness": "alkera",
                "title": "switch",
                "model": {
                    "id": OPUS_55.id,
                    "display_name": OPUS_55.display_name,
                    "wire": "anthropic",
                    "efforts": ["low", "high"],
                    "default_effort": "high",
                },
                "effort": "high",
            },
        },
    )
    while True:
        frame = await asyncio.wait_for(_recv(cr), timeout=10)
        if frame.get("id") == 1:
            return str(frame["result"]["session_id"])


async def _replied(factory: Any, sid: str, model_id: str | None, *, reasoned: bool = True) -> None:
    """A reply on ``model_id`` (or unstamped), with the reasoning part it
    produced unless the model ran with thinking off."""
    adapter = factory.adapters[0]
    await adapter.feed(
        MessageCreated(
            event_id=f"m-{model_id}",
            time=datetime.now(UTC),
            session_id=sid,
            message_id=f"msg-{model_id}",
            role="assistant",
            model={"provider_id": "alkera-anthropic", "model_id": model_id} if model_id else {},
        )
    )
    if reasoned:
        await adapter.feed(
            PartCreated(
                event_id=f"p-{model_id}",
                time=datetime.now(UTC),
                session_id=sid,
                part=ReasoningPart(part_id=f"prt-{model_id}", message_id=f"msg-{model_id}"),
            )
        )
    await asyncio.sleep(0.05)


def _selection(model: GatewayModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "display_name": model.display_name,
        "wire": model.wire,
        "efforts": list(model.efforts),
        "default_effort": model.default_effort,
    }


async def test_the_options_carry_the_same_verdicts_as_the_cloud(daemon_with_fake: Any) -> None:
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)
    await _replied(factory, sid, OPUS_55.id)

    got = (await _call(cw, cr, 30, "harness.model_options", {"session_id": sid}))["result"]

    states = {o["model"]["id"]: (o["state"], o["reason_code"]) for o in got["options"]}
    assert states == {
        OPUS_55.id: ("current", None),
        FABLE_51.id: ("available", None),
        GPT.id: ("unavailable", "reasoning_not_readable"),
    }
    gpt = next(o for o in got["options"] if o["model"]["id"] == GPT.id)
    assert gpt["message"] == (
        "This chat has reasoning from Claude Opus 5.5 that GPT-5.5 can't read. "
        "Start a new chat to use GPT-5.5."
    )
    assert got["current_model_id"] == OPUS_55.id
    assert gpt["group_message"] == (
        "This chat has reasoning from Claude Opus 5.5 that these models can't read. "
        "Use them in a new chat."
    )


async def test_a_reply_with_no_reasoning_leaves_the_chat_free(daemon_with_fake: Any) -> None:
    """A model run with thinking off produced nothing to lose: the editor's
    picker, like the cloud's, offers every model."""
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)
    await _replied(factory, sid, OPUS_55.id, reasoned=False)

    got = (await _call(cw, cr, 34, "harness.model_options", {"session_id": sid}))["result"]

    assert {o["model"]["id"]: o["state"] for o in got["options"]}[GPT.id] == "available"


@pytest.mark.parametrize(
    ("ran", "target", "refused_with"),
    [
        pytest.param("claude-opus-5.5", GPT, "reasoning_not_readable", id="history-blocks"),
        pytest.param("claude-opus-5.5", FABLE_51, None, id="the-target-reads-it"),
        pytest.param(None, GPT, "reasoning_not_readable", id="an-unstamped-reply-ran-on-the-pin"),
    ],
)
async def test_set_model_moves_the_chat_or_answers_the_refusal(
    daemon_with_fake: Any, ran: str | None, target: GatewayModel, refused_with: str | None
) -> None:
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)
    await _replied(factory, sid, ran)

    got = await _call(
        cw, cr, 31, "harness.set_model", {"session_id": sid, "model": _selection(target)}
    )

    manifest = json.loads(
        (project_path / ".alkera" / "chats" / sid / "manifest.json").read_text(encoding="utf-8")
    )["model"]
    if refused_with is None:
        assert got["result"]["model"]["model_id"] == target.id
        assert manifest["model_id"] == target.id
    else:
        assert got["error"]["code"] == -32010
        assert got["error"]["data"]["code"] == refused_with
        assert got["error"]["data"]["escape_new_chat_model"] == target.id
        assert manifest["model_id"] == OPUS_55.id, "a refusal moves nothing"


async def test_an_effort_change_on_the_same_model_is_never_refused(daemon_with_fake: Any) -> None:
    _server, cw, cr, factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)
    await _replied(factory, sid, "a-model-the-catalog-lost")

    got = await _call(
        cw,
        cr,
        32,
        "harness.set_model",
        {"session_id": sid, "model": _selection(OPUS_55), "effort": "low"},
    )

    assert got["result"]["model"]["effort"] == "low"


async def test_a_switch_raced_by_another_is_refused(daemon_with_fake: Any) -> None:
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)

    got = await _call(
        cw,
        cr,
        33,
        "harness.set_model",
        {"session_id": sid, "model": _selection(FABLE_51), "expected_model_id": GPT.id},
    )

    assert got["error"]["data"]["code"] == "model_changed"


@pytest.mark.parametrize(
    ("target", "effort"),
    [
        pytest.param(OPUS_55, "ultra", id="an-effort-the-current-model-lacks"),
        pytest.param(FABLE_51, "high", id="any-effort-on-a-model-that-offers-none"),
    ],
)
async def test_an_effort_the_model_does_not_offer_is_refused_and_moves_nothing(
    daemon_with_fake: Any, target: GatewayModel, effort: str
) -> None:
    """Refused as the cloud refuses it, never swapped for the model's default:
    the person asked for an effort and would otherwise run on another one with
    nothing saying so."""
    _server, cw, cr, _factory, project_path, _stop = daemon_with_fake
    sid = await _open(cw, cr, project_path)

    got = await _call(
        cw,
        cr,
        34,
        "harness.set_model",
        {"session_id": sid, "model": _selection(target), "effort": effort},
    )

    manifest = json.loads(
        (project_path / ".alkera" / "chats" / sid / "manifest.json").read_text(encoding="utf-8")
    )["model"]
    assert got["error"]["code"] == -32010
    assert got["error"]["data"]["code"] == "effort_not_offered"
    assert got["error"]["data"]["efforts"] == list(target.efforts)
    assert (manifest["model_id"], manifest["effort"]) == (OPUS_55.id, "high")
