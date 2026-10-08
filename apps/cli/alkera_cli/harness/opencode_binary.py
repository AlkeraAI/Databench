"""Locate the harness binary or source-mode invocation.

Resolution order. There is no PATH lookup: we only run binaries we own, so a
user-installed ``opencode`` can never intercept the flow with an unvetted
version.

1. `ALKERA_OPENCODE_BIN`: an explicit absolute path, for tests and edge cases.
2. Bundled: extracted from the daemon's onefile bundle to
   `~/.alkera/cache/runtime-<sha>/alkera-agent` on first call, atomically via
   temp+rename so concurrent daemon spawns are safe. The cache dir and
   executable use the product's names for its agent runtime (see
   `_OPENCODE_CACHE_PREFIX` / `_opencode_filename`).
3. Staged: `apps/cli/dist/opencode/alkera-agent`, produced by
   `make opencode-binary`. The dev fast path.
4. Source mode via Bun: runs the harness TypeScript entry from the vendored
   subtree. Needs `bun` on PATH and `vendor/opencode/` populated.

A bundled per-platform `rg` (`ripgrep_path`) is resolved alongside, in the same
tiers:

1. `ALKERA_RIPGREP_BIN` wins in every mode. Under an `ALKERA_OPENCODE_BIN`
   override, the `rg` sibling of that binary comes next.
2. Bundled: extracted from the onefile (`runtime/rg`) into the same
   `runtime-<sha>/` cache dir.
3. Staged: `apps/cli/dist/opencode/rg`.
4. bun-dev: the staged `rg` if a prior `make opencode-binary` produced one,
   otherwise `None`.

`None` leaves opencode's own ripgrep download enabled. When a path resolves,
the adapter prepends its dir to PATH and sets
`OPENCODE_DISABLE_RIPGREP_DOWNLOAD`.

`ResolvedOpencodeBinary` carries the binary path and prefix args, so one spawn
call works for a compiled binary and for `bun run …`::

    proc = await asyncio.create_subprocess_exec(
        str(resolved.path), *resolved.prefix_args, "serve",
        "--hostname", "127.0.0.1", "--port", "0", ...
    )
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from alkera_cli.host import paths

logger = logging.getLogger(__name__)

_LAST_USED_MARKER = ".last-used"
"""Per-cache-dir sentinel; its mtime records when that harness SHA was last
resolved, so periodic cleanup can reclaim only versions nobody still uses."""

# Cache dirs for the extracted agent runtime are `runtime-<sha>/` (formerly
# `opencode-<sha>/`). Kept in sync with the cleanup sweep below +
# `maintenance.cleanupCache`.
_OPENCODE_CACHE_PREFIX = "runtime-"


ResolvedSource = Literal[
    "env",
    "bundled",
    "staged",
    "bun-dev",
]


@dataclass(slots=True, frozen=True)
class ResolvedOpencodeBinary:
    """Where to find opencode + how to invoke it."""

    path: Path
    """Absolute path to the executable that gets argv[0]."""
    prefix_args: tuple[str, ...]
    """Args to prepend before our own (`serve`, `--port`, …). Used by
    the `bun run <ts>` path; empty for the compiled-binary path."""
    source: ResolvedSource
    """How we resolved this binary. Useful for debug + status."""
    sha: str | None = None
    """Opencode commit SHA, when known."""
    ripgrep_path: Path | None = None
    """Absolute path to the bundled per-platform ``rg``, when we ship one
    (bundled → extracted to cache; staged → sibling of the harness binary).
    ``None`` when no bundled rg is available (e.g. a bare bun-dev checkout
    that never ran ``make opencode-binary``); the adapter then leaves the
    runtime's GitHub download fallback enabled. When set, the adapter puts
    its directory FIRST on ``PATH`` and disables that download — so the
    harness never phones home for ripgrep. See ``_build_env``."""


def _opencode_filename() -> str:
    """The harness executable's filename for the current platform.

    The binary is our build of opencode, named `alkera-agent`, the product's
    name for its agent runtime. We rename Bun's `opencode` output to this when
    staging (build-opencode-binary.sh) and bundle it under this name
    (build-daemon-binary.sh), so the resolver looks for the matching name
    across staged/bundled/cache paths. Windows gets `.exe`. Read
    `sys.platform` at call time (not at import) so tests can monkeypatch it."""
    return "alkera-agent.exe" if sys.platform == "win32" else "alkera-agent"


def _ripgrep_filename() -> str:
    """The bundled ripgrep executable's filename for the current platform.

    Unlike the harness binary we DON'T rename this: opencode's own resolver
    looks for exactly `rg` / `rg.exe` on PATH (see
    `which.ts`), so the on-disk name must match for the PATH lookup to hit.
    Read `sys.platform` at call time so tests can monkeypatch it."""
    return "rg.exe" if sys.platform == "win32" else "rg"


def _resolve_env_ripgrep() -> Path | None:
    """`ALKERA_RIPGREP_BIN` override — an explicit absolute path to `rg`.

    The rg analogue of `ALKERA_OPENCODE_BIN`: highest-priority, used by tests
    and power users. Returns `None` (not an error) when unset or not
    executable, so resolution falls through to the bundled/staged rg."""
    raw = os.environ.get("ALKERA_RIPGREP_BIN", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return path.absolute()
    return None


def _staged_ripgrep(stage_dir: Path) -> Path | None:
    """The staged `rg` sibling of the harness binary, if present + executable.

    `stage_dir` is `apps/cli/dist/opencode/` (staged + bun-dev) — populated by
    `make opencode-binary`'s `stage_ripgrep`."""
    rg = stage_dir / _ripgrep_filename()
    if rg.is_file() and os.access(rg, os.X_OK):
        return rg.absolute()
    return None


