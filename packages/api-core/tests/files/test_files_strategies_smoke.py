"""The strategies are a deliverable, so they get their own tests.

A strategy that quietly stops drawing the hostile cases turns every property
test built on it green for the wrong reason. These tests pin the bias itself:
over a fixed number of draws the name generator must produce a non-UTF-8 name,
an NFD name, a bidi/ignorable name, a name at the byte ceiling, and — when
invalid names are asked for — every refusal code the naming contract defines.
"""

from __future__ import annotations

import tracemalloc
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import pytest
import strategies as fs
from alkera_core.files.names import NAME_MAX_BYTES, InvalidName, flags, name_key, validate
from hypothesis import HealthCheck, find, given, settings

#: Enough draws that each of the generator's branches is hit many times over;
#: the observed minimum for the rarest case is comfortably above 1.
DRAWS = 500

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = [
    pytest.mark.filterwarnings("ignore::hypothesis.errors.NonInteractiveExampleWarning"),
    pytest.mark.xdist_group("files_strategies_smoke"),
]


@pytest.fixture(scope="module")
def valid_sample() -> list[bytes]:
    return [fs.names().example() for _ in range(DRAWS)]


# ---------------------------------------------------------------------------
# Every strategy produces a usable example
# ---------------------------------------------------------------------------


def test_names_example_is_a_valid_name() -> None:
    validate(fs.names().example())


def test_name_pairs_example_collides_under_folding_but_not_bytewise() -> None:
    left, right = fs.name_pairs().example()
    validate(left)
    validate(right)
    assert left != right
    assert name_key(left) == name_key(right)


def test_trees_example_is_a_connected_tree() -> None:
    spec = fs.trees(max_nodes=50).example()
    assert len(spec.nodes) <= 50
    known = {()} | {node.path for node in spec.nodes}
    for node in spec.nodes:
        assert node.parent in known


def test_op_sequences_example_only_names_paths_the_tree_reached() -> None:
    spec, ops = fs.op_sequences(max_ops=30, max_nodes=20).example()
    assert len(ops) <= 30
    live: set[tuple[bytes, ...]] = {node.path for node in spec.nodes}
    for op in ops:
        tag = op[0]
        if tag == "create":
            parent, name = op[1], op[2]
            assert isinstance(parent, tuple)
            assert isinstance(name, bytes)
            live.add((*parent, name))
        else:
            target = op[1]
            assert isinstance(target, tuple)
            assert target in live, f"{tag!r} names a path the sequence never created"
            if tag == "rename":
                live.add((*target[:-1], op[2]))
            elif tag == "move":
                live.add((*op[2], target[-1]))


def test_byte_streams_example_yields_the_size_it_promised() -> None:
    size, chunks = fs.byte_streams(size_classes=("inline_edge",)).example()
    assert size in fs.SIZE_CLASSES["inline_edge"]
    assert isinstance(chunks, Iterator)
    body = b"".join(chunks)
    assert len(body) == size


def test_principals_example_carries_a_uuid_and_a_known_kind() -> None:
    principal = fs.principals().example()
    assert principal.kind in ("user", "team", "org")
    assert isinstance(principal.id, UUID)


def test_grants_example_carries_a_role_and_an_optional_expiry() -> None:
    grant = fs.grants().example()
    assert grant.role in fs.ROLES
    assert grant.expires_at is None or isinstance(grant.expires_at, datetime)


def test_byte_streams_rejects_an_unknown_size_class() -> None:
    with pytest.raises(ValueError, match="unknown size classes"):
        fs.byte_streams(size_classes=("gigantic",))


# ---------------------------------------------------------------------------
# The bias — one invariant per test
# ---------------------------------------------------------------------------


def test_names_draws_a_name_that_is_not_utf_8(valid_sample: list[bytes]) -> None:
    assert any(_undecodable(name) for name in valid_sample)


def test_names_draws_a_decomposed_name(valid_sample: list[bytes]) -> None:
    decomposed = [
        name
        for name in valid_sample
        if not _undecodable(name)
        and (text := name.decode("utf-8")) != unicodedata.normalize("NFC", text)
    ]
    assert decomposed


