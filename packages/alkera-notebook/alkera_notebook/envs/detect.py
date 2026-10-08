"""Detecting a notebook's candidate environments.

Kinds: ``default`` (always; the spec lives in the workspace at
``.alkera/envs/default/`` and the environment outside the tree under the env
root), ``uv_project`` (the nearest enclosing ``pyproject.toml`` with a
``[project]`` table, environment at ``<project>/.venv``), ``script`` (the
notebook's PEP 723 block, environment under the env root), ``venv`` (an
existing directory with ``pyvenv.cfg``, used as is), and the listed-only
``requirements`` and ``conda``.

An ``env_id`` is ``<kind>:<spec root relative to the workspace>`` (the
notebook's own path for ``script``), so it is stable across machines and
copies of the tree.

The workspace tree and the env root are written by the notebook's people and
agents (from any cell, as the kernel's uid) while the detector reads them as
the engine's uid, which can read far more. Every read goes through a
:class:`~alkera_notebook.tree_io.Tree` anchored at one of the two roots, so a
link or a fifo planted at a spec path is never followed or opened: detection
reads it as absent, and :func:`project_spec` refuses it.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

from alkera_notebook.envs.models import EnvDescriptor, EnvKind, EnvState
from alkera_notebook.envs.script import script_block_text
from alkera_notebook.envs.state import BuildRecord, BuildRecords
from alkera_notebook.tree_io import LinkRefusedError, Tree

DEFAULT_SPEC_DIR = Path(".alkera") / "envs" / "default"
_VENV_SKIP = frozenset({"node_modules", "__pycache__", ".git", "__marimo__"})


class EnvNotFoundError(Exception):
    """A recorded ``env`` names nothing usable."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def spec_hash(spec: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(spec):
        h.update(name.encode("utf-8") + b"\x00" + spec[name] + b"\x00")
    return h.hexdigest()


def env_fingerprint(kind: str, spec: bytes, python_version: str, platform_tag: str) -> str:
    """``sha256(kind || lock or spec bytes || python version || platform tag)``."""
    return sha256_hex(
        b"\x00".join([kind.encode(), spec, python_version.encode(), platform_tag.encode()])
    )


def interpreter_in(prefix: Path) -> Path:
    if sys.platform == "win32":
        return prefix / "Scripts" / "python.exe"
    return prefix / "bin" / "python"


def pyvenv_version(cfg: bytes | None) -> str:
    """The Python version a ``pyvenv.cfg``'s bytes name, or ``""``."""
    if cfg is None:
        return ""
    values: dict[str, str] = {}
    for line in cfg.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values.get("version_info") or values.get("version") or ""


def _rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel or "."


def recorded_path(target: Path, notebook_dir: Path) -> str:
    """``target`` as a header ``env`` value: ``./x`` or ``../x``."""
    rel = Path(os.path.relpath(target, notebook_dir)).as_posix()
    if rel == ".":
        return "./"
    if all(part == ".." for part in rel.split("/")):
        return rel + "/"
    return rel if rel.startswith("../") or rel == ".." else f"./{rel}"


def read_tree_file(tree: Tree, path: Path) -> bytes | None:
    """``path``'s bytes, read through ``tree`` without following a link, or
    None when it is missing. A link (at ``path`` or on its way), an entry that
    is not a regular file, or a path outside the tree raises
    :class:`LinkRefusedError`."""
    try:
        return tree.read_bytes(path)
    except FileNotFoundError:
        return None
    except ValueError:
        raise LinkRefusedError(path, "is outside the tree") from None


