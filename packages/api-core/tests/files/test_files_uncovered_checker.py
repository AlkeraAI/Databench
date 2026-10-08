"""The coverage-gap checker (`scripts/check_files_uncovered.py`) itself.

The checker is what makes the 95% floor mean something: it fails when a gap is
unreviewed AND when a review is stale. Both directions are driven here with
hand-written coverage JSON and hand-written tables in `tmp_path`, through the
real script in a real subprocess — the same way `make files-coverage` runs it.

Rows are keyed by the line's own text, not its number, so inserting code above
a gap must not stale the row that reviewed it — that case is pinned here too.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CHECKER = REPO_ROOT / "scripts" / "check_files_uncovered.py"

HEADER = "| path | excerpt | reason |\n| --- | --- | --- |\n"


def write_report(
    tmp_path: Path,
    *,
    missing_lines: list[int] | None = None,
    missing_branches: list[list[int]] | None = None,
    source: str = "src/thing.py",
) -> Path:
    report: dict[str, Any] = {
        "files": {
            source: {
                "missing_lines": missing_lines or [],
                "missing_branches": missing_branches or [],
            }
        }
    }
    target = tmp_path / "coverage.json"
    target.write_text(json.dumps(report), encoding="utf-8")
    return target


def write_table(tmp_path: Path, rows: str) -> Path:
    target = tmp_path / "UNCOVERED.md"
    target.write_text(HEADER + rows, encoding="utf-8")
    return target


def write_source(tmp_path: Path, body: str, *, name: str = "src/thing.py") -> None:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def run(tmp_path: Path, report: Path, table: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            str(report),
            str(table),
            "--root",
            "src",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_uncovered_line_absent_from_the_table_fails_and_is_named(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nb = 2\nc = 3\n")
    report = write_report(tmp_path, missing_lines=[2])
    table = write_table(tmp_path, "")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "src/thing.py:2" in result.stdout
    assert "not listed" in result.stdout


def test_an_uncovered_line_that_is_listed_passes(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nb = 2\nc = 3\n")
    report = write_report(tmp_path, missing_lines=[2])
    table = write_table(tmp_path, "| src/thing.py | `b = 2` | platform-only branch |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 0, result.stdout


def test_a_listed_line_that_is_now_covered_fails_as_stale(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nb = 2\nc = 3\n")
    report = write_report(tmp_path, missing_lines=[])
    table = write_table(tmp_path, "| src/thing.py | `b = 2` | used to be unreachable |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "src/thing.py:2" in result.stdout
    assert "now covered" in result.stdout


def test_a_reason_less_row_is_refused(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nb = 2\n")
    report = write_report(tmp_path, missing_lines=[2])
    table = write_table(tmp_path, "| src/thing.py | `b = 2` |  |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "malformed" in result.stdout


def test_an_unlisted_pragma_no_cover_fails_even_when_coverage_is_clean(tmp_path: Path) -> None:
    """A pragma is invisible to coverage — it is excluded, not missing. The
    checker reads the sources so an unreviewed pragma cannot hide under a 100%
    report."""
    write_source(tmp_path, "a = 1\nif a:  # pragma: no cover\n    b = 2\n")
    report = write_report(tmp_path, missing_lines=[])
    table = write_table(tmp_path, "")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "src/thing.py:2" in result.stdout
    assert "pragma: no cover" in result.stdout


def test_a_listed_pragma_passes_and_does_not_read_as_stale(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nif a:  # pragma: no cover\n    b = 2\n")
    report = write_report(tmp_path, missing_lines=[])
    table = write_table(tmp_path, "| src/thing.py | `if a:  # pragma: no cover` | windows-only |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 0, result.stdout


def test_an_uncovered_branch_arc_is_anchored_at_its_source_line(tmp_path: Path) -> None:
    """A partial branch leaves every line executed — only the arc is missing.
    The gate would be blind to it if the checker read `missing_lines` alone."""
    write_source(tmp_path, "a = 1\nif a:\n    b = 2\n")
    report = write_report(tmp_path, missing_lines=[], missing_branches=[[2, 4]])
    table = write_table(tmp_path, "")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "src/thing.py:2" in result.stdout


def test_a_listed_file_coverage_never_measured_fails(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\n")
    report = write_report(tmp_path, missing_lines=[1])
    table = write_table(
        tmp_path,
        "| src/thing.py | `a = 1` | fine |\n| src/gone.py | `x = 4` | file was deleted |\n",
    )

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "src/gone.py" in result.stdout
    assert "no such file" in result.stdout


def _checker() -> Any:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_files_uncovered
    finally:
        sys.path.pop(0)
    return check_files_uncovered


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        pytest.param("`b = 2`", [("b = 2", 1)], id="one-excerpt"),
        pytest.param("`a = 1`, `b = 2`", [("a = 1", 1), ("b = 2", 1)], id="two-excerpts"),
        pytest.param("`return None` (#3)", [("return None", 3)], id="named-occurrence"),
        pytest.param("`f(a, b)`", [("f(a, b)", 1)], id="code-with-a-comma"),
        pytest.param(r"`a \| b`", [("a | b", 1)], id="escaped-pipe"),
    ],
)
def test_excerpt_cell_parsing(cell: str, expected: list[tuple[str, int]]) -> None:
    parsed = _checker().parse_excerpts(cell)

    assert [(item.code, item.occurrence) for item in parsed] == expected


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("2", id="a-bare-line-number"),
        pytest.param("", id="empty"),
        pytest.param("`a = 1` and `b = 2`", id="unquoted-prose-between-excerpts"),
        pytest.param("`a = 1` (#0)", id="occurrence-zero"),
    ],
)
def test_an_unparseable_excerpt_cell_is_refused_rather_than_silently_empty(cell: str) -> None:
    checker = _checker()

    with pytest.raises(checker.TableError):
        checker.parse_excerpts(cell)


def test_a_row_survives_an_edit_above_the_gap(tmp_path: Path) -> None:
    """The whole point of keying by text: the gap moved from line 2 to line 4
    and the row that reviewed it is still the row that reviews it."""
    write_source(tmp_path, "import os\nimport sys\na = 1\nb = 2\n")
    report = write_report(tmp_path, missing_lines=[4])
    table = write_table(tmp_path, "| src/thing.py | `b = 2` | platform-only branch |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 0, result.stdout


def test_a_row_whose_excerpt_is_gone_fails_as_stale(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nc = 3\n")
    report = write_report(tmp_path, missing_lines=[2])
    table = write_table(
        tmp_path,
        "| src/thing.py | `b = 2` | the line it reviewed |\n"
        "| src/thing.py | `c = 3` | the line that is really uncovered |\n",
    )

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "`b = 2`" in result.stdout
    assert "not in the file any more" in result.stdout


def test_a_repeated_excerpt_must_name_its_occurrence(tmp_path: Path) -> None:
    write_source(tmp_path, "if a:\n    return None\nif b:\n    return None\n")
    report = write_report(tmp_path, missing_lines=[4])
    table = write_table(tmp_path, "| src/thing.py | `return None` | ambiguous |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "occurs 2 times" in result.stdout


def test_a_named_occurrence_resolves_to_that_line_and_no_other(tmp_path: Path) -> None:
    write_source(tmp_path, "if a:\n    return None\nif b:\n    return None\n")
    report = write_report(tmp_path, missing_lines=[4])

    second = write_table(tmp_path, "| src/thing.py | `return None` (#2) | reviewed |\n")
    assert run(tmp_path, report, second).returncode == 0

    first = write_table(tmp_path, "| src/thing.py | `return None` (#1) | wrong line |\n")
    wrong = run(tmp_path, report, first)
    assert wrong.returncode == 1, wrong.stdout
    assert "now covered" in wrong.stdout
    assert "src/thing.py:4" in wrong.stdout


def test_an_occurrence_past_the_last_one_is_refused(tmp_path: Path) -> None:
    write_source(tmp_path, "a = 1\nb = 2\n")
    report = write_report(tmp_path, missing_lines=[2])
    table = write_table(tmp_path, "| src/thing.py | `b = 2` (#2) | there is only one |\n")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "occurs only 1 time" in result.stdout


def test_an_unlisted_gap_is_reported_as_a_row_to_paste(tmp_path: Path) -> None:
    """A new gap has to be trivial to review, so the message IS the row."""
    write_source(tmp_path, "if a:\n    return None\nif b:\n    return None\n")
    report = write_report(tmp_path, missing_lines=[4])
    table = write_table(tmp_path, "")

    result = run(tmp_path, report, table)

    assert result.returncode == 1, result.stdout
    assert "| src/thing.py | `return None` (#2) | <reason> |" in result.stdout


def test_the_real_table_matches_the_real_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """The committed UNCOVERED.md must parse and must list every pragma that is
    actually in the Files sources — a pragma added without a row fails here as
    well as in `make files-coverage`."""
    monkeypatch.chdir(REPO_ROOT)  # rows name repo-relative paths
    checker = _checker()

    table_path = REPO_ROOT / "packages" / "api-core" / "tests" / "files" / "UNCOVERED.md"
    listed = checker.parse_table(table_path.read_text(encoding="utf-8"))
    resolved, problems = checker.resolve(listed)
    assert problems == []

    pragmas = checker.pragma_lines(
        [str(REPO_ROOT / "packages" / "api-core" / "alkera_core" / "files")]
    )
    unlisted = {
        source: sorted(lines - resolved.get(source, set())) for source, lines in pragmas.items()
    }
    assert {k: v for k, v in unlisted.items() if v} == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("src/thing.py", "src/thing.py", id="already-posix"),
        pytest.param("src\\thing.py", "src/thing.py", id="a-windows-coverage-key-is-spelled-posix"),
        pytest.param("packages\\api-core\\x.py", "packages/api-core/x.py", id="nested-windows"),
        pytest.param("  src/./thing.py  ", "src/thing.py", id="trimmed-and-normalised"),
        pytest.param("src/sub/../thing.py", "src/thing.py", id="a-parent-hop-collapses"),
    ],
)
def test_a_path_is_spelled_the_same_way_on_every_host(raw: str, expected: str) -> None:
    """UNCOVERED.md is committed, so its rows and the rows the report tells a
    reader to paste are POSIX paths whichever host measured coverage. On Windows
    `coverage json` keys every file with backslashes and `os.path.normpath` hands
    them straight back — the report then names a path that is in no table and the
    paste-ready row would put a backslash into the document."""
    assert _checker()._normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "root", "expected"),
    [
        pytest.param(
            "C:\\a\\main\\main\\packages\\api-core\\alkera_core\\files\\acl.py",
            "C:/a/main/main",
            "packages/api-core/alkera_core/files/acl.py",
            id="a-windows-absolute-coverage-key",
        ),
        pytest.param(
            "c:\\a\\main\\main\\packages\\api-core\\x.py",
            "C:/a/main/main",
            "packages/api-core/x.py",
            id="the-drive-letter-case-does-not-decide",
        ),
        pytest.param(
            "/home/runner/work/main/main/packages/api-core/x.py",
            "/home/runner/work/main/main",
            "packages/api-core/x.py",
            id="a-posix-absolute-coverage-key",
        ),
        pytest.param(
            "packages/api-core/x.py",
            "/home/runner/work/main/main",
            "packages/api-core/x.py",
            id="an-already-relative-key-is-untouched",
        ),
        pytest.param(
            "D:\\somewhere\\else\\x.py",
            "C:/a/main/main",
            "D:/somewhere/else/x.py",
            id="a-path-outside-the-checkout-keeps-its-own-spelling",
        ),
    ],
)
def test_an_absolute_path_is_keyed_the_way_the_committed_table_spells_it(
    raw: str, root: str, expected: str
) -> None:
    """`coverage json` keys every file absolutely on the Windows runner.

    UNCOVERED.md rows are repo-relative, so an absolute key matches no row:
    every measured gap reads as unreviewed and every reviewed row as stale, and
    the paste-ready row the report prints names `C:/a/main/main/...` — a path
    that could never appear in the committed document. A key that is not under
    this checkout is left alone rather than mangled, so the "coverage measured
    no such file" report still names what it actually saw.
    """
    assert _checker()._normalize(raw, root=root) == expected


def test_the_checkout_root_comes_from_the_script_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make files-coverage` and this suite run the checker from different places.

    Keying on the working directory would make the same coverage report parse
    into different rows depending on where the checker was launched from.
    """
    checker = _checker()
    monkeypatch.chdir(tmp_path)

    assert checker.REPO_ROOT == REPO_ROOT.as_posix()
    absolute = f"{REPO_ROOT.as_posix()}/packages/api-core/alkera_core/files/acl.py"
    assert checker._normalize(absolute) == "packages/api-core/alkera_core/files/acl.py"
