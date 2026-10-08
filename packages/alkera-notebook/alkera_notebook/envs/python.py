"""Which Python an environment is built with, asked of uv explicitly.

A bare minor such as ``3.14`` can resolve to the free-threaded build when uv
finds one first, so a version request always names the build: ``3.14+gil``
or ``3.14t``. An interpreter path is already one specific build and is passed
as is. After a build the environment's interpreter reports which build it
is, and that is what gets recorded.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PythonBuild = Literal["gil", "freethreaded"]

# The first minor with a free-threaded build.
FREE_THREADED_SINCE = (3, 13)

_VERSION = re.compile(
    r"(?P<impl>(?:cpython|python)[-@]?)?(?P<major>\d+)\.(?P<minor>\d+)(?:\.\d+)?"
    r"(?P<variant>t|\+gil|\+freethreaded)?",
    re.IGNORECASE,
)

# Run by the environment's own interpreter: its version and build.
PROBE = (
    "import json, platform, sysconfig; print(json.dumps({"
    "'version': platform.python_version(), "
    "'build': 'freethreaded' if sysconfig.get_config_var('Py_GIL_DISABLED') else 'gil'}))"
)


class InvalidPythonRequestError(ValueError):
    """The configured Python is neither a version nor an interpreter path."""


@dataclass(frozen=True)
class PythonRequest:
    """What uv receives as ``--python``, and the build it asks for (None for
    an interpreter path, whose build is whatever that binary is)."""

    request: str
    build: PythonBuild | None


def default_python(python_home: str | None = None) -> str:
    """The Python an environment is built with when none is configured.

    From source it is the running interpreter. In a compiled build
    ``sys.executable`` is the build's own binary, which no tool can inspect as
    a Python: the request is ``python_home``'s interpreter when the box has
    one there, else this build's version for uv to find (or download).
    """
    compiled = getattr(sys.modules.get("__main__"), "__compiled__", None) is not None
    if not compiled and sys.executable:
        return sys.executable
    if python_home:
        interpreter = Path(python_home) / "bin" / "python3"
        if interpreter.is_file():
            return str(interpreter)
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _is_path(spec: str) -> bool:
    return "/" in spec or "\\" in spec or spec.startswith("~")


def python_request(spec: str, *, free_threaded: bool = False) -> PythonRequest:
    """The explicit uv request for ``spec`` (a version or an interpreter path).

    A version without a build gets one: ``+gil``, or ``t`` when
    ``free_threaded``. A version that already names its build keeps it.
    """
    spec = spec.strip()
    if not spec:
        raise InvalidPythonRequestError("no Python was configured")
    if _is_path(spec):
        return PythonRequest(spec, None)
    m = _VERSION.fullmatch(spec)
    if m is None:
        raise InvalidPythonRequestError(
            f"{spec!r} is not a Python version (such as 3.14) or an interpreter path"
        )
    variant = (m["variant"] or "").lower()
    if variant:
        return PythonRequest(spec, "gil" if variant == "+gil" else "freethreaded")
    if free_threaded:
        if (int(m["major"]), int(m["minor"])) < FREE_THREADED_SINCE:
            raise InvalidPythonRequestError(f"Python {spec} has no free-threaded build")
        return PythonRequest(f"{spec}t", "freethreaded")
    return PythonRequest(f"{spec}+gil", "gil")


def parse_probe(stdout: str) -> tuple[str, PythonBuild]:
    """(version, build) from :data:`PROBE`'s output; ValueError if it is not that."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"the interpreter did not report its build: {stdout!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"the interpreter did not report its build: {stdout!r}")
    version, build = data.get("version"), data.get("build")
    if not isinstance(version, str) or build not in ("gil", "freethreaded"):
        raise ValueError(f"the interpreter did not report its build: {stdout!r}")
    return version, build
