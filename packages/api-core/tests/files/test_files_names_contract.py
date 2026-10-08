"""The shared naming corpus, driven through the server's table.

The field in the browser and the namespace on the wire are two hand-kept copies
of one rule table, and the TypeScript one says so in its header: every rule is
"one the Files namespace itself refuses (``files.invalid_name.<rule>``), spelled
under the SAME rule name". Nothing enforced that, and the two had already
drifted — a name of three spaces was ``empty`` in the browser and
``surrounding_space`` here.

So one corpus is committed at ``packages/shared-openapi/name-rules.cases.json``
and both sides drive it: this module against :mod:`alkera_core.files.names`, and
``packages/chat-model/src/names.contract.test.ts`` against ``names.ts``. The
server is the authority — a disagreement is fixed by changing the client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.files.names import NAME_MAX_BYTES, InvalidName, flags, validate

REPO_ROOT = Path(__file__).resolve().parents[4]
CASES_FILE = REPO_ROOT / "packages" / "shared-openapi" / "name-rules.cases.json"

CORPUS: dict[str, Any] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = CORPUS["cases"]


def _verdict(name: str) -> str | None:
    try:
        validate(name.encode("utf-8"))
    except InvalidName as exc:
        return exc.code
    return None


def test_the_corpus_is_worth_driving() -> None:
    """A corpus that shrank to nothing, or to one verdict, would pass forever."""
    assert len(CASES) >= 50
    assert len({case["id"] for case in CASES}) == len(CASES)
    verdicts = {case["rule"] for case in CASES}
    # Every rule the server enforces is exercised, plus the accepted case.
    assert verdicts == {None, *CORPUS["rules"]}
    assert sum(1 for case in CASES if not case["windowsSafe"]) >= 10


def test_the_ceiling_is_the_same_number_on_both_sides() -> None:
    """Each side derives ``NAME_MAX`` less the pull sidecar independently, so a
    change to the sidecar on one side would otherwise move only one ceiling."""
    assert CORPUS["nameMaxBytes"] == NAME_MAX_BYTES


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[str(case["id"]) for case in CASES],
)
def test_the_server_agrees_with_the_corpus(case: dict[str, Any]) -> None:
    name: bytes = str(case["name"]).encode("utf-8")
    assert _verdict(str(case["name"])) == case["rule"]
    assert flags(name).windows_safe is case["windowsSafe"]