def resolve_ripgrep_bin() -> Path | None:
    """A best-effort standalone `rg` for callers that only need ripgrep (e.g. the
    KB seed's gitignore-aware file walk) WITHOUT resolving the whole harness
    binary. Tiers: ``ALKERA_RIPGREP_BIN`` → `rg` on PATH → the bundled/staged rg
    that rides with the harness (best-effort; never raises). ``None`` when none
    is found, so the caller can degrade (the seed falls back to a plain walk).
    Any rg version works — we only use ``rg --files``."""
    env = _resolve_env_ripgrep()
    if env is not None:
        return env
    on_path = shutil.which(_ripgrep_filename())
    if on_path:
        return Path(on_path).absolute()
    try:
        return resolve_opencode_binary().ripgrep_path
    except Exception:
        logger.debug("no bundled ripgrep: the harness binary did not resolve", exc_info=True)
        return None


class OpencodeBinaryNotFoundError(RuntimeError):
    """Raised when no resolution path produces a usable harness binary.

    The message names opencode, the harness the binary provides.
    """

    def __init__(self, attempted: list[str], *, dev_hint: str | None = None) -> None:
        msg = "Could not locate the opencode harness binary. Tried:\n" + "\n".join(
            f"  - {a}" for a in attempted
        )
        # In a source checkout the fix is always one command; lead with it so the
        # actionable hint is the first thing a dev sees. None in production.
        if dev_hint:
            msg = f"{dev_hint}\n\n{msg}"
        super().__init__(msg)
        self.attempted = attempted


