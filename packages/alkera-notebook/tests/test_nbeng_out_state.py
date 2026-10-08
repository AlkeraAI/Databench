"""Cell output state and provenance hashes."""

from __future__ import annotations

import hashlib

import pytest
from alkera_notebook.engine.config import OutputLimits
from alkera_notebook.engine.models import ErrorInfo, RunActor
from alkera_notebook.outputs import CellOutputs, RunMeta, code_hash, lineage_hashes

LIMITS = OutputLimits()
ANA = RunActor(kind="person", id="u-ana", display_name="Ana")
BOT = RunActor(kind="agent", id="a-bot", display_name="Agent")


def _fresh() -> CellOutputs:
    out = CellOutputs()
    out.begin_run(
        RunMeta("run_1", "run", ANA), code_hash="h", lineage_hash="L", env_fingerprint="E"
    )
    return out


def test_code_hash_is_marimos_md5() -> None:
    assert code_hash("x = 1") == hashlib.md5(b"x = 1").hexdigest()
    assert code_hash("é") == hashlib.md5("é".encode()).hexdigest()


def _sha(*parts: str) -> str:
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def test_lineage_is_code_hash_then_sorted_parent_lineages() -> None:
    cells = [("a", "a = 1"), ("b", "b = 2"), ("c", "c = a + b")]
    got = lineage_hashes(cells, [("a", "c"), ("b", "c")])
    la, lb = _sha(code_hash("a = 1")), _sha(code_hash("b = 2"))
    assert got["a"] == la and got["b"] == lb
    assert got["c"] == _sha(code_hash("c = a + b"), *sorted([la, lb]))


@pytest.mark.parametrize(
    ("edit", "changed"),
    [
        pytest.param("a", {"a", "b", "c"}, id="root_edit_reaches_all_descendants"),
        pytest.param("b", {"b", "c"}, id="middle_edit_spares_its_ancestor"),
        pytest.param("d", {"d"}, id="unrelated_edit_touches_only_itself"),
    ],
)
def test_lineage_changes_exactly_downstream(edit: str, changed: set[str]) -> None:
    base = {"a": "a = 1", "b": "b = a", "c": "c = b", "d": "d = 0"}
    edges = [("a", "b"), ("b", "c")]
    before = lineage_hashes(list(base.items()), edges)
    after = lineage_hashes(
        [(k, v + " # edited" if k == edit else v) for k, v in base.items()], edges
    )
    assert {k for k in base if before[k] != after[k]} == changed


