"""Tests for `alkera_cli.harness.opencode_binary` resolver."""

from __future__ import annotations

import os
import stat
import sys
import time
import types
from pathlib import Path

import pytest
from alkera_cli.harness.opencode_binary import (
    _LAST_USED_MARKER,
    OpencodeBinaryNotFoundError,
    _opencode_filename,
    _resolve_bundled_binary,
    _ripgrep_filename,
    cleanup_stale_opencode_caches,
    resolve_opencode_binary,
)
from alkera_cli.host import paths


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_env_override_used_when_executable(tmp_path: Path, monkeypatch) -> None:
    bin_path = _make_executable(tmp_path / "my_opencode")
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(bin_path))
    # Block other resolution paths so env is unambiguously the source.
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "env"
    assert resolved.path == bin_path
    assert resolved.prefix_args == ()


def _block_other_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)


def test_env_override_carries_the_rg_staged_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override pointed at a staged build gets that build's rg, so the agent
    never falls back to downloading one from github.com mid-tool-call. This is
    the Windows e2e job's layout: `ALKERA_OPENCODE_BIN` names the compiled
    `alkera-agent.exe` and `make opencode-ripgrep` staged `rg.exe` beside it."""
    stage = tmp_path / "stage"
    stage.mkdir()
    bin_path = _make_executable(stage / _opencode_filename())
    rg = _make_executable(stage / _ripgrep_filename())
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(bin_path))
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    _block_other_tiers(monkeypatch)

    resolved = resolve_opencode_binary()
    assert resolved.source == "env"
    assert resolved.ripgrep_path == rg.absolute()


def test_env_override_without_a_staged_rg_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rg beside the override: nothing to ship, so the adapter keeps the
    agent's own fallback rather than naming an rg that is not there."""
    bin_path = _make_executable(tmp_path / _opencode_filename())
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(bin_path))
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    _block_other_tiers(monkeypatch)

    assert resolve_opencode_binary().ripgrep_path is None


def test_env_ripgrep_override_wins_over_the_rg_beside_an_env_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    bin_path = _make_executable(stage / _opencode_filename())
    _make_executable(stage / _ripgrep_filename())
    (tmp_path / "custom").mkdir()
    override_rg = _make_executable(tmp_path / "custom" / _ripgrep_filename())
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(bin_path))
    monkeypatch.setenv("ALKERA_RIPGREP_BIN", str(override_rg))
    _block_other_tiers(monkeypatch)

    assert resolve_opencode_binary().ripgrep_path == override_rg.absolute()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod-based executability is POSIX; on Windows os.access(X_OK) is just existence",
)
def test_env_override_skipped_if_not_executable(
    tmp_path: Path, monkeypatch, require_effective_chmod: None
) -> None:
    """A non-executable env path falls through to other resolution
    layers rather than failing the whole call."""
    plain_file = tmp_path / "not_executable"
    plain_file.write_text("text")
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(plain_file))
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(OpencodeBinaryNotFoundError) as exc:
        resolve_opencode_binary()
    assert "ALKERA_OPENCODE_BIN" in str(exc.value)


def test_no_resolution_raises_with_audit_trail(monkeypatch) -> None:
    """When every path fails, the error message reports the bundled
    binary as one of the attempted locations. Specifically should
    NOT mention `opencode on PATH` — we deliberately don't fall back
    to a user-installed harness."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(OpencodeBinaryNotFoundError) as exc:
        resolve_opencode_binary()
    msg = str(exc.value)
    assert "bundled harness" in msg
    # Regression guard: a PATH lookup would let an unvetted
    # user-installed version intercept.
    assert "harness on PATH" not in msg
    assert "opencode on PATH" not in msg


def test_staged_binary_used_when_present(tmp_path: Path, monkeypatch) -> None:
    """If `apps/cli/dist/opencode/opencode` exists and is executable,
    it's preferred over bun-dev."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    # Build a fake repo root with the staged binary.
    fake_root = tmp_path / "alkera_repo"
    staged_dir = fake_root / "apps" / "cli" / "dist" / "opencode"
    staged_dir.mkdir(parents=True)
    staged_bin = _make_executable(staged_dir / _opencode_filename())
    (staged_dir / ".alkera-sha").write_text("abc1234\n")
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "staged"
    assert resolved.path == staged_bin.absolute()
    assert resolved.sha == "abc1234"


