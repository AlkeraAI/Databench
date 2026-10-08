"""Only a probe's report may say a box keeps its orgs apart.

Placement puts a second org on a box that names ``BoxCapability.ORG_ISOLATION``
and on no other. A box names it through ``IsolationReport.capabilities``, which
answers from the mechanisms a probe proved; a list that carried the member
unconditionally would claim isolation for a host that has none, and two orgs
would share a container. So the member is referenced in shipped source only in
the module that owns the profile, where the report names it and the backend
reads it back.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _source_roots import shipped_source_roots
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import (
    EVERY_MECHANISM,
    ORG_NAMESPACES_NEEDS,
    IsolationMechanism,
    IsolationProfile,
    IsolationReport,
    profile_from_capabilities,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
HOME = "packages/api-core/alkera_core/compute/box_isolation.py"
MEMBER = "ORG_ISOLATION"


def _probe_claims(tree: ast.AST) -> set[int]:
    """The member as a key of a claims table whose row says only the probe
    may claim it (``{BoxCapability.ORG_ISOLATION: ClaimedBy.PROBE}``): that
    row hands the claim to the report, it does not make one."""
    return {
        id(key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Attribute)
        and key.attr == MEMBER
        and isinstance(value, ast.Attribute)
        and value.attr == "PROBE"
    }


def references(repo_root: Path, roots: list[str]) -> list[str]:
    """Every ``<name>.ORG_ISOLATION`` in shipped source outside the home."""
    found: list[str] = []
    for root in roots:
        for path in sorted((repo_root / root).rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if rel == HOME or "/_generated/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            probed = _probe_claims(tree)
            found.extend(
                f"{rel}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr == MEMBER
                and id(node) not in probed
            )
    return found


def test_only_the_isolation_report_names_the_capability() -> None:
    assert references(REPO_ROOT, shipped_source_roots(REPO_ROOT)) == []


def test_the_scan_catches_a_list_that_claims_isolation(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "caps.py").write_text(
        "from x import BoxCapability as B\n\nCAPS = (B.ORG_WORKERS, B.ORG_ISOLATION)\n",
        encoding="utf-8",
    )
    assert references(tmp_path, ["pkg"]) == ["pkg/caps.py:3"]


def test_a_claims_row_passes_only_when_it_hands_the_claim_to_the_probe(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "claims.py").write_text(
        "from x import BoxCapability as B, ClaimedBy as C\n\n"
        "PROBED = {B.ORG_ISOLATION: C.PROBE}\n"
        "LISTED = {B.ORG_ISOLATION: C.SUPERVISOR}\n",
        encoding="utf-8",
    )
    assert references(tmp_path, ["pkg"]) == ["pkg/claims.py:4"]


@pytest.mark.parametrize(
    "missing",
    [pytest.param(mechanism, id=mechanism.value) for mechanism in sorted(ORG_NAMESPACES_NEEDS)],
)
def test_a_report_missing_any_needed_mechanism_names_no_capability(
    missing: IsolationMechanism,
) -> None:
    report = IsolationReport(frozenset(IsolationMechanism) - {missing})
    assert report.capabilities() == ()
    assert profile_from_capabilities(report.capabilities()) == IsolationProfile.SINGLE_ORG


def test_a_report_with_every_needed_mechanism_names_it_and_reads_back() -> None:
    report = IsolationReport(ORG_NAMESPACES_NEEDS)
    assert report.capabilities() == (BoxCapability.ORG_ISOLATION,)
    assert [str(c) for c in report.capabilities()] == ["org_isolation"]
    assert profile_from_capabilities(["org_isolation"]) == IsolationProfile.ORG_NAMESPACES
    assert EVERY_MECHANISM.capabilities() == (BoxCapability.ORG_ISOLATION,)


@pytest.mark.parametrize(
    "said",
    [
        pytest.param(None, id="nothing"),
        pytest.param([], id="empty"),
        pytest.param(["org_workers"], id="workers-only"),
        pytest.param(["org_isolation_v2"], id="near-miss"),
    ],
)
def test_a_box_that_does_not_say_so_is_single_org(said: list[str] | None) -> None:
    assert profile_from_capabilities(said) == IsolationProfile.SINGLE_ORG
