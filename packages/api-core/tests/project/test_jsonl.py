"""Crash-safe JSONL iteration tests."""

from __future__ import annotations

import json
from pathlib import Path

from alkera_core.project.jsonl import iter_jsonl, tail_jsonl


def test_iter_jsonl_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert list(iter_jsonl(tmp_path / "nonexistent.jsonl")) == []


def test_iter_jsonl_yields_in_order(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text(
        json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2}) + "\n" + json.dumps({"i": 3}) + "\n"
    )
    assert list(iter_jsonl(target)) == [{"i": 1}, {"i": 2}, {"i": 3}]


def test_iter_jsonl_skips_partial_trailing_line(tmp_path: Path) -> None:
    """The classic "writer was killed mid-line" recovery path."""
    target = tmp_path / "log.jsonl"
    target.write_text(
        json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2}) + "\n" + '{"partial":'
        # Note: no trailing newline → marks the last line as partial.
    )
    assert list(iter_jsonl(target)) == [{"i": 1}, {"i": 2}]


def test_iter_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text("\n\n" + json.dumps({"i": 1}) + "\n\n" + json.dumps({"i": 2}) + "\n")
    assert list(iter_jsonl(target)) == [{"i": 1}, {"i": 2}]


def test_iter_jsonl_skips_mid_file_malformed_line(tmp_path: Path) -> None:
    """Internal corruption isn't fatal — we skip and move on. Higher-
    level fold layers can report it."""
    target = tmp_path / "log.jsonl"
    target.write_text(json.dumps({"i": 1}) + "\n" + "garbage\n" + json.dumps({"i": 2}) + "\n")
    assert list(iter_jsonl(target)) == [{"i": 1}, {"i": 2}]


def test_iter_jsonl_skips_non_object_lines(tmp_path: Path) -> None:
    """Convention: JSONL has one object per line. Arrays / strings /
    numbers are valid JSON but get skipped here."""
    target = tmp_path / "log.jsonl"
    target.write_text('{"i": 1}\n[1,2,3]\n42\n"string"\n{"i": 2}\n')
    assert list(iter_jsonl(target)) == [{"i": 1}, {"i": 2}]


def test_tail_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text("\n".join(json.dumps({"i": i}) for i in range(10)) + "\n")
    assert tail_jsonl(target, n=3) == [{"i": 7}, {"i": 8}, {"i": 9}]


def test_tail_jsonl_smaller_than_file(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text(json.dumps({"only": True}) + "\n")
    assert tail_jsonl(target, n=10) == [{"only": True}]