def test_opencode_filename_is_platform_aware(monkeypatch) -> None:
    """The harness leaf name carries `.exe` only on Windows."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert _opencode_filename() == "alkera-agent.exe"
    monkeypatch.setattr(sys, "platform", "linux")
    assert _opencode_filename() == "alkera-agent"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _opencode_filename() == "alkera-agent"


def test_staged_binary_uses_exe_suffix_on_windows(tmp_path: Path, monkeypatch) -> None:
    """On Windows the resolver resolves the staged `opencode.exe` — Bun and
    Nuitka emit a `.exe` on win32 and the build bundles it under that name."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = tmp_path / "alkera_repo"
    staged_dir = fake_root / "apps" / "cli" / "dist" / "opencode"
    staged_dir.mkdir(parents=True)
    staged_bin = _make_executable(staged_dir / "alkera-agent.exe")
    (staged_dir / ".alkera-sha").write_text("abc1234\n")
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "staged"
    assert resolved.path == staged_bin.absolute()
    assert resolved.path.name == "alkera-agent.exe"
    assert resolved.sha == "abc1234"


def test_bun_dev_used_when_source_and_bun_present(tmp_path: Path, monkeypatch) -> None:
    """If no staged binary but vendor/opencode is checked out + bun is
    on PATH, use the source-mode invocation."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = tmp_path / "alkera_repo"
    oc_src_dir = fake_root / "vendor" / "opencode" / "packages" / "opencode" / "src"
    oc_src_dir.mkdir(parents=True)
    oc_src = oc_src_dir / "index.ts"
    oc_src.write_text("// opencode entry\n")
    (fake_root / "vendor" / "opencode" / "node_modules").mkdir()  # deps installed
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    # Pretend bun is on PATH.
    fake_bun = _make_executable(tmp_path / "bin" / "bun") if False else None
    fake_bun_dir = tmp_path / "bin"
    fake_bun_dir.mkdir(exist_ok=True)
    fake_bun = _make_executable(fake_bun_dir / "bun")
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bun) if name == "bun" else None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "bun-dev"
    assert resolved.path == Path(str(fake_bun))
    assert resolved.prefix_args == ("run", str(oc_src.absolute()))


def test_bun_dev_missing_deps_raises_with_make_hint(tmp_path: Path, monkeypatch) -> None:
    """Source checked out + bun present but vendor deps NOT installed: we do NOT
    offer the (doomed) bun-dev invocation — it would exit before reporting its
    listen URL. Instead we fail loudly with the one-time `make opencode-binary`
    fix (the local-dev reminder this whole change is about)."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = tmp_path / "alkera_repo"
    oc_src_dir = fake_root / "vendor" / "opencode" / "packages" / "opencode" / "src"
    oc_src_dir.mkdir(parents=True)
    (oc_src_dir / "index.ts").write_text("// opencode entry\n")
    # NOTE: deliberately NO vendor/opencode/node_modules.
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    fake_bun_dir = tmp_path / "bin"
    fake_bun_dir.mkdir()
    fake_bun = _make_executable(fake_bun_dir / "bun")
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bun) if name == "bun" else None)

    with pytest.raises(OpencodeBinaryNotFoundError) as exc:
        resolve_opencode_binary()
    msg = str(exc.value)
    assert "make opencode-binary" in msg  # the actionable local-dev reminder
    assert "node_modules" in msg  # the precise reason


def test_no_dev_hint_in_production(monkeypatch) -> None:
    """A packaged install infers no repo root, so the not-found error must NOT
    mention `make opencode-binary` — that step is meaningless to a bundled build
    (the dev hint is strictly local-dev-only)."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(OpencodeBinaryNotFoundError) as exc:
        resolve_opencode_binary()
    assert "make opencode-binary" not in str(exc.value)


def test_no_path_fallback_even_if_opencode_is_installed_globally(
    tmp_path: Path, monkeypatch
) -> None:
    """Even when the user has a global `opencode` on PATH, we MUST
    NOT use it. Running a third-party-installed harness is a security
    risk + would mask real misconfiguration in dev environments where
    the vendored subtree isn't checked out."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: None)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_global = _make_executable(tmp_path / "opencode")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: str(fake_global) if name == "opencode" else None,
    )

    # The global binary must be ignored — resolution must fail loudly.
    with pytest.raises(OpencodeBinaryNotFoundError):
        resolve_opencode_binary()


