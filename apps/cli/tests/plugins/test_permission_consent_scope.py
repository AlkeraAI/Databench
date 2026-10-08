"""How far a person's "Always allow" on a shell command reaches.

An "Always allow" on a destructive command (``rm``, ``git push --force``,
``chmod -R`` …) is consent to THAT command line, never to its verb: the same
line later runs without a card, and any other line raises a fresh one. A
non-destructive command keeps the family rule (``git push`` covers
``git push origin feat``). Driven through the real :class:`DecisionEngine`,
the real shell classifier and the real on-disk policy file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_cli.harness.adapters.opencode_translate import ask_options
from alkera_cli.plugins.plugin_base.permissions import consent_scope
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_cli.plugins.plugin_base.permissions.config import (
    PERMISSIONS_LOCAL_FILE,
    load_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.consent_scope import (
    EXACT_ALWAYS_LABEL,
    ExactConsent,
    normalize_command,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import ActionResolution, DecisionEngine


class _Person:
    """A person at the card, answering every ask with ``option``."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.asked: list[list[str]] = []
        self.offered: list[list[tuple[str, str]]] = []

    async def resolve(self, request: Any) -> str:
        self.asked.append(list(request.patterns))
        self.offered.append([(o.option_id, o.name) for o in request.options])
        return self.option


def _alkera(tmp_path: Path) -> Path:
    alkera = tmp_path / ".alkera"
    (alkera / "chat").mkdir(parents=True)
    return alkera


async def _run(alkera: Path, command: str, person: _Person, *, mode: str) -> ActionResolution:
    engine = DecisionEngine(
        sink=DecisionSink(alkera / "chat"),
        permissions=load_permissions(alkera),
        broker=person,
        alkera_dir=alkera,
        session_id="s",
    )
    return await engine.resolve(classify_command(command), mode=mode)


async def _always(alkera: Path, command: str, *, mode: str) -> None:
    person = _Person("allow_always")
    res = await _run(alkera, command, person, mode=mode)
    assert person.asked, f"{command!r} was not asked, so nothing was consented"
    assert res.allowed


async def _replay(alkera: Path, command: str, *, mode: str) -> tuple[bool, bool]:
    """``(allowed, asked)`` for ``command`` after the grant, answered "reject"
    if a card is raised — so an allow with no card is the standing rule's."""
    person = _Person("reject_once")
    res = await _run(alkera, command, person, mode=mode)
    return res.allowed, bool(person.asked)


def _rules(alkera: Path) -> list[dict[str, Any]]:
    path = alkera / PERMISSIONS_LOCAL_FILE
    return (
        list((yaml.safe_load(path.read_text()) or {}).get("rules") or []) if path.exists() else []
    )


MODES = [pytest.param("default", id="default"), pytest.param("auto", id="auto")]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("granted", "later", "runs_unasked"),
    [
        pytest.param("rm -rf a/", "rm -rf a/", True, id="rm-same-line-runs"),
        pytest.param("rm -rf a/", "rm   -rf  a/", True, id="rm-same-line-other-spacing-runs"),
        pytest.param("rm -rf a/", "rm -rf b/", False, id="rm-other-path-asks"),
        pytest.param("rm -rf a/", "rm x", False, id="rm-other-args-asks"),
        pytest.param("rm -rf a/", "rm -rf a/ b/", False, id="rm-extra-path-asks"),
        pytest.param("rm -rf a/", "rm '-rf  a/'", False, id="rm-quoted-spacing-is-another-line"),
        pytest.param(
            "git push --force origin x", "git push --force origin x", True, id="force-push-same"
        ),
        pytest.param(
            "git push --force origin x", "git push --force origin y", False, id="force-push-other"
        ),
        pytest.param("git push --force origin x", "git push origin x", False, id="force-not-push"),
        pytest.param("git reset --hard HEAD~1", "git reset --hard HEAD~5", False, id="reset-hard"),
        pytest.param("chmod -R 755 a", "chmod -R 777 /", False, id="chmod-recursive-other"),
        pytest.param("chmod -R 755 a", "chmod -R 755 a", True, id="chmod-recursive-same"),
        pytest.param("kill -9 123", "kill -9 1", False, id="kill-9-other-pid"),
        pytest.param("find . -name x -delete", "find / -delete", False, id="find-delete"),
        pytest.param("rm -rf ~/out", "rm -rf ~/out", True, id="home-tilde-is-fixed-text"),
    ],
)
async def test_a_destructive_always_is_consent_to_that_exact_line(
    tmp_path: Path, mode: str, granted: str, later: str, runs_unasked: bool
) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, granted, mode=mode)
    allowed, asked = await _replay(alkera, later, mode=mode)
    if runs_unasked:
        assert (allowed, asked) == (True, False), f"{later!r} should run on the earlier consent"
    else:
        assert asked, f"{later!r} ran on a consent given to {granted!r}"
        assert not allowed


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("granted", "later"),
    [
        pytest.param("git push", "git push origin feat", id="git-push-family"),
        pytest.param("mkdir build", "mkdir -p dist/out", id="mkdir-family"),
        pytest.param("touch a.txt", "touch b.txt", id="touch-family"),
        pytest.param("chmod 644 f", "chmod 600 g", id="chmod-without-recursion-family"),
    ],
)
async def test_a_non_destructive_always_keeps_the_family_rule(
    tmp_path: Path, mode: str, granted: str, later: str
) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, granted, mode=mode)
    assert await _replay(alkera, later, mode=mode) == (True, False)


