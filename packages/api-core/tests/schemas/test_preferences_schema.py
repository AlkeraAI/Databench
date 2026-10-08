"""Tests for the `Preferences` versioned schema.

Covers the defaults, the version-stamp invariant, and the load-bearing
forward-compat property: an older reader must preserve an unknown field a
newer writer added (so a concurrent read-modify-write of
`preferences.yml` never clobbers a preference it doesn't understand).

Includes a lineage check that every historical fixture under
`packages/api-core/tests/fixtures/preferences/v*.json` still loads with the current reader
— the backward-compat regression net (see CLAUDE.md §versioning).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.preferences import (
    ORG_SCOPED_KEYS,
    DesktopPreferences,
    Preferences,
    ToolDisclosure,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "preferences"


def test_defaults() -> None:
    prefs = Preferences()
    assert prefs.show_banner is True
    assert prefs.reduce_motion is False
    assert prefs.tool_card_border is True
    assert prefs.default_permission_mode == "default"
    assert prefs.tool_card_load_policy == "use_finished_default"
    assert prefs.tool_card_disclosure == {}
    assert prefs.model_efforts == {}
    assert prefs.telemetry_enabled is True
    assert prefs.default_chat_model is None
    assert prefs.default_chat_effort is None
    assert prefs.sql_statement_timeout_seconds == 900
    assert prefs.onboarded_cli is False
    assert prefs.onboarded_extension is False
    assert prefs.schema_version == Preferences.SCHEMA_VERSION


def test_tool_disclosure_registry_round_trips() -> None:
    """A per-kind disclosure override survives serialize → validate, so a
    read-modify-write of the registry keeps the user's choice."""
    prefs = Preferences(
        tool_card_disclosure={"bash": ToolDisclosure(on_spawn=False, on_finish=True)}
    )
    restored = Preferences.model_validate(prefs.model_dump(mode="json"))
    assert restored.tool_card_disclosure["bash"].on_spawn is False
    assert restored.tool_card_disclosure["bash"].on_finish is True


def test_tool_disclosure_defaults() -> None:
    # The nested model defaults to open/open — the same as the CLI's generic
    # built-in, so an entry created without args is a no-op override.
    disclosure = ToolDisclosure()
    assert disclosure.on_spawn is True
    assert disclosure.on_finish is True


def test_legacy_show_thinking_is_removed_instead_of_preserved_as_unknown() -> None:
    """Catch a migration that removes the typed field but accidentally preserves
    it through ``extra='allow'``, leaving two independent thinking controls."""
    restored = Preferences.model_validate({"schema_version": "1.7.0", "show_thinking": True})
    payload = restored.model_dump(mode="json")
    assert "show_thinking" not in Preferences.model_fields
    assert "show_thinking" not in payload
    assert payload["schema_version"] == Preferences.SCHEMA_VERSION


def test_tool_card_border_non_default_round_trips() -> None:
    """A NON-default `tool_card_border` (False — spine) survives serialize →
    validate, so a user who turned the border off keeps that choice across a
    read-modify-write. The default test only proves the True default; this
    proves the field is actually persisted and read back, not silently dropped
    (an impl that ignored the field on load would pass `test_defaults` but fail
    here)."""
    payload = Preferences(tool_card_border=False).model_dump(mode="json")
    assert payload["tool_card_border"] is False
    restored = Preferences.model_validate(payload)
    assert restored.tool_card_border is False


def test_v1_2_0_fixture_carries_spine_choice() -> None:
    """The v1_2_0 fixture pins `tool_card_border: false`. The lineage test only
    checks the version stamps to current; this pins that the SPINE choice in
    that fixture is actually decoded — the regression an impl that dropped the
    field on the historical-load path would otherwise hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_2_0.json").read_text())
    assert payload["tool_card_border"] is False  # guard the fixture itself
    prefs = Preferences.model_validate(payload)
    assert prefs.tool_card_border is False


def test_theme_non_default_round_trips() -> None:
    """A chosen theme name survives serialize → validate, so a read-modify-write
    of `preferences.yml` keeps it. `test_defaults` only proves the empty default;
    this proves the field is actually persisted and read back (an impl that
    dropped it on load would pass the defaults test but fail here)."""
    payload = Preferences(theme="alkera-violet").model_dump(mode="json")
    assert payload["theme"] == "alkera-violet"
    restored = Preferences.model_validate(payload)
    assert restored.theme == "alkera-violet"


def test_theme_defaults_to_empty_meaning_auto_detect() -> None:
    """The default theme is the empty string — the sentinel that means 'never
    chosen, auto-detect the terminal background'. A non-empty default would make
    a fresh user skip auto-detection."""
    assert Preferences().theme == ""


def test_v1_3_0_fixture_carries_theme_choice() -> None:
    """The v1_3_0 fixture pins `theme: alkera-slate`. The lineage test only checks
    the version stamps to current; this pins that the chosen theme in that fixture
    is actually DECODED — the regression an impl that dropped `theme` on the
    historical-load path would otherwise hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_3_0.json").read_text())
    assert payload["theme"] == "alkera-slate"  # guard the fixture itself
    prefs = Preferences.model_validate(payload)
    assert prefs.theme == "alkera-slate"


