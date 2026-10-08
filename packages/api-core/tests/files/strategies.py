"""Shared Hypothesis strategies for the Files suite.

Property tests over names, trees, operations, byte streams and grants only find
the bugs that matter when the generator is biased toward the hostile cases: a
plain ``st.text()`` never draws a non-UTF-8 name, a name at the byte ceiling or
a right-to-left override. Every Files property test draws from here.

Nothing in this module knows about a driver, a session or the database — the
strategies produce plain values (bytes, tuples, dataclasses, iterators) that a
test feeds to whatever layer it is exercising.
"""

from __future__ import annotations

import random
import string
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from alkera_core.files.names import NAME_MAX_BYTES, InvalidName, name_key, validate
from hypothesis import settings
from hypothesis import strategies as st

__all__ = [
    "MAX_CHUNK",
    "ROLES",
    "SIZE_CLASSES",
    "GrantSpec",
    "NodeKind",
    "Op",
    "PrincipalSpec",
    "Role",
    "TreeNode",
    "TreeSpec",
    "byte_streams",
    "grants",
    "name_pairs",
    "names",
    "op_sequences",
    "principals",
    "stream_bytes",
    "trees",
]

# A failing property test must print the reproduction blob: the seed is the
# whole value of a seeded generator.
settings.register_profile("alkera_files", parent=settings.get_profile("default"), print_blob=True)
settings.load_profile("alkera_files")


NodeKind = Literal["folder", "file", "symlink"]
Role = Literal["reader", "commenter", "writer", "manager", "owner"]
PrincipalKind = Literal["user", "team", "org"]

ROLES: Final[tuple[Role, ...]] = ("reader", "commenter", "writer", "manager", "owner")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_PLAIN_ALPHABET: Final = string.ascii_letters + string.digits + "-_ +()"

#: Names Windows refuses or mangles while Linux accepts them happily.
_WINDOWS_HOSTILE: Final = (
    b"CON",
    b"NUL.txt",
    b"COM1",
    b"LPT9",
    b"aux",
    b"trailing.",
    b"a<b",
    b'a"b',
    b"a|b",
    b"a?b",
    b"a*b",
    b"a:b",
    b"a\\b",
)

#: Bidi controls and default-ignorable code points — legal bytes that make a
#: display surface lie about the name.
_INVISIBLE_CHARS: Final = (
    "‮",  # right-to-left override
    "‪",  # left-to-right embedding
    "⁦",  # left-to-right isolate
    "​",  # zero width space
    "⁠",  # word joiner
    "﻿",  # zero width no-break space
    "\U000e0001",  # language tag
    "\U000e0041",  # tag latin capital A
)

_ACCENTED_STEMS: Final = ("café", "résumé", "naïve", "Ångström")

_DOTTED: Final = (
    b"..hidden",
    b"...",
    b"a.b.c",
    b".gitignore",
    b"archive.tar.gz",
    b"x..y",
    b"....",
)


def _plain_names() -> st.SearchStrategy[bytes]:
    return st.text(alphabet=_PLAIN_ALPHABET, min_size=1, max_size=24).map(
        lambda s: s.encode("utf-8")
    )


def _non_utf8_names() -> st.SearchStrategy[bytes]:
    """Bytes no UTF-8 decoder accepts — 0xFF never starts a valid sequence."""
    return st.builds(
        lambda head, tail: head.encode("utf-8") + b"\xff" + tail.encode("utf-8"),
        st.text(alphabet=_PLAIN_ALPHABET, min_size=0, max_size=6),
        st.text(alphabet=_PLAIN_ALPHABET, min_size=0, max_size=6),
    )


def _nfd_nfc_names() -> st.SearchStrategy[bytes]:
    return st.builds(
        lambda stem, form: unicodedata.normalize(form, stem).encode("utf-8"),
        st.sampled_from(_ACCENTED_STEMS),
        st.sampled_from(("NFC", "NFD")),
    )


