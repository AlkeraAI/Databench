"""What a box remembers of a chat whose mirror would not start, how long it
waits before trying again, whether it may make room for the try, and what
the reader is told."""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass
from typing import Any

from alkera_core.chat_refusals import ChatRefusalKind

from alkera_cli.cloud.faults import RefusalKind, says_refused
from alkera_cli.cloud.folder_paths import chat_folder_node
from alkera_cli.harness.adapter import HarnessSandboxRefusedError

#: What the reader is told when the gateway refuses this box the chat's
#: transcript, by the gateway's refusal code.
UNKNOWN_REFUSAL_SENTENCE = "This workspace is not allowed to write this chat's transcript."
#: A transient refusal said only once it has outlasted its tries
#: (``faults.TRANSIENT_LIMIT``): it is still not a verdict on the chat.
UNOPENED_SENTENCE = (
    "This workspace machine could not open the chat's transcript and will try again on its own."
)
REFUSAL_SENTENCES = {
    "not_publisher": (
        "This chat is served by a different workspace machine, so this one will not write it."
    ),
    "forbidden": UNKNOWN_REFUSAL_SENTENCE,
    "not_found": UNOPENED_SENTENCE,
    "timeout": UNOPENED_SENTENCE,
}

#: The kind a reader is shown for each gateway refusal code (the sentence
#: above stays in the box's report and is never shown).
REFUSAL_KINDS: dict[str, ChatRefusalKind] = {
    "not_publisher": "moving",
    "forbidden": "not_allowed",
    "not_found": "transcript_unopened",
    "timeout": "transcript_unopened",
}


def gateway_refusal(code: str) -> tuple[str, ChatRefusalKind]:
    """The sentence and the kind the box reports for a gateway refusal code.
    A code this box does not know is a verdict: it will not publish the chat."""
    return (
        REFUSAL_SENTENCES.get(code, UNKNOWN_REFUSAL_SENTENCE),
        REFUSAL_KINDS.get(code, "not_allowed"),
    )


START_FAILURE_BACKOFF_CAP_SECONDS = 300.0
"""Ceiling on the wait before a chat whose mirror would not start is tried
again. The first wait is twice the poll interval and each failure in a row
doubles it: a start that fails for a reason no tick will change (the agent
session the chat's manifest pins is not in the harness storage, the binary is
gone) would otherwise take the chat's folder and fail on it every fifteen
seconds, a full traceback each time, for as long as the box runs."""
EVICTING_START_FAILURES = 2
"""Failed starts in a row after which a chat no longer puts a served chat to
sleep to be tried again: it takes only a slot that is already free."""
#: What the reader is told when this box could not open the chat's session.
#: The harness's own words ride along: unlike a refusal code they say what to
#: do (which chat to repair, which binary is missing).
START_FAILURE_SENTENCE = (
    "This workspace machine could not resume the chat and will try again on its own: {failure}"
)
#: The same for a chat whose session no box has ever opened: there is nothing
#: to resume, and saying so sends the reader looking for a chat that broke.
FIRST_START_FAILURE_SENTENCE = (
    "This workspace machine could not start the chat and will try again on its own: {failure}"
)


def start_failure_sentence(exc: BaseException, *, never_ran: bool = False) -> str:
    """The reader's one sentence for a mirror that would not start, with the
    harness's reason on one line. A sandbox refusal is its own sentence: the
    chat will not run on this box, so it is not framed as a retry.
    ``never_ran`` is the server's word that no box has held the chat's
    session yet (its ``session_state`` reads ``starting``)."""
    if isinstance(exc, HarnessSandboxRefusedError):
        return " ".join(str(exc).split())
    failure = " ".join(f"{type(exc).__name__}: {exc}".split())
    template = FIRST_START_FAILURE_SENTENCE if never_ran else START_FAILURE_SENTENCE
    return template.format(failure=failure)


def start_failure_kind(exc: BaseException, *, never_ran: bool = False) -> ChatRefusalKind:
    """The kind of a mirror that would not start: a sandbox refusal is a
    verdict on this box, anything else is tried again on its own."""
    if isinstance(exc, HarnessSandboxRefusedError):
        return "sandbox_refused"
    return "start_failed" if never_ran else "resume_failed"


