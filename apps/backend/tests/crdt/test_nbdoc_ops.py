"""Operations from agent and box peers on a notebook document, and the
document's totality under any interleaving of writers.

Real Loro documents, in this process; the format API is the test stand-in."""

from __future__ import annotations

from typing import Any

import pytest
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox import notebook_ops as nbo
from backend.services.crdt.sandbox.core import DocCache, SandboxError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from loro import EphemeralStore, ExportMode, LoroDoc
from tests.crdt import nbdoc_fmt_standin as fmt
from tests.crdt.test_nbdoc_sandbox import (
    KEY,
    SEED,
    STANDIN,
    TAB,
    TWO,
    A,
    B,
    C,
    _commit,
    _rendered,
    _seed,
    _tab,
    _update,
    _validate,
    cell,
    cmap,
    nb_text,
    source,
)

pytestmark = [pytest.mark.spread]

PEER = 7000


def _ops(
    cache: DocCache,
    ops: list[dict[str, Any]],
    *,
    log_seq: int = 0,
    base: bytes | None = None,
    peer: int = PEER,
) -> nbo.OpsApplied:
    applied = nbo.apply_ops(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        peer=peer,
        base_vv=base,
        ops=ops,
        strategy=STANDIN,
    )
    assert applied is not None
    return applied


def _cells(cache: DocCache, log_seq: int) -> list[fmt.CellIR]:
    return list(fmt.read(_rendered(cache, log_seq)).cells)


# ---------------------------------------------------------------------------
# Each operation
# ---------------------------------------------------------------------------


def test_insert_creates_a_cell_with_a_fresh_id_where_it_was_asked() -> None:
    cache, _ = _seed(TWO)
    applied = _ops(cache, [{"op": "insert", "source": "z = y * 2", "after": A, "name": "z"}])
    assert applied.outcome == "ok"
    (created,) = applied.result["created"]
    assert nb.CELL_ID_RE.match(created) and created not in (A, B)
    _commit(cache, applied.delta, 1)
    assert [(c.id, c.name, c.source) for c in _cells(cache, 1)] == [
        (A, "_", "x = 1"),
        (created, "z", "z = y * 2"),
        (B, "_", "y = x + 1"),
    ]
    assert applied.result["cells"] == [
        {"id": created, "index": 1, "kind": "python", "name": "z", "deleted": False}
    ]


def test_edit_replaces_exactly_the_named_text() -> None:
    cache, _ = _seed(nb_text(cell(A, "a = 1\nb = 1\n")))
    applied = _ops(
        cache,
        [{"op": "edit", "cell_id": A, "edits": [{"old": "1", "new": "2", "occurrence": 2}]}],
    )
    _commit(cache, applied.delta, 1)
    assert _cells(cache, 1)[0].source == "a = 1\nb = 2\n"


def test_set_meta_changes_a_sql_cells_settings_and_null_resets_a_key() -> None:
    cache, _ = _seed(nb_text(cell(A, "SELECT 1", kind="sql", meta={"output_var": "_df"})))
    applied = _ops(
        cache,
        [
            {
                "op": "set_meta",
                "cell_id": A,
                "meta": {"connection": "warehouse", "output_var": "orders", "show_output": False},
            }
        ],
    )
    _commit(cache, applied.delta, 1)
    assert dict(_cells(cache, 1)[0].meta) == {
        "connection": "warehouse",
        "output_var": "orders",
        "show_output": False,
    }
    reset = _ops(cache, [{"op": "set_meta", "cell_id": A, "meta": {"connection": None}}], log_seq=1)
    _commit(cache, reset.delta, 2)
    rendered = _cells(cache, 2)[0]
    assert dict(rendered.meta) == {"output_var": "orders", "show_output": False}
    # The cell's code follows its settings: the result is bound to the new name.
    assert rendered.code.startswith("orders = alkera.sql(")


