"""The SQL and Markdown template vectors, run against their owner.

``vectors/notebook_templates.json`` states what each (kind, text, meta)
renders as and what each cell code reads back as. The browser's port of the
templates runs the same file (``apps/web/src/tests/api/realtime/crdt/
notebookOpRules.test.ts``), so the two cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.format.templates import classify, render_cell

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "notebook_templates.json").read_text(encoding="utf-8")
)
RENDER: list[dict[str, Any]] = VECTORS["render"]
CLASSIFY: list[dict[str, Any]] = VECTORS["classify"]


@pytest.mark.parametrize("case", RENDER, ids=[c["name"] for c in RENDER])
def test_a_cell_renders_and_reads_back_as_the_vectors_say(case: dict[str, Any]) -> None:
    code = render_cell(case["kind"], case["source"], case["meta"])
    assert code == case["code"]
    assert list(classify(code)) == case["reads_as"]


@pytest.mark.parametrize("case", CLASSIFY, ids=[c["name"] for c in CLASSIFY])
def test_cell_code_reads_as_the_vectors_say(case: dict[str, Any]) -> None:
    assert list(classify(case["code"])) == case["reads_as"]