def test_chat_defaults_round_trip() -> None:
    """A chosen Default Chat Model + Effort survive serialize → validate, so a
    read-modify-write of `preferences.yml` keeps the user's new-chat seed."""
    payload = Preferences(
        default_chat_model="claude-opus-4.5", default_chat_effort="high"
    ).model_dump(mode="json")
    assert payload["default_chat_model"] == "claude-opus-4.5"
    assert payload["default_chat_effort"] == "high"
    restored = Preferences.model_validate(payload)
    assert restored.default_chat_model == "claude-opus-4.5"
    assert restored.default_chat_effort == "high"


def test_literal_none_chat_effort_round_trips_without_becoming_null() -> None:
    """Catch normalization that conflates the user-facing ``"none"`` effort
    with Python/JSON null, which means no saved override rather than explicit off."""
    payload = Preferences(
        default_chat_model="claude-opus-4.5",
        default_chat_effort="none",
    ).model_dump(mode="json")
    restored = Preferences.model_validate(payload)
    assert payload["default_chat_effort"] == "none"
    assert restored.default_chat_effort == "none"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_chat_default_blank_normalizes_to_none(blank: str) -> None:
    """A cleared field collapses to None (never a meaningless empty string), so the
    resolver treats 'cleared' and 'never set' identically."""
    prefs = Preferences(default_chat_model=blank, default_chat_effort=blank)
    assert prefs.default_chat_model is None
    assert prefs.default_chat_effort is None


def test_v1_4_0_fixture_carries_chat_defaults() -> None:
    """The v1_4_0 fixture pins the Default Chat Model + Effort. The lineage test
    only checks the version stamp; this pins that the two fields are actually
    DECODED — the regression an impl that dropped them on load would hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_4_0.json").read_text())
    assert payload["default_chat_model"] == "claude-opus-4.5"  # guard the fixture
    prefs = Preferences.model_validate(payload)
    assert prefs.default_chat_model == "claude-opus-4.5"
    assert prefs.default_chat_effort == "high"


def test_v1_3_0_fixture_has_no_chat_defaults_field() -> None:
    """A pre-1.4 file (no `default_chat_model`) loads with the field defaulting to
    None — the additive minor bump needs no migration, and an older file degrades
    to 'never set' rather than failing."""
    payload = json.loads((FIXTURE_ROOT / "v1_3_0.json").read_text())
    assert "default_chat_model" not in payload  # guard the fixture
    prefs = Preferences.model_validate(payload)
    assert prefs.default_chat_model is None
    assert prefs.default_chat_effort is None


def test_telemetry_disabled_round_trips() -> None:
    """A NON-default `telemetry_enabled` (False — opted out) survives serialize →
    validate, so a user who turned telemetry off keeps that choice across a
    read-modify-write. `test_defaults` only proves the True default; this proves
    the field is actually persisted and read back (an impl that dropped it on load
    would pass the defaults test but silently re-enable telemetry here)."""
    payload = Preferences(telemetry_enabled=False).model_dump(mode="json")
    assert payload["telemetry_enabled"] is False
    restored = Preferences.model_validate(payload)
    assert restored.telemetry_enabled is False


def test_v1_5_0_fixture_carries_telemetry_choice() -> None:
    """The v1_5_0 fixture pins `telemetry_enabled: false`. The lineage test only
    checks the version stamp; this pins that the opt-out in that fixture is
    actually DECODED — the regression an impl that dropped the field on the
    historical-load path (silently re-enabling telemetry) would otherwise hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_5_0.json").read_text())
    assert payload["telemetry_enabled"] is False  # guard the fixture itself
    prefs = Preferences.model_validate(payload)
    assert prefs.telemetry_enabled is False


