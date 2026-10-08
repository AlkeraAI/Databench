"""``alkera.persistent_cache``: results survive the process, follow the
function's source and arguments, and a bad entry is recomputed."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import alkera
import pytest
from alkera._cache import CACHE_SUBDIR

PACKAGE_ROOT = Path(alkera.__file__).resolve().parents[1]


@pytest.fixture
def notebook_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _entries(root: Path) -> list[Path]:
    directory = root / CACHE_SUBDIR
    return sorted(directory.iterdir()) if directory.exists() else []


def test_a_repeated_call_is_served_from_disk(notebook_dir: Path) -> None:
    runs: list[int] = []

    @alkera.persistent_cache
    def square(x: int) -> int:
        runs.append(x)
        return x * x

    assert square(4) == 16
    assert square(4) == 16
    assert runs == [4]
    (entry,) = _entries(notebook_dir)
    assert entry.name.startswith("test_a_repeated_call_is_served_from_disk._locals_.square-")


def test_different_arguments_are_different_entries(notebook_dir: Path) -> None:
    runs: list[tuple[int, int]] = []

    @alkera.persistent_cache
    def add(a: int, b: int = 0) -> int:
        runs.append((a, b))
        return a + b

    assert add(1, b=2) == 3
    assert add(2, b=1) == 3
    assert add(1, b=2) == 3
    assert runs == [(1, 2), (2, 1)]
    assert len(_entries(notebook_dir)) == 2


def test_keyword_order_does_not_matter(notebook_dir: Path) -> None:
    runs: list[str] = []

    @alkera.persistent_cache
    def f(**kw: int) -> int:
        runs.append("run")
        return sum(kw.values())

    f(a=1, b=2)
    f(b=2, a=1)
    assert runs == ["run"]


def test_a_cached_none_is_still_a_hit(notebook_dir: Path) -> None:
    runs: list[str] = []

    @alkera.persistent_cache
    def nothing() -> None:
        runs.append("run")

    nothing()
    nothing()
    assert runs == ["run"]


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda p: p.write_bytes(p.read_bytes()[:5]), id="truncated"),
        pytest.param(lambda p: p.write_bytes(b"garbage"), id="garbage"),
        pytest.param(lambda p: p.write_bytes(b""), id="empty"),
        pytest.param(
            lambda p: p.write_bytes(pickle.dumps({"not": "an entry"})), id="foreign-pickle"
        ),
        pytest.param(
            lambda p: p.write_bytes(pickle.dumps(("alkera.persistent_cache", 99, 1))),
            id="future-version",
        ),
    ],
)
def test_a_bad_entry_is_recomputed_and_rewritten(notebook_dir: Path, damage: object) -> None:
    runs: list[int] = []

    @alkera.persistent_cache
    def value() -> int:
        runs.append(1)
        return 7

    value()
    (entry,) = _entries(notebook_dir)
    damage(entry)  # type: ignore[operator]
    assert value() == 7
    assert len(runs) == 2
    assert value() == 7
    assert len(runs) == 2


def test_an_unpicklable_argument_runs_uncached_with_a_warning(notebook_dir: Path) -> None:
    runs: list[str] = []

    @alkera.persistent_cache
    def size(obj: object) -> int:
        runs.append("run")
        return 1

    with pytest.warns(UserWarning, match="cannot be pickled"):
        size(lambda: None)
    with pytest.warns(UserWarning):
        size(lambda: None)
    assert runs == ["run", "run"]
    assert _entries(notebook_dir) == []


def test_an_unpicklable_result_is_returned_but_not_stored(notebook_dir: Path) -> None:
    @alkera.persistent_cache
    def make() -> object:
        return lambda: 1

    with pytest.warns(UserWarning, match="result of"):
        result = make()
    assert callable(result)
    # No entry and no temporary file left behind.
    assert _entries(notebook_dir) == []


def test_a_directory_can_be_given(tmp_path: Path) -> None:
    where = tmp_path / "elsewhere"

    @alkera.persistent_cache(directory=where)
    def one() -> int:
        return 1

    assert one() == 1
    assert len(list(where.iterdir())) == 1


def _run(script: Path, cwd: Path) -> str:
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)}
    done = subprocess.run(
        [sys.executable, str(script)], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout


SCRIPT = """
import alkera

@alkera.persistent_cache
def slow(n):
    print("computing", n)
    return {BODY}

print("result", slow(3))
"""


def test_results_persist_across_processes_until_the_source_changes(tmp_path: Path) -> None:
    script = tmp_path / "nb.py"
    script.write_text(textwrap.dedent(SCRIPT).replace("{BODY}", "n * 10"))
    assert _run(script, tmp_path) == "computing 3\nresult 30\n"
    assert _run(script, tmp_path) == "result 30\n"
    script.write_text(textwrap.dedent(SCRIPT).replace("{BODY}", "n * 100"))
    assert _run(script, tmp_path) == "computing 3\nresult 300\n"
    assert _run(script, tmp_path) == "result 300\n"
    cache = tmp_path / CACHE_SUBDIR
    assert len(list(cache.glob("slow-*.pkl"))) == 2
    assert list(cache.glob("*.tmp")) == []
