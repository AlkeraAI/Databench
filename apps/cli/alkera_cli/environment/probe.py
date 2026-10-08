"""Running the environment probe and reading what it printed.

The probe (``_probe/env_probe.py``) is a stdlib-only script run by the
interpreter of the environment being read, wherever commands run (this
machine, or a chat's sandbox as the chat's user). Its output is untrusted
data: it is parsed here into typed models and nothing in it is executed.

With no interpreter to run it (a capture of the project files only, on this
machine) the same script runs in this process.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from alkera_cli.environment.runner import Command, CommandRunner

#: The script, shipped verbatim beside this module: the packaged binary carries
#: the folder as a raw directory (the binary build), since a
#: compiled module has no source to hand another interpreter.
PROBE_PATH = Path(__file__).resolve().parent / "_probe" / "env_probe.py"
#: How long reading an environment may take.
PROBE_TIMEOUT_S = 120.0
MARKER = "ALKERA-ENV-PROBE:"


class ProbeError(RuntimeError):
    """The probe did not run, or printed nothing the capture can read."""


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProbeFile(_Data):
    size: int = 0
    sha256: str = ""
    text: str | None = None


class ProbePython(_Data):
    version: str = ""
    implementation: str = ""
    executable: str = ""
    prefix: str = ""
    base_prefix: str = ""


class ProbeDistribution(_Data):
    name: str
    version: str = ""
    installer: str = ""
    requested: bool = False
    requires: list[str] = Field(default_factory=list)
    direct_url: dict[str, Any] | None = None
    site: str = ""


class ProbePathEntry(_Data):
    kind: str = "pth"
    file: str = ""
    path: str


class ProbeCondaPackage(_Data):
    name: str = ""
    version: str = ""
    build: str = ""
    channel: str = ""
    url: str = ""
    md5: str = ""
    subdir: str = ""


class ProbeConda(_Data):
    packages: list[ProbeCondaPackage] = Field(default_factory=list)
    history: str = ""


class ProbeEnv(_Data):
    python: ProbePython = Field(default_factory=ProbePython)
    pyvenv_cfg: dict[str, str] | None = None
    conda: ProbeConda | None = None
    sites: list[str] = Field(default_factory=list)
    distributions: list[ProbeDistribution] = Field(default_factory=list)
    path_entries: list[ProbePathEntry] = Field(default_factory=list)
    pip_conf: str | None = None


class ProbeHost(_Data):
    sys_platform: str = ""
    machine: str = ""
    platform_tag: str = ""


class ProbeResult(_Data):
    probe_version: int = 1
    root: str = ""
    host: ProbeHost = Field(default_factory=ProbeHost)
    files: dict[str, ProbeFile] = Field(default_factory=dict)
    paths: dict[str, bool] = Field(default_factory=dict)
    env_vars: dict[str, str] = Field(default_factory=dict)
    env: ProbeEnv | None = None


def probe_source() -> str:
    return PROBE_PATH.read_text(encoding="utf-8")


def probe_command(
    python: str, root: str, *, inspect_env: bool = True, paths: tuple[str, ...] = ()
) -> Command:
    """The probe run by ``python`` (``-I``: no user site, no ``PYTHON*``
    variables, no working directory on ``sys.path``)."""
    argv = [python, "-I", "{probe}", "--root", root]
    if not inspect_env:
        argv.append("--no-env")
    for rel in paths:
        argv.extend(("--path", rel))
    return Command(
        argv=tuple(argv), files={"probe": probe_source()}, cwd=None, timeout_s=PROBE_TIMEOUT_S
    )


def parse_probe_output(output: str) -> ProbeResult:
    """The probe's JSON line, validated; :class:`ProbeError` when there is none."""
    for line in reversed(output.splitlines()):
        if not line.startswith(MARKER):
            continue
        try:
            return ProbeResult.model_validate(json.loads(line[len(MARKER) :]))
        except (ValueError, ValidationError) as exc:
            raise ProbeError(f"the environment probe printed unreadable output: {exc}") from exc
    tail = output.strip()[-2000:]
    raise ProbeError(f"the environment probe printed no result{': ' + tail if tail else ''}")


def probe_in_process(root: str, *, paths: tuple[str, ...] = ()) -> ProbeResult:
    """The probe run in this interpreter, for the project files only: this
    interpreter's own environment is never the one being captured."""
    namespace = runpy.run_path(str(PROBE_PATH), run_name="alkera_env_probe")
    payload = namespace["collect"](root, inspect_env=False, paths=list(paths))
    return ProbeResult.model_validate(payload)


async def run_probe(
    runner: CommandRunner,
    *,
    python: str | None,
    root: str,
    inspect_env: bool = True,
    paths: tuple[str, ...] = (),
) -> ProbeResult:
    """Read the environment ``python`` belongs to and the workspace at ``root``.
    ``python=None`` reads the workspace alone, in this process."""
    if python is None:
        return probe_in_process(root, paths=paths)
    result = await runner.run(probe_command(python, root, inspect_env=inspect_env, paths=paths))
    if result.exit_code is None:
        raise ProbeError(f"reading the environment timed out after {PROBE_TIMEOUT_S:g} s")
    if not result.ok:
        raise ProbeError(
            f"reading the environment with {python} failed (exit {result.exit_code}): "
            + result.output.strip()[-2000:]
        )
    return parse_probe_output(result.output)


__all__ = [
    "PROBE_PATH",
    "ProbeConda",
    "ProbeDistribution",
    "ProbeEnv",
    "ProbeError",
    "ProbeFile",
    "ProbeResult",
    "parse_probe_output",
    "probe_command",
    "probe_in_process",
    "run_probe",
]