@pytest.mark.parametrize(
    ("kind", "meta"),
    [
        pytest.param("sql", {"quote": "r"}, id="a-key-sql-cells-do-not-have"),
        pytest.param("sql", {"output_var": "1x"}, id="result-name-not-an-identifier"),
        pytest.param("sql", {"output_var": "class"}, id="result-name-a-keyword"),
        pytest.param("sql", {"connection": 'wh"'}, id="connection-with-a-quote"),
        pytest.param("sql", {"connection": 7}, id="connection-not-text"),
        pytest.param("sql", {"show_output": "no"}, id="show-output-not-a-boolean"),
        pytest.param("python", {"connection": "wh"}, id="a-python-cell-has-no-meta"),
    ],
)
def test_set_meta_refuses_what_the_cells_kind_does_not_admit(
    kind: str, meta: dict[str, Any]
) -> None:
    start = {"output_var": "_df"} if kind == "sql" else {}
    cache, _ = _seed(nb_text(cell(A, "SELECT 1", kind=kind, meta=start)))
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [{"op": "set_meta", "cell_id": A, "meta": meta}])
    assert (refused.value.index, refused.value.code) == (0, "invalid_config")


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        pytest.param({"old": "nope", "new": "x"}, "edit_not_found", id="absent"),
        pytest.param({"old": "", "new": "x"}, "edit_ambiguous", id="empty-old"),
        pytest.param({"old": "1", "new": "2"}, "edit_ambiguous", id="twice"),
        pytest.param({"old": "1", "new": "2", "occurrence": 3}, "edit_not_found", id="occurrence"),
    ],
)
def test_an_edit_that_does_not_name_one_place_is_refused(edit: dict[str, Any], code: str) -> None:
    cache, _ = _seed(nb_text(cell(A, "a = 1\nb = 1\n")))
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [{"op": "edit", "cell_id": A, "edits": [edit]}])
    assert (refused.value.index, refused.value.code) == (0, code)


@pytest.mark.parametrize(
    ("op", "code"),
    [
        pytest.param(
            {"op": "edit", "cell_id": "zzzzzzzzzz", "edits": [{"old": "x", "new": "y"}]},
            "cell_not_found",
            id="edit-unknown-cell",
        ),
        pytest.param(
            {"op": "insert", "after": "zzzzzzzzzz"}, "cell_not_found", id="insert-after-unknown"
        ),
        pytest.param({"op": "insert", "kind": "rust"}, "unknown_kind", id="insert-unknown-kind"),
        pytest.param({"op": "insert", "name": "not a name"}, "invalid_name", id="insert-bad-name"),
        pytest.param(
            {"op": "insert", "config": {"hide_code": "yes"}},
            "invalid_config",
            id="insert-bad-config",
        ),
        pytest.param(
            {"op": "insert", "meta": {"output_var": "x"}},
            "invalid_config",
            id="insert-meta-for-kind",
        ),
        pytest.param(
            {"op": "insert", "kind": "setup", "after": A},
            "setup_must_be_first",
            id="setup-not-first",
        ),
        pytest.param(
            {"op": "insert", "source": "x" * (nb.MAX_SOURCE_BYTES + 1)},
            "cap_exceeded",
            id="source-over-cap",
        ),
        pytest.param(
            {"op": "rename", "cell_id": A, "name": "for"}, "invalid_name", id="rename-keyword"
        ),
        pytest.param(
            {"op": "set_kind", "cell_id": A, "kind": "cobol"}, "unknown_kind", id="set-unknown-kind"
        ),
        pytest.param(
            {"op": "set_config", "cell_id": A, "config": {"theme": 1}},
            "invalid_config",
            id="config-unknown-key",
        ),
        pytest.param(
            {"op": "set_setting", "key": "reactivity", "value": "eager"},
            "invalid_config",
            id="setting-value",
        ),
        pytest.param(
            {"op": "set_setting", "key": "theme", "value": "dark"},
            "invalid_config",
            id="setting-unknown",
        ),
        pytest.param(
            {"op": "set_setting", "key": "sql_row_limit", "value": 0},
            "invalid_config",
            id="row-limit-below-one",
        ),
        pytest.param(
            {"op": "set_setting", "key": "sql_row_limit", "value": "1000"},
            "invalid_config",
            id="row-limit-as-text",
        ),
        pytest.param(
            {"op": "set_setting", "key": "sql_row_limit", "value": True},
            "invalid_config",
            id="row-limit-as-bool",
        ),
        pytest.param(
            {"op": "replace", "cell_id": A, "source": "y" * (nb.MAX_SOURCE_BYTES + 1)},
            "cap_exceeded",
            id="replace-over-cap",
        ),
        pytest.param(
            {"op": "move", "cell_id": A, "before": "zzzzzzzzzz"},
            "cell_not_found",
            id="move-before-unknown",
        ),
    ],
)
def test_each_operation_refuses_what_its_contract_refuses(op: dict[str, Any], code: str) -> None:
    cache, _ = _seed(TWO)
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [op])
    assert refused.value.code == code


