"""The ``path_ids`` label codec: how a node is spelled as one ltree label.

Pure functions with no database in them: a label IS the node's per-drive ino
in base36, so the chain a node sits on can be read off its path without a
statement.
"""

from __future__ import annotations

import uuid
from typing import Final

#: The alphabet :func:`ino_label` writes and :func:`label_ino` reads.
_BASE36: Final = "0123456789abcdefghijklmnopqrstuvwxyz"


def ino_label(ino: int) -> str:
    """One ``path_ids`` label for a node: its per-drive ino in base36.

    A GiST key holds the whole value it indexes, so the label's width is what
    decides how deep a tree can be before an entry no longer fits on a page. An
    ino is dense and monotonic per drive, so base36 keeps a realistic path an
    order of magnitude narrower than the 33-byte UUID labels this replaced —
    the spec's 1,024-deep tree indexes where a UUID path broke around depth 60.
    """
    if ino < 0:
        raise ValueError(f"ino must be >= 0, got {ino}")
    if ino == 0:
        return "0"
    out: list[str] = []
    while ino:
        ino, remainder = divmod(ino, 36)
        out.append(_BASE36[remainder])
    return "".join(reversed(out))


def label_ino(label: str) -> int:
    """The inverse of :func:`ino_label`."""
    return int(label, 36)


def chain_inos(path_ids: str) -> list[int]:
    """The inos on a node's root-to-node chain, root first.

    A ``path_ids`` label IS the node's ino in base36, so the chain a node sits
    on is already written on the node: reading it costs no statement, no join
    and no ltree operator.
    """
    return [label_ino(label) for label in path_ids.split(".")]


def node_label(id: uuid.UUID) -> str:
    """An opaque ``path_ids`` label minted from a node id.

    Not what the product writes — a real path is a chain of :func:`ino_label`
    labels. This is kept for the tests that seed a synthetic path and never read
    it back, where a collision-free label matters and its width does not.

    An ltree label admits ``[A-Za-z0-9_]`` only, so a UUID's hyphens are dropped
    and a letter is prefixed because a bare hex string could read as a number.
    """
    return f"n{id.hex}"


def label_node_id(label: str) -> uuid.UUID:
    """The inverse of :func:`node_label`."""
    return uuid.UUID(label[1:])


__all__ = [
    "chain_inos",
    "ino_label",
    "label_ino",
    "label_node_id",
    "node_label",
]
