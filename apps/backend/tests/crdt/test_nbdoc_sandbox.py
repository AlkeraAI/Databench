"""The sandbox's ``notebook`` strategy: what the document admits, how a file
becomes the document and back, how an outside change merges, normal form,
and the operations agents and boxes send.

Real Loro documents play the tabs, in this process. The format API is the
test stand-in (``nbdoc_fmt_standin``) until the real one lands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import loro
import pytest
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox import notebook_ops as nbo
from backend.services.crdt.sandbox.core import DocCache, SandboxError
from loro import EphemeralStore, ExportMode, LoroDoc, LoroMap, LoroText, Side
from tests.crdt import nbdoc_fmt_standin as fmt

pytestmark = [pytest.mark.spread]

KEY = "org:notebook:node-1"
SEED = 4000
MERGE = 4001
AGENT = 4002
TAB = 5000
OTHER_TAB = 5001

A, B, C, D = "a1b2c3d4e5", "f6g7h8j9k0", "m2n3p4q5r6", "s7t8v9w0x1"


#: The notebook strategy reading and writing through the test stand-in.
STANDIN = nb.NotebookStrategy(load=lambda: fmt)


def _no_format() -> object:
    raise SandboxError("format_unavailable", "no format API here")


#: The strategy on an interpreter that cannot import the format API.
NO_FORMAT = nb.NotebookStrategy(load=_no_format)


def cell(
    cell_id: str,
    source: str,
    *,
    kind: str = "python",
    name: str = "_",
    config: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> fmt.CellIR:
    return fmt.CellIR(
        id=cell_id,
        kind=kind,
        name=name,
        source=source,
        code=source,
        config=config or {},
        meta=meta or {},
        extra={},
        resolution="keyword",
    )


def nb_text(
    *cells: fmt.CellIR, settings: dict[str, Any] | None = None, fmt_version: str = "1.0"
) -> str:
    return fmt.write(
        fmt.NotebookIR(
            format=fmt_version,
            header_text='"""A notebook."""',
            settings=settings or {"reactivity": "autorun"},
            unknown_settings="",
            app_config={"width": "medium"},
            generated_with="0.25.1",
            cells=cells,
        )
    )


def strip_ids(text: str) -> str:
    """The file as a stock editor saves it: no ids on the cells."""
    return "\n".join(
        " ".join(p for p in line.split(" ") if not p.startswith("id="))
        if line.startswith("#cell ")
        else line
        for line in text.split("\n")
    )


def _seed(text: str) -> tuple[DocCache, core.Seeded]:
    cache = DocCache()
    return cache, core.seed(cache, key=KEY, epoch=1, rules=STANDIN, text=text, peer=SEED)


def _tab(snapshot: bytes, peer: int = TAB) -> LoroDoc:
    doc = core.new_doc()
    doc.peer_id = peer
    doc.import_(snapshot)
    return doc


def _update(tab: LoroDoc, change: Callable[[LoroDoc], object]) -> bytes:
    before = tab.oplog_vv
    change(tab)
    tab.commit()
    return bytes(tab.export(ExportMode.Updates(before)))


def _validate(cache: DocCache, update: bytes, *, log_seq: int = 0) -> core.Validated:
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=STANDIN,
        peers=frozenset({TAB, OTHER_TAB}),
        update=update,
    )
    assert verdict is not None
    return verdict


def _commit(cache: DocCache, delta: bytes, log_seq: int) -> None:
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq, delta=delta)


def _rendered(cache: DocCache, log_seq: int) -> str:
    data = core.content(cache, key=KEY, epoch=1, log_seq=log_seq, rules=STANDIN)
    assert data is not None
    return data.decode("utf-8")


def _cells(cache: DocCache, log_seq: int) -> list[fmt.CellIR]:
    return list(fmt.read(_rendered(cache, log_seq)).cells)


def cmap(doc: LoroDoc, cell_id: str) -> LoroMap:
    found = nb.child(doc.get_map("cells"), cell_id)
    assert isinstance(found, LoroMap)
    return found


def source(doc: LoroDoc, cell_id: str) -> LoroText:
    found = nb.child(cmap(doc, cell_id), "source")
    assert isinstance(found, LoroText)
    return found


TWO = nb_text(cell(A, "x = 1"), cell(B, "y = x + 1"))


# ---------------------------------------------------------------------------
# Seed and render
# ---------------------------------------------------------------------------


def test_a_seeded_notebook_renders_back_to_the_file_it_was_read_from() -> None:
    text = nb_text(
        cell(A, "import polars as pl", kind="setup"),
        cell(
            B, "SELECT 1", kind="sql", name="q", meta={"output_var": "q", "connection": "Warehouse"}
        ),
        cell(C, "q.head()", config={"hide_code": True}),
        settings={"reactivity": "lazy", "dataframe": "polars"},
    )
    cache, _ = _seed(text)
    assert _rendered(cache, 0) == text


def test_the_document_holds_each_cell_under_its_id_with_a_text_source() -> None:
    _, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    assert nb.raw_order(tab) == [A, B]
    assert str(source(tab, B).to_string()) == "y = x + 1"
    assert nb.child(cmap(tab, A), "kind") == "python"
    assert nb.child(cmap(tab, A), "deleted") is False


def test_a_file_in_a_newer_major_format_is_refused_as_not_editable() -> None:
    with pytest.raises(SandboxError) as refused:
        _seed(nb_text(cell(A, "x = 1"), fmt_version="2.0"))
    assert refused.value.code == "not_editable"
    assert "newer_format" in refused.value.message


def test_the_projection_is_cheap_counts_and_the_rendered_files_hash() -> None:
    import hashlib

    cache, seeded = _seed(nb_text(cell(A, "x = 1"), cell(B, "# hi", kind="markdown")))
    assert seeded.projection["cells"] == 2
    assert seeded.projection["kinds"] == {"python": 1, "markdown": 1}
    assert seeded.projection["source_bytes"] == len("x = 1") + len("# hi")
    rendered = _rendered(cache, 0)
    assert seeded.projection["sha256"] == hashlib.sha256(rendered.encode()).hexdigest()


def test_an_updates_projection_never_renders_and_reads_as_unsaved() -> None:
    """Judging a keystroke must not render the file; the hash a projection
    carries then is one no file has, so the session reads as unsaved."""
    cache, seeded = _seed(TWO)
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=NO_FORMAT,
        peers=frozenset({TAB}),
        update=_update(_tab(seeded.snapshot), _typing),
    )
    assert verdict is not None
    assert verdict.outcome == "ok"
    assert verdict.projection["cells"] == 2
    assert verdict.projection["sha256"] == nb.UNRENDERED


def test_a_projection_at_rest_keeps_the_unsaved_mark_when_the_format_cannot_render() -> None:
    _, seeded = _seed(TWO)
    projection = NO_FORMAT.project_at_rest(_tab(seeded.snapshot))
    assert projection["cells"] == 2 and projection["sha256"] == nb.UNRENDERED


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def _typing(tab: LoroDoc) -> None:
    source(tab, B).insert(0, "# note\n")


def test_typing_in_a_cells_source_is_admitted_and_names_the_cell() -> None:
    cache, seeded = _seed(TWO)
    verdict = _validate(cache, _update(_tab(seeded.snapshot), _typing))
    assert verdict.outcome == "ok", verdict.reason
    assert verdict.notes == {"touched": [B], "normal": True}


def _new_cell(tab: LoroDoc, cell_id: str = C, **fields: Any) -> LoroMap:
    created = nb.insert_map(tab.get_map("cells"), cell_id)
    created.insert("kind", fields.get("kind", "python"))
    created.insert("name", fields.get("name", "_"))
    created.insert("deleted", False)
    nb.insert_text(created, "source").insert(0, fields.get("source", "z = 3"))
    nb.insert_map(created, "config")
    nb.insert_map(created, "meta")
    nb.insert_map(created, "extra")
    return created


def test_a_person_inserting_a_whole_cell_is_admitted() -> None:
    cache, seeded = _seed(TWO)

    def insert(tab: LoroDoc) -> None:
        _new_cell(tab)
        tab.get_movable_list("order").insert(1, C)

    verdict = _validate(cache, _update(_tab(seeded.snapshot), insert))
    assert verdict.outcome == "ok", verdict.reason
    _commit(cache, verdict.delta, 1)
    assert [c.id for c in _cells(cache, 1)] == [A, C, B]


def _root_other(tab: LoroDoc) -> None:
    tab.get_map("other").insert("k", 1)


def _apps(tab: LoroDoc) -> None:
    tab.get_map("apps").insert("k", 1)


def _bad_cell_key(tab: LoroDoc) -> None:
    _new_cell(tab, "NOT-AN-ID!")


def _hard_delete(tab: LoroDoc) -> None:
    tab.get_map("cells").delete(A)


def _unknown_cell_key(tab: LoroDoc) -> None:
    cmap(tab, A).insert("colour", "red")


def _bad_kind(tab: LoroDoc) -> None:
    cmap(tab, A).insert("kind", "rust")


def _bad_name(tab: LoroDoc) -> None:
    cmap(tab, A).insert("name", "not a name")


def _keyword_name(tab: LoroDoc) -> None:
    cmap(tab, A).insert("name", "class")


def _bad_deleted(tab: LoroDoc) -> None:
    cmap(tab, A).insert("deleted", "yes")


def _config_key(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "config").insert("theme", "dark")


def _config_type(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "config").insert("hide_code", "yes")


def _config_column(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "config").insert("column", -1)


def _meta_for_kind(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "meta").insert("output_var", "df")


def _meta_connection(tab: LoroDoc) -> None:
    cmap(tab, A).insert("kind", "sql")
    nb.child(cmap(tab, A), "meta").insert("connection", "bad/name;drop")


def _meta_output_var(tab: LoroDoc) -> None:
    cmap(tab, A).insert("kind", "sql")
    nb.child(cmap(tab, A), "meta").insert("output_var", "1df")


def _extra_key(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "extra").insert("colour", 1)


def _extra_id(tab: LoroDoc) -> None:
    nb.child(cmap(tab, A), "extra").insert("alkera_id", "x")


def _nested_container(tab: LoroDoc) -> None:
    nb.insert_map(nb.child(cmap(tab, A), "config"), "column")


def _setting_value(tab: LoroDoc) -> None:
    tab.get_map("settings").insert("reactivity", "eager")


def _setting_env(tab: LoroDoc) -> None:
    tab.get_map("settings").insert("env", "/etc/passwd")


def _setting_key(tab: LoroDoc) -> None:
    tab.get_map("settings").insert("theme", "dark")


def _order_value(tab: LoroDoc) -> None:
    tab.get_movable_list("order").insert(0, "nope")


def _order_set(tab: LoroDoc) -> None:
    tab.get_movable_list("order").set(0, "NOPE")


def _mark(tab: LoroDoc) -> None:
    tab.config_text_style(loro.StyleConfigMap.default_rich_text_config())
    source(tab, A).mark(0, 1, "bold", True)


def _source_as_map(tab: LoroDoc) -> None:
    nb.insert_map(cmap(tab, A), "source")


def _replace_header(tab: LoroDoc) -> None:
    nb.insert_text(tab.get_map("meta"), "header")


def _delete_header(tab: LoroDoc) -> None:
    tab.get_map("meta").delete("header")


def _meta_key(tab: LoroDoc) -> None:
    tab.get_map("meta").insert("author", "me")


def _app_key(tab: LoroDoc) -> None:
    nb.child(tab.get_map("meta"), "app").insert("not a key", 1)


def _cell_without_source(tab: LoroDoc) -> None:
    created = nb.insert_map(tab.get_map("cells"), C)
    created.insert("kind", "python")
    created.insert("name", "_")


def _source_too_large(tab: LoroDoc) -> None:
    source(tab, A).insert(0, "x" * (nb.MAX_SOURCE_BYTES + 1))


def _header_too_large(tab: LoroDoc) -> None:
    nb.child(tab.get_map("meta"), "header").insert(0, "#" * (nb.MAX_FREE_TEXT_BYTES + 1))


def _long_scalar(tab: LoroDoc) -> None:
    cmap(tab, A).insert("name", "n" * (nb.MAX_SCALAR_BYTES + 1))


def _container_string(tab: LoroDoc) -> None:
    cmap(tab, A).insert("name", "\U0001f99c:cid:root-x:Map")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param(_root_other, "container", id="unknown-root"),
        pytest.param(_apps, "reserved", id="reserved-apps"),
        pytest.param(_bad_cell_key, "cell_id", id="cell-key-not-an-id"),
        pytest.param(_hard_delete, "structure", id="cell-hard-deleted"),
        pytest.param(_unknown_cell_key, "key", id="cell-unknown-field"),
        pytest.param(_bad_kind, "kind", id="kind-unregistered"),
        pytest.param(_bad_name, "name", id="name-not-identifier"),
        pytest.param(_keyword_name, "name", id="name-keyword"),
        pytest.param(_bad_deleted, "deleted", id="deleted-not-bool"),
        pytest.param(_config_key, "key", id="config-unknown-key"),
        pytest.param(_config_type, "config", id="config-wrong-type"),
        pytest.param(_config_column, "config", id="config-negative-column"),
        pytest.param(_meta_for_kind, "meta", id="meta-key-for-another-kind"),
        pytest.param(_meta_connection, "meta", id="meta-connection-regex"),
        pytest.param(_meta_output_var, "meta", id="meta-output-var-not-identifier"),
        pytest.param(_extra_key, "key", id="extra-not-alkera-keyword"),
        pytest.param(_extra_id, "key", id="extra-id-keyword"),
        pytest.param(_nested_container, "value", id="container-inside-config"),
        pytest.param(_setting_value, "setting", id="setting-enum"),
        pytest.param(_setting_env, "setting", id="setting-env-path"),
        pytest.param(_setting_key, "key", id="setting-unknown-key"),
        pytest.param(_order_value, "order_value", id="order-insert-not-an-id"),
        pytest.param(_order_set, "order_value", id="order-set-not-an-id"),
        pytest.param(_mark, "op_type", id="rich-text-mark"),
        pytest.param(_source_as_map, "structure", id="source-wrong-container-type"),
        pytest.param(_replace_header, "structure", id="header-replaced"),
        pytest.param(_delete_header, "structure", id="header-deleted"),
        pytest.param(_meta_key, "key", id="meta-unknown-key"),
        pytest.param(_app_key, "key", id="app-key-not-identifier"),
        pytest.param(_cell_without_source, "cell", id="cell-without-source"),
        pytest.param(_source_too_large, "source_too_large", id="source-over-cap"),
        pytest.param(_header_too_large, "text_too_large", id="header-over-cap"),
        pytest.param(_long_scalar, "value", id="scalar-over-cap"),
        pytest.param(_container_string, "value", id="scalar-spelled-as-container"),
    ],
)
def test_the_validator_refuses_what_the_notebook_schema_does_not_admit(
    change: Callable[[LoroDoc], None], reason: str
) -> None:
    cache, seeded = _seed(TWO)
    verdict = _validate(cache, _update(_tab(seeded.snapshot), change))
    assert (verdict.outcome, verdict.reason) == ("reject", reason)


def test_more_cells_than_the_cap_are_refused_but_a_full_notebook_may_still_shrink() -> None:
    cache, seeded = _seed(TWO)
    ids = [fmt._minted(str(n), set()) for n in range(nb.MAX_CELLS - 1)]

    def fill(tab: LoroDoc) -> None:
        for cell_id in ids:
            _new_cell(tab, cell_id, source="")

    verdict = _validate(cache, _update(_tab(seeded.snapshot), fill))
    assert verdict.outcome == "reject" and verdict.reason == "too_many_cells"


def test_a_source_already_over_the_cap_may_shrink() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    verdict = _validate(cache, _update(tab, _typing))
    assert verdict.outcome == "ok"
    _commit(cache, verdict.delta, 1)
    shrink = _update(tab, lambda t: source(t, B).delete(0, 2))
    assert _validate(cache, shrink, log_seq=1).outcome == "ok"


def test_typing_that_lands_in_a_source_set_kind_swapped_out_is_admitted() -> None:
    """The old text keeps the path it was created at, so late typing into it
    from a tab that had not seen the kind change is no schema breach."""
    cache, seeded = _seed(TWO)
    swapper, typist = _tab(seeded.snapshot), _tab(seeded.snapshot, OTHER_TAB)
    swap = _update(
        swapper,
        lambda t: (
            cmap(t, B).insert("kind", "markdown"),
            nb.insert_text(cmap(t, B), "source").insert(0, "# y"),
        ),
    )
    late = _update(typist, lambda t: source(t, B).insert(0, "z = 0\n"))
    first = _validate(cache, swap)
    assert first.outcome == "ok", first.reason
    _commit(cache, first.delta, 1)
    second = _validate(cache, late, log_seq=1)
    assert second.outcome == "ok", second.reason
    _commit(cache, second.delta, 2)
    rendered = _cells(cache, 2)
    assert (rendered[1].kind, rendered[1].source) == ("markdown", "# y")


def test_an_update_that_leaves_order_out_of_normal_form_says_so() -> None:
    cache, seeded = _seed(TWO)
    verdict = _validate(
        cache, _update(_tab(seeded.snapshot), lambda t: t.get_movable_list("order").push(A))
    )
    assert verdict.outcome == "ok"
    assert verdict.notes["normal"] is False


# ---------------------------------------------------------------------------
# Normal form
# ---------------------------------------------------------------------------


def _doc_from(text: str) -> LoroDoc:
    _, seeded = _seed(text)
    doc = _tab(seeded.snapshot, MERGE)
    return doc


@pytest.mark.parametrize(
    ("break_it", "order"),
    [
        pytest.param(lambda d: d.get_movable_list("order").push(A), [A, B, C], id="duplicate"),
        pytest.param(lambda d: cmap(d, B).insert("deleted", True), [A, C], id="deleted-in-order"),
        pytest.param(lambda d: d.get_movable_list("order").delete(2, 1), [A, B, C], id="missing"),
        pytest.param(
            lambda d: (cmap(d, C).insert("kind", "setup"), cmap(d, A).insert("kind", "setup")),
            [A, B, C],
            id="second-setup",
        ),
        pytest.param(lambda d: cmap(d, B).insert("kind", "setup"), [B, A, C], id="setup-not-first"),
    ],
)
def test_normalization_restores_normal_form_and_is_idempotent(
    break_it: Callable[[LoroDoc], object], order: list[str]
) -> None:
    doc = _doc_from(nb_text(cell(A, "a = 1"), cell(B, "b = 2"), cell(C, "c = 3")))
    break_it(doc)
    doc.commit()
    assert nb.normalize(doc) is True
    doc.commit()
    assert nb.raw_order(doc) == order
    setups = [c for c in order if nb.read_cell(doc, c).kind == "setup"]  # type: ignore[union-attr]
    assert len(setups) <= 1 and (not setups or order[0] == setups[0])
    assert nb.is_normal(doc)
    before = doc.oplog_vv
    assert nb.normalize(doc) is False
    doc.commit()
    assert doc.oplog_vv == before


def test_normalizing_a_cached_document_is_a_server_update_or_a_dup() -> None:
    cache, seeded = _seed(TWO)
    verdict = _validate(
        cache, _update(_tab(seeded.snapshot), lambda t: t.get_movable_list("order").push(A))
    )
    _commit(cache, verdict.delta, 1)
    normalized = nbo.normalize_cached(
        cache, key=KEY, epoch=1, log_seq=1, peer=MERGE, strategy=STANDIN
    )
    assert normalized is not None and normalized.outcome == "ok"
    _commit(cache, normalized.delta, 2)
    again = nbo.normalize_cached(cache, key=KEY, epoch=1, log_seq=2, peer=MERGE + 1)
    assert again is not None and again.outcome == "dup"


# ---------------------------------------------------------------------------
# Merging a change made outside the session
# ---------------------------------------------------------------------------


def _merge(
    cache: DocCache, log_seq: int, base: bytes, text: str, *, keep: bool = False
) -> core.Validated:
    merged = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=STANDIN,
        peer=MERGE + log_seq,
        base_vv=base,
        text=text,
        keep=keep,
    )
    assert merged is not None
    return merged


def test_an_outside_edit_merges_into_its_cell_and_live_typing_survives() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    typed = _validate(
        cache, _update(tab, lambda t: source(t, B).insert(len("y = x + 1"), "  # live"))
    )
    _commit(cache, typed.delta, 1)
    outside = nb_text(cell(A, "x = 10"), cell(B, "y = x + 1"))
    merged = _merge(cache, 1, seeded.base_vv, outside)
    assert merged.outcome == "ok", merged.reason
    _commit(cache, merged.delta, 2)
    assert [(c.id, c.source) for c in _cells(cache, 2)] == [(A, "x = 10"), (B, "y = x + 1  # live")]


def test_concurrent_typing_inside_the_same_cell_keeps_both_edits() -> None:
    """A VS Code save of the file while a person types in the same cell."""
    base_source = "def f():\n    return 1\n"
    cache, seeded = _seed(nb_text(cell(A, base_source)))
    tab = _tab(seeded.snapshot)
    typed = _validate(cache, _update(tab, lambda t: source(t, A).insert(0, "# live\n")))
    _commit(cache, typed.delta, 1)
    outside = nb_text(cell(A, base_source.replace("return 1", "return 2")))
    merged = _merge(cache, 1, seeded.base_vv, outside)
    _commit(cache, merged.delta, 2)
    assert _cells(cache, 2)[0].source == "# live\ndef f():\n    return 2\n"


def test_a_file_saved_without_ids_keeps_the_documents_ids() -> None:
    """A stock-marimo save strips the keywords: the merge reads it against the
    base's cells as known prior state, so no cell is deleted and minted again."""
    original = nb_text(cell(A, "x = 1"), cell(B, "y = x + 1"), cell(C, "print(y)"))
    cache, seeded = _seed(original)
    edited = strip_ids(nb_text(cell(A, "x = 1"), cell(B, "y = x + 2"), cell(C, "print(y)")))
    merged = _merge(cache, 0, seeded.base_vv, edited)
    assert merged.outcome == "ok", merged.reason
    _commit(cache, merged.delta, 1)
    assert [(c.id, c.source) for c in _cells(cache, 1)] == [
        (A, "x = 1"),
        (B, "y = x + 2"),
        (C, "print(y)"),
    ]
    doc = _tab(core.export(cache, key=KEY, epoch=1, log_seq=1, since=None).data)  # type: ignore[union-attr]
    assert sorted(doc.get_map("cells").keys()) == sorted([A, B, C])