def test_a_second_setup_is_refused() -> None:
    cache, _ = _seed(nb_text(cell(A, "import x", kind="setup"), cell(B, "y = 1")))
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [{"op": "set_kind", "cell_id": B, "kind": "setup"}])
    assert refused.value.code == "setup_must_be_first"


def test_a_batch_is_atomic_and_names_the_operation_that_failed() -> None:
    cache, _ = _seed(TWO)
    with pytest.raises(nbo.OpError) as refused:
        _ops(
            cache,
            [
                {"op": "replace", "cell_id": A, "source": "x = 99"},
                {"op": "rename", "cell_id": B, "name": "not ok"},
            ],
        )
    assert refused.value.index == 1
    # Nothing of the batch reached the document, and the worker still serves it.
    assert [c.source for c in _cells(cache, 0)] == ["x = 1", "y = x + 1"]


def test_delete_restore_move_rename_set_kind_config_and_setting() -> None:
    cache, _ = _seed(nb_text(cell(A, "a = 1"), cell(B, "b = 2"), cell(C, "c = 3")))
    applied = _ops(
        cache,
        [
            {"op": "delete", "cell_id": B},
            {"op": "move", "cell_id": C, "before": A},
            {"op": "rename", "cell_id": A, "name": "first"},
            {"op": "set_kind", "cell_id": A, "kind": "markdown"},
            {"op": "set_config", "cell_id": C, "config": {"hide_code": True, "column": 1}},
            {"op": "set_setting", "key": "reactivity", "value": "lazy"},
        ],
    )
    _commit(cache, applied.delta, 1)
    rendered = fmt.read(_rendered(cache, 1))
    assert [(c.id, c.kind, c.name, dict(c.config)) for c in rendered.cells] == [
        (C, "python", "_", {"column": 1, "hide_code": True}),
        (A, "markdown", "first", {}),
    ]
    assert rendered.settings["reactivity"] == "lazy"
    restored = _ops(cache, [{"op": "restore", "cell_id": B, "after": C}], log_seq=1)
    _commit(cache, restored.delta, 2)
    assert [c.id for c in _cells(cache, 2)] == [C, B, A]


def test_the_sql_row_limit_is_a_setting_of_the_file() -> None:
    """Every setting the format knows is one ``set_setting`` takes; the row
    limit was refused as unknown while the notice told people to change it."""
    cache, _ = _seed(TWO)
    applied = _ops(cache, [{"op": "set_setting", "key": "sql_row_limit", "value": 1000}])
    _commit(cache, applied.delta, 1)
    assert fmt.read(_rendered(cache, 1)).settings["sql_row_limit"] == 1000


def test_a_setting_set_to_none_returns_to_its_default() -> None:
    cache, _ = _seed(nb_text(cell(A, "a"), settings={"reactivity": "lazy"}))
    applied = _ops(cache, [{"op": "set_setting", "key": "reactivity", "value": None}])
    _commit(cache, applied.delta, 1)
    assert "reactivity" not in fmt.read(_rendered(cache, 1)).settings


def test_a_batch_that_changes_nothing_is_a_dup() -> None:
    cache, _ = _seed(TWO)
    applied = _ops(cache, [{"op": "replace", "cell_id": A, "source": "x = 1"}])
    assert applied.outcome == "dup" and applied.delta == b""


# ---------------------------------------------------------------------------
# Concurrency: operations at a base token
# ---------------------------------------------------------------------------


def test_an_edit_at_an_old_base_merges_with_typing_since() -> None:
    cache, seeded = _seed(nb_text(cell(A, "total = price * qty\n")))
    base = seeded.vv
    tab = _tab(seeded.snapshot)
    typed = _validate(cache, _update(tab, lambda t: source(t, A).insert(0, "# person\n")))
    _commit(cache, typed.delta, 1)
    applied = _ops(
        cache,
        [{"op": "edit", "cell_id": A, "edits": [{"old": "qty", "new": "quantity"}]}],
        log_seq=1,
        base=base,
    )
    _commit(cache, applied.delta, 2)
    assert _cells(cache, 2)[0].source == "# person\ntotal = price * quantity\n"
    assert applied.result["notices"] == []


