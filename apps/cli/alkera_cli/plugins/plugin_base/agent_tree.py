"""The chat tree a tool writes into, and whose it is.

The daemon writes into the root as itself (root on a box): the rest of a
shortened tool result, the payload a Python tool hands its child, a result
the model asked to have written out. The agent writes there too, as its own
uid, and a name the daemon is about to use may already be something the agent
left: a link to a host file, a directory swapped for a link to a host
directory, a FIFO that holds its first writer until a reader comes. Every such
write is :class:`~alkera_cli.files.chat_fs.ChatTree`'s: each component below
the root opened with no link followed, the leaf without blocking and checked
to be a plain file, and what is made handed to the chat's identity with the
tree's modes, so the agent's commands can read and write it. This module
answers the one question the tools have before they write: whose tree it is
(:func:`tool_owner`), through the same resolver every other daemon write asks,
and names the one place their long results go (:class:`ToolOutput`).
"""

from __future__ import annotations

import contextlib
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from alkera_cli.files.chat_fs import ChatTree, TreeIdentity, identity_for
from alkera_cli.plugins.plugin_base.tool import ToolContext

IdentityResolver = Callable[[str], TreeIdentity | None]

#: The directory under the root a tool's long results and payloads go to.
TOOL_OUTPUT_DIRNAME = "tool-output"


def tool_owner(ctx: ToolContext) -> TreeIdentity | None:
    """Who the files a tool makes in this session's root are handed to: the
    identity of the session that owns the tree (the session itself, or the
    parent of a subagent child), as the tree's resolver answers it. ``None``
    for a local session (the person's own files already) or a host with no
    per-chat uid. The resolver is the context's when one was injected, else
    the one the box daemon installed."""
    if ctx.fence is None:
        return None
    owner = ctx.owner_session_id or ctx.session_id
    if not owner:
        return None
    resolve: IdentityResolver = ctx.identity_resolver or identity_for
    return resolve(owner)


def tool_tree(ctx: ToolContext, root: Path) -> ChatTree:
    """The chat tree at ``root`` writing as this session's owner."""
    return ChatTree(root, tool_owner(ctx))


@dataclass(frozen=True, slots=True)
class SpillTarget:
    """One file a tool's output goes to: a name under the tool-output
    directory of a tree. Nothing exists until the first write, which makes the
    directory and the file through the tree (nothing the agent planted at
    either name is followed, and both are the chat's)."""

    tree: ChatTree
    relative: str
    """The file's path under the tree's root, in the tree's spelling."""

    @property
    def path(self) -> Path:
        """The file's host path, for the sentence that names it."""
        return self.tree.root / self.relative

    def open_append(self) -> contextlib.AbstractContextManager[BinaryIO]:
        """The file open for appending, created on the first open."""
        return self.tree.open_write(self.relative, append=True)

    def write_text(self, text: str) -> None:
        """The file replaced whole by ``text``."""
        self.tree.write(self.relative, text.encode("utf-8"))


SpillFactory = Callable[[], SpillTarget]
"""Names a fresh spill file each time it is called; the tool supplies one
rooted in its session's tool-output directory, a test one in a temp tree."""


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """The tool-output directory of one session, as names under its tree.

    Made the first time something is written into it, never ahead of that: a
    chat whose commands were never long enough to spill has no such directory.
    """

    tree: ChatTree
    dirname: str = TOOL_OUTPUT_DIRNAME

    @property
    def path(self) -> Path:
        return self.tree.root / self.dirname

    def target(self, name: str) -> SpillTarget:
        return SpillTarget(self.tree, f"{self.dirname}/{name}")

    def spill(self, prefix: str) -> SpillTarget:
        """A fresh, uniquely named spill file."""
        return self.target(f"{prefix}-{uuid.uuid4().hex[:12]}.txt")

    def write_text(self, name: str, text: str) -> Path:
        """``text`` written as ``name`` in the directory; its host path."""
        target = self.target(name)
        target.write_text(text)
        return target.path

    def unlink(self, name: str) -> None:
        self.tree.unlink(f"{self.dirname}/{name}")


def tool_output(ctx: ToolContext, *, fallback: str) -> ToolOutput:
    """Where a tool's long results write themselves for this session.

    The session's own working directory when it has one. A shortened result
    hands the model a path and tells it to read that file instead of running
    the command again, so the file has to sit where the model may read: a
    cloud session's fence admits the one directory that session owns and
    refuses the rest of the daemon's state (other chats' transcripts,
    connector credentials). A local
    session may read either, so pointing both at the working directory keeps
    one answer rather than two. Falls back to ``<.alkera>/tool-output`` for a
    call made outside a chat, and to ``<tmp>/<fallback>`` when there is no
    project at all.

    A local session's root is the daemon's own directory (the chat's sandbox
    under its records, the project's ``.alkera``), made here when it is not
    there yet. A bounded session's root is its working directory, which exists
    before any tool runs and is never made by the daemon: a directory root
    made would not be the chat's to write.
    """
    if ctx.sandbox_dir is not None:
        root, name = Path(ctx.sandbox_dir), TOOL_OUTPUT_DIRNAME
    elif ctx.alkera_dir is not None:
        root, name = Path(ctx.alkera_dir), TOOL_OUTPUT_DIRNAME
    else:
        root, name = Path(tempfile.gettempdir()), fallback
    if ctx.fence is None:
        root.mkdir(parents=True, exist_ok=True)
    return ToolOutput(tool_tree(ctx, root), name)


__all__ = [
    "TOOL_OUTPUT_DIRNAME",
    "IdentityResolver",
    "SpillFactory",
    "SpillTarget",
    "ToolOutput",
    "tool_output",
    "tool_owner",
    "tool_tree",
]