def test_a_reordering_pull_moves_cells_without_retyping_them() -> None:
    cache, seeded = _seed(nb_text(cell(A, "a"), cell(B, "b"), cell(C, "c"), cell(D, "d")))
    tab = _tab(seeded.snapshot)
    typed = _validate(cache, _update(tab, lambda t: source(t, C).insert(1, "!")))
    _commit(cache, typed.delta, 1)
    pulled = nb_text(cell(A, "a"), cell(C, "c"), cell(D, "d"), cell(B, "b"))
    merged = _merge(cache, 1, seeded.base_vv, pulled)
    _commit(cache, merged.delta, 2)
    assert [(c.id, c.source) for c in _cells(cache, 2)] == [
        (A, "a"),
        (C, "c!"),
        (D, "d"),
        (B, "b"),
    ]


def test_reorder_moves_only_the_cells_out_of_place() -> None:
    doc = core.new_doc()
    order = doc.get_movable_list("order")
    for cell_id in [A, B, C, D]:
        order.push(cell_id)
    doc.commit()
    before = doc.oplog_vv
    nb.reorder(order, [A, C, D, B])
    doc.commit()
    assert [str(v) for v in order.get_value()] == [A, C, D, B]
    ops = sum(end - start for start, end in core._forward(before, doc.oplog_vv).values())
    assert ops == 1


