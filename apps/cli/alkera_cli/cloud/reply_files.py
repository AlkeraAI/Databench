"""Whether the files a published reply names are in the chat.

The mirror holds a reply until the files it names have their bytes on the
drive, then publishes it. What that wait cannot do is make a file exist: an
agent that links a file it never wrote, or one it deleted before it finished
the reply (a result it wrote out with ``blob.materialize``, charted, and then
removed as clean-up, before linking the result), publishes a reference the
drive will never answer. Every reader then sees that reference as a file that
is not in the chat.

This module is the box's side of that: it learns, from the durable entries the
mirror publishes, which file each held result was written out to (the same
rule the web uses to open a ``blob:`` link), checks a published reply's
references against the chat's folder on this machine, and words what it found
for the agent, so the agent's next turn knows its last reply pointed at
nothing. Pure bookkeeping over plain dicts and the filesystem: no socket, no
harness.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alkera_core.chat_paths import ReplyFile, reply_files

#: The spellings of the tool that writes a held result out to a file. Opencode
#: composes an MCP tool as ``<client>_<tool>`` and the model may echo either
#: casing, so the name is matched after the prefix and case are dropped.
_MATERIALIZE = frozenset({"blob.materialize", "blob_materialize"})
_PREFIX = re.compile(r"^(?:mcp__[^_]+__|alkera[_-])", re.IGNORECASE)
_CALL_TOOL = "call_tool"

#: How many references one note to the agent names. A reply that broke more
#: links than this is told the first ones and the count of the rest.
NOTE_LIMIT = 10


def _canonical(name: str) -> str:
    return _PREFIX.sub("", name.strip()).lower()


def _result_of(output: Any) -> Mapping[str, Any] | None:
    """The tool's own result: the loopback MCP server hands it back as a JSON
    string with the result nested under ``result``."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return None
    if isinstance(output, Mapping) and isinstance(output.get("result"), Mapping):
        output = output["result"]
    return output if isinstance(output, Mapping) else None


def materialized(tool_name: str, tool_input: Any, output: Any) -> tuple[str, str] | None:
    """``(handle, path)`` when this finished call wrote a held result to a file.

    The call reaches the transcript either as the tool itself or wrapped in
    ``call_tool`` (name and arguments under ``input.name`` / ``input.args``);
    both say the same thing. ``tool_name`` may be empty when the call's
    announcement was not seen: a wrapper is recognised by its input alone.
    """
    if not isinstance(tool_input, Mapping):
        return None
    name = _canonical(tool_name)
    args: Any = tool_input
    inner = tool_input.get("name")
    if (name in ("", _CALL_TOOL)) and isinstance(inner, str):
        name = _canonical(inner)
        args = tool_input.get("args")
    if name not in _MATERIALIZE or not isinstance(args, Mapping):
        return None
    handle = args.get("handle")
    result = _result_of(output)
    path = result.get("path") if result is not None else None
    if not isinstance(handle, str) or not handle or not isinstance(path, str) or not path:
        return None
    return handle, path


@dataclass(frozen=True, slots=True)
class MissingFile:
    """A file a published reply names that is not in the chat's folder.

    ``shown_as`` is where the agent would look for it: relative to its working
    directory when it is under it, else the path inside the chat's folder.
    """

    reference: ReplyFile
    shown_as: str