def test_v1_4_0_fixture_has_no_telemetry_field() -> None:
    """A pre-1.5 file (no `telemetry_enabled`) loads with the field defaulting to
    True — the additive minor bump needs no migration, and an older file degrades
    to telemetry-on (the same default a fresh user gets)."""
    payload = json.loads((FIXTURE_ROOT / "v1_4_0.json").read_text())
    assert "telemetry_enabled" not in payload  # guard the fixture
    prefs = Preferences.model_validate(payload)
    assert prefs.telemetry_enabled is True


def test_sql_timeout_non_default_round_trips() -> None:
    """A NON-default `sql_statement_timeout_seconds` survives serialize → validate,
    so a user who shortened the timeout keeps it across a read-modify-write.
    `test_defaults` only proves the 900 default; this proves the field is actually
    persisted and read back (an impl that dropped it on load would pass the defaults
    test but silently revert to 15 min)."""
    payload = Preferences(sql_statement_timeout_seconds=120).model_dump(mode="json")
    assert payload["sql_statement_timeout_seconds"] == 120
    restored = Preferences.model_validate(payload)
    assert restored.sql_statement_timeout_seconds == 120


def test_sql_timeout_zero_means_no_limit_round_trips() -> None:
    """0 (disable the timeout) is a meaningful, distinct value — it must survive the
    round-trip rather than being coerced to the 900 default (which would silently
    re-impose a limit the user turned off)."""
    payload = Preferences(sql_statement_timeout_seconds=0).model_dump(mode="json")
    assert payload["sql_statement_timeout_seconds"] == 0
    restored = Preferences.model_validate(payload)
    assert restored.sql_statement_timeout_seconds == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(-5, 900, id="negative-degrades-to-default"),
        pytest.param("not-a-number", 900, id="garbage-degrades-to-default"),
        pytest.param(None, 900, id="none-degrades-to-default"),
        pytest.param("300", 300, id="numeric-string-coerces"),
        pytest.param(0, 0, id="zero-preserved"),
    ],
)
def test_sql_timeout_sanitizes_bad_values(raw: object, expected: int) -> None:
    """A negative / non-numeric / null timeout degrades to the 900 default rather than
    bricking the whole preferences load (a corrupt scalar must not fail the file); a
    numeric string coerces; a legitimate 0 is preserved."""
    prefs = Preferences.model_validate(
        {"schema_version": "1.6.0", "sql_statement_timeout_seconds": raw}
    )
    assert prefs.sql_statement_timeout_seconds == expected


def test_v1_6_0_fixture_carries_sql_timeout() -> None:
    """The v1_6_0 fixture pins `sql_statement_timeout_seconds: 1800`. The lineage test
    only checks the version stamp; this pins that the field in that fixture is actually
    DECODED — the regression an impl that dropped it on the historical-load path
    (silently reverting to 15 min) would otherwise hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_6_0.json").read_text())
    assert payload["sql_statement_timeout_seconds"] == 1800  # guard the fixture itself
    prefs = Preferences.model_validate(payload)
    assert prefs.sql_statement_timeout_seconds == 1800


def test_v1_5_0_fixture_has_no_sql_timeout_field() -> None:
    """A pre-1.6 file (no `sql_statement_timeout_seconds`) loads with the field
    defaulting to 900 — the additive minor bump needs no migration, and an older file
    degrades to the same 15-min default a fresh user gets."""
    payload = json.loads((FIXTURE_ROOT / "v1_5_0.json").read_text())
    assert "sql_statement_timeout_seconds" not in payload  # guard the fixture
    prefs = Preferences.model_validate(payload)
    assert prefs.sql_statement_timeout_seconds == 900


def test_onboarded_flags_round_trip() -> None:
    """A completed onboarding (both flags True — non-default) survives serialize →
    validate, so finishing the flow sticks across a read-modify-write. An impl that
    dropped the flags on load would pass `test_defaults` but re-show the wizard on
    every launch here."""
    payload = Preferences(onboarded_cli=True, onboarded_extension=True).model_dump(mode="json")
    assert payload["onboarded_cli"] is True
    assert payload["onboarded_extension"] is True
    restored = Preferences.model_validate(payload)
    assert restored.onboarded_cli is True
    assert restored.onboarded_extension is True


def test_onboarded_flags_are_independent() -> None:
    """The two surfaces onboard independently: completing one flow must not flip
    the other's flag."""
    prefs = Preferences.model_validate(Preferences(onboarded_cli=True).model_dump(mode="json"))
    assert prefs.onboarded_cli is True
    assert prefs.onboarded_extension is False