def test_a_deleted_cell_is_soft_deleted_and_comes_back_when_the_file_has_it_again() -> None:
    cache, seeded = _seed(TWO)
    merged = _merge(cache, 0, seeded.base_vv, nb_text(cell(A, "x = 1")))
    _commit(cache, merged.delta, 1)
    doc = _tab(core.export(cache, key=KEY, epoch=1, log_seq=1, since=None).data)  # type: ignore[union-attr]
    assert nb.child(cmap(doc, B), "deleted") is True
    assert [c.id for c in _cells(cache, 1)] == [A]
    back = _merge(cache, 1, merged.base_vv, TWO)
    _commit(cache, back.delta, 2)
    assert [c.id for c in _cells(cache, 2)] == [A, B]


def test_a_merge_that_keeps_never_deletes_a_cell() -> None:
    cache, seeded = _seed(TWO)
    merged = _merge(cache, 0, seeded.base_vv, nb_text(cell(A, "x = 5")), keep=True)
    _commit(cache, merged.delta, 1)
    cells = _cells(cache, 1)
    assert [c.id for c in cells] == [A, B]
    # Keeping takes only what the text adds: the new line lands beside the old.
    assert cells[0].source == "x = 1\nx = 5" and cells[1].source == "y = x + 1"


def test_a_kind_change_from_outside_swaps_the_source_text() -> None:
    cache, seeded = _seed(TWO)
    merged = _merge(
        cache,
        0,
        seeded.base_vv,
        nb_text(cell(A, "x = 1"), cell(B, "# Title", kind="markdown")),
    )
    _commit(cache, merged.delta, 1)
    assert [(c.kind, c.source) for c in _cells(cache, 1)] == [
        ("python", "x = 1"),
        ("markdown", "# Title"),
    ]


