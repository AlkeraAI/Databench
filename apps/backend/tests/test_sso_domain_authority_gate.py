"""Only the domain-claim owner may act on what an SSO connection lists.

``sso_connections.allowed_domains`` is a copy kept for the previous release.
Which domains an org's SSO speaks for is ``alkera_core.auth.sso_domains``
(domains staff assigned, one org per domain). Code that decides who may sign
in or be provisioned from the column would bring back the hole where typing a
domain was owning it, so this walks the product source and refuses any access
to the attribute outside the owner, which writes the copy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

#: Product code that could hold a session and a connection.
ROOTS = (
    REPO / "apps" / "backend" / "backend",
    REPO / "apps" / "worker" / "worker",
    REPO / "apps" / "model-gateway" / "model_gateway",
    REPO / "packages" / "api-core" / "alkera_core",
)

#: Where the attribute may be read or written, and why. This list only shrinks.
#: The column and the schema fields are definitions, not accesses, and need no entry.
ALLOWED: dict[str, str] = {
    "packages/api-core/alkera_core/auth/sso_domains.py": "the owner writes the saved list",
}


def _reads(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "allowed_domains"
    ]


def _sources() -> list[Path]:
    return sorted(p for root in ROOTS if root.is_dir() for p in root.rglob("*.py"))


def test_the_scan_sees_the_code_it_guards() -> None:
    """The gate is only as good as its reach: the roots exist and the scan
    finds the attribute where it is known to be."""
    scanned = {p.relative_to(REPO).as_posix() for p in _sources()}
    assert set(ALLOWED) <= scanned
    owner = REPO / "packages" / "api-core" / "alkera_core" / "auth" / "sso_domains.py"
    assert _reads(owner), "the owner's write of the list should be visible to the scan"


def test_nothing_outside_the_owner_reads_the_listed_domains() -> None:
    offenders = {
        rel: lines
        for path in _sources()
        if (rel := path.relative_to(REPO).as_posix()) not in ALLOWED and (lines := _reads(path))
    }
    assert offenders == {}, (
        "read verified domains from alkera_core.auth.sso_domains "
        f"(verified_domains / verified_owner), not the connection's list: {offenders}"
    )


@pytest.mark.parametrize("rel", sorted(ALLOWED))
def test_an_allowed_file_still_needs_its_exemption(rel: str) -> None:
    assert _reads(REPO / rel), f"{rel} no longer touches allowed_domains; remove it from ALLOWED"
