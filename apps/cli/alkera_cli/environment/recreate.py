"""Recreate: make an environment at a caller-given path match a spec.

Three stages, each testable on its own:

* :func:`read_target` probes the target (the environment if it exists, the
  workspace tree either way) into a :class:`TargetState`;
* :func:`plan_recreate` is pure: spec + target state + the tools at hand in,
  the commands that would close the gap (and what cannot be closed) out. A
  target that already satisfies the spec plans no command, which is what
  makes a re-run a no-op;
* :func:`recreate` runs the plan through a :class:`CommandRunner` under a
  deadline, reads the target again, and reports what is still missing.

uv does the pip side when it is present (``uv venv``, ``uv sync`` for a uv
project whose lock is unchanged, ``uv pip install``), with ``python -m venv``
and pip as the fallback; micromamba does the conda side. Nothing is removed
from an existing environment and an environment on another Python is never
replaced; both are reported instead.

A recreate nobody approved (the restore a chat's wake runs on its own) plans
with ``pinned_inputs``: a step that would run something read from the live
tree (a ``requirements.txt``, a project's build hooks) is planned only when
that file is the one the spec recorded, so whoever else can write the tree
cannot change what runs in the chat's sandbox at its wake.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, Field

from alkera_cli.environment.capture import Roots, file_url_path
from alkera_cli.environment.probe import ProbeError, ProbeResult, run_probe
from alkera_cli.environment.redact import redact_url
from alkera_cli.environment.runner import Command, CommandRunner
from alkera_cli.environment.spec import (
    KNOWN_PACKAGE_SOURCES,
    CondaSpec,
    EnvironmentSpec,
    PackageSpec,
    is_safe_relpath,
    normalize_name,
)

#: Network bounds every install command runs under: a stalled index answers
#: within a minute or the command fails, never hangs a turn.
NETWORK_ENV = {
    "UV_HTTP_TIMEOUT": "60",
    "PIP_DEFAULT_TIMEOUT": "60",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
}
#: How long one install step and the whole recreate may take by default.
STEP_TIMEOUT_S = 900.0
DEADLINE_S = 2700.0
#: The ``.pth`` file a recreate writes for the workspace folders on ``sys.path``.
PTH_NAME = "alkera-environment.pth"
#: Writes ``argv[1]`` as the ``.pth`` file in the interpreter's own site-packages.
_WRITE_PTH = (
    "import os, sys, sysconfig; "
    "p = os.path.join(sysconfig.get_paths()['purelib'], sys.argv[2]); "
    "open(p, 'w', encoding='utf-8').write(sys.argv[1])"
)
#: The conda platform name for a probe's (sys.platform, machine).
_CONDA_SUBDIRS = {
    ("linux", "x86_64"): "linux-64",
    ("linux", "aarch64"): "linux-aarch64",
    ("linux", "arm64"): "linux-aarch64",
    ("darwin", "arm64"): "osx-arm64",
    ("darwin", "x86_64"): "osx-64",
    ("win32", "amd64"): "win-64",
}

StepPurpose = Literal[
    "create_env",
    "conda_install",
    "uv_sync",
    "install_requirements",
    "install_packages",
    "install_local",
    "path_entries",
]
GapKind = Literal[
    "python_mismatch",
    "target_not_conda",
    "conda_unavailable",
    "no_installer",
    "local_source_missing",
    "not_installed",
    "deadline",
    "step_failed",
    "unknown_source",
    "unpinned_input",
]
#: The steps that build in the workspace tree (an editable's build hooks write
#: there, ``uv sync`` builds the project): two of them in one tree at once
#: write over each other, so they run under the tree's lock.
TREE_STEPS: frozenset[StepPurpose] = frozenset({"uv_sync", "install_requirements", "install_local"})
#: The files a project's build reads at its root: a step that builds one runs
#: what these say.
BUILD_FILES = ("pyproject.toml", "setup.py", "setup.cfg")
TreeLock = Callable[[], AbstractAsyncContextManager[object]]


@dataclass(frozen=True, slots=True)
class Tools:
    uv: str | None = None
    micromamba: str | None = None
    python: str | None = None
    """An interpreter to make a plain venv from when uv is missing."""


@dataclass(frozen=True, slots=True)
class Installed:
    version: str
    editable: str | None = None
    """The editable source, relative to the target root (``None`` when not
    editable or outside it)."""
    commit: str = ""
    url: str = ""


@dataclass
class TargetState:
    exists: bool
    python_version: str = ""
    implementation: str = ""
    conda: dict[str, tuple[str, str]] | None = None
    """Conda packages by name: (version, build). ``None``: not a conda prefix."""
    installed: dict[str, Installed] = field(default_factory=dict)
    path_entries: set[str] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)
    """The target tree's environment files: path -> sha256."""
    dirs: dict[str, bool] = field(default_factory=dict)
    sys_platform: str = ""
    machine: str = ""
    host_python: str = ""
    """When the target does not exist: the interpreter the probe ran on (the
    active environment's in a sandbox), which a new venv is made from when its
    version fits, so no interpreter is looked for or downloaded."""
    host_python_version: str = ""

    @property
    def windows(self) -> bool:
        return self.sys_platform == "win32"


