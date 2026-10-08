"""Subagent definition registry — built-ins + .alkera/agents/*.md + precedence."""

from __future__ import annotations

from pathlib import Path

from alkera_cli.plugins.plugin_base.agents import (
    builtin_agents,
    discover_agent_files,
    resolve_agents,
)
from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition


def test_builtins_are_explore_and_review() -> None:
    # Two built-ins: explore (cheap, fast discovery) and review (standard tier,
    # thorough end-of-change verification).
    by_name = {a.name: a for a in builtin_agents()}
    assert set(by_name) == {"explore", "review"}
    assert by_name["explore"].mode == "explore"
    assert by_name["explore"].tool_scope == "read_only"
    assert by_name["explore"].model_tier == "cheap"
    # The Explore agent ships a real system prompt (the core deliverable) carrying
    # the self-contained ±findings + parallelism instructions.
    assert "READ-ONLY" in by_name["explore"].prompt
    assert "file:line" in by_name["explore"].prompt
    # The prompt names the full read-only palette, not just file reads.
    assert "read-only SQL" in by_name["explore"].prompt
    assert "read-only bash" in by_name["explore"].prompt
    assert "SELECT" in by_name["explore"].prompt
    # The prompt explicitly forbids the "save a scratch file" pattern (`cat > file`),
    # which a read-only agent would otherwise try and have refused mid-investigation.
    assert "cat > file" in by_name["explore"].prompt
    # Explore should LEAD with the lineage tools for a data asset's up/downstream
    # dependencies, not hand-trace ref()/FROM/JOIN through files.
    assert "lineage_impact" in by_name["explore"].prompt
    assert "lineage_traverse" in by_name["explore"].prompt
    # The DESCRIPTION (what list_agent_types shows the main model, so it can pick
    # explore for DATA work) must surface SQL/data too — not just "codebase".
    desc = by_name["explore"].description
    assert "SQL" in desc
    assert "data" in desc


def test_review_builtin_is_read_only_standard_tier_and_lineage_aware() -> None:
    review = {a.name: a for a in builtin_agents()}["review"]
    # Read-only like explore, but the MIDDLE tier so it can actually reason; it
    # never spawns (the main agent fans out parallel reviews, not the review itself).
    assert review.tool_scope == "read_only"
    assert review.model_tier == "standard"
    assert review.can_spawn is False
    # mode reuses "explore" purely for the read-only clamp — identity is the name.
    assert review.mode == "explore"
    # The prompt is read-only, demands file:line, drives the lineage blast-radius
    # reasoning, carries the helper-not-ground-truth caveat, and stays parallel.
    prompt = review.prompt
    assert "READ-ONLY" in prompt
    assert "file:line" in prompt
    assert "lineage" in prompt.lower()
    assert "upstream" in prompt.lower() and "downstream" in prompt.lower()
    assert "NEVER" in prompt and "safe" in prompt  # never launder lineage into "safe"
    assert "PARALLELISM" in prompt  # reviews fast, like explore
    # The description (what list_agent_types shows) frames it as reviewing a CHANGE.
    desc = review.description.lower()
    assert "review" in desc
    assert "data" in desc


def test_discover_parses_frontmatter_and_body(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "reviewer.md").write_text(
        "---\n"
        "description: Reviews SQL for correctness\n"
        "mode: explore\n"
        "tool_scope: read_only\n"
        "---\n"
        "You are a meticulous SQL reviewer. Find bugs.\n"
    )
    [agent] = discover_agent_files(agents_dir)
    assert agent.name == "reviewer"  # from the file stem
    assert agent.description == "Reviews SQL for correctness"
    assert agent.mode == "explore"
    assert "meticulous SQL reviewer" in agent.prompt


def test_discover_skips_malformed_files(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "broken.md").write_text("---\n: : not yaml : :\n---\nbody\n")
    (agents_dir / "ok.md").write_text("---\ndescription: fine\n---\nbody\n")
    names = [a.name for a in discover_agent_files(agents_dir)]
    assert names == ["ok"]  # broken one skipped, not fatal


def test_discover_empty_when_no_dir(tmp_path: Path) -> None:
    assert discover_agent_files(tmp_path / "nope") == []


def test_resolve_precedence_programmatic_over_file_over_builtin(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # A file defines a custom "scout"; programmatic re-defines it (programmatic wins)
    # and adds "custom". The built-in "explore" survives both.
    (agents_dir / "scout.md").write_text("---\ndescription: file scout\n---\nfrom file\n")
    programmatic = [
        AgentDefinition(name="scout", description="programmatic scout"),
        AgentDefinition(name="custom", description="added"),
    ]
    resolved = resolve_agents(agents_dir=agents_dir, programmatic=programmatic)
    assert resolved["scout"].description == "programmatic scout"  # programmatic wins over file
    assert resolved["explore"].description.startswith("Read-only")  # built-in survives
    assert "custom" in resolved


def test_resolve_file_overrides_builtin(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "explore.md").write_text("---\ndescription: my explore\n---\n")
    resolved = resolve_agents(agents_dir=agents_dir)
    assert resolved["explore"].description == "my explore"
