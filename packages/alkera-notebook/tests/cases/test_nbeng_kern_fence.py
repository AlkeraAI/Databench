"""A local kernel whose folder is fenced writes nothing until it is let go,
and no new kernel starts meanwhile (the box's freeze where no cgroup is)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from alkera_notebook.kernels.launch_local import (
    KernelsFencedError,
    LaunchSpec,
    LocalSubprocessLauncher,
)

needs_posix_signals = pytest.mark.skipif(
    sys.platform == "win32", reason="stops a process group with SIGSTOP"
)

#: A kernel that writes a tick to its folder every 20 ms.
TICKER = (
    "import os, time\n"
    "path = os.path.join(os.environ['ALKERA_NOTEBOOK_DIR'], 'ticks')\n"
    "while True:\n"
    "    with open(path, 'a') as out:\n"
    "        out.write('.')\n"
    "    time.sleep(0.02)\n"
)


def _spec(tmp_path: Path, kernel_id: str) -> LaunchSpec:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    (mount / "boot.py").write_text(TICKER, encoding="utf-8")
    return LaunchSpec(
        interpreter=sys.executable,
        notebook_dir=tmp_path,
        kernel_id=kernel_id,
        endpoint="unix:/nonexistent",
        token="t",
        mount=mount,
        log_path=tmp_path / f"{kernel_id}.log",
    )


def _ticks(tmp_path: Path) -> int:
    path = tmp_path / "ticks"
    return len(path.read_text(encoding="utf-8")) if path.exists() else 0


def _until_ticking(tmp_path: Path) -> None:
    deadline = time.monotonic() + 10
    while _ticks(tmp_path) < 3:
        assert time.monotonic() < deadline, "the kernel never started writing"
        time.sleep(0.02)


@needs_posix_signals
def test_a_fenced_kernel_writes_nothing_and_goes_on_once_let_go(tmp_path: Path) -> None:
    launcher = LocalSubprocessLauncher()
    launcher.launch(_spec(tmp_path, "krn_a"))
    try:
        _until_ticking(tmp_path)
        launcher.fence()
        time.sleep(0.2)  # what was in flight lands
        stopped = _ticks(tmp_path)
        time.sleep(0.5)
        assert _ticks(tmp_path) == stopped
        with pytest.raises(KernelsFencedError):
            launcher.launch(_spec(tmp_path, "krn_b"))
        launcher.unfence()
        deadline = time.monotonic() + 10
        while _ticks(tmp_path) == stopped:
            assert time.monotonic() < deadline, "the kernel never went on"
            time.sleep(0.02)
    finally:
        launcher.unfence()
        launcher.kill_all()