def resolve_opencode_binary(*, repo_root: Path | None = None) -> ResolvedOpencodeBinary:
    """Resolve opencode invocation. See module docstring for order.

    `repo_root` is the alkera repo root (for finding vendor/opencode in
    source-mode). If `None`, we infer from this module's location —
    works for dev mode; production (Nuitka onefile) skips it.
    """
    attempted: list[str] = []

    # An explicit `ALKERA_RIPGREP_BIN` wins over the bundled/staged rg in
    # every mode (tests + power users). Resolved once, threaded into whichever
    # binary tier we pick below.
    env_rg = _resolve_env_ripgrep()

    # (1) Env var override — explicit absolute path.
    env_bin = os.environ.get("ALKERA_OPENCODE_BIN", "").strip()
    if env_bin:
        env_path = Path(env_bin).expanduser()
        if env_path.is_file() and os.access(env_path, os.X_OK):
            # A compiled build ships its `rg` beside it (the staged layout), so an
            # override pointed at one carries that rg too. Without it opencode
            # falls back to downloading rg from github.com on the first glob/grep.
            return ResolvedOpencodeBinary(
                path=env_path.absolute(),
                prefix_args=(),
                source="env",
                ripgrep_path=env_rg or _staged_ripgrep(env_path.parent),
            )
        attempted.append(f"ALKERA_OPENCODE_BIN={env_bin!r} (not executable)")

    # (2) Nuitka-bundled binary — daemon onefile case.
    bundled = _resolve_bundled_binary()
    if bundled is not None:
        # An explicit ALKERA_RIPGREP_BIN overrides the bundle's own rg.
        return replace(bundled, ripgrep_path=env_rg) if env_rg else bundled
    attempted.append("bundled harness binary (none found)")

    # (3) Staged binary from `make opencode-binary`.
    root = repo_root or _infer_repo_root()
    if root is not None:
        staged = root / "apps" / "cli" / "dist" / "opencode" / _opencode_filename()
        if staged.is_file() and os.access(staged, os.X_OK):
            sha = _read_sha(staged.parent / ".alkera-sha")
            return ResolvedOpencodeBinary(
                path=staged.absolute(),
                prefix_args=(),
                source="staged",
                sha=sha,
                ripgrep_path=env_rg or _staged_ripgrep(staged.parent),
            )
        attempted.append(f"staged binary at {staged.name} (not present)")

        # (4) Bun-dev — point at opencode source. Requires the vendored deps to be
        # installed (`make opencode-binary` does this); without node_modules a
        # `bun run` exits immediately with no output (the cryptic "harness exited
        # before reporting its listen URL"), so we DON'T offer bun-dev in that
        # state — we surface the one-time build step instead (see the dev hint).
        oc_dir = root / "vendor" / "opencode"
        oc_src = oc_dir / "packages" / "opencode" / "src" / "index.ts"
        oc_deps = oc_dir / "node_modules"
        bun_path = shutil.which("bun")
        if oc_src.is_file() and bun_path is not None and oc_deps.is_dir():
            return ResolvedOpencodeBinary(
                path=Path(bun_path),
                prefix_args=("run", str(oc_src.absolute())),
                source="bun-dev",
                sha=_git_head_sha(oc_dir),
                # A dev who ran `make opencode-binary` gets the staged rg even
                # in source mode; otherwise None → opencode downloads rg itself.
                ripgrep_path=env_rg or _staged_ripgrep(root / "apps" / "cli" / "dist" / "opencode"),
            )
        if not oc_src.is_file():
            attempted.append(
                "harness source (missing — vendor/opencode is committed in-tree; "
                "re-checkout the repo or run `make opencode-bump`)"
            )
        if bun_path is None:
            attempted.append("bun on PATH (not found — install from https://bun.sh)")
        if oc_src.is_file() and bun_path is not None and not oc_deps.is_dir():
            attempted.append(
                "harness deps (vendor/opencode/node_modules missing — run `make opencode-binary`)"
            )

    # No PATH fallback: we never run a harness binary we didn't ship
    # ourselves. Forces users into the bundled path in production +
    # the vendored source in dev.

    # A resolved repo root means we're in a SOURCE CHECKOUT (local dev) — the
    # failure is always a one-time setup gap, so lead with the fix. Production
    # (Nuitka onefile) infers no repo root → no hint, because `make
    # opencode-binary` is meaningless to a packaged install.
    dev_hint = (
        "Local dev: the harness isn't built yet. Run `make opencode-binary` "
        "(one-time; needs bun) — this is only needed in a source checkout."
        if root is not None
        else None
    )
    raise OpencodeBinaryNotFoundError(attempted=attempted, dev_hint=dev_hint)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _infer_repo_root() -> Path | None:
    """Walk up from this file looking for a `Makefile` / `pyproject.toml`
    that screams "alkera root". `None` if we can't find it (likely we're
    running from a Nuitka onefile bundle)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "Makefile").is_file():
            return parent
    return None


def _resolve_bundled_binary() -> ResolvedOpencodeBinary | None:
    """Production-mode (Nuitka onefile): the opencode binary was bundled
    via `--include-data-files=apps/cli/dist/opencode/alkera-agent=runtime/alkera-agent`.
    At runtime it lives in Nuitka's onefile extract dir alongside the
    compiled Python modules — we copy it into a stable cache so concurrent
    daemons share one extracted copy, and so we don't re-exec from the
    Nuitka temp dir on every invocation.

    Resolution: Nuitka 4.x sets `__compiled__` on every compiled module.
    We use that purely as a "are we running inside a Nuitka build?"
    sentinel — the historically-documented `__compiled__.containing_dir`
    attribute is NOT reliably present (different across Nuitka versions
    and may be missing entirely). The robust source of the extract dir
    is `Path(__file__).parents[2]`, because our own module file lives
    at `<extract_dir>/alkera_cli/harness/opencode_binary.py`. The
    bundled opencode lands at `<extract_dir>/runtime/alkera-agent` via the
    `--include-data-files` flag in the build script. The bundled `rg` rides
    along at `<extract_dir>/runtime/rg` and is extracted next to it."""
    if not getattr(sys.modules.get("__main__"), "__compiled__", None):
        # Dev / source mode — fall through to staged or bun-dev.
        return None
    try:
        bundle_dir = Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None
    bundled_bin = bundle_dir / "runtime" / _opencode_filename()
    sha_file = bundle_dir / "runtime" / ".alkera-sha"
    if not (bundled_bin.is_file() and sha_file.is_file()):
        return None
    sha = _read_sha(sha_file)
    if sha is None:
        # Defensive — without an SHA we'd cache to an undefined path.
        return None

    cache_dir = paths.cache_dir() / f"{_OPENCODE_CACHE_PREFIX}{sha}"
    cache_bin = cache_dir / _opencode_filename()
    if not (
        cache_bin.is_file()
        and os.access(cache_bin, os.X_OK)
        and _files_match(bundled_bin, cache_bin)
    ):
        cache_dir.mkdir(parents=True, exist_ok=True)
        _extract_to_cache(bundled_bin, cache_bin)
        logger.info("Extracted bundled runtime (sha=%s) → %s", sha, cache_bin)

    # Extract the bundled rg into the SAME cache dir (best-effort: an older
    # bundle built before rg-bundling has no rg → ripgrep_path stays None and
    # the adapter falls back to opencode's own download).
    ripgrep_path = _resolve_bundled_ripgrep(bundle_dir, cache_dir)

    _touch_last_used(cache_dir)
    return ResolvedOpencodeBinary(
        path=cache_bin.absolute(),
        prefix_args=(),
        source="bundled",
        sha=sha,
        ripgrep_path=ripgrep_path,
    )


def _resolve_bundled_ripgrep(bundle_dir: Path, cache_dir: Path) -> Path | None:
    """Extract the bundled `rg` (`<bundle>/runtime/rg`) into ``cache_dir``,
    next to the extracted harness binary, and return its absolute path.

    Mirrors the harness-binary extraction (atomic, dedup on cache-hit). Returns
    `None` when the bundle carries no rg — keeps older onefiles (built before
    rg-bundling) working by deferring to opencode's download fallback."""
    bundled_rg = bundle_dir / "runtime" / _ripgrep_filename()
    if not bundled_rg.is_file():
        return None
    cache_rg = cache_dir / _ripgrep_filename()
    if not (
        cache_rg.is_file() and os.access(cache_rg, os.X_OK) and _files_match(bundled_rg, cache_rg)
    ):
        cache_dir.mkdir(parents=True, exist_ok=True)
        _extract_to_cache(bundled_rg, cache_rg)
        logger.info("Extracted bundled ripgrep → %s", cache_rg)
    return cache_rg.absolute()


