"""Which rule ids an org may force comes from the domains that define rules.

The open org settings schema names no rule itself: the schema gate registers
its diff rules on ``GATE_RULE_IDS`` when its extension installs. With nothing
registered (the open platform) every forced rule is refused.
"""

from __future__ import annotations

import pytest
from alkera_core.extensions import ExtensionPoint
from alkera_core.schemas.tenancy import org as org_schema
from alkera_core.schemas.tenancy.org import OrgSettingsUpdate
from pydantic import ValidationError


@pytest.fixture
def point(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[frozenset[str]]:
    fresh: ExtensionPoint[frozenset[str]] = ExtensionPoint("org_settings.gate_rule_ids")
    monkeypatch.setattr(org_schema, "GATE_RULE_IDS", fresh)
    return fresh


def test_with_no_domain_registered_every_forced_rule_is_refused(
    point: ExtensionPoint[frozenset[str]],
) -> None:
    with pytest.raises(ValidationError, match="unknown gate rule id"):
        OrgSettingsUpdate(gate_force_rules=["TYPE_CHANGED"])


def test_a_registered_rule_is_accepted_sorted_and_deduplicated(
    point: ExtensionPoint[frozenset[str]],
) -> None:
    point.register(frozenset({"TYPE_CHANGED", "LOGIC_CHANGED"}))
    update = OrgSettingsUpdate(gate_force_rules=["TYPE_CHANGED", "LOGIC_CHANGED", "TYPE_CHANGED"])
    assert update.gate_force_rules == ["LOGIC_CHANGED", "TYPE_CHANGED"]


def test_a_rule_no_domain_registered_is_refused_by_name(
    point: ExtensionPoint[frozenset[str]],
) -> None:
    point.register(frozenset({"TYPE_CHANGED"}))
    with pytest.raises(ValidationError, match="NOT_A_RULE"):
        OrgSettingsUpdate(gate_force_rules=["TYPE_CHANGED", "NOT_A_RULE"])


def test_clearing_needs_no_rule_vocabulary(point: ExtensionPoint[frozenset[str]]) -> None:
    assert OrgSettingsUpdate(gate_force_rules=None).gate_force_rules is None
