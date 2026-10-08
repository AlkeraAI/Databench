"""Policy decision + config tests."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.permissions import (
    STANDING_OPTIONS,
    AutoDecision,
    PermissionRule,
    PermissionsConfig,
    bind_to_this_ask,
    decide,
    descriptor_from_sql,
    without_standing_grant,
)
from alkera_cli.plugins.plugin_base.permissions import config as _config
from alkera_cli.plugins.plugin_base.permissions.config import (
    load_permissions,
    load_permissions_cached,
    save_permissions,
)
from alkera_core.schemas.chat import PermissionOption, PermissionOptionId

#: Every option id a permission reply can carry, off the wire vocabulary itself
#: — so a new one added there is a new case here, not a silent fall-through.
_PERMISSION_OPTION_IDS = get_args(PermissionOptionId)


def _d(sql: str):  # type: ignore[no-untyped-def]
    return descriptor_from_sql(sql, dialect="snowflake")


def test_insert_overwrite_table_binds_human_floor_in_auto() -> None:
    """INSERT OVERWRITE TABLE atomically replaces existing rows (TRUNCATE+reload), so in
    auto mode it must hit the HUMAN floor (prompt, decided_by='floor') exactly like TRUNCATE/
    DROP — never auto-allow as a recoverable write. Asymmetric: a plain INSERT auto-allows."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    overwrite = descriptor_from_sql(
        "INSERT OVERWRITE TABLE w.sales SELECT * FROM s", dialect="databricks"
    )
    assert overwrite.effect == Effect.DESTROY
    assert decide(overwrite, mode="auto") == AutoDecision.PROMPT
    res = evaluate_action(overwrite, mode="auto")
    assert res.decision == AutoDecision.PROMPT
    assert res.decided_by == "floor"
    # A plain insert, by contrast, stays an auto-allowed recoverable write.
    plain = descriptor_from_sql("INSERT INTO w.sales SELECT * FROM s", dialect="databricks")
    assert plain.effect == Effect.WRITE
    assert decide(plain, mode="auto") == AutoDecision.ALLOW


