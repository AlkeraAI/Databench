"""Read one Python environment and one workspace, and print what was found as JSON.

This file is a script, not a module of the package: it is handed to a FOREIGN
interpreter (the environment being captured, run as the chat's own user inside
its sandbox, or the user's own venv on a laptop) and must run on any CPython or
PyPy from 3.8 up with nothing but the standard library. It reads files and
prints; it installs, writes and imports nothing from the environment beyond
what the interpreter itself loads at start.

Usage::

    python -I env_probe.py --root DIR [--no-env] [--path REL ...]

The JSON is printed on one line after :data:`MARKER`, so a launcher that mixes
stderr into stdout still finds it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import sysconfig
from importlib import metadata
from typing import Any

MARKER = "ALKERA-ENV-PROBE:"
PROBE_VERSION = 1

#: The top-level workspace files that describe an environment.
PROJECT_FILES = (
    "pyproject.toml",
    "uv.lock",
    "environment.yml",
    "environment.yaml",
    "pixi.toml",
    "pixi.lock",
    ".python-version",
    "runtime.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
)
#: Requirement files are matched by name in the root and one level under a
#: ``requirements/`` folder.
_REQUIREMENTS_RE = re.compile(r"^requirements[\w.-]*\.(txt|in)$")
#: Files whose text the capture parses are returned whole up to this size;
#: anything larger is returned as a digest only.
MAX_TEXT_BYTES = 8 * 1024 * 1024
#: Lockfiles nobody parses (pixi, poetry, Pipfile) are only digested.
_DIGEST_ONLY = frozenset({"pixi.lock", "poetry.lock", "Pipfile.lock"})
#: Settings that point pip or uv at an index. The capture redacts credentials.
INDEX_ENV_VARS = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_FIND_LINKS",
)
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file(path: str) -> dict[str, Any] | None:
    """A regular file's digest and (when small enough) text; ``None`` when the
    path is not a regular file."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            data = handle.read(MAX_TEXT_BYTES + 1)
    except OSError:
        return None
    entry: dict[str, Any] = {"size": size}
    if size > MAX_TEXT_BYTES:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        entry["sha256"] = digest.hexdigest()
        return entry
    entry["sha256"] = _sha256(data)
    if os.path.basename(path) not in _DIGEST_ONLY:
        entry["text"] = data.decode("utf-8", "replace")
    return entry


def project_files(root: str) -> dict[str, Any]:
    """Every environment file in the workspace root, keyed by its path relative
    to the root (``/``-separated)."""
    found: dict[str, Any] = {}
    names = list(PROJECT_FILES)
    try:
        listing = sorted(os.listdir(root))
    except OSError:
        listing = []
    names.extend(n for n in listing if _REQUIREMENTS_RE.match(n))
    req_dir = os.path.join(root, "requirements")
    if os.path.isdir(req_dir) and not os.path.islink(req_dir):
        try:
            names.extend(
                "requirements/" + n for n in sorted(os.listdir(req_dir)) if n.endswith(".txt")
            )
        except OSError:
            pass
    for rel in names:
        entry = _read_file(os.path.join(root, *rel.split("/")))
        if entry is not None:
            found[rel] = entry
    return found


def _site_dirs() -> list[str]:
    paths = sysconfig.get_paths()
    out: list[str] = []
    for key in ("purelib", "platlib"):
        value = paths.get(key)
        if value and value not in out and os.path.isdir(value):
            out.append(value)
    return out


def _requires(dist: metadata.Distribution) -> list[str]:
    names: list[str] = []
    for line in dist.requires or []:
        if "extra ==" in line or "extra==" in line:
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            names.append(match.group(1))
    return names


def _distribution(dist: metadata.Distribution, site: str) -> dict[str, Any] | None:
    name = dist.metadata["Name"]
    if not name:
        return None
    direct_url = None
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            direct_url = json.loads(raw)
        except ValueError:
            direct_url = None
    installer = (dist.read_text("INSTALLER") or "").strip()
    return {
        "name": name,
        "version": dist.version or "",
        "installer": installer,
        "requested": dist.read_text("REQUESTED") is not None,
        "requires": _requires(dist),
        "direct_url": direct_url,
        "site": site,
    }


