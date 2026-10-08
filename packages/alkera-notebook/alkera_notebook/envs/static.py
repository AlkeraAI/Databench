"""An environment registry with one fixed interpreter.

For standalone use (``alkera-notebook run`` with ``--python``) and for tests
that do not exercise environments: every notebook resolves to the same
ready environment, nothing is materialized or installed.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from collections.abc import Sequence

from alkera_notebook.envs.models import EnvDescriptor
from alkera_notebook.envs.registry import EnvNotMaterializableError

#: Why a notebook run on an interpreter someone named takes no installs.
GIVEN_INTERPRETER = (
    "This notebook runs on the Python interpreter it was started with (--python), "
    "whose packages are installed outside Alkera. Run it in a workspace to use the "
    "workspace's default environment."
)


class StaticEnvRegistry:
    def __init__(self, interpreter: str | None = None, python_version: str | None = None) -> None:
        self.interpreter = interpreter or sys.executable
        self.python_version = python_version or platform.python_version()
        self.descriptor = EnvDescriptor(
            env_id="static",
            kind="venv",
            spec_root="",
            prefix=sys.prefix if interpreter is None else "",
            interpreter=self.interpreter,
            python_version=self.python_version,
            spec_hash="",
            built_spec_hash="",
            state="ready",
            recorded="",
        )

    async def detect(self, notebook_path: str) -> list[EnvDescriptor]:
        return [self.descriptor]

    async def resolve(self, notebook_path: str, recorded: str | None) -> EnvDescriptor:
        return self.descriptor

    async def materialize(self, env_id: str, *, by: str = "") -> EnvDescriptor:
        return self.descriptor

    async def install(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        raise EnvNotMaterializableError(GIVEN_INTERPRETER)

    async def remove(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[EnvDescriptor, list[str], str]:
        raise EnvNotMaterializableError(GIVEN_INTERPRETER)

    async def requirements(self, env_id: str) -> list[str]:
        return []

    async def cancel(self, env_id: str) -> bool:
        return False

    async def packages(self, env_id: str) -> list[tuple[str, str]]:
        return []

    def fingerprint(self, env: EnvDescriptor) -> str:
        raw = "\x00".join([env.kind, "", env.python_version, platform.platform()])
        return hashlib.sha256(raw.encode()).hexdigest()