def test_insert_overwrite_directory_is_egress_and_judged_in_auto() -> None:
    """INSERT OVERWRITE DIRECTORY '<path|s3://…>' exfiltrates rows to an external location, so
    it classifies as EGRESS and (in auto) routes to the grounded safety judge with the EGRESS
    signal — not the plain-write path. In default mode the egress floor prompts."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action, needs_auto_grounding

    d = descriptor_from_sql(
        "INSERT OVERWRITE DIRECTORY 's3://b/k' SELECT * FROM t", dialect="databricks"
    )
    assert d.effect == Effect.EGRESS
    res = evaluate_action(d, mode="auto")
    assert needs_auto_grounding(mode="auto", effect=res.effective_effect, decided_by=res.decided_by)
    assert decide(d, mode="default") == AutoDecision.PROMPT  # egress floors outside auto


def test_config_round_trips_through_yaml(tmp_path: Path) -> None:
    cfg = PermissionsConfig(
        default_mode="auto",
        rules=[PermissionRule(capability="sql", effect=Effect.WRITE, decision="ask")],
    )
    save_permissions(tmp_path, cfg)
    loaded = load_permissions(tmp_path)
    assert loaded.default_mode == "auto"
    assert loaded.rules[0].decision == "ask"
    assert loaded.rules[0].effect is Effect.WRITE


def test_load_permissions_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_permissions(tmp_path)
    assert cfg.default_mode == "default"
    assert cfg.rules == []


def test_local_overlay_appends_rules_and_overrides_mode(tmp_path: Path) -> None:
    # Project policy (committed) + a developer's gitignored local overlay.
    save_permissions(
        tmp_path,
        PermissionsConfig(
            default_mode="default",
            rules=[PermissionRule(capability="sql", effect=Effect.WRITE, decision="ask")],
        ),
    )
    (tmp_path / "permissions.local.yml").write_text(
        "default_mode: read_only\nrules:\n  - effect: destroy\n    decision: deny\n"
    )
    cfg = load_permissions(tmp_path)
    # Local overrides the scalar default_mode...
    assert cfg.default_mode == "read_only"
    # ...and its rules are appended to (not replacing) the project's.
    assert len(cfg.rules) == 2
    # The local deny is in force (collect-all max → a DROP is rejected).
    assert cfg.rule_decision(_d("DROP TABLE t"), mode="default") == AutoDecision.REJECT


def test_local_overlay_is_tighten_only(tmp_path: Path) -> None:
    # A project deny can't be loosened by a local allow (collect-all max wins).
    save_permissions(
        tmp_path,
        PermissionsConfig(rules=[PermissionRule(effect=Effect.WRITE, decision="deny")]),
    )
    (tmp_path / "permissions.local.yml").write_text(
        "rules:\n  - effect: write\n    decision: allow\n"
    )
    cfg = load_permissions(tmp_path)
    assert cfg.rule_decision(_d("INSERT INTO t VALUES (1)"), mode="default") == AutoDecision.REJECT


def test_rule_decision_collect_all_deny_beats_allow() -> None:
    cfg = PermissionsConfig(
        rules=[
            PermissionRule(capability="sql", decision="allow"),
            PermissionRule(effect=Effect.WRITE, decision="deny"),
        ]
    )
    # An INSERT matches both → deny wins (collect-all).
    assert cfg.rule_decision(_d("INSERT INTO t VALUES(1)"), mode="default") == AutoDecision.REJECT
    # A SELECT matches only the allow rule.
    assert cfg.rule_decision(_d("SELECT 1"), mode="default") == AutoDecision.ALLOW
    # A guarded UPDATE (a recoverable write) matches both too → deny.
    assert (
        cfg.rule_decision(_d("UPDATE t SET x=1 WHERE id=1"), mode="default") == AutoDecision.REJECT
    )


# --- the mtime-keyed cache (standalone tool.call hot path) -----------------


def _seed_policy(alkera_dir: Path, default_mode: str) -> None:
    alkera_dir.mkdir(parents=True, exist_ok=True)
    save_permissions(alkera_dir, PermissionsConfig(default_mode=default_mode))


def test_cached_returns_same_instance_until_files_change(tmp_path: Path) -> None:
    _config._PERMISSIONS_CACHE.clear()
    alkera = tmp_path / ".alkera"
    _seed_policy(alkera, "default")
    first = load_permissions_cached(alkera)
    # Same files (same mtime+size) → same cached object, no re-parse.
    assert load_permissions_cached(alkera) is first


def test_cached_busts_when_policy_is_rewritten(tmp_path: Path) -> None:
    _config._PERMISSIONS_CACHE.clear()
    alkera = tmp_path / ".alkera"
    _seed_policy(alkera, "default")
    assert load_permissions_cached(alkera).default_mode == "default"
    # A rewrite (different content/size, and a fresh mtime) must be picked up.
    _seed_policy(alkera, "read_only")
    fresh = load_permissions_cached(alkera)
    assert fresh.default_mode == "read_only"


def test_cached_missing_file_is_the_default_and_still_cached(tmp_path: Path) -> None:
    _config._PERMISSIONS_CACHE.clear()
    alkera = tmp_path / ".alkera"
    alkera.mkdir(parents=True, exist_ok=True)
    cfg = load_permissions_cached(alkera)
    assert cfg.default_mode == "default"  # no file → defaults
    assert load_permissions_cached(alkera) is cfg  # the "no files" key caches too


def test_cached_does_not_leak_across_projects(tmp_path: Path) -> None:
    _config._PERMISSIONS_CACHE.clear()
    a = tmp_path / "a" / ".alkera"
    b = tmp_path / "b" / ".alkera"
    _seed_policy(a, "read_only")
    _seed_policy(b, "bypass")
    assert load_permissions_cached(a).default_mode == "read_only"
    assert load_permissions_cached(b).default_mode == "bypass"


def test_cache_evicts_only_the_oldest_at_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # At capacity the cache evicts exactly ONE entry (the oldest), not the whole
    # map — a full clear would make a daemon serving many sessions re-read every
    # project's YAML in a burst.
    _config._PERMISSIONS_CACHE.clear()
    monkeypatch.setattr(_config, "_PERMISSIONS_CACHE_MAX", 2)
    dirs = [tmp_path / f"p{i}" / ".alkera" for i in range(3)]
    for d in dirs:
        _seed_policy(d, "default")
        load_permissions_cached(d)
    # Three projects, cap of 2 → bounded to 2, and the FIRST-seen is the casualty.
    assert len(_config._PERMISSIONS_CACHE) == 2
    keys = {k[0] for k in _config._PERMISSIONS_CACHE}
    assert str(dirs[0].resolve()) not in keys  # oldest evicted
    assert str(dirs[2].resolve()) in keys  # newest kept


def test_cache_recency_refresh_protects_a_reused_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-read marks an entry most-recently-used, so it survives the next
    # eviction while a stale neighbour is dropped (true LRU, not FIFO).
    _config._PERMISSIONS_CACHE.clear()
    monkeypatch.setattr(_config, "_PERMISSIONS_CACHE_MAX", 2)
    a, b, c = (tmp_path / x / ".alkera" for x in ("a", "b", "c"))
    for d in (a, b):
        _seed_policy(d, "default")
        load_permissions_cached(d)
    load_permissions_cached(a)  # touch `a` → now most-recently-used
    _seed_policy(c, "default")
    load_permissions_cached(c)  # forces an eviction — `b` (now oldest) goes
    keys = {k[0] for k in _config._PERMISSIONS_CACHE}
    assert str(a.resolve()) in keys, "the recently-reused entry was wrongly evicted"
    assert str(b.resolve()) not in keys
    assert str(c.resolve()) in keys


# --- always-allow persistence + cross-chat sync ----------------------------


def test_add_local_rule_persists_and_is_idempotent(tmp_path: Path) -> None:
    from alkera_cli.plugins.plugin_base.permissions import classify_command
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    desc = classify_command("git add .")  # shell / operation=git_add / write
    assert add_local_rule(alkera, desc, mode="default") is True
    text = (alkera / "permissions.local.yml").read_text(encoding="utf-8")
    assert "git_add" in text and "allow" in text
    # idempotent — a second always-allow of the same op doesn't duplicate.
    assert add_local_rule(alkera, desc, mode="default") is False
    assert text.count("git_add") == 1
    # and it's in force: a future git-add auto-allows via the persisted rule.
    cfg = load_permissions(alkera)
    assert (
        cfg.rule_decision(classify_command("git add file.py"), mode="default") == AutoDecision.ALLOW
    )


def test_add_local_rule_persists_a_deny(tmp_path: Path) -> None:
    # "Always reject" persists a deny rule (tighten-only, symmetric with allow).
    from alkera_cli.plugins.plugin_base.permissions import classify_command
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    desc = classify_command("git commit -m wip")  # shell / operation=git_commit
    assert add_local_rule(alkera, desc, decision="deny", mode="default") is True
    cfg = load_permissions(alkera)
    assert (
        cfg.rule_decision(classify_command("git commit -m other"), mode="default")
        == AutoDecision.REJECT
    )


def test_always_allow_syncs_across_chats_via_mtime_cache(tmp_path: Path) -> None:
    # Two chats share one .alkera. When chat A persists an always-allow, chat B's
    # next cached load picks it up (the (mtime,size) key busts) — no restart.
    from alkera_cli.plugins.plugin_base.permissions import classify_command
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    _config._PERMISSIONS_CACHE.clear()
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    # chat B: no rule yet for `mkdir`
    assert (
        load_permissions_cached(alkera).rule_decision(classify_command("mkdir x"), mode="default")
        is None
    )
    # chat A: always-allow mkdir
    add_local_rule(alkera, classify_command("mkdir build"), mode="default")
    # chat B's NEXT cached load sees it (cache busted by the file write)
    assert (
        load_permissions_cached(alkera).rule_decision(classify_command("mkdir y"), mode="default")
        == AutoDecision.ALLOW
    )


# ---------------------------------------------------------------------------
# Rules bind READ-effect actions through evaluate_action, the real pipeline
# entry. A configured deny/ask on a read must fire there, not only in decide().
# ---------------------------------------------------------------------------


def _read_descriptor(capability: str = "network") -> object:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    return ActionDescriptor(capability=capability, effect=Effect.READ, operation="webfetch")


@pytest.mark.parametrize("mode", ["default", "auto", "plan", "read_only"])
def test_deny_rule_binds_reads_in_every_asking_mode(mode: str) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="network", decision="deny")])
    out = evaluate_action(_read_descriptor(), mode=mode, permissions=cfg)  # type: ignore[arg-type]
    assert out.decision == AutoDecision.REJECT, f"deny-on-read dropped in mode={mode}"
    assert out.decided_by == "rule"


def test_bypass_overrides_a_deny_rule_even_on_a_read() -> None:
    # bypass is a total override — it runs even past an explicit deny, on a read.
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="network", decision="deny")])
    out = evaluate_action(_read_descriptor(), mode="bypass", permissions=cfg)  # type: ignore[arg-type]
    assert out.decision == AutoDecision.ALLOW


def test_ask_rule_forces_review_of_a_read() -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="sql", decision="ask")])
    out = evaluate_action(_d("SELECT * FROM secrets"), mode="auto", permissions=cfg)
    assert out.decision == AutoDecision.PROMPT
    assert out.decided_by == "rule"


def test_allow_rule_and_no_rule_keep_the_read_fast_path() -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    allow_cfg = PermissionsConfig(rules=[PermissionRule(capability="sql", decision="allow")])
    for cfg in (allow_cfg, PermissionsConfig(), None):
        out = evaluate_action(_d("SELECT 1"), mode="default", permissions=cfg)
        assert out.decision == AutoDecision.ALLOW
        assert out.decided_by == "read"


# ---------------------------------------------------------------------------
# An `allow` rule may relax `default`/`auto` (that IS "Always allow"), but never
# `read_only`/`plan` — those modes advertise "no side effects at all", and the
# harness clamps explore/review subagents to `read_only` on that promise. A rule
# persisted weeks earlier from one README edit must not silently waive it.
# ---------------------------------------------------------------------------


def _fs_write() -> object:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    return ActionDescriptor(capability="fs", effect=Effect.WRITE, operation="edit")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # The sanctioned relaxation: an allow rule silences the prompt where the mode
        # permits mutation at all.
        pytest.param("default", AutoDecision.ALLOW, id="default-allow-rule-still-relaxes"),
        pytest.param("auto", AutoDecision.ALLOW, id="auto-allow-rule-still-relaxes"),
        pytest.param("bypass", AutoDecision.ALLOW, id="bypass-total-override"),
        # The fix: the analyst modes are a ceiling, not a default.
        pytest.param("read_only", AutoDecision.REJECT, id="read_only-clamps-allow-rule"),
        pytest.param("plan", AutoDecision.REJECT, id="plan-clamps-allow-rule"),
    ],
)
def test_allow_rule_cannot_relax_the_no_mutation_modes(mode: str, expected: AutoDecision) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="fs", decision="allow")])
    if mode != "bypass":
        # bypass never reaches decide() because evaluate_action settles it first.
        assert decide(_fs_write(), mode=mode, rule_decision=AutoDecision.ALLOW) == expected  # type: ignore[arg-type]
    out = evaluate_action(_fs_write(), mode=mode, permissions=cfg)  # type: ignore[arg-type]
    assert out.decision == expected
    if expected == AutoDecision.REJECT:
        # The MODE is what refused — the audit must not blame the rule.
        assert out.decided_by == "mode"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("touch ~/.zshrc", id="shell-write"),
        pytest.param("sort -o /Users/dev/.zshrc payload.txt", id="shell-output-flag-write"),
        pytest.param("rm -rf build", id="shell-destroy"),
    ],
)
def test_persisted_always_allow_does_not_reopen_writes_in_read_only(
    tmp_path: Path, command: str
) -> None:
    """End to end through the real persistence path: a human answers "Always allow"
    once, which writes an UNSCOPED capability-wide rule to permissions.local.yml. A
    later `read_only` session (or the harness's read_only clamp on an explore
    subagent) must still refuse every mutation."""
    from alkera_cli.plugins.plugin_base.permissions import classify_command, evaluate_action
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    add_local_rule(alkera, classify_command("touch README.md"), mode="default")
    add_local_rule(alkera, classify_command(command), mode="default")
    cfg = load_permissions(alkera)
    desc = classify_command(command)
    assert cfg.rule_decision(desc, mode="default") == AutoDecision.ALLOW  # the rule DOES match
    for mode in ("read_only", "plan"):
        assert evaluate_action(desc, mode=mode, permissions=cfg).decision == AutoDecision.REJECT


def test_deny_rule_still_wins_in_the_no_mutation_modes() -> None:
    """The clamp is a max, not a replacement — a deny rule must still tighten past
    the mode base (asymmetry guard: the fix must not turn REJECT into the ceiling)."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="network", decision="deny")])
    for mode in ("read_only", "plan"):
        out = evaluate_action(_read_descriptor(), mode=mode, permissions=cfg)  # type: ignore[arg-type]
        assert out.decision == AutoDecision.REJECT
        assert out.decided_by == "rule"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # The sanctioned relaxation: a reclassify-down still auto-allows where the mode
        # permits mutation at all (that is the feature — see the gate tests).
        pytest.param("default", AutoDecision.ALLOW, id="default-reclassify-still-relaxes"),
        pytest.param("auto", AutoDecision.ALLOW, id="auto-reclassify-still-relaxes"),
        pytest.param("bypass", AutoDecision.ALLOW, id="bypass-total-override"),
        # …but it is the same relaxation an allow rule is, so it takes the same ceiling.
        pytest.param("read_only", AutoDecision.REJECT, id="read_only-clamps-reclassify"),
        pytest.param("plan", AutoDecision.REJECT, id="plan-clamps-reclassify"),
    ],
)
def test_reclassify_down_cannot_relax_the_no_mutation_modes(
    mode: str, expected: AutoDecision
) -> None:
    """The other half of the same hole: ``reclassify: insert → read`` moves the effect
    the mode base is computed from, so without a clamp on the REAL classification it
    re-opens every write in ``read_only``/``plan`` exactly as a blanket allow rule did.
    A reclassified write is still a write for the purpose of a mode that promises none."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action
    from alkera_cli.plugins.plugin_base.permissions.config import EffectRule

    cfg = PermissionsConfig(
        reclassify=[EffectRule(capability="sql", operation="insert", to_effect=Effect.READ)]
    )
    out = evaluate_action(_d("INSERT INTO t VALUES(1)"), mode=mode, permissions=cfg)
    assert out.decision == expected
    # The reclassification still took effect for everything downstream of the decision.
    assert out.effective_effect == Effect.READ


def test_reclassify_down_still_permits_a_genuine_read_in_the_no_mutation_modes() -> None:
    """Asymmetry guard: the clamp keys off the REAL effect, so a real read stays allowed
    in read_only/plan even when a reclassify rule is configured for something else."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action
    from alkera_cli.plugins.plugin_base.permissions.config import EffectRule

    cfg = PermissionsConfig(
        reclassify=[EffectRule(capability="sql", operation="insert", to_effect=Effect.READ)]
    )
    for mode in ("read_only", "plan"):
        assert evaluate_action(_d("SELECT 1"), mode=mode, permissions=cfg).decision == (
            AutoDecision.ALLOW
        )