# ---------------------------------------------------------------------------
# Bundled resolution (Nuitka onefile) — exercises _resolve_bundled_binary().
#
# We can't actually run Nuitka in unit tests, but the resolver's contract
# is two-fold:
#   1. `sys.modules["__main__"].__compiled__` exists (truthy) — Nuitka sets
#      this on every compiled module to signal we're running a build.
#   2. The runtime/ subdir lives at `Path(opencode_binary.__file__)
#      .parents[2]`, because Nuitka extracts our module alongside the
#      bundled data files (e.g. .../alkera_cli/harness/opencode_binary.py
#      next to .../runtime/alkera-agent).
#
# We monkeypatch BOTH to simulate a Nuitka extract dir.
# ---------------------------------------------------------------------------


def _fake_compiled_bundle(
    tmp_path: Path, *, sha: str, body: bytes, rg_body: bytes | None = None
) -> Path:
    """Lay out a fake Nuitka onefile extract dir mirroring the real
    structure: a placeholder `alkera_cli/harness/opencode_binary.py` (so
    `parents[2]` resolves to our bundle root) plus the bundled opencode
    binary + .alkera-sha. When `rg_body` is given, also lay down the bundled
    `runtime/rg` (older bundles built before rg-bundling omit it)."""
    bundle_dir = tmp_path / "nuitka_extract"
    # Placeholder module file — resolver only uses its PATH, not contents.
    module_path = bundle_dir / "alkera_cli" / "harness" / "opencode_binary.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# fake nuitka-extracted module\n")
    # The bundled harness binary + sha, under the `runtime/` dest (matches
    # build-daemon-binary.sh's --include-data-files + the resolver's read path).
    runtime_dir = bundle_dir / "runtime"
    runtime_dir.mkdir(parents=True)
    bin_path = runtime_dir / _opencode_filename()
    bin_path.write_bytes(body)
    bin_path.chmod(0o755)
    (runtime_dir / ".alkera-sha").write_text(sha + "\n")
    if rg_body is not None:
        rg_path = runtime_dir / _ripgrep_filename()
        rg_path.write_bytes(rg_body)
        rg_path.chmod(0o755)
        (runtime_dir / ".alkera-rg-version").write_text("15.1.0\n")
    return bundle_dir


def _install_fake_compiled(monkeypatch, bundle_dir: Path) -> None:
    """Simulate a Nuitka environment: ``sys.modules['__main__']
    .__compiled__`` is set, AND ``opencode_binary.__file__`` points at
    the fake extracted-module path inside ``bundle_dir``."""
    import alkera_cli.harness.opencode_binary as oc_mod

    main_mod = sys.modules.get("__main__")
    if main_mod is None:
        main_mod = types.ModuleType("__main__")
        monkeypatch.setitem(sys.modules, "__main__", main_mod)
    # Truthy sentinel — value shape doesn't matter to the resolver.
    monkeypatch.setattr(main_mod, "__compiled__", 1, raising=False)
    fake_module_file = bundle_dir / "alkera_cli" / "harness" / "opencode_binary.py"
    monkeypatch.setattr(oc_mod, "__file__", str(fake_module_file))


def _cache_dir_for(sha: str, monkeypatch, tmp_path: Path) -> Path:
    """Redirect the alkera home so the cache lands inside the test's tmp
    instead of polluting the dev's real ``~/.alkera/cache/``. We patch the
    ``paths.ALKERA_HOME`` constant — the same hook the preferences/auth tests
    use — since ``opencode_binary`` derives the cache root from it via
    ``paths.cache_dir()``."""
    alkera_home = tmp_path / "alkera-home"
    alkera_home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", alkera_home)
    return alkera_home / "cache" / f"runtime-{sha}"


def test_bundled_resolver_returns_none_without_compiled_attribute(
    monkeypatch,
) -> None:
    """No `__compiled__` on `__main__` (dev / source run) → None."""
    main_mod = sys.modules["__main__"]
    monkeypatch.delattr(main_mod, "__compiled__", raising=False)
    assert _resolve_bundled_binary() is None


