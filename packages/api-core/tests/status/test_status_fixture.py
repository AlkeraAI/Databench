"""The facts the web's tests stand in for the server with are the server's own.

``packages/api-core/tests/fixtures/status/facts.json`` holds facts exactly as a read carries
them. The web renders them in its own tests, so a sentence reworded here and
not there would leave the web green on words no server sends. The file must
be what ``generate.py`` writes today.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from alkera_core.status import registered

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "status"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("status_facts_generate", FIXTURES / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_is_what_the_vocabularies_write_today() -> None:
    written = (FIXTURES / "facts.json").read_text(encoding="utf-8")
    assert written == _generator().render(), (
        "rerun packages/api-core/tests/fixtures/status/generate.py"
    )


def test_the_named_facts_cover_every_state_of_their_subject() -> None:
    corpus = json.loads((FIXTURES / "facts.json").read_text(encoding="utf-8"))
    for subject in ("chat", "workspace"):
        covered = {case["fact"]["state"] for case in corpus[subject].values()}
        assert covered == set(registered()[subject].states), subject
