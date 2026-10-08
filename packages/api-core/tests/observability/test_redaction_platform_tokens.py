"""The platform's own credentials, and a password in a URL, never reach a log.

The free-text scrub knew provider keys and anything after ``Bearer``, but not
the tokens the platform mints itself when they are quoted bare (an exception
message, a logged command line), nor ``user:password@`` in a DSN or a clone
URL. Each kind of token is recognised by its prefix, read from the one module
that defines every prefix, so a new kind is covered the day it is added there.
A gate refuses a prefix spelled anywhere else.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from alkera_core.observability.redaction import REDACTED, scrub_text
from alkera_core.token_prefixes import TOKEN_PREFIXES

REPO = Path(__file__).resolve().parents[4]
OWNER = REPO / "packages" / "api-core" / "alkera_core" / "token_prefixes.py"
ROOTS = (REPO / "apps", REPO / "packages")

#: What a minted token's prefix looks like.
_PREFIX_SHAPE = re.compile(r"^alk_[a-z]+_$")


@pytest.mark.parametrize("prefix", TOKEN_PREFIXES)
def test_a_bare_platform_token_is_redacted(prefix: str) -> None:
    secret = prefix + "Q9x_-lmNopqrsTUVwxyz0123"
    scrubbed = scrub_text(f"refused credential {secret} for org 7")
    assert secret not in scrubbed
    assert scrubbed == f"refused credential {REDACTED} for org 7"


@pytest.mark.parametrize(
    ("logged", "expected"),
    [
        pytest.param(
            "connect postgresql+asyncpg://alkera:s3cr3t-pw@db:5432/app failed",
            f"connect postgresql+asyncpg://alkera:{REDACTED}@db:5432/app failed",
            id="dsn",
        ),
        pytest.param(
            "git clone https://bot:ghp_abc123@github.com/o/r.git",
            f"git clone https://bot:{REDACTED}@github.com/o/r.git",
            id="clone-url",
        ),
        pytest.param(
            "https://bot:${T}oken@corp.example/simple",
            f"https://bot:{REDACTED}@corp.example/simple",
            id="a-password-that-only-starts-like-a-reference",
        ),
    ],
)
def test_a_password_in_a_url_is_redacted_and_the_rest_kept(logged: str, expected: str) -> None:
    assert scrub_text(logged) == expected


@pytest.mark.parametrize(
    "plain",
    [
        pytest.param("https://user@host/path", id="a-user-with-no-password"),
        pytest.param("http://example.com:8080/health", id="a-port"),
        pytest.param("https://${U}:${T}@corp.example/simple", id="a-reference-to-a-variable"),
        pytest.param("mailto:ana@example.com", id="an-address"),
        pytest.param("the alk_pat_ prefix names a personal token", id="a-prefix-alone"),
    ],
)
def test_what_is_not_a_credential_is_left_as_it_is(plain: str) -> None:
    assert scrub_text(plain) == plain


def _prefix_literals(roots: tuple[Path, ...], owner: Path) -> list[str]:
    """String constants shaped like a token prefix, outside the owner module
    and the tests (which spell prefixes on purpose)."""
    found: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path == owner or "tests" in path.parts or "node_modules" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _PREFIX_SHAPE.match(node.value)
                ):
                    found.append(f"{path.relative_to(root.parent)}:{node.lineno} {node.value}")
    return found


def test_every_token_prefix_is_defined_in_one_module() -> None:
    assert _prefix_literals(ROOTS, OWNER) == []
    assert all(_PREFIX_SHAPE.match(prefix) for prefix in TOKEN_PREFIXES)


def test_a_prefix_spelled_elsewhere_is_caught(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mint.py").write_text(
        'TASK_PREFIX = "alk_task_"\nNOTE = "alk_task_ tokens"\nDOC = "alk_"\n', encoding="utf-8"
    )
    assert _prefix_literals((pkg,), tmp_path / "owner.py") == ["pkg/mint.py:1 alk_task_"]