def test_bundled_resolver_returns_none_when_bundle_lacks_files(tmp_path: Path, monkeypatch) -> None:
    """`__compiled__` is set but the expected runtime/ subdir isn't
    inside the extract (e.g., older Nuitka build that didn't include
    the data files) → None, fall-through to staged / bun-dev."""
    bundle_dir = tmp_path / "nuitka_extract"
    # Create just the placeholder module path; OMIT runtime/.
    (bundle_dir / "alkera_cli" / "harness").mkdir(parents=True)
    (bundle_dir / "alkera_cli" / "harness" / "opencode_binary.py").write_text("")
    _install_fake_compiled(monkeypatch, bundle_dir)
    assert _resolve_bundled_binary() is None


def test_bundled_resolver_extracts_to_cache_on_first_call(tmp_path: Path, monkeypatch) -> None:
    """First call against a populated bundle: returns source='bundled',
    cache file matches the bundled file byte-for-byte + is executable."""
    sha = "deadbeefcafefeed1122334455667788"
    payload = b"#!/bin/sh\necho fake-opencode\n"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=payload)
    _install_fake_compiled(monkeypatch, bundle_dir)
    cache_dir = _cache_dir_for(sha, monkeypatch, tmp_path)

    resolved = _resolve_bundled_binary()
    assert resolved is not None
    assert resolved.source == "bundled"
    assert resolved.sha == sha
    assert resolved.prefix_args == ()
    # The returned path is the CACHE path, not the bundle path.
    cache_bin = cache_dir / _opencode_filename()
    assert resolved.path == cache_bin.absolute()
    assert cache_bin.is_file()
    assert cache_bin.read_bytes() == payload
    # Executable bit set.
    import os

    assert os.access(cache_bin, os.X_OK)


def test_bundled_resolver_reuses_cache_when_content_matches(tmp_path: Path, monkeypatch) -> None:
    """Second call with IDENTICAL bundle content returns the cached path
    without re-copying — the dedupe the resolver promises so concurrent
    daemons don't repeatedly extract the binary."""
    sha = "1111222233334444555566667777888899990000"
    payload = b"first-extract-body"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=payload)
    _install_fake_compiled(monkeypatch, bundle_dir)
    cache_dir = _cache_dir_for(sha, monkeypatch, tmp_path)

    first = _resolve_bundled_binary()
    assert first is not None
    cache_bin = cache_dir / _opencode_filename()
    first_mtime = cache_bin.stat().st_mtime_ns

    second = _resolve_bundled_binary()  # bundle unchanged
    assert second is not None
    assert second.path == first.path
    assert cache_bin.read_bytes() == payload
    assert cache_bin.stat().st_mtime_ns == first_mtime  # not rewritten


def test_bundled_resolver_refreshes_cache_when_bundle_content_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """The cache dir is keyed by the CONTENT-BLIND vendor fingerprint, so a
    rebuilt/re-signed agent carrying the SAME fingerprint must still replace a
    mismatched cached copy. Pins the field bug: a signed Windows
    alkera-agent.exe was shadowed by a prior build's unsigned extraction at the
    same cache key, so the broken old binary kept running."""
    sha = "1111222233334444555566667777888899990000"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"old-unsigned-body")
    _install_fake_compiled(monkeypatch, bundle_dir)
    cache_dir = _cache_dir_for(sha, monkeypatch, tmp_path)

    first = _resolve_bundled_binary()
    assert first is not None
    cache_bin = cache_dir / _opencode_filename()

    # Same SHA (fingerprint unchanged) but DIFFERENT bytes (e.g. now signed,
    # which also changes the size) — the resolver must re-extract the new bytes.
    new_body = b"new-signed-body-with-a-cert-table-appended"
    (bundle_dir / "runtime" / _opencode_filename()).write_bytes(new_body)
    second = _resolve_bundled_binary()
    assert second is not None
    assert second.path == cache_bin
    assert cache_bin.read_bytes() == new_body  # cache refreshed, not stale


def test_resolve_picks_bundled_over_staged(tmp_path: Path, monkeypatch) -> None:
    """Resolution-order regression: when BOTH a bundled bundle AND a
    staged binary are present, bundled wins (production path)."""
    # No env override.
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)

    sha = "aaaabbbbccccdddd0000111122223333"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"bundled-body")
    _install_fake_compiled(monkeypatch, bundle_dir)
    _cache_dir_for(sha, monkeypatch, tmp_path)

    # Lay down a staged binary too — must NOT be picked.
    staged_root = tmp_path / "repo"
    staged_dir = staged_root / "apps" / "cli" / "dist" / "opencode"
    staged_dir.mkdir(parents=True)
    _make_executable(staged_dir / _opencode_filename())

    resolved = resolve_opencode_binary(repo_root=staged_root)
    assert resolved.source == "bundled"
    assert resolved.sha == sha


