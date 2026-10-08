"""Locate the Claude Code (`claude`) CLI binary the Claude-agent harness spawns.

Unlike opencode (which we Bun-compile + bundle ourselves — MIT, freely
redistributable), the `claude` binary is **proprietary and not ours to ship**.
So the Claude-agent harness is only available when the user already has Claude
Code installed **locally**, and we drive *that* binary. Discovery is therefore
deliberately PATH-aware (the opposite of the opencode resolver).

Resolution order (first hit wins):

1. ``ALKERA_CLAUDE_BIN`` env var — explicit absolute path (tests / power users /
   an Alkera-orchestrated install that records its path here).
2. ``$PATH`` — ``shutil.which("claude")`` (the native installer lands here).
3. Known install locations — npm-global / Homebrew / `~/.local/bin` /
   `~/.claude/local` (covers installs not on the current ``$PATH``).
4. SDK-bundled — the ``claude`` that ships inside the installed
   ``claude-agent-sdk`` wheel (dev/CI convenience; we do NOT re-bundle it into
   our shipped onefile, so this tier is empty in production).

``find_claude_binary()`` returns ``None`` when nothing resolves (drives
``HarnessAdapter.is_available()``); ``resolve_claude_binary()`` raises
``ClaudeBinaryNotFoundError`` (used at spawn time).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from alkera_core.process import SpawnSpec, run

from alkera_cli.host.limits import env_seconds

logger = logging.getLogger(__name__)

#: Budget for the best-effort ``claude --version`` probe. It sits on the
#: session-start path, so a stalled binary pauses chat creation for exactly this
#: long and then reports no agent version — harmless, but visible. 0 waits for
#: the probe, which only a deployment that would rather block than mis-report
#: the version should set.
ENV_VERSION_PROBE_TIMEOUT = "ALKERA_CLAUDE_VERSION_PROBE_TIMEOUT_SECONDS"
VERSION_PROBE_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_VERSION_PROBE_TIMEOUT), default=5.0)

ResolvedClaudeSource = Literal["env", "path", "known", "sdk"]

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


@dataclass(slots=True, frozen=True)
class ResolvedClaudeBinary:
    """A discovered, locally-installed ``claude`` CLI + how we found it."""

    path: Path
    source: ResolvedClaudeSource
    version: str | None = None


class ClaudeBinaryNotFoundError(RuntimeError):
    """No locally-installed ``claude`` CLI could be found. User-facing — the
    Claude-agent harness requires the user to install Claude Code first."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code is not installed. Install it (https://claude.ai/install.sh, "
            "`npm i -g @anthropic-ai/claude-code`, or `pip install claude-agent-sdk`), "
            "or set ALKERA_CLAUDE_BIN to its path."
        )


def _claude_filename() -> str:
    """Executable name for this platform. Read ``sys.platform`` at call time so
    tests can monkeypatch it."""
    return "claude.exe" if sys.platform == "win32" else "claude"


def _probe_version(path: Path) -> str | None:
    """Best-effort ``<claude> --version`` → ``"X.Y.Z"``. Never raises."""
    try:
        out = run(
            SpawnSpec(argv=[str(path), "--version"], env=os.environ, stdout="pipe", stderr="pipe"),
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    said = (out.stdout + b"\n" + out.stderr).decode("utf-8", errors="replace")
    match = _VERSION_RE.search(said)
    return match.group(1) if match else None


def _known_locations() -> list[Path]:
    """Common install paths the native installer / npm / Homebrew use, for
    installs that aren't on the current process's ``$PATH``."""
    home = Path.home()
    name = _claude_filename()
    return [
        home / ".local" / "bin" / name,
        home / ".claude" / "local" / name,
        home / ".npm-global" / "bin" / name,
        home / ".yarn" / "bin" / name,
        home / "node_modules" / ".bin" / name,
        Path("/usr/local/bin") / name,
        Path("/opt/homebrew/bin") / name,
    ]


def _sdk_bundled_path() -> Path | None:
    """The ``claude`` bundled inside the installed ``claude-agent-sdk`` wheel
    (``<site-packages>/claude_agent_sdk/_bundled/<claude>``). Present in a
    `uv sync`'d dev/CI env; absent from our shipped onefile (we never
    re-bundle the proprietary binary)."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    bundled = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / _claude_filename()
    return bundled if bundled.is_file() else None


def _discover() -> tuple[Path, ResolvedClaudeSource] | None:
    """Find a local ``claude`` (path + how). Cheap — no version probe — so it's
    safe for ``is_available()`` checks."""
    env_bin = os.environ.get("ALKERA_CLAUDE_BIN", "").strip()
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p.absolute(), "env"

    which = shutil.which("claude")
    if which:
        return Path(which).absolute(), "path"

    for cand in _known_locations():
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.absolute(), "known"

    sdk = _sdk_bundled_path()
    if sdk is not None:
        return sdk.absolute(), "sdk"

    return None


def find_claude_binary() -> ResolvedClaudeBinary | None:
    """Resolve a locally-installed ``claude``, or ``None`` if none is found."""
    found = _discover()
    if found is None:
        return None
    path, source = found
    return ResolvedClaudeBinary(path=path, source=source, version=_probe_version(path))


def resolve_claude_binary() -> ResolvedClaudeBinary:
    """Like :func:`find_claude_binary` but raises :class:`ClaudeBinaryNotFoundError`
    when no local ``claude`` is installed."""
    found = find_claude_binary()
    if found is None:
        raise ClaudeBinaryNotFoundError
    return found


def claude_is_available() -> bool:
    """Whether a local ``claude`` CLI is installed/discoverable (cheap — no
    version probe). Backs ``ClaudeAgentAdapter.is_available()``."""
    return _discover() is not None


__all__ = [
    "ClaudeBinaryNotFoundError",
    "ResolvedClaudeBinary",
    "ResolvedClaudeSource",
    "claude_is_available",
    "find_claude_binary",
    "resolve_claude_binary",
]
