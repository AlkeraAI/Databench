"""Where a box finds the kernel's platform files: ``boot.py``, the
``_alkera_kernel`` package beside it, and the public ``alkera`` package a
notebook imports when its environment does not install it.

From a source checkout or an installed ``alkera-kernel`` they are the
installed sources. A compiled build cannot hand them over that way (its own
modules are compiled, and these run in another interpreter), so the build
bundles them as plain files under ``runtime/kernel-mount`` (``python -m
alkera_cli.notebooks.kernel_mount <dir>`` lays that tree out) and a box reads
them from there.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Final, NamedTuple

#: The bundled tree, under the compiled build's own directory.
BUNDLED_SUBDIR: Final = ("runtime", "kernel-mount")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


class KernelSources(NamedTuple):
    """The ``boot.py`` a mount runs, the ``_alkera_kernel`` package it loads,
    and the public ``alkera`` package directory (``None`` when there is none)."""

    boot: Path
    package: Path
    public: Path | None


def _bundle_dir() -> Path | None:
    """The compiled build's directory, or ``None`` when running from source
    (``__compiled__`` on ``__main__`` is the build's own sentinel)."""
    if not getattr(sys.modules.get("__main__"), "__compiled__", None):
        return None
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def kernel_sources(bundle_dir: Path | None = None) -> KernelSources:
    """The kernel's platform files on this box: the build's bundled tree when
    it carries one, else the installed sources. Raises ``FileNotFoundError``
    when neither is there (the box cannot run a kernel)."""
    base = bundle_dir if bundle_dir is not None else _bundle_dir()
    if base is not None:
        root = base.joinpath(*BUNDLED_SUBDIR)
        if (root / "boot.py").is_file() and (root / "_alkera_kernel").is_dir():
            public = root / "public" / "alkera"
            return KernelSources(
                root / "boot.py", root / "_alkera_kernel", public if public.is_dir() else None
            )
    from alkera_notebook.kernels.launch_local import kernel_files

    files = kernel_files()
    spec = importlib.util.find_spec("alkera")
    public_dir = Path(spec.origin).resolve().parent if spec and spec.origin else None
    return KernelSources(files.boot, files.package, public_dir)


def bundle(target: Path) -> Path:
    """Lay the installed platform files out at ``target`` as the build
    bundles them (replacing what was there). For the build script."""
    if _bundle_dir() is not None:
        raise RuntimeError("a compiled build does not bundle the kernel's files again")
    sources = kernel_sources()
    if sources.public is None:
        raise FileNotFoundError("the public alkera package is not installed")
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    shutil.copy2(sources.boot, target / "boot.py")
    shutil.copytree(sources.package, target / "_alkera_kernel", ignore=_IGNORE)
    shutil.copytree(sources.public, target / "public" / "alkera", ignore=_IGNORE)
    return target


if __name__ == "__main__":  # pragma: no cover - the build script's entry
    if len(sys.argv) != 2:
        sys.exit("usage: python -m alkera_cli.notebooks.kernel_mount <target dir>")
    print(bundle(Path(sys.argv[1])))


__all__ = ["BUNDLED_SUBDIR", "KernelSources", "bundle", "kernel_sources"]