# ---------------------------------------------------------------------------
# Bundled ripgrep resolution — the harness must ship its own rg so the runtime
# never downloads one from github.com (per-platform, see ripgrep.ts PLATFORM).
# ---------------------------------------------------------------------------


def test_ripgrep_filename_is_platform_aware(monkeypatch) -> None:
    """`rg` keeps its real name (only the harness binary is renamed); `.exe`
    only on Windows. opencode's `which("rg")` needs the exact name on PATH."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert _ripgrep_filename() == "rg.exe"
    monkeypatch.setattr(sys, "platform", "linux")
    assert _ripgrep_filename() == "rg"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _ripgrep_filename() == "rg"


def _staged_layout(tmp_path: Path, *, with_rg: bool) -> Path:
    """Build a fake repo root with a staged harness binary (+ optionally a
    sibling staged rg). Returns the repo root."""
    fake_root = tmp_path / "alkera_repo"
    staged_dir = fake_root / "apps" / "cli" / "dist" / "opencode"
    staged_dir.mkdir(parents=True)
    _make_executable(staged_dir / _opencode_filename())
    (staged_dir / ".alkera-sha").write_text("abc1234\n")
    if with_rg:
        _make_executable(staged_dir / _ripgrep_filename())
        (staged_dir / ".alkera-rg-version").write_text("15.1.0\n")
    return fake_root


def test_staged_resolution_includes_sibling_ripgrep(tmp_path: Path, monkeypatch) -> None:
    """Staged mode resolves the staged `rg` sibling of the harness binary."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = _staged_layout(tmp_path, with_rg=True)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "staged"
    expected_rg = fake_root / "apps" / "cli" / "dist" / "opencode" / _ripgrep_filename()
    assert resolved.ripgrep_path == expected_rg.absolute()


def test_staged_resolution_ripgrep_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    """No staged rg → ripgrep_path is None (adapter keeps the download
    fallback rather than breaking glob/grep)."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = _staged_layout(tmp_path, with_rg=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "staged"
    assert resolved.ripgrep_path is None


def test_env_ripgrep_override_wins_over_staged(tmp_path: Path, monkeypatch) -> None:
    """`ALKERA_RIPGREP_BIN` beats the staged rg in every mode."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    (tmp_path / "custom").mkdir()
    override_rg = _make_executable(tmp_path / "custom" / _ripgrep_filename())
    monkeypatch.setenv("ALKERA_RIPGREP_BIN", str(override_rg))
    fake_root = _staged_layout(tmp_path, with_rg=True)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    assert resolved.ripgrep_path == override_rg.absolute()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod-based executability is POSIX; on Windows os.access(X_OK) is just existence",
)
def test_env_ripgrep_override_ignored_when_not_executable(
    tmp_path: Path, monkeypatch, require_effective_chmod: None
) -> None:
    """A non-executable ALKERA_RIPGREP_BIN is ignored (falls back to staged)."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    plain = tmp_path / "not-exec-rg"
    plain.write_text("nope")
    monkeypatch.setenv("ALKERA_RIPGREP_BIN", str(plain))
    fake_root = _staged_layout(tmp_path, with_rg=True)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    resolved = resolve_opencode_binary()
    staged_rg = fake_root / "apps" / "cli" / "dist" / "opencode" / _ripgrep_filename()
    assert resolved.ripgrep_path == staged_rg.absolute()


def test_bun_dev_uses_staged_ripgrep_when_present(tmp_path: Path, monkeypatch) -> None:
    """Bun-dev mode (no staged harness binary) still picks up a staged rg if a
    prior `make opencode-binary` produced one — so devs get the bundled rg too."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._resolve_bundled_binary", lambda: None)
    fake_root = tmp_path / "alkera_repo"
    oc_src_dir = fake_root / "vendor" / "opencode" / "packages" / "opencode" / "src"
    oc_src_dir.mkdir(parents=True)
    (oc_src_dir / "index.ts").write_text("// opencode entry\n")
    (fake_root / "vendor" / "opencode" / "node_modules").mkdir()  # deps installed
    # Stage ONLY rg (no harness binary) so resolution falls to bun-dev.
    staged_dir = fake_root / "apps" / "cli" / "dist" / "opencode"
    staged_dir.mkdir(parents=True)
    _make_executable(staged_dir / _ripgrep_filename())
    (staged_dir / ".alkera-rg-version").write_text("15.1.0\n")
    monkeypatch.setattr("alkera_cli.harness.opencode_binary._infer_repo_root", lambda: fake_root)
    fake_bun_dir = tmp_path / "bin"
    fake_bun_dir.mkdir()
    fake_bun = _make_executable(fake_bun_dir / "bun")
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bun) if name == "bun" else None)

    resolved = resolve_opencode_binary()
    assert resolved.source == "bun-dev"
    assert resolved.ripgrep_path == (staged_dir / _ripgrep_filename()).absolute()


