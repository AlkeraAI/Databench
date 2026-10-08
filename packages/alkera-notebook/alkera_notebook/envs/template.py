"""The template a workspace's default notebook environment starts from.

A notebook whose ``env`` is ``default`` runs in the environment specified at
``.alkera/envs/default/`` in its workspace. The first run in a workspace
without one seeds it from this template: a project pinning a curated set of
popular data, charting, science and I/O libraries. The template is only the
``pyproject.toml``; the lock is made once, when the spec is seeded, with the
same index and Python as the build, and written into the workspace's spec, so
every later build is ``uv sync --frozen`` on that workspace's own lock.
Packages are added to it by an install. The files are written at run time
(:func:`default_env_template`), since a compiled build carries no package data.

A spec someone changed stays theirs, but it is never left short of the
template: once per template, every template package it does not name is added
at the template's pin (nothing it names is changed, so nothing is
downgraded), and the next build takes them. An install asking for a template package by name alone
gets the template's pin; asking for another version is allowed, and the
install says it moved the pin.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import secrets
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from alkera_notebook.envs.detect import DEFAULT_SPEC_DIR, sha256_hex
from alkera_notebook.envs.models import EnvDescriptor
from alkera_notebook.envs.registry import EnvBuildError, EnvError, LocalEnvRegistry
from alkera_notebook.tree_io import LinkRefusedError, Tree

#: What every workspace's default environment has before anyone installs
#: anything. Each is pinned to one version that has wheels for CPython 3.10
#: to 3.13 on Linux (x86_64, aarch64) and macOS; where the newest release
#: dropped Python 3.10 (numpy, pandas, scipy, ...) the pin is the newest that
#: still has it. Changing the list upgrades every workspace still on the
#: template (see :data:`PAST_TEMPLATE_SPECS`).
DEFAULT_ENV_PACKAGES: Final = (
    # data
    "polars==1.44.2",  # the newest 1.x: 2.0 shipped on Oct 6 2026
    "pandas==2.3.3",
    "pyarrow==25.0.1",
    "duckdb==1.5.6",
    "numpy==2.2.6",
    # charts
    "matplotlib==3.10.9",
    "seaborn==0.13.2",
    "plotly==7.1.0",
    "altair==6.2.2",
    # widgets
    "ipywidgets==8.1.9",
    "anywidget==0.11.0",
    # science
    "scipy==1.15.3",
    "scikit-learn==1.7.2",
    "statsmodels==0.15.0",
    # I/O
    "requests==2.34.2",
    "httpx==0.28.1",
    "sqlalchemy==2.0.54",
    "openpyxl==3.1.5",
    # utilities
    "pydantic==2.13.5",
    "tqdm==4.70.1",
    "beautifulsoup4==4.15.0",
)


#: The sha256 of every template ``pyproject.toml`` an earlier release seeded.
#: A workspace whose spec still has one is upgraded to the current template
#: on its next build, its lock made again. Entries are never removed (an engine
#: may meet a workspace seeded by any past release): when the list above
#: changes, the digest of the template it replaces is added here.
PAST_TEMPLATE_PYPROJECTS: Final = frozenset(
    {
        # ``dependencies = []``, seeded with a one-package lock.
        "16c871f5d0080ae8d6946dad6bc66a05fa294f375483a05a4100eeddaaef7d39",
    }
)

#: Under the env root: the sha256 of the template the spec was last brought
#: up to. A package someone removes after that stays removed until the
#: template itself changes.
_TEMPLATE_APPLIED_MARKER: Final = "default-template-applied"

_DEFAULT_ENV_ID: Final = f"default:{DEFAULT_SPEC_DIR.as_posix()}"

_NAME: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def requirement_name(requirement: str) -> str:
    """A requirement's distribution name, normalized (PEP 503)."""
    found = _NAME.match(requirement.strip())
    return re.sub(r"[-_.]+", "-", found.group(0)).lower() if found else ""


