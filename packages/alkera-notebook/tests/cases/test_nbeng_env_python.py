"""Environment builds ask uv for an explicit Python build (``3.14+gil`` or
``3.14t``; a bare minor can resolve to the free-threaded build) and record
the build the environment's interpreter reports. No network: the runner is
a stand-in that plays uv and the built interpreter."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from alkera_notebook.envs import (
    CommandResult,
    EnvBuildError,
    InvalidPythonError,
    LocalEnvRegistry,
    interpreter_in,
)
from alkera_notebook.envs.python import (
    InvalidPythonRequestError,
    PythonRequest,
    default_python,
    parse_probe,
    python_request,
)

DEFAULT_ID = "default:.alkera/envs/default"
SCRIPT_ID = "script:nb.alknb.py"


@pytest.mark.parametrize(
    ("spec", "free_threaded", "expected"),
    [
        pytest.param("3.14", False, PythonRequest("3.14+gil", "gil"), id="minor_asks_gil"),
        pytest.param("3.14", True, PythonRequest("3.14t", "freethreaded"), id="minor_asks_t"),
        pytest.param("3.13", False, PythonRequest("3.13+gil", "gil"), id="first_ft_minor_gil"),
        pytest.param("3.13", True, PythonRequest("3.13t", "freethreaded"), id="first_ft_minor_t"),
        pytest.param("3.12", False, PythonRequest("3.12+gil", "gil"), id="older_minor_gil"),
        pytest.param("3.14.2", False, PythonRequest("3.14.2+gil", "gil"), id="patch_gil"),
        pytest.param("3.14.2", True, PythonRequest("3.14.2t", "freethreaded"), id="patch_t"),
        pytest.param(
            "cpython@3.14", False, PythonRequest("cpython@3.14+gil", "gil"), id="implementation"
        ),
        pytest.param("3.14t", False, PythonRequest("3.14t", "freethreaded"), id="named_t_kept"),
        pytest.param("3.14+gil", True, PythonRequest("3.14+gil", "gil"), id="named_gil_kept"),
        pytest.param(
            "3.14+freethreaded",
            False,
            PythonRequest("3.14+freethreaded", "freethreaded"),
            id="named_freethreaded_kept",
        ),
        pytest.param(
            "/opt/py/bin/python3", True, PythonRequest("/opt/py/bin/python3", None), id="path"
        ),
        pytest.param(
            "C:\\py\\python.exe", False, PythonRequest("C:\\py\\python.exe", None), id="win_path"
        ),
        pytest.param(" 3.14 ", False, PythonRequest("3.14+gil", "gil"), id="trimmed"),
    ],
)
def test_python_request(spec: str, free_threaded: bool, expected: PythonRequest) -> None:
    assert python_request(spec, free_threaded=free_threaded) == expected


@pytest.mark.parametrize(
    ("spec", "free_threaded"),
    [
        pytest.param("", False, id="empty"),
        pytest.param("3.12", True, id="no_free_threaded_build"),
        pytest.param(">=3.12", False, id="range"),
        pytest.param("3", False, id="major_only"),
        pytest.param("python3", False, id="bare_executable_name"),
        pytest.param("pypy@3.11", False, id="other_implementation"),
        pytest.param("3.14+debug", False, id="unknown_variant"),
    ],
)
def test_python_request_refused(spec: str, free_threaded: bool) -> None:
    with pytest.raises(InvalidPythonRequestError):
        python_request(spec, free_threaded=free_threaded)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param('{"version": "3.14.2", "build": "gil"}\n', ("3.14.2", "gil"), id="gil"),
        pytest.param(
            'noise\n{"version": "3.14.2", "build": "freethreaded"}',
            ("3.14.2", "freethreaded"),
            id="last_line_counts",
        ),
    ],
)
def test_parse_probe(stdout: str, expected: tuple[str, str]) -> None:
    assert parse_probe(stdout) == expected


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param("", id="empty"),
        pytest.param("not json", id="not_json"),
        pytest.param("[1]", id="not_an_object"),
        pytest.param('{"version": "3.14.2", "build": "nogil"}', id="unknown_build"),
        pytest.param('{"build": "gil"}', id="no_version"),
    ],
)
def test_parse_probe_refuses(stdout: str) -> None:
    with pytest.raises(ValueError, match="did not report its build"):
        parse_probe(stdout)


class PlayRunner:
    """Plays uv (creating the interpreter a build would) and the built
    interpreter answering the build probe."""

    def __init__(self, build: str = "gil", version: str = "3.14.2", probe_exit: int = 0) -> None:
        self.build, self.version, self.probe_exit = build, version, probe_exit
        self.calls: list[list[str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "uv":
            prefix = None
            if argv[1] == "venv":
                prefix = Path(argv[-1])
            elif argv[1] == "sync":
                prefix = Path((env or {})["UV_PROJECT_ENVIRONMENT"])
            if prefix is not None:
                interp = interpreter_in(prefix)
                interp.parent.mkdir(parents=True, exist_ok=True)
                interp.write_text("")
            return CommandResult(0, "", "")
        # The built interpreter, running the probe.
        assert argv[1:3] == ["-I", "-c"]
        if self.probe_exit:
            return CommandResult(self.probe_exit, "", "boom")
        return CommandResult(0, json.dumps({"version": self.version, "build": self.build}), "")

    def pythons(self) -> list[str]:
        return [c[c.index("--python") + 1] for c in self.calls if c[0] == "uv"]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    spec = ws / ".alkera" / "envs" / "default"
    spec.mkdir(parents=True)
    (spec / "pyproject.toml").write_text('[project]\nname = "d"\nversion = "0"\n')
    (ws / "nb.alknb.py").write_text("# /// script\n# dependencies = []\n# ///\nx = 1\n")
    return ws


def _registry(tmp_path: Path, runner: PlayRunner, **kw: object) -> LocalEnvRegistry:
    return LocalEnvRegistry(_workspace(tmp_path), tmp_path / "envs", runner=runner, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize("env_id", [DEFAULT_ID, SCRIPT_ID])
@pytest.mark.parametrize(
    ("python", "free_threaded", "reported", "asked"),
    [
        pytest.param("3.14", False, "gil", "3.14+gil", id="minor_gil"),
        pytest.param("3.14", True, "freethreaded", "3.14t", id="minor_free_threaded"),
        pytest.param("3.14t", False, "freethreaded", "3.14t", id="named_build"),
        pytest.param(
            "/opt/py/bin/python3.14t",
            False,
            "freethreaded",
            "/opt/py/bin/python3.14t",
            id="path_records_what_it_is",
        ),
        pytest.param(
            "/opt/py/bin/python3.14", False, "gil", "/opt/py/bin/python3.14", id="path_gil"
        ),
    ],
)
async def test_build_asks_explicitly_and_records_what_it_got(
    tmp_path: Path, env_id: str, python: str, free_threaded: bool, reported: str, asked: str
) -> None:
    runner = PlayRunner(build=reported)
    reg = _registry(tmp_path, runner, python=python, free_threaded=free_threaded)
    built = await reg.materialize(env_id)
    assert runner.pythons() and set(runner.pythons()) == {asked}
    assert built.state == "ready" and built.python_build == reported
    # The record survives a new registry (another process, a wake).
    again = LocalEnvRegistry(tmp_path / "ws", tmp_path / "envs", runner=PlayRunner())
    assert again.describe(env_id).python_build == reported
    record = again._detector.records.get(env_id)
    assert (record.python_request, record.python_build, record.python_version) == (
        asked,
        reported,
        "3.14.2",
    )


@pytest.mark.parametrize(
    ("python", "free_threaded", "reported"),
    [
        pytest.param("3.14", False, "freethreaded", id="asked_gil_got_free_threaded"),
        pytest.param("3.14", True, "gil", id="asked_t_got_gil"),
    ],
)
async def test_a_build_other_than_asked_fails(
    tmp_path: Path, python: str, free_threaded: bool, reported: str
) -> None:
    reg = _registry(
        tmp_path, PlayRunner(build=reported), python=python, free_threaded=free_threaded
    )
    with pytest.raises(EnvBuildError, match=f"got a {reported} Python") as caught:
        await reg.materialize(DEFAULT_ID)
    assert "<build probe>" in caught.value.log
    desc = reg.describe(DEFAULT_ID)
    assert desc.state == "failed" and desc.python_build == ""


async def test_an_interpreter_that_cannot_report_its_build_fails(tmp_path: Path) -> None:
    reg = _registry(tmp_path, PlayRunner(probe_exit=1), python="3.14")
    with pytest.raises(EnvBuildError, match="did not report its build"):
        await reg.materialize(DEFAULT_ID)
    assert reg.describe(DEFAULT_ID).state == "failed"


async def test_install_asks_for_the_same_explicit_build(tmp_path: Path) -> None:
    runner = PlayRunner(build="freethreaded")
    reg = _registry(tmp_path, runner, python="3.14", free_threaded=True)
    await reg.materialize(DEFAULT_ID)
    runner.calls.clear()
    desc, _, _ = await reg.install(DEFAULT_ID, ["tinypkg"], "nb.alknb.py")
    assert [c[1] for c in runner.calls if c[0] == "uv"] == ["add"]  # spec unchanged: no rebuild
    assert set(runner.pythons()) == {"3.14t"}
    assert desc.python_build == "freethreaded"


@pytest.mark.parametrize("python", ["python3", ">=3.12", ""])
def test_registry_refuses_an_inexplicit_python(tmp_path: Path, python: str) -> None:
    with pytest.raises(InvalidPythonError):
        LocalEnvRegistry(tmp_path, tmp_path / "envs", runner=PlayRunner(), python=python or " ")


COMPILED_BINARY = "/opt/alkera/alkera.dist/alkera"


@pytest.mark.parametrize("env_id", [DEFAULT_ID, SCRIPT_ID])
async def test_a_compiled_build_never_hands_uv_its_own_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_id: str
) -> None:
    # In a compiled build sys.executable is the build's binary, which uv
    # cannot inspect as a Python: the build asks for its own version instead.
    monkeypatch.setattr(sys, "executable", COMPILED_BINARY)
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", object(), raising=False)
    runner = PlayRunner()
    await _registry(tmp_path, runner).materialize(env_id)
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert runner.pythons() and set(runner.pythons()) == {f"{minor}+gil"}


def test_a_compiled_build_prefers_the_box_python_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", COMPILED_BINARY)
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", object(), raising=False)
    home = tmp_path / "python"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "python3").write_text("")
    assert default_python(str(home)) == str(home / "bin" / "python3")
    # A python_home with no interpreter falls back to the build's version.
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert default_python(str(tmp_path / "missing")) == minor


def test_from_source_python_home_is_not_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/py/bin/python3.14")
    monkeypatch.delattr(sys.modules["__main__"], "__compiled__", raising=False)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_text("")
    assert default_python(str(tmp_path)) == "/opt/py/bin/python3.14"


async def test_from_source_the_running_interpreter_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/py/bin/python3.14")
    monkeypatch.delattr(sys.modules["__main__"], "__compiled__", raising=False)
    runner = PlayRunner()
    await _registry(tmp_path, runner).materialize(DEFAULT_ID)
    assert set(runner.pythons()) == {"/opt/py/bin/python3.14"}