def distributions(sites: list[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for site in sites:
        for dist in metadata.distributions(path=[site]):
            entry = _distribution(dist, site)
            if entry is None:
                continue
            key = entry["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def path_entries(sites: list[str]) -> list[dict[str, str]]:
    """Directories a ``.pth`` file adds to ``sys.path`` (``conda develop``, a
    hand-written ``.pth``, a legacy ``setup.py develop``), and legacy
    ``.egg-link`` editables."""
    out: list[dict[str, str]] = []
    for site in sites:
        try:
            names = sorted(os.listdir(site))
        except OSError:
            continue
        for name in names:
            full = os.path.join(site, name)
            if name.endswith(".egg-link"):
                target = _first_line(full)
                if target:
                    out.append({"kind": "egg-link", "file": name, "path": target})
                continue
            if not name.endswith(".pth"):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            for line in lines:
                text = line.strip()
                if not text or text.startswith(("#", "import ", "import\t")):
                    continue
                target = text if os.path.isabs(text) else os.path.join(site, text)
                if os.path.isdir(target):
                    out.append({"kind": "pth", "file": name, "path": os.path.normpath(target)})
    return out


def _first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _pyvenv_cfg(prefix: str) -> dict[str, str] | None:
    path = os.path.join(prefix, "pyvenv.cfg")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    cfg: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            cfg[key.strip().lower()] = value.strip()
    return cfg


def conda_state(prefix: str) -> dict[str, Any] | None:
    """The conda packages a prefix holds (``conda-meta/*.json``) and the specs
    the user asked for (``conda-meta/history``), or ``None`` for a non-conda
    prefix."""
    meta = os.path.join(prefix, "conda-meta")
    if not os.path.isdir(meta):
        return None
    packages: list[dict[str, Any]] = []
    for name in sorted(os.listdir(meta)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(meta, name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        packages.append(
            {
                key: record.get(key, "")
                for key in ("name", "version", "build", "channel", "url", "md5", "subdir")
            }
        )
    history = ""
    try:
        with open(os.path.join(meta, "history"), encoding="utf-8", errors="replace") as handle:
            history = handle.read(MAX_TEXT_BYTES)
    except OSError:
        pass
    return {"packages": packages, "history": history}


def environment() -> dict[str, Any]:
    prefix = sys.prefix
    sites = _site_dirs()
    pip_conf = None
    for name in ("pip.conf", "pip.ini"):
        entry = _read_file(os.path.join(prefix, name))
        if entry is not None and "text" in entry:
            pip_conf = entry["text"]
            break
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": sys.implementation.name,
            "executable": sys.executable,
            "prefix": prefix,
            "base_prefix": getattr(sys, "base_prefix", prefix),
        },
        "pyvenv_cfg": _pyvenv_cfg(prefix),
        "conda": conda_state(prefix),
        "sites": sites,
        "distributions": distributions(sites),
        "path_entries": path_entries(sites),
        "pip_conf": pip_conf,
    }


def _inside_dir(root: str, rel: str) -> bool:
    """Whether ``rel`` is a folder that stays inside ``root`` once every link is
    followed: a link out of the workspace (``libs/x -> /``) is never present."""
    target = os.path.join(root, *rel.split("/"))
    if not os.path.isdir(target):
        return False
    real_root = os.path.realpath(root)
    real = os.path.realpath(target)
    return real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)


def collect(
    root: str, *, inspect_env: bool = True, paths: list[str] | None = None
) -> dict[str, Any]:
    """Everything the capture and the recreate planner read, as plain data."""
    result: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "root": root,
        "host": {
            "sys_platform": sys.platform,
            "machine": platform.machine(),
            "platform_tag": sysconfig.get_platform(),
        },
        "files": project_files(root) if os.path.isdir(root) else {},
        "paths": {rel: _inside_dir(root, rel) for rel in paths or []},
        "env_vars": {name: os.environ[name] for name in INDEX_ENV_VARS if os.environ.get(name)},
        "env": environment() if inspect_env else None,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="env_probe")
    parser.add_argument("--root", required=True)
    parser.add_argument("--no-env", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    args = parser.parse_args(argv)
    payload = collect(args.root, inspect_env=not args.no_env, paths=args.path)
    sys.stdout.write(MARKER + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