@dataclass(frozen=True, slots=True)
class Step:
    purpose: StepPurpose
    command: Command
    label: str = ""


class Gap(BaseModel):
    """Something the recreate cannot (or did not) make match the spec."""

    kind: GapKind
    name: str = ""
    detail: str = ""


@dataclass
class RecreatePlan:
    steps: list[Step] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    satisfied: int = 0


class StepReport(BaseModel):
    purpose: StepPurpose
    label: str = ""
    command: str
    inputs: dict[str, str] = Field(default_factory=dict)
    """The files the command reads, by the name it gives them (``<requirements>``
    in ``command``): what a person approving the step needs to see."""
    ran: bool = False
    exit_code: int | None = None
    output_tail: str = ""


class RecreateReport(BaseModel):
    target_env: str
    dry_run: bool
    already_satisfied: bool
    steps: list[StepReport] = Field(default_factory=list)
    not_recreated: list[Gap] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def env_python(env: str, *, windows: bool = False, conda: bool = False) -> str:
    """An environment's interpreter: ``bin/python`` on POSIX; on Windows
    ``Scripts\\python.exe`` for a venv and ``python.exe`` at the prefix root
    for a conda environment."""
    if windows:
        base = PureWindowsPath(env)
        return str(base / "python.exe" if conda else base / "Scripts" / "python.exe")
    return str(PurePosixPath(env) / "bin" / "python")


#: The environment a local workspace uses unless told otherwise (uv's default).
LOCAL_ENV_DIR = ".venv"


def local_python(root: Path, env: str | Path | None = None, *, windows: bool = False) -> str | None:
    """The interpreter of a local workspace's environment: ``env`` when named,
    else the workspace's ``.venv``; ``None`` when that is not an environment
    (a capture then reads the project files alone)."""
    target = Path(env) if env else root / LOCAL_ENV_DIR
    conda = (target / "conda-meta").is_dir()
    if not (target / "pyvenv.cfg").is_file() and not conda:
        return None
    return env_python(str(target), windows=windows, conda=conda)


def host_python() -> str | None:
    """An interpreter on this machine's ``PATH`` to make a new venv from, for a
    caller running locally (the running process may be a compiled binary,
    never a Python)."""
    return shutil.which("python3") or shutil.which("python")


def _join(root: str, rel: str, *, windows: bool) -> str:
    # The spec's validation already refused any other path; a planner bug
    # must not turn one into a command.
    if not is_safe_relpath(rel):
        raise ValueError(f"refusing an unsafe workspace path: {rel!r}")
    base: PurePath = PureWindowsPath(root) if windows else PurePosixPath(root)
    return str(base if rel in ("", ".") else base.joinpath(*rel.split("/")))


def _installed(probe: ProbeResult, roots: Roots) -> dict[str, Installed]:
    out: dict[str, Installed] = {}
    env = probe.env
    if env is None:
        return out
    windows = probe.host.sys_platform == "win32"
    for dist in env.distributions:
        info = dist.direct_url or {}
        url = str(info.get("url", ""))
        editable = None
        dir_info = info.get("dir_info")
        local = file_url_path(url, windows=windows) if url else None
        if local is not None and isinstance(dir_info, dict) and dir_info.get("editable"):
            editable = roots.relative(local)
        vcs = info.get("vcs_info")
        commit = str(vcs.get("commit_id", "")) if isinstance(vcs, dict) else ""
        out[normalize_name(dist.name)] = Installed(
            version=dist.version,
            editable=editable,
            commit=commit,
            url=redact_url(url)[0] if url and local is None else "",
        )
    return out


