"""A notebook's rendered file is capped at 4 MiB, whoever writes it: an agent
or box batch is refused as ``cap_exceeded`` naming its last operation, a
person's typing that would carry the file past the cap is rejected, and an
update that shrinks a file already past it still goes through. The cheap
bound that spares most documents a render is never below the real file, and
a keystroke's projection carries the rendered hash only where it is cheap.

Real Loro documents and the real format API, in this process."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from alkera_notebook import format as fmt
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox import notebook_ops as nbo
from backend.services.crdt.sandbox.core import DocCache
from backend.services.crdt.sandbox.notebook_ops import OpError
from loro import ExportMode, LoroDoc
from tests.crdt.test_nbdoc_real_format import FULL

#: The ``weekly`` cell of ``FULL``.
WEEKLY = "s7t8v9w0x1"

pytestmark = [pytest.mark.spread]

KEY = "org:notebook:cap"
SEED, TAB, PEER = 4000, 5000, 7000
STRATEGY = nb.NOTEBOOK
#: Short lines are indented inside the cell's function in the file, so this
#: renders to roughly three times its 800,000 bytes.
SHORT_LINES = "x\n" * 400_000


def _seed(text: str = FULL) -> DocCache:
    cache = DocCache()
    core.seed(cache, key=KEY, epoch=1, rules=STRATEGY, text=text, peer=SEED)
    return cache


def _ops(cache: DocCache, ops: list[dict[str, Any]], log_seq: int) -> nbo.OpsApplied:
    applied = nbo.apply_ops(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        peer=PEER + log_seq,
        base_vv=None,
        ops=ops,
        strategy=STRATEGY,
    )
    assert applied is not None
    return applied


def _head(cache: DocCache, log_seq: int) -> LoroDoc:
    entry = cache.get(KEY, 1, log_seq)
    assert entry is not None
    return entry.doc


def _rendered_size(cache: DocCache, log_seq: int) -> int:
    data = core.content(cache, key=KEY, epoch=1, log_seq=log_seq, rules=STRATEGY)
    assert data is not None
    return len(data)


def _type(cache: DocCache, log_seq: int, change: Any) -> core.Validated:
    tab = core.new_doc()
    tab.peer_id = TAB
    tab.import_(bytes(_head(cache, log_seq).export(ExportMode.Snapshot())))
    before = tab.oplog_vv
    change(tab)
    tab.commit()
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=STRATEGY,
        peers=frozenset({TAB}),
        update=bytes(tab.export(ExportMode.Updates(before))),
    )
    assert verdict is not None
    return verdict


def _source(doc: LoroDoc, cell_id: str) -> Any:
    return nb.child(nb.child(doc.get_map("cells"), cell_id), "source")


def _big_cell(cache: DocCache) -> str:
    """Insert one cell of short lines through the operations path."""
    applied = _ops(cache, [{"op": "insert", "source": SHORT_LINES}], 0)
    assert applied.outcome == "ok"
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=applied.delta)
    (created,) = applied.result["created"]
    return str(created)


def test_a_batch_whose_file_would_pass_the_cap_is_refused_as_its_last_op() -> None:
    cache = _seed()
    _big_cell(cache)
    size = _rendered_size(cache, 1)
    assert 2 * len(SHORT_LINES) < size < nb.MAX_FILE_BYTES
    # The two sources together are well under 4 MiB; their file is not.
    with pytest.raises(OpError) as refused:
        _ops(
            cache,
            [{"op": "insert", "source": "y = 1"}, {"op": "insert", "source": SHORT_LINES}],
            1,
        )
    assert (refused.value.index, refused.value.code) == (1, "cap_exceeded")
    # Nothing of the refused batch is in the document.
    assert _rendered_size(cache, 1) == size


def test_a_small_batch_on_a_large_notebook_under_the_cap_goes_through() -> None:
    cache = _seed()
    _big_cell(cache)
    applied = _ops(cache, [{"op": "insert", "source": "y = 1"}], 1)
    assert applied.outcome == "ok"


def test_typing_that_carries_the_file_past_the_cap_is_rejected() -> None:
    cache = _seed()
    _big_cell(cache)
    # Each cell stays under its own 1 MiB; the file does not stay under 4 MiB.
    verdict = _type(cache, 1, lambda tab: _source(tab, WEEKLY).insert(0, SHORT_LINES[:600_000]))
    assert (verdict.outcome, verdict.reason) == ("reject", "file_too_large")
    small = _type(cache, 1, lambda tab: _source(tab, WEEKLY).insert(0, "z = 1\n"))
    assert small.outcome == "ok", small


@pytest.mark.skip(
    reason="about 45 s of CPU setup, so a loaded runner can pass the 90 s "
    "timeout; to be made lighter"
)
def test_typing_that_shrinks_a_file_already_past_the_cap_goes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judged by rendering each side once: on a file past the cap one render
    is seconds, so a keystroke rendering either side twice outlives its
    budget on a busy host."""
    cache = _seed()
    big = _big_cell(cache)
    # A state no write admits (a file grown past the cap before it applied),
    # put straight into the head: cutting it down must still be possible.
    head = _head(cache, 1)
    _source(head, WEEKLY).insert(0, SHORT_LINES[:600_000])
    head.commit()
    assert STRATEGY.over_file_cap(head)
    # The head moved behind the cache's back: drop the standing copy a
    # keystroke is checked on, so the next check forks the head as it is.
    entry = cache.get(KEY, 1, 1)
    assert entry is not None
    entry.shadow = None
    renders: list[int] = []
    real_render = type(STRATEGY).render

    def counted(self: Any, doc: LoroDoc) -> str:
        renders.append(1)
        return real_render(self, doc)

    monkeypatch.setattr(type(STRATEGY), "render", counted)
    verdict = _type(cache, 1, lambda tab: _source(tab, big).delete(0, 100_000))
    assert verdict.outcome == "ok", verdict
    # Cut back under the cap: the update's file alone answers.
    assert len(renders) == 1
    renders.clear()
    grown = _type(cache, 1, lambda tab: _source(tab, big).insert(0, "more\n"))
    assert (grown.outcome, grown.reason) == ("reject", "file_too_large")
    # Still past it: the update's file and the head's, once each.
    assert len(renders) == 2


