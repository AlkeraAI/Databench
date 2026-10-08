"""The sentences a refusal is composed from, shared by the harness (which
answers the agent's own permission asks) and the in-tool gate (which refuses a
tool call): one vocabulary, so every surface recognises a refusal wherever it
was composed. Pure: nothing here imports the harness or a tool."""

from __future__ import annotations

#: A person's refusal, as the tool result and its card read it.
NOT_GRANTED = "Permission was not granted for this call."

#: A policy refusal that reached the reply with no reason of its own. A refusal
#: always carries one: the vendor's bare sentence would read as a person's.
POLICY_REFUSED = "The workspace policy refused this call."

#: Provenances a person stands behind, or that hand the turn back to one.
_PERSON_SIDE = frozenset({"human", "timeout", "broker", "cancelled"})


def person_decided(decided_by: str) -> bool:
    """Whether a decision is a person's — theirs, or one handed back to them
    (a prompt that ran out, a broker that failed, a turn they cancelled) —
    rather than the policy's."""
    return decided_by in _PERSON_SIDE


def refusal_feedback(decided_by: str, reason: str | None) -> str | None:
    """The one sentence a REJECT carries back to the harness — which the model
    reads as the tool result and the transcript card shows as the call's error.

    The policy's refusal is its reason alone (never framed as a person's
    decision), and never empty. A person's refusal carries their feedback after
    :data:`NOT_GRANTED`; with no feedback it carries nothing, because a
    reason-less reject is what ends the turn, and the harness's own sentence for
    it is :data:`NOT_GRANTED`."""
    text = (reason or "").strip()
    if decided_by == "human":
        return f"{NOT_GRANTED} {text}" if text else None
    if decided_by in _PERSON_SIDE:
        return text or None
    return text or POLICY_REFUSED


__all__ = [
    "NOT_GRANTED",
    "POLICY_REFUSED",
    "person_decided",
    "refusal_feedback",
]