@dataclass(frozen=True, slots=True)
class TargetRead:
    """One probe of the target: of its environment when it exists, else of the
    tree, run by a fallback interpreter (whose ``env`` then describes that
    interpreter, not the target)."""

    probe: ProbeResult
    exists: bool


def target_state(read: TargetRead, *, aliases: Sequence[str] = ()) -> TargetState:
    """What the target holds."""
    probe = read.probe
    windows = probe.host.sys_platform == "win32"
    roots = Roots([probe.root, *aliases], windows=windows)
    state = TargetState(
        exists=read.exists,
        files={rel: f.sha256 for rel, f in probe.files.items()},
        dirs=dict(probe.paths),
        sys_platform=probe.host.sys_platform,
        machine=probe.host.machine,
    )
    env = probe.env
    if env is None:
        return state
    if not read.exists:
        state.host_python = env.python.executable
        state.host_python_version = env.python.version
        return state
    state.python_version = env.python.version
    state.implementation = env.python.implementation
    if env.conda is not None:
        state.conda = {p.name: (p.version, p.build) for p in env.conda.packages if p.name}
    state.installed = _installed(probe, roots)
    for entry in env.path_entries:
        rel = roots.relative(entry.path)
        if rel is not None:
            state.path_entries.add(rel)
    return state


def is_satisfied(pkg: PackageSpec, installed: dict[str, Installed]) -> bool:
    have = installed.get(normalize_name(pkg.name))
    if have is None:
        return False
    if pkg.source == "editable":
        return have.editable == pkg.path
    if pkg.source == "vcs" and pkg.vcs is not None and pkg.vcs.commit:
        return have.commit == pkg.vcs.commit
    if pkg.source == "url" and have.url and have.url == pkg.url:
        return True
    return not pkg.version or have.version == pkg.version


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _index_lines(spec: EnvironmentSpec) -> list[str]:
    flags = {
        "index": "--index-url",
        "extra_index": "--extra-index-url",
        "find_links": "--find-links",
    }
    return [f"{flags[i.kind]} {i.url}" for i in spec.indexes if i.kind in flags]


def _requirement(pkg: PackageSpec, *, hashes: bool) -> str:
    if pkg.source == "vcs" and pkg.vcs is not None:
        ref = f"@{pkg.vcs.commit}" if pkg.vcs.commit else ""
        url = pkg.vcs.url if "+" in pkg.vcs.url.split(":", 1)[0] else f"{pkg.vcs.vcs}+{pkg.vcs.url}"
        sub = f"#subdirectory={pkg.subdirectory}" if pkg.subdirectory else ""
        return f"{pkg.name} @ {url}{ref}{sub}"
    if pkg.source == "url":
        return f"{pkg.name} @ {pkg.url}"
    line = f"{pkg.name}=={pkg.version}" if pkg.version else pkg.name
    if hashes:
        line += "".join(f" --hash={h}" for h in pkg.hashes)
    return line


