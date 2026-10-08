"""The words a cloud chat uses when it will not run something, in one place.

A cloud session is an analyst's: it opens in ``read_only`` permission mode, so
a statement that would modify data, an unparseable statement that cannot be
PROVEN read-only, and a shell command or a file write are all refused before
they run. That refusal is the product working, not an error — so the reader
gets a plain sentence in the transcript instead of a raw permission failure,
and the agent keeps the turn and offers a read-only alternative.

A handful of sentences cover every refusal a cloud chat can produce — the ones
above, a read of a file outside the workspace, and a WRITE outside the chat's
own folder (both from :mod:`alkera_cli.cloud.fence`, which fences reads and
writes at different boundaries) — and they live here as data so the reader
never meets two different phrasings of the same refusal.
They describe the WORKSPACE, never an editor, a terminal or a setting — the
reader has none of those, and naming one would be an instruction they cannot
follow.

The statement that was refused rides with the sentence, sanitised to one
bounded line with no control, bidi or zero-width characters, and is rendered
as inert text: never markdown, never a link.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from alkera_core.schemas.chat import (
    Event,
    PermissionRequest,
    PermissionResolved,
    ToolCall,
    ToolCallUpdate,
)

RefusalReason = Literal[
    "unprovable_read",
    "read_only_workspace",
    "read_only_shell",
    "outside_workspace",
    "outside_chat_folder",
]

#: The reader-facing copy. The ONLY place these sentences are spelled.
REFUSAL_COPY: Mapping[RefusalReason, str] = {
    "unprovable_read": "I couldn't prove that query is read-only, so I won't run it.",
    "read_only_workspace": (
        "This connection is read-only, so statements that modify data are refused."
    ),
    "read_only_shell": "This workspace is read-only; I won't run shell commands.",
    "outside_workspace": "I can only read files inside this workspace.",
    "outside_chat_folder": "I can only save files inside this chat's own folder.",
}

#: How much of the refused statement the note quotes before it is elided.
MAX_STATEMENT_CHARS = 240

#: How many in-flight tool calls / permission asks a watch remembers, so a long
#: session cannot grow it without bound.
WATCH_MEMORY = 64

#: The prefix the in-tool gate puts on every refusal it raises as a tool error.
_DENIED_PREFIX = "permission denied"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Zero-width and bidi-override characters: invisible in a rendered line, and a
#: way to make a quoted statement read as something other than what would run.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

#: Where a SHELL tool call keeps its command — a refused one is a "no shell here"
#: refusal, not a "no data writes" one, so it gets its own sentence.
_SHELL_KEYS = ("command", "cmd")


def quote_statement(text: str, *, limit: int = MAX_STATEMENT_CHARS) -> str:
    """The refused statement as one bounded, inert, quoted line.

    Newlines and tabs collapse to single spaces, control characters go, and
    zero-width / bidi-override characters are dropped outright so the quoted
    line reads as exactly the text that would have run. The result is plain
    data for a text node — the renderer never treats it as markup."""
    cleaned = _INVISIBLE.sub("", _CONTROL.sub(" ", text))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if len(cleaned) > limit:
        cleaned = cleaned[: max(1, limit - 1)].rstrip() + "…"
    return f'"{cleaned}"'


@dataclass(frozen=True, slots=True)
class RefusalNote:
    """One refusal, ready for the transcript."""

    reason: RefusalReason
    statement: str = ""
    """The refused text, already sanitised and quoted; ``""`` when unknown."""

    @property
    def headline(self) -> str:
        return REFUSAL_COPY[self.reason]

    def as_note(self) -> str:
        """The transcript entry: the sentence, then the quoted statement on its
        own line. A reader's UI shows the first line as the note and the rest as
        its detail."""
        return f"{self.headline}\n{self.statement}" if self.statement else self.headline


def fence_reason(*, writing: bool, target: str, boundary: Path | str) -> str:
    """What the MODEL is told when the fence refuses, as one sentence.

    Not the reader's sentence above: the reader is being told what happened,
    the model is being told how to succeed on its next attempt. So it names
    three things the reader's copy leaves out — WHO refused (the workspace
    policy; no human was asked, and saying one was is a lie the model then
    reasons from), WHICH operation was refused (a write refused with "I can
    only read files here" reads as a missing capability rather than a
    boundary), and WHERE it may go instead, which is the only part that lets
    the model recover inside the same turn.

    The boundaries differ by operation on purpose: a cloud chat READS the
    workspace and WRITES only its own folder.
    """
    where = quote_statement(target) or "that location"
    if writing:
        return (
            f"The workspace policy refused this write: {where} is outside this chat's "
            f"sandbox; write inside {boundary} instead."
        )
    return (
        f"The workspace policy refused this read: {where} is outside this chat's "
        f"workspace; read inside {boundary} instead."
    )


def _denied(error_text: str | None) -> bool:
    return bool(error_text) and str(error_text).strip().lower().startswith(_DENIED_PREFIX)


def _sql_of(args: Mapping[str, Any]) -> str | None:
    value = args.get("sql")
    return value if isinstance(value, str) and value.strip() else None


def _shell_of(args: Mapping[str, Any]) -> str | None:
    for key in _SHELL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _reason_for_sql(sql: str, dialect: str = "") -> RefusalReason:
    """``unprovable_read`` when the classifier could not parse the statement at
    all (it fails closed to a write, which is exactly what we cannot prove);
    ``read_only_workspace`` when it parsed and the statement mutates."""
    from alkera_cli.plugins.plugin_base.permissions.classifier import classify_statements

    _, confidence = classify_statements(sql, dialect)
    return "unprovable_read" if confidence == "unknown" else "read_only_workspace"


#: What the SQL tool says when the CONNECTION refuses a write by its nature
#: (a read-replica, a warehouse role with no write grant) — true in every mode.
_READ_ONLY_CONNECTION = "is read-only by its nature"


def _sql_note(sql: str, *, read_only: bool) -> RefusalNote | None:
    """A refused statement's note. An unprovable statement says so in any mode; a
    write says the connection is read-only only when it IS — the chat is in an
    analyst's mode or the connection itself has no write path. Any other refusal
    of a write (a person's, a rule's) is not the read-only sentence's to explain."""
    reason = _reason_for_sql(sql)
    if reason == "read_only_workspace" and not read_only:
        return None
    return RefusalNote(reason, quote_statement(sql))


