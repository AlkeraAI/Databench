"""The public API description cites no internal spec.

Route and schema docstrings become the OpenAPI ``description`` and ``summary``,
and from there the docstrings of both generated SDKs. A pointer at an internal
design document ("SPEC §9.2", "PLUGINS §4.1 / D-FOO", "R17",
"Phase 3") means nothing to a customer reading the SDK: the description must
state the rule itself. A standards citation (``RFC 9110 §15.5.20``) is a public
reference and stays.

This reads the committed ``packages/shared-openapi/openapi.json`` (the drift job
keeps it equal to what the app generates), so a new leak fails here on the PR
that adds it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI = REPO_ROOT / "packages" / "shared-openapi" / "openapi.json"

#: The keys whose text a reader sees as prose.
_PROSE_KEYS = frozenset({"description", "summary"})

#: Technical terms that look like a plan id (a letter or two then digits) but
#: are vocabulary of the thing described. ``T0`` is the billing model's name for
#: the start of an org's period.
_TERMS = frozenset({"T0"})

#: A section sign is public when it continues a standards citation
#: ("RFC 9110 §15.5.20").
_RFC_TAIL = re.compile(r"\bRFC \d+ $")

#: Each rule names one shape of internal reference.
_RULES: dict[str, re.Pattern[str]] = {
    "section-sign": re.compile(r"§"),
    "spec-name": re.compile(r"\b(?:SPEC|PLUGINS|SUBAGENTS)\b"),
    "ledger-id": re.compile(r"\bledger [A-Z]+\d"),
    "decision-tag": re.compile(r"\bD-[A-Z]{2,}\b"),
    "phase": re.compile(r"\bPhase \d"),
    "plan-id": re.compile(r"\b[A-Z]{1,2}\d+(?:\.\d+)*\b"),
}


def _violations(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for rule, pattern in _RULES.items():
        for match in pattern.finditer(text):
            token = match.group(0).strip()
            if rule == "plan-id" and token in _TERMS:
                continue
            if rule == "section-sign" and _RFC_TAIL.search(text, 0, match.start()):
                continue
            start = max(0, match.start() - 40)
            found.append((rule, text[start : match.end() + 20].replace("\n", " ")))
    return found


def _prose(node: object, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in _PROSE_KEYS and isinstance(value, str):
                yield here, value
            else:
                yield from _prose(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _prose(value, f"{path}/{index}")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Widgets are sorted by name (SPEC §9.2).", id="spec-section"),
        pytest.param("the rule in §9.2 forbids", id="bare-section"),
        pytest.param("kept for the first widget (PLUGINS §4.1 / D-FOO)", id="plugins-decision"),
        pytest.param("on the widget page (SPEC A9, ledger Q2)", id="spec-ledger"),
        pytest.param("R17 admits no widget without one", id="requirement-id"),
        pytest.param("the widget marker of N7.4", id="dotted-id"),
        pytest.param("a widget plus a handle (C9)", id="paren-id"),
        pytest.param("Phase 3 writes ``present``", id="phase"),
        pytest.param("a widget decision (D-FOO-2)", id="decision-with-number"),
    ],
)
def test_detector_flags_internal_references(text: str) -> None:
    assert _violations(text), text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("otherwise an RFC 8628 §3.5 error", id="rfc-section"),
        pytest.param("a 413 per RFC 9110 §15.5.14", id="rfc-four-digit"),
        pytest.param("ordinary ledger rows, not restated", id="ledger-word"),
        pytest.param("a name that is not UTF-8 travels escaped", id="utf-8"),
        pytest.param("one ZIP64 archive", id="zip64"),
        pytest.param("The org period T0; defaults to now", id="billing-term"),
        pytest.param("errors carry ids, never names", id="plain-rule"),
    ],
)
def test_detector_passes_public_prose(text: str) -> None:
    assert _violations(text) == []


def test_committed_openapi_cites_no_internal_spec() -> None:
    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    leaks = [
        f"{path}: [{rule}] …{excerpt}…"
        for path, text in _prose(schema)
        for rule, excerpt in _violations(text)
    ]
    assert leaks == [], "internal spec references in openapi.json:\n" + "\n".join(leaks)