async def test_git_push_and_force_push_are_different_consents(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, "git push origin feat", mode="default")
    allowed, asked = await _replay(alkera, "git push --force origin feat", mode="default")
    assert asked and not allowed


async def test_the_recorded_rule_names_the_line_not_the_verb(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, "rm  -rf   a/", mode="default")
    [rule] = _rules(alkera)
    assert rule["match"] == "rm -rf a/"
    assert rule["mode"] == "default"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rm -rf $DIR", id="variable"),
        pytest.param("rm -rf build/*", id="glob"),
        pytest.param("rm -rf $(cat list)", id="substitution"),
    ],
)
async def test_a_destructive_line_that_expands_offers_and_records_no_standing_allow(
    tmp_path: Path, command: str
) -> None:
    alkera = _alkera(tmp_path)
    person = _Person("allow_always")
    await _run(alkera, command, person, mode="default")
    assert "allow_always" not in [option for option, _ in person.offered[0]]
    assert _rules(alkera) == []
    allowed, asked = await _replay(alkera, command, mode="default")
    assert asked and not allowed


@pytest.mark.parametrize(
    ("command", "label"),
    [
        pytest.param("rm -rf a/", EXACT_ALWAYS_LABEL, id="rm"),
        pytest.param("git push -f origin x", EXACT_ALWAYS_LABEL, id="force-push"),
        pytest.param("chmod -R 755 a", EXACT_ALWAYS_LABEL, id="chmod-recursive"),
        pytest.param("git push origin x", "Always allow", id="push"),
    ],
)
async def test_the_card_names_what_always_will_remember(
    tmp_path: Path, command: str, label: str
) -> None:
    alkera = _alkera(tmp_path)
    person = _Person("reject_once")
    await _run(alkera, command, person, mode="default")
    assert ("allow_always", label) in person.offered[0]
    # The opencode ask for the same command offers the same words.
    options = ask_options(always=[], descriptor=classify_command(command))
    assert ("allow_always", label) in [(o.option_id, o.name) for o in options]


async def test_a_new_table_row_is_honoured_without_code_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mkdir`` keeps its family rule under the shipped table; one data row
    holds it to its exact text everywhere a standing allow is read or written."""
    monkeypatch.setattr(
        consent_scope, "EXACT_CONSENT", (*consent_scope.EXACT_CONSENT, ExactConsent("mkdir"))
    )
    alkera = _alkera(tmp_path)
    await _always(alkera, "mkdir build", mode="default")
    assert await _replay(alkera, "mkdir build", mode="default") == (True, False)
    allowed, asked = await _replay(alkera, "mkdir other", mode="default")
    assert asked and not allowed


async def test_an_exact_consent_does_not_reach_a_no_mutation_stance(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, "rm -rf a/", mode="default")
    allowed, _ = await _replay(alkera, "rm -rf a/", mode="read_only")
    assert not allowed


async def test_a_deny_still_beats_an_exact_consent(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, "rm -rf a/", mode="default")
    path = alkera / PERMISSIONS_LOCAL_FILE
    data = yaml.safe_load(path.read_text())
    data["rules"].append({"capability": "shell", "operation": "rm", "decision": "deny"})
    path.write_text(yaml.safe_dump(data))
    allowed, _ = await _replay(alkera, "rm -rf a/", mode="default")
    assert not allowed


async def test_a_default_consent_reaches_auto_but_an_auto_consent_not_default(
    tmp_path: Path,
) -> None:
    alkera = _alkera(tmp_path)
    await _always(alkera, "rm -rf a/", mode="auto")
    allowed, asked = await _replay(alkera, "rm -rf a/", mode="default")
    assert asked and not allowed


@pytest.mark.parametrize(
    ("raw", "normal"),
    [
        pytest.param("  rm   -rf\ta/  ", "rm -rf a/", id="collapses-outside-quotes"),
        pytest.param("rm 'a  b'", "rm 'a  b'", id="keeps-single-quoted"),
        pytest.param('rm "a  b"', 'rm "a  b"', id="keeps-double-quoted"),
        pytest.param("rm a\\  b", "rm a\\  b", id="keeps-escaped-space"),
        pytest.param("rm a\nrm b", "rm a\nrm b", id="keeps-newline-separator"),
    ],
)
def test_normalize_command(raw: str, normal: str) -> None:
    assert normalize_command(raw) == normal
