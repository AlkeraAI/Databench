"""A lifecycle transition may only move a deadline earlier.

A lease's ``expires_at`` is the bound that ends it. The holder renewing its
own lease moves it later, and a fresh grant starts one; any other write is a
transition someone else makes on the holder's lease (forcing it, fencing it,
handing it over), and setting it to ``now() + ttl`` there gave a dead holder a
new lease on life every time. Every SQL write of ``expires_at`` on
``file_leases`` under ``alkera_core.files`` is listed here: a renewal or a
grant by name, and every other write must be ``LEAST(expires_at, ...)`` or
``now()``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import alkera_core.files

FILES = Path(alkera_core.files.__file__).resolve().parent

#: The functions that may move a lease's deadline later, and why.
EXTENDS: dict[str, str] = {
    "leases.py::acquire": "a fresh grant starts its own deadline",
    "leases.py::heartbeat": "the holder's own renewal; a forced deadline is kept by its CASE",
}

SETS_EXPIRY = re.compile(r"\bexpires_at\s*=\s*(?P<value>[^,]+)")


def deadline_writes(source: str, filename: str) -> list[tuple[str, str]]:
    """Each ``(file::function, value)`` that sets ``expires_at`` in SQL on
    ``file_leases``."""
    found: list[tuple[str, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        sql = " ".join(
            part.value
            for part in ast.walk(node)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        if "file_leases" not in sql:
            continue
        for match in SETS_EXPIRY.finditer(sql):
            found.append((f"{filename}::{node.name}", match.group("value").strip()))
    return found


def _moves_earlier(value: str) -> bool:
    return value.startswith("LEAST(expires_at") or (value.startswith("now()") and "+" not in value)


def test_every_lease_deadline_write_only_moves_it_earlier_or_is_listed() -> None:
    offending: dict[str, str] = {}
    seen: set[str] = set()
    for path in sorted(FILES.rglob("*.py")):
        for where, value in deadline_writes(path.read_text(encoding="utf-8"), path.name):
            seen.add(where)
            if where not in EXTENDS and not _moves_earlier(value):
                offending[where] = value
    assert offending == {}
    assert set(EXTENDS) <= seen, "a listed function no longer sets the deadline: remove it"


def test_the_scan_catches_a_transition_that_pushes_a_deadline_out() -> None:
    decoy = """
async def force(session):
    await session.execute(text(
        "UPDATE file_leases SET expires_at = now() + make_interval(secs => :ttl) "
        "WHERE node_id = :node"
    ))

async def fence(session):
    await session.execute(text(
        "UPDATE file_leases SET expires_at = LEAST(expires_at, now()), released_at = now()"
    ))
"""
    writes = dict(deadline_writes(decoy, "decoy.py"))
    assert not _moves_earlier(writes["decoy.py::force"])
    assert _moves_earlier(writes["decoy.py::fence"])
