"""The editor's model switch on an open chat: the options and the switch itself.

The same rule the cloud enforces (``alkera_core.chat_models.switching``), with
the chat's reasoning ledger read off its own transcript: each reply the harness
ran is stamped with its model (``message.created`` from opencode,
``turn.started`` from the claude agent), and a reply with no stamp is taken to
have run on the chat's pin. The catalog is the gateway's, as ``harness.list_models``
reads it.

- ``harness.model_options{session_id}`` → every model the user may pick, each
  with whether the chat may move to it and why not (the same sentences the web
  shows), and when a switch applies.
- ``harness.set_model{session_id, model, effort?, expected_model_id?}`` → moves
  the chat (the next turn runs on it), or answers JSON-RPC ``-32010``
  (``MODEL_SWITCH_REFUSED``) with ``{code, message, blocking_formats,
  escape_new_chat_model}``. An effort the model does not offer is refused
  (``effort_not_offered``), as the cloud refuses it, rather than swapped for
  the model's default.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from alkera_core.chat_models.harnesses import capabilities_of
from alkera_core.chat_models.switching import (
    ModelFacts,
    SwitchVerdict,
    evaluate_switch,
    group_message_for,
    message_for,
)
from alkera_core.schemas.chat import MessageCreated, PartCreated, ReasoningPart, TurnStarted
from pydantic import Field

from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.daemon.methods.harness import (
    GatewayModelInfo,
    GatewayModelSelection,
    HarnessListModelsRequest,
    _require_session,
    harness_list_models,
)
from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.daemon.server import ModelSwitchRefusedRpcError
from alkera_cli.gateway.client import REMEMBERED_CATALOG, selectable_models
from alkera_cli.harness.adapters.opencode_alkera import build_manifest_model

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer
    from alkera_cli.harness import ChatSession


class HarnessModelOptionsRequest(_DaemonModel):
    session_id: str


class HarnessModelOption(_DaemonModel):
    model: GatewayModelInfo
    state: Literal["current", "available", "unavailable"]
    reason_code: (
        Literal["model_not_offered", "harness_wire_unsupported", "reasoning_not_readable"] | None
    ) = None
    message: str | None = None
    group_message: str | None = None
    escape_new_chat_model: str | None = None


class HarnessModelOptionsResponse(_DaemonModel):
    current_model_id: str | None = None
    current_effort: str | None = None
    can_switch: bool = True
    applies: Literal["next_turn", "after_reopen"] = "next_turn"
    options: list[HarnessModelOption] = Field(default_factory=list)


class HarnessSetModelRequest(_DaemonModel):
    session_id: str
    model: GatewayModelSelection
    effort: str | None = None
    expected_model_id: str | None = None


class HarnessSetModelResponse(_DaemonModel):
    model: dict[str, Any]
    """The manifest's pinned model dict after the switch."""
    applies: Literal["next_turn", "after_reopen"] = "next_turn"


def _facts(model: GatewayModel) -> ModelFacts:
    return ModelFacts(
        id=model.id,
        display_name=model.display_name,
        wire=model.wire,
        efforts=tuple(model.efforts),
        reasoning_format=model.reasoning_format,
        reads_reasoning_formats=frozenset(model.reads_reasoning_formats),
    )


def ran_models(session: ChatSession) -> tuple[set[str], bool]:
    """The model ids whose replies in this chat's own session produced reasoning
    (a reply with no reasoning part leaves nothing to lose; a claude turn counts
    whenever it ran), and whether such a reply carries no stamp."""
    stamped: set[str] = set()
    unstamped = False
    events = [e for e in session.events() if e.session_id == session.session_id]
    reasoned = {
        e.part.message_id
        for e in events
        if isinstance(e, PartCreated) and isinstance(e.part, ReasoningPart)
    }
    for event in events:
        if isinstance(event, MessageCreated) and event.role == "assistant":
            if event.message_id not in reasoned:
                continue  # this reply produced no reasoning to lose
            model_id = event.model.get("model_id")
        elif isinstance(event, TurnStarted):
            model_id = (event.model or {}).get("model_id")
        else:
            continue
        if isinstance(model_id, str) and model_id:
            stamped.add(model_id)
        else:
            unstamped = True
    return stamped, unstamped


def ledger_of(
    stamped: Iterable[str], unstamped: bool, pinned_id: str | None, catalog: Sequence[GatewayModel]
) -> list[str]:
    """The reasoning formats the chat's history carries; a model the catalog no
    longer lists contributes a format nothing reads."""
    by_id = {m.id: m for m in catalog}
    ran = set(stamped) | ({pinned_id} if unstamped and pinned_id else set())
    formats: list[str] = []
    for model_id in sorted(ran):
        known = by_id.get(model_id)
        fmt = known.reasoning_format if known is not None else f"model:{model_id}"
        if fmt and fmt not in formats:
            formats.append(fmt)
    return formats


