"""Helpers for the environment tests: local wheels, a recording runner, a tree."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from alkera_notebook.envs import CommandResult, LocalCommandRunner

UV = shutil.which("uv")
PY = sys.executable


def build_wheel(directory: Path, name: str, version: str) -> Path:
    """A minimal pure-Python wheel, written by hand."""
    files = {
        f"{name}/__init__.py": f'VERSION = "{version}"\n'.encode(),
        f"{name}-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
        ),
        f"{name}-{version}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: alkera-test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(data)}")
    record_path = f"{name}-{version}.dist-info/RECORD"
    record.append(f"{record_path},,")
    files[record_path] = ("\n".join(record) + "\n").encode()
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(out, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return out


def uv_env(cache: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")}
    env.update(
        {
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_CONFIG": "1",
            "UV_CACHE_DIR": str(cache),
        }
    )
    return env


def lock_project(project: Path, wheels: Path, cache: Path) -> None:
    assert UV is not None
    subprocess.run(
        [
            UV,
            "lock",
            "--project",
            str(project),
            "--python",
            PY,
            "--no-index",
            "--find-links",
            str(wheels),
        ],
        check=True,
        env=uv_env(cache),
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


DEFAULT_PYPROJECT = """[project]
name = "alkera-default-env"
version = "0"
requires-python = ">=3.11"
dependencies = ["tinypkg"]
"""


class RecordingRunner:
    """Runs commands for real and remembers them."""

    def __init__(self) -> None:
        self.inner = LocalCommandRunner()
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        return await self.inner.run(argv, cwd=cwd, env=env, timeout_s=timeout_s)

    def subcommands(self) -> list[str]:
        return [" ".join(c[1:3]) if c[1] == "pip" else c[1] for c in self.calls]
