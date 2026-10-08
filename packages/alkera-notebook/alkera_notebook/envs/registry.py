"""The core's environment registry: detect, resolve, materialize, install.

Every build command is ``uv``, run through a :class:`CommandRunner` with no
user config, no Python downloads (``UV_PYTHON_DOWNLOADS=never``), an explicit
``--python`` and the configured index arguments; offline (``UV_OFFLINE=1``)
unless the registry is told its builds reach the index. A configured Python
version always names its build (``3.14+gil`` or ``3.14t``); after a build the
environment's interpreter reports the build it is, which is recorded (and a
build other than the one asked for fails the build).

Builds: an environment builds whenever its spec changed since its last
build, is missing, or failed. There is no separate approval: a change an
agent makes went through the agent's own tool permission, and a change a
person makes builds right away. The build record keeps what was built, who
asked and when.

Installing into a ``script`` environment edits the notebook's PEP 723 block,
which is part of the document: the registry refuses with
:class:`ScriptInstallViaDocumentError`, and the engine applies
:func:`~alkera_notebook.envs.script.add_script_dependencies` to the header as
a document op, then materializes.

Every spec file the registry reads is read through the workspace's
:class:`~alkera_notebook.tree_io.Tree`, never through a link: the spec lives
in a tree any cell can write, and what the registry reads it may write back
there (a failed change puts the spec back) or report out.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import sys
import sysconfig
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from alkera_notebook import tree_io
from alkera_notebook.envs import generations
from alkera_notebook.envs.detect import (
    DEFAULT_SPEC_DIR,
    Detector,
    EnvNotFoundError,
    env_fingerprint,
    interpreter_in,
    project_spec,
    read_tree_file,
)
from alkera_notebook.envs.models import CommandRunner, EnvDescriptor
from alkera_notebook.envs.python import (
    PROBE,
    InvalidPythonRequestError,
    PythonBuild,
    default_python,
    parse_probe,
    python_request,
)
from alkera_notebook.envs.script import (
    script_block_text,
    script_dependencies,
)
from alkera_notebook.envs.state import BuildRecord
from alkera_notebook.tree_io import Tree, TreeModes

# Import names whose distribution is called something else.
MODULE_DISTRIBUTIONS: dict[str, str] = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
}

_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
    r"(?:(?:~=|===|==|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+"
    r"(?:,(?:~=|===|==|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+)*)?$"
)

_RESOLUTION_ORDER = ("uv_project", "venv", "script", "default")


class EnvError(Exception):
    def __init__(self, message: str, *, log: str = "") -> None:
        super().__init__(message)
        self.log = log


class EnvBuildError(EnvError):
    """A build command failed; ``log`` holds its output."""


class EnvBuildCancelledError(EnvError):
    """Someone cancelled the build; nothing it would have changed changed."""


class EnvNotMaterializableError(EnvError):
    """v1 lists this kind but does not build it (``requirements``, ``conda``)."""


class InvalidRequirementError(EnvError, ValueError):
    pass


class InvalidPythonError(EnvError, InvalidPythonRequestError):
    """The configured Python is neither a version nor an interpreter path."""


class DistributionRequiredError(EnvError):
    """An import name with no known distribution needs an explicit one."""


class ScriptInstallViaDocumentError(EnvError):
    """A ``script`` env's spec is the notebook: install is a document op."""


def validate_requirement(requirement: str) -> str:
    """A PEP 508 name with optional extras and version specifiers, nothing else."""
    if not isinstance(requirement, str) or not _REQUIREMENT.fullmatch(requirement):
        raise InvalidRequirementError(f"not a package requirement: {requirement!r}")
    return requirement


def distribution_for_module(module: str, distribution: str | None = None) -> str:
    """The distribution to install for a missing import.

    An explicit ``distribution`` wins; otherwise only import names in
    :data:`MODULE_DISTRIBUTIONS` are mapped. Any other module requires an
    explicit distribution (an import name is not proof of a package name).
    """
    if distribution:
        return validate_requirement(distribution)
    top = module.partition(".")[0]
    mapped = MODULE_DISTRIBUTIONS.get(top)
    if mapped is None:
        raise DistributionRequiredError(
            f"no known distribution for module {top!r}; name the distribution to install"
        )
    return mapped