def test_bundled_resolution_extracts_ripgrep_to_cache(tmp_path: Path, monkeypatch) -> None:
    """A bundle carrying `opencode/rg` extracts it into the SAME
    runtime-<sha>/ cache dir as the harness binary, byte-for-byte + executable."""
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    sha = "cafe0000cafe1111cafe2222cafe3333"
    rg_payload = b"#!/bin/sh\necho fake-rg\n"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"oc-body", rg_body=rg_payload)
    _install_fake_compiled(monkeypatch, bundle_dir)
    cache_dir = _cache_dir_for(sha, monkeypatch, tmp_path)

    resolved = _resolve_bundled_binary()
    assert resolved is not None
    cache_rg = cache_dir / _ripgrep_filename()
    assert resolved.ripgrep_path == cache_rg.absolute()
    assert cache_rg.read_bytes() == rg_payload
    assert os.access(cache_rg, os.X_OK)


def test_bundled_resolution_ripgrep_none_without_bundled_rg(tmp_path: Path, monkeypatch) -> None:
    """An older bundle WITHOUT rg → ripgrep_path None (graceful: the adapter
    leaves opencode's download fallback enabled instead of crashing)."""
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    sha = "0bad0bad0bad0bad0bad0bad0bad0bad"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"oc-only")
    _install_fake_compiled(monkeypatch, bundle_dir)
    _cache_dir_for(sha, monkeypatch, tmp_path)

    resolved = _resolve_bundled_binary()
    assert resolved is not None
    assert resolved.ripgrep_path is None


def test_env_ripgrep_override_wins_over_bundled(tmp_path: Path, monkeypatch) -> None:
    """Even in bundled mode an explicit ALKERA_RIPGREP_BIN takes precedence."""
    monkeypatch.delenv("ALKERA_OPENCODE_BIN", raising=False)
    sha = "abcd0000abcd1111abcd2222abcd3333"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"oc-body", rg_body=b"bundled-rg")
    _install_fake_compiled(monkeypatch, bundle_dir)
    _cache_dir_for(sha, monkeypatch, tmp_path)
    (tmp_path / "custom").mkdir()
    override_rg = _make_executable(tmp_path / "custom" / _ripgrep_filename())
    monkeypatch.setenv("ALKERA_RIPGREP_BIN", str(override_rg))

    resolved = resolve_opencode_binary()
    assert resolved.source == "bundled"
    assert resolved.ripgrep_path == override_rg.absolute()


# ---------------------------------------------------------------------------
# Mark-on-use + periodic cache cleanup.
# ---------------------------------------------------------------------------


def test_bundled_resolver_touches_last_used_on_extract_and_hit(tmp_path: Path, monkeypatch) -> None:
    """Resolving a bundled binary writes a `.last-used` marker on the fresh
    extract, and bumps its mtime again on a subsequent cache-hit resolution —
    so an in-service version keeps refreshing its recency."""
    sha = "feed0000feed1111feed2222feed3333"
    bundle_dir = _fake_compiled_bundle(tmp_path, sha=sha, body=b"body")
    _install_fake_compiled(monkeypatch, bundle_dir)
    cache_dir = _cache_dir_for(sha, monkeypatch, tmp_path)

    first = _resolve_bundled_binary()
    assert first is not None
    marker = cache_dir / _LAST_USED_MARKER
    assert marker.is_file()

    # Back-date the marker, resolve again (cache-hit path), assert it advanced.
    past = time.time() - 10_000
    os.utime(marker, (past, past))
    old_mtime = marker.stat().st_mtime
    second = _resolve_bundled_binary()
    assert second is not None
    assert marker.stat().st_mtime > old_mtime


