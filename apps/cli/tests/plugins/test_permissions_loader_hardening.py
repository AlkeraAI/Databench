"""Malformed-``permissions.yml`` hardening + torn-write immunity.

The loader must NEVER raise — a typo falls back to safe defaults (tighten-only
salvage) with a loud warning, so chat-open and even pure reads keep working. A
grep-guard pins that every policy/state writer goes through the atomic helpers.
"""

from __future__ import annotations

import re
from pathlib import Path

from alkera_cli.plugins.plugin_base.permissions import load_permissions
from alkera_cli.plugins.plugin_base.permissions.config import (
    PERMISSIONS_FILE,
    PERMISSIONS_LOCAL_FILE,
)


def _write(alkera: Path, name: str, text: str) -> None:
    alkera.mkdir(parents=True, exist_ok=True)
    (alkera / name).write_text(text)


def test_missing_files_load_defaults(tmp_path: Path) -> None:
    cfg = load_permissions(tmp_path / ".alkera")
    assert cfg.default_mode == "default"
    assert cfg.load_warnings == []


def test_valid_file_loads_clean(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, "default_mode: auto\nrules: []\n")
    cfg = load_permissions(alkera)
    assert cfg.default_mode == "auto"
    assert cfg.load_warnings == []


def test_syntax_error_falls_back_to_defaults_with_warning(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, "default_mode: : : not yaml\n  - broken")
    cfg = load_permissions(alkera)
    assert cfg.default_mode == "default"  # safe default, no crash
    assert cfg.load_warnings  # loud warning recorded
    assert "permissions.yml" in cfg.load_warnings[0]


def test_non_mapping_falls_back(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, "- just\n- a\n- list\n")
    cfg = load_permissions(alkera)
    assert cfg.default_mode == "default"
    assert cfg.load_warnings


def test_validation_error_salvages_deny_ask_only(tmp_path: Path) -> None:
    # A rules list where one rule is valid-deny, one valid-ask, one valid-allow,
    # but the config also has a bad-typed field that fails whole-model validation.
    alkera = tmp_path / ".alkera"
    _write(
        alkera,
        PERMISSIONS_FILE,
        "default_mode: 12345\n"  # bad type forces whole-model validation failure
        "rules:\n"
        "  - {capability: sql, effect: write, decision: deny}\n"
        "  - {capability: shell, decision: ask}\n"
        "  - {capability: fs, decision: allow}\n",
    )
    cfg = load_permissions(alkera)
    # Tighten-only salvage: the allow rule is DROPPED; deny + ask survive.
    decisions = sorted(r.decision for r in cfg.rules)
    assert decisions == ["ask", "deny"]
    assert "allow" not in decisions
    assert cfg.load_warnings


def test_broken_local_overlay_does_not_poison_valid_project(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, "default_mode: read_only\n")
    _write(alkera, PERMISSIONS_LOCAL_FILE, "::: not yaml :::")
    cfg = load_permissions(alkera)
    # The valid project file's mode survives; only the overlay warns.
    assert cfg.default_mode == "read_only"
    assert any(PERMISSIONS_LOCAL_FILE in w for w in cfg.load_warnings)


# --- the grep-guard: no bare writers of policy/state files ------------------

_GUARDED_FILES = ("permissions.yml", "permissions.local.yml", "cost_state.json", "decisions.jsonl")
_BARE_WRITE = re.compile(
    r"open\([^)]*[\"']w[\"']|\.write_text\(|\.write_bytes\(|yaml\.safe_dump|json\.dump\("
)


def test_no_bare_writers_of_policy_state_files() -> None:
    """Every writer of a policy/state file must go through ``atomic_io``.
    Scan the source for a bare write whose nearby context names a guarded
    file — a regression guard so a future writer can't reintroduce a torn write."""
    roots = [
        Path(__file__).resolve().parents[2] / "alkera_cli",
        Path(__file__).resolve().parents[4] / "packages" / "api-core" / "alkera_core",
    ]
    offenders: list[str] = []
    for root in roots:
        for py in root.rglob("*.py"):
            if "_generated" in py.parts or "tests" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if not _BARE_WRITE.search(line):
                    continue
                window = "\n".join(lines[max(0, i - 3) : i + 1])
                if any(name in window for name in _GUARDED_FILES):
                    offenders.append(f"{py}:{i + 1}: {line.strip()}")
    assert not offenders, "bare (non-atomic) writer of a policy/state file:\n" + "\n".join(
        offenders
    )


# --- retired environment scoping: tighten-only strip with loud warnings -----