def test_allow_rule_still_permits_reads_in_the_no_mutation_modes() -> None:
    """A read is allowed in read_only/plan (that's the whole point of the mode), and
    the clamp must not regress that."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(capability="sql", decision="allow")])
    for mode in ("read_only", "plan"):
        assert evaluate_action(_d("SELECT 1"), mode=mode, permissions=cfg).decision == (
            AutoDecision.ALLOW
        )


def test_concurrent_local_rule_writes_lose_nothing(tmp_path: Path) -> None:
    # The read→merge→write of permissions.local.yml is serialized: N concurrent
    # writers (sessions persisting "always" answers from the daemon's thread
    # pool, /cost set racing a rule persist) must all land — an unserialized
    # second writer would read the same base and atomically erase the first's
    # rule, INCLUDING a persisted always-deny.
    from concurrent.futures import ThreadPoolExecutor

    from alkera_cli.contracts.tool_types import ActionDescriptor
    from alkera_cli.plugins.plugin_base.permissions.config import (
        add_local_rule,
        update_local_cost_caps,
    )

    alkera = tmp_path / ".alkera"
    alkera.mkdir()

    def _persist_rule(i: int) -> None:
        d = ActionDescriptor(capability="shell", effect=Effect.WRITE, operation=f"op_{i}")
        add_local_rule(alkera, d, decision="deny" if i % 2 else "allow", mode="default")

    def _persist_cap(_: int) -> None:
        update_local_cost_caps(alkera, {"chat": 5.0})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_persist_rule, range(16)))
        list(pool.map(_persist_cap, range(4)))

    cfg = load_permissions(alkera)
    persisted_ops = {r.operation for r in cfg.rules}
    missing = {f"op_{i}" for i in range(16)} - persisted_ops
    assert not missing, f"concurrent writers lost rules: {sorted(missing)}"
    assert cfg.cost.get("chat_usd_cap") == 5.0  # the cap write survived the rules


# ---------------------------------------------------------------------------
# Agent memory. A knowledge write that stays on this machine is `Effect.MEMORY`:
# it changes neither the workspace nor a data system, so every mode admits it,
# the no-mutation modes included. The asymmetric pair is pinned beside it — a
# real WRITE in the same mode keeps the mode's answer — and a deny rule still
# binds the memory effect, because a rule only ever tightens.
# ---------------------------------------------------------------------------


def _memory_note() -> object:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    return ActionDescriptor(
        capability="knowledge", effect=Effect.MEMORY, operation="knowledge_note"
    )


@pytest.mark.parametrize(
    ("mode", "write_expected"),
    [
        pytest.param("read_only", AutoDecision.REJECT, id="read_only"),
        pytest.param("plan", AutoDecision.REJECT, id="plan"),
        pytest.param("default", AutoDecision.PROMPT, id="default"),
        pytest.param("auto", AutoDecision.ALLOW, id="auto"),
        pytest.param("bypass", AutoDecision.ALLOW, id="bypass"),
    ],
)
def test_agent_memory_is_admitted_in_every_mode_while_a_write_keeps_the_modes_answer(
    mode: str, write_expected: AutoDecision
) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    memory = evaluate_action(_memory_note(), mode=mode)  # type: ignore[arg-type]
    assert memory.decision == AutoDecision.ALLOW, (mode, memory)
    assert memory.decided_by == ("bypass" if mode == "bypass" else "mode")
    assert evaluate_action(_fs_write(), mode=mode).decision == write_expected  # type: ignore[arg-type]


def test_agent_memory_is_never_handed_to_the_auto_mode_judge() -> None:
    from alkera_cli.plugins.plugin_base.permissions import needs_auto_grounding

    assert not needs_auto_grounding(mode="auto", effect=Effect.MEMORY, decided_by="mode")
    assert needs_auto_grounding(mode="auto", effect=Effect.WRITE, decided_by="mode")


@pytest.mark.parametrize("mode", ["read_only", "plan", "default", "auto"])
def test_a_deny_rule_still_refuses_agent_memory(mode: str) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[PermissionRule(effect=Effect.MEMORY, decision="deny")])
    out = evaluate_action(_memory_note(), mode=mode, permissions=cfg)  # type: ignore[arg-type]
    assert out.decision == AutoDecision.REJECT and out.decided_by == "rule"


def test_a_read_only_tool_scope_still_lists_read_tools_only() -> None:
    """An Explore subagent's `read_only` tool scope is untouched by the memory effect:
    it sees the READ tools and not the knowledge writers."""
    from alkera_cli.plugins.plugin_base.tool import ToolSpec, tool_in_scope

    assert not tool_in_scope(ToolSpec(name="context_note", effect_hint=Effect.MEMORY), "read_only")
    assert tool_in_scope(ToolSpec(name="context_search", effect_hint=Effect.READ), "read_only")


# -- the standing-grant vocabulary -------------------------------------------


def test_standing_options_are_exactly_the_answers_that_outlive_their_ask() -> None:
    """The one spelling of "this answer is meant to outlive the ask". Everything
    that offers, withholds or downgrades a standing grant reads it from here, so
    a new option id that records one has to be added HERE to be handled — not in
    each of the four places that would otherwise carry its own list."""
    assert STANDING_OPTIONS == {"allow_always", "reject_always"}
    assert STANDING_OPTIONS < set(_PERMISSION_OPTION_IDS)


@pytest.mark.parametrize(
    ("option", "bound"),
    [
        pytest.param("allow_always", "allow_once", id="allow-always-loses-its-scope"),
        pytest.param("reject_always", "reject_once", id="reject-always-loses-its-scope"),
        pytest.param("allow_once", "allow_once", id="allow-once-is-already-bound"),
        pytest.param("reject_once", "reject_once", id="reject-once-is-already-bound"),
        pytest.param("cancelled", "cancelled", id="a-cancel-is-not-an-answer-to-rewrite"),
    ],
)
def test_binding_an_answer_keeps_its_verdict_and_drops_only_its_scope(
    option: str, bound: str
) -> None:
    """A path that records no rule still honours WHAT the person said — an allow
    stays an allow and a reject stays a reject. Only the promise past this one
    call is dropped, and an answer that made none is returned untouched."""
    assert bind_to_this_ask(option) == bound  # type: ignore[arg-type]
    assert bind_to_this_ask(bind_to_this_ask(option)) == bound  # type: ignore[arg-type]


def test_binding_is_total_over_every_option_a_reply_can_carry() -> None:
    """No option id falls through unbound: an id this function did not recognise
    would travel to the vendor exactly as it arrived, which is the failure the
    binding exists to prevent."""
    for option in _PERMISSION_OPTION_IDS:
        assert bind_to_this_ask(option) not in STANDING_OPTIONS  # type: ignore[arg-type]


def test_withholding_leaves_the_decisions_a_reader_can_still_take() -> None:
    """Withholding the grants must not take the approval with it: the reader can
    still allow and still refuse, in the order the ask named them."""
    offered = [
        PermissionOption(option_id="allow_once", name="Allow once"),
        PermissionOption(option_id="allow_always", name="Always allow"),
        PermissionOption(option_id="reject_once", name="Reject once"),
        PermissionOption(option_id="reject_always", name="Always reject"),
    ]
    kept = without_standing_grant(offered)
    assert [option.option_id for option in kept] == ["allow_once", "reject_once"]
    assert without_standing_grant(kept) == kept
    assert without_standing_grant([]) == []


# --------------------------------------------------------------------------- #
# EXEC — server-side program execution / server-filesystem access.
#
# EXEC is its own floor: never auto-allowed in any stance, forced to the human in
# default/auto (never the judge), refused in read_only/plan, and — the property
# that separates it from destroy/egress — REFUSED even under `bypass`, unless the
# owner's rule explicitly grants it. Each branch is exercised, with the inverting
# negative (a non-exec sibling under the same stance) so nothing is tautological.
# --------------------------------------------------------------------------- #

_EXEC_D = "COPY t FROM PROGRAM 'id'"  # -> Effect.EXEC on postgres


def _exec_d():  # type: ignore[no-untyped-def]
    return descriptor_from_sql(_EXEC_D, dialect="postgres")


def test_exec_is_the_floor_and_is_recognized() -> None:
    from alkera_cli.plugins.plugin_base.permissions import is_exec, is_floor

    d = _exec_d()
    assert d.effect == Effect.EXEC
    assert is_exec(d)
    assert is_floor(d)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("default", AutoDecision.PROMPT, id="default"),
        pytest.param("auto", AutoDecision.PROMPT, id="auto"),
        pytest.param("read_only", AutoDecision.REJECT, id="read_only"),
        pytest.param("plan", AutoDecision.REJECT, id="plan"),
    ],
)
def test_exec_is_never_auto_allowed_outside_bypass(mode: str, expected: AutoDecision) -> None:
    """In every asking/analyst mode EXEC lands on the human floor (default/auto) or is
    refused outright (read_only/plan) — it is never the auto-allowed write middle."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    res = evaluate_action(_exec_d(), mode=mode)
    assert res.decision == expected
    # In default/auto the FLOOR owns the decision, never the judge-eligible middle.
    if expected is AutoDecision.PROMPT:
        assert res.decided_by == "floor"