class Detector:
    """Builds descriptors for one workspace and env root."""

    def __init__(self, workspace_root: Path, env_root: Path) -> None:
        self.root = workspace_root.resolve()
        self.env_root = env_root.resolve()
        self.records = BuildRecords(self.env_root)
        #: The two trees everything read lies in, the deeper root first (the
        #: env root may lie inside the workspace).
        self._trees = sorted(
            (Tree(self.root), Tree(self.env_root)), key=lambda t: len(t.root.parts), reverse=True
        )

    # -- reading -----------------------------------------------------------

    def _tree_for(self, path: Path) -> Tree | None:
        for tree in self._trees:
            if path == tree.root or tree.root in path.parents:
                return tree
        return None

    def read_file(self, path: Path) -> bytes | None:
        """A regular file's bytes, or None when it is missing, a link, not a
        regular file, or outside both trees."""
        tree = self._tree_for(path)
        if tree is None:
            return None
        try:
            return read_tree_file(tree, path)
        except OSError:
            return None

    def has_file(self, path: Path) -> bool:
        """Whether ``path`` is a regular file reached through no link."""
        tree = self._tree_for(path)
        if tree is None:
            return False
        try:
            return tree.is_file(path)
        except (OSError, ValueError):
            return False

    def is_uv_project(self, d: Path) -> bool:
        return parses_as_uv_project(self.read_file(d / "pyproject.toml"))

    def spec(self, spec_root: Path) -> dict[str, bytes]:
        """The project spec files under ``spec_root`` that are regular files
        (a link is left out, never read)."""
        spec: dict[str, bytes] = {}
        for name in SPEC_FILES:
            data = self.read_file(spec_root / name)
            if data is not None:
                spec[name] = data
        return spec

    # -- helpers -----------------------------------------------------------

    def _ancestors(self, start: Path) -> Iterator[Path]:
        d = start
        while True:
            yield d
            if d == self.root or self.root not in d.parents:
                return
            d = d.parent

    def inside(self, path: Path) -> bool:
        p = path.resolve()
        return p == self.root or self.root in p.parents

    def _state(self, kind: EnvKind, prefix: Path, spec_h: str, record: BuildRecord) -> EnvState:
        built = interpreter_in(prefix).exists()
        if kind == "venv":
            return "ready" if built else "missing"
        if kind in ("requirements", "conda"):
            return "missing"
        if not built:
            return "failed" if record.failed else "missing"
        if record.failed:
            return "failed"
        return "ready" if record.built_spec_hash == spec_h else "stale"

    def _make(
        self,
        kind: EnvKind,
        env_id: str,
        spec_root: Path,
        prefix: Path | None,
        spec: dict[str, bytes],
        recorded: str,
    ) -> EnvDescriptor:
        spec_h = spec_hash(spec)
        record = self.records.get(env_id)
        if kind == "venv":
            built: str | None = spec_h
        else:
            built = record.built_spec_hash
        if prefix is None:
            return EnvDescriptor(
                env_id=env_id,
                kind=kind,
                spec_root=str(spec_root),
                prefix="",
                interpreter="",
                python_version="",
                spec_hash=spec_h,
                built_spec_hash=None,
                state="missing",
                recorded=recorded,
            )
        interp = interpreter_in(prefix)
        return EnvDescriptor(
            env_id=env_id,
            kind=kind,
            spec_root=str(spec_root),
            prefix=str(prefix),
            interpreter=str(interp),
            python_version=pyvenv_version(self.read_file(prefix.resolve() / "pyvenv.cfg")),
            spec_hash=spec_h,
            built_spec_hash=built,
            state=self._state(kind, prefix, spec_h, record),
            recorded=recorded,
            python_build="" if kind == "venv" else record.python_build,
            last_failure="" if kind == "venv" else record.last_failure,
        )

    def _managed_prefix(self, env_id: str, kind: str) -> Path:
        return self.env_root / f"{kind}-{sha256_hex(env_id.encode())[:12]}"

    # -- one descriptor per kind ---------------------------------------------

    def default(self) -> EnvDescriptor:
        spec_root = self.root / DEFAULT_SPEC_DIR
        env_id = f"default:{DEFAULT_SPEC_DIR.as_posix()}"
        return self._make(
            "default",
            env_id,
            spec_root,
            self._managed_prefix(env_id, "default"),
            self.spec(spec_root),
            "default",
        )

    def uv_project(self, project: Path, notebook_dir: Path) -> EnvDescriptor:
        project = project.resolve()
        return self._make(
            "uv_project",
            f"uv_project:{_rel(project, self.root)}",
            project,
            project / ".venv",
            self.spec(project),
            recorded_path(project, notebook_dir),
        )

    def venv(self, prefix: Path, notebook_dir: Path) -> EnvDescriptor:
        prefix = prefix.resolve()
        cfg = self.read_file(prefix / "pyvenv.cfg") or b""
        return self._make(
            "venv",
            f"venv:{_rel(prefix, self.root)}",
            prefix,
            prefix,
            {"pyvenv.cfg": cfg},
            recorded_path(prefix, notebook_dir),
        )

    def script(self, notebook: Path) -> EnvDescriptor | None:
        text = (self.read_file(notebook) or b"").decode("utf-8", "replace")
        block = script_block_text(text)
        if block is None:
            return None
        env_id = f"script:{_rel(notebook, self.root)}"
        return self._make(
            "script",
            env_id,
            notebook,
            self._managed_prefix(env_id, "script"),
            {"script": block.encode("utf-8")},
            "script",
        )

    def listed(self, kind: EnvKind, spec_file: Path, notebook_dir: Path) -> EnvDescriptor:
        return self._make(
            kind,
            f"{kind}:{_rel(spec_file.parent, self.root)}",
            spec_file.parent,
            None,
            {spec_file.name: self.read_file(spec_file) or b""},
            recorded_path(spec_file.parent, notebook_dir),
        )

    # -- detection -------------------------------------------------------------

    def detect(self, notebook_path: Path) -> list[EnvDescriptor]:
        notebook = notebook_path.resolve()
        nb_dir = notebook.parent
        found: list[EnvDescriptor] = []
        project_prefixes: set[Path] = set()
        for d in self._ancestors(nb_dir):
            if self.is_uv_project(d):
                found.append(self.uv_project(d, nb_dir))
                project_prefixes.add((d / ".venv").resolve())
                break
        for d in self._ancestors(nb_dir):
            candidates = [
                d,
                *sorted((c for c in _subdirs(d) if c.name not in _VENV_SKIP), key=lambda p: p.name),
            ]
            for c in candidates:
                if self.has_file(c / "pyvenv.cfg") and c.resolve() not in project_prefixes:
                    found.append(self.venv(c, nb_dir))
        script = self.script(notebook)
        if script is not None:
            found.append(script)
        found.append(self.default())
        listed_kinds: tuple[tuple[EnvKind, tuple[str, ...]], ...] = (
            ("requirements", ("requirements.txt",)),
            ("conda", ("environment.yml", "environment.yaml")),
        )
        for kind, names in listed_kinds:
            for d in self._ancestors(nb_dir):
                hit = next((d / n for n in names if self.has_file(d / n)), None)
                if hit is not None:
                    found.append(self.listed(kind, hit, nb_dir))
                    break
        return found

    def describe_path(self, target: Path, notebook_dir: Path) -> EnvDescriptor:
        """The environment a recorded path names: a venv or a uv project."""
        target = target.resolve()
        if not self.inside(target):
            raise EnvNotFoundError(f"{target} is outside the workspace")
        if self.has_file(target / "pyvenv.cfg"):
            return self.venv(target, notebook_dir)
        if self.is_uv_project(target):
            return self.uv_project(target, notebook_dir)
        raise EnvNotFoundError(f"{target} is neither a venv nor a uv project")


