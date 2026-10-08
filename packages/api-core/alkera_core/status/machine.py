"""Where a machine stands, as its card and the platform fleet draw it.

A starting step said "about 1 minute" for fourteen, because the estimate is a
constant and nothing held it against the clock; a revoked credential read
"Starting…" because nothing decided a word for it and the client filled one
in. Here a step's sentence carries its estimate only while the estimate still
holds, a step past it says so, and every state a machine can be in has its
own words.

Pure: every fact arrives in :class:`MachineEvidence` and the clock is a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alkera_core.status.contract import Generic, ReasonDef, StateDef, StatusFact, register

SUBJECT: Final = "machine"

#: A step's name as a sentence opens.
_STEP_WORDS: Final[dict[str, str]] = {
    "reserving": "Reserving hardware",
    "booting": "Booting",
    "installing": "Installing Alkera",
    "connecting": "Connecting",
}

_STARTING_REASONS = {
    step: ReasonDef(f"{words}, about {{minutes}}.") for step, words in _STEP_WORDS.items()
} | {
    f"{step}_overdue": ReasonDef(f"{words} is taking longer than usual.")
    for step, words in _STEP_WORDS.items()
}

MACHINE_STATUS = register(
    SUBJECT,
    {
        "starting": StateDef("Starting", "info", "{machine} is starting.", _STARTING_REASONS),
        "no_capacity": StateDef(
            "Waiting for hardware",
            "warning",
            "No hardware is free for {machine} right now. We keep trying.",
            {
                "gpu": ReasonDef("No {gpu} is free right now. We keep trying."),
                # The provider's own refusal, as the reconcile read it.
                "provider": ReasonDef("{detail} We keep trying."),
            },
        ),
        "running": StateDef("Running", "success", "{machine} is running."),
        "disk_full": StateDef("Almost out of disk", "warning", "{machine} is almost out of disk."),
        "unhealthy": StateDef("Can't run chats", "danger", "{machine} can't run chats."),
        "unreachable": StateDef("Not responding", "danger", "{machine} isn't responding."),
        "stopping": StateDef(
            "Stopping",
            "neutral",
            "{machine} is stopping.",
            {
                "credits": ReasonDef(
                    "Credits ran out, so {machine} stops soon. Your files are saved."
                ),
                "cap": ReasonDef(
                    "{machine} reached its monthly cap and stops soon. Your files are saved."
                ),
            },
        ),
        "stopped": StateDef(
            "Stopped",
            "muted",
            "{machine} is stopped. Sending a message starts it.",
            {
                "credits": ReasonDef(
                    "{machine} is stopped. It ran out of credits.", tone="warning"
                ),
                "cap": ReasonDef(
                    "{machine} is stopped. It reached its monthly cap.", tone="warning"
                ),
                "free_expired": ReasonDef("{machine} is stopped. Its free period ended."),
                "idle": ReasonDef("{machine} stopped after sitting idle. A message starts it."),
                "provider": ReasonDef(
                    "{machine} was stopped by its provider. Sending a message starts it."
                ),
                "not_responding": ReasonDef(
                    "{machine} stopped because it stopped responding. Its disk is kept.",
                    tone="warning",
                ),
            },
        ),
        "failed": StateDef(
            "Couldn't start",
            "danger",
            "{machine} couldn't start.",
            {
                # Its node never registered with this server, tries exhausted.
                "never_registered": ReasonDef(
                    "{machine} couldn't start because its node never registered with this server."
                ),
                # The provider refused or failed the start, in its words.
                "start_failed": ReasonDef(
                    '{machine} couldn\'t start. The last error was "{detail}".'
                ),
                # The same, with the node's own last error.
                "never_registered_error": ReasonDef(
                    "{machine} couldn't start because its node never registered with this "
                    'server. Its last error was "{detail}".'
                ),
            },
        ),
        "deleted": StateDef("Deleted", "muted", "{machine} was deleted."),
        "shared": StateDef("Shared", "neutral", "Runs on {machine}."),
    },
)


@dataclass(frozen=True, slots=True)
class MachineEvidence:
    """What a machine's card knows."""

    #: The card's state (``OrgMachineState`` or ``shared``).
    state: str
    name: str = ""
    step: str | None = None
    step_started_at: datetime | None = None
    step_expected_seconds: int | None = None
    stop_reason: str = ""
    gpu_name: str = ""
    #: Set when the box answers but no worker of it can serve.
    unhealthy: bool = False
    #: Set when a running machine's volume is almost full
    #: (``alkera_core.compute.disk.disk_nearly_full``).
    disk_full: bool = False
    #: While it waits for hardware: why, in a sentence of the provider's
    #: refusal, and when the provider is asked again.
    wait_reason: str = ""
    wait_retry_at: datetime | None = None
    #: Why a ``failed`` machine stopped trying: ``never_registered`` when its
    #: node never claimed it, ``start_failed`` when the provider refused or
    #: failed the start, else empty.
    failure: str = ""
    #: The node's own last error, when its provider read one.
    failure_detail: str = ""


