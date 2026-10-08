"""The committed corpus both implementations of the page rule replay.

The browser keeps its own model of where a page opens, so the rule exists
twice. Two copies of one rule drift in silence, and the way a reader finds out
is a chat that pages one way on the server and another in the tab. This pins
the Python side of that corpus: every case's answer is still this rule's
answer, and the committed file is still exactly what the generator writes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alkera_core.objects.transcript_page import PageReach, PageRow, align_boundary

_GENERATOR = Path(__file__).with_name("gen_transcript_page_cases.py")


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_transcript_page_cases", _GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _generator()
CORPUS: dict[str, Any] = json.loads(gen.FIXTURE.read_text(encoding="utf-8"))


def test_the_corpus_covers_the_shapes_that_trap_the_rule() -> None:
    """A corpus that never traps the rule would agree with any mirror at all."""
    cases = CORPUS["cases"]
    assert CORPUS["rule"] == "align_boundary"
    assert len(cases) == CORPUS["generator"]["cases"] >= 300
    assert any(case["expect"]["cut"] for case in cases), "some page must be cut"
    assert any(not case["expect"]["cut"] for case in cases), "and some must not"
    assert any(case["expect"]["seq"] < case["page_low"] for case in cases), "some must descend"
    assert any(case["oldest"] > 1 for case in cases), "a log need not start at sequence 1"
    reannounced = [
        case
        for case in cases
        if len({row[2] for row in case["rows"] if row[3]})
        < len([row for row in case["rows"] if row[3]])
    ]
    assert reannounced, "an opening row is re-announced somewhere in the corpus"


@pytest.mark.parametrize("index", range(len(CORPUS["cases"])))
def test_every_committed_case_is_still_what_the_rule_answers(index: int) -> None:
    case = CORPUS["cases"][index]
    rows = [
        PageRow(seq=seq, starts_turn=turn, message_id=name, opens_message=opens)
        for seq, turn, name, opens in case["rows"]
    ]
    reach = PageReach(**case["reach"])

    answer = align_boundary(rows, page_low=case["page_low"], oldest=case["oldest"], reach=reach)

    assert {"seq": answer.seq, "cut": answer.cut} == case["expect"]


def test_the_committed_file_is_what_the_generator_writes() -> None:
    """Byte for byte, so a hand-edited case cannot quietly bless a regression
    and a re-cut corpus cannot be half-committed."""
    assert gen.render(gen.build()) == gen.FIXTURE.read_text(encoding="utf-8")
