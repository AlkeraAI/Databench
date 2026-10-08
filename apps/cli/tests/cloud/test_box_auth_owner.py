"""A box process signs its requests with its credential in one place.

An org worker's credential is replaced every few minutes, and a client that
kept a copy of the bearer is refused every request once the copy expires. So
box and worker code never spells the bearer header, builds the org header
pair from a token, or hands the SDK a fixed token: each of those goes through
``alkera_cli.cloud.box_auth``, whose clients ask the credential holder at each
request.

The allowlist names the code that does it today for a reason unrelated to the
worker's credential. It may only shrink: a new site fails, and so does an
entry the tree no longer has.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import alkera_cli
import pytest

CLI = Path(alkera_cli.__file__).resolve().parent
OWNER = "cloud/box_auth.py"
#: Box and worker code, relative to the package.
SCOPE = ("cloud", "supervisor", "org_worker_channel.py", "commands/box.py")

#: ``(path, what) -> (count, reason)``.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("supervisor/machine_api.py", "header"): (
        2,
        "the supervisor's own machine credential, which is never replaced, on the "
        "stdlib client the supervisor runs with (it imports no HTTP library)",
    ),
    ("supervisor/box_log_shipping.py", "header"): (
        2,
        "the supervisor ships its own events on the same never-replaced machine "
        "credential, over stdlib urllib for the same reason",
    ),
    ("cloud/attachments.py", "header"): (
        1,
        "clears the header on the redirect to a signed content URL, so no "
        "credential follows the request off the backend",
    ),
}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def sites(source: str) -> Counter[str]:
    """What one module does that only the owner may: ``header`` (spells the
    bearer header or a bearer value), ``org_headers`` (builds the header pair
    from a token), ``sdk_token`` (hands the SDK a fixed token)."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if node.value.lower() == "authorization" or node.value.startswith("Bearer "):
                found["header"] += 1
        elif isinstance(node, ast.Call):
            if _callee(node) == "org_headers":
                found["org_headers"] += 1
            elif _callee(node) == "AlkeraClient" and any(k.arg == "token" for k in node.keywords):
                found["sdk_token"] += 1
    return found


def scan(root: Path, scope: Iterable[str]) -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for entry in scope:
        target = root / entry
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            rel = path.relative_to(root).as_posix()
            if rel == OWNER or not path.is_file():
                continue
            for what, count in sites(path.read_text(encoding="utf-8")).items():
                found[(rel, what)] += count
    return found


def violations(found: Counter[tuple[str, str]]) -> tuple[list[str], list[str]]:
    new = [
        f"{path}: {what} x{count}"
        for (path, what), count in sorted(found.items())
        if count > ALLOWED.get((path, what), (0, ""))[0]
    ]
    stale = [
        f"{path}: {what} (listed x{listed}, found x{found.get((path, what), 0)})"
        for (path, what), (listed, _reason) in sorted(ALLOWED.items())
        if found.get((path, what), 0) < listed
    ]
    return new, stale


def test_box_code_signs_requests_only_through_the_credential_owner() -> None:
    new, stale = violations(scan(CLI, SCOPE))
    assert not new, (
        "box code builds a bearer outside alkera_cli.cloud.box_auth; sign the client "
        "with the credential holder's auth instead:\n  " + "\n  ".join(new)
    )
    assert not stale, "remove these allowlist entries:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'H = {"Authorization": f"Bearer {t}"}\n', ["cloud/planted.py: header x2"], id="dict"
        ),
        pytest.param('x = "Bearer " + t\n', ["cloud/planted.py: header x1"], id="concat"),
        pytest.param("h = org_headers(t, org)\n", ["cloud/planted.py: org_headers x1"]),
        pytest.param("c = AlkeraClient(base_url=u, token=t)\n", ["cloud/planted.py: sdk_token x1"]),
        pytest.param('"""Carries an Authorization header."""\n', [], id="prose"),
        pytest.param("c = AlkeraClient(base_url=u)\n", [], id="sdk-without-token"),
    ],
)
def test_the_scan_catches_a_planted_bearer(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    (tmp_path / "cloud").mkdir()
    (tmp_path / "cloud" / "planted.py").write_text(source, encoding="utf-8")
    new, _stale = violations(scan(tmp_path, ("cloud",)))
    assert new == expected
