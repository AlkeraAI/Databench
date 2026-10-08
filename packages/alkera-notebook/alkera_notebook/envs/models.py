"""Environment descriptors and the seams environments are built through."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

EnvKind = Literal["default", "uv_project", "script", "venv", "requirements", "conda"]
EnvState = Literal["ready", "stale", "missing", "building", "failed"]

# Kinds v1 can materialize (``venv`` is used as is; the rest are listed only).
MATERIALIZABLE: frozenset[str] = frozenset({"default", "uv_project", "script"})


@dataclass(frozen=True)
class EnvDescriptor:
    env_id: str
    kind: EnvKind
    spec_root: str
    prefix: str
    interpreter: str
    python_version: str
    spec_hash: str
    built_spec_hash: str | None
    state: EnvState
    # The value a notebook header records to select this environment
    # (``default``, ``script`` or a relative path).
    recorded: str = ""
    # The interpreter build the last build got: ``gil`` or ``freethreaded``
    # (empty when nothing was built here, as for a ``venv`` used as is).
    python_build: str = ""
    #: Why the last build attempt failed, while an older build is still the
    #: one in use (empty otherwise).
    last_failure: str = ""


AdmittedEnvAction = Literal["build", "install", "remove", "cancel"]
#: The kinds whose package list Alkera owns: the default environment's
#: ``pyproject.toml``, a uv project's, and the notebook's PEP 723 block. A
#: ``venv``, ``requirements`` or ``conda`` environment is managed outside.
PACKAGE_MANAGED: frozenset[str] = frozenset({"default", "uv_project", "script"})


def env_actions(desc: EnvDescriptor) -> list[AdmittedEnvAction]:
    """What ``desc`` admits now, whoever asks: the one decision a panel's
    controls and the engine's refusals both follow."""
    if desc.state == "building":
        return ["cancel"]
    actions: list[AdmittedEnvAction] = []
    if desc.kind in MATERIALIZABLE and desc.state in ("missing", "stale", "failed"):
        actions.append("build")
    if desc.kind in PACKAGE_MANAGED:
        actions += ["install", "remove"]
    return actions


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner(Protocol):
    """Runs a build command. The core runs local subprocesses; the platform
    runs them as the dedicated build uid inside the sandbox."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult: ...


class EnvRegistry(Protocol):
    async def detect(self, notebook_path: str) -> list[EnvDescriptor]: ...

    async def resolve(self, notebook_path: str, recorded: str | None) -> EnvDescriptor: ...

    async def materialize(self, env_id: str, *, by: str = "") -> EnvDescriptor: ...

    async def install(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]: ...

    async def remove(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]: ...

    async def requirements(self, env_id: str) -> list[str]: ...

    async def cancel(self, env_id: str) -> bool: ...

    async def packages(self, env_id: str) -> list[tuple[str, str]]: ...

    def fingerprint(self, env: EnvDescriptor) -> str: ...