def test_names_draws_an_invisible_or_bidi_name(valid_sample: list[bytes]) -> None:
    assert any(flags(name).display_warning and not _undecodable(name) for name in valid_sample)


def test_names_draws_a_name_at_the_byte_ceiling(valid_sample: list[bytes]) -> None:
    assert any(len(name) == NAME_MAX_BYTES for name in valid_sample)


def test_names_draws_a_windows_hostile_name(valid_sample: list[bytes]) -> None:
    assert any(not flags(name).windows_safe for name in valid_sample)


def test_names_without_invalid_never_draws_one_linux_refuses(
    valid_sample: list[bytes],
) -> None:
    for name in valid_sample:
        validate(name)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("empty", id="empty"),
        pytest.param("nul", id="nul"),
        pytest.param("separator", id="separator"),
        pytest.param("dot", id="dot"),
        pytest.param("too_long", id="too_long"),
        pytest.param("control", id="control"),
        pytest.param("surrounding_space", id="surrounding_space"),
    ],
)
def test_names_with_invalid_draws_every_refusal_code(code: str) -> None:
    """Every refusal the naming contract defines must be reachable.

    `find()` searches the strategy for each code in turn instead of sampling
    and hoping: a rare branch that a fixed number of random draws happens to
    miss used to flake the whole suite red.
    """

    def refused_with_this_code(name: bytes) -> bool:
        try:
            validate(name)
        except InvalidName as exc:
            return exc.code == code
        return False

    drawn = find(
        fs.names(include_invalid=True),
        refused_with_this_code,
        settings=settings(max_examples=2_000, database=None),
    )

    with pytest.raises(InvalidName) as raised:
        validate(drawn)
    assert raised.value.code == code


def _undecodable(name: bytes) -> bool:
    try:
        name.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


# ---------------------------------------------------------------------------
# Properties that must hold on every draw
# ---------------------------------------------------------------------------


@given(fs.trees(max_nodes=25))
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_sibling_names_are_byte_unique(spec: fs.TreeSpec) -> None:
    """Byte-unique, and nothing stronger: a folder may hold README and readme."""
    siblings: dict[tuple[bytes, ...], set[bytes]] = {}
    for node in spec.nodes:
        bucket = siblings.setdefault(node.parent, set())
        assert node.name not in bucket
        bucket.add(node.name)


@given(fs.grants())
@settings(max_examples=25)
def test_a_grant_is_always_one_of_the_ladder_roles(grant: fs.GrantSpec) -> None:
    assert grant.role in fs.ROLES


# ---------------------------------------------------------------------------
# Laziness — the 32 MiB class must never exist as one bytes object
# ---------------------------------------------------------------------------


def test_a_large_stream_never_holds_more_than_a_chunk_in_memory() -> None:
    """Stream 32 MiB and assert the heap never grew anywhere near it.

    An implementation that built the payload eagerly (``iter([blob])``) peaks at
    32 MiB and fails here; the generator peaks at roughly two chunks.
    """
    size, chunks = fs.byte_streams(size_classes=("large",)).example()
    assert size == 32 << 20

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        total = 0
        for chunk in chunks:
            assert len(chunk) <= fs.MAX_CHUNK
            total += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert total == size
    assert peak < 8 << 20, f"peaked at {peak} bytes streaming {size}"


def test_the_stream_is_reproducible_from_its_seed() -> None:
    first = b"".join(fs.stream_bytes(70_000, seed=7))
    second = b"".join(fs.stream_bytes(70_000, seed=7))
    other = b"".join(fs.stream_bytes(70_000, seed=8))
    assert first == second
    assert first != other


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        pytest.param("empty", (0,), id="empty"),
        pytest.param("tiny", (1,), id="tiny"),
        pytest.param("inline_edge", (65_535, 65_536, 65_537), id="inline_edge"),
        pytest.param("block_edge", (4_194_303, 4_194_304, 4_194_305), id="block_edge"),
    ],
)
def test_each_size_class_streams_exactly_its_boundary_sizes(
    class_name: str, expected: tuple[int, ...]
) -> None:
    assert fs.SIZE_CLASSES[class_name] == expected
    for size in expected:
        assert sum(len(chunk) for chunk in fs.stream_bytes(size, seed=3)) == size