def _files_match(src: Path, cached: Path) -> bool:
    """True iff ``src`` and ``cached`` are byte-identical (size, then content
    hash). The cache dir is keyed by the vendor git fingerprint, which is
    CONTENT-BLIND — so a rebuilt or re-signed agent carrying the SAME
    fingerprint must still replace a mismatched cached copy. This is the guard
    against the field bug where a signed Windows ``alkera-agent.exe`` was
    shadowed by a prior build's unsigned extraction (Authenticode changes the
    size, so the cheap stat check alone already catches that case)."""
    try:
        if src.stat().st_size != cached.stat().st_size:
            return False
    except OSError:
        return False
    return _sha256(src) == _sha256(cached)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_to_cache(src: Path, dst: Path) -> None:
    """Copy ``src`` → ``dst`` atomically (temp in the same dir → chmod →
    rename), so concurrent daemon spawns never observe a half-written binary.
    ``dst.parent`` must already exist.

    Replace-while-running (the in-place UPDATE case): on Windows,
    ``os.replace`` onto an EXECUTING image fails with ``PermissionError``
    (WinError 5). That is exactly the state after an extension update whose
    rebuilt/re-signed agent lands in the SAME vendor-sha cache dir while an
    agent from the old install is still running from it — the field failure
    where every chat died on "Access is denied" until that process exited.
    Windows DOES permit RENAMING a running image, so on that error we rename
    the live exe aside (``.old-…``) and install the new one; the running
    process keeps its (renamed) file, and the sweep below reclaims leftovers
    on later stagings."""
    _sweep_replaced_binaries(dst.parent)
    with tempfile.NamedTemporaryFile(
        dir=dst.parent, prefix=".agent.", suffix=".tmp", delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)
    try:
        shutil.copyfile(src, tmp_path)
        tmp_path.chmod(0o755)
        try:
            os.replace(tmp_path, dst)
        except PermissionError:
            aside = dst.with_name(f".old-{dst.name}.{os.getpid()}.{int(time.time())}")
            os.replace(dst, aside)
            os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _sweep_replaced_binaries(cache_dir: Path) -> None:
    """Best-effort unlink of ``.old-*`` binaries a replace-while-running
    staging renamed aside. One still held by a live process just stays for a
    later sweep; nothing here may fail a staging."""
    try:
        leftovers = list(cache_dir.glob(".old-*"))
    except OSError:
        return
    for leftover in leftovers:
        try:
            leftover.unlink()
        except OSError:
            continue


