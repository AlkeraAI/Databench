"""Whether a chat may move from one model to another: the one checker.

The rule: a chat that has reasoning history from a model may not move to
a model that would lose that reasoning. There is no "allowed, but the reasoning
is dropped" state. A chat with no reasoning history yet may move freely, and an
effort change on the same model is always allowed.

Reasoning continuity is model-bound and directional (an Opus 5.5 thinking block
is read by Fable 5.1, not the reverse), so it is modelled as catalog data rather
than as provider classes:

- ``reasoning_format``: the identity of the replayable reasoning a model emits
  (``None`` = none, so a turn on it adds nothing to a chat's history);
- ``reads_reasoning_formats``: the OTHER formats it reads when they are replayed.
  A model always reads its own; a model with no list reads only its own, which
  is the default for anything not proven otherwise.

The chat's *ledger* is the set of formats its replayed history carries. Three
independent checks decide a switch, each with its own reason code:

1. offered: the target is in the catalog this caller may use;
2. harness: the chat's agent can drive the target's wire mid-session
   (:mod:`alkera_core.chat_models.harnesses`);
3. reasoning: the target reads every format in the ledger.

Pure: no I/O, no globals. The server enforces it, the box re-checks it, and every
surface renders the server's verdicts with :func:`message_for`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

ReasonCode = Literal["model_not_offered", "harness_wire_unsupported", "reasoning_not_readable"]
SwitchState = Literal["current", "available", "unavailable"]

#: Every reason a switch can be refused, in the order the checks run.
REASON_CODES: tuple[ReasonCode, ...] = (
    "model_not_offered",
    "harness_wire_unsupported",
    "reasoning_not_readable",
)


@dataclass(frozen=True, slots=True)
class ModelFacts:
    """What the checker knows about one model."""

    id: str
    display_name: str
    wire: str
    efforts: tuple[str, ...] = ()
    reasoning_format: str | None = None
    reads_reasoning_formats: frozenset[str] = frozenset()

    @property
    def readable(self) -> frozenset[str]:
        """Every format this model reads: its own plus the ones it lists."""
        own = {self.reasoning_format} if self.reasoning_format else set()
        return self.reads_reasoning_formats | own

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> ModelFacts:
        """From a catalog row or a chat pin (``id``, ``display_name``, ``wire``,
        ``efforts``, ``reasoning_format``, ``reads_reasoning_formats``)."""
        model_id = str(row.get("id") or "")
        display = row.get("display_name")
        fmt = row.get("reasoning_format")
        return cls(
            id=model_id,
            display_name=display if isinstance(display, str) and display else model_id,
            wire=str(row.get("wire") or ""),
            efforts=tuple(e for e in row.get("efforts") or () if isinstance(e, str)),
            reasoning_format=fmt if isinstance(fmt, str) and fmt else None,
            reads_reasoning_formats=frozenset(
                f for f in row.get("reads_reasoning_formats") or () if isinstance(f, str) and f
            ),
        )


@dataclass(frozen=True, slots=True)
class SwitchVerdict:
    state: SwitchState
    reason_code: ReasonCode | None = None
    #: Ledger formats the target cannot read (``reasoning_not_readable`` only).
    blocking_formats: tuple[str, ...] = ()
    #: The target's id when a new chat is the way to use it.
    escape_new_chat_model: str | None = None

    @property
    def allowed(self) -> bool:
        return self.state != "unavailable"


def evaluate_switch(
    current: ModelFacts | None,
    target: ModelFacts,
    *,
    ledger: Iterable[str],
    harness_wires: frozenset[str],
    offered: bool,
) -> SwitchVerdict:
    """The verdict on moving a chat on ``current`` (``None``: no pin yet) to
    ``target``. Staying on the same model is ``current``: an effort change is
    decided by :func:`evaluate_effort` alone and never on reasoning grounds."""
    if current is not None and target.id == current.id:
        return SwitchVerdict(state="current")
    if not offered:
        return SwitchVerdict(state="unavailable", reason_code="model_not_offered")
    if target.wire not in harness_wires:
        return SwitchVerdict(
            state="unavailable",
            reason_code="harness_wire_unsupported",
            escape_new_chat_model=target.id,
        )
    readable = target.readable
    blocking = tuple(sorted({f for f in ledger if f not in readable}))
    if blocking:
        return SwitchVerdict(
            state="unavailable",
            reason_code="reasoning_not_readable",
            blocking_formats=blocking,
            escape_new_chat_model=target.id,
        )
    return SwitchVerdict(state="available")


def evaluate_effort(current: ModelFacts, effort: str | None) -> bool:
    """Whether ``effort`` is one the current model offers (``None``: the model's
    own default, always allowed)."""
    return effort is None or effort in current.efforts


def ledger_after_turn(ledger: Iterable[str], ran: ModelFacts | None) -> list[str]:
    """The ledger once a turn ran on ``ran``: its format added, deduplicated, in
    insertion order. A model that emits no reasoning adds nothing."""
    out = list(dict.fromkeys(ledger))
    if ran is not None and ran.reasoning_format and ran.reasoning_format not in out:
        out.append(ran.reasoning_format)
    return out


def message_for(
    verdict: SwitchVerdict,
    target: ModelFacts,
    *,
    format_names: Mapping[str, str] | None = None,
) -> str | None:
    """The one sentence every surface shows for an unavailable target.
    ``format_names`` maps a reasoning format to the display name of the model
    that emits it; a format no model in the catalog emits is named as it is."""
    if verdict.reason_code is None:
        return None
    name = target.display_name
    if verdict.reason_code == "model_not_offered":
        return f"{name} isn't available in this workspace."
    if verdict.reason_code == "harness_wire_unsupported":
        return f"This chat's agent runs Anthropic models only. Start a new chat to use {name}."
    source = _sources(verdict, format_names)
    return (
        f"This chat has reasoning from {source} that {name} can't read. "
        f"Start a new chat to use {name}."
    )


def group_message_for(
    verdict: SwitchVerdict, *, format_names: Mapping[str, str] | None = None
) -> str | None:
    """The one line a picker shows above every model refused for the same
    reason: :func:`message_for` without the target's name, so the models that
    share it are listed under it once instead of each repeating it."""
    if verdict.reason_code is None:
        return None
    if verdict.reason_code == "model_not_offered":
        return "Not available in this workspace."
    if verdict.reason_code == "harness_wire_unsupported":
        return "This chat's agent runs Anthropic models only. Use these in a new chat."
    source = _sources(verdict, format_names)
    return (
        f"This chat has reasoning from {source} that these models can't read. "
        "Use them in a new chat."
    )


def _sources(verdict: SwitchVerdict, format_names: Mapping[str, str] | None) -> str:
    names = format_names or {}
    sources = list(dict.fromkeys(names.get(f, f) for f in verdict.blocking_formats))
    if len(sources) > 1:
        return ", ".join(sources[:-1]) + " and " + sources[-1]
    return sources[0] if sources else "another model"


__all__ = [
    "REASON_CODES",
    "ModelFacts",
    "ReasonCode",
    "SwitchState",
    "SwitchVerdict",
    "evaluate_effort",
    "evaluate_switch",
    "group_message_for",
    "ledger_after_turn",
    "message_for",
]