def test_exec_in_auto_is_not_handed_to_the_judge() -> None:
    """The auto-mode grounding predicate must exclude EXEC — an irreversible program
    on the data host is the human's, exactly like DESTROY, never the safety judge's."""
    from alkera_cli.plugins.plugin_base.permissions import needs_auto_grounding

    d = _exec_d()
    assert not needs_auto_grounding(mode="auto", effect=d.effect, decided_by="floor")


def test_bypass_refuses_exec_without_a_rule() -> None:
    """The load-bearing property: `bypass` waives the ASKING, not this boundary. A
    program on a shared data host is refused even here, with the exec-floor label —
    while a destroy under bypass, by contrast, runs (its floor IS waived)."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    res = evaluate_action(_exec_d(), mode="bypass")
    assert res.decision == AutoDecision.REJECT
    assert res.decided_by == "exec_floor"
    # Inverting the tier: a DESTROY under bypass still runs, so the refusal above is
    # specific to EXEC, not a blanket "bypass refuses floors".
    destroy = descriptor_from_sql("DROP TABLE t", dialect="postgres")
    assert evaluate_action(destroy, mode="bypass").decision == AutoDecision.ALLOW


def test_bypass_allows_exec_only_with_an_explicit_allow_rule() -> None:
    """The one escape hatch: the owner's explicit allow rule lets `bypass` run an EXEC,
    recorded as decided_by='rule'. An ask/deny rule is NOT a grant."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    allow_cfg = PermissionsConfig(
        rules=[PermissionRule(capability="sql", effect=Effect.EXEC, decision="allow")]
    )
    res = evaluate_action(_exec_d(), mode="bypass", permissions=allow_cfg)
    assert res.decision == AutoDecision.ALLOW
    assert res.decided_by == "rule"
    # A deny rule keeps it refused (and an absent rule already did).
    deny_cfg = PermissionsConfig(
        rules=[PermissionRule(capability="sql", effect=Effect.EXEC, decision="deny")]
    )
    assert evaluate_action(_exec_d(), mode="bypass", permissions=deny_cfg).decision == (
        AutoDecision.REJECT
    )