def test_an_outside_file_in_a_newer_major_format_is_rejected_for_the_session_to_close() -> None:
    cache, seeded = _seed(TWO)
    merged = _merge(cache, 0, seeded.base_vv, nb_text(cell(A, "x"), fmt_version="3.1"))
    assert (merged.outcome, merged.reason) == ("reject", "newer_format")


def test_an_outside_text_that_only_reformats_is_a_dup() -> None:
    cache, seeded = _seed(TWO)
    reformatted = TWO.replace(
        "#fmt format=1.0 generated_with=0.25.1", "#fmt generated_with=0.25.1 format=1.0"
    )
    merged = _merge(cache, 0, seeded.base_vv, reformatted)
    assert merged.outcome == "dup"


def test_a_new_epoch_from_a_document_ahead_of_its_file_keeps_a_version_matching_the_file() -> None:
    edited = nb_text(cell(A, "x = 2"), cell(B, "y = x + 1"))
    cache = DocCache()
    seeded = core.seed(cache, key=KEY, epoch=2, rules=STANDIN, text=edited, peer=SEED, base=TWO)
    at_base = core.content(cache, key=KEY, epoch=2, log_seq=0, rules=STANDIN, at=seeded.base_vv)
    assert at_base is not None and at_base.decode() == TWO
    assert _rendered_epoch(cache, 2) == edited