def _touch_last_used(cache_dir: Path) -> None:
    """Bump the cache dir's ``.last-used`` marker so periodic cleanup knows
    this opencode SHA is still in service. Best-effort — a marker write must
    never fail binary resolution (the spawn that follows is what matters)."""
    try:
        (cache_dir / _LAST_USED_MARKER).touch(exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("could not touch last-used marker in %s: %s", cache_dir, exc)


def _cache_mtime(cache_dir: Path) -> float:
    """Last-used time for a cache dir: the ``.last-used`` marker's mtime when
    present, else the dir's own mtime (covers dirs extracted before the marker
    convention existed)."""
    marker = cache_dir / _LAST_USED_MARKER
    if marker.exists():
        return marker.stat().st_mtime
    return cache_dir.stat().st_mtime


@dataclass(slots=True, frozen=True)
class CacheCleanupResult:
    """Outcome of one cleanup sweep over the opencode cache root."""

    removed: tuple[str, ...]
    """SHAs whose cache dirs were deleted."""
    kept: tuple[str, ...]
    """SHAs left in place — still fresh, or the protected active SHA."""
    errors: tuple[str, ...]
    """``"<sha>: <reason>"`` for dirs we tried but failed to remove."""


def cleanup_stale_opencode_caches(
    *,
    max_age_seconds: float,
    keep_sha: str | None = None,
    now: float | None = None,
) -> CacheCleanupResult:
    """Delete ``opencode-<sha>`` cache dirs unused for ``max_age_seconds``.

    "Age" is measured from each dir's ``.last-used`` marker (bumped on every
    resolution), falling back to the dir's own mtime when the marker is
    absent. The dir named for ``keep_sha`` is never removed regardless of age,
    so the currently-active opencode survives even on a long-lived daemon that
    hasn't re-resolved recently.

    Deletion failures are recorded in the result rather than raised: on
    Windows the OS refuses to remove an executing ``.exe``, so a version still
    in use by another editor window simply survives the sweep. (On POSIX,
    removing an in-use binary is harmless — the running process keeps its
    inode and only a fresh spawn re-extracts.)

    ``now`` is injectable so tests are deterministic.
    """
    root = paths.cache_dir()
    removed: list[str] = []
    kept: list[str] = []
    errors: list[str] = []
    if not root.is_dir():
        return CacheCleanupResult(removed=(), kept=(), errors=())

    current = now if now is not None else time.time()
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith(_OPENCODE_CACHE_PREFIX):
            continue
        sha = entry.name[len(_OPENCODE_CACHE_PREFIX) :]
        if keep_sha is not None and sha == keep_sha:
            kept.append(sha)
            continue
        try:
            age = current - _cache_mtime(entry)
        except OSError as exc:
            errors.append(f"{sha}: stat failed: {exc}")
            continue
        if age < max_age_seconds:
            kept.append(sha)
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            errors.append(f"{sha}: {exc}")
            continue
        removed.append(sha)
        logger.info("removed stale runtime cache (sha=%s)", sha)

    return CacheCleanupResult(removed=tuple(removed), kept=tuple(kept), errors=tuple(errors))


def _read_sha(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except (OSError, FileNotFoundError):
        return None


def _git_head_sha(dir_: Path) -> str | None:
    """Best-effort: read `<dir>/.git/HEAD` or the resolved file."""
    head_path = dir_ / ".git"
    try:
        if head_path.is_file():
            # Submodule indirection — `.git` is a file pointing at the
            # real gitdir.
            content = head_path.read_text().strip()
            if content.startswith("gitdir: "):
                head_path = (dir_ / content[len("gitdir: ") :]).resolve()
            else:
                return None
        head_file = head_path / "HEAD"
        if not head_file.is_file():
            return None
        ref = head_file.read_text().strip()
        if ref.startswith("ref: "):
            ref_file = head_path / ref[len("ref: ") :]
            if ref_file.is_file():
                return ref_file.read_text().strip()
            return None
        return ref or None
    except OSError:
        return None


__all__ = [
    "CacheCleanupResult",
    "OpencodeBinaryNotFoundError",
    "ResolvedOpencodeBinary",
    "ResolvedSource",
    "cleanup_stale_opencode_caches",
    "resolve_opencode_binary",
    "resolve_ripgrep_bin",
]