def test_a_reclassify_down_cannot_strip_the_exec_floor_under_bypass() -> None:
    """`reclassify: exec -> read` changes the tier the policy REASONS over, but the exec
    floor is read off the REAL descriptor — so a project rule cannot quietly turn a
    server-side program into an auto-allowed read under bypass."""
    from alkera_cli.plugins.plugin_base.permissions import EffectRule, evaluate_action

    cfg = PermissionsConfig(
        reclassify=[EffectRule(capability="sql", effect=Effect.EXEC, to_effect=Effect.READ)]
    )
    res = evaluate_action(_exec_d(), mode="bypass", permissions=cfg)
    assert res.decision == AutoDecision.REJECT
    assert res.decided_by == "exec_floor"


def test_always_allow_on_exec_is_clamped_to_once() -> None:
    """The card can never persist a blanket allow for an EXEC: `clamp_always` drops a
    human `allow_always` to `allow_once` for a floor action (a real write, by contrast,
    keeps it)."""
    from alkera_cli.plugins.plugin_base.permissions import clamp_always

    assert clamp_always("allow_always", _exec_d()) == "allow_once"
    write = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="postgres")
    assert clamp_always("allow_always", write) == "allow_always"


# --------------------------------------------------------------------------- #
# EXEC under bypass: only a rule that NAMES `effect: exec` grants it.
#
# A blanket allow is written for writes and reads; if it reached EXEC, a program on
# the data host would run in bypass because of a rule nobody wrote for it. The grant
# is matched against the REAL classification, so a reclassify cannot manufacture it,
# and a reclassify can never lower EXEC either: the effective tier stays EXEC.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(PermissionRule(decision="allow"), id="blank-allow"),
        pytest.param(PermissionRule(match="*", decision="allow"), id="match-star"),
        pytest.param(PermissionRule(capability="sql", decision="allow"), id="capability-only"),
        pytest.param(
            PermissionRule(capability="sql", operation="copy_program", decision="allow"),
            id="operation-only",
        ),
        pytest.param(
            PermissionRule(capability="sql", match="COPY t FROM PROGRAM*", decision="allow"),
            id="exact-glob-without-effect",
        ),
    ],
)
def test_bypass_refuses_exec_under_a_rule_that_does_not_name_exec(rule: PermissionRule) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(rules=[rule])
    res = evaluate_action(_exec_d(), mode="bypass", permissions=cfg)
    assert (res.decision, res.decided_by) == (AutoDecision.REJECT, "exec_floor")
    # The same rule is not inert: it still allows a write it matches in default.
    write = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="postgres")
    if rule.operation is None and rule.match is None:
        assert evaluate_action(write, mode="default", permissions=cfg).decision == (
            AutoDecision.ALLOW
        )


