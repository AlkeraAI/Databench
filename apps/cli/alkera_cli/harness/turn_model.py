"""The model and reasoning effort a turn is sent with, and the one it ran on.

A chat's model and effort can move while its agent runs (a reader's pick in an
open chat, an effort chip, a switch the box follows from the chat row). No agent
re-reads the chat's pin, so every turn carries it, and every harness applies it
per turn:

- **opencode** takes the turn's model nested (``model: {providerID, modelID}``)
  with the effort in the model key (``<id>::<effort>``). Its config is read
  once, at spawn: a host whose agent was spawned without the pinned model opens
  it again before the turn (:func:`config_declares`).
- **claude** takes ``<model>::<effort>::<display>`` as its model string. The
  SDK's ``set_model`` cannot carry a change through the gateway: claude
  validates a new model with a non-streaming probe, the gateway answers every
  request as a stream, and the probe fails after the gateway has billed it. So
  the adapter spawns its client again on the new string and resumes the
  conversation.

A model switch keeps the reasoning formats of the models the chat moved off
(:func:`with_reasoning_history`), so a config rebuilt from the pin alone still
tells opencode which earlier reasoning the new model reads as written.

What a turn ran on is stamped on the transcript as ``{provider_id, model_id,
effort?}`` (string values only, so the event schemas need no new version).

Everything here is pure: plain data in, plain data out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alkera_core.gateway import join_model_effort, split_model_effort, split_model_variant

Model = dict[str, str]


#: The manifest-model key holding ``{base model id: reasoning format}`` for the
#: models a chat moved off. Only formats travel, never reasoning.
REASONING_HISTORY_KEY = "reasoning_formats_by_model"


def reasoning_history(manifest_model: Mapping[str, Any]) -> dict[str, str]:
    """The formats of the models a chat ran on before its current one."""
    held = manifest_model.get(REASONING_HISTORY_KEY)
    if not isinstance(held, Mapping):
        return {}
    return {k: v for k, v in held.items() if isinstance(k, str) and isinstance(v, str) and v}


def with_reasoning_history(
    current: Mapping[str, Any] | None, selection: Mapping[str, Any]
) -> dict[str, Any]:
    """``selection`` carrying the formats of every model the chat has run on
    but is moving off: those ``current`` already carried, plus ``current``'s own."""
    known = reasoning_history(current or {})
    known.update(reasoning_history(selection))
    model_id, fmt = (current or {}).get("model_id"), (current or {}).get("reasoning_format")
    if isinstance(model_id, str) and isinstance(fmt, str) and fmt:
        known[split_model_effort(model_id)[0]] = fmt
    target = split_model_effort(str(selection.get("model_id") or ""))[0]
    known.pop(target, None)
    out = dict(selection)
    if known:
        out[REASONING_HISTORY_KEY] = known
    return out


def pinned_turn_model(manifest_model: Mapping[str, Any]) -> tuple[Model, str | None] | None:
    """The chat's pin as a turn sends it: ``({provider_id, model_id}, effort)``,
    with the base model id. ``None`` when the manifest names no model and the
    harness runs its own default.

    The effort follows the rule the spawn-time config default follows
    (``resolve_runtime_effort``): the picked effort when the model offers it,
    else none, so a turn never names a config entry that does not exist."""
    provider_id = manifest_model.get("provider_id")
    model_id = manifest_model.get("model_id")
    if not (isinstance(provider_id, str) and provider_id and isinstance(model_id, str)):
        return None
    base, carried = split_model_effort(model_id)
    if not base:
        return None
    efforts = [e for e in manifest_model.get("efforts") or [] if isinstance(e, str)]
    effort = manifest_model.get("effort")
    if not (isinstance(effort, str) and effort in efforts):
        effort = carried if carried in efforts else None
    return {"provider_id": provider_id, "model_id": base}, effort


def pinned_model_key(manifest_model: Mapping[str, Any]) -> Model | None:
    """The pin as the model key a turn names (``{provider_id, model_id}`` with
    ``<id>::<effort>``), or ``None`` when the manifest names no model."""
    pinned = pinned_turn_model(manifest_model)
    if pinned is None:
        return None
    model, effort = pinned
    return {**model, "model_id": join_model_effort(model["model_id"], effort)}


def turn_model(
    prompt_model: Mapping[str, str] | None,
    variant: str | None,
    manifest_model: Mapping[str, Any],
) -> tuple[Model | None, str | None]:
    """The model and effort a turn is sent with. A caller that names a model is
    obeyed; otherwise the turn carries the pin as the manifest holds it now. A
    caller's effort rides on the pinned model; with none, the pinned effort
    does."""
    if prompt_model:
        return dict(prompt_model), variant
    pinned = pinned_turn_model(manifest_model)
    if pinned is None:
        return None, variant
    model, effort = pinned
    return model, (variant if variant is not None else effort)


def config_declares(config: Mapping[str, Any], model: Mapping[str, str]) -> bool:
    """Whether an opencode config declares ``model`` (``{provider_id, model_id}``,
    the model id as it will be sent, effort and all: each effort is its own
    entry)."""
    provider = config.get("provider")
    entry = (
        provider.get(str(model.get("provider_id") or "")) if isinstance(provider, dict) else None
    )
    models = entry.get("models") if isinstance(entry, dict) else None
    return isinstance(models, dict) and str(model.get("model_id") or "") in models


def opencode_model_ref(model: Mapping[str, str]) -> dict[str, str]:
    """A ``{provider_id, model_id}`` as opencode's prompt takes it: nested, as
    ``model: {providerID, modelID}``. Its schema drops top-level ids silently."""
    return {"providerID": model["provider_id"], "modelID": model["model_id"]}


def ran_model_stamp(info: Mapping[str, Any]) -> Model:
    """The stamp of the model an opencode reply ran on, from its message info
    (``providerID`` and the model key ``<id>::<effort>[::<display>]`` it was filed
    under). Empty when either is missing (an older harness, a synthetic message)."""
    provider_id, model_key = info.get("providerID"), info.get("modelID")
    if not isinstance(provider_id, str) or not isinstance(model_key, str) or not model_key:
        return {}
    model_id, effort, _display = split_model_variant(model_key)
    stamp = {"provider_id": provider_id, "model_id": model_id}
    if effort:
        stamp["effort"] = effort
    return stamp


__all__ = [
    "REASONING_HISTORY_KEY",
    "config_declares",
    "opencode_model_ref",
    "pinned_model_key",
    "pinned_turn_model",
    "ran_model_stamp",
    "reasoning_history",
    "turn_model",
    "with_reasoning_history",
]