def _rendered_epoch(cache: DocCache, epoch: int) -> str:
    data = core.content(cache, key=KEY, epoch=epoch, log_seq=0, rules=STANDIN)
    assert data is not None
    return data.decode()


# ---------------------------------------------------------------------------
# Carets
# ---------------------------------------------------------------------------


def _caret_update(doc: LoroDoc, peer: int, value: dict[str, Any]) -> bytes:
    store = EphemeralStore(60_000)
    store.set(str(peer), value)
    return bytes(store.encode(str(peer)))


def test_a_caret_in_a_cell_is_relayed_with_the_cell_it_names() -> None:
    _, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    cursor = bytes(source(tab, B).get_cursor(1, Side.Left).encode())  # type: ignore[union-attr]
    data = _caret_update(tab, TAB, {"anchor": cursor, "focus": cursor, "cell": B})
    judged = core.caret(rules=STANDIN, peer=TAB, data=data)
    assert judged.facts == {"cell": B}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"anchor": b"x", "cell": "NOPE"}, id="cell-not-an-id"),
        pytest.param({"anchor": b"x", "colour": 1}, id="unknown-key"),
        pytest.param({}, id="empty"),
    ],
)
def test_a_caret_that_is_not_an_anchor_focus_and_cell_is_refused(value: dict[str, Any]) -> None:
    with pytest.raises(SandboxError) as refused:
        core.caret(rules=STANDIN, peer=TAB, data=_caret_update(core.new_doc(), TAB, value))
    assert refused.value.code == "ephemeral"


def test_a_caret_into_a_root_container_is_refused_for_a_notebook() -> None:
    doc = core.new_doc()
    cursor = bytes(doc.get_text("content").get_cursor(0, Side.Left).encode())  # type: ignore[union-attr]
    with pytest.raises(SandboxError):
        core.caret(rules=STANDIN, peer=TAB, data=_caret_update(doc, TAB, {"anchor": cursor}))
