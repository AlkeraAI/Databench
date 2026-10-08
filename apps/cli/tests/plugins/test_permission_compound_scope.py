"""A compound shell command has no family: "Always allow" covers the exact text.

A simple command's standing grant records its classified ``(capability,
operation)`` — approving ``git status`` stops every ``git status`` being asked.
A pipe, a list, a loop, a subshell or a write redirect is several actions, and
naming the first program in it (``date``) as the family would both misstate the
grant and, once recorded, wave through any later ``date`` write the person never
saw. So the descriptor marks the scope, the rule records the exact text, and
both cards say so (the terminal chat's card is checked in its own suite).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers.shell_commands import COMPOUND_COMMANDS as _COMPOUND
from _helpers.shell_commands import LOOP as _LOOP
from alkera_cli.plugins.plugin_base.permissions import AutoDecision, classify_command
from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule, load_permissions

_SIMPLE = [
    pytest.param("git status", "git_status", id="git-status"),
    pytest.param("npm install left-pad", "npm_install", id="npm-install"),
    pytest.param("date", "date", id="bare"),
    pytest.param("ls 2>/dev/null", "ls", id="discarded-stderr"),
    pytest.param("ls 2>&1", "ls", id="fd-duplication"),
    pytest.param("sort < input.txt", "sort", id="input-redirect"),
    pytest.param("FOO=1 make", "make", id="env-prefix"),
]


@pytest.mark.parametrize("command", _COMPOUND)
def test_a_compound_command_is_scoped_to_its_exact_text(command: str) -> None:
    assert classify_command(command).scope == "command"


@pytest.mark.parametrize(("command", "operation"), _SIMPLE)
def test_a_simple_command_keeps_its_family(command: str, operation: str) -> None:
    desc = classify_command(command)
    assert desc.scope == "operation"
    assert desc.operation == operation


def _alkera(tmp_path: Path) -> Path:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    return alkera


def test_an_always_allowed_compound_command_allows_only_itself(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    assert add_local_rule(alkera, classify_command(_LOOP), mode="default") is True
    cfg = load_permissions(alkera)

    assert cfg.rule_decision(classify_command(_LOOP), mode="default") == AutoDecision.ALLOW
    # The first program's family is not what was granted: a different write
    # that happens to lead with `date` still asks.
    for other in ("date >> f", "date >> f && rm -rf build", _LOOP.replace("8", "9")):
        assert cfg.rule_decision(classify_command(other), mode="default") is None


def test_a_glob_character_in_the_command_reads_as_itself(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    granted = "ls *.py | wc -l"
    add_local_rule(alkera, classify_command(granted), mode="default")
    cfg = load_permissions(alkera)

    assert cfg.rule_decision(classify_command(granted), mode="default") == AutoDecision.ALLOW
    assert cfg.rule_decision(classify_command("ls a.py | wc -l"), mode="default") is None


def test_two_compound_grants_with_one_leading_program_are_two_rules(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    assert add_local_rule(alkera, classify_command("ls | wc -l"), mode="default") is True
    assert add_local_rule(alkera, classify_command("ls | head"), mode="default") is True
    assert add_local_rule(alkera, classify_command("ls | head"), mode="default") is False
    assert len(load_permissions(alkera).rules) == 2


def test_a_simple_command_grant_still_covers_its_family(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    add_local_rule(alkera, classify_command("git add ."), mode="default")
    cfg = load_permissions(alkera)
    assert cfg.rule_decision(classify_command("git add src/"), mode="default") == AutoDecision.ALLOW


def test_a_familys_allow_does_not_bind_a_compound_command(tmp_path: Path) -> None:
    # "Always allow" on `touch a.txt` covers every touch — of one invocation. A
    # compound command that happens to touch is several actions, so the family's
    # standing answer must not run it unasked.
    alkera = _alkera(tmp_path)
    add_local_rule(alkera, classify_command("touch a.txt"), mode="default")
    cfg = load_permissions(alkera)

    assert cfg.rule_decision(classify_command("touch b.txt"), mode="default") == AutoDecision.ALLOW
    assert cfg.rule_decision(classify_command("echo one && touch b.txt"), mode="default") is None
    assert cfg.exact_text_decision(classify_command("touch b.txt"), mode="default") is None


def test_a_familys_deny_still_binds_a_compound_command(tmp_path: Path) -> None:
    # Tightening is never narrowed: a deny written for a family refuses the
    # compound that reaches into it too.
    alkera = _alkera(tmp_path)
    (alkera / "permissions.yml").write_text(
        "rules:\n- capability: shell\n  operation: rm\n  decision: deny\n"
    )
    cfg = load_permissions(alkera)

    assert (
        cfg.rule_decision(classify_command("rm -rf build"), mode="default") == AutoDecision.REJECT
    )
    assert (
        cfg.rule_decision(classify_command("echo one && rm -rf build"), mode="default")
        == AutoDecision.REJECT
    )


def test_the_exact_text_answer_is_the_compound_grants_alone(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    add_local_rule(alkera, classify_command(_LOOP), mode="default")
    add_local_rule(alkera, classify_command("date"), mode="default")
    cfg = load_permissions(alkera)

    assert cfg.exact_text_decision(classify_command(_LOOP), mode="default") == AutoDecision.ALLOW
    assert (
        cfg.exact_text_decision(classify_command(_LOOP.replace("8", "9")), mode="default") is None
    )
    # The simple grant is a family's answer, not an exact text's.
    assert cfg.exact_text_decision(classify_command("date"), mode="default") is None
    assert cfg.rule_decision(classify_command("date"), mode="default") == AutoDecision.ALLOW


def test_a_loaded_policy_refreshes_once_its_files_change(tmp_path: Path) -> None:
    # A session loads its policy once. An answer recorded during the session
    # lands in the local file, and the policy the session holds must say so on
    # the next decision — without a restart.
    alkera = _alkera(tmp_path)
    cfg = load_permissions(alkera)
    assert cfg.current() is cfg  # nothing changed: the same object, no reload

    add_local_rule(alkera, classify_command(_LOOP), mode="default")
    now = cfg.current()
    assert now is not cfg
    assert now.rule_decision(classify_command(_LOOP), mode="default") == AutoDecision.ALLOW
    assert (
        cfg.rule_decision(classify_command(_LOOP), mode="default") is None
    )  # the snapshot stays what it was
    assert now.current() is now


def test_a_policy_built_in_memory_is_its_own_current_self() -> None:
    from alkera_cli.plugins.plugin_base.permissions.config import PermissionsConfig

    cfg = PermissionsConfig()
    assert cfg.current() is cfg