def project_dependencies(pyproject: bytes | str | None) -> list[str]:
    """``[project].dependencies`` of a ``pyproject.toml``, as written."""
    if pyproject is None:
        return []
    text = pyproject.decode("utf-8", "replace") if isinstance(pyproject, bytes) else pyproject
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return []
    deps = project.get("dependencies") if isinstance(project, dict) else None
    return [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []


def missing_from_template(spec_deps: list[str], template_deps: list[str]) -> list[str]:
    """The template's pins for every package ``spec_deps`` does not name."""
    named = {requirement_name(d) for d in spec_deps}
    return [d for d in template_deps if requirement_name(d) not in named]


def template_pyproject(packages: Sequence[str]) -> str:
    """The default environment's ``pyproject.toml`` with ``packages``."""
    deps = "".join(f'    "{p}",\n' for p in packages)
    return (
        "[project]\n"
        'name = "default"\n'
        'version = "0.0.0"\n'
        'description = "The workspace\'s default notebook environment."\n'
        'requires-python = ">=3.10"\n'
        f"dependencies = [\n{deps}]\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )


DEFAULT_ENV_PYPROJECT: Final = template_pyproject(DEFAULT_ENV_PACKAGES)

TEMPLATE_FILES: Final = {"pyproject.toml": DEFAULT_ENV_PYPROJECT}


def default_env_template(directory: Path, packages: Sequence[str] = DEFAULT_ENV_PACKAGES) -> Path:
    """The template in ``directory`` (written when absent or different), for
    ``TemplatedEnvRegistry(default_template=...)``. A lock an earlier
    release left there is removed: the lock is made when a spec is seeded."""
    tree = Tree(directory)
    text = template_pyproject(packages)
    try:
        current: str | None = tree.read_text("pyproject.toml")
    except (OSError, UnicodeDecodeError):
        current = None
    if current != text:
        tree.write_text("pyproject.toml", text)
    tree.unlink("uv.lock")
    return directory


class TemplatedEnvRegistry(LocalEnvRegistry):
    """A registry whose ``default`` environment starts from the built-in
    template: seeded from it when absent, upgraded to it while its
    ``pyproject.toml`` is an earlier template, and brought up to it (missing
    packages added) once per template when someone changed it."""

    def __init__(
        self,
        workspace_root: str | Path,
        env_root: str | Path,
        *,
        default_template: Path,
        past_templates: frozenset[str] = PAST_TEMPLATE_PYPROJECTS,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace_root, env_root, default_template=default_template, **kwargs)
        self._template_dir = default_template
        self._past = past_templates - {sha256_hex(self._template_pyproject())}
        self._seed_lock = asyncio.Lock()

    @property
    def _spec_root(self) -> Path:
        return self.root / DEFAULT_SPEC_DIR

    def _template_pyproject(self) -> bytes:
        return (self._template_dir / "pyproject.toml").read_bytes()

    def _applied_marker(self) -> Path:
        return self.env_root / _TEMPLATE_APPLIED_MARKER

    def _template_pins(self) -> dict[str, str]:
        """``name -> the template's pin`` for every template package."""
        deps = project_dependencies(self._template_pyproject())
        return {requirement_name(d): d for d in deps}

    async def _seed_default(self) -> Path:
        """The default spec, seeded from the template when absent and
        upgraded to it when its pyproject is an earlier template. Any other
        spec is left as it is."""
        async with self._seed_lock:
            spec_root = self._spec_root
            if spec_root.is_symlink():
                return await super()._seed_default()  # which refuses it
            pyproject = self._spec(spec_root).get("pyproject.toml")
            if pyproject is not None and sha256_hex(pyproject) not in self._past:
                await self._top_up(spec_root, pyproject)
                return await super()._seed_default()
            await self._seed(spec_root)
            self._env_tree.write_text(
                self._applied_marker(), sha256_hex(self._template_pyproject())
            )
            return spec_root

    async def _top_up(self, spec_root: Path, pyproject: bytes) -> None:
        """Add, at the template's pins, every template package a changed spec
        does not name, once per template. What the spec names is left as it
        is. A failed add leaves the spec as it was and says why."""
        digest = sha256_hex(self._template_pyproject())
        try:
            applied = self._env_tree.read_text(self._applied_marker()).strip()
        except (OSError, ValueError):
            applied = ""
        if applied == digest:
            return
        missing = missing_from_template(
            project_dependencies(pyproject), project_dependencies(self._template_pyproject())
        )
        if missing:
            before = self._spec(spec_root)
            log: list[str] = []
            argv = [
                self._uv,
                "add",
                "--no-sync",
                "--project",
                str(spec_root),
                "--python",
                self._python.request,
                *self._index_args,
                *missing,
            ]
            try:
                await self._run(argv, spec_root, log, None)
            except EnvBuildError as exc:
                self._restore_spec(spec_root, before)
                message = f"Adding the workspace template's packages failed. {_cause(log)}".strip()
                self._record_failure(message, exc.log)
                return
        self._env_tree.write_text(self._applied_marker(), digest)

    async def install(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        """An install into the default environment never moves a template pin
        unasked: a template package asked for by name alone gets the
        template's pin, and one the spec already names is left as it is.
        Asking for another version moves the pin, and the log says so."""
        if not env_id.startswith("default:"):
            return await super().install(env_id, packages, notebook_path)
        try:
            await self._seed_default()
        except LinkRefusedError as exc:
            raise EnvError(str(exc)) from exc
        pins = self._template_pins()
        current = {requirement_name(d): d for d in await self.requirements(env_id)}
        asked: list[str] = []
        said: list[str] = []
        for package in packages:
            name = requirement_name(package)
            found = _NAME.match(package.strip())
            bare = found is not None and found.group(0) == package.strip()
            if bare and name in current:
                said.append(f"{name} is already in the environment as {current[name]}.")
                continue
            if bare and name in pins:
                asked.append(pins[name])
                continue
            if name in pins and package.strip() != pins[name]:
                said.append(f"{name} moves from the template's {pins[name]} to {package.strip()}.")
            asked.append(package)
        note = "\n".join(said)
        if not asked:
            return self.describe(env_id), [], note
        desc, changed, log = await super().install(env_id, asked, notebook_path)
        return desc, changed, f"{note}\n{log}" if note else log

    async def _seed(self, spec_root: Path) -> None:
        """Lock the template in a staging project beside the spec, then put
        the lock and the pyproject in place, the lock first. A failed lock
        leaves the spec as it was (or absent); an interruption between the
        two leaves an earlier template's pyproject, so the next build seeds
        again."""
        template = self._template_pyproject()
        staging = spec_root.with_name(f".default-seed-{secrets.token_hex(6)}")
        self._tree.make_dirs(staging)
        try:
            self._tree.write_atomic(staging / "pyproject.toml", template)
            await self._lock_into(staging)
            lock = self._spec(staging).get("uv.lock")
            if lock is None:
                raise EnvBuildError("Locking the default environment wrote no uv.lock.")
            self._tree.make_dirs(spec_root)
            self._tree.write_atomic(spec_root / "uv.lock", lock)
            self._tree.write_atomic(spec_root / "pyproject.toml", template)
        finally:
            with contextlib.suppress(OSError):
                self._tree.rmtree(staging)

    async def _lock_into(self, project: Path) -> None:
        log: list[str] = []
        argv = [
            self._uv,
            "lock",
            "--project",
            str(project),
            "--python",
            self._python.request,
            *self._index_args,
        ]
        try:
            await self._run(argv, project, log, None)
        except EnvBuildError as exc:
            message = f"Locking the default environment's packages failed. {_cause(log)}"
            self._record_failure(message, exc.log)
            raise EnvBuildError(message, log=exc.log) from None

    def _record_failure(self, message: str, log: str) -> None:
        """The failed lock as the environment's ``last_failure``; it reads
        ``failed`` only when no earlier build is there to run."""
        records = self._detector.records
        records.put(
            _DEFAULT_ENV_ID,
            dataclasses.replace(
                records.get(_DEFAULT_ENV_ID),
                failed=not Path(self._detector.default().interpreter).exists(),
                last_failure=message,
                log=log,
            ),
        )


#: How uv starts the statement that names what went wrong.
_CAUSE_MARKERS: Final = ("\u2570\u2500\u25b6", "\u00d7", "error:")


def _cause(log: list[str]) -> str:
    """What uv said went wrong: its last statement, which uv wraps over
    several lines to the width it guesses (narrow on a CI runner), joined
    back into one sentence."""
    for entry in reversed(log):
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        if not lines or entry.startswith("$ "):
            continue
        start = max(
            (i for i, line in enumerate(lines) if line.startswith(_CAUSE_MARKERS)),
            default=len(lines) - 1,
        )
        said = " ".join(lines[start:])
        for marker in _CAUSE_MARKERS:
            said = said.removeprefix(marker)
        return said.strip()
    return ""


__all__ = [
    "DEFAULT_ENV_PACKAGES",
    "DEFAULT_ENV_PYPROJECT",
    "PAST_TEMPLATE_PYPROJECTS",
    "TEMPLATE_FILES",
    "TemplatedEnvRegistry",
    "default_env_template",
    "missing_from_template",
    "project_dependencies",
    "requirement_name",
    "template_pyproject",
]
