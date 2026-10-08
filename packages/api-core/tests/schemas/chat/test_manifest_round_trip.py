"""Round-trip + defaulting tests for ChatManifest.

The manifest is a CACHE: every field
must default sensibly so a corrupt or partial manifest never bricks a
chat. The reader can always reconstruct from JSONL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.schemas.chat import ChatManifest, TokenTotals

_T = datetime(2026, 5, 26, tzinfo=UTC)


def test_empty_manifest_validates_with_all_defaults() -> None:
    """An empty dict is a valid manifest — every field defaults."""
    m = ChatManifest.model_validate({})
    assert m.session_id == ""
    assert m.tokens_total.input == 0
    assert m.cost_total == 0.0
    assert m.harness == {}


def test_partial_manifest_validates() -> None:
    """A manifest the writer only filled halfway in (e.g. crash mid-
    flight) must still load — the unset fields just default."""
    m = ChatManifest.model_validate({"session_id": "s1", "title": "Test"})
    assert m.session_id == "s1"
    assert m.title == "Test"
    assert m.cost_total == 0.0


def test_full_round_trip() -> None:
    original = ChatManifest(
        session_id="01HQZ",
        parent_session_id=None,
        title="Investigate",
        created_at=_T,
        updated_at=_T,
        cwd="/Users/x/proj",
        model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
        agent="general",
        tokens_total=TokenTotals(input=1234, output=567),
        cost_total=0.0123,
        harness={"name": "alkera-cli", "version": "0.1.0"},
    )
    dumped = original.model_dump(mode="json")
    reloaded = ChatManifest.model_validate(dumped)
    assert reloaded.model_dump(mode="json") == dumped


def test_unknown_fields_survive() -> None:
    payload = {
        "session_id": "s1",
        "title": "x",
        "future_aggregate": {"tools_used": 5},
    }
    m = ChatManifest.model_validate(payload)
    out = m.model_dump(mode="json")
    assert out["future_aggregate"] == {"tools_used": 5}


def test_corrupt_value_does_not_crash_when_using_extra_allow_default() -> None:
    """If a numeric field arrives as a string, Pydantic coerces — but
    if a field is the wrong shape entirely (e.g. tokens_total is a
    string), Pydantic raises. That's expected and acceptable; the
    chat handle catches the exception and falls back to folding JSONL.

    This test pins the documented behavior: a fully-broken manifest
    raises, the caller is expected to handle the fallback.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChatManifest.model_validate({"tokens_total": "not a dict"})