@pytest.mark.parametrize(
    ("rule", "granted"),
    [
        pytest.param(PermissionRule(effect=Effect.EXEC, decision="allow"), True, id="effect-exec"),
        pytest.param(
            PermissionRule(
                capability="sql", effect=Effect.EXEC, match="COPY t FROM PROGRAM*", decision="allow"
            ),
            True,
            id="effect-exec-narrowed-to-the-command",
        ),
        pytest.param(
            PermissionRule(
                capability="sql", effect=Effect.EXEC, match="COPY other*", decision="allow"
            ),
            False,
            id="effect-exec-for-another-command",
        ),
        pytest.param(
            PermissionRule(capability="shell", effect=Effect.EXEC, decision="allow"),
            False,
            id="effect-exec-for-another-capability",
        ),
    ],
)
def test_bypass_grants_exec_only_through_a_matching_exec_rule(
    rule: PermissionRule, granted: bool
) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    res = evaluate_action(_exec_d(), mode="bypass", permissions=PermissionsConfig(rules=[rule]))
    expected = (AutoDecision.ALLOW, "rule") if granted else (AutoDecision.REJECT, "exec_floor")
    assert (res.decision, res.decided_by) == expected
    assert res.effective_effect == Effect.EXEC


def test_a_deny_beside_the_exec_grant_still_refuses_in_bypass() -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    cfg = PermissionsConfig(
        rules=[
            PermissionRule(effect=Effect.EXEC, decision="allow"),
            PermissionRule(match="*PROGRAM*", decision="deny"),
        ]
    )
    assert evaluate_action(_exec_d(), mode="bypass", permissions=cfg).decision == (
        AutoDecision.REJECT
    )