def test_the_old_text_is_matched_at_the_base_not_at_the_head() -> None:
    """``TextEdit.old`` is matched against the cell at ``base_token``: text a
    person typed since is not there for the agent's edit to name."""
    cache, seeded = _seed(nb_text(cell(A, "a = 1\n")))
    tab = _tab(seeded.snapshot)
    typed = _validate(cache, _update(tab, lambda t: source(t, A).insert(0, "b = 2\n")))
    _commit(cache, typed.delta, 1)
    with pytest.raises(nbo.OpError) as refused:
        _ops(
            cache,
            [{"op": "edit", "cell_id": A, "edits": [{"old": "b = 2", "new": "b = 3"}]}],
            log_seq=1,
            base=seeded.vv,
        )
    assert refused.value.code == "edit_not_found"


def test_editing_a_cell_someone_deleted_since_says_so() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    deleted = _validate(cache, _update(tab, lambda t: cmap(t, B).insert("deleted", True)))
    _commit(cache, deleted.delta, 1)
    applied = _ops(
        cache,
        [{"op": "replace", "cell_id": B, "source": "y = 2"}],
        log_seq=1,
        base=seeded.vv,
    )
    assert [n["kind"] for n in applied.result["notices"]] == ["edited_deleted_cell"]


def test_editing_a_cell_whose_kind_someone_changed_since_says_so() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    swapped = _validate(
        cache,
        _update(
            tab,
            lambda t: (
                cmap(t, B).insert("kind", "markdown"),
                nb.insert_text(cmap(t, B), "source").insert(0, "# y"),
            ),
        ),
    )
    _commit(cache, swapped.delta, 1)
    applied = _ops(
        cache,
        [{"op": "edit", "cell_id": B, "edits": [{"old": "x + 1", "new": "x + 2"}]}],
        log_seq=1,
        base=seeded.vv,
    )
    assert [n["kind"] for n in applied.result["notices"]] == ["kind_changed_by_other"]


def test_a_base_that_is_not_a_version_of_the_document_is_refused() -> None:
    cache, _ = _seed(TWO)
    other = core.new_doc()
    other.peer_id = 9999
    other.get_map("meta").insert("format", "1.0")
    other.commit()
    with pytest.raises(SandboxError) as refused:
        _ops(cache, [{"op": "delete", "cell_id": A}], base=core.encode_vv(other))
    assert refused.value.code == "bad_base"


def test_the_writer_gets_a_caret_at_the_end_of_its_last_edit() -> None:
    cache, _ = _seed(nb_text(cell(A, "abc\n")))
    applied = _ops(cache, [{"op": "edit", "cell_id": A, "edits": [{"old": "b", "new": "BBB"}]}])
    caret = bytes.fromhex(applied.result["caret"])
    store = EphemeralStore(60_000)
    store.apply(caret)
    state = store.get_all_states()[str(PEER)]
    assert state["cell"] == A
    _commit(cache, applied.delta, 1)
    doc = _tab(core.export(cache, key=KEY, epoch=1, log_seq=1, since=None).data)  # type: ignore[union-attr]
    import loro

    position = doc.get_cursor_pos(loro.Cursor.decode(state["anchor"]))
    assert position.current.pos == len("aBBB")


def test_operations_are_written_by_a_minted_peer_only() -> None:
    cache, _ = _seed(TWO)
    with pytest.raises(SandboxError):
        _ops(cache, [{"op": "delete", "cell_id": A}], peer=core.SERVER_PEER_MAX)


def test_a_view_reads_the_live_cells_and_whether_a_frontier_is_in() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    typed = _validate(cache, _update(tab, lambda t: source(t, B).insert(0, "# t\n")))
    found = nbo.view(cache, key=KEY, epoch=1, log_seq=0, frontier=core.encode_vv(tab))
    assert found is not None and found[0]["covered"] is False
    _commit(cache, typed.delta, 1)
    found = nbo.view(cache, key=KEY, epoch=1, log_seq=1, frontier=core.encode_vv(tab))
    assert found is not None
    view, _ = found
    assert view["covered"] is True
    assert [(c["id"], c["source"]) for c in view["cells"]] == [(A, "x = 1"), (B, "# t\ny = x + 1")]