class ReplyFileCheck:
    """One chat's record of its written-out results, and the check of a reply.

    ``observe`` every durable entry the mirror publishes; ``missing`` a text
    part once it is published. Memory is bounded: the oldest calls and results
    are forgotten first, and a forgotten result is simply not checked.
    """

    def __init__(
        self, *, chat_id: str, chat_folder: Path, working_dir: Path, memory: int = 512
    ) -> None:
        self._chat_id = chat_id
        self._chat_folder = chat_folder
        self._working_dir = working_dir
        self._memory = max(1, memory)
        self._tool_names: OrderedDict[str, str] = OrderedDict()
        self._results: OrderedDict[str, str] = OrderedDict()

    @property
    def result_files(self) -> Mapping[str, str]:
        """Each held result this chat wrote out, by handle: the last write wins,
        as it does on the web."""
        return dict(self._results)

    def observe(self, entry: Mapping[str, Any]) -> None:
        """Learn from one durable entry: a call's tool name, or the file a
        finished ``blob.materialize`` wrote."""
        payload = entry.get("payload")
        if not isinstance(payload, Mapping):
            return
        kind = entry.get("kind")
        call_id = payload.get("tool_call_id")
        call_id = call_id if isinstance(call_id, str) else ""
        if kind == "tool.call":
            name = payload.get("tool_name")
            if call_id and isinstance(name, str):
                self._remember(self._tool_names, call_id, name)
            return
        if kind != "tool.call_update" or payload.get("status") != "completed":
            return
        found = materialized(
            self._tool_names.get(call_id, ""), payload.get("input"), payload.get("output")
        )
        if found is not None:
            handle, path = found
            self._results.pop(handle, None)
            self._remember(self._results, handle, path)

    def missing(self, text: str) -> list[MissingFile]:
        """The files ``text`` shows or links that are not in the chat's folder
        on this machine — the ones no reader will ever be able to open."""
        out: list[MissingFile] = []
        for reference in reply_files(text, chat_id=self._chat_id, result_files=self._results):
            on_disk = self._on_disk(reference)
            if on_disk.is_file():
                continue
            try:
                shown = on_disk.relative_to(self._working_dir).as_posix()
            except ValueError:
                shown = reference.path.path
            out.append(MissingFile(reference=reference, shown_as=shown))
        return out

    def missing_in(self, entry: Mapping[str, Any]) -> list[MissingFile]:
        """:meth:`missing` for a published entry: an agent's finished text
        part (never a reader's, never a synthetic one); nothing for any other."""
        if entry.get("role") == "user":
            return []
        payload = entry.get("payload")
        part = payload.get("part") if isinstance(payload, Mapping) else None
        if not isinstance(part, Mapping) or part.get("type") != "text" or part.get("synthetic"):
            return []
        text = part.get("text")
        return self.missing(text) if isinstance(text, str) and text else []

    def still_missing(self, items: list[MissingFile]) -> list[MissingFile]:
        """Those of ``items`` still not in the chat's folder now."""
        return [item for item in items if not self._on_disk(item.reference).is_file()]

    def _on_disk(self, reference: ReplyFile) -> Path:
        base = self._working_dir if reference.path.anchor == "working" else self._chat_folder
        return base / reference.path.path

    def _remember(self, table: OrderedDict[str, str], key: str, value: str) -> None:
        table[key] = value
        while len(table) > self._memory:
            table.popitem(last=False)


def missing_files_note(missing: list[MissingFile]) -> str:
    """What the agent is told, on its next turn, about the files its last reply
    named that are not in the chat. Hidden from readers: it rides the per-turn
    context channel, never the person's bubble."""
    lines = [
        "FILES YOUR LAST REPLY NAMED ARE NOT IN THE CHAT. The reader sees each of these "
        "as 'not in the chat' instead of a file they can open:"
    ]
    for item in missing[:NOTE_LIMIT]:
        ref = item.reference
        if ref.handle is not None:
            lines.append(
                f"- `{item.shown_as}` — the file your link `[{ref.label}]({ref.target})` "
                "opens, which `blob.materialize` wrote and which was then deleted"
            )
        else:
            lines.append(f"- `{item.shown_as}` — linked as `{ref.label}`")
    if len(missing) > NOTE_LIMIT:
        lines.append(f"- and {len(missing) - NOTE_LIMIT} more")
    lines.append(
        "Each one was never written or was deleted after it was written. If the reader "
        "still needs one, write it again (run `blob.materialize` again for a result) and "
        "link the new file. Never delete or move a file a reply links, including when "
        "cleaning up."
    )
    return "\n".join(lines)


__all__ = [
    "NOTE_LIMIT",
    "MissingFile",
    "ReplyFileCheck",
    "materialized",
    "missing_files_note",
]