async def _catalog(server: JsonRpcServer) -> list[GatewayModel]:
    """The catalog ``harness.list_models`` reads (it raises AUTH_REQUIRED when
    signed out); the read leaves it remembered with every catalog field."""
    await harness_list_models(server, HarnessListModelsRequest())
    return selectable_models(list(REMEMBERED_CATALOG.models))


def _verdicts(
    session: ChatSession, catalog: Sequence[GatewayModel]
) -> list[tuple[GatewayModel, SwitchVerdict]]:
    pinned_id = str(session.manifest.model.get("model_id") or "") or None
    stamped, unstamped = ran_models(session)
    ledger = ledger_of(stamped, unstamped, pinned_id, catalog)
    current = next((m for m in catalog if m.id == pinned_id), None)
    wires = capabilities_of(session.manifest.harness_type).wires
    return [
        (
            m,
            evaluate_switch(
                _facts(current) if current is not None else None,
                _facts(m),
                ledger=ledger,
                harness_wires=wires,
                offered=True,
            ),
        )
        for m in catalog
    ]


def _applies(session: ChatSession, model: GatewayModel, effort: str | None) -> str:
    """Whether the running agent carries ``model``: if not, the switch runs once
    the chat is opened again (the agent reads its model list at spawn)."""
    served = session.serves_pinned_model(build_manifest_model(model, effort))
    return "next_turn" if served else "after_reopen"


def _info(m: GatewayModel) -> GatewayModelInfo:
    return GatewayModelInfo(
        id=m.id,
        display_name=m.display_name,
        wire=m.wire,
        efforts=list(m.efforts),
        default_effort=m.default_effort,
    )


@method("harness.model_options")
async def harness_model_options(
    server: JsonRpcServer, params: HarnessModelOptionsRequest
) -> HarnessModelOptionsResponse:
    session = _require_session(server, params.session_id)
    catalog = await _catalog(server)
    names = {m.reasoning_format: m.display_name for m in catalog if m.reasoning_format}
    verdicts = _verdicts(session, catalog)
    reachable = [m for m, v in verdicts if v.state == "available"]
    applies = (
        "after_reopen"
        if any(_applies(session, m, m.default_effort) == "after_reopen" for m in reachable)
        else "next_turn"
    )
    return HarnessModelOptionsResponse(
        current_model_id=session.manifest.model.get("model_id"),
        current_effort=session.manifest.model.get("effort"),
        applies=applies,  # type: ignore[arg-type]
        options=[
            HarnessModelOption(
                model=_info(m),
                state=v.state,
                reason_code=v.reason_code,
                message=message_for(v, _facts(m), format_names=names),
                group_message=group_message_for(v, format_names=names),
                escape_new_chat_model=v.escape_new_chat_model,
            )
            for m, v in verdicts
        ],
    )


@method("harness.set_model")
async def harness_set_model(
    server: JsonRpcServer, params: HarnessSetModelRequest
) -> HarnessSetModelResponse:
    session = _require_session(server, params.session_id)
    pinned_id = session.manifest.model.get("model_id")
    if params.expected_model_id is not None and params.expected_model_id != pinned_id:
        raise ModelSwitchRefusedRpcError(
            "Someone moved this chat onto another model. Pick again.",
            {"code": "model_changed", "model": pinned_id},
        )
    catalog = await _catalog(server)
    verdict = dict((m.id, (m, v)) for m, v in _verdicts(session, catalog)).get(params.model.id)
    if verdict is None:
        raise ModelSwitchRefusedRpcError(
            f"{params.model.display_name or params.model.id} isn't available in this workspace.",
            {"code": "model_not_offered", "model": params.model.id},
        )
    model, outcome = verdict
    if not outcome.allowed:
        names = {m.reasoning_format: m.display_name for m in catalog if m.reasoning_format}
        raise ModelSwitchRefusedRpcError(
            message_for(outcome, _facts(model), format_names=names) or "",
            {
                "code": outcome.reason_code,
                "model": model.id,
                "blocking_formats": list(outcome.blocking_formats),
                "escape_new_chat_model": outcome.escape_new_chat_model,
            },
        )
    if params.effort is not None and params.effort not in model.efforts:
        # Refused as the cloud route refuses it (422 ``effort_not_offered``),
        # never swapped for the model's default behind the person's back.
        offered = ", ".join(model.efforts) or "none"
        raise ModelSwitchRefusedRpcError(
            f"{model.display_name} doesn't offer the {params.effort!r} effort (offers: {offered})",
            {
                "code": "effort_not_offered",
                "model": model.id,
                "effort": params.effort,
                "efforts": list(model.efforts),
            },
        )
    selection = build_manifest_model(model, params.effort)
    await session.set_model(selection)
    return HarnessSetModelResponse(
        model=dict(session.manifest.model),
        applies=_applies(session, model, selection.get("effort")),  # type: ignore[arg-type]
    )


__all__ = [
    "HarnessModelOption",
    "HarnessModelOptionsRequest",
    "HarnessModelOptionsResponse",
    "HarnessSetModelRequest",
    "HarnessSetModelResponse",
    "harness_model_options",
    "harness_set_model",
    "ledger_of",
    "ran_models",
]
