"""opencode's ask for the parent-hosted shell names no command.

Under the parent-shell flag the shell the model reaches as ``bash`` is Alkera's
own loopback-MCP tool, and opencode raises the permission ask it raises for
every MCP tool (``session/tools.ts``): ``patterns=["*"]`` — the permission RULE
glob, not a command — and empty metadata. The command is only known to the tool
itself, which gates it in-process with the real text.

Classifying the glob as though it were the command made every such ask a
heuristic WRITE named ``*``, and a cloud chat's write fence then quoted the
glob back as a write outside the chat's folder — a refusal with no card, for
``ls``. A pattern list that carries no command must read as exactly that.

The glob reached the reader too: the card renders ``patterns[0]``, so a live
ask read "Run this command?" over a bare ``*``, and the ``always`` globs beside
it were ``*`` as well — an "Always allow" there would have recorded a standing
grant over every later call of the tool, unasked and so ungated. So the ask
names the command off the call it gates, and offers a standing grant only where
one could be scoped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _TranslatorContext,
    glob_approves_everything,
)
from alkera_core.schemas.chat import PermissionRequest

WORKSPACE = Path("/Users/someone/repo")


def _ask_event(
    *,
    permission: str = "bash",
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    always: list[str] | None = None,
    call_id: str | None = None,
    request_id: str = "perm_1",
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "id": request_id,
        "permission": permission,
        "patterns": patterns,
        "metadata": metadata if metadata is not None else {},
    }
    if always is not None:
        props["always"] = always
    if call_id is not None:
        props["tool"] = {"messageID": "m1", "callID": call_id}
    return {"type": "permission.asked", "properties": props}


def _running_call_event(*, call_id: str, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """The frame opencode publishes when the model's arguments land and before the
    tool body runs (``session/processor.ts`` marks the part running WITH its
    input) — the only carrier of the arguments an MCP-tool ask omits."""
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": f"prt_{call_id}",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "tool": tool,
                "callID": call_id,
                "state": {"status": "running", "input": tool_input},
            }
        },
    }


def _bash_ask(
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    *,
    always: list[str] | None = None,
) -> PermissionRequest:
    """The request the runtime and every UI see, straight out of the translator."""
    ctx = _TranslatorContext(session_id="sid", workspace_root=WORKSPACE, sandbox_dir=None)
    event = OpencodeEventTranslator(ctx).translate(
        _ask_event(patterns=patterns, metadata=metadata, always=always)
    )
    assert isinstance(event, PermissionRequest)
    return event


def _bash_subject(patterns: list[str], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """The subject the runtime resolves, straight out of the translator."""
    subject = _bash_ask(patterns, metadata).subject
    assert subject is not None
    return subject


@pytest.mark.parametrize(
    "patterns",
    [
        pytest.param(["*"], id="the-rule-glob-alone"),
        pytest.param(["*", "*"], id="the-rule-glob-repeated"),
        pytest.param([" * "], id="the-rule-glob-with-whitespace"),
        pytest.param([], id="no-patterns-at-all"),
        pytest.param(["", "  "], id="blank-patterns"),
    ],
)
def test_a_bash_ask_that_names_no_command_is_an_unknown_prompting_write(
    patterns: list[str],
) -> None:
    """Nothing to classify: the ask fails closed to a write nobody can vouch
    for (it prompts, it never auto-allows) and names NO command — never the
    glob, which the fence would otherwise read as a destination."""
    subject = _bash_subject(patterns)
    assert subject["raw"] is None
    assert subject["effect"] == "write"
    assert subject["confidence"] == "unknown"
    assert subject["classifier"] == "opencode-empty"
    assert subject["targets"] == []
    assert not any("*" in reason for reason in subject["reasons"])


@pytest.mark.parametrize(
    ("patterns", "effect"),
    [
        pytest.param(["ls -la"], "read", id="a-listing-is-a-read"),
        pytest.param(["cat README.md"], "read", id="a-cat-is-a-read"),
        pytest.param(["echo hi > out.txt"], "write", id="a-redirect-is-a-write"),
        pytest.param(["rm -rf build"], "destroy", id="an-rm-is-a-destroy"),
    ],
)
def test_a_bash_ask_that_names_a_command_is_classified_from_it(
    patterns: list[str], effect: str
) -> None:
    """The native shell's ask carries the command in its patterns, and that
    path is untouched: the verdict comes from the command."""
    subject = _bash_subject(patterns)
    assert subject["raw"] == patterns[0]
    assert subject["effect"] == effect


def test_the_rule_glob_beside_a_command_does_not_hide_the_command() -> None:
    """A glob sitting next to a real command is dropped, not classified: the
    command decides, and it is what the ask names."""
    subject = _bash_subject(["*", "echo hi > out.txt"])
    assert subject["raw"] == "echo hi > out.txt"
    assert subject["effect"] == "write"


# ---------------------------------------------------------------------------
# What the card is given to show
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patterns",
    [
        pytest.param(["*"], id="the-rule-glob-alone"),
        pytest.param(["*", "*"], id="the-rule-glob-repeated"),
        pytest.param([" * "], id="the-rule-glob-with-whitespace"),
        pytest.param(["", "  "], id="blank-patterns"),
    ],
)
def test_the_rule_glob_never_reaches_the_card_as_the_subject(patterns: list[str]) -> None:
    """The card renders ``patterns[0]``. A card reading ``$ *`` asks a reader to
    approve a command they cannot see, so the glob is not carried there either —
    an ask that named nothing says nothing."""
    assert _bash_ask(patterns).patterns == []


def test_an_argument_less_ask_names_the_command_off_the_call_it_gates() -> None:
    """opencode's MCP-tool ask ships no arguments, but the call it gates was
    published with them a frame earlier. The ask names that command, so the reader
    approves the text that will run and the classifier rules on it."""
    ctx = _TranslatorContext(session_id="sid", workspace_root=WORKSPACE, sandbox_dir=None)
    translator = OpencodeEventTranslator(ctx)
    translator.translate(
        _running_call_event(call_id="call_1", tool="bash", tool_input={"command": "rm -rf build"})
    )
    event = translator.translate(_ask_event(patterns=["*"], always=["*"], call_id="call_1"))

    assert isinstance(event, PermissionRequest)
    assert event.patterns == ["rm -rf build"]
    assert event.subject is not None
    assert event.subject["raw"] == "rm -rf build"
    assert event.subject["effect"] == "destroy"
    assert event.subject["confidence"] != "unknown"


def test_a_command_the_ask_carries_itself_beats_the_gated_call() -> None:
    """The native shell names the command on the ask. That is the authority; the
    remembered call input is only the fallback for an ask that named nothing."""
    ctx = _TranslatorContext(session_id="sid", workspace_root=WORKSPACE, sandbox_dir=None)
    translator = OpencodeEventTranslator(ctx)
    translator.translate(
        _running_call_event(call_id="call_1", tool="bash", tool_input={"command": "rm -rf build"})
    )
    event = translator.translate(_ask_event(patterns=["ls -la"], call_id="call_1"))

    assert isinstance(event, PermissionRequest)
    assert event.patterns == ["ls -la"]


def test_a_call_that_closed_leaves_no_command_behind_for_a_later_ask() -> None:
    """The remembered command is keyed by the provider's call id and dropped when
    that call ends, so a later ask can never be captioned with an earlier call's
    command — the one failure mode that would be worse than showing nothing."""
    ctx = _TranslatorContext(session_id="sid", workspace_root=WORKSPACE, sandbox_dir=None)
    translator = OpencodeEventTranslator(ctx)
    translator.translate(
        _running_call_event(call_id="call_1", tool="bash", tool_input={"command": "rm -rf build"})
    )
    closed = _running_call_event(
        call_id="call_1", tool="bash", tool_input={"command": "rm -rf build"}
    )
    closed["properties"]["part"]["state"]["status"] = "completed"
    translator.translate(closed)

    event = translator.translate(_ask_event(patterns=["*"], always=["*"], call_id="call_1"))
    assert isinstance(event, PermissionRequest)
    assert event.patterns == []


# ---------------------------------------------------------------------------
# What "Always allow" would record
# ---------------------------------------------------------------------------


def _option_ids(event: PermissionRequest) -> list[str]:
    return [option.option_id for option in event.options]


@pytest.mark.parametrize(
    ("glob", "everything"),
    [
        pytest.param("*", True, id="star"),
        pytest.param("**", True, id="double-star"),
        pytest.param("?*", True, id="question-star"),
        pytest.param("*?", True, id="star-question"),
        pytest.param("???*", True, id="three-questions-star"),
        pytest.param("? *", True, id="one-character-then-a-space"),
        pytest.param("  *  ", True, id="padded"),
        pytest.param("git status *", False, id="a-command-prefix"),
        pytest.param("/tmp/data/*", False, id="a-directory"),
        pytest.param("*.sql", False, id="an-extension"),
        pytest.param("rm *", False, id="one-program"),
        pytest.param("*/node_modules/*", False, id="a-path-fragment"),
    ],
)
def test_a_glob_is_read_by_the_matcher_that_would_apply_it(glob: str, everything: bool) -> None:
    """What a rule would approve is asked of opencode's own compilation
    (`core/src/util/wildcard.ts`), not of a spelling: ``**`` and ``?*`` take
    anything just as ``*`` does, and a check against the literal ``*`` alone
    would hand both a standing grant over every later call of the tool. A glob
    that is nothing but wildcards is refused on its shape even where the probes
    do not reach it — ``???*`` leaves a two-letter ``ls`` asked, and is still no
    scope. A glob with a literal in it is one, and survives."""
    assert glob_approves_everything(glob) is everything


@pytest.mark.parametrize(
    "always",
    [
        pytest.param(["*"], id="the-rule-glob"),
        pytest.param(["*", " * "], id="the-rule-glob-repeated"),
        pytest.param(["**"], id="a-double-star"),
        pytest.param(["?*"], id="a-question-star"),
        pytest.param(["*", "ls *"], id="one-open-glob-beside-a-narrow-one"),
        pytest.param(["ls *", "?*"], id="a-narrow-glob-beside-an-open-one"),
        pytest.param([], id="no-globs-at-all"),
        pytest.param(None, id="no-always-key"),
    ],
)
def test_always_allow_is_withheld_when_nothing_narrower_would_be_recorded(
    always: list[str] | None,
) -> None:
    """opencode pushes EVERY glob in the ask's ``always`` into its ruleset, so
    one open glob beside a narrow one is still a standing grant over every later
    call of the tool — the test is "any", not "all". For the subject-less MCP ask
    the action is too unclassifiable for Alkera's own precise rule either, so
    offering the choice would grant everything or record nothing."""
    event = _bash_ask(["*"], always=always)
    assert "allow_always" not in _option_ids(event)
    assert _option_ids(event) == ["allow_once", "reject_once", "reject_always"]


def test_always_allow_stays_for_an_ask_a_precise_rule_can_be_written_from() -> None:
    """A command the classifier can read gets Alkera's own ``(capability,
    operation)`` rule on an always-allow, so the standing grant is real and
    narrow — the choice belongs on that card."""
    event = _bash_ask(["git status"], always=["*"])
    assert "allow_always" in _option_ids(event)


def test_always_allow_stays_for_a_scoped_glob() -> None:
    """opencode's own directory gate asks with the directory it would approve.
    That glob IS a scope, so the reply may carry it."""
    ctx = _TranslatorContext(session_id="sid", workspace_root=WORKSPACE, sandbox_dir=None)
    event = OpencodeEventTranslator(ctx).translate(
        _ask_event(
            permission="external_directory",
            patterns=["/tmp/data/*"],
            always=["/tmp/data/*"],
        )
    )
    assert isinstance(event, PermissionRequest)
    assert "allow_always" in _option_ids(event)
