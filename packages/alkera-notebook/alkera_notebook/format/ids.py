"""Cell identity: the id alphabet, minting, and resolution.

A cell id is ten characters of lower-case Crockford base32 (50 bits). Each
cell carries its id as the first keyword of its own marimo decorator; nothing
else in the file repeats it. When a tool has stripped the keywords (a save by
stock marimo), :func:`resolve` recovers ids by matching cells against a known
prior state, with a similarity threshold, and mints new ids deterministically
when nothing matches well enough. It never assigns an id by position.
"""

from __future__ import annotations

import difflib
import hashlib
import heapq
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from alkera_notebook.format.ir import Resolution

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_LENGTH = 10
ID_BITS = 5 * ID_LENGTH
ID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{10}$")

SIMILARITY_THRESHOLD = 0.6
"""The least ``difflib`` ratio at which an edited cell keeps its id."""

RATIO_PREFIX = 65_536
"""Characters of each code string the similarity ratio looks at."""

_MINT_DOMAIN = b"alkera-cell-id\x00"


def is_cell_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def encode_id(bits: int) -> str:
    """The id for a 50-bit integer, most significant bits first."""
    if not 0 <= bits < 1 << ID_BITS:
        raise ValueError(f"cell id bits out of range: {bits}")
    return "".join(ALPHABET[(bits >> shift) & 31] for shift in range(ID_BITS - 5, -1, -5))


def new_cell_id() -> str:
    """A fresh random id, for cells an editor creates or pastes."""
    return encode_id(secrets.randbits(ID_BITS))


def mint_id(code: str, used: frozenset[str] | set[str]) -> str:
    """The deterministic id for an unresolved cell with ``code``.

    For ``n = 0, 1, 2, ...`` the first 50 bits of
    ``sha256("alkera-cell-id\\0" + code + "\\0" + n)`` are encoded; the first id
    not in ``used`` wins, so identical unresolved cells get distinct ids.
    """
    n = 0
    while True:
        digest = hashlib.sha256(
            _MINT_DOMAIN + code.encode("utf-8", "surrogatepass") + b"\x00" + str(n).encode()
        ).digest()
        candidate = encode_id(int.from_bytes(digest[:7], "big") >> (56 - ID_BITS))
        if candidate not in used:
            return candidate
        n += 1


def normalize_code(code: str) -> str:
    """Code as identity compares it.

    Line endings become LF, trailing whitespace is removed from every line and
    trailing blank lines are removed. Nothing else changes.
    """
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stripped = [line.rstrip() for line in lines]
    while stripped and not stripped[-1]:
        stripped.pop()
    return "\n".join(stripped)


def similarity(a: str, b: str) -> float:
    """``difflib`` ratio of two normalized codes (first 64 Ki characters)."""
    matcher = difflib.SequenceMatcher(None, a[:RATIO_PREFIX], b[:RATIO_PREFIX], autojunk=False)
    return matcher.ratio()


def _similarity_matches(
    cells: list[tuple[int, str]], candidates: list[tuple[str, str]]
) -> list[tuple[int, str, float]]:
    """Greedy best-first matching of cells to known ids by similarity.

    The result equals sorting every pair with a ratio of at least the
    threshold by ``(-ratio, cell index, id)`` and taking each pair whose cell
    and id are both still free. It is computed lazily: pairs enter a heap
    keyed by ``quick_ratio()`` (an upper bound of the ratio), and a pair's
    exact ratio is computed only when it reaches the top. An exact entry
    reaching the top is therefore no smaller than any unexplored pair's ratio,
    and ties order exactly as the sort does, so the assignment is the same
    while most pairs are never measured.
    """
    heap: list[tuple[float, int, str, int]] = []  # (-value, index, id, exact?)
    matchers: dict[str, difflib.SequenceMatcher[str]] = {}
    texts = {index: code[:RATIO_PREFIX] for index, code in cells}
    for candidate, known_code in candidates:
        matcher = difflib.SequenceMatcher(None, autojunk=False)
        matcher.set_seq2(known_code[:RATIO_PREFIX])
        matchers[candidate] = matcher
        for index, _ in cells:
            matcher.set_seq1(texts[index])
            if matcher.real_quick_ratio() < SIMILARITY_THRESHOLD:
                continue
            bound = matcher.quick_ratio()
            if bound >= SIMILARITY_THRESHOLD:
                heap.append((-bound, index, candidate, 1))
    heapq.heapify(heap)
    taken_cells: set[int] = set()
    taken_ids: set[str] = set()
    matches: list[tuple[int, str, float]] = []
    while heap:
        value, index, candidate, is_bound = heapq.heappop(heap)
        if index in taken_cells or candidate in taken_ids:
            continue
        if is_bound:
            matcher = matchers[candidate]
            matcher.set_seq1(texts[index])
            ratio = matcher.ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                heapq.heappush(heap, (-ratio, index, candidate, 0))
            continue
        taken_cells.add(index)
        taken_ids.add(candidate)
        matches.append((index, candidate, -value))
    return matches