def test_lineage_cycle_terminates_and_is_order_independent() -> None:
    cells = [("a", "a = c"), ("b", "b = a"), ("c", "c = b"), ("d", "d = c")]
    edges = [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")]
    one = lineage_hashes(cells, edges)
    two = lineage_hashes(list(reversed(cells)), list(reversed(edges)))
    assert one == two
    assert len({one["a"], one["b"], one["c"]}) == 3
    # The cell below the cycle still changes when the cycle's code does.
    edited = lineage_hashes([("a", "a = c + 1"), *cells[1:]], edges)
    assert edited["d"] != one["d"]


def test_lineage_ignores_edges_to_unknown_cells_and_self_loops() -> None:
    plain = lineage_hashes([("a", "a = 1")], [])
    assert lineage_hashes([("a", "a = 1")], [("a", "a"), ("ghost", "a")]) == plain


def test_lineage_long_chain_does_not_recurse() -> None:
    n = 5000
    cells = [(f"c{i}", f"x{i} = {i}") for i in range(n)]
    edges = [(f"c{i}", f"c{i + 1}") for i in range(n - 1)]
    assert len(lineage_hashes(cells, edges)) == n


def test_replace_and_append_outputs() -> None:
    out = _fresh()
    out.apply_output({"text/plain": "1"}, "append", LIMITS)
    out.apply_output({"text/html": "<b>2</b>"}, "append", LIMITS)
    assert [b.get("text/plain") for b in out.bundles] == ["1", "<html>"]
    out.apply_output({"text/plain": "3"}, "replace", LIMITS)
    assert out.bundles == [{"text/plain": "3"}]


def test_structured_output_over_cap_becomes_placeholder_not_truncated() -> None:
    limits = OutputLimits(rich_bytes_per_cell=1000)
    out = _fresh()
    spec = {"data": ["v" * 2000]}
    stored = out.apply_output({"application/vnd.vegalite.v5+json": spec}, "append", limits)
    assert "application/vnd.vegalite.v5+json" not in stored
    assert "too large" in stored["text/plain"]
    # The cap counts every bundle the cell holds.
    out2 = _fresh()
    out2.apply_output({"text/plain": "a" * 600}, "append", limits)
    second = out2.apply_output({"text/plain": "b" * 600}, "append", limits)
    assert "too large" in second["text/plain"]
    # Replace frees the earlier bundles' share.
    third = out2.apply_output({"text/plain": "c" * 600}, "replace", limits)
    assert third == {"text/plain": "c" * 600}


def test_streams_merge_consecutive_same_name() -> None:
    out = _fresh()
    out.apply_stream("stdout", "a", LIMITS)
    out.apply_stream("stdout", "b\n", LIMITS)
    out.apply_stream("stderr", "warn\n", LIMITS)
    out.apply_stream("stdout", "c", LIMITS)
    assert [(i.name, i.text) for i in out.console] == [
        ("stdout", "ab\n"),
        ("stderr", "warn\n"),
        ("stdout", "c"),
    ]


def test_stream_cap_keeps_head_and_tail_with_note() -> None:
    out = _fresh()
    head = "H" * (64 * 1024)
    for _ in range(40):
        out.apply_stream("stdout", head if not out.console else "m" * 32 * 1024, LIMITS)
    out.apply_stream("stdout", "THE END", LIMITS)
    text = "".join(i.text for i in out.console)
    assert text.startswith(head)
    assert text.endswith("THE END")
    notes = [i for i in out.console if "omitted" in i.text]
    assert len(notes) == 1
    kept = sum(len(i.text.encode()) for i in out.console if i is not notes[0])
    assert kept == LIMITS.stream_bytes_per_cell
    total = 64 * 1024 + 39 * 32 * 1024 + len("THE END")
    assert f"[... {total - kept} bytes of output omitted ...]" in notes[0].text
    assert out.summary().truncated


def test_finished_error_and_begin_run_clears() -> None:
    out = _fresh()
    out.apply_output({"text/plain": "x"}, "append", LIMITS)
    out.apply_finished(ErrorInfo(ename="ValueError", evalue="bad"), "error")
    assert out.error is not None and out.run is not None and out.run.status == "error"
    out.begin_run(
        RunMeta("run_2", "autorun", BOT), code_hash="h2", lineage_hash=None, env_fingerprint=None
    )
    assert (
        out.bundles == [] and out.error is None and out.code_hash == "h2" and out.origin == "kernel"
    )


def test_summary_flags_and_detail_parts() -> None:
    out = _fresh()
    out.apply_stream("stdout", "log line\n", LIMITS)
    out.apply_output({"text/plain": "<Figure>", "image/png": "iVBORw0KGgo="}, "append", LIMITS)
    out.apply_output({"application/vnd.alkera.chart+json": {"mark": "bar"}}, "append", LIMITS)
    out.apply_output(
        {"application/vnd.alkera.table+json": {"rows": [[1]], "total_rows": 1}}, "append", LIMITS
    )
    out.apply_output(
        {"application/vnd.jupyter.widget-view+json": {"model_id": "m1"}}, "append", LIMITS
    )
    s = out.summary()
    assert (s.has_image, s.has_chart, s.has_table, s.has_widget) == (True, True, True, True)
    assert s.kinds[0] == "text/plain" and "stream" in s.kinds
    assert "log line" in s.text and "<Figure>" in s.text

    assert out.detail("image").images == ["iVBORw0KGgo="]
    assert out.detail("image").text == "" and out.detail("image").chart_spec is None
    assert out.detail("chart").chart_spec == {"mark": "bar"}
    assert out.detail("table").table == {"rows": [[1]], "total_rows": 1}
    assert out.detail("widget").widgets == [{"model_id": "m1"}]
    full = out.detail("all", max_chars=5)
    assert full.text == "log l" and full.truncated
    assert full.run is not None and full.run.by == ANA


def test_summary_of_plain_quiet_cell_has_no_flags() -> None:
    out = _fresh()
    out.apply_output({"text/plain": "42"}, "append", LIMITS)
    s = out.summary()
    assert not (s.has_image or s.has_chart or s.has_table or s.has_widget or s.truncated)
    assert s.text == "42\n"