def _subdirs(d: Path) -> list[Path]:
    try:
        return [c for c in d.iterdir() if c.is_dir()]
    except OSError:
        return []


def parses_as_uv_project(pyproject: bytes | None) -> bool:
    """Whether a ``pyproject.toml``'s bytes have a ``[project]`` table."""
    if pyproject is None:
        return False
    try:
        parsed = tomllib.loads(pyproject.decode("utf-8", "replace"))
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(parsed.get("project"), dict)


#: The files a ``default`` or ``uv_project`` spec is made of.
SPEC_FILES = ("pyproject.toml", "uv.lock")


def project_spec(tree: Tree, spec_root: Path) -> dict[str, bytes]:
    """The spec files under ``spec_root``, read through ``tree``. A missing
    file is left out; a link or any other kind of entry raises
    :class:`LinkRefusedError`, so its target's bytes are never read."""
    spec: dict[str, bytes] = {}
    for name in SPEC_FILES:
        data = read_tree_file(tree, spec_root / name)
        if data is not None:
            spec[name] = data
    return spec


def detect_envs(
    workspace_root: str | Path, notebook_path: str | Path, *, env_root: str | Path
) -> list[EnvDescriptor]:
    """Every environment a notebook could run in, most specific first."""
    return Detector(Path(workspace_root), Path(env_root)).detect(Path(notebook_path))
