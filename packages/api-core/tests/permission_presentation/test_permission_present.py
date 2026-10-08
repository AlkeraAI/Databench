"""The permission presentation: every ask reads as the registry says, and never
as a bare tag.

The web card and the Slack card both render what ``present`` returns, so these
cases are the contract for both surfaces at once.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.permission_presentation import (
    PermissionAsk,
    ask_from_event,
    present,
    redact,
)
from alkera_core.permission_presentation.model import AskCall
from alkera_core.permission_presentation.present import (
    always_scope,
    format_bytes,
    js_json,
    js_number,
    js_to_fixed,
    requested_by,
    resolve_tool,
)
from alkera_core.permission_presentation.registry import (
    EXEC_TITLE,
    Presenter,
    presenters,
    register,
)
from alkera_core.permission_presentation.vectors import VECTORS

ONCE = [
    {"optionId": "allow_once", "name": "Allow once"},
    {"optionId": "reject_once", "name": "Reject once"},
]


def ask(**fields: Any) -> PermissionAsk:
    return PermissionAsk.model_validate({"options": ONCE, **fields})


# --------------------------------------------------------------------------
# Coverage of the registry
# --------------------------------------------------------------------------


def test_every_presenter_that_can_ask_the_question_has_a_vector() -> None:
    """A presenter with no vector is one the TS twin and the Slack card were
    never held to. The lanes that only name a standing grant ask nothing."""
    covered = {present(PermissionAsk.model_validate(raw)).presenter for raw in VECTORS.values()}
    titled = {p.key for p in presenters() if p.titles}
    assert titled - covered == set()


def test_a_second_presenter_for_the_same_key_is_refused() -> None:
    with pytest.raises(ValueError, match="already presented"):
        register(Presenter(key="dup", layer="canonical", matches=("shell",)))


@pytest.mark.parametrize(
    "canonical",
    [
        pytest.param("other", id="other"),
        pytest.param("some_future_class", id="unregistered-class"),
    ],
)
def test_an_unregistered_tool_falls_back_to_its_name_and_input(canonical: str) -> None:
    """The bug this exists for: a Slack card that said "shell" and nothing
    else. A tool nobody registered still gets a question, a label, the tool's
    name and its whole input."""
    shown = present(
        ask(
            permission_kind="acme_widget",
            canonical_kind=canonical,
            call=AskCall(name="mcp__acme__widget", input={"size": 3, "colour": "red"}),
        )
    )
    assert shown.presenter == "generic"
    assert shown.title == "Allow this action?"
    assert shown.label == "Use a tool"
    assert shown.details is not None
    assert shown.details.tool == "widget"
    assert '"colour": "red"' in shown.details.input
    assert "details" in shown.primary
    assert "acme_widget" not in shown.model_dump_json()


def test_a_named_tool_nobody_registered_heads_with_its_name() -> None:
    shown = present(ask(permission_kind="mcp__alkera__sql.query", patterns=["select 1"]))
    assert shown.title == "Allow sql.query?"
    assert shown.subject is not None and shown.subject.text == "select 1"
    assert shown.details is None


# --------------------------------------------------------------------------
# Titles, formats and lines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "title"),
    [
        pytest.param(
            {"canonical_kind": "shell", "patterns": ["ls"]}, "Run this command?", id="shell"
        ),
        pytest.param({"canonical_kind": "shell"}, "Run a command?", id="shell-unnamed"),
        pytest.param(
            {"canonical_kind": "edit", "patterns": ["a.py"]}, "Edit these files?", id="edit"
        ),
        pytest.param({"canonical_kind": "network"}, "Fetch an address?", id="network-unnamed"),
        pytest.param(
            {"permission_kind": "doom_loop", "canonical_kind": "shell", "patterns": ["x"]},
            "Repeat this call again?",
            id="mechanism-outranks-class",
        ),
        pytest.param(
            {
                "permission_kind": "doom_loop",
                "subject": {"capability": "sql", "effect": "exec"},
                "patterns": ["x"],
            },
            "Repeat this call again?",
            id="mechanism-outranks-exec",
        ),
        pytest.param(
            {"subject": {"capability": "sql", "effect": "exec"}, "patterns": ["COPY"]},
            EXEC_TITLE,
            id="exec-outranks-lane",
        ),
        pytest.param(
            {"subject": {"capability": "sql", "effect": "destroy"}, "patterns": ["drop t"]},
            "Run this destructive query?",
            id="sql-destroy",
        ),
        pytest.param(
            {"subject": {"capability": "integration_sdk"}, "patterns": ["x"]},
            "Run this code?",
            id="sdk-without-connection",
        ),
        pytest.param(
            {"subject": {"capability": "databricks", "operation": "run"}, "patterns": ["x"]},
            "Allow this action?",
            id="control-plane-without-connection",
        ),
        pytest.param(
            {
                "canonical_kind": "shell",
                "subject": {
                    "capability": "databricks",
                    "targets": [{"name": "j", "connection": "dbx"}],
                },
                "patterns": ["x"],
            },
            "Run this command?",
            id="control-plane-words-only-an-unclassed-ask",
        ),
    ],
)
def test_the_question(fields: dict[str, Any], title: str) -> None:
    assert present(ask(**fields)).title == title


@pytest.mark.parametrize(
    ("fields", "fmt", "language"),
    [
        pytest.param({"canonical_kind": "shell"}, "command", "bash", id="shell"),
        pytest.param({"canonical_kind": "edit"}, "path", None, id="edit"),
        pytest.param({"canonical_kind": "network"}, "url", None, id="network"),
        pytest.param({"subject": {"capability": "sql"}}, "code", "sql", id="sql"),
        pytest.param({"subject": {"capability": "knowledge"}}, "sentence", None, id="knowledge"),
        pytest.param(
            {"canonical_kind": "edit", "subject": {"capability": "knowledge"}},
            "path",
            None,
            id="harness-class-keeps-its-register",
        ),
        pytest.param(
            {"canonical_kind": "shell", "subject": {"capability": "sql"}},
            "command",
            "sql",
            id="lane-syntax-under-harness-register",
        ),
    ],
)
def test_the_subject_register(fields: dict[str, Any], fmt: str, language: str | None) -> None:
    shown = present(ask(patterns=["subject"], **fields))
    assert shown.subject is not None
    assert (shown.subject.format, shown.subject.language) == (fmt, language)


def test_a_sentence_subject_raises_its_first_word_only() -> None:
    shown = present(ask(subject={"capability": "knowledge"}, patterns=["withdraw 'fact:ARR'"]))
    assert shown.subject is not None and shown.subject.text == "Withdraw 'fact:ARR'"


def test_a_waiting_ask_says_so_and_names_no_tool() -> None:
    shown = present(ask(permission_kind="bash", canonical_kind="shell", subject_pending=True))
    assert shown.waiting == "Waiting for the command…"
    assert shown.missing_subject is None


def test_the_subject_is_recovered_from_the_gated_call() -> None:
    shown = present(
        ask(
            permission_kind="bash",
            canonical_kind="shell",
            subject_pending=True,
            call=AskCall(name="bash", input={"command": "  ", "path": "x.txt"}),
        )
    )
    assert shown.subject is not None and shown.subject.text == "x.txt"
    assert shown.waiting is None
    assert shown.title == "Run this command?"


def test_a_lane_names_its_own_input_before_the_generic_keys() -> None:
    shown = present(
        ask(
            permission_kind="sql",
            call=AskCall(name="sql.query", input={"path": "/x", "sql": "select 2"}),
        )
    )
    assert shown.subject is not None and shown.subject.text == "select 2"


@pytest.mark.parametrize(
    ("kind", "canonical", "line"),
    [
        pytest.param("bash", "shell", "Requested by the bash tool.", id="native"),
        pytest.param("terminal", "shell", "Requested by the bash tool.", id="alias"),
        pytest.param(
            "alkera_blob_query", "shell", "Requested by the blob.query tool.", id="sanitized"
        ),
        pytest.param("task", "task", None, id="key-that-names-no-tool"),
        pytest.param("mcp__alkera__blob.query", "other", None, id="title-names-the-tool"),
        pytest.param("repo_clone", "shell", None, id="mechanism"),
    ],
)
def test_requested_by(kind: str, canonical: str, line: str | None) -> None:
    assert requested_by(ask(permission_kind=kind, canonical_kind=canonical)) == line


def test_a_note_replaces_the_statement_and_a_diff_rides_under_the_path() -> None:
    note = present(
        ask(
            subject={"capability": "knowledge", "operation": "knowledge_share"},
            patterns=["Share with the Team Knowledge Base:\nx"],
            preview={"kind": "text", "title": "Revenue", "content": "Body."},
        )
    )
    assert note.subject is None
    assert note.note is not None and (note.note.title, note.note.body) == ("Revenue", "Body.")
    change = present(
        ask(
            canonical_kind="edit",
            patterns=["a.py"],
            preview={"kind": "diff", "title": "a.py", "content": "-a\n+b"},
        )
    )
    assert change.subject is not None and change.change is not None
    assert change.change.content == "-a\n+b"


@pytest.mark.parametrize(
    ("label", "subject", "line"),
    [
        pytest.param(
            "Always allow",
            {"capability": "shell", "operation": "git_status"},
            "Always allow covers every git status command.",
            id="shell",
        ),
        pytest.param(
            "Always allow this exact command",
            {"capability": "shell", "operation": "rm"},
            "Always allow covers this exact command.",
            id="exact-label",
        ),
        pytest.param(
            "Always allow",
            {"capability": "shell", "operation": "x", "scope": "command"},
            "Always allow covers this exact command.",
            id="compound-command",
        ),
        pytest.param(
            "Always allow",
            {"capability": "knowledge", "operation": "knowledge_note"},
            "Always allow covers every save to the Project Knowledge Base.",
            id="knowledge-fallback",
        ),
        pytest.param(
            "Always allow",
            {"capability": "file", "operation": "graph_create"},
            "Always allow covers every graph create.",
            id="nounless",
        ),
        pytest.param(
            "Always allow",
            {"capability": "snowflake", "operation": "suspend"},
            "Always allow covers every suspend action.",
            id="unregistered-lane",
        ),
        pytest.param("Always allow", {"capability": "shell"}, None, id="no-operation"),
        pytest.param(
            "Always allow", {"capability": "shell", "operation": "unknown"}, None, id="unknown"
        ),
        pytest.param("Always allow", None, None, id="no-subject"),
    ],
)
def test_always_scope(label: str, subject: dict[str, Any] | None, line: str | None) -> None:
    parsed = ask(subject=subject).subject if subject else None
    assert always_scope(label, parsed) == line


def test_no_standing_grant_offered_means_no_scope_line() -> None:
    shown = present(
        ask(
            canonical_kind="shell",
            patterns=["ls"],
            subject={"capability": "shell", "operation": "ls"},
        )
    )
    assert shown.always_scope is None


@pytest.mark.parametrize(
    ("option_id", "role"),
    [
        pytest.param("allow_once", "allow", id="allow"),
        pytest.param("allow_always", "always", id="always"),
        pytest.param("reject_once", "deny", id="reject"),
        pytest.param("reject_always", "deny", id="reject-always"),
        pytest.param("cancelled", "deny", id="cancelled"),
    ],
)
def test_decision_roles(option_id: str, role: str) -> None:
    shown = present(ask(options=[{"optionId": option_id, "name": "X"}]))
    assert [(d.option_id, d.role) for d in shown.decisions] == [(option_id, role)]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("export GITHUB_TOKEN=abc", "export GITHUB_TOKEN=[redacted]", id="env"),
        pytest.param('DB_PASSWORD="a b" run', "DB_PASSWORD=[redacted] run", id="quoted-env"),
        pytest.param("api_key: xyz", "api_key: [redacted]", id="yaml"),
        pytest.param(
            "curl -H 'Authorization: Bearer ab.cd'",
            "curl -H 'Authorization: Bearer [redacted]'",
            id="bearer",
        ),
        pytest.param("mysql --password hunter2", "mysql --password [redacted]", id="flag"),
        pytest.param("psql postgres://u:pw@h/db", "psql postgres://u:[redacted]@h/db", id="url"),
        pytest.param("x AKIAABCDEFGHIJKLMNOP y", "x [redacted] y", id="aws-key"),
        pytest.param("xoxb-1234567890-abc", "[redacted]", id="slack-token"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("git log --oneline -20", id="plain"),
        pytest.param("SELECT max_tokens FROM usage", id="token-word-without-assignment"),
        pytest.param("https://example.com/path", id="url-without-userinfo"),
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str) -> None:
    assert redact(text) == text


def test_every_shown_field_is_redacted() -> None:
    secret = "sk-abcdefghijklmnopqrstuv"
    shown = present(
        ask(
            canonical_kind="edit",
            patterns=[f"echo {secret}"],
            preview={"kind": "diff", "content": f"+KEY={secret}"},
        )
    )
    assert secret not in shown.model_dump_json()
    details = present(
        ask(call=AskCall(name="x", input={"token": "plain", "nested": {"cookie": 1}}))
    )
    assert details.details is not None
    assert (
        "plain" not in details.details.input and '"cookie": "[redacted]"' in details.details.input
    )


# --------------------------------------------------------------------------
# JavaScript-compatible formatting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param(3, "3", id="int"),
        pytest.param(3.0, "3", id="integral-float"),
        pytest.param(0.5, "0.5", id="fraction"),
        pytest.param(1e21, "1e+21", id="large"),
        pytest.param(1.5e-7, "1.5e-7", id="small"),
        pytest.param(123456.789, "123456.789", id="mixed"),
        pytest.param(float("nan"), "null", id="nan"),
        pytest.param(True, "true", id="bool"),
    ],
)
def test_js_number(value: float, text: str) -> None:
    assert js_number(value) == text


@pytest.mark.parametrize(
    ("value", "places", "text"),
    [
        pytest.param(0.425, 2, "0.42", id="binary-below-tie"),
        pytest.param(2.25, 1, "2.3", id="exact-tie-rounds-up"),
        pytest.param(1.0, 2, "1.00", id="pads"),
    ],
)
def test_js_to_fixed(value: float, places: int, text: str) -> None:
    assert js_to_fixed(value, places) == text


@pytest.mark.parametrize(
    ("size", "text"),
    [
        pytest.param(512, "512 B", id="bytes"),
        pytest.param(1536, "1.5 KB", id="kb"),
        pytest.param(1288490188, "1.2 GB", id="gb"),
        pytest.param(20 * 1024**2, "20 MB", id="whole"),
    ],
)
def test_format_bytes(size: int, text: str) -> None:
    assert format_bytes(size) == text


def test_js_json_matches_json_stringify_indent_two() -> None:
    assert js_json({"a": [1, {"b": None}], "c": {}, "d": [], "e": "é\n"}) == (
        '{\n  "a": [\n    1,\n    {\n      "b": null\n    }\n  ],\n  "c": {},\n  "d": [],\n'
        '  "e": "é\\n"\n}'
    )


@pytest.mark.parametrize(
    ("wire", "tool"),
    [
        pytest.param("mcp__alkera__blob.query", "blob.query", id="mcp-prefix"),
        pytest.param("alkera_blob_query", "blob.query", id="sanitized"),
        pytest.param("Bash", "bash", id="case"),
        pytest.param("ripgrep", "grep", id="alias"),
        pytest.param("task", None, id="no-tool"),
    ],
)
def test_resolve_tool(wire: str, tool: str | None) -> None:
    assert resolve_tool(wire) == tool


# --------------------------------------------------------------------------
# Reading the event
# --------------------------------------------------------------------------


def test_ask_from_event_reads_what_the_web_fold_reads() -> None:
    parsed = ask_from_event(
        {
            "permission_kind": "mcp__alkera__sql.query",
            "canonical_kind": "martian",
            "patterns": ["select 1", 7],
            "subject_pending": "yes",
            "subject": {
                "capability": "sql",
                "effect": "teleport",
                "targets": [{"name": "t"}, {}, {"kind": "schema"}],
                "cost_estimate": {
                    "usd_amount": 0.1,
                    "bytes_scanned": 10.0,
                    "wallet_currency": "usd",
                },
                "scope": "operation",
            },
            "preview": {"kind": "weird", "content": "x", "truncated": 1},
            "options": [{"option_id": "allow_once"}, {"name": "N"}, "junk"],
        }
    )
    assert parsed.canonical_kind == "other"
    assert parsed.patterns == ["select 1"]
    assert parsed.subject_pending is False
    assert parsed.subject is not None
    assert parsed.subject.effect == "unknown"
    assert [(t.kind, t.name) for t in parsed.subject.targets] == [("resource", "t"), ("schema", "")]
    assert parsed.subject.cost is not None and parsed.subject.cost.bytes_scanned == 10
    assert parsed.subject.scope is None
    assert parsed.preview is not None and (parsed.preview.kind, parsed.preview.truncated) == (
        "text",
        False,
    )
    # Every entry becomes an option, a malformed one read as a rejection, the
    # way the web fold reads it: the two surfaces offer the same buttons.
    assert [(o.option_id, o.name) for o in parsed.options] == [
        ("allow_once", "Reject"),
        ("reject_once", "N"),
        ("reject_once", "Reject"),
    ]


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param(None, id="absent"),
        pytest.param({"targets": [], "reasons": []}, id="empty"),
        pytest.param("not a dict", id="wrong-type"),
    ],
)
def test_a_subject_that_says_nothing_is_none(subject: Any) -> None:
    assert ask_from_event({"subject": subject}).subject is None