@pytest.mark.parametrize("to_effect", [Effect.READ, Effect.WRITE, Effect.EGRESS, Effect.DESTROY])
def test_a_reclassify_down_plus_an_allow_on_the_lower_tier_does_not_grant_exec(
    to_effect: Effect,
) -> None:
    """`reclassify: exec -> <lower>` with an allow rule on the lower tier is the
    down-classify shape: it once returned ALLOW / rule in bypass."""
    from alkera_cli.plugins.plugin_base.permissions import EffectRule, evaluate_action

    cfg = PermissionsConfig(
        reclassify=[EffectRule(effect=Effect.EXEC, to_effect=to_effect)],
        rules=[PermissionRule(effect=to_effect, decision="allow")],
    )
    res = evaluate_action(_exec_d(), mode="bypass", permissions=cfg)
    assert (res.decision, res.decided_by) == (AutoDecision.REJECT, "exec_floor")
    assert res.effective_effect == Effect.EXEC


@pytest.mark.parametrize("mode", ["default", "auto"])
def test_a_reclassify_never_lowers_the_effective_tier_of_exec(mode: str) -> None:
    """The effective class of an EXEC is max(classified, reclassified), so the card,
    the audit row and every later layer see EXEC, not the tier a rule asked for."""
    from alkera_cli.plugins.plugin_base.permissions import EffectRule, evaluate_action

    cfg = PermissionsConfig(reclassify=[EffectRule(effect=Effect.EXEC, to_effect=Effect.READ)])
    res = evaluate_action(_exec_d(), mode=mode, permissions=cfg)
    assert (res.decision, res.decided_by, res.effective_effect) == (
        AutoDecision.PROMPT,
        "floor",
        Effect.EXEC,
    )
    # Inverted: the same reclassify still lowers a write, which is what it is for.
    write_cfg = PermissionsConfig(
        reclassify=[EffectRule(effect=Effect.WRITE, to_effect=Effect.READ)]
    )
    write = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="postgres")
    assert evaluate_action(write, mode=mode, permissions=write_cfg).effective_effect == Effect.READ


