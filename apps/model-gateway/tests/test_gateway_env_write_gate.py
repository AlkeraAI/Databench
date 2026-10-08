"""Gateway code never writes a credential into the process environment.

The process environment is shared by every client the gateway builds, so a
credential written there signs requests for every org in the process (botocore,
for one, reads ``AWS_BEARER_TOKEN_BEDROCK`` from it and prefers it over SigV4).
Credentials are bound per client and per request instead. This gate parses every
gateway module and refuses any environment write outside a short allowlist of
non-credential names; a write whose key is not a literal is refused outright,
since nothing can show it is not a credential.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

GATEWAY_SRC = Path(__file__).resolve().parents[1] / "model_gateway"

# Non-credential variables the gateway may set. Never add a credential here.
ALLOWED_ENV_WRITES = frozenset({"TIKTOKEN_CACHE_DIR"})

_ENVIRON_MUTATORS = frozenset({"setdefault", "update", "__setitem__"})


def _is_os_environ(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _literal_key(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def environment_writes(source: str) -> list[tuple[int, str | None]]:
    """Every environment write in ``source`` as (line, literal key or None)."""
    writes: list[tuple[int, str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _is_os_environ(target.value):
                    writes.append((node.lineno, _literal_key(target.slice)))
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        first = node.args[0] if node.args else None
        if func.attr in _ENVIRON_MUTATORS and _is_os_environ(func.value):
            key = None if func.attr == "update" else _literal_key(first)
            writes.append((node.lineno, key))
        if func.attr == "putenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            writes.append((node.lineno, _literal_key(first)))
    return writes


def test_gateway_writes_no_credential_to_the_environment() -> None:
    offenders = [
        f"{path.relative_to(GATEWAY_SRC.parent)}:{line} writes {key or '<non-literal key>'}"
        for path in sorted(GATEWAY_SRC.rglob("*.py"))
        for line, key in environment_writes(path.read_text(encoding="utf-8"))
        if key not in ALLOWED_ENV_WRITES
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'import os\nos.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", t)',
            [(2, "AWS_BEARER_TOKEN_BEDROCK")],
            id="setdefault",
        ),
        pytest.param(
            'import os\nos.environ["OPENAI_API_KEY"] = k', [(2, "OPENAI_API_KEY")], id="item"
        ),
        pytest.param("import os\nos.environ[name] = k", [(2, None)], id="non-literal-key"),
        pytest.param("import os\nos.environ.update(creds)", [(2, None)], id="update"),
        pytest.param(
            'import os\nos.putenv("AWS_SECRET_ACCESS_KEY", s)',
            [(2, "AWS_SECRET_ACCESS_KEY")],
            id="putenv",
        ),
        pytest.param(
            'from os import environ\nenviron["AWS_SESSION_TOKEN"] = s',
            [(2, "AWS_SESSION_TOKEN")],
            id="bare-environ",
        ),
        pytest.param('import os\nx = os.environ.get("AWS_REGION")', [], id="read-is-fine"),
        pytest.param('import os\nos.environ.pop("X", None)', [], id="pop-is-not-a-write"),
    ],
)
def test_the_scanner_sees_every_write_shape(
    source: str, expected: list[tuple[int, str | None]]
) -> None:
    assert environment_writes(source) == expected
