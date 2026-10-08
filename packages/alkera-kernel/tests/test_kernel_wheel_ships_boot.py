"""An installed ``alkera-kernel`` carries ``boot.py``: a box installs the
distribution, not this checkout, and a kernel cannot start without the file.

The wheel is built from this tree and installed into a directory of its own;
the kernel's files are then resolved from that install, by the same function
a box calls, in a fresh interpreter where the install comes first."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("kernel_wheel_ships_boot")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="builds the wheel with uv")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(PACKAGE_ROOT)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    [built] = out.glob("alkera_kernel-*.whl")
    return built


@needs_uv
def test_the_wheel_carries_boot_py_inside_the_package(wheel: Path) -> None:
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "_alkera_kernel/boot.py" in names
    # Nothing lands at the top of site-packages but the package itself.
    assert not {name for name in names if "/" not in name}


@needs_uv
def test_an_installed_wheel_resolves_the_kernel_files_a_box_mounts(
    wheel: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    subprocess.run(
        ["uv", "pip", "install", "--no-deps", "--target", str(site), str(wheel)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        "from alkera_notebook.kernels.launch_local import kernel_files\n"
        "files = kernel_files()\n"
        "print(files.boot)\n"
        "print(files.package)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    boot, package = (Path(line) for line in done.stdout.split())
    assert package == (site / "_alkera_kernel").resolve()
    assert boot == package / "boot.py" and boot.is_file()