def test_v1_7_0_fixture_carries_onboarded_flags() -> None:
    """The v1_7_0 fixture pins both onboarding flags True. The lineage test only
    checks the version stamp; this pins that the flags are actually DECODED — the
    regression an impl that dropped them on the historical-load path (re-showing
    onboarding to an already-onboarded user) would otherwise hide."""
    payload = json.loads((FIXTURE_ROOT / "v1_7_0.json").read_text())
    assert payload["onboarded_cli"] is True  # guard the fixture itself
    assert payload["onboarded_extension"] is True
    prefs = Preferences.model_validate(payload)
    assert prefs.onboarded_cli is True
    assert prefs.onboarded_extension is True


def test_v1_6_0_fixture_has_no_onboarded_fields() -> None:
    """A pre-1.7 file (no onboarding flags) loads with both defaulting to False —
    the additive minor bump needs no migration, and an existing user sees the
    onboarding flow once, same as a fresh install."""
    payload = json.loads((FIXTURE_ROOT / "v1_6_0.json").read_text())
    assert "onboarded_cli" not in payload  # guard the fixture
    assert "onboarded_extension" not in payload
    prefs = Preferences.model_validate(payload)
    assert prefs.onboarded_cli is False
    assert prefs.onboarded_extension is False


def test_current_version_drops_show_thinking_but_preserves_unknown_field() -> None:
    """Catch a current-version read that either revives the retired field or
    over-filters extras and drops a future writer's preference."""
    payload = {
        "schema_version": Preferences.SCHEMA_VERSION,
        "show_thinking": True,
        "future_pref": "newer-writer-value",
    }
    prefs = Preferences.model_validate(payload)
    dumped = prefs.model_dump(mode="json")
    assert dumped["future_pref"] == "newer-writer-value"
    assert "show_thinking" not in dumped


@pytest.mark.parametrize(
    "fixture_path",
    [pytest.param(p, id=p.stem) for p in sorted(FIXTURE_ROOT.glob("v*.json"))],
)
def test_historical_fixture_loads(fixture_path: Path) -> None:
    payload = json.loads(fixture_path.read_text())
    prefs = Preferences.model_validate(payload)
    # After load, the version is upgraded/stamped to current.
    assert prefs.schema_version == Preferences.SCHEMA_VERSION


# --- the desktop file: the shared document plus a copy per org -------------------

DESKTOP_FIXTURE_ROOT = FIXTURE_ROOT.parent / "desktop_preferences"


@pytest.mark.parametrize(
    "fixture_path",
    [
        pytest.param(p, id=f"{p.parent.name}-{p.stem}")
        for root in (FIXTURE_ROOT, DESKTOP_FIXTURE_ROOT)
        for p in sorted(root.glob("v*.json"))
    ],
)
def test_every_preferences_file_loads_as_the_desktop_file(fixture_path: Path) -> None:
    """The desktop reader opens every file any earlier CLI wrote, and a file
    from before the per-org copies reads with none."""
    payload = json.loads(fixture_path.read_text())
    prefs = DesktopPreferences.model_validate(payload)
    assert prefs.schema_version == DesktopPreferences.SCHEMA_VERSION
    assert prefs.orgs == payload.get("orgs", {})


def test_the_v2_1_0_fixture_keeps_each_orgs_copy_through_a_round_trip() -> None:
    payload = json.loads((DESKTOP_FIXTURE_ROOT / "v2_1_0.json").read_text())
    assert len(payload["orgs"]) == 2  # guard the fixture itself
    again = DesktopPreferences.model_validate(
        DesktopPreferences.model_validate(payload).model_dump(mode="json")
    )
    assert again.orgs == payload["orgs"]


def test_an_older_reader_keeps_the_per_org_copies_it_cannot_name() -> None:
    """A CLI from before the per-org copies reads the file as ``Preferences``:
    the copies ride through its write as an unknown field, so a newer CLI
    finds them again."""
    payload = json.loads((DESKTOP_FIXTURE_ROOT / "v2_1_0.json").read_text())
    older = Preferences.model_validate(payload).model_dump(mode="json")
    assert older["orgs"] == payload["orgs"]
    assert DesktopPreferences.model_validate(older).orgs == payload["orgs"]


def test_the_org_scoped_keys_are_exactly_the_catalog_naming_fields() -> None:
    """Each key is a real field, so a rename cannot leave a key that scopes
    nothing; and the set is the three that name a catalog model."""
    assert ORG_SCOPED_KEYS <= set(Preferences.model_fields)
    assert ORG_SCOPED_KEYS == {"model_efforts", "default_chat_model", "default_chat_effort"}