def test_the_graph_is_the_format_apis_analysis_of_the_live_cells() -> None:
    cache, _ = _seed(TWO)
    found = nbo.graph(cache, key=KEY, epoch=1, log_seq=0, strategy=STANDIN)
    assert found is not None
    graph, vv = found
    assert graph["edges"] == [[A, B]]
    assert core.same_vv(_tab(core.export(cache, key=KEY, epoch=1, log_seq=0, since=None).data), vv)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Totality: any interleaving of writers renders, projects and normalizes
# ---------------------------------------------------------------------------

_IDS = [A, B, C, "s7t8v9w0x1"]

action = st.one_of(
    st.tuples(st.just("type"), st.sampled_from(_IDS), st.integers(0, 30), st.text(max_size=5)),
    st.tuples(st.just("cut"), st.sampled_from(_IDS), st.integers(0, 30), st.integers(1, 5)),
    st.tuples(st.just("delete"), st.sampled_from(_IDS)),
    st.tuples(st.just("restore"), st.sampled_from(_IDS)),
    st.tuples(st.just("kind"), st.sampled_from(_IDS), st.sampled_from(sorted(nb.KINDS))),
    st.tuples(st.just("move"), st.integers(0, 6), st.integers(0, 6)),
    st.tuples(st.just("insert"), st.sampled_from(_IDS), st.integers(0, 6)),
    st.tuples(st.just("drop"), st.integers(0, 6)),
)


def _act(doc: LoroDoc, step: tuple[Any, ...]) -> None:
    """One person's edit through the editor's own moves (the shapes a real
    tab writes), applied leniently: a move that does not fit is skipped."""
    kind = step[0]
    cells = doc.get_map("cells")
    order = doc.get_movable_list("order")
    if kind in ("type", "cut", "delete", "restore", "kind"):
        target = nb.child(cells, step[1])
        if target is None:
            return
        text = nb.child(target, "source")
        if kind == "type" and text is not None:
            text.insert(min(step[2], int(text.len_unicode)), step[3])
        elif kind == "cut" and text is not None:
            at = min(step[2], int(text.len_unicode))
            span = min(step[3], int(text.len_unicode) - at)
            if span > 0:
                text.delete(at, span)
        elif kind == "delete":
            target.insert("deleted", True)
        elif kind == "restore":
            target.insert("deleted", False)
            order.push(step[1])
        else:
            target.insert("kind", step[2])
            nb.insert_text(target, "source").insert(0, "swapped")
    elif kind == "move":
        size = len(nb.raw_order(doc))
        if size > 1:
            order.mov(step[1] % size, step[2] % size)
    elif kind == "insert":
        if nb.child(cells, step[1]) is None:
            created = nb.insert_map(cells, step[1])
            created.insert("kind", "python")
            created.insert("name", "_")
            created.insert("deleted", False)
            nb.insert_text(created, "source")
            for key in ("config", "meta", "extra"):
                nb.insert_map(created, key)
        order.insert(min(step[2], len(nb.raw_order(doc))), step[1])
    elif kind == "drop":
        size = len(nb.raw_order(doc))
        if size:
            order.delete(step[1] % size, 1)
    doc.commit()


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=st.lists(action, max_size=8),
    second=st.lists(action, max_size=8),
    third=st.lists(action, max_size=8),
)
def test_any_interleaving_of_three_writers_renders_projects_and_normalizes(
    first: list[tuple[Any, ...]],
    second: list[tuple[Any, ...]],
    third: list[tuple[Any, ...]],
) -> None:
    cache = DocCache()
    seeded = core.seed(
        cache,
        key=KEY,
        epoch=1,
        rules=STANDIN,
        text=nb_text(cell(A, "x = 1"), cell(B, "y = x"), cell(C, "z = y")),
        peer=SEED,
    )
    log_seq = 0
    writers = [_tab(seeded.snapshot, TAB + n) for n in range(3)]
    for writer, steps in zip(writers, (first, second, third), strict=True):
        for step in steps:
            _act(writer, step)
    for writer in writers:
        update = bytes(writer.export(ExportMode.Updates(seeded_vv(seeded))))
        verdict = core.validate(
            cache,
            key=KEY,
            epoch=1,
            log_seq=log_seq,
            rules=STANDIN,
            peers=frozenset({TAB, TAB + 1, TAB + 2}),
            update=update,
        )
        assert verdict is not None
        if verdict.outcome == "ok":
            log_seq += 1
            _commit(cache, verdict.delta, log_seq)
    # Whatever got in: the document renders, projects and reads as a view.
    rendered = _rendered(cache, log_seq)
    fmt.read(rendered)
    view = nbo.view(cache, key=KEY, epoch=1, log_seq=log_seq, frontier=None)
    assert view is not None
    ids = [c["id"] for c in view[0]["cells"]]
    assert len(ids) == len(set(ids))
    assert sum(1 for c in view[0]["cells"] if c["kind"] == "setup") <= 1
    # Normalization reaches normal form in one pass and then writes nothing.
    first_pass = nbo.normalize_cached(
        cache, key=KEY, epoch=1, log_seq=log_seq, peer=8000, strategy=STANDIN
    )
    assert first_pass is not None
    if first_pass.outcome == "ok":
        log_seq += 1
        _commit(cache, first_pass.delta, log_seq)
    second_pass = nbo.normalize_cached(
        cache, key=KEY, epoch=1, log_seq=log_seq, peer=8001, strategy=STANDIN
    )
    assert second_pass is not None and second_pass.outcome == "dup"
    assert _rendered(cache, log_seq) == rendered


