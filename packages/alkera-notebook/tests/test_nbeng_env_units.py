"""Pure parts of environments: script blocks, requirements, states, the runner."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from alkera_notebook.envs import (
    DistributionRequiredError,
    InvalidRequirementError,
    LocalCommandRunner,
    add_script_dependencies,
    detect_envs,
    distribution_for_module,
    env_fingerprint,
    parse_script_block,
    remove_script_dependencies,
    script_dependencies,
    validate_requirement,
)
from alkera_notebook.envs.detect import Detector, interpreter_in
from alkera_notebook.envs.state import BuildRecord, BuildRecords

# Script blocks ------------------------------------------------------------------

ADD_CASES = [
    pytest.param(
        "",
        ["polars>=1.9"],
        '# /// script\n# dependencies = [\n#     "polars>=1.9",\n# ]\n# ///\n',
        id="empty-header",
    ),
    pytest.param(
        '#!/usr/bin/env -S uv run --script\n# -*- coding: utf-8 -*-\n"""Doc."""\n',
        ["tinypkg"],
        "#!/usr/bin/env -S uv run --script\n# -*- coding: utf-8 -*-\n# /// script\n"
        '# dependencies = [\n#     "tinypkg",\n# ]\n# ///\n"""Doc."""\n',
        id="after-shebang-and-coding",
    ),
    pytest.param(
        '# /// script\n# requires-python = ">=3.11"\n# ///\n',
        ["tinypkg"],
        '# /// script\n# requires-python = ">=3.11"\n# dependencies = [\n'
        '#     "tinypkg",\n# ]\n# ///\n',
        id="block-without-dependencies",
    ),
    pytest.param(
        '# /// script\n# dependencies = [\n#   "a==1",\n#   "Tiny.Pkg<2",\n# ]\n'
        '# requires-python = ">=3.11"\n# ///\nx = 1\n',
        ["tiny-pkg==1.0.0", "b"],
        '# /// script\n# dependencies = [\n#     "a==1",\n#     "tiny-pkg==1.0.0",\n'
        '#     "b",\n# ]\n# requires-python = ">=3.11"\n# ///\nx = 1\n',
        id="replaces-same-canonical-name-keeps-other-keys",
    ),
    pytest.param(
        '# /// script\n# dependencies = ["a"]\n# ///\n',
        ["c"],
        '# /// script\n# dependencies = [\n#     "a",\n#     "c",\n# ]\n# ///\n',
        id="single-line-array",
    ),
]


@pytest.mark.parametrize(("header", "packages", "expected"), ADD_CASES)
def test_add_script_dependencies(header: str, packages: list[str], expected: str) -> None:
    out = add_script_dependencies(header, packages)
    assert out == expected
    data = parse_script_block(out)
    assert data is not None
    for p in packages:
        assert p in script_dependencies(out)


@pytest.mark.parametrize(
    ("header", "names", "expected"),
    [
        pytest.param(
            '# /// script\n# dependencies = [\n#   "a==1",\n#   "Tiny.Pkg<2",\n# ]\n'
            '# requires-python = ">=3.11"\n# ///\nx = 1\n',
            ["tiny-pkg"],
            '# /// script\n# dependencies = [\n#     "a==1",\n# ]\n'
            '# requires-python = ">=3.11"\n# ///\nx = 1\n',
            id="by-canonical-name-keeps-other-keys",
        ),
        pytest.param(
            '# /// script\n# dependencies = ["a"]\n# ///\n',
            ["b"],
            '# /// script\n# dependencies = [\n#     "a",\n# ]\n# ///\n',
            id="a-name-not-listed-changes-no-requirement",
        ),
        pytest.param("x = 1\n", ["a"], "x = 1\n", id="no-block"),
    ],
)
def test_remove_script_dependencies(header: str, names: list[str], expected: str) -> None:
    assert remove_script_dependencies(header, names) == expected


def test_add_script_dependencies_is_idempotent_for_the_same_requirement() -> None:
    once = add_script_dependencies("", ["tinypkg"])
    assert add_script_dependencies(once, ["tinypkg"]) == once


def test_add_script_dependencies_refuses_a_block_that_is_not_toml() -> None:
    with pytest.raises(ValueError, match="not valid TOML"):
        add_script_dependencies("# /// script\n# dependencies = [\n# ///\n", ["a"])


def test_a_non_script_pep723_block_is_not_the_script_block() -> None:
    text = '# /// other\n# dependencies = ["x"]\n# ///\n'
    assert parse_script_block(text) is None
    out = add_script_dependencies(text, ["y"])
    assert script_dependencies(out) == ["y"]
    assert out.endswith(text)


# Requirements and module names ---------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    [
        pytest.param("polars", id="name"),
        pytest.param("polars>=1.9", id="specifier"),
        pytest.param("polars>=1.9,<2", id="two-specifiers"),
        pytest.param("scikit-learn==1.5.*", id="wildcard"),
        pytest.param("ray[default]~=2.0", id="extras"),
        pytest.param("zope.interface", id="dotted"),
    ],
)
def test_valid_requirements_pass(requirement: str) -> None:
    assert validate_requirement(requirement) == requirement


@pytest.mark.parametrize(
    "requirement",
    [
        pytest.param("--index-url=https://evil", id="flag"),
        pytest.param("-e .", id="editable"),
        pytest.param("pkg @ https://example.com/pkg.whl", id="url"),
        pytest.param("a b", id="space"),
        pytest.param("../local", id="path"),
        pytest.param("pkg; os_name=='nt'", id="marker"),
        pytest.param("", id="empty"),
        pytest.param("pkg>=1;rm -rf", id="shell"),
    ],
)
def test_invalid_requirements_are_refused(requirement: str) -> None:
    with pytest.raises(InvalidRequirementError):
        validate_requirement(requirement)


@pytest.mark.parametrize(
    ("module", "distribution", "expected"),
    [
        pytest.param("sklearn", None, "scikit-learn", id="sklearn"),
        pytest.param("sklearn.linear_model", None, "scikit-learn", id="submodule"),
        pytest.param("cv2", None, "opencv-python", id="cv2"),
        pytest.param("PIL", None, "pillow", id="pil"),
        pytest.param("yaml", None, "pyyaml", id="yaml"),
        pytest.param("bs4", None, "beautifulsoup4", id="bs4"),
        pytest.param("whatever", "my-dist==1.0", "my-dist==1.0", id="explicit-wins"),
    ],
)
def test_distribution_for_module(module: str, distribution: str | None, expected: str) -> None:
    assert distribution_for_module(module, distribution) == expected


@pytest.mark.parametrize("module", ["polars", "requests", "mypkg.sub"])
def test_unmapped_module_requires_a_distribution(module: str) -> None:
    with pytest.raises(DistributionRequiredError):
        distribution_for_module(module)


def test_explicit_distribution_is_still_validated() -> None:
    with pytest.raises(InvalidRequirementError):
        distribution_for_module("x", "--extra-index-url=http://x")


# Fingerprint ----------------------------------------------------------------------


def test_fingerprint_changes_with_each_field() -> None:
    base = env_fingerprint("default", b"lock", "3.13", "macosx-arm64")
    assert base == env_fingerprint("default", b"lock", "3.13", "macosx-arm64")
    variants = [
        env_fingerprint("uv_project", b"lock", "3.13", "macosx-arm64"),
        env_fingerprint("default", b"lock2", "3.13", "macosx-arm64"),
        env_fingerprint("default", b"lock", "3.12", "macosx-arm64"),
        env_fingerprint("default", b"lock", "3.13", "linux-x86_64"),
        # Field boundaries matter: moving a byte across a separator changes it.
        env_fingerprint("defaul", b"tlock", "3.13", "macosx-arm64"),
    ]
    assert len({base, *variants}) == 6


# States from build records ----------------------------------------------------------


def _fake_interpreter(prefix: Path) -> None:
    interp = interpreter_in(prefix)
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("")
    (prefix / "pyvenv.cfg").write_text("home = /x\nversion_info = 3.13.1\n")


@pytest.mark.parametrize(
    ("built", "record", "expected"),
    [
        pytest.param(False, None, "missing", id="never-built"),
        pytest.param(False, "failed", "failed", id="failed-build"),
        pytest.param(True, "current", "ready", id="built-current"),
        pytest.param(True, "old", "stale", id="built-from-an-older-spec"),
        pytest.param(True, None, "stale", id="built-with-no-record"),
        pytest.param(True, "failed", "failed", id="built-then-failed"),
    ],
)
def test_default_state_follows_the_build_and_its_record(
    tmp_path: Path, built: bool, record: str | None, expected: str
) -> None:
    ws, env_root = tmp_path / "ws", tmp_path / "envs"
    spec = ws / ".alkera" / "envs" / "default"
    spec.mkdir(parents=True)
    (spec / "pyproject.toml").write_text('[project]\nname = "d"\nversion = "0"\n')
    det = Detector(ws, env_root)
    desc = det.default()
    if built:
        _fake_interpreter(Path(desc.prefix))
    if record == "current":
        det.records.put(desc.env_id, BuildRecord(built_spec_hash=desc.spec_hash))
    elif record == "old":
        det.records.put(desc.env_id, BuildRecord(built_spec_hash="0" * 64))
    elif record == "failed":
        det.records.put(desc.env_id, BuildRecord(failed=True))
    state = det.default().state
    assert state == expected
    if built:
        assert det.default().python_version == "3.13.1"


def test_build_records_ignore_corrupt_and_foreign_files(tmp_path: Path) -> None:
    records = BuildRecords(tmp_path)
    records.put("default:x", BuildRecord(built_spec_hash="abc", built_spec={"a": "b"}))
    assert records.get("default:x").built_spec == {"a": "b"}
    path = next((tmp_path / "records").iterdir())
    path.write_text("{not json")
    assert records.get("default:x") == BuildRecord()
    path.write_text('{"env_id": "other", "built_spec_hash": "abc"}')
    assert records.get("default:x").built_spec_hash is None


def test_a_record_an_earlier_release_wrote_reads_as_the_build_it_names(tmp_path: Path) -> None:
    """Records on a machine from before builds stopped waiting for approval
    name the build's spec ``approved_spec``; reading them as no build would
    rebuild every environment once for nothing."""
    records = BuildRecords(tmp_path)
    records.put("default:x", BuildRecord())
    path = next((tmp_path / "records").iterdir())
    path.write_text(
        '{"env_id": "default:x", "approved_spec_hash": "abc", "approved_spec": {"a": "b"}}'
    )
    record = records.get("default:x")
    assert (record.built_spec_hash, record.built_spec, record.built_by) == ("abc", {"a": "b"}, "")


def test_records_live_under_the_env_root_not_the_tree(tmp_path: Path) -> None:
    ws, env_root = tmp_path / "ws", tmp_path / "envs"
    (ws / "nb").mkdir(parents=True)
    nb = ws / "nb" / "n.alknb.py"
    nb.write_text("x = 1\n")
    det = Detector(ws, env_root)
    det.records.put(det.default().env_id, BuildRecord(built_spec_hash="x"))
    descs = detect_envs(ws, nb, env_root=env_root)
    assert all(not Path(d.prefix).is_relative_to(ws) for d in descs if d.kind == "default")
    assert sorted(p.name for p in ws.rglob("*")) == ["n.alknb.py", "nb"]


# The runner --------------------------------------------------------------------------


async def test_runner_captures_output_and_exit_code(tmp_path: Path) -> None:
    result = await LocalCommandRunner().run(
        ["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=str(tmp_path)
    )
    assert (result.returncode, result.stdout, result.stderr, result.timed_out) == (
        3,
        "out\n",
        "err\n",
        False,
    )


async def test_runner_never_inherits_stdin(tmp_path: Path) -> None:
    started = time.monotonic()
    result = await LocalCommandRunner().run(["cat"], cwd=str(tmp_path), timeout_s=10)
    assert result.returncode == 0 and result.stdout == ""
    assert time.monotonic() - started < 5


async def test_runner_passes_only_the_given_environment(tmp_path: Path) -> None:
    result = await LocalCommandRunner().run(
        ["/usr/bin/env"], cwd=str(tmp_path), env={"ONLY_THIS": "1"}
    )
    assert result.stdout.strip().splitlines() == ["ONLY_THIS=1"]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
async def test_runner_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    result = await LocalCommandRunner().run(
        ["sh", "-c", f"sleep 60 & echo $! > {pid_file}; wait"],
        cwd=str(tmp_path),
        timeout_s=0.5,
    )
    assert result.timed_out and result.returncode != 0
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            alive = False
            break
        await asyncio.sleep(0.05)
    assert not alive, "the grandchild outlived the timeout"


async def test_runner_refuses_an_empty_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        await LocalCommandRunner().run([], cwd=str(tmp_path))
