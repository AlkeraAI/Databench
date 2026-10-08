"""Filesystem paths the CLI cares about.

`~/.alkera/` is the CLI's per-user directory, used today for the auth file
and intended to also house: the local-server handoff file, log output,
cached project state, and per-user config. Define the constants here so
no module reaches for `Path.home()` directly.

`SYSTEM_CONFIG_PATH` is the optional system-wide config file (typically
written by corporate IT / MDM) that supplies the backend URL and other
defaults for every user on the machine. Lower precedence than env vars and
the user's auth file.

Overrides for tests:
- `ALKERA_HOME` env var — switches the per-user directory.
- `ALKERA_SYSTEM_CONFIG` env var — switches the system config path.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory


def _resolve_home() -> Path:
    override = os.environ.get("ALKERA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".alkera"


def _resolve_system_config() -> Path:
    override = os.environ.get("ALKERA_SYSTEM_CONFIG")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "Alkera" / "config.yml"
    return Path("/etc/alkera/config.yml")


ALKERA_HOME: Path = _resolve_home()

AUTH_FILE_PATH: Path = ALKERA_HOME / "auth.yml"

PREFERENCES_FILE_PATH: Path = ALKERA_HOME / "preferences.yml"

PREFERENCES_LOCK_PATH: Path = ALKERA_HOME / ".preferences.lock"
"""Advisory lock guarding read-modify-write of `preferences.yml` so the
CLI and the daemon (on behalf of the VS Code extension) can't lose each
other's updates when editing concurrently."""

INSTRUCTIONS_FILE_PATH: Path = ALKERA_HOME / "instructions.md"
"""The user's GLOBAL agent instructions — a plain Markdown file applied to every
project the agent runs in. Edited from the VS Code preferences panel (and directly
on disk); materialized into each opencode session's `config.instructions[]` and
appended to the Claude adapter's per-turn system prompt. Plain text (not a
`VersionedModel`): it's prompt content, not a structured schema."""

INSTRUCTIONS_LOCK_PATH: Path = ALKERA_HOME / ".instructions.lock"
"""Advisory lock guarding writes to `instructions.md` (same discipline as
`PREFERENCES_LOCK_PATH`)."""

SYSTEM_CONFIG_PATH: Path = _resolve_system_config()


def cache_dir() -> Path:
    """Root of the Alkera cache: ``<ALKERA_HOME>/cache``.

    The shared on-disk cache for things Alkera derives and can re-create.
    Today that's the per-SHA opencode extractions (``cache/opencode-<sha>/``)
    the daemon unpacks from its Nuitka onefile; more cache kinds may live here
    later. Defined as a function rather than a frozen module constant like the
    paths above so it reflects a ``monkeypatch.setattr(paths, "ALKERA_HOME",
    ...)`` in tests — the same override hook the preferences/auth tests use.
    """
    return ALKERA_HOME / "cache"


def agents_dir() -> Path:
    """Registry of spawned ``alkera-agent`` (harness) subprocesses:
    ``<ALKERA_HOME>/agents``.

    Each live agent drops a ``<agent_pid>.json`` breadcrumb naming the agent
    PID and the PID of the ``alkera`` that spawned it. The startup orphan sweep
    reads these to reap an agent whose parent died ungracefully — the macOS
    safety net where there's no Job Object / PR_SET_PDEATHSIG to bind the
    child's lifetime to the parent's. A function (not a frozen constant) so it
    honors a ``monkeypatch.setattr(paths, "ALKERA_HOME", ...)`` in tests.
    """
    return ALKERA_HOME / "agents"


PROJECT_STATE_DIRNAME = ".alkera"
"""The per-workspace state directory every project root carries."""


def project_state_dir(root: Path) -> Path:
    """``<root>/.alkera``, the workspace's state directory. Creates nothing."""
    return root / PROJECT_STATE_DIRNAME


def project_directory(root: Path, *, create_if_missing: bool = True) -> ProjectDirectory:
    """The ``ProjectDirectory`` for the workspace at ``root``.

    ``create_if_missing`` is passed through: the default creates ``<root>/.alkera``,
    ``False`` raises ``FileNotFoundError`` when it is absent."""
    # Imported here so the auth and config readers that import this module stay
    # free of pydantic and the project store.
    from alkera_core.project import ProjectDirectory

    return ProjectDirectory(project_state_dir(root), create_if_missing=create_if_missing)


def existing_project_directory(root: Path | None) -> ProjectDirectory | None:
    """The workspace at ``root``, or ``None`` when ``root`` is ``None`` or has no
    ``.alkera/`` yet: a folder nothing has run in has no pin or state to read,
    and reading it must not create one."""
    if root is None:
        return None
    try:
        return project_directory(root, create_if_missing=False)
    except FileNotFoundError:
        return None


def ensure_home() -> Path:
    """Create `ALKERA_HOME` if missing, with mode 0700. Returns the path."""
    ALKERA_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ALKERA_HOME


__all__ = [
    "ALKERA_HOME",
    "AUTH_FILE_PATH",
    "INSTRUCTIONS_FILE_PATH",
    "INSTRUCTIONS_LOCK_PATH",
    "PREFERENCES_FILE_PATH",
    "PREFERENCES_LOCK_PATH",
    "PROJECT_STATE_DIRNAME",
    "SYSTEM_CONFIG_PATH",
    "agents_dir",
    "cache_dir",
    "ensure_home",
    "existing_project_directory",
    "project_directory",
    "project_state_dir",
]
