"""Real Python environments for the environment-cloning tests, with no network.

Packages come from wheels built here (a wheel is a zip with three metadata
files), served from a ``--find-links`` folder with every index switched off; an
editable package is built by a backend that lives inside the package's own
folder (``backend-path``), so neither uv nor pip fetches a build backend. The
network is the one boundary these tests fake.
"""

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

UV = shutil.which("uv")
MICROMAMBA = shutil.which("micromamba")
WINDOWS = os.name == "nt"


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


WHEEL_FILE = b"Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def build_wheel(
    out: Path, name: str, version: str, *, deps: Sequence[str] = (), module: str | None = None
) -> tuple[Path, str]:
    """A pure-Python wheel for ``name``; returns its path and ``sha256:<hex>``."""
    out.mkdir(parents=True, exist_ok=True)
    mod = module or name.replace("-", "_")
    dist = f"{name.replace('-', '_')}-{version}.dist-info"
    meta = "\n".join(
        ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        + [f"Requires-Dist: {d}" for d in deps]
    )
    files = {
        f"{mod}/__init__.py": f"VERSION = {version!r}\n".encode(),
        f"{dist}/METADATA": (meta + "\n").encode(),
        f"{dist}/WHEEL": WHEEL_FILE,
    }
    path = out / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    records = [f"{arc},{_record_hash(data)},{len(data)}" for arc, data in files.items()]
    records.append(f"{dist}/RECORD,,")
    with zipfile.ZipFile(path, "w") as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
        zf.writestr(f"{dist}/RECORD", "\n".join(records) + "\n")
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_BACKEND = """
import base64, hashlib, os, zipfile

NAME = {name!r}
VERSION = {version!r}
DEPS = {deps!r}
MOD = NAME.replace("-", "_")
HERE = os.path.dirname(os.path.abspath(__file__))
DIST = MOD + "-" + VERSION + ".dist-info"


def _hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _write(directory, files):
    files[DIST + "/METADATA"] = (
        "Metadata-Version: 2.1\\nName: " + NAME + "\\nVersion: " + VERSION + "\\n"
        + "".join("Requires-Dist: " + d + "\\n" for d in DEPS)
    ).encode()
    files[DIST + "/WHEEL"] = (
        b"Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    )
    name = MOD + "-" + VERSION + "-py3-none-any.whl"
    records = [arc + "," + _hash(data) + "," + str(len(data)) for arc, data in files.items()]
    records.append(DIST + "/RECORD,,")
    with zipfile.ZipFile(os.path.join(directory, name), "w") as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
        zf.writestr(DIST + "/RECORD", "\\n".join(records) + "\\n")
    return name


def get_requires_for_build_wheel(config_settings=None):
    return []


get_requires_for_build_editable = get_requires_for_build_wheel


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    src = os.path.join(HERE, "src", MOD)
    files = {{}}
    for entry in os.listdir(src):
        with open(os.path.join(src, entry), "rb") as handle:
            files[MOD + "/" + entry] = handle.read()
    return _write(wheel_directory, files)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    pth = (os.path.join(HERE, "src") + "\\n").encode()
    return _write(wheel_directory, {{"_" + MOD + ".pth": pth}})
"""


def editable_project(
    folder: Path, name: str, version: str = "0.1.0", deps: Sequence[str] = ()
) -> Path:
    """A package folder whose own backend builds it, wheel or editable, offline."""
    mod = name.replace("-", "_")
    (folder / "src" / mod).mkdir(parents=True, exist_ok=True)
    (folder / "src" / mod / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (folder / "_backend.py").write_text(
        _BACKEND.format(name=name, version=version, deps=list(deps)), encoding="utf-8"
    )
    dep_list = ", ".join(f'"{d}"' for d in deps)
    (folder / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "_backend"\nbackend-path = ["."]\n\n'
        f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{dep_list}]\n',
        encoding="utf-8",
    )
    return folder


def offline_env(tmp: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every command in these tests runs with: no index, no
    downloads, a private uv cache."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("UV_", "PIP_", "VIRTUAL_ENV", "CONDA"))
    }
    env.update(
        {
            "UV_OFFLINE": "1",
            "UV_NO_INDEX": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": str(tmp / "uv-cache"),
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    env.update(extra or {})
    return env


def bin_python(env: Path) -> Path:
    return env / ("Scripts/python.exe" if WINDOWS else "bin/python")


def make_venv(env: Path, base: Mapping[str, str]) -> Path:
    assert UV is not None
    env.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [UV, "venv", "--quiet", "--python", sys.executable, str(env)],
        check=True,
        env=dict(base),
        cwd=env.parent,
    )
    return bin_python(env)


def uv_install(python: Path, base: Mapping[str, str], *args: str) -> None:
    # Run outside any project, so no uv settings but the test's apply.
    assert UV is not None
    subprocess.run(
        [UV, "pip", "install", "--quiet", "--python", str(python), *args],
        check=True,
        env=dict(base),
        cwd=python.parent,
    )


def run_py(python: Path, code: str, base: Mapping[str, str]) -> str:
    out = subprocess.run(
        [str(python), "-c", code], check=True, env=dict(base), capture_output=True, text=True
    )
    return out.stdout.strip()
