"""The names of a chat's own records, spelled once.

A chat's folder holds two kinds of thing. Its working directory is where the
agent runs and a person drops, edits and takes files. Beside it, at the top of
the folder, sit the chat's RECORDS: the manifest that pins the agent session,
the transcript and the decision and cost logs the next box reads back, the
digest that hashes those logs, and the harness's runtime directory. They are
how a chat resumes on another machine, so they travel through the drive like
any file — and they are the record of what was said, so nobody but the machine
holding the chat may rewrite one.

Every rule about them keys on this set: the server marks a node born under one
of these names at a chat folder's top level as a record (``files.authz``), the
agent's write fence refuses them in every mode, the live watcher never streams
them, and the trace digest pins the logs among them. A name added here is
covered by all of those at once; a name spelled in one of them alone is a rule
the others do not know about.

A leaf module on purpose: ``alkera_core.files`` may not import the CLI's
``alkera_core.project`` store, and the store may not depend on the drive, so
the one thing they share sits below both.
"""

from __future__ import annotations

from typing import Final

#: The cache of the chat's state, which pins the agent session inside the
#: runtime directory.
MANIFEST_FILENAME: Final = "manifest.json"

#: The append-only logs a session writes: the transcript (the source of
#: truth the next box replays), the permission decisions and the spend.
TRACE_FILES: Final[tuple[str, ...]] = ("chat.jsonl", "decisions.jsonl", "cost_ledger.jsonl")

#: The hashes of :data:`TRACE_FILES`, written beside them.
DIGEST_FILENAME: Final = "trace.digest.json"

#: The harness's per-chat state: its own session database and configuration.
#: A directory, so everything inside it is a record too.
RUNTIME_DIRNAME: Final = ".runtime"

#: Every name that is a chat's record when it sits directly under the chat's
#: folder. The same name inside the working directory is ordinary working
#: material and is nobody's record.
CHAT_RECORD_NAMES: Final[frozenset[str]] = frozenset(
    {MANIFEST_FILENAME, DIGEST_FILENAME, *TRACE_FILES, RUNTIME_DIRNAME}
)


def is_chat_record_name(name: str | bytes) -> bool:
    """Whether a child of a chat folder wearing ``name`` is one of its records.

    Takes the bytes the drive stores a name as, or the text a path spells it
    in; a name that is not valid UTF-8 is no record, because none of the
    records is spelled in anything else.
    """
    if isinstance(name, bytes):
        try:
            name = name.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return name in CHAT_RECORD_NAMES


__all__ = [
    "CHAT_RECORD_NAMES",
    "DIGEST_FILENAME",
    "MANIFEST_FILENAME",
    "RUNTIME_DIRNAME",
    "TRACE_FILES",
    "is_chat_record_name",
]
