"""The workspace's own files, as a version-controlled repository lists them.

``discover_files`` honours ``.gitignore`` through ripgrep and falls back to a plain
walk with a hardcoded skip list when ripgrep is unavailable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import structlog
from alkera_core.process import SpawnSpec, run

from alkera_cli.harness.opencode_binary import resolve_ripgrep_bin

log = structlog.get_logger(__name__)

# Directories never worth seeding (vendored, generated, virtualenvs, VCS, caches).
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".alkera",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "out",
        ".next",
        ".nuxt",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "coverage",
        ".idea",
        ".vscode",
        "site-packages",
    }
)

# Lockfiles + obvious generated/secret files we must not ingest.
_SKIP_NAMES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "go.sum",
        ".env",
    }
)


def discover_files(root: Path) -> list[Path]:
    """Candidate files under ``root``, HONORING ``.gitignore``.

    Prefers ripgrep (``rg --files``), which respects ``.gitignore``/``.ignore``
    (+ git's exclude files) — so locally-ignored junk (data dumps, logs, build
    output, secrets) never leaks into the KB. ``--hidden`` keeps the dotfiles we
    DO want (``.editorconfig``, ``.github/``); ``.git``/``.alkera`` are excluded
    explicitly. Falls back to a plain recursive walk with the hardcoded skip-lists
    when rg is unavailable (a bare environment), so seeding always works."""
    rg = _resolve_rg()
    if rg is not None:
        files = _rg_files(rg, root)
        if files is not None:
            return files
    return _walk(root)


def _resolve_rg() -> Path | None:
    try:
        return resolve_ripgrep_bin()
    except Exception:
        log.debug("repo_files.rg_unresolved", exc_info=True)
        return None


def _rg_files(rg: Path, root: Path) -> list[Path] | None:
    """``rg --files`` under ``root`` → absolute file paths, or ``None`` on any rg
    failure so the caller falls back to the plain walk."""
    try:
        proc = run(
            SpawnSpec(
                argv=[str(rg), "--files", "--hidden", "--glob", "!.git", "--glob", "!.alkera"],
                env=os.environ,
                cwd=root,
                stdout="pipe",
                stderr="pipe",
            ),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):  # 0 = listed, 1 = nothing matched; else error
        log.warning("context.seed.rg_failed", returncode=proc.returncode)
        return None
    out: list[Path] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        rel = line.strip()
        if not rel:
            continue
        path = root / rel
        # .gitignore (via rg) is the primary filter; the hardcoded lists are a
        # belt-and-suspenders backstop so a repo that forgot to ignore
        # node_modules/.venv/lockfiles still never gets those seeded.
        if path.is_file() and not _skip(path):
            out.append(path)
    return sorted(out)


def _skip(path: Path) -> bool:
    """A vendored/generated/cache/secret path we never seed, regardless of git."""
    return any(part in _SKIP_DIRS for part in path.parts) or path.name in _SKIP_NAMES


def _walk(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not _skip(p))


__all__ = ["discover_files"]
