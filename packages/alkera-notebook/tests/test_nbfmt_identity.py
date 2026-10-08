"""Cell identity: encoding, minting, and resolution by keyword, exact code,
similarity and deterministic minting, never by position."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from alkera_notebook.format import read, write
from alkera_notebook.format.ids import (
    ALPHABET,
    ID_RE,
    SIMILARITY_THRESHOLD,
    CellKey,
    encode_id,
    is_cell_id,
    mint_id,
    new_cell_id,
    normalize_code,
    resolve,
    similarity,
)
from hypothesis import given, settings
from hypothesis import strategies as st

HEAD = 'import marimo\n\n__generated_with = "0.25.1"\napp = marimo.App()\n\n\n'
GUARD = '\n\nif __name__ == "__main__":\n    app.run()\n'


def stock_file(*codes: str) -> str:
    """A file as stock marimo saves it: no keywords, no settings fence."""
    blocks = []
    for code in codes:
        body = "\n".join("    " + line if line else "" for line in code.split("\n"))
        blocks.append(f"@app.cell\ndef _():\n{body}\n    return\n")
    return HEAD + "\n\n".join(blocks) + GUARD


# ---- encoding ---------------------------------------------------------------------


def test_alphabet_is_lower_case_crockford_base32() -> None:
    assert ALPHABET == "0123456789abcdefghjkmnpqrstvwxyz"
    assert not set("ilou") & set(ALPHABET)


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        pytest.param(0, "0000000000", id="zero"),
        pytest.param(1, "0000000001", id="one"),
        pytest.param(31, "000000000z", id="last-digit"),
        pytest.param(32, "0000000010", id="carry"),
        pytest.param((1 << 50) - 1, "zzzzzzzzzz", id="max"),
    ],
)
def test_encode_id(bits: int, expected: str) -> None:
    assert encode_id(bits) == expected


@pytest.mark.parametrize("bits", [-1, 1 << 50], ids=["negative", "too-large"])
def test_encode_id_refuses_out_of_range(bits: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        encode_id(bits)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param("a1b2c3d4e5", True, id="valid"),
        pytest.param("a1b2c3d4e", False, id="nine-chars"),
        pytest.param("a1b2c3d4e5f", False, id="eleven-chars"),
        pytest.param("A1B2C3D4E5", False, id="upper-case"),
        pytest.param("iiiiiiiiii", False, id="excluded-i"),
        pytest.param("llllllllll", False, id="excluded-l"),
        pytest.param("oooooooooo", False, id="excluded-o"),
        pytest.param("uuuuuuuuuu", False, id="excluded-u"),
        pytest.param("a1b2c3d4e5\n", False, id="trailing-newline"),
        pytest.param(1234567890, False, id="not-a-string"),
    ],
)
def test_is_cell_id(value: object, valid: bool) -> None:
    assert is_cell_id(value) is valid


def test_new_cell_ids_are_valid_and_distinct() -> None:
    ids = {new_cell_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(ID_RE.fullmatch(i) for i in ids)


def test_mint_is_the_documented_hash() -> None:
    import hashlib

    digest = hashlib.sha256(b"alkera-cell-id\x00x = 1\x000").digest()
    assert mint_id("x = 1", set()) == encode_id(int.from_bytes(digest[:7], "big") >> 6)


def test_mint_skips_used_ids_deterministically() -> None:
    first = mint_id("x = 1", set())
    second = mint_id("x = 1", {first})
    assert second != first
    assert mint_id("x = 1", {first}) == second
    assert mint_id("x = 1", {first, second}) not in {first, second}


# ---- normalization ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "normalized"),
    [
        pytest.param("x = 1   \ny = 2\t", "x = 1\ny = 2", id="trailing-whitespace"),
        pytest.param("x = 1\n\n\n", "x = 1", id="trailing-blank-lines"),
        pytest.param("x = 1\r\ny = 2\r", "x = 1\ny = 2", id="crlf-and-cr"),
        pytest.param("\n  x = 1", "\n  x = 1", id="leading-kept"),
        pytest.param("a\n\nb", "a\n\nb", id="inner-blank-kept"),
        pytest.param("", "", id="empty"),
    ],
)
def test_normalize_code(code: str, normalized: str) -> None:
    assert normalize_code(code) == normalized


# ---- resolution (section 7 cases, at the algorithm level) ---------------------------

KNOWN = {
    "aaaaaaaaa1": "import polars as pl",
    "aaaaaaaaa2": "df = pl.read_csv('orders.csv')",
    "aaaaaaaaa3": "total = df['amount'].sum()",
    "aaaaaaaaa4": "print(f'total: {total}')",
}


def ids_of(codes: list[str], known: dict[str, str] | None = KNOWN) -> list[tuple[str, str]]:
    report = resolve([CellKey(None, normalize_code(c)) for c in codes], known)
    return [(r.id, r.resolution) for r in report.cells]


def test_every_cell_with_a_keyword_keeps_it_whatever_the_known_state_says() -> None:
    report = resolve(
        [CellKey("aaaaaaaaa4", "completely different"), CellKey("bbbbbbbbbb", "x")],
        KNOWN,
    )
    assert [(r.id, r.resolution) for r in report.cells] == [
        ("aaaaaaaaa4", "keyword"),
        ("bbbbbbbbbb", "keyword"),
    ]


def test_stripped_with_no_structural_change_matches_exactly() -> None:
    assert ids_of(list(KNOWN.values())) == [(k, "exact") for k in KNOWN]


def test_stripped_and_reordered_follows_the_code_not_the_position() -> None:
    codes = [KNOWN["aaaaaaaaa3"], KNOWN["aaaaaaaaa1"], KNOWN["aaaaaaaaa4"], KNOWN["aaaaaaaaa2"]]
    assert ids_of(codes) == [
        ("aaaaaaaaa3", "exact"),
        ("aaaaaaaaa1", "exact"),
        ("aaaaaaaaa4", "exact"),
        ("aaaaaaaaa2", "exact"),
    ]


def test_insert_plus_delete_at_equal_count_does_not_shift_ids() -> None:
    codes = [KNOWN["aaaaaaaaa1"], "new_cell = compute_something()", *list(KNOWN.values())[2:]]
    result = ids_of(codes)
    assert result[0] == ("aaaaaaaaa1", "exact")
    assert result[1][1] == "minted"
    assert result[1][0] not in KNOWN
    assert result[2:] == [("aaaaaaaaa3", "exact"), ("aaaaaaaaa4", "exact")]


def test_delete_and_unrelated_insert_mints_instead_of_reusing() -> None:
    # marimo's own matcher gives the new cell the deleted cell's id here.
    codes = [
        KNOWN["aaaaaaaaa1"],
        "url = 'https://example.com/data.json'",
        *list(KNOWN.values())[2:],
    ]
    new_id, how = ids_of(codes)[1]
    assert how == "minted"
    assert new_id != "aaaaaaaaa2"


def test_a_thirty_percent_edit_keeps_its_id_by_similarity() -> None:
    edited = "df = pl.read_csv('orders_2026.csv', try_parse_dates=True)"
    ratio = similarity(normalize_code(edited), KNOWN["aaaaaaaaa2"])
    assert SIMILARITY_THRESHOLD <= ratio < 1
    report = resolve([CellKey(None, edited)], KNOWN)
    assert (report.cells[0].id, report.cells[0].resolution) == ("aaaaaaaaa2", "similar")
    assert report.cells[0].ratio == pytest.approx(ratio)


def test_an_edit_beyond_the_threshold_mints() -> None:
    rewritten = "import numpy as np\nweights = np.linspace(0, 1, 50)"
    assert all(similarity(rewritten, code) < SIMILARITY_THRESHOLD for code in KNOWN.values())
    assert ids_of([rewritten]) == [(mint_id(rewritten, set(KNOWN)), "minted")]


def test_exact_match_beats_a_better_positioned_similar_one() -> None:
    known = {"aaaaaaaaa1": "x = 1\ny = 2", "aaaaaaaaa2": "x = 1\ny = 3"}
    codes = ["x = 1\ny = 3", "x = 1\ny = 2"]
    assert ids_of(codes, known) == [("aaaaaaaaa2", "exact"), ("aaaaaaaaa1", "exact")]


def test_similarity_takes_the_best_pair_first() -> None:
    known = {"aaaaaaaaa1": "value = compute(alpha, beta, gamma)"}
    codes = ["value = compute(alpha, beta)", "value = compute(alpha, beta, gamma, delta)"]
    first, second = ids_of(codes, known)
    best = max(range(2), key=lambda i: similarity(codes[i], known["aaaaaaaaa1"]))
    assert [first, second][best] == ("aaaaaaaaa1", "similar")
    assert [first, second][1 - best][1] == "minted"


def test_similarity_ties_go_to_the_earlier_cell_then_the_smaller_id() -> None:
    known = {"aaaaaaaab2": "x = 10", "aaaaaaaab1": "x = 10"}
    report = resolve([CellKey(None, "x = 11"), CellKey(None, "x = 12")], known)
    assert [(r.id, r.resolution) for r in report.cells] == [
        ("aaaaaaaab1", "similar"),
        ("aaaaaaaab2", "similar"),
    ]


def test_duplicate_keyword_stays_with_the_cell_matching_the_known_code() -> None:
    report = resolve(
        [CellKey("aaaaaaaaa2", "df = 1  # copy"), CellKey("aaaaaaaaa2", KNOWN["aaaaaaaaa2"])],
        KNOWN,
    )
    assert report.cells[1].id == "aaaaaaaaa2"
    assert report.cells[0].id != "aaaaaaaaa2"
    assert report.duplicates == (0,)


def test_duplicate_keyword_without_known_stays_with_the_earlier_cell() -> None:
    report = resolve([CellKey("aaaaaaaaa2", "a"), CellKey("aaaaaaaaa2", "b")], None)
    assert report.cells[0].id == "aaaaaaaaa2"
    assert report.cells[1].resolution == "minted"
    assert report.duplicates == (1,)


def test_a_copy_that_still_matches_known_exactly_is_not_treated_as_the_original() -> None:
    # Both copies equal the known code: the earlier keeps the id.
    report = resolve(
        [CellKey("aaaaaaaaa2", KNOWN["aaaaaaaaa2"]), CellKey("aaaaaaaaa2", KNOWN["aaaaaaaaa2"])],
        KNOWN,
    )
    assert report.cells[0].id == "aaaaaaaaa2"
    assert report.cells[1].id not in KNOWN


@pytest.mark.parametrize(
    "keyword",
    ["short", "ABCDEFGHJK", "iiiiiiiiii", "a1b2c3d4e5f"],
    ids=["short", "upper", "excluded", "long"],
)
def test_malformed_keywords_are_ignored_and_reported(keyword: str) -> None:
    report = resolve([CellKey(keyword, KNOWN["aaaaaaaaa3"])], KNOWN)
    assert report.malformed == (0,)
    assert (report.cells[0].id, report.cells[0].resolution) == ("aaaaaaaaa3", "exact")


def test_identical_unkeyed_cells_get_distinct_stable_ids() -> None:
    first = ids_of(["x = 1", "x = 1"], None)
    assert first[0][0] != first[1][0]
    assert first == ids_of(["x = 1", "x = 1"], None)


def test_no_known_state_mints_every_cell() -> None:
    assert [how for _, how in ids_of(list(KNOWN.values()), None)] == ["minted"] * 4


def test_released_ids_are_reported_and_never_minted_again() -> None:
    report = resolve([CellKey(None, "something else entirely")], {"aaaaaaaaa1": "x"})
    assert report.released == ("aaaaaaaaa1",)
    assert report.cells[0].id != "aaaaaaaaa1"


def test_invalid_known_ids_are_ignored() -> None:
    report = resolve([CellKey(None, "x = 1")], {"NOT-AN-ID": "x = 1"})
    assert report.cells[0].resolution == "minted"


# ---- through the file reader ---------------------------------------------------------


def test_a_stock_save_is_recovered_from_the_known_state() -> None:
    original = read(stock_file(*KNOWN.values()), known=None)
    known = {c.id: normalize_code(c.code) for c in original.cells}
    canonical = write(original)
    # Stock marimo drops the keywords: simulate it on the canonical text.
    stripped = re.sub(r'alkera_id="[0-9a-z]{10}"(, )?', "", canonical).replace(
        "@app.cell()", "@app.cell"
    )
    recovered = read(stripped, known=known)
    assert [c.id for c in recovered.cells] == [c.id for c in original.cells]
    assert {c.resolution for c in recovered.cells} == {"exact"}


def test_known_codes_are_normalized_before_matching() -> None:
    text = stock_file("x = 1", "y = 2")
    ir = read(text, known={"aaaaaaaaa1": "x = 1   \r\n\n", "aaaaaaaaa2": "y = 2\n"})
    assert [(c.id, c.resolution) for c in ir.cells] == [
        ("aaaaaaaaa1", "exact"),
        ("aaaaaaaaa2", "exact"),
    ]


# ---- properties ------------------------------------------------------------------

_lines = st.lists(
    st.sampled_from(
        [
            "x = 1",
            "y = x + 2",
            "print(y)",
            "import math",
            "z = math.sqrt(16)",
            "for i in range(3):\n    total = i",
            "label = 'café ☕'",
            "data = [1, 2, 3]",
            "# a comment",
            "result = sum(data) / len(data)",
        ]
    ),
    min_size=1,
    max_size=4,
)
_cell_codes = _lines.map("\n".join)


@st.composite
def edited_notebooks(draw: st.DrawFn) -> tuple[list[tuple[str, str]], list[tuple[str | None, str]]]:
    """An original notebook and a sequence of edits, renames, reorders, inserts
    and deletes applied to it; each edited cell remembers its original id."""
    codes = draw(st.lists(_cell_codes, min_size=1, max_size=8))
    original = [(new_cell_id(), c) for c in codes]
    current: list[tuple[str | None, str]] = [(i, c) for i, c in original]
    for _ in range(draw(st.integers(0, 8))):
        op = draw(st.sampled_from(["edit", "reorder", "insert", "delete"]))
        if op == "edit" and current:
            index = draw(st.integers(0, len(current) - 1))
            cid, code = current[index]
            current[index] = (cid, code + "\n" + draw(_cell_codes))
        elif op == "reorder" and len(current) > 1:
            current = draw(st.permutations(current))
        elif op == "insert":
            index = draw(st.integers(0, len(current)))
            current.insert(index, (None, draw(_cell_codes)))
        elif op == "delete" and len(current) > 1:
            current.pop(draw(st.integers(0, len(current) - 1)))
    return original, current


@settings(max_examples=150, deadline=None)
@given(edited_notebooks())
def test_property_keywords_present_resolve_to_exactly_the_original_ids(
    case: tuple[list[tuple[str, str]], list[tuple[str | None, str]]],
) -> None:
    original, current = case
    known = {cid: normalize_code(code) for cid, code in original}
    keys = [CellKey(cid, normalize_code(code)) for cid, code in current]
    report = resolve(keys, known)
    for (cid, code), resolved in zip(current, report.cells, strict=True):
        if cid is not None:
            assert resolved.id == cid
            assert resolved.resolution == "keyword"
        elif resolved.id in known:
            # A new cell takes a released id only when its code matches that
            # deleted cell's code (the same cell coming back).
            assert resolved.id not in {c for c, _ in current if c is not None}
            assert similarity(normalize_code(code), known[resolved.id]) >= SIMILARITY_THRESHOLD
    assert len({r.id for r in report.cells}) == len(current)


@settings(max_examples=150, deadline=None)
@given(edited_notebooks())
def test_property_keywords_stripped_never_reassign_below_the_threshold(
    case: tuple[list[tuple[str, str]], list[tuple[str | None, str]]],
) -> None:
    original, current = case
    known = {cid: normalize_code(code) for cid, code in original}
    keys = [CellKey(None, normalize_code(code)) for _, code in current]
    report = resolve(keys, known)
    assert len({r.id for r in report.cells}) == len(current)
    for key, resolved in zip(keys, report.cells, strict=True):
        if resolved.id in known:
            assert similarity(key.code, known[resolved.id]) >= SIMILARITY_THRESHOLD
        else:
            assert resolved.resolution == "minted"


# ---- determinism across processes ------------------------------------------------------

_PROBE = """
import json, sys
from alkera_notebook.format import read
text, known = json.loads(sys.stdin.read())
print(json.dumps([[c.id, c.resolution] for c in read(text, known=known).cells]))
"""


def test_resolution_is_identical_in_two_processes() -> None:
    text = stock_file("x = 1", "x = 1", "y = 2", "import math\nz = math.pi")
    known = {"aaaaaaaaa1": "y = 3", "aaaaaaaaa2": "unrelated = True"}
    payload = json.dumps([text, known])
    results = [
        subprocess.run(
            [sys.executable, "-c", _PROBE],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "", "PYTHONPATH": ":".join(sys.path)},
            cwd=Path(__file__).parent,
        ).stdout
        for seed in ("1", "2")
    ]
    assert results[0] == results[1]
    ids = json.loads(results[0])
    assert ids[2] == ["aaaaaaaaa1", "similar"]
    assert ids[0][0] != ids[1][0]


# ---- the lazy similarity matching equals the specified sort ----------------------------


def reference_resolve(codes: list[str], known: dict[str, str]) -> list[tuple[str, str]]:
    """The identity document's algorithm for unkeyed cells, written as specified."""
    taken: dict[int, tuple[str, str]] = {}
    available = list(known)
    for i, code in enumerate(codes):
        for a in available:
            if known[a] == code:
                taken[i] = (a, "exact")
                available.remove(a)
                break
    unresolved = [i for i in range(len(codes)) if i not in taken]
    pairs = []
    for i in unresolved:
        for a in available:
            r = similarity(codes[i], known[a])
            if r >= SIMILARITY_THRESHOLD:
                pairs.append((r, i, a))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used: set[str] = set()
    for _, i, a in pairs:
        if i in taken or a in used:
            continue
        taken[i] = (a, "similar")
        used.add(a)
    pool = set(known)
    out = []
    for i, code in enumerate(codes):
        if i not in taken:
            minted = mint_id(code, pool | {t[0] for t in taken.values()})
            pool.add(minted)
            taken[i] = (minted, "minted")
        out.append(taken[i])
    return out


_small_codes = st.lists(
    st.sampled_from(["x = 1", "x = 2", "y = 1", "x = 10", "total = x + y", "print(x)", "x = 1  "]),
    min_size=1,
    max_size=3,
).map("\n".join)


@settings(max_examples=300, deadline=None)
@given(st.lists(_small_codes, max_size=7), st.lists(_small_codes, max_size=7))
def test_resolution_equals_the_specified_algorithm(
    codes: list[str], known_codes: list[str]
) -> None:
    known = {encode_id(i + 1): normalize_code(c) for i, c in enumerate(known_codes)}
    normalized = [normalize_code(c) for c in codes]
    report = resolve([CellKey(None, c) for c in normalized], known)
    assert [(r.id, r.resolution) for r in report.cells] == reference_resolve(normalized, known)
