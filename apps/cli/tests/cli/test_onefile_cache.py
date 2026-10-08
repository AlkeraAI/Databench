"""Stale onefile extraction-cache pruning (`alkera_cli.host.onefile_cache`).

The compiled binary extracts to `{CACHE_DIR}/{PRODUCT}/{VERSION}` and the
daemon sweeps sibling version dirs at startup. The invariants pinned here:
only version-shaped siblings carrying the alkera data marker are removed,
the running version's own dir is never touched, an undeletable sibling is
skipped without aborting the sweep, and the whole sweep is a no-op when the
extraction dir doesn't look like a cached-mode version dir (a temp-spec
build's parent is the shared temp dir — deleting siblings there would eat
unrelated files).
"""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import pytest
from alkera_cli.host import onefile_cache
from alkera_cli.host.onefile_cache import (
    current_extract_dir,
    prune_stale_onefile_caches,
    prune_stale_onefile_caches_in_background,
    prune_stale_version_dirs,
)


def make_extraction(parent: Path, name: str, *, marker: bool = True) -> Path:
    """A fake extracted onefile dist: `<parent>/<name>` with the bundled-data
    marker (`runtime/.alkera-sha`) that proves it's an alkera extraction."""
    dist = parent / name
    (dist / "runtime").mkdir(parents=True)
    if marker:
        (dist / "runtime" / ".alkera-sha").write_text("deadbeef\n")
    return dist


class TestPruneStaleVersionDirs:
    def test_removes_stale_version_sibling(self, tmp_path: Path) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        stale = make_extraction(tmp_path, "0.0.1.0")

        removed = prune_stale_version_dirs(ours)

        assert removed == [stale]
        assert not stale.exists()
        assert ours.is_dir()

    def test_own_dir_never_removed(self, tmp_path: Path) -> None:
        ours = make_extraction(tmp_path, "0.0.1.0")

        assert prune_stale_version_dirs(ours) == []
        assert ours.is_dir()
        assert (ours / "runtime" / ".alkera-sha").is_file()

    @pytest.mark.parametrize(
        "sibling_name",
        [
            pytest.param("not-a-version", id="non-version-name"),
            pytest.param("onefile_1234_5678", id="temp-spec-shaped-name"),
            pytest.param("v1.2.3.0", id="version-with-prefix"),
        ],
    )
    def test_non_version_siblings_kept_even_with_marker(
        self, tmp_path: Path, sibling_name: str
    ) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        other = make_extraction(tmp_path, sibling_name)

        assert prune_stale_version_dirs(ours) == []
        assert other.is_dir()

    def test_version_sibling_without_marker_kept(self, tmp_path: Path) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        foreign = make_extraction(tmp_path, "0.0.1.0", marker=False)

        assert prune_stale_version_dirs(ours) == []
        assert foreign.is_dir()

    def test_plain_files_kept(self, tmp_path: Path) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        stray = tmp_path / "0.0.1.0"
        stray.write_text("a file, not an extraction dir")

        assert prune_stale_version_dirs(ours) == []
        assert stray.is_file()

    @pytest.mark.parametrize(
        "extract_name",
        [
            pytest.param("onefile_1234_5678", id="temp-spec-dir"),
            pytest.param("alkera", id="plain-name"),
        ],
    )
    def test_noop_when_own_dir_not_version_shaped(self, tmp_path: Path, extract_name: str) -> None:
        # A build reverted to the per-run temp spec extracts into a dir whose
        # PARENT is the shared temp dir — the sweep must refuse to touch
        # anything there, even dirs that look like prunable extractions.
        ours = make_extraction(tmp_path, extract_name)
        innocent = make_extraction(tmp_path, "0.0.1.0")

        assert prune_stale_version_dirs(ours) == []
        assert innocent.is_dir()

    def test_undeletable_sibling_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ours = make_extraction(tmp_path, "0.0.3.0")
        locked = make_extraction(tmp_path, "0.0.1.0")
        stale = make_extraction(tmp_path, "0.0.2.0")
        real_rmtree = shutil.rmtree

        def rmtree(path: Path | str, *args: object, **kwargs: object) -> None:
            if Path(path).name == locked.name:
                raise OSError("file in use (running daemon holds a lock)")
            real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(shutil, "rmtree", rmtree)

        removed = prune_stale_version_dirs(ours)

        assert removed == [stale]
        assert locked.is_dir()
        assert not stale.exists()

    def test_unlistable_parent_returns_empty(self, tmp_path: Path) -> None:
        ours = tmp_path / "gone" / "0.0.1.0"  # parent doesn't exist

        assert prune_stale_version_dirs(ours) == []


class TestCompiledSentinel:
    def test_uncompiled_returns_none_and_prunes_nothing(self) -> None:
        # pytest's __main__ has no __compiled__ — the real "running from
        # source" case needs no monkeypatching.
        assert current_extract_dir() is None
        assert prune_stale_onefile_caches() == []

    def _fake_compiled(self, monkeypatch: pytest.MonkeyPatch, module_file: Path) -> None:
        monkeypatch.setattr(sys.modules["__main__"], "__compiled__", object(), raising=False)
        monkeypatch.setattr(onefile_cache, "__file__", str(module_file))

    def test_compiled_resolves_extract_dir_from_module_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        self._fake_compiled(monkeypatch, ours / "alkera_cli" / "host" / "onefile_cache.py")

        assert current_extract_dir() == ours

    def test_compiled_end_to_end_prunes_stale_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ours = make_extraction(tmp_path, "0.0.2.0")
        stale = make_extraction(tmp_path, "0.0.1.0")
        self._fake_compiled(monkeypatch, ours / "alkera_cli" / "host" / "onefile_cache.py")

        assert prune_stale_onefile_caches() == [stale]
        assert not stale.exists()
        assert ours.is_dir()

    def test_prune_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Path | None:
            raise RuntimeError("anything")

        monkeypatch.setattr(onefile_cache, "current_extract_dir", boom)

        assert prune_stale_onefile_caches() == []


def test_background_wrapper_runs_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = threading.Event()
    monkeypatch.setattr(onefile_cache, "prune_stale_onefile_caches", lambda: ran.set())

    prune_stale_onefile_caches_in_background()

    assert ran.wait(timeout=5.0), "background prune thread never ran"