class LocalEnvRegistry:
    def __init__(
        self,
        workspace_root: str | Path,
        env_root: str | Path,
        *,
        runner: CommandRunner,
        python: str | None = None,
        free_threaded: bool = False,
        uv: str = "uv",
        index_args: Sequence[str] = (),
        default_template: Path | None = None,
        cache_dir: Path | None = None,
        timeout_s: float = 900.0,
        base_env: Mapping[str, str] | None = None,
        offline: bool = True,
        tree_modes: TreeModes | None = None,
    ) -> None:
        self._detector = Detector(Path(workspace_root), Path(env_root))
        self._modes = tree_modes
        self.root = self._detector.root
        self.env_root = self._detector.env_root
        #: The workspace tree and the env root, both written by the notebook's
        #: people and agents too: everything the registry writes there goes
        #: through these, never through a link.
        self._tree = Tree(self.root, modes=tree_modes)
        self._env_tree = Tree(self.env_root, modes=tree_modes)
        self._runner = runner
        try:
            self._python = python_request(python or default_python(), free_threaded=free_threaded)
        except InvalidPythonRequestError as exc:
            raise InvalidPythonError(str(exc)) from exc
        self._uv = uv
        self._index_args = list(index_args)
        #: Whether builds are kept off the network. An install or a
        #: materialization that must fetch packages needs it ``False``.
        self._offline = offline
        self._template = default_template
        self._cache = cache_dir or (self.env_root / "cache")
        self._timeout_s = timeout_s
        self._base_env = dict(base_env if base_env is not None else os.environ)
        self._locks: dict[str, asyncio.Lock] = {}
        self._building: set[str] = set()
        #: The build running for an environment, so it can be cancelled.
        self._builds: dict[str, asyncio.Task[tuple[str, PythonBuild]]] = {}
        self._cancelled: set[str] = set()

    # -- describing --------------------------------------------------------------

    def _notebook(self, notebook_path: str) -> Path:
        p = Path(notebook_path)
        return (p if p.is_absolute() else self.root / p).resolve()

    def describe(self, env_id: str, notebook_dir: Path | None = None) -> EnvDescriptor:
        kind, sep, rel = env_id.partition(":")
        if not sep:
            raise EnvNotFoundError(f"unknown environment {env_id!r}")
        target = (self.root / rel).resolve()
        if not self._detector.inside(target):
            raise EnvNotFoundError(f"unknown environment {env_id!r}")
        nb_dir = notebook_dir or self.root
        desc: EnvDescriptor | None
        if kind == "default" and rel == DEFAULT_SPEC_DIR.as_posix():
            desc = self._detector.default()
        elif kind == "uv_project" and self._detector.is_uv_project(target):
            desc = self._detector.uv_project(target, nb_dir)
        elif kind == "venv" and self._detector.has_file(target / "pyvenv.cfg"):
            desc = self._detector.venv(target, nb_dir)
        elif kind == "script":
            desc = self._detector.script(target)
        else:
            desc = None
        if desc is None or desc.env_id != env_id:
            raise EnvNotFoundError(f"unknown environment {env_id!r}")
        if env_id in self._building:
            desc = dataclasses.replace(desc, state="building")
        return desc

    async def detect(self, notebook_path: str) -> list[EnvDescriptor]:
        return self._detector.detect(self._notebook(notebook_path))

    async def resolve(self, notebook_path: str, recorded: str | None) -> EnvDescriptor:
        """The notebook's environment: its recorded ``env``, else detection's pick."""
        nb = self._notebook(notebook_path)
        nb_dir = nb.parent
        if recorded == "default":
            return self._detector.default()
        if recorded == "script":
            script = self._detector.script(nb)
            if script is None:
                raise EnvNotFoundError("env is 'script' but the notebook has no script block")
            return script
        if recorded is not None:
            if not recorded.startswith(("./", "../")) and recorded != "..":
                raise EnvNotFoundError(f"unrecognized env value {recorded!r}")
            return self._detector.describe_path(nb_dir / recorded, nb_dir)
        found = self._detector.detect(nb)
        for kind in _RESOLUTION_ORDER:
            for desc in found:
                if desc.kind == kind:
                    return desc
        return self._detector.default()

    # -- building ----------------------------------------------------------------

    def _env(self, prefix: Path | None = None) -> dict[str, str]:
        keep = ("PATH", "HOME", "LANG", "TZ", "TMPDIR", "SYSTEMROOT", "USERPROFILE")
        env = {k: v for k, v in self._base_env.items() if k in keep}
        if self._offline:
            env["UV_OFFLINE"] = "1"
        env.update(
            {
                "UV_PYTHON_DOWNLOADS": "never",
                "UV_NO_CONFIG": "1",
                "UV_CACHE_DIR": str(self._cache),
                "UV_NO_PROGRESS": "1",
            }
        )
        if prefix is not None:
            env["UV_PROJECT_ENVIRONMENT"] = str(prefix)
        return env

    async def _run(self, argv: list[str], cwd: Path, log: list[str], prefix: Path | None) -> None:
        log.append("$ " + " ".join(argv))
        result = await self._runner.run(
            argv, cwd=str(cwd), env=self._env(prefix), timeout_s=self._timeout_s
        )
        if result.stdout:
            log.append(result.stdout.rstrip())
        if result.stderr:
            log.append(result.stderr.rstrip())
        if result.returncode != 0:
            raise EnvBuildError(
                f"{argv[1] if len(argv) > 1 else argv[0]} failed (exit {result.returncode})",
                log="\n".join(log),
            )

    def _spec(self, project: Path) -> dict[str, bytes]:
        """The spec files of a ``default`` or ``uv_project`` environment; a
        link or another kind of entry at one is refused."""
        try:
            return project_spec(self._tree, project)
        except tree_io.LinkRefusedError as exc:
            raise EnvError(str(exc)) from exc

    def _script_text(self, notebook: Path) -> str:
        """A ``script`` environment's notebook text; a link is refused."""
        try:
            data = read_tree_file(self._tree, notebook)
        except tree_io.LinkRefusedError as exc:
            raise EnvError(str(exc)) from exc
        return "" if data is None else data.decode("utf-8", "replace")

    def _current_spec(self, desc: EnvDescriptor) -> dict[str, str]:
        root = Path(desc.spec_root)
        if desc.kind in ("default", "uv_project"):
            return {
                name: data.decode("utf-8", "replace") for name, data in self._spec(root).items()
            }
        if desc.kind == "script":
            return {"script": script_block_text(self._script_text(root)) or ""}
        return {}

    async def _seed_default(self) -> Path:
        spec_root = self.root / DEFAULT_SPEC_DIR
        pyproject = spec_root / "pyproject.toml"
        if self._tree.exists(pyproject) and not self._tree.is_file(pyproject):
            raise tree_io.LinkRefusedError(pyproject, "is a link or not a file")
        if self._tree.is_file(pyproject):
            # Seeded before: by an earlier build, possibly under the old
            # owner-only modes, which a build as another uid cannot enter.
            self._tree.make_dirs(spec_root)
            for name in ("pyproject.toml", "uv.lock"):
                self._tree.share_file(spec_root / name)
            return spec_root
        if self._template is None:
            raise EnvError("the default environment has no spec and no template is configured")
        self._tree.make_dirs(spec_root)
        for name in ("pyproject.toml", "uv.lock"):
            src = self._template / name
            if src.is_file():
                self._tree.write_atomic(spec_root / name, src.read_bytes())
        return spec_root

    def _lock(self, env_id: str) -> asyncio.Lock:
        return self._locks.setdefault(env_id, asyncio.Lock())

    async def materialize(self, env_id: str, *, by: str = "") -> EnvDescriptor:
        """Build ``env_id`` from its current spec unless the build in use was
        made from it. ``by`` names who asked, for the build record."""
        try:
            async with self._lock(env_id):
                return await self._materialize(env_id, by=by)
        except tree_io.LinkRefusedError as exc:
            raise EnvError(str(exc)) from exc

    async def _materialize(self, env_id: str, *, by: str = "") -> EnvDescriptor:
        """Build ``env_id`` (its lock held). The build in use changes only
        when the new one succeeded; a failure is kept as ``last_failure`` and
        reads ``failed`` only when no build is there to run."""
        if env_id.startswith("default:"):
            await self._seed_default()
        desc = self.describe(env_id)
        if desc.kind == "venv":
            if desc.state != "ready":
                raise EnvNotFoundError(f"{desc.prefix} has no interpreter")
            return desc
        if desc.kind in ("requirements", "conda"):
            raise EnvNotMaterializableError(managed_elsewhere(desc))
        if desc.state == "ready":
            return desc
        records = self._detector.records
        record = records.get(env_id)
        spec = self._current_spec(desc)
        built_hash = desc.spec_hash
        prefix = Path(desc.prefix)
        managed = _managed(desc)
        target = generations.new_generation(prefix) if managed else prefix
        log: list[str] = []
        self._building.add(env_id)
        self._cancelled.discard(env_id)
        build = asyncio.ensure_future(self._build_into(desc, spec, log, target))
        self._builds[env_id] = build
        try:
            version, python_build = await build
            if managed:
                generations.adopt(self._env_tree, prefix, target)
        except (EnvError, asyncio.CancelledError) as exc:
            cancelled = env_id in self._cancelled
            if managed:
                generations.discard(self._env_tree, target)
            text = "\n".join(log) or (exc.log if isinstance(exc, EnvError) else "")
            message = "The build was cancelled." if cancelled else str(exc)
            records.put(
                env_id,
                dataclasses.replace(
                    record,
                    failed=not _exists(desc.interpreter),
                    last_failure=message,
                    log=text,
                ),
            )
            if cancelled:
                raise EnvBuildCancelledError(message, log=text) from None
            raise
        finally:
            self._building.discard(env_id)
            self._builds.pop(env_id, None)
            self._cancelled.discard(env_id)
        records.put(
            env_id,
            BuildRecord(
                built_spec_hash=built_hash,
                built_spec=spec,
                built_by=by,
                built_at=datetime.now(UTC).isoformat(),
                failed=False,
                log="\n".join(log),
                python_request=self._python.request,
                python_build=python_build,
                python_version=version,
            ),
        )
        return self.describe(env_id)

    async def cancel(self, env_id: str) -> bool:
        """Stop the build running for ``env_id``; False when none runs. The
        build's process group is killed and whatever it changed is undone."""
        build = self._builds.get(env_id)
        if build is None or build.done():
            return False
        self._cancelled.add(env_id)
        build.cancel()
        return True

    async def _build_into(
        self,
        desc: EnvDescriptor,
        spec: dict[str, str],
        log: list[str],
        target: Path,
    ) -> tuple[str, PythonBuild]:
        await self._build(desc, spec, log, target)
        return await self._probe(target, log)

    async def _build(
        self,
        desc: EnvDescriptor,
        spec: dict[str, str],
        log: list[str],
        prefix: Path,
    ) -> None:
        if desc.kind in ("default", "uv_project"):
            project = Path(desc.spec_root)
            await self._run(
                [
                    self._uv,
                    "sync",
                    "--frozen",
                    "--project",
                    str(project),
                    "--python",
                    self._python.request,
                    *self._index_args,
                ],
                project,
                log,
                prefix,
            )
            return
        if desc.kind == "script":
            deps = script_dependencies(spec.get("script", ""))
            for d in deps:
                validate_requirement(d)
            self._env_tree.make_dirs(prefix.parent)
            await self._run(
                [self._uv, "venv", "--clear", "--python", self._python.request, str(prefix)],
                self.env_root,
                log,
                None,
            )
            if deps:
                await self._run(
                    [
                        self._uv,
                        "pip",
                        "install",
                        "--python",
                        str(interpreter_in(prefix)),
                        *self._index_args,
                        *deps,
                    ],
                    self.env_root,
                    log,
                    None,
                )
            return
        raise EnvNotMaterializableError(f"cannot build a {desc.kind} environment")

    async def _probe(self, prefix: Path, log: list[str]) -> tuple[str, PythonBuild]:
        """The built interpreter's version and build; a build other than the
        one uv was asked for fails."""
        argv = [str(interpreter_in(prefix)), "-I", "-c", PROBE]
        log.append("$ " + " ".join(argv[:3]) + " <build probe>")
        result = await self._runner.run(
            argv, cwd=str(self.env_root), env=self._env(), timeout_s=60.0
        )
        try:
            if result.returncode != 0:
                raise ValueError(f"exit {result.returncode}: {result.stderr.strip()}")
            version, build = parse_probe(result.stdout)
        except ValueError as exc:
            log.append(str(exc))
            raise EnvBuildError(
                f"the built interpreter did not report its build ({exc})", log="\n".join(log)
            ) from exc
        log.append(f"python {version} ({build})")
        wanted = self._python.build
        if wanted is not None and build != wanted:
            raise EnvBuildError(
                f"asked uv for {self._python.request} but got a {build} Python {version}",
                log="\n".join(log),
            )
        return version, build

    # -- installing ----------------------------------------------------------------

    async def install(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        """Add ``packages`` to the env's spec and rebuild it.

        Returns the new descriptor, the spec files that changed (relative to
        the workspace) and the build log. A build that fails or is cancelled
        puts the spec back as it was and leaves the build in use."""
        reqs = [validate_requirement(p) for p in packages]
        if not reqs:
            raise InvalidRequirementError("no packages given")
        return await self._change_spec(env_id, "add", reqs, notebook_path)

    async def remove(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        """Take ``packages`` (names) out of the env's spec and rebuild it,
        on the same terms as :meth:`install`."""
        names = [validate_requirement(p) for p in packages]
        if not names:
            raise InvalidRequirementError("no packages given")
        return await self._change_spec(env_id, "remove", names, notebook_path)

    async def requirements(self, env_id: str) -> list[str]:
        """The spec's own requirements, as written (not what is installed)."""
        desc = self.describe(env_id)
        if desc.kind == "script":
            text = await asyncio.to_thread(self._script_text, Path(desc.spec_root))
            return script_dependencies(text)
        if desc.kind not in ("default", "uv_project"):
            return []
        data = self._spec(Path(desc.spec_root)).get("pyproject.toml")
        project = tomllib.loads(data.decode("utf-8", "replace")).get("project") if data else None
        deps = project.get("dependencies") if isinstance(project, dict) else None
        return [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []

    async def _change_spec(
        self, env_id: str, verb: str, args: list[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        if env_id.startswith("default:"):
            try:
                await self._seed_default()
            except tree_io.LinkRefusedError as exc:
                raise EnvError(str(exc)) from exc
        desc = self.describe(env_id, self._notebook(notebook_path).parent)
        if desc.kind == "script":
            raise ScriptInstallViaDocumentError(
                "the script environment's spec is the notebook's PEP 723 block; "
                "apply add_script_dependencies to the header as a document op"
            )
        if desc.kind not in ("default", "uv_project"):
            raise EnvNotMaterializableError(managed_elsewhere(desc))
        project = Path(desc.spec_root)
        log: list[str] = []
        async with self._lock(env_id):
            before = self._spec(project)
            try:
                await self._run(
                    [
                        self._uv,
                        verb,
                        "--no-sync",
                        "--project",
                        str(project),
                        "--python",
                        self._python.request,
                        *self._index_args,
                        *args,
                    ],
                    project,
                    log,
                    Path(desc.prefix),
                )
                after = self._spec(project)
                built = await self._materialize(env_id)
            except BaseException as exc:
                self._restore_spec(project, before)
                self._note_failure(env_id, verb, args, exc)
                if isinstance(exc, EnvBuildCancelledError):
                    raise
                if isinstance(exc, EnvError):
                    raise EnvBuildError(str(exc), log="\n".join(log) + "\n" + exc.log) from exc
                if isinstance(exc, tree_io.LinkRefusedError):
                    raise EnvBuildError(str(exc), log="\n".join(log)) from exc
                raise
        changed = sorted(
            (project / name).relative_to(self.root).as_posix()
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )
        record = self._detector.records.get(env_id)
        return built, changed, "\n".join(log) + "\n" + record.log

    def _note_failure(self, env_id: str, verb: str, args: list[str], exc: BaseException) -> None:
        """Say in the build record what was asked and that it did not go
        through, for the environment panel."""
        doing = "Installing" if verb == "add" else "Removing"
        ended = "was cancelled" if isinstance(exc, EnvBuildCancelledError) else "failed"
        record = self._detector.records.get(env_id)
        self._detector.records.put(
            env_id,
            dataclasses.replace(record, last_failure=f"{doing} {', '.join(args)} {ended}."),
        )

    def _restore_spec(self, project: Path, before: Mapping[str, bytes]) -> None:
        """The spec files byte for byte as they were before a change."""
        for name in ("pyproject.toml", "uv.lock"):
            path = project / name
            if name in before:
                self._tree.write_atomic(path, before[name])
            else:
                self._tree.unlink(path)

    def install_for_module(self, module: str, distribution: str | None = None) -> str:
        return distribution_for_module(module, distribution)

    async def packages(self, env_id: str) -> list[tuple[str, str]]:
        desc = self.describe(env_id)
        if not _exists(desc.interpreter):
            return []
        result = await self._runner.run(
            [self._uv, "pip", "list", "--format", "json", "--python", desc.interpreter],
            cwd=str(self.env_root if self.env_root.is_dir() else self.root),
            env=self._env(),
            timeout_s=60.0,
        )
        if result.returncode != 0:
            raise EnvError("listing packages failed", log=result.stderr)
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise EnvError("listing packages returned no JSON", log=result.stdout) from exc
        return sorted((str(r["name"]), str(r["version"])) for r in rows if isinstance(r, dict))

    def fingerprint(self, env: EnvDescriptor) -> str:
        return env_fingerprint(
            env.kind, self._fingerprint_bytes(env), env.python_version, sysconfig.get_platform()
        )

    def _fingerprint_bytes(self, env: EnvDescriptor) -> bytes:
        root = Path(env.spec_root)
        detector = self._detector
        if env.kind in ("default", "uv_project"):
            spec = detector.spec(root)
            return spec.get("uv.lock") or spec.get("pyproject.toml") or b""
        if env.kind == "script":
            text = (detector.read_file(root) or b"").decode("utf-8", "replace")
            return (script_block_text(text) or "").encode("utf-8")
        return detector.read_file(root / "pyvenv.cfg") or b""


def managed_elsewhere(desc: EnvDescriptor) -> str:
    """Why Alkera does not change this environment's packages, and what to
    use instead: the one reason an environment refuses an install."""
    where = desc.recorded or desc.spec_root
    if desc.kind == "venv":
        return (
            f"This notebook runs in the virtual environment at {where}, which Alkera does "
            "not manage. Switch the notebook to the workspace's default environment to "
            "install packages here."
        )
    return (
        f"This notebook names a {desc.kind} environment at {where}, which Alkera lists but "
        "does not build. Switch the notebook to the workspace's default environment to "
        "install packages here."
    )


def _managed(desc: EnvDescriptor) -> bool:
    """Whether the registry keeps this environment's builds as generations
    (outside the tree, on a platform with symbolic links)."""
    return desc.kind in ("default", "script") and sys.platform != "win32"


def _exists(path: str) -> bool:
    return bool(path) and Path(path).exists()