def _make_cache_dir(root: Path, sha: str, *, age_seconds: float, now: float, marker: bool) -> Path:
    """Create `root/opencode-<sha>/` aged `age_seconds` before `now`, dating
    either its `.last-used` marker or (when ``marker=False``) the dir itself."""
    d = root / f"runtime-{sha}"
    d.mkdir(parents=True)
    (d / _opencode_filename()).write_text("x")
    ts = now - age_seconds
    if marker:
        m = d / _LAST_USED_MARKER
        m.touch()
        os.utime(m, (ts, ts))
    else:
        os.utime(d, (ts, ts))
    return d


def test_cleanup_removes_stale_keeps_fresh_and_active(
    tmp_path: Path, monkeypatch, require_effective_utime: None
) -> None:
    alkera_home = tmp_path / "alkera-home"
    root = alkera_home / "cache"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", alkera_home)

    # Large enough that even `max_age * 5` back-dating stays post-epoch —
    # DrvFs (WSL /mnt/c) silently refuses pre-1970 timestamps, which made the
    # "stale" entries stat as fresh there.
    now = 5_000_000_000.0
    max_age = 14 * 24 * 3600
    fresh = _make_cache_dir(root, "fresh", age_seconds=3600, now=now, marker=True)
    stale = _make_cache_dir(root, "stale", age_seconds=max_age + 3600, now=now, marker=True)
    # No marker → falls back to the dir's own (back-dated) mtime.
    nomark = _make_cache_dir(root, "nomark", age_seconds=max_age + 7200, now=now, marker=False)
    # Older than everything, but the protected active SHA → never removed.
    active = _make_cache_dir(root, "active", age_seconds=max_age * 5, now=now, marker=True)

    res = cleanup_stale_opencode_caches(max_age_seconds=max_age, keep_sha="active", now=now)

    assert set(res.removed) == {"stale", "nomark"}
    assert set(res.kept) == {"fresh", "active"}
    assert res.errors == ()
    assert fresh.exists() and active.exists()
    assert not stale.exists() and not nomark.exists()


def test_cleanup_skips_non_opencode_entries_and_records_errors(tmp_path: Path, monkeypatch) -> None:
    alkera_home = tmp_path / "alkera-home"
    root = alkera_home / "cache"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", alkera_home)

    now = 1_000_000.0
    # A non-prefixed dir and a stray file with the prefix must both be ignored.
    (root / "something-else").mkdir()
    (root / "runtime-loose.txt").write_text("not a dir")
    stale = _make_cache_dir(root, "stale", age_seconds=10_000, now=now, marker=False)

    def _raise(*_a: object, **_k: object) -> None:
        raise OSError("locked")

    monkeypatch.setattr("shutil.rmtree", _raise)

    res = cleanup_stale_opencode_caches(max_age_seconds=100.0, now=now)

    assert res.removed == ()
    assert any("stale" in e for e in res.errors)
    assert stale.exists()  # locked binary survives the sweep


def test_cleanup_noop_when_cache_root_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "ALKERA_HOME", tmp_path / "does-not-exist")
    res = cleanup_stale_opencode_caches(max_age_seconds=1.0, now=2.0)
    assert res.removed == () and res.kept == () and res.errors == ()


# --- _extract_to_cache: replace-while-running (the in-place update case) -----


