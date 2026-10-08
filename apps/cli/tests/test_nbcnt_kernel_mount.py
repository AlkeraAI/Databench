"""A box always has the kernel's platform files: the CLI distribution
installs ``alkera-kernel`` (a box's venv is ``uv sync --package alkera-cli``),
and the compiled build bundles them as plain files it reads back."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import pytest
from alkera_cli.notebooks import kernel_mount
from alkera_cli.notebooks.engine_host import stage_platform_mount
from alkera_cli.notebooks.kernel_mount import BUNDLED_SUBDIR, bundle, kernel_sources

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-daemon-binary.sh"


def _requirement_names(dist: str) -> set[str]:
    names = set()
    for requirement in importlib.metadata.requires(dist) or []:
        if "extra ==" in requirement:
            continue
        match = re.match(r"[A-Za-z0-9_.\-]+", requirement)
        assert match is not None
        names.add(match.group(0).lower().replace("_", "-"))
    return names


@pytest.mark.parametrize("needed", ["alkera-kernel", "alkera"])
def test_the_cli_distribution_installs_what_a_kernel_sandbox_mounts(needed: str) -> None:
    """A box's venv holds the CLI's own dependencies and nothing else: a
    kernel package the CLI does not declare is missing on every box."""
    assert needed in _requirement_names("alkera-cli")


def test_from_source_the_platform_files_are_the_installed_packages() -> None:
    sources = kernel_sources(bundle_dir=None)
    assert sources.boot.is_file() and sources.boot.name == "boot.py"
    assert (sources.package / "__init__.py").is_file()
    assert sources.public is not None and (sources.public / "__init__.py").is_file()


def test_a_compiled_build_reads_the_tree_it_bundled(tmp_path: Path) -> None:
    build = tmp_path / "alkera.dist"
    bundle(build.joinpath(*BUNDLED_SUBDIR))
    sources = kernel_sources(bundle_dir=build)
    tree = build.joinpath(*BUNDLED_SUBDIR)
    assert (sources.boot, sources.package) == (tree / "boot.py", tree / "_alkera_kernel")
    assert sources.public == tree / "public" / "alkera"
    # What the box stages from it is a whole kernel mount.
    staged = stage_platform_mount(sources.boot, sources.package, sources.public, tmp_path / "mount")
    assert (staged / "boot.py").is_file()
    assert (staged / "_alkera_kernel" / "runtime.py").is_file()
    assert (staged / "public" / "alkera" / "__init__.py").is_file()
    assert not list(staged.rglob("__pycache__"))


def test_a_compiled_build_without_the_tree_falls_back_to_installed_sources(
    tmp_path: Path,
) -> None:
    sources = kernel_sources(bundle_dir=tmp_path / "empty-build")
    assert sources == kernel_sources(bundle_dir=None)


def test_a_compiled_build_refuses_to_bundle_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kernel_mount, "_bundle_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError):
        bundle(tmp_path / "again")


def _code_lines() -> list[str]:
    return [
        line.strip()
        for line in BUILD_SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]


def test_the_binary_build_stages_and_bundles_the_kernel_tree_where_the_box_reads_it() -> None:
    lines = _code_lines()
    stage = next(i for i, line in enumerate(lines) if "alkera_cli.notebooks.kernel_mount" in line)
    start = next(i for i, line in enumerate(lines) if line.startswith("NUITKA_ARGS=("))
    assert stage < start, "the tree is staged before the compile reads it"
    target = "/".join(BUNDLED_SUBDIR)
    flags = [line for line in lines if line.startswith("--include-raw-dir=") and target in line]
    assert flags == [f'--include-raw-dir="$REPO_ROOT/$KERNEL_MOUNT_STAGE_DIR={target}"']