def start_key(chat: Mapping[str, Any]) -> tuple[Any, ...]:
    """The row facts a failed start is remembered under. Any of them moving
    (a reader said something, opened the chat afresh, or the chat was filed
    under another folder) is news worth trying again on before the wait is
    up. Nothing else on the row is: the box's own status reports touch it
    too, and a wait that ended on those would be no wait."""
    return (chat.get("last_seq"), chat.get("wake_requested_at"), chat_folder_node(chat))


@dataclass(slots=True)
class StartFailure:
    """What the box remembers of a chat whose mirror would not start here."""

    #: Failures in a row; the wait doubles with each.
    count: int
    #: The service clock reading at which the chat may be tried again.
    retry_at: float
    #: The row facts the failure was recorded under (see ``start_key``).
    seen: tuple[Any, ...]
    #: The failure's reason (the sentence, or the gateway's code).
    reason: str
    #: Whether this reason has been put on the chat, so it is said once.
    said: bool = False


def next_failure(
    previous: StartFailure | None,
    count: int,
    retry_at: float,
    seen: tuple[Any, ...],
    reason: str,
    kind: RefusalKind,
) -> tuple[StartFailure, bool]:
    """The failure to remember after ``previous``, and whether its reason is
    put on the chat now: once per reason, and a passing fault only once it has
    outlasted its tries (``faults.says_refused``)."""
    said = previous is not None and previous.said and previous.reason == reason
    say = says_refused(kind, count) and not said
    failure = StartFailure(count, retry_at, seen, reason, said=said or say)
    return failure, say


def may_evict_for(
    chat_id: str, refused: Container[str], failures: Mapping[str, StartFailure]
) -> bool:
    """Whether a served chat may be put to sleep to start ``chat_id``.

    A chat the gateway refused last time, or whose start has failed twice in
    a row, is tried only in a slot that is already free: evicting a served
    chat for it would most likely buy one more failure, and a retry loop would
    put a healthy chat to sleep on every try (seen: fifteen tries in 37
    minutes, each one evicting a reader's chat). One failed start may still
    make room, since it can be a passing fault and a full box could otherwise
    keep the chat waiting for a day."""
    failure = failures.get(chat_id)
    failed_twice = failure is not None and failure.count >= EVICTING_START_FAILURES
    return chat_id not in refused and not failed_twice


#: Said once while a chat that may not make room waits for a free slot.
REFUSED_SLOT_WAIT = (
    "chat %s was refused on its last start; it waits for a free slot "
    "rather than putting a served chat to sleep for another try"
)
UNSENT_SLOT_WAIT = (
    "chat %s has had nothing said in it yet; it is opened when a slot is free or its "
    "first message arrives, not by putting a served chat to sleep"
)


def unsent(chat: Mapping[str, Any]) -> bool:
    """Whether nobody has said anything in the chat yet: an empty transcript
    and no turn owed. A chat a person has only created has no work to hold a
    slot for, and the chat it would evict may have a reader."""
    return chat.get("last_seq") == 0 and not chat.get("pending_turn")


def slot_admission(
    chat_id: str,
    chat: Mapping[str, Any],
    refused: Container[str],
    failures: Mapping[str, StartFailure],
) -> tuple[bool, str]:
    """Whether starting ``chat_id`` may put a served chat to sleep, and what is
    said while it waits for a slot that is already free instead."""
    if unsent(chat):
        return False, UNSENT_SLOT_WAIT
    return may_evict_for(chat_id, refused, failures), REFUSED_SLOT_WAIT


__all__ = [
    "EVICTING_START_FAILURES",
    "FIRST_START_FAILURE_SENTENCE",
    "REFUSAL_KINDS",
    "REFUSAL_SENTENCES",
    "REFUSED_SLOT_WAIT",
    "START_FAILURE_BACKOFF_CAP_SECONDS",
    "START_FAILURE_SENTENCE",
    "UNKNOWN_REFUSAL_SENTENCE",
    "UNOPENED_SENTENCE",
    "UNSENT_SLOT_WAIT",
    "StartFailure",
    "gateway_refusal",
    "may_evict_for",
    "next_failure",
    "slot_admission",
    "start_failure_kind",
    "start_failure_sentence",
    "start_key",
    "unsent",
]
