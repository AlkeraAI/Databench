"""Hold the Files coverage gap list honest.

`make files-coverage` enforces a 95% floor, but a floor alone lets gaps
accumulate silently under it. This checker closes that: every line and every
branch arc the Files suite does not execute must appear in
`packages/api-core/tests/files/UNCOVERED.md`, with a written reason, and every
`# pragma: no cover` in the Files sources must be listed there too, a pragma
is an exclusion from the measurement, so an unlisted one is a gap that does not
even show up as a gap.

A row is keyed by **file + the stripped text of the line** (a code excerpt),
never by a line number: an edit anywhere above a gap used to stale every row
below it, which made the table churn on changes that touched none of the gaps.
The excerpt is resolved to its current line(s) at check time; when a file holds
the same line twice, the row names the occurrence with a `(#2)` suffix.

It fails in both directions on purpose:

* an uncovered (or pragma-excluded) line that is NOT in the table, a new gap
  nobody reviewed; the message prints the row to paste;
* a row whose excerpt is gone, or whose line is now covered and carries no
  pragma, a stale entry. Without this half the table rots into a permanent
  allowlist and the next real gap hides behind an obsolete row.

Usage::

    python scripts/check_files_uncovered.py .coverage-files.json \
        packages/api-core/tests/files/UNCOVERED.md [--root DIR ...]
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

#: Where the Files sources live. Scanned for `# pragma: no cover`; entries that
#: do not exist yet (the backend/worker halves) are skipped.
DEFAULT_ROOTS: tuple[str, ...] = (
    "packages/api-core/alkera_core/files",
    "apps/backend/backend/api/routes/files",
    "apps/backend/backend/services/files",
    "apps/worker/worker/tasks",
)

PRAGMA = re.compile(r"#\s*pragma:\s*no cover")
_ROW = re.compile(r"^\s*\|(?P<cells>.*)\|\s*$")
#: One excerpt in the second cell: a backtick-quoted code span, optionally
#: followed by `(#2)` naming which occurrence of a repeated line is meant.
_EXCERPT = re.compile(r"`(?P<code>[^`]*)`(?:\s*\(#(?P<occurrence>\d+)\))?")


class TableError(Exception):
    """The UNCOVERED.md table cannot be parsed."""


@dataclass(frozen=True)
class Excerpt:
    """One `file + code text` key, as written in a row."""

    code: str
    occurrence: int = 1
    #: True when the row (or the suggestion) spells the occurrence out, which
    #: it must whenever the same text appears more than once in the file.
    explicit: bool = False

    def cell(self) -> str:
        suffix = f" (#{self.occurrence})" if self.explicit or self.occurrence > 1 else ""
        return f"`{self.code}`{suffix}"


#: This checkout's root: `scripts/` is a direct child of it. Read from the
#: script's own location rather than the working directory so an absolute
#: coverage key resolves the same way whatever the caller `cd`-ed to.
REPO_ROOT: str = Path(__file__).resolve().parent.parent.as_posix()


def _normalize(raw: str, *, root: str = REPO_ROOT) -> str:
    """One spelling of a path: `/`-separated and repo-relative, on every platform.

    UNCOVERED.md is a committed document and the rows this prints for a reader
    to paste go straight into it, so both sides are POSIX paths no matter which
    host ran coverage. `os.path.normpath` alone is the host's flavour, on
    Windows it hands back `src\\thing.py`, and the report then names a path that
    is in no table. A literal backslash is taken as a separator here, which a
    source file under this checker never has.

    Absolute keys get the same treatment: coverage on the Windows runner writes
    `C:\\a\\main\\main\\packages\\...`, which spells a file the table names but
    matches no row in it, so every gap reads as unlisted and every row as
    stale. The drive letter is compared case-blind because Windows spells the
    same drive both ways and the paths are the same file either way.
    """
    spelled = posixpath.normpath(raw.strip().replace("\\", "/"))
    base = posixpath.normpath(root.strip().replace("\\", "/"))
    if base in {"", "."}:
        return spelled
    for value, prefix in ((spelled, base), (spelled.lower(), base.lower())):
        if value.startswith(f"{prefix}/"):
            return spelled[len(prefix) + 1 :]
    return spelled


def parse_excerpts(spec: str) -> list[Excerpt]:
    """Parse an `excerpt` cell: one or more backtick-quoted code spans.

    Backticks delimit each excerpt so code containing commas, or pipes escaped
    as `\\|`, needs no further quoting.
    """
    out: list[Excerpt] = []
    consumed = 0
    for match in _EXCERPT.finditer(spec):
        between = spec[consumed : match.start()].strip().strip(",")
        if between:
            raise TableError(f"unquoted text {between!r} in excerpt cell {spec!r}")
        consumed = match.end()
        raw_occurrence = match.group("occurrence")
        occurrence = int(raw_occurrence) if raw_occurrence else 1
        if occurrence < 1:
            raise TableError(f"occurrence must be 1-based in {spec!r}")
        out.append(
            Excerpt(
                code=match.group("code").replace("\\|", "|"),
                occurrence=occurrence,
                explicit=raw_occurrence is not None,
            )
        )
    trailing = spec[consumed:].strip().strip(",")
    if trailing:
        raise TableError(f"unquoted text {trailing!r} in excerpt cell {spec!r}")
    if not out:
        raise TableError(f"excerpt cell {spec!r} names no `code` excerpt")
    return out


def parse_table(text: str) -> dict[str, list[Excerpt]]:
    """Read the `| path | excerpt | reason |` table out of UNCOVERED.md."""
    listed: dict[str, list[Excerpt]] = {}
    for raw in text.splitlines():
        match = _ROW.match(raw)
        if match is None:
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", match.group("cells"))]
        if len(cells) < 3:
            continue
        path_cell, excerpt_cell, reason = cells[0], cells[1], cells[2]
        if path_cell.lower() == "path" or set(path_cell) <= {"-", ":"}:
            continue  # header / separator
        if not reason:
            raise TableError(f"row for {path_cell!r} has no reason")
        listed.setdefault(_normalize(path_cell), []).extend(parse_excerpts(excerpt_cell))
    return listed


def source_lines(path: str) -> list[str] | None:
    """The stripped text of every line of `path`, or None when it is gone."""
    target = Path(path)
    if not target.is_file():
        return None
    return [line.strip() for line in target.read_text(encoding="utf-8").splitlines()]


def excerpt_for(stripped: list[str], line: int) -> Excerpt:
    """The row key naming `line`: its text, plus which occurrence it is."""
    code = stripped[line - 1] if 0 < line <= len(stripped) else ""
    occurrence = sum(1 for text in stripped[:line] if text == code)
    repeated = sum(1 for text in stripped if text == code) > 1
    return Excerpt(code=code, occurrence=max(occurrence, 1), explicit=repeated)


def resolve(
    listed: dict[str, list[Excerpt]],
) -> tuple[dict[str, set[int]], list[str]]:
    """Map every row onto the line it names today; report the rows that no longer resolve."""
    resolved: dict[str, set[int]] = {}
    problems: list[str] = []
    for path, excerpts in sorted(listed.items()):
        stripped = source_lines(path)
        if stripped is None:
            resolved[path] = set()
            continue  # reported by check() as a file coverage never measured
        for excerpt in excerpts:
            hits = [number for number, text in enumerate(stripped, 1) if text == excerpt.code]
            if not hits:
                problems.append(
                    f"{path}: excerpt {excerpt.cell()} is not in the file any more; "
                    "the row is stale, drop it or re-key it to the line that replaced it"
                )
                continue
            if len(hits) > 1 and not excerpt.explicit:
                problems.append(
                    f"{path}: excerpt `{excerpt.code}` occurs {len(hits)} times "
                    f"(lines {', '.join(str(hit) for hit in hits)}); "
                    "name the occurrence, e.g. `…` (#2)"
                )
                continue
            if excerpt.occurrence > len(hits):
                problems.append(
                    f"{path}: excerpt `{excerpt.code}` occurs only {len(hits)} time(s), "
                    f"but the row names occurrence #{excerpt.occurrence}"
                )
                continue
            resolved.setdefault(path, set()).add(hits[excerpt.occurrence - 1])
    return resolved, problems


def uncovered_from_report(report: dict[str, object]) -> dict[str, set[int]]:
    """Every line coverage could not prove ran, keyed by file.

    A missing branch arc is anchored at its source line: that is the line whose
    *other* outcome was never taken, and the line a reader has to reason about.
    """
    files = report.get("files")
    if not isinstance(files, dict):
        raise TableError("coverage JSON has no 'files' object")
    out: dict[str, set[int]] = {}
    for raw_path, raw_data in files.items():
        data = raw_data if isinstance(raw_data, dict) else {}
        gaps: set[int] = set()
        missing = data.get("missing_lines")
        if isinstance(missing, list):
            gaps.update(int(line) for line in missing)
        branches = data.get("missing_branches")
        if isinstance(branches, list):
            for arc in branches:
                if isinstance(arc, list) and arc:
                    gaps.add(int(arc[0]))
        out[_normalize(str(raw_path))] = gaps
    return out


def iter_sources(roots: Iterable[str]) -> Iterator[Path]:
    for root in roots:
        base = Path(root)
        if base.is_file() and base.suffix == ".py":
            yield base
        elif base.is_dir():
            yield from sorted(base.rglob("*.py"))


def pragma_lines(roots: Iterable[str]) -> dict[str, set[int]]:
    """`# pragma: no cover` sites in the Files sources, keyed by file."""
    out: dict[str, set[int]] = {}
    for source in iter_sources(roots):
        hits = {
            number
            for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
            if PRAGMA.search(line)
        }
        if hits:
            out[_normalize(str(source))] = hits
    return out


def check(
    report: dict[str, object],
    table_text: str,
    roots: Iterable[str],
) -> list[str]:
    """Return the problems; an empty list means the gap list is honest."""
    listed = parse_table(table_text)
    resolved, problems = resolve(listed)
    uncovered = uncovered_from_report(report)
    pragmas = pragma_lines(roots)

    measured = set(uncovered) | set(pragmas)
    for source in sorted(measured):
        gaps = uncovered.get(source, set()) | pragmas.get(source, set())
        unlisted = sorted(gaps - resolved.get(source, set()))
        if not unlisted:
            continue
        stripped = source_lines(source) or []
        for line in unlisted:
            why = "pragma: no cover" if line in pragmas.get(source, set()) else "uncovered"
            row = f"| {source} | {excerpt_for(stripped, line).cell()} | <reason> |"
            problems.append(
                f"{source}:{line} is {why} but is not listed in UNCOVERED.md; add: {row}"
            )

    for source in sorted(listed):
        if source not in measured:
            problems.append(
                f"{source} is listed in UNCOVERED.md but coverage measured no such file"
            )
            continue
        covered_now = (
            resolved.get(source, set()) - uncovered.get(source, set()) - pragmas.get(source, set())
        )
        stripped = source_lines(source) or []
        for line in sorted(covered_now):
            problems.append(
                f"{source}:{line} {excerpt_for(stripped, line).cell()} is listed in "
                "UNCOVERED.md but is now covered; drop the row"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Files coverage gap list.")
    parser.add_argument("report", type=Path, help="coverage JSON (--cov-report=json:...)")
    parser.add_argument("table", type=Path, help="UNCOVERED.md")
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="Files source root to scan for pragmas (repeatable).",
    )
    args = parser.parse_args(argv)
    roots: list[str] = args.root if args.root else list(DEFAULT_ROOTS)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    try:
        problems = check(report, args.table.read_text(encoding="utf-8"), roots)
    except TableError as exc:
        print(f"UNCOVERED.md is malformed: {exc}")
        return 1

    if problems:
        print(f"Files coverage gap list is out of date ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Files coverage gap list is honest ({args.table}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