def test_extract_replaces_a_locked_destination_by_renaming_it_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses os.replace onto an EXECUTING image (WinError 5) — the
    field failure where an extension update re-staged the agent into the same
    vendor-sha dir while the old install's agent still ran from it, killing
    every chat with 'Access is denied'. The staging must rename the live exe
    aside (renaming a running image IS allowed) and install the new one."""
    from alkera_cli.harness.opencode_binary import _extract_to_cache

    src = tmp_path / "bundle-agent"
    src.write_text("NEW BYTES")
    dst = tmp_path / "cache" / "alkera-agent"
    dst.parent.mkdir()
    dst.write_text("OLD RUNNING BYTES")

    real_replace = os.replace
    state = {"denied": False}

    def locked_once(a: object, b: object) -> None:
        # Simulate the Windows image lock: the FIRST replace onto dst fails;
        # the rename-aside of dst and the retry both succeed.
        if Path(str(b)) == dst and not state["denied"]:
            state["denied"] = True
            raise PermissionError(13, "Access is denied")
        real_replace(a, b)

    monkeypatch.setattr(os, "replace", locked_once)
    _extract_to_cache(src, dst)

    assert dst.read_text() == "NEW BYTES", "the new binary must be installed"
    aside = [p for p in dst.parent.iterdir() if p.name.startswith(".old-")]
    assert len(aside) == 1, "the running image was renamed aside, not clobbered"
    assert aside[0].read_text() == "OLD RUNNING BYTES", "the live process keeps its file"


def test_extract_sweeps_stale_old_leftovers(tmp_path: Path) -> None:
    """A later staging reclaims the .old-* files a replace-while-running
    rename left behind (best-effort; a still-locked one would just stay)."""
    from alkera_cli.harness.opencode_binary import _extract_to_cache

    src = tmp_path / "bundle-agent"
    src.write_text("V3")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".old-alkera-agent.111.222").write_text("V1")
    (cache / ".old-alkera-agent.333.444").write_text("V2")

    _extract_to_cache(src, cache / "alkera-agent")

    assert (cache / "alkera-agent").read_text() == "V3"
    assert not list(cache.glob(".old-*")), "prior leftovers are swept"


def test_extract_failure_still_cleans_its_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging that fails outright (even the rename-aside path denied) must
    not leave a .agent.*.tmp litter behind."""
    from alkera_cli.harness.opencode_binary import _extract_to_cache

    src = tmp_path / "bundle-agent"
    src.write_text("NEW")
    dst = tmp_path / "cache" / "alkera-agent"
    dst.parent.mkdir()
    dst.write_text("OLD")

    def always_denied(a: object, b: object) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", always_denied)
    with pytest.raises(PermissionError):
        _extract_to_cache(src, dst)

    assert not list(dst.parent.glob(".agent.*.tmp")), "no temp litter on failure"
    assert dst.read_text() == "OLD", "the existing binary is untouched"


# --- real Windows image-lock semantics (no monkeypatch) -----------------------


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows image-lock semantics")
def test_extract_replaces_a_genuinely_running_agent_exe_win32(
    tmp_path: Path, spawn_pinned_exe
) -> None:
    """The monkeypatch tests above SIMULATE the image lock; this proves the
    staging against the real thing: a live process running the destination exe
    raises the real WinError 5, the live image is renamed aside, and the new
    binary installs — all while the process keeps running."""
    from alkera_cli.harness.opencode_binary import _extract_to_cache

    dst = tmp_path / "cache" / "alkera-agent.exe"
    dst.parent.mkdir()
    proc = spawn_pinned_exe(dst)
    src = tmp_path / "bundle-agent"
    src.write_text("NEW BYTES")

    _extract_to_cache(src, dst)

    assert dst.read_text() == "NEW BYTES", "the new binary must be installed"
    aside = [p for p in dst.parent.iterdir() if p.name.startswith(".old-")]
    assert len(aside) == 1, "the running image was renamed aside, not clobbered"
    assert proc.poll() is None, "the running agent must survive the staging"


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows image-lock semantics")
def test_extract_sweep_tolerates_a_still_running_old_leftover_win32(
    tmp_path: Path, spawn_pinned_exe
) -> None:
    """A ``.old-*`` leftover whose process is STILL alive can't be unlinked —
    the sweep must skip it without failing the staging, and reclaim it once
    the process exits."""
    from alkera_cli.harness.opencode_binary import _extract_to_cache

    cache = tmp_path / "cache"
    cache.mkdir()
    pinned = cache / ".old-alkera-agent.111.222"
    proc = spawn_pinned_exe(pinned)
    src = tmp_path / "bundle-agent"
    src.write_text("V2")

    _extract_to_cache(src, cache / "alkera-agent.exe")

    assert (cache / "alkera-agent.exe").read_text() == "V2"
    assert pinned.exists(), "a live leftover survives the sweep, no error"

    proc.kill()
    proc.wait(timeout=30)
    _extract_to_cache(tmp_path / "bundle-agent", cache / "alkera-agent.exe")
    assert not pinned.exists(), "the leftover is reclaimed once its process exits"
