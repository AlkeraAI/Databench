"""No jq program in a workflow or a script names a variable after a jq keyword.

`--arg label x` then `$label` is a syntax error on the runner's jq (`label` is
a keyword: `label $name | break`), while the jq on a developer's Mac accepted
it, so the box-build record step passed every local test and failed in the
first deploy that ran it. The rule is checked on the text, where it holds on
every jq version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
JQ_KEYWORDS = {
    "__loc__",
    "and",
    "as",
    "catch",
    "def",
    "elif",
    "else",
    "end",
    "foreach",
    "if",
    "import",
    "include",
    "label",
    "not",
    "or",
    "reduce",
    "then",
    "try",
}
ARG = re.compile(r"--(?:arg|argjson|slurpfile|rawfile)\s+([A-Za-z_][A-Za-z0-9_]*)\s")
SOURCES = sorted(
    [
        *(REPO_ROOT / ".github").rglob("*.yml"),
        *(REPO_ROOT / "ops").rglob("*.sh"),
        *(REPO_ROOT / "scripts").rglob("*.sh"),
    ]
)


def test_there_are_jq_programs_to_check() -> None:
    assert sum(len(ARG.findall(p.read_text(encoding="utf-8"))) for p in SOURCES) > 20


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_jq_variable_is_named_after_a_keyword(path: Path) -> None:
    named = set(ARG.findall(path.read_text(encoding="utf-8")))
    assert not named & JQ_KEYWORDS, (
        f"{path}: jq variables named after keywords: {named & JQ_KEYWORDS}"
    )


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        pytest.param('jq -n --arg label "$L" ', {"label"}, id="the-box-record-bug"),
        pytest.param("jq --argjson if 1 ", {"if"}, id="argjson"),
        pytest.param('jq -n --arg box_label "$L" ', set(), id="renamed"),
    ],
)
def test_the_scan_finds_a_keyword_argument(text: str, bad: set[str]) -> None:
    assert set(ARG.findall(text)) & JQ_KEYWORDS == bad
