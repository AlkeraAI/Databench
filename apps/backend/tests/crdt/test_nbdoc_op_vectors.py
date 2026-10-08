"""The shared notebook op vectors, run against the platform's live document.

``packages/alkera-notebook/tests/vectors/notebook_ops.json`` is the one
statement of what an op means; the file store runs the same file
(``test_nbfmt_op_vectors.py``). Each case is seeded from a real ``.alknb.py``
file, applied through the sandbox's ``apply_ops`` and read from the live
document itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook import format as fmt
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox import notebook_ops as nbo

pytestmark = [pytest.mark.spread]

VECTORS = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "alkera-notebook"
    / "tests"
    / "vectors"
    / "notebook_ops.json"
)
CASES: list[dict[str, Any]] = json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]
KEY = "org:notebook:vectors"
SEED, WRITER = 4000, 4100


def _file(cells: list[dict[str, Any]]) -> str:
    irs = []
    for cell in cells:
        meta = dict(cell.get("meta") or {})
        code = fmt.render_cell(cell["kind"], cell["source"], meta)
        irs.append(
            fmt.CellIR(
                id=cell["id"],
                kind=cell["kind"],
                name="_",
                source=cell["source"],
                code=code,
                meta=fmt.classify(code)[2] if cell["kind"] in ("sql", "markdown") else {},
            )
        )
    return fmt.write(fmt.NotebookIR(cells=tuple(irs)))


def _round_trips(cell: dict[str, Any]) -> bool:
    read = fmt.read(_file([cell])).cells
    return [(c.id, c.kind, c.source) for c in read if c.id == cell["id"]] == [
        (cell["id"], cell["kind"], cell["source"])
    ]


def _seed(cells: list[dict[str, Any]]) -> tuple[core.DocCache, int]:
    """A live document holding ``cells``, at the log position to write next.

    A file cannot hold every state a live document can (a Python cell whose
    text is exactly the SQL template reads back as a SQL cell), so such a cell
    is seeded holding ``pass`` and given its text with a ``replace``."""
    cache = core.DocCache()
    odd = {c["id"] for c in cells if not _round_trips(c)}
    seeded = [{**c, "source": "pass"} if c["id"] in odd else c for c in cells]
    core.seed(cache, key=KEY, epoch=1, rules=nb.NOTEBOOK, text=_file(seeded), peer=SEED)
    if not odd:
        return cache, 0
    fixes = [
        {"op": "replace", "cell_id": c["id"], "source": c["source"]}
        for c in cells
        if c["id"] in odd
    ]
    prepared = nbo.apply_ops(
        cache, key=KEY, epoch=1, log_seq=0, peer=WRITER - 1, base_vv=None, ops=fixes
    )
    assert prepared is not None and prepared.outcome == "ok"
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=prepared.delta)
    return cache, 1


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_live_document_applies_every_op_vector(case: dict[str, Any]) -> None:
    cache, log_seq = _seed(case["cells"])
    expect = case["expect"]

    def apply() -> Any:
        return nbo.apply_ops(
            cache, key=KEY, epoch=1, log_seq=log_seq, peer=WRITER, base_vv=None, ops=case["ops"]
        )

    if "error" in expect:
        with pytest.raises(nbo.OpError) as refused:
            apply()
        assert (refused.value.code, refused.value.index) == (expect["error"], expect["index"])
        return
    applied = apply()
    assert applied is not None and applied.outcome == "ok"
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq + 1, delta=applied.delta)
    entry = cache.get(KEY, 1, log_seq + 1)
    assert entry is not None
    cells = nb.all_cells(entry.doc)
    named = {c["id"] for c in case["cells"]}
    # The live document as it holds each cell (a file read back would make a
    # Python cell holding the SQL template a SQL cell). The writer's setup cell
    # beside a SQL cell is not one the vectors name.
    got = [
        [cid, cells[cid].kind, cells[cid].source]
        for cid in nb.live_order(entry.doc, cells)
        if cid in named
    ]
    assert got == expect["cells"]


def test_every_refusal_the_vectors_name_is_one_the_platform_and_the_engine_report() -> None:
    """The route's error codes (``alkera_core.notebooks``) and the engine's
    (``alkera_notebook.document.ops``) are the same list, and hold every code
    a vector expects, so a refusal an applier reports always reaches the
    agent as a code it was told about."""
    from alkera_core.notebooks.schemas import OP_ERROR_CODES as ROUTE_CODES
    from alkera_notebook.document.ops import OP_ERROR_CODES as ENGINE_CODES

    expected = {c["expect"]["error"] for c in CASES if "error" in c["expect"]}
    assert set(ROUTE_CODES) == set(ENGINE_CODES)
    assert expected <= set(ROUTE_CODES)
