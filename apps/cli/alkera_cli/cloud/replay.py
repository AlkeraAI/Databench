"""Re-run a file tool call the harness never finished, from its recorded input.

A cloud chat that sleeps or restarts with a write ask pending loses the
harness that raised it: the native session is closed underneath the ask, and
when the reader's allow arrives there is no process left holding the tool
call. Telling a resumed agent "carry on, it was allowed" is not enough — the
model, reading its own transcript, tends to report the write as done without
running it again (the live failure this module answers).

So for the tools whose whole effect is determined by the arguments already on
the transcript — a file write, an edit — the workspace performs the approved
call ITSELF, under the reader's decision, and the real result closes the
original call. Everything else (a shell command, a subagent, a fetch) is not
replayed: its effect is not a pure function of its input, so the agent is told
the call did not run and must be retried.

Both harness shapes are read: opencode's ``write`` / ``edit`` / ``multiedit``
(``filePath`` / ``content`` / ``oldString`` / ``newString`` / ``replaceAll``)
and the claude adapter's ``Write`` / ``Edit`` / ``MultiEdit`` (``file_path`` /
``old_string`` / ``new_string`` / ``replace_all`` / ``edits``). A relative
path resolves against the chat's folder — the directory both harnesses run
in — and a destination outside that folder is refused before anything is
written, whatever the reader answered: the folder governs reach.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alkera_cli.cloud import fence

#: Tools that replace a file's whole content from their input.
WRITE_TOOLS: frozenset[str] = frozenset({"write"})
#: Tools that rewrite part of a file from an anchor and its replacement.
EDIT_TOOLS: frozenset[str] = frozenset({"edit", "multiedit"})
#: Every tool the workspace may re-run from the transcript alone.
REPLAYABLE_TOOLS: frozenset[str] = WRITE_TOOLS | EDIT_TOOLS

#: Why a call was not re-run, in the model's and the reader's words.
OUTSIDE_FOLDER = "the destination is outside this chat's folder"
NO_CONTENT = "the call names no content to write"
ANCHOR_MISSING = "the text to replace is not in the file"


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What re-running one call did: the file it touched and whether it landed."""

    tool: str
    path: str
    ok: bool
    bytes_written: int = 0
    error: str | None = None

    @property
    def summary(self) -> str:
        """One sentence for the agent and the transcript."""
        if self.ok:
            return f"{self.tool} {self.path}: written ({self.bytes_written} bytes)"
        return f"{self.tool} {self.path}: not written — {self.error}"

    @property
    def output(self) -> dict[str, Any]:
        """The tool output the closing update carries."""
        out: dict[str, Any] = {
            "replayed": True,
            "tool": self.tool,
            "path": self.path,
            "summary": self.summary,
        }
        if self.ok:
            out["bytes"] = self.bytes_written
        else:
            out["error"] = self.error
        return out


def replayable(tool_name: str) -> bool:
    """Whether ``tool_name`` is one the workspace can re-run from its input."""
    return tool_name.strip().lower() in REPLAYABLE_TOOLS


def replay_file_tool(
    tool_name: str, tool_input: Mapping[str, Any], *, folder: Path, base: Path | None = None
) -> ReplayResult | None:
    """Perform the file tool call ``tool_name(tool_input)`` inside ``folder``.

    ``None`` when the tool is not one whose effect its input fully determines,
    or when the input names no file — nothing is done, and the caller tells
    the agent to retry. Otherwise the result says whether the write landed;
    a refused or failed replay writes nothing. A relative name lands where the
    agent would have put it — under ``base``, the directory it runs in, which
    is ``folder`` itself when none is given.
    """
    name = tool_name.strip().lower()
    if name not in REPLAYABLE_TOOLS:
        return None
    raw = _first_str(tool_input, "filePath", "file_path", "path")
    if raw is None:
        return None
    if fence.write_escapes(raw, folder=folder, base=base):
        return ReplayResult(tool=name, path=raw, ok=False, error=OUTSIDE_FOLDER)
    target = Path(raw) if Path(raw).is_absolute() else (base or folder) / raw
    if name in WRITE_TOOLS:
        content = tool_input.get("content")
        if not isinstance(content, str):
            return ReplayResult(tool=name, path=raw, ok=False, error=NO_CONTENT)
        text = content
    else:
        applied = apply_edits(_read_text_or_empty(target), tool_input)
        if applied is None:
            return ReplayResult(tool=name, path=raw, ok=False, error=ANCHOR_MISSING)
        text = applied
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        return ReplayResult(tool=name, path=raw, ok=False, error=f"{type(exc).__name__}: {exc}")
    return ReplayResult(tool=name, path=raw, ok=True, bytes_written=len(text.encode("utf-8")))


def apply_edits(old: str, tool_input: Mapping[str, Any]) -> str | None:
    """The file text after the edit(s) ``tool_input`` describes, in either
    harness's spelling; ``None`` when an anchor is not in the text. An empty
    anchor means "the whole file" — the shape both harnesses use to create a
    file through their edit tool."""
    raw_edits = tool_input.get("edits")
    edits = raw_edits if isinstance(raw_edits, list) else [tool_input]
    text = old
    for edit in edits:
        if not isinstance(edit, Mapping):
            continue
        anchor = _first_str(edit, "oldString", "old_string")
        replacement = _first_str(edit, "newString", "new_string")
        if anchor is None or replacement is None:
            return None
        if anchor == "":
            text = replacement
            continue
        if anchor not in text:
            return None
        everywhere = edit.get("replaceAll") is True or edit.get("replace_all") is True
        text = text.replace(anchor, replacement, -1 if everywhere else 1)
    return text


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _first_str(spec: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = spec.get(key)
        if isinstance(value, str):
            return value
    return None


__all__ = [
    "ANCHOR_MISSING",
    "EDIT_TOOLS",
    "NO_CONTENT",
    "OUTSIDE_FOLDER",
    "REPLAYABLE_TOOLS",
    "WRITE_TOOLS",
    "ReplayResult",
    "apply_edits",
    "replay_file_tool",
    "replayable",
]
