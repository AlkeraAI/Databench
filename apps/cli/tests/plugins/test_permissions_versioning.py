"""PermissionsConfig v2.3.0 — the removal-migration contract for retired
environment scoping: a stamped 1.0.0 / 1.1.0 document walks the ladder,
env-scoped rules are stripped tighten-only, and the dead ``connections:``
block disappears. (Hand-authored files carry no ``schema_version`` and are
cleaned by the loader pre-pass instead — pinned in
``test_permissions_loader_hardening.py``; the historical fixture corpus is
exercised by ``test_plugin_lineage.py``.)"""

from __future__ import annotations

from alkera_cli.plugins.plugin_base.permissions import PermissionsConfig


def test_v1_1_0_document_with_env_scoping_migrates_to_current() -> None:
    payload = {
        "schema_version": "1.1.0",
        "default_mode": "auto",
        "rules": [
            {"capability": "sql", "effect": "read", "decision": "allow"},
            {"capability": "sql", "effect": "write", "environment": "prod", "decision": "ask"},
            {"capability": "sql", "effect": "write", "environment": "dev", "decision": "allow"},
        ],
        "reclassify": [
            {"capability": "sql", "operation": "insert", "environment": "dev", "to_effect": "read"}
        ],
        "connections": [
            {
                "handle": "snow_prod",
                "dialect": "snowflake",
                "environment": "prod",
                "max_effect": "write",
            }
        ],
    }
    cfg = PermissionsConfig.model_validate(payload)
    dumped = cfg.model_dump(mode="json")
    assert dumped["schema_version"] == PermissionsConfig.SCHEMA_VERSION == "2.3.0"
    # Tighten-only: the unscoped allow and the (key-stripped) ask survive; the
    # env-scoped allow and the env-scoped reclassify are dropped; the dead
    # connections block is gone entirely.
    assert [r["decision"] for r in dumped["rules"]] == ["allow", "ask"]
    assert all("environment" not in r for r in dumped["rules"])
    assert dumped["reclassify"] == []
    assert "connections" not in dumped
    assert dumped["default_mode"] == "auto"


def test_v1_0_0_document_walks_the_full_ladder() -> None:
    payload = {
        "schema_version": "1.0.0",
        "rules": [{"capability": "shell", "environment": "prod", "decision": "deny"}],
    }
    cfg = PermissionsConfig.model_validate(payload)
    dumped = cfg.model_dump(mode="json")
    assert dumped["schema_version"] == "2.3.0"
    assert dumped["rules"][0]["decision"] == "deny"
    assert "environment" not in dumped["rules"][0]


def test_a_v2_1_0_document_advances_to_the_exec_vocabulary() -> None:
    """2.2.0 is additive: a 2.1.0 document keeps every rule, including a recorded
    stance, and is re-stamped."""
    payload = {
        "schema_version": "2.1.0",
        "rules": [{"capability": "sql", "effect": "write", "decision": "allow", "mode": "auto"}],
    }
    dumped = PermissionsConfig.model_validate(payload).model_dump(mode="json")
    assert dumped["schema_version"] == "2.3.0"
    assert [(r["effect"], r["decision"], r["mode"]) for r in dumped["rules"]] == [
        ("write", "allow", "auto")
    ]


def test_a_rule_and_a_reclassify_may_name_exec() -> None:
    payload = {
        "schema_version": "2.2.0",
        "rules": [{"capability": "sql", "effect": "exec", "decision": "deny"}],
        "reclassify": [{"operation": "copy_file", "to_effect": "exec"}],
    }
    dumped = PermissionsConfig.model_validate(payload).model_dump(mode="json")
    assert dumped["rules"][0]["effect"] == "exec"
    assert dumped["reclassify"][0]["to_effect"] == "exec"