def seeded_vv(seeded: core.Seeded) -> Any:
    return core.decode_vv(seeded.vv)


def test_setting_the_header_diffs_it_so_concurrent_typing_survives() -> None:
    cache, seeded = _seed(TWO)
    tab = _tab(seeded.snapshot)
    typed = _validate(
        cache,
        _update(tab, lambda t: nb.child(t.get_map("meta"), "header").insert(0, "# person\n")),
    )
    _commit(cache, typed.delta, 1)
    applied = _ops(
        cache,
        [{"op": "set_setting", "key": "header", "value": '"""A better notebook."""'}],
        log_seq=1,
        base=seeded.vv,
    )
    _commit(cache, applied.delta, 2)
    assert fmt.read(_rendered(cache, 2)).header_text == '# person\n"""A better notebook."""'


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param(3, "invalid_config", id="not-text"),
        pytest.param("#" * (nb.MAX_FREE_TEXT_BYTES + 1), "cap_exceeded", id="over-cap"),
    ],
)
def test_a_header_that_is_not_text_within_the_cap_is_refused(value: object, code: str) -> None:
    cache, _ = _seed(TWO)
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [{"op": "set_setting", "key": "header", "value": value}])
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("config", "says"),
    [
        pytest.param({"show_output": False}, "show_output is a SQL cell setting", id="sql-setting"),
        pytest.param({"connection": "wh"}, "change it with set_meta", id="points-at-set-meta"),
        pytest.param({"colour": "red"}, "set_config has no key colour", id="unknown-key"),
    ],
)
def test_set_config_with_a_key_it_does_not_take_says_which_it_does(
    config: dict[str, Any], says: str
) -> None:
    """The refusal an agent got for show_output was "not marimo's"; it now
    names the keys set_config takes and points a SQL setting at set_meta."""
    cache, _ = _seed(nb_text(cell(A, "SELECT 1", kind="sql", meta={"output_var": "_df"})))
    with pytest.raises(nbo.OpError) as refused:
        _ops(cache, [{"op": "set_config", "cell_id": A, "config": config}])
    assert (refused.value.index, refused.value.code) == (0, "invalid_config")
    assert says in str(refused.value)
    assert "disabled, hide_code, expand_output" in str(refused.value)


def test_the_sandbox_takes_the_same_cell_keys_as_the_format() -> None:
    """The sandbox keeps its own copy (it loads the format lazily); this holds
    the copy to the format's op rules, so the refusal and the check agree."""
    from alkera_notebook.format import op_rules

    assert dict(nb.CONFIG_TYPES) == dict(op_rules.CONFIG_TYPES)
    assert dict(nb.META_KEYS) == dict(op_rules.META_KEYS)
