"""Provenance of outputs: code hashes, lineage hashes and environment fingerprints.

- ``code_hash`` is marimo's: the MD5 hex digest of the cell's code as UTF-8,
  so snapshots written by stock marimo reattach by the same key.
- ``lineage_hash(c) = sha256(code_hash(c) || sorted(lineage_hash(p) for each
  direct parent p))``, hex digests concatenated with no separator (each is
  fixed length, so the concatenation is unambiguous). Direct parents carry
  their own ancestors, so the hash covers the whole upstream closure. It is
  computed over the kernel graph: the code each cell actually ran with.
- Cycles (a broken notebook) never hang: the cells of one strongly connected
  component are hashed together. Each member's hash covers its own code hash,
  the sorted code hashes of the other members, and the sorted lineage hashes
  of parents outside the component. The result depends only on the graph,
  never on traversal order.
- ``env_fingerprint = sha256(kind || spec bytes || python version || platform
  tag)``, fields joined by NUL so no two field splits collide.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence


def code_hash(code: str) -> str:
    return hashlib.md5(code.encode("utf-8"), usedforsecurity=False).hexdigest()


def env_fingerprint(kind: str, spec_bytes: bytes, python_version: str, platform_tag: str) -> str:
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\0")
    h.update(spec_bytes)
    h.update(b"\0")
    h.update(python_version.encode("utf-8"))
    h.update(b"\0")
    h.update(platform_tag.encode("utf-8"))
    return h.hexdigest()


def _components(nodes: list[str], children: dict[str, list[str]]) -> list[list[str]]:
    """Strongly connected components in reverse topological order (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            succ = children.get(node, [])
            if i < len(succ):
                work.append((node, i + 1))
                nxt = succ[i]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                out.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return out


def lineage_hashes(
    cells: Sequence[tuple[str, str]], edges: Iterable[tuple[str, str]]
) -> dict[str, str]:
    """Each cell's lineage hash over the graph ``edges`` (parent, child)."""
    codes = {cid: code_hash(code) for cid, code in cells}
    nodes = list(codes)
    parents: dict[str, set[str]] = {cid: set() for cid in nodes}
    children: dict[str, list[str]] = {cid: [] for cid in nodes}
    for parent, child in edges:
        if parent in codes and child in codes and parent != child:
            if parent not in parents[child]:
                parents[child].add(parent)
                children[parent].append(child)
    # Tarjan over parent links yields components parents-first.
    parent_lists = {cid: sorted(parents[cid]) for cid in nodes}
    out: dict[str, str] = {}
    for comp in _components(sorted(nodes), parent_lists):
        members = set(comp)
        outside = sorted({out[p] for m in comp for p in parents[m] if p not in members})
        for m in comp:
            h = hashlib.sha256(codes[m].encode("ascii"))
            if len(comp) > 1:
                for other in sorted(codes[o] for o in comp if o != m):
                    h.update(other.encode("ascii"))
                parts = outside
            else:
                parts = sorted(out[p] for p in parents[m])
            for part in parts:
                h.update(part.encode("ascii"))
            out[m] = h.hexdigest()
    return out