def _case_names() -> st.SearchStrategy[bytes]:
    return st.builds(
        lambda stem, upper: (stem.upper() if upper else stem.lower()).encode("utf-8"),
        st.sampled_from(("readme", "Makefile", "straße", "İstanbul", "photo.JPG")),
        st.booleans(),
    )


def _invisible_names() -> st.SearchStrategy[bytes]:
    return st.builds(
        lambda stem, ch: (stem + ch + "txt").encode("utf-8"),
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
        st.sampled_from(_INVISIBLE_CHARS),
    )


def _at_the_byte_ceiling() -> st.SearchStrategy[bytes]:
    """Exactly at the ceiling, in one-, two- and multi-byte flavours.

    Grown from the ceiling rather than spelled, so lowering it moves these
    with it instead of turning the whole branch into draws the filter throws
    away.
    """
    two_byte = "é" * (NAME_MAX_BYTES // 2)
    dotted = b"deep." * (NAME_MAX_BYTES // 5)
    return st.sampled_from(
        (
            b"a" * NAME_MAX_BYTES,
            two_byte.encode("utf-8") + b"a" * (NAME_MAX_BYTES - len(two_byte.encode("utf-8"))),
            dotted + b"a" * (NAME_MAX_BYTES - len(dotted)),
        )
    )


def _refused_names() -> st.SearchStrategy[bytes]:
    """One name per refusal code in :func:`alkera_core.files.names.validate`."""
    return st.sampled_from(
        (
            b"",  # empty
            b"a\x00b",  # nul
            b"a/b",  # separator
            b".",  # dot
            b"..",  # dot
            b"a" * (NAME_MAX_BYTES + 1),  # too_long, one one-byte rune over
            ("é" * (NAME_MAX_BYTES // 2 + 1)).encode("utf-8"),  # too_long, in two-byte runes
            b"a\tb",  # control
            b"a\x7fb",  # control
            b" leading",  # surrounding_space
            b"trailing ",  # surrounding_space
        )
    )


def _is_valid(name: bytes) -> bool:
    try:
        validate(name)
    except InvalidName:
        return False
    return True


def names(*, include_invalid: bool = False) -> st.SearchStrategy[bytes]:
    """Names biased toward everything that breaks a naive implementation.

    Valid draws cover plain ASCII, non-UTF-8 bytes, Windows-hostile names,
    NFC/NFD and case pairs, the exact byte ceiling, bidi controls and
    default-ignorables, and dotted names. With ``include_invalid`` one name per
    refusal code joins in; without it every draw satisfies
    :func:`alkera_core.files.names.validate`.
    """
    branches: list[st.SearchStrategy[bytes]] = [
        _plain_names(),
        _non_utf8_names(),
        st.sampled_from(_WINDOWS_HOSTILE),
        _nfd_nfc_names(),
        _case_names(),
        _at_the_byte_ceiling(),
        _invisible_names(),
        st.sampled_from(_DOTTED),
    ]
    if include_invalid:
        branches.append(_refused_names())
        return st.one_of(branches)
    return st.one_of(branches).filter(_is_valid)


def name_pairs() -> st.SearchStrategy[tuple[bytes, bytes]]:
    """Two byte-distinct valid names that a folding client cannot tell apart.

    They share a :func:`~alkera_core.files.names.name_key`, so a case- or
    normalization-insensitive filesystem sees one name where the server sees two.
    """
    return st.sampled_from(
        (
            (b"README", b"readme"),
            (b"Photo.JPG", b"photo.jpg"),
            (
                "café".encode(),
                unicodedata.normalize("NFD", "café").encode("utf-8"),
            ),
            (
                "Ångström".encode(),
                unicodedata.normalize("NFD", "ångström").encode("utf-8"),
            ),
            ("straße".encode(), b"STRASSE"),
            (b"MAKEFILE", b"Makefile"),
        )
    ).filter(lambda pair: pair[0] != pair[1] and name_key(pair[0]) == name_key(pair[1]))


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One node of a generated tree. ``path`` is byte names from the root down."""

    path: tuple[bytes, ...]
    kind: NodeKind

    @property
    def name(self) -> bytes:
        return self.path[-1]

    @property
    def parent(self) -> tuple[bytes, ...]:
        return self.path[:-1]


@dataclass(frozen=True, slots=True)
class TreeSpec:
    """A whole tree, parents always before their children."""

    nodes: tuple[TreeNode, ...]

    def folders(self) -> tuple[tuple[bytes, ...], ...]:
        """Every path a child may be created under, the root included."""
        return ((), *(n.path for n in self.nodes if n.kind == "folder"))

    def paths(self) -> tuple[tuple[bytes, ...], ...]:
        return tuple(n.path for n in self.nodes)


@st.composite
def trees(draw: st.DrawFn, *, max_nodes: int = 50) -> TreeSpec:
    """A tree of at most ``max_nodes`` nodes with byte-unique sibling names.

    Siblings are unique byte-exactly and nothing else: a folder may hold
    ``README`` next to ``readme``, which is precisely the case a client that
    folds names has to be told about.
    """
    count = draw(st.integers(min_value=0, max_value=max_nodes))
    nodes: list[TreeNode] = []
    folders: list[tuple[bytes, ...]] = [()]
    taken: dict[tuple[bytes, ...], set[bytes]] = {(): set()}
    for _ in range(count):
        parent = draw(st.sampled_from(folders))
        siblings = frozenset(taken[parent])
        name = draw(names().filter(lambda n, taken=siblings: n not in taken))
        kind: NodeKind = draw(st.sampled_from(("folder", "file", "symlink")))
        taken[parent].add(name)
        path = (*parent, name)
        nodes.append(TreeNode(path=path, kind=kind))
        if kind == "folder":
            folders.append(path)
            taken[path] = set()
    return TreeSpec(nodes=tuple(nodes))


# ---------------------------------------------------------------------------
# Operation sequences
# ---------------------------------------------------------------------------

Op = tuple[object, ...]
"""A tagged operation: ``(tag, *args)`` where the tag names the verb."""


@st.composite
def op_sequences(
    draw: st.DrawFn, *, max_ops: int = 30, max_nodes: int = 20
) -> tuple[TreeSpec, tuple[Op, ...]]:
    """A starting tree plus a sequence of operations that only names live paths.

    Each op is a tagged tuple::

        ("create", parent_path, name, kind)
        ("rename", path, new_name)
        ("move", path, new_parent_path)
        ("trash", path)
        ("restore", path)
        ("grant", path, PrincipalSpec, role)
        ("revoke", path, PrincipalSpec)

    The generator tracks which paths are live and which are trashed, so a
    ``restore`` always names something that was trashed and a ``move`` never
    names a node's own descendant.
    """
    spec = draw(trees(max_nodes=max_nodes))
    live: list[TreeNode] = list(spec.nodes)
    trashed: list[TreeNode] = []
    ops: list[Op] = []
    op_count = draw(st.integers(min_value=0, max_value=max_ops))

    for _ in range(op_count):
        folders: list[tuple[bytes, ...]] = [(), *[n.path for n in live if n.kind == "folder"]]
        choices: list[str] = ["create"]
        if live:
            choices += ["rename", "move", "trash", "grant", "revoke"]
        if trashed:
            choices.append("restore")
        tag = draw(st.sampled_from(choices))

        if tag == "create":
            parent = draw(st.sampled_from(folders))
            siblings = frozenset(n.name for n in live if n.parent == parent)
            name = draw(names().filter(lambda n, taken=siblings: n not in taken))
            kind: NodeKind = draw(st.sampled_from(("folder", "file", "symlink")))
            live.append(TreeNode(path=(*parent, name), kind=kind))
            ops.append(("create", parent, name, kind))
            continue

        node = draw(st.sampled_from(trashed if tag == "restore" else live))

        if tag == "rename":
            siblings = frozenset(
                n.name for n in live if n.parent == node.parent and n.path != node.path
            )
            new_name = draw(names().filter(lambda n, taken=siblings: n not in taken))
            live.remove(node)
            live.append(TreeNode(path=(*node.parent, new_name), kind=node.kind))
            ops.append(("rename", node.path, new_name))
        elif tag == "move":
            targets = [
                folder
                for folder in folders
                if folder[: len(node.path)] != node.path
                and folder != node.parent
                and node.name not in {n.name for n in live if n.parent == folder}
            ]
            if not targets:
                continue
            target = draw(st.sampled_from(targets))
            live.remove(node)
            live.append(TreeNode(path=(*target, node.name), kind=node.kind))
            ops.append(("move", node.path, target))
        elif tag == "trash":
            live.remove(node)
            trashed.append(node)
            ops.append(("trash", node.path))
        elif tag == "restore":
            trashed.remove(node)
            live.append(node)
            ops.append(("restore", node.path))
        elif tag == "grant":
            ops.append(("grant", node.path, draw(principals()), draw(st.sampled_from(ROLES))))
        else:
            ops.append(("revoke", node.path, draw(principals())))

    return spec, tuple(ops)


# ---------------------------------------------------------------------------
# Byte streams
# ---------------------------------------------------------------------------

MAX_CHUNK: Final = 1 << 20
_PATTERN_LEN: Final = 4096

SIZE_CLASSES: Final[dict[str, tuple[int, ...]]] = {
    "empty": (0,),
    "tiny": (1,),
    "inline_edge": ((64 << 10) - 1, 64 << 10, (64 << 10) + 1),
    "block_edge": ((4 << 20) - 1, 4 << 20, (4 << 20) + 1),
    "large": (32 << 20,),
}


def _pattern(seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(_PATTERN_LEN))


def stream_bytes(size: int, seed: int) -> Iterator[bytes]:
    """Yield ``size`` bytes of a seeded repeating pattern, at most 1 MiB at a time.

    Lazy on purpose: the 32 MiB class must never exist as one ``bytes`` object,
    so a test can run it through a hasher or an uploader on a small heap.
    """
    pattern = _pattern(seed)
    produced = 0
    while produced < size:
        take = min(MAX_CHUNK, size - produced)
        start = produced % _PATTERN_LEN
        repeats = -(-(start + take) // _PATTERN_LEN)
        yield (pattern * repeats)[start : start + take]
        produced += take


def byte_streams(
    size_classes: Sequence[str] = ("empty", "tiny", "inline_edge", "block_edge", "large"),
) -> st.SearchStrategy[tuple[int, Iterator[bytes]]]:
    """``(size, chunk iterator)`` over the boundary sizes that break chunkers.

    Every size that matters is a boundary: the inline threshold, the block size,
    one byte either side of each, and one stream far larger than either.
    """
    unknown = [name for name in size_classes if name not in SIZE_CLASSES]
    if unknown:
        raise ValueError(f"unknown size classes: {sorted(unknown)}")
    sizes = [size for name in size_classes for size in SIZE_CLASSES[name]]
    return st.builds(
        lambda size, seed: (size, stream_bytes(size, seed)),
        st.sampled_from(sizes),
        st.integers(min_value=0, max_value=2**32 - 1),
    )


# ---------------------------------------------------------------------------
# Principals and grants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrincipalSpec:
    """``{kind, id}`` — the open registry of principal kinds, day-one members."""

    kind: PrincipalKind
    id: UUID


@dataclass(frozen=True, slots=True)
class GrantSpec:
    """A role held by a principal, optionally time-boxed."""

    principal: PrincipalSpec
    role: Role
    expires_at: datetime | None


_GRANT_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)


def principals() -> st.SearchStrategy[PrincipalSpec]:
    return st.builds(
        PrincipalSpec,
        kind=st.sampled_from(("user", "team", "org")),
        id=st.uuids(version=4),
    )


def grants() -> st.SearchStrategy[GrantSpec]:
    """A grant, half of them time-boxed, landing either side of the TTL boundary."""
    return st.builds(
        GrantSpec,
        principal=principals(),
        role=st.sampled_from(ROLES),
        expires_at=st.one_of(
            st.none(),
            st.integers(min_value=-3600, max_value=3600).map(
                lambda offset: _GRANT_EPOCH + timedelta(seconds=offset)
            ),
        ),
    )