def test_env_scoped_allow_rule_is_dropped_with_warning(tmp_path: Path) -> None:
    # Stripping `environment:` from an ALLOW would silently widen it to every
    # connection — so the whole rule is dropped (loudly) and a matching write
    # still PROMPTS instead of auto-allowing.
    from alkera_cli.plugins.plugin_base.permissions import (
        AutoDecision,
        descriptor_from_sql,
        evaluate_action,
    )

    alkera = tmp_path / ".alkera"
    _write(
        alkera,
        PERMISSIONS_FILE,
        "rules:\n  - {capability: sql, effect: write, environment: dev, decision: allow}\n",
    )
    cfg = load_permissions(alkera)
    assert cfg.rules == []
    assert any("allow rule" in w and "environment" in w for w in cfg.load_warnings)
    d = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="duckdb", connection="db")
    assert cfg.rule_decision(d, mode="default") is None
    assert evaluate_action(d, mode="default", permissions=cfg).decision is AutoDecision.PROMPT


def test_env_scoped_deny_rule_now_binds_on_every_connection(tmp_path: Path) -> None:
    # Tighten-only asymmetry: a deny/ask keeps its rule with the retired key
    # stripped (plus a warning) — a formerly prod-only deny now rejects the same
    # write on any connection, which can only be stricter.
    from alkera_cli.plugins.plugin_base.permissions import AutoDecision, descriptor_from_sql

    alkera = tmp_path / ".alkera"
    _write(
        alkera,
        PERMISSIONS_FILE,
        "rules:\n  - {capability: sql, effect: write, environment: prod, decision: deny}\n",
    )
    cfg = load_permissions(alkera)
    assert len(cfg.rules) == 1 and cfg.rules[0].decision == "deny"
    assert any("applies to every connection" in w for w in cfg.load_warnings)
    d = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="duckdb", connection="dev_db")
    assert cfg.rule_decision(d, mode="default") is AutoDecision.REJECT


def test_env_scoped_reclassify_is_dropped_with_warning(tmp_path: Path) -> None:
    # A scoped reclassify can't be widened either — dropping it means the
    # matched actions keep their real (stricter-or-equal policy) classification.
    from alkera_cli.plugins.plugin_base.permissions import descriptor_from_sql

    alkera = tmp_path / ".alkera"
    _write(
        alkera,
        PERMISSIONS_FILE,
        "reclassify:\n"
        "  - {capability: sql, operation: insert, environment: dev, to_effect: read}\n",
    )
    cfg = load_permissions(alkera)
    assert cfg.reclassify == []
    assert any("reclassify" in w and "environment" in w for w in cfg.load_warnings)
    d = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="duckdb", connection="db")
    assert cfg.reclassified_effect(d) is None


def test_dead_connections_block_is_ignored_silently(tmp_path: Path) -> None:
    # The `connections:` block never influenced a decision, so an old file
    # carrying one loads cleanly with no warning and the rest of the policy holds.
    alkera = tmp_path / ".alkera"
    _write(
        alkera,
        PERMISSIONS_FILE,
        "default_mode: auto\n"
        "connections:\n"
        "  - {handle: snow_prod, dialect: snowflake, environment: prod, max_effect: write}\n",
    )
    cfg = load_permissions(alkera)
    assert cfg.default_mode == "auto"
    assert cfg.load_warnings == []


def test_env_scoped_rule_in_local_overlay_is_stripped_too(tmp_path: Path) -> None:
    # The overlay file goes through the same tighten-only strip: a local
    # env-scoped allow is dropped (warned), a local deny binds everywhere.
    from alkera_cli.plugins.plugin_base.permissions import AutoDecision, descriptor_from_sql

    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, "default_mode: default\n")
    _write(
        alkera,
        PERMISSIONS_LOCAL_FILE,
        "rules:\n"
        "  - {capability: sql, effect: write, environment: dev, decision: allow}\n"
        "  - {capability: sql, effect: destroy, environment: prod, decision: deny}\n",
    )
    cfg = load_permissions(alkera)
    assert [r.decision for r in cfg.rules] == ["deny"]
    assert any(PERMISSIONS_LOCAL_FILE in w and "allow rule" in w for w in cfg.load_warnings)
    d = descriptor_from_sql("DROP TABLE t", dialect="duckdb", connection="anywhere")
    assert cfg.rule_decision(d, mode="default") is AutoDecision.REJECT


def test_junk_that_parses_to_a_mapping_loads_defaults_silently(tmp_path: Path) -> None:
    # `:::` is NOT a YAML syntax error — it parses to {"::": None}, a valid mapping
    # whose unknown key is ignored (extra="allow"). It falls back to safe defaults
    # SILENTLY: a malformed permissions.yml never warns or logs (user preference);
    # the guarantee is the safe-default behavior, not a notification.
    alkera = tmp_path / ".alkera"
    _write(alkera, PERMISSIONS_FILE, ":::\n")
    cfg = load_permissions(alkera)
    assert cfg.default_mode == "default"  # safe defaults
    # mutations still prompt — the fallback is the SAFE policy, not a loosened one.
    from alkera_cli.plugins.plugin_base.permissions import classify_command

    assert (
        cfg.rule_decision(classify_command("rm -rf x"), mode="default") is None
    )  # no rule loosened the floor; decide() still prompts/floors it