def note_for_tool(
    args: Mapping[str, Any], *, read_only: bool, error_text: str | None = None
) -> RefusalNote | None:
    """The note for a refused tool call, from the arguments it was called with,
    or ``None`` when no read-only sentence describes the refusal.

    ``read_only`` is whether the chat is in an analyst's mode. Only a refused
    statement or shell command has a sentence here: a refused file read or write
    is the fence's (outside the workspace, outside the chat's folder) or the
    card's own reason, never "this is read-only"."""
    sql = _sql_of(args)
    if sql is not None:
        connection = bool(error_text) and _READ_ONLY_CONNECTION in str(error_text)
        return _sql_note(sql, read_only=read_only or connection)
    shell = _shell_of(args)
    if shell is not None and read_only:
        return RefusalNote("read_only_shell", quote_statement(shell))
    return None


def note_for_ask(request: PermissionRequest, *, read_only: bool) -> RefusalNote | None:
    """The note for a permission ask the policy refused without asking anyone —
    a shell command or a statement — or ``None`` when no read-only sentence
    describes it (a file read or write the fence turned away says so itself)."""
    subject = request.subject if isinstance(request.subject, Mapping) else {}
    raw = subject.get("raw")
    statement = raw if isinstance(raw, str) else ""
    capability = subject.get("capability")
    if capability == "sql" and statement:
        return _sql_note(statement, read_only=read_only)
    if capability == "shell" and read_only:
        return RefusalNote("read_only_shell", quote_statement(statement))
    return None


class RefusalWatch:
    """Every harness event in; the note a refusal deserves out.

    Two shapes reach it. The in-tool gate raises its refusal as a tool error
    (``permission denied: …``), and the arguments it refused arrived on an
    earlier event, so the watch remembers them per call. A vendor tool ask the
    policy refuses on its own — no human in the loop — arrives as a request
    followed by a ``policy`` reject, so the watch remembers the ask.

    A human's own reject is not this: the reader made that decision and does
    not need it explained back to them.

    ``read_only`` answers whether the chat is in an analyst's mode NOW: the
    read-only sentences are true only then (or for a connection that has no
    write path at all), so a refusal in any other mode gets none of them."""

    def __init__(
        self,
        *,
        read_only: Callable[[], bool] = lambda: False,
        remember: int = WATCH_MEMORY,
    ) -> None:
        self._read_only = read_only
        self._remember = max(1, remember)
        self._calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._asks: OrderedDict[str, PermissionRequest] = OrderedDict()

    def note(self, event: Event) -> RefusalNote | None:
        if isinstance(event, ToolCall):
            if event.input:
                self._keep(self._calls, event.tool_call_id, dict(event.input))
            return None
        if isinstance(event, ToolCallUpdate):
            if event.input:
                self._keep(self._calls, event.tool_call_id, dict(event.input))
            if event.status != "error" or not _denied(event.error_text):
                if event.status in ("completed", "error"):
                    self._calls.pop(event.tool_call_id, None)
                return None
            return note_for_tool(
                self._calls.pop(event.tool_call_id, {}),
                read_only=self._read_only(),
                error_text=event.error_text,
            )
        if isinstance(event, PermissionRequest):
            self._keep(self._asks, event.request_id, event)
            return None
        if isinstance(event, PermissionResolved):
            ask = self._asks.pop(event.request_id, None)
            if ask is None or event.decided_by != "policy":
                return None
            if not event.option_id.startswith("reject"):
                return None
            return note_for_ask(ask, read_only=self._read_only())
        return None

    def _keep(self, store: OrderedDict[str, Any], key: str, value: Any) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > self._remember:
            store.popitem(last=False)


__all__ = [
    "MAX_STATEMENT_CHARS",
    "REFUSAL_COPY",
    "RefusalNote",
    "RefusalReason",
    "RefusalWatch",
    "fence_reason",
    "note_for_ask",
    "note_for_tool",
    "quote_statement",
]