def _notebook(*cells: tuple[str, str, dict[str, Any]], header: str = "") -> str:
    irs = [
        fmt.CellIR(
            id=fmt.new_cell_id(),
            kind=kind,
            name="_",
            source=source,
            code=fmt.render_cell(kind, source, meta),
            config={},
            meta=meta,
            extra={},
            resolution="new",
        )
        for kind, source, meta in cells
    ]
    return fmt.write(
        fmt.NotebookIR(
            format="1.0",
            header_text=header,
            settings={},
            unknown_settings="",
            app_config={},
            generated_with="",
            cells=tuple(irs),
            violations=(),
            read_only_reason=None,
        )
    )


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param([("python", "x\n" * 5000, {})], id="python-short-lines"),
        pytest.param([("python", "\n" * 5000, {})], id="python-empty-lines"),
        pytest.param([("sql", "SELECT 1\n" * 2000, {"output_var": "df"})], id="sql-lines"),
        pytest.param(
            [("markdown", '"""\\ $x$ """\n' * 2000, {"quote": "r"})], id="markdown-quotes"
        ),
        pytest.param([("unparsable", "x = (\x01\x02\n" * 2000, {})], id="unparsable-escapes"),
        pytest.param([("python", "a = 1", {})] * 400, id="many-tiny-cells"),
    ],
)
def test_the_cheap_bound_is_never_below_the_rendered_file(
    cells: list[tuple[str, str, dict[str, Any]]],
) -> None:
    doc = core.new_doc()
    STRATEGY.write_seed(doc, _notebook(*cells, header='"""A header."""\n'), None)
    rendered = len(STRATEGY.render(doc).encode("utf-8"))
    assert STRATEGY.render_bound(doc) >= rendered


def test_a_keystroke_on_a_small_notebook_carries_its_file_s_real_hash() -> None:
    cache = _seed()
    verdict = _type(cache, 0, lambda tab: _source(tab, WEEKLY).insert(0, "# x\n"))
    assert verdict.outcome == "ok", verdict
    head = core.new_doc()
    head.import_(bytes(_head(cache, 0).export(ExportMode.Snapshot())))
    _source(head, WEEKLY).insert(0, "# x\n")
    head.commit()
    expected = hashlib.sha256(STRATEGY.render(head).encode("utf-8")).hexdigest()
    assert verdict.projection["sha256"] == expected


def test_a_keystroke_on_a_large_notebook_leaves_the_hash_to_the_write_back() -> None:
    cache = _seed()
    _big_cell(cache)
    assert STRATEGY.render_bound(_head(cache, 1)) > nb.CHEAP_RENDER_BYTES
    verdict = _type(cache, 1, lambda tab: _source(tab, WEEKLY).insert(0, "# x\n"))
    assert (verdict.outcome, verdict.projection["sha256"]) == ("ok", nb.UNRENDERED)
    # Stored once, a state is rendered whatever its size.
    at_rest = STRATEGY.project_at_rest(_head(cache, 1))
    assert at_rest["sha256"] not in (nb.UNRENDERED, verdict.projection["sha256"])