def _minutes(seconds: float) -> str:
    whole = max(1, round(seconds / 60))
    return f"{whole} minute" if whole == 1 else f"{whole} minutes"


def machine_status(evidence: MachineEvidence, *, now: datetime) -> StatusFact:
    """The machine's status. A starting step quotes its estimate only while it
    has time left; past it, the step is overdue and says so."""
    say = MACHINE_STATUS.fact
    machine = evidence.name or Generic("the machine")
    state = evidence.state
    if evidence.unhealthy and state == "running":
        return say("unhealthy", machine=machine)
    if evidence.disk_full and state == "running":
        return say("disk_full", machine=machine)
    if state == "starting":
        step = evidence.step or ""
        started, expected = evidence.step_started_at, evidence.step_expected_seconds
        if step not in _STEP_WORDS or started is None or expected is None:
            return say("starting", since=started, machine=machine)
        due = started + timedelta(seconds=expected)
        if now >= due:
            return say("starting", reason=f"{step}_overdue", since=started, machine=machine)
        return say(
            "starting",
            reason=step,
            since=started,
            recheck_at=due,
            machine=machine,
            minutes=_minutes((due - now).total_seconds()),
        )
    if state == "waiting_for_hardware":
        if evidence.wait_reason:
            return say(
                "no_capacity",
                reason="provider",
                recheck_at=evidence.wait_retry_at,
                machine=machine,
                detail=evidence.wait_reason,
            )
        if evidence.gpu_name:
            return say("no_capacity", reason="gpu", machine=machine, gpu=evidence.gpu_name)
        return say("no_capacity", machine=machine)
    if state == "failed" and evidence.failure == "never_registered":
        if evidence.failure_detail.strip():
            return say(
                "failed",
                reason="never_registered_error",
                machine=machine,
                detail=evidence.failure_detail,
            )
        return say("failed", reason="never_registered", machine=machine)
    if state == "failed" and evidence.failure == "start_failed" and evidence.failure_detail.strip():
        return say("failed", reason="start_failed", machine=machine, detail=evidence.failure_detail)
    if state in ("stopping", "stopped"):
        definition = MACHINE_STATUS.states[state]
        reason = evidence.stop_reason if evidence.stop_reason in definition.reasons else ""
        return say(state, reason=reason, machine=machine)
    # running, unreachable, other failures, deleted and shared say it in their word.
    return say(state, machine=machine)


#: The platform console's own words for an allocation: staff run the fleet by
#: the allocation's lifecycle (a drain, a sleep, a release), which an org's
#: card folds into fewer states.
FLEET_STATUS = register(
    "fleet",
    {
        "starting": StateDef(
            "Starting",
            "neutral",
            "{machine} is starting.",
            {
                "after_stop": ReasonDef(
                    "{machine} starts once its provider has stopped it.",
                    label="Starting once stopped",
                ),
            },
        ),
        "ready": StateDef("Ready", "success", "{machine} is serving."),
        "unreachable": StateDef("Not responding", "danger", "{machine} isn't responding."),
        "draining": StateDef("Draining", "warning", "{machine} is draining."),
        "asleep": StateDef("Asleep", "muted", "{machine} is asleep with its disk kept."),
        "failed": StateDef("Failed", "danger", "{machine} failed."),
        "lost": StateDef("Lost", "danger", "{machine} was lost by its provider."),
        "releasing": StateDef("Releasing", "muted", "{machine} is being released."),
        "released": StateDef("Released", "muted", "{machine} was released."),
        "revoked": StateDef("Credential revoked", "muted", "Its credential was revoked."),
    },
)

_FLEET_STATE: Final[dict[str, str]] = {
    "pending": "starting",
    "provisioning": "starting",
    "bootstrapping": "starting",
}


def fleet_status(
    *,
    allocation_state: str | None,
    liveness: str,
    name: str,
    revoked: bool,
    wake_pending: bool,
) -> StatusFact:
    """A platform fleet row's status, for a credential and the allocation it
    claimed, if any. A credential revoked with no allocation behind it is
    ``revoked``, never "starting": nothing will start. A ready allocation is
    as live as its heartbeat says."""
    say = FLEET_STATUS.fact
    machine = name or Generic("the machine")
    if allocation_state is None:
        return say("revoked" if revoked else "starting", machine=machine)
    if allocation_state == "ready":
        if liveness in ("starting", "unreachable"):
            return say(liveness, machine=machine)
        return say("ready", machine=machine)
    if allocation_state == "asleep" and wake_pending:
        return say("starting", reason="after_stop", machine=machine)
    state = _FLEET_STATE.get(allocation_state, allocation_state)
    return say(state if state in FLEET_STATUS.states else "failed", machine=machine)


__all__ = [
    "FLEET_STATUS",
    "MACHINE_STATUS",
    "SUBJECT",
    "MachineEvidence",
    "fleet_status",
    "machine_status",
]
