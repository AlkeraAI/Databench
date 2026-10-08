"""Where a chat's agent session stands, from what the platform knows of it."""

from __future__ import annotations

from alkera_core.schemas.objects.specs import ChatSpec, MachineStatus, SessionState

#: The machine statuses under which the bound box is alive and answering: a
#: chat it slept reads ``asleep`` on that same live box.
BOX_ANSWERS = frozenset({"ready", "asleep"})
#: The machine statuses under which no box will open the chat however long it
#: waits: the box refused it, or nothing serves it.
NO_BOX_WILL_OPEN = frozenset({"refused", "stranded", "none"})


def chat_session_state(
    spec: ChatSpec, machine_status: MachineStatus, pending_turn: bool
) -> SessionState:
    """``awake`` or ``working`` only while the box the chat is bound to is
    ready and last said it holds the chat's session; ``queued`` while that
    box, alive, has said it has the chat's message but no free slot for it;
    ``waking`` while a message or a person's request waits for a box that
    can open it (never for one that refused it, or with no box at all),
    ``starting`` when no box has ever held its session (there is nothing to
    wake: a new chat, or one a passing fault keeps a box from opening);
    ``asleep`` otherwise. A box that said ``awake`` and then went quiet,
    drained or vanished no longer holds anything, so its word stops counting."""
    if spec.mirror_state == "awake" and machine_status == "ready":
        return "working" if pending_turn else "awake"
    if machine_status in NO_BOX_WILL_OPEN:
        # "Waking" over a chat nothing will open is a promise; the chat's
        # status says why it can't run.
        return "asleep"
    if spec.wake_requested_at or pending_turn:
        live = machine_status in BOX_ANSWERS
        if spec.slot_wait_at and live:
            return "queued"
        return "starting" if spec.mirror_state is None else "waking"
    return "asleep"


__all__ = ["chat_session_state"]
