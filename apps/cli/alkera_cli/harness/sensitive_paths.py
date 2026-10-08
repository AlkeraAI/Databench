"""The sensitive-path approval floor both harness adapters apply to a tool call.

The rule: a tool call that names a path which may hold a secret (the gateway
token at ``~/.alkera/auth.yml``, an SSH key, ``~/.aws/credentials``, a project
``.env``, ...) is raised to at least ``EGRESS``, so it costs an approval instead
of auto-allowing as a plain read. The one exemption is the chat's own scratch
dir: it lives under ``.alkera/`` (which the marker list covers) but holds only the
model's plan and scratch files, which plan mode depends on writing and reading
back. A directory-walking tool that names no location and has no root to fall
back on is escalated too, because what it reads cannot be ruled out.

Without this floor a ``read("~/.alkera/auth.yml")`` from a file tool is a plain
fs READ, which the policy auto-allows in every mode (no prompt, no judge, no
decision record), while the identical ``cat ~/.alkera/auth.yml`` through the
shell gate prompts. Reusing the marker scan the shell gate uses
(:func:`command_touches_sensitive_path`) means a credential read costs the same
approval whichever tool, and whichever harness, reaches it. Both the Claude and
the opencode adapter call :func:`escalate_sensitive_path`; it lives once, here,
so a fix to the floor reaches both.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.plugins.plugin_base.permissions import (
    command_touches_sensitive_path,
    credential_path_gate_enabled,
    raise_effect,
)

SENSITIVE_PATH_REASON = "path may hold credentials or private keys"
UNRESOLVED_PATH_REASON = "target directory not identified — cannot rule out a credential path"


def inside_sandbox(target: str, sandbox_dir: Path | None, workspace_root: Path | None) -> bool:
    """Whether ``target`` is the chat's own Alkera-managed scratch dir, or a path
    under it.

    A harness reports a path either absolute or relative to the agent's working
    directory, so a relative one is joined to ``workspace_root`` first. Both sides
    are resolved (``..`` collapsed, symlinks followed) and compared by path
    component, never by string prefix, so ``/sandbox-evil`` is not inside
    ``/sandbox`` and ``/sandbox/../x`` is not either. Anything that cannot be
    resolved counts as OUTSIDE, so the caller escalates."""
    if sandbox_dir is None:
        return False
    try:
        path = Path(target).expanduser()
        if not path.is_absolute():
            if workspace_root is None:
                return False
            path = Path(workspace_root) / path
        resolved = path.resolve()
        root = Path(sandbox_dir).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def escalate_sensitive_path(
    descriptor: ActionDescriptor,
    candidates: Sequence[str],
    *,
    unresolved: bool = False,
    sandbox_dir: Path | None,
    workspace_root: Path | None,
) -> ActionDescriptor:
    """Raise an action that reaches a secret-bearing path to ``EGRESS``.

    ``candidates`` is every location the call names, because each tool names its
    target under a different key, and a grep returns the matched LINES (a full
    read primitive, not a listing). Any one candidate that trips the marker scan
    and is not inside the sandbox escalates. ``unresolved`` is the fail-closed
    arm: a directory-walking tool that named no location at all, with no root to
    fall back on, is escalated rather than trusted.

    The effect is raised, never set: a ``DESTROY`` or an ``EXEC`` is already above
    ``EGRESS`` and keeps it. With the credential-path gate switched off
    (:func:`credential_path_gate_enabled`) the descriptor comes back unchanged."""
    if not credential_path_gate_enabled():
        # Off unless its switch is set: the descriptor keeps the effect its tool
        # gave it, and the mode decides.
        return descriptor
    reason: str | None = None
    if unresolved:
        reason = UNRESOLVED_PATH_REASON
    else:
        for candidate in candidates:
            if not candidate or not command_touches_sensitive_path(candidate):
                continue
            if inside_sandbox(candidate, sandbox_dir, workspace_root):
                continue
            reason = f"{SENSITIVE_PATH_REASON}: {candidate}"
            break
    if reason is None:
        return descriptor
    return raise_effect(descriptor, Effect.EGRESS, reason=reason)
