"""What a reader is shown of an agent's error: the words it carried, never the
stack trace or the file paths of the agent's own internals."""

from __future__ import annotations

import re

#: A line of a JavaScript stack trace (``    at fn (/$bunfs/root/...:12:3)``).
_STACK_FRAME = re.compile(r"^\s*at\s")
#: A message that is nothing but an error class's name (``ProviderModelNotFoundError:``).
_BARE_ERROR_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:Error|Exception)\s*:?$")
#: What a reader is shown for an internal failure that carried no words of its own.
INTERNAL_ERROR_DETAIL = "The agent stopped on an internal error. Send the message again."


def readable_error(message: str) -> str:
    """``message`` as a reader may be shown it: an error that carries a stack
    trace (an uncaught defect inside the agent) is cut to its first line, and
    one whose first line is only an error class's name says the agent stopped
    on an internal error instead. Nothing from a stack (frames, the agent's
    file paths) reaches a transcript."""
    lines = [line for line in message.splitlines() if line.strip()]
    if not lines:
        return INTERNAL_ERROR_DETAIL
    stacked = any(_STACK_FRAME.match(line) for line in lines[1:])
    first = lines[0].strip()
    if _STACK_FRAME.match(first) or _BARE_ERROR_NAME.match(first):
        return INTERNAL_ERROR_DETAIL
    return first if stacked else message


__all__ = ["INTERNAL_ERROR_DETAIL", "readable_error"]
