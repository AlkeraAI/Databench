"""The compatibility corpus: every case of every shipped format version.

Cases are discovered from ``format_corpus/v<MAJOR.MINOR>/<case>/``, so a new
case extends the net without touching this file. ``expected.json`` is a
record: when a case fails, the reader or writer changed behaviour, and the fix
is in code (or a migration), never in the expected file.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.format import NotebookIR, analyze, read, write

CORPUS = Path(__file__).resolve().parent / "format_corpus"
CASES = sorted(p for p in CORPUS.glob("v*/*") if (p / "notebook.alknb.py").is_file())
WRITER_PRODUCED = [
    p for p in CASES if json.loads((p / "expected.json").read_text(encoding="utf-8"))["canonical"]
]


def _load(case: Path) -> tuple[str, dict[str, Any], dict[str, str] | None]:
    text = (case / "notebook.alknb.py").read_bytes().decode("utf-8")
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    known_path = case / "known.json"
    known = json.loads(known_path.read_text(encoding="utf-8")) if known_path.exists() else None
    return text, expected, known


def _skip_if_newer_python(expected: Mapping[str, Any]) -> None:
    limit = expected.get("max_python")
    if limit is not None and sys.version_info[:2] > tuple(int(p) for p in limit.split(".")):
        pytest.skip(f"the case pins behaviour of interpreters up to Python {limit}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _observed(ir: NotebookIR) -> dict[str, Any]:
    return {
        "format": ir.format,
        "header_text": ir.header_text,
        "settings": _plain(ir.settings),
        "unknown_settings": ir.unknown_settings,
        "app_config": _plain(ir.app_config),
        "generated_with": ir.generated_with,
        "read_only_reason": ir.read_only_reason,
        "cells": [
            {
                "id": c.id,
                "kind": c.kind,
                "name": c.name,
                "source": c.source,
                "code": c.code,
                "config": _plain(c.config),
                "meta": _plain(c.meta),
                "extra": _plain(c.extra),
                "resolution": c.resolution,
            }
            for c in ir.cells
        ],
        "violations": [[v.code, v.line] for v in ir.violations],
    }


def _case_id(case: Path) -> str:
    return f"{case.parent.name}/{case.name}"


def test_the_corpus_is_not_empty() -> None:
    assert len(CASES) >= 30
    assert len(WRITER_PRODUCED) >= 10


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_read_matches_the_record(case: Path) -> None:
    text, expected, known = _load(case)
    _skip_if_newer_python(expected)
    assert _observed(read(text, known=known)) == expected["notebook"]


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_graph_matches_the_record(case: Path) -> None:
    text, expected, known = _load(case)
    _skip_if_newer_python(expected)
    graph = analyze(read(text, known=known))
    assert json.loads(json.dumps(graph)) == expected["graph"]


@pytest.mark.parametrize("case", WRITER_PRODUCED, ids=_case_id)
def test_writer_produced_cases_round_trip_byte_for_byte(case: Path) -> None:
    text, expected, known = _load(case)
    _skip_if_newer_python(expected)
    assert write(read(text, known=known)) == text


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_every_case_reaches_a_fixed_point_without_losing_code(case: Path) -> None:
    text, expected, known = _load(case)
    _skip_if_newer_python(expected)
    first = read(text, known=known)
    once = write(first)
    second = read(once, known=known)
    assert write(second) == once
    assert [c.code for c in second.cells] == [c.code for c in first.cells]
    assert [c.id for c in second.cells] == [c.id for c in first.cells]
    # Kept cells survive byte for byte inside the written file.
    for cell in first.cells:
        verbatim = cell.meta.get("verbatim")
        if isinstance(verbatim, str):
            body = verbatim.split("\n", 1)[-1]
            assert body in once


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_a_written_file_carries_an_id_on_every_cell_it_can(case: Path) -> None:
    text, expected, known = _load(case)
    _skip_if_newer_python(expected)
    ir = read(text, known=known)
    reread = read(write(ir), known=None)
    for before, after in zip(ir.cells, reread.cells, strict=True):
        # A stray statement (or a setup block after cells) cannot carry a
        # keyword; every cell form can.
        if before.meta.get("stray"):
            continue
        assert after.resolution == "keyword", before
        assert after.id == before.id