@dataclass(frozen=True)
class CellKey:
    """What resolution needs from one cell: its keyword (if any) and code."""

    keyword: str | None
    code: str


@dataclass(frozen=True)
class Resolved:
    id: str
    resolution: Resolution
    ratio: float | None = None


@dataclass(frozen=True)
class ResolutionReport:
    """The result of :func:`resolve`.

    ``cells`` follows the input order. ``duplicates`` lists the indexes of cells
    whose keyword another cell kept; ``malformed`` the indexes whose keyword was
    not a valid id; ``released`` the known ids no cell received (cells that no
    longer exist).
    """

    cells: tuple[Resolved, ...]
    duplicates: tuple[int, ...]
    malformed: tuple[int, ...]
    released: tuple[str, ...]


def resolve(cells: Sequence[CellKey], known: Mapping[str, str] | None = None) -> ResolutionReport:
    """Give every cell an id.

    ``cells`` are in file order; ``code`` must be normalized
    (:func:`normalize_code`). ``known`` maps id to normalized code for the last
    version of the notebook the caller trusts, in that version's order; its
    codes are normalized again here, which is harmless. The steps are, in
    order: the cell's own keyword (a duplicate stays with the cell whose code
    equals the known code for it, else with the earlier cell); exact code match
    against unassigned known ids; similarity of at least 0.6, best pairs first
    with ties broken by cell order then id; deterministic minting.
    """
    known_codes = {k: normalize_code(v) for k, v in (known or {}).items() if is_cell_id(k)}
    assigned: dict[int, Resolved] = {}
    taken: dict[str, int] = {}
    duplicates: list[int] = []
    malformed: list[int] = []

    for index, cell in enumerate(cells):
        keyword = cell.keyword
        if keyword is None:
            continue
        if not is_cell_id(keyword):
            malformed.append(index)
            continue
        if keyword not in taken:
            taken[keyword] = index
            continue
        holder = taken[keyword]
        expected = known_codes.get(keyword)
        if expected is not None and cell.code == expected and cells[holder].code != expected:
            taken[keyword] = index
            duplicates.append(holder)
        else:
            duplicates.append(index)
    for keyword, index in taken.items():
        assigned[index] = Resolved(keyword, "keyword")

    unresolved = [i for i in range(len(cells)) if i not in assigned]
    available = [k for k in known_codes if k not in taken]

    for index in unresolved:
        code = cells[index].code
        for candidate in available:
            if known_codes[candidate] == code:
                assigned[index] = Resolved(candidate, "exact", 1.0)
                available.remove(candidate)
                break
    unresolved = [i for i in unresolved if i not in assigned]

    for index, candidate, ratio in _similarity_matches(
        [(i, cells[i].code) for i in unresolved], [(k, known_codes[k]) for k in available]
    ):
        assigned[index] = Resolved(candidate, "similar", ratio)

    # Released known ids stay out of the minting pool, so a deleted cell's id
    # never lands on unrelated code.
    used = {r.id for r in assigned.values()} | set(known_codes)
    for index in range(len(cells)):
        if index in assigned:
            continue
        minted = mint_id(cells[index].code, used)
        used.add(minted)
        assigned[index] = Resolved(minted, "minted")

    final_ids = {r.id for r in assigned.values()}
    return ResolutionReport(
        cells=tuple(assigned[i] for i in range(len(cells))),
        duplicates=tuple(sorted(duplicates)),
        malformed=tuple(malformed),
        released=tuple(k for k in known_codes if k not in final_ids),
    )