@dataclass(frozen=True, slots=True)
class _Ctx:
    spec: EnvironmentSpec
    state: TargetState
    env: str
    root: str
    tools: Tools
    timeout_s: float
    pinned: bool = False

    @property
    def python(self) -> str:
        return env_python(self.env, windows=self.state.windows, conda=self.spec.env_kind == "conda")

    def pip(self, *args: str) -> tuple[str, ...]:
        """An install into the target, by uv when present, else by its pip."""
        if self.tools.uv:
            return (self.tools.uv, "pip", "install", "--python", self.python, *args)
        return (self.python, "-m", "pip", "install", *args)

    def command(
        self,
        argv: tuple[str, ...],
        files: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> Command:
        # In the target tree, so uv reads the workspace's own settings and
        # never those of wherever the caller happens to be.
        return Command(
            argv=argv,
            files=files or {},
            env={**NETWORK_ENV, **(env or {})},
            cwd=self.root,
            timeout_s=self.timeout_s,
        )


def _create_conda(ctx: _Ctx, plan: RecreatePlan) -> bool:
    conda = ctx.spec.conda
    if conda is None or not (conda.packages or conda.specs):
        return _create_venv(ctx, plan)
    if ctx.tools.micromamba:
        plan.steps.append(Step("create_env", _conda_create(ctx, conda), "conda environment"))
        return True
    plan.gaps.append(
        Gap(kind="conda_unavailable", detail="micromamba is not installed; made a venv instead")
    )
    return _create_venv(ctx, plan)


def _create_venv(ctx: _Ctx, plan: RecreatePlan) -> bool:
    spec, tools = ctx.spec, ctx.tools
    version = spec.python.version if spec.python else ""
    if tools.uv:
        # The host interpreter when its minor fits (nothing to look for or
        # download), else the minor version: any 3.12 runs what a 3.12.4 ran.
        argv: tuple[str, ...] = (tools.uv, "venv", "--quiet", ctx.env)
        host = ctx.state.host_python
        if version and host and _minor(ctx.state.host_python_version) == _minor(version):
            argv = (tools.uv, "venv", "--quiet", "--python", host, ctx.env)
        elif version:
            argv = (tools.uv, "venv", "--quiet", "--python", _minor(version), ctx.env)
        plan.steps.append(
            Step("create_env", ctx.command(argv), f"venv on Python {_minor(version) or 'default'}")
        )
        return True
    if tools.python:
        plan.steps.append(
            Step("create_env", ctx.command((tools.python, "-m", "venv", ctx.env)), "venv")
        )
        if version:
            plan.notes.append(
                f"uv is not installed, so the venv uses {tools.python}, not Python {version}"
            )
        return True
    plan.gaps.append(
        Gap(kind="no_installer", detail="neither uv, micromamba nor a python3 was found")
    )
    return False


#: How a missing environment is made, by the kind of environment the spec
#: holds; any other kind (a newer writer's) gets a venv.
ENV_CREATORS: dict[str, Callable[[_Ctx, RecreatePlan], bool]] = {
    "conda": _create_conda,
    "venv": _create_venv,
}


def _plan_create(ctx: _Ctx, plan: RecreatePlan) -> bool:
    """The step that makes a missing environment; False when it cannot be made."""
    kind = "conda" if ctx.spec.conda is not None else ctx.spec.env_kind
    return ENV_CREATORS.get(kind, _create_venv)(ctx, plan)


def _conda_subdir(state: TargetState) -> str:
    return _CONDA_SUBDIRS.get((state.sys_platform, state.machine.lower()), "")


def _conda_create(ctx: _Ctx, conda: CondaSpec) -> Command:
    mamba = ctx.tools.micromamba or "micromamba"
    explicit = bool(conda.packages) and all(p.url for p in conda.packages)
    # The explicit list pins builds for one platform; a list of noarch
    # packages only is every platform's.
    same_platform = conda.subdir == _conda_subdir(ctx.state) if conda.subdir else True
    if explicit and same_platform:
        lines = ["@EXPLICIT", *(f"{p.url}#{p.md5}" if p.md5 else p.url for p in conda.packages)]
        return ctx.command(
            (mamba, "create", "--yes", "--prefix", ctx.env, "--file", "{explicit}"),
            files={"explicit": "\n".join(lines) + "\n"},
        )
    specs = list(conda.specs) or [f"{p.name}={p.version}" for p in conda.packages]
    version = ctx.spec.python.version if ctx.spec.python else ""
    if version and not any(
        normalize_name(s.split("=")[0].split("::")[-1]) == "python" for s in specs
    ):
        specs.append(f"python={version}")
    channels: list[str] = []
    for channel in conda.channels or ["conda-forge"]:
        channels.extend(("--channel", channel))
    argv = (mamba, "create", "--yes", "--prefix", ctx.env, "--override-channels", *channels, *specs)
    return ctx.command(argv)


def _plan_conda_missing(ctx: _Ctx, plan: RecreatePlan) -> None:
    conda, have = ctx.spec.conda, ctx.state.conda
    if conda is None or have is None:
        return
    same = conda.subdir == _conda_subdir(ctx.state)
    missing = [
        (f"{p.name}={p.version}={p.build}" if same and p.build else f"{p.name}={p.version}")
        for p in conda.packages
        if have.get(p.name, ("", ""))[0] != p.version
    ]
    plan.satisfied += len(conda.packages) - len(missing)
    if not missing:
        return
    if not ctx.tools.micromamba:
        plan.gaps.extend(Gap(kind="conda_unavailable", name=m) for m in missing)
        return
    channels: list[str] = []
    for channel in conda.channels or ["conda-forge"]:
        channels.extend(("--channel", channel))
    argv = (ctx.tools.micromamba, "install", "--yes", "--prefix", ctx.env, *channels, *missing)
    plan.steps.append(
        Step("conda_install", ctx.command(argv), _count(len(missing), "conda package"))
    )


def _plan_uv_sync(ctx: _Ctx, plan: RecreatePlan, *, needed: bool) -> None:
    spec, state = ctx.spec, ctx.state
    lock = spec.project_file("uv_lock")
    if lock is None or spec.project_file("pyproject") is None or not ctx.tools.uv:
        return
    if state.files.get("uv.lock") != lock.sha256:
        plan.notes.append("uv.lock differs from the captured one; installed the captured versions")
        return
    if not needed:
        return
    if (changed := _changed_build_file(ctx, ".")) is not None:
        plan.gaps.append(Gap(kind="unpinned_input", name="uv.lock", detail=changed))
        return
    argv = (
        ctx.tools.uv,
        "sync",
        "--frozen",
        "--inexact",
        "--project",
        ctx.root,
        "--python",
        ctx.python,
    )
    plan.steps.append(
        Step("uv_sync", ctx.command(argv, env={"UV_PROJECT_ENVIRONMENT": ctx.env}), "uv.lock")
    )


def _plan_requirements_file(ctx: _Ctx, plan: RecreatePlan) -> None:
    """A capture of the project files alone: install its ``requirements.txt``."""
    req = next((f for f in ctx.spec.project_files if f.path == "requirements.txt"), None)
    if req is None or "requirements.txt" not in ctx.state.files:
        return
    if ctx.pinned and ctx.state.files["requirements.txt"] != req.sha256:
        plan.gaps.append(
            Gap(
                kind="unpinned_input",
                name="requirements.txt",
                detail="requirements.txt changed since the capture",
            )
        )
        return
    path = _join(ctx.root, "requirements.txt", windows=ctx.state.windows)
    plan.steps.append(
        Step("install_requirements", ctx.command(ctx.pip("-r", path)), "requirements.txt")
    )


def _plan_packages(ctx: _Ctx, plan: RecreatePlan, missing: list[PackageSpec]) -> None:
    remote = [p for p in missing if p.source in ("index", "vcs", "url")]
    if not remote:
        return
    same_platform = bool(
        ctx.spec.platform
        and ctx.spec.platform.sys_platform == ctx.state.sys_platform
        and ctx.spec.platform.machine.lower() == ctx.state.machine.lower()
    )
    hashed = same_platform and all(p.hashes and p.source != "vcs" for p in remote)
    head = _index_lines(ctx.spec)
    files: dict[str, str] = {}
    args: list[str] = ["-r", "{requirements}"]
    if hashed:
        chosen = remote
        args.extend(("--require-hashes", "--no-deps"))
    elif same_platform:
        chosen = remote
    else:
        # Another platform: install what was asked for and let the resolver
        # pick the platform's dependencies, held to the captured versions.
        chosen = [p for p in remote if p.requested] or remote
        pins = [
            f"{p.name}=={p.version}" for p in ctx.spec.packages if p.source == "index" and p.version
        ]
        if pins:
            files["constraints"] = "\n".join(pins) + "\n"
            args.extend(("-c", "{constraints}"))
    files["requirements"] = (
        "\n".join([*head, *(_requirement(p, hashes=hashed) for p in chosen)]) + "\n"
    )
    label = _count(len(chosen), "package") + (" (hash-checked)" if hashed else "")
    plan.steps.append(Step("install_packages", ctx.command(ctx.pip(*args), files=files), label))


def _changed_build_file(ctx: _Ctx, rel: str) -> str | None:
    """Under ``pinned_inputs``, why building the workspace folder ``rel`` would
    run something the spec does not pin, or ``None`` when it would not (or
    nothing is pinned). Only the workspace root's build files are recorded in
    a spec, so a folder below it is never pinned."""
    if not ctx.pinned:
        return None
    if rel not in ("", "."):
        return f"the build files of {rel} are not recorded in the spec"
    recorded = {f.path: f.sha256 for f in ctx.spec.project_files}
    for name in BUILD_FILES:
        if ctx.state.files.get(name) != recorded.get(name):
            return f"{name} changed since the capture"
    return None


def _plan_local(ctx: _Ctx, plan: RecreatePlan, missing: list[PackageSpec]) -> None:
    lines: list[str] = []
    for pkg in missing:
        if pkg.source not in ("editable", "path"):
            continue
        rel = pkg.path if not pkg.subdirectory else f"{pkg.path}/{pkg.subdirectory}"
        if not ctx.state.dirs.get(pkg.path, False):
            plan.gaps.append(Gap(kind="local_source_missing", name=pkg.name, detail=pkg.path))
            continue
        if (changed := _changed_build_file(ctx, rel)) is not None:
            plan.gaps.append(Gap(kind="unpinned_input", name=pkg.name, detail=changed))
            continue
        target = _join(ctx.root, rel, windows=ctx.state.windows)
        lines.append(f"-e {target}" if pkg.source == "editable" else target)
    if not lines:
        return
    content = "\n".join([*_index_lines(ctx.spec), *lines]) + "\n"
    argv = ctx.pip("--no-deps", "-r", "{local}")
    plan.steps.append(
        Step(
            "install_local",
            ctx.command(argv, files={"local": content}),
            _count(len(lines), "package") + " from the workspace",
        )
    )


def _plan_path_entries(ctx: _Ctx, plan: RecreatePlan) -> None:
    wanted = ctx.spec.path_entries
    if not wanted or all(p in ctx.state.path_entries for p in wanted):
        return
    present = [p for p in wanted if ctx.state.dirs.get(p, False)]
    plan.gaps.extend(
        Gap(kind="local_source_missing", name="sys.path", detail=p)
        for p in wanted
        if p not in present
    )
    if not present:
        return
    content = "\n".join(_join(ctx.root, p, windows=ctx.state.windows) for p in present) + "\n"
    argv = (ctx.python, "-I", "-c", _WRITE_PTH, content, PTH_NAME)
    plan.steps.append(
        Step("path_entries", ctx.command(argv), _count(len(present), "folder") + " on sys.path")
    )


def missing_packages(spec: EnvironmentSpec, state: TargetState) -> list[PackageSpec]:
    """The packages of a source this reader knows that the target lacks."""
    return [
        p
        for p in spec.packages
        if p.source in KNOWN_PACKAGE_SOURCES and not is_satisfied(p, state.installed)
    ]


def plan_recreate(
    spec: EnvironmentSpec,
    state: TargetState,
    *,
    target_env: str,
    target_root: str,
    tools: Tools,
    step_timeout_s: float = STEP_TIMEOUT_S,
    pinned_inputs: bool = False,
) -> RecreatePlan:
    """The commands that make ``target_env`` match ``spec``, in order. With
    ``pinned_inputs`` a step that would run a tree file the spec does not pin
    is left out and reported (see the module docstring)."""
    plan = RecreatePlan()
    ctx = _Ctx(spec, state, target_env, target_root, tools, step_timeout_s, pinned_inputs)
    wanted = spec.python.version if spec.python else ""
    if state.exists and wanted and _minor(state.python_version) != _minor(wanted):
        plan.gaps.append(
            Gap(
                kind="python_mismatch",
                detail=f"{target_env} runs Python {state.python_version}; the spec needs {wanted}",
            )
        )
        return plan
    impl = spec.python.implementation if spec.python else ""
    if state.exists and impl and state.implementation and impl != state.implementation:
        plan.gaps.append(
            Gap(
                kind="python_mismatch", detail=f"{target_env} is {state.implementation}, not {impl}"
            )
        )
        return plan
    if state.exists and spec.env_kind == "conda" and state.conda is None:
        plan.gaps.append(Gap(kind="target_not_conda", detail=target_env))
        return plan
    if not state.exists:
        if not _plan_create(ctx, plan):
            return plan
    else:
        _plan_conda_missing(ctx, plan)
    missing = missing_packages(spec, state)
    unknown = [p for p in spec.packages if p.source not in KNOWN_PACKAGE_SOURCES]
    plan.gaps.extend(Gap(kind="unknown_source", name=p.name, detail=p.source) for p in unknown)
    plan.satisfied += len(spec.packages) - len(missing) - len(unknown)
    _plan_uv_sync(ctx, plan, needed=bool(missing) or not spec.packages)
    if not spec.packages:
        if not any(s.purpose == "uv_sync" for s in plan.steps):
            _plan_requirements_file(ctx, plan)
    _plan_packages(ctx, plan, missing)
    _plan_local(ctx, plan, missing)
    _plan_path_entries(ctx, plan)
    return plan


def local_paths(spec: EnvironmentSpec) -> tuple[str, ...]:
    """The workspace folders a recreate needs to find in the target tree."""
    rels = {p.path for p in spec.packages if p.source in ("editable", "path")}
    rels.update(spec.path_entries)
    return tuple(sorted(rels))


async def read_target(
    runner: CommandRunner,
    *,
    target_env: str,
    target_root: str,
    fallback_python: str | None,
    windows: bool = False,
    paths: tuple[str, ...] = (),
    conda: bool = False,
) -> TargetRead:
    """Probe the target environment, or, when it does not exist (its
    interpreter cannot be started), the tree with ``fallback_python``."""
    python = env_python(target_env, windows=windows, conda=conda)
    try:
        probe = await run_probe(runner, python=python, root=target_root, paths=paths)
        return TargetRead(probe=probe, exists=True)
    except ProbeError:
        result = await runner.run(Command(argv=(python, "-c", "pass"), timeout_s=30))
        if result.ok:
            raise
    probe = await run_probe(
        runner,
        python=fallback_python,
        root=target_root,
        inspect_env=fallback_python is not None,
        paths=paths,
    )
    return TargetRead(probe=probe, exists=False)


@dataclass
class PreparedRecreate:
    """A plan made against a probe of the target, and the report it starts.
    What a person approves is this plan's steps, and :func:`execute_recreate`
    runs exactly these, never a plan made again after the approval."""

    spec: EnvironmentSpec
    plan: RecreatePlan
    report: RecreateReport
    target_env: str
    target_root: str
    aliases: tuple[str, ...]
    fallback_python: str | None
    windows: bool
    paths: tuple[str, ...]


async def prepare_recreate(
    spec: EnvironmentSpec,
    runner: CommandRunner,
    *,
    target_env: str,
    target_root: str,
    aliases: Sequence[str] = (),
    dry_run: bool = False,
    fallback_python: str | None = None,
    windows: bool = False,
    step_timeout_s: float = STEP_TIMEOUT_S,
    pinned_inputs: bool = False,
) -> PreparedRecreate:
    """Probe the target and plan what would make it match ``spec``."""
    paths = local_paths(spec)
    tools = Tools(
        uv=await runner.which("uv"),
        micromamba=await runner.which("micromamba"),
        python=fallback_python,
    )
    read = await read_target(
        runner,
        target_env=target_env,
        target_root=target_root,
        fallback_python=fallback_python,
        windows=windows,
        paths=paths,
        conda=spec.env_kind == "conda",
    )
    state = target_state(read, aliases=aliases)
    plan = plan_recreate(
        spec,
        state,
        target_env=target_env,
        target_root=target_root,
        tools=tools,
        step_timeout_s=step_timeout_s,
        pinned_inputs=pinned_inputs,
    )
    report = RecreateReport(
        target_env=target_env,
        dry_run=dry_run,
        already_satisfied=not plan.steps and not plan.gaps,
        steps=[
            StepReport(
                purpose=s.purpose,
                label=s.label,
                command=s.command.display(),
                inputs=dict(s.command.files),
            )
            for s in plan.steps
        ],
        not_recreated=list(plan.gaps),
        notes=list(plan.notes),
    )
    return PreparedRecreate(
        spec=spec,
        plan=plan,
        report=report,
        target_env=target_env,
        target_root=target_root,
        aliases=tuple(aliases),
        fallback_python=fallback_python,
        windows=windows,
        paths=paths,
    )


async def execute_recreate(
    prepared: PreparedRecreate,
    runner: CommandRunner,
    *,
    deadline_s: float = DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
    tree_lock: TreeLock | None = None,
) -> RecreateReport:
    """Run a prepared plan under a deadline, then probe the target again and
    report what is still missing. A step that builds in the workspace tree
    (:data:`TREE_STEPS`) runs holding ``tree_lock`` when one is given."""
    plan = prepared.plan
    report = prepared.report.model_copy(deep=True, update={"dry_run": False})
    if not plan.steps:
        return report
    deadline = clock() + deadline_s
    for step, shown in zip(plan.steps, report.steps, strict=True):
        held: AbstractAsyncContextManager[object] = (
            tree_lock()
            if tree_lock is not None and step.purpose in TREE_STEPS
            else contextlib.nullcontext()
        )
        async with held:
            left = deadline - clock()
            if left <= 0:
                report.not_recreated.append(Gap(kind="deadline", name=step.label or step.purpose))
                continue
            command = Command(
                argv=step.command.argv,
                files=step.command.files,
                env=step.command.env,
                cwd=step.command.cwd,
                timeout_s=min(step.command.timeout_s, left),
            )
            result = await runner.run(command)
        shown.ran = True
        shown.exit_code = result.exit_code
        shown.output_tail = result.output.strip()[-4000:]
        if not result.ok:
            report.not_recreated.append(
                Gap(
                    kind="step_failed",
                    name=step.label or step.purpose,
                    detail=shown.output_tail[-500:],
                )
            )
            if step.purpose == "create_env":
                return report
    read = await read_target(
        runner,
        target_env=prepared.target_env,
        target_root=prepared.target_root,
        fallback_python=prepared.fallback_python,
        windows=prepared.windows,
        paths=prepared.paths,
        conda=prepared.spec.env_kind == "conda",
    )
    after = target_state(read, aliases=prepared.aliases)
    report.not_recreated.extend(
        Gap(kind="not_installed", name=p.name, detail=p.version)
        for p in missing_packages(prepared.spec, after)
    )
    return report


async def recreate(
    spec: EnvironmentSpec,
    runner: CommandRunner,
    *,
    target_env: str,
    target_root: str,
    aliases: Sequence[str] = (),
    dry_run: bool = False,
    fallback_python: str | None = None,
    windows: bool = False,
    step_timeout_s: float = STEP_TIMEOUT_S,
    deadline_s: float = DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
    pinned_inputs: bool = False,
    tree_lock: TreeLock | None = None,
) -> RecreateReport:
    """Make ``target_env`` match ``spec`` (or, with ``dry_run``, say how)."""
    prepared = await prepare_recreate(
        spec,
        runner,
        target_env=target_env,
        target_root=target_root,
        aliases=aliases,
        dry_run=dry_run,
        fallback_python=fallback_python,
        windows=windows,
        step_timeout_s=step_timeout_s,
        pinned_inputs=pinned_inputs,
    )
    if dry_run:
        return prepared.report
    return await execute_recreate(
        prepared, runner, deadline_s=deadline_s, clock=clock, tree_lock=tree_lock
    )


__all__ = [
    "DEADLINE_S",
    "ENV_CREATORS",
    "LOCAL_ENV_DIR",
    "NETWORK_ENV",
    "STEP_TIMEOUT_S",
    "TREE_STEPS",
    "Gap",
    "PreparedRecreate",
    "RecreatePlan",
    "RecreateReport",
    "Step",
    "StepReport",
    "TargetRead",
    "TargetState",
    "Tools",
    "TreeLock",
    "env_python",
    "execute_recreate",
    "host_python",
    "is_satisfied",
    "local_paths",
    "local_python",
    "missing_packages",
    "plan_recreate",
    "prepare_recreate",
    "read_target",
    "recreate",
    "target_state",
]