# --------------------------------------------------------------------------- #
# EXEC on a shared box: only the box owner's policy grants it.
#
# A cloud box (the platform pool or one org's machine) serves every member's chats.
# The committed `.alkera/permissions.yml` there is the box owner's: it sits inside the
# fence's always-refused `.alkera`, so no member's agent can write it. The gitignored
# `permissions.local.yml` is where one member's card answers are recorded, so on a
# shared box a rule there never grants EXEC, however it is spelled. On a local
# machine both files belong to the one person running the chat.
# --------------------------------------------------------------------------- #

_EXEC_RULE_YAML = "rules:\n  - {capability: sql, effect: exec, decision: allow}\n"


@pytest.mark.parametrize(
    ("filename", "shared_host", "granted"),
    [
        pytest.param("permissions.yml", False, True, id="local-machine-project-file"),
        pytest.param("permissions.local.yml", False, True, id="local-machine-overlay"),
        pytest.param("permissions.yml", True, True, id="shared-box-owner-policy"),
        pytest.param("permissions.local.yml", True, False, id="shared-box-member-overlay"),
    ],
)
def test_on_a_shared_box_only_the_owners_policy_grants_exec(
    tmp_path: Path, filename: str, shared_host: bool, granted: bool
) -> None:
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    (tmp_path / filename).write_text(_EXEC_RULE_YAML)
    cfg = load_permissions(tmp_path)
    res = evaluate_action(_exec_d(), mode="bypass", permissions=cfg, shared_host=shared_host)
    expected = (AutoDecision.ALLOW, "rule") if granted else (AutoDecision.REJECT, "exec_floor")
    assert (res.decision, res.decided_by) == expected


def test_a_member_overlay_decides_nothing_on_a_shared_box(tmp_path: Path) -> None:
    """The overlay on a box is every chat's answers at once, of every org, so
    neither its allow nor its deny is anyone's policy there: one org's "Always
    reject" must not refuse another org's chat. The owner's policy decides. On a
    local machine the same overlay is its one owner's and still tightens."""
    from alkera_cli.plugins.plugin_base.permissions import evaluate_action

    (tmp_path / "permissions.yml").write_text(_EXEC_RULE_YAML)
    (tmp_path / "permissions.local.yml").write_text(
        "rules:\n  - {capability: sql, effect: exec, decision: deny}\n"
    )
    cfg = load_permissions(tmp_path)
    shared = evaluate_action(_exec_d(), mode="bypass", permissions=cfg, shared_host=True)
    local = evaluate_action(_exec_d(), mode="bypass", permissions=cfg, shared_host=False)
    assert (shared.decision, shared.decided_by) == (AutoDecision.ALLOW, "rule")
    assert local.decision == AutoDecision.REJECT


@pytest.mark.parametrize(
    ("decision", "shared_host", "expected"),
    [
        pytest.param("allow", False, AutoDecision.ALLOW, id="laptop-recorded-allow"),
        pytest.param("deny", False, AutoDecision.REJECT, id="laptop-recorded-deny"),
        pytest.param("allow", True, AutoDecision.PROMPT, id="box-recorded-allow"),
        pytest.param("deny", True, AutoDecision.PROMPT, id="box-recorded-deny"),
    ],
)
def test_a_recorded_answer_binds_only_off_a_shared_host(
    tmp_path: Path, decision: str, shared_host: bool, expected: AutoDecision
) -> None:
    """An answer a chat recorded in the overlay decides the next ask on a laptop
    and nothing on a box, where the write asks as if it were never recorded."""
    from alkera_cli.plugins.plugin_base.permissions import classify_command, evaluate_action
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    write = classify_command("touch notes.txt")
    add_local_rule(tmp_path, write, decision=decision, mode="default")
    cfg = load_permissions(tmp_path)
    res = evaluate_action(write, mode="default", permissions=cfg, shared_host=shared_host)
    assert res.decision == expected


def test_the_shared_host_view_does_not_reload_the_overlay(tmp_path: Path) -> None:
    """The view a shared host consults is detached from the files: asking it for
    its current self after the overlay changed must not bring the overlay back."""
    from alkera_cli.plugins.plugin_base.permissions import classify_command
    from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule

    cfg = load_permissions(tmp_path)
    view = cfg.for_host(shared_host=True)
    add_local_rule(tmp_path, classify_command("touch a.txt"), mode="default")
    assert view.current().rules == []
    assert cfg.current().rules  # the loaded policy itself does see the new answer
