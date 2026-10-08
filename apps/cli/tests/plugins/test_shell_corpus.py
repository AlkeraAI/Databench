"""The shell corpus: the classifier on the unified model, held to the bar.

Two independent parsers used to decide what a shell command reads and writes:
the permission classifier (``permissions.bash``, tree-sitter) decided the
EFFECT, and the box's fence (``cloud.fence``, a hand lexer with two readers of
its own) decided the LOCATIONS. ``fixtures/shell/corpus.json`` pinned what all
three said on 1,804 commands before they were unified onto one shell-effect
model (``permissions.shell``). The fence side of the bar (every write the old
readers found is still found, everything they refused is still refused) is
held in ``test_shell_model.py``; this module holds the classifier side: on
every row, the classifier on the model is at least as strict as the classifier
was, and every row where it is stricter is named below, with the reason.

The disagreements the corpus surfaced between the old readers are documented
at the end, with the rows that showed them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base.permissions import classify_command
from alkera_cli.plugins.plugin_base.permissions.shell import READERS, WRAPPERS, analyze_shell

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "shell" / "corpus.json"
ROWS: list[dict[str, Any]] = json.loads(CORPUS.read_text(encoding="utf-8"))["rows"]
BY_ID: dict[str, dict[str, Any]] = {row["id"]: row for row in ROWS}
CASES = [pytest.param(row, id=row["id"]) for row in ROWS]

SEVERITY = {"read": 0, "write": 1, "egress": 2, "destroy": 3, "exec": 4}


def classifier_verdict(command: str) -> dict[str, Any]:
    """The classifier's verdict in the corpus's shape."""
    descriptor = classify_command(command)
    return {
        "effect": str(descriptor.effect),
        "confidence": descriptor.confidence,
        "operation": descriptor.operation,
        "scope": descriptor.scope,
        "statements": [
            [str(statement.effect), statement.operation, statement.heuristic]
            for statement in descriptor.statements
        ],
    }


def test_the_corpus_is_the_size_it_was_generated_at() -> None:
    """Every id is unique and every row carries all three verdicts, so a row
    that is dropped or half-filled by a hand edit is noticed."""
    assert len(BY_ID) == len(ROWS) == 1925
    for row in ROWS:
        assert set(row) >= {
            "command",
            "env",
            "backslash_escapes",
            "classifier",
            "write_targets",
            "locations",
        }
        assert set(row["classifier"]) == {
            "effect",
            "confidence",
            "operation",
            "scope",
            "statements",
        }


# --------------------------------------------------------------------------- #
# The classifier side of the bar
# --------------------------------------------------------------------------- #

#: Rows where the classifier on the model is STRICTER than the classifier was,
#: each with what changed and why. Effect first; confidence only matters when it
#: becomes ``unknown`` (the policy treats ``exact`` and ``heuristic`` alike).
TIGHTENED: dict[str, str] = {
    # A variable set for an observer (LD_PRELOAD, PAGER, BASH_ENV, ...) can make it
    # run code the command does not show, so only inert ones keep it a read.
    "cloud_shell_fence/a-dot-relative-name-behind-env": "an env prefix can change what runs",
    "cloud_shell_fence/named-by-an-option": "an env prefix can change what runs",
    "cloud_shell_read_ask/a-listing-behind-env": "an env prefix can change what runs",
    "computed/floor-behind-env-the-operators-token": "an env prefix can change what runs",
    "computed/floor-behind-env-own-environ": "an env prefix can change what runs",
    "computed/floor-behind-env-another-process-environ": "an env prefix can change what runs",
    "computed/floor-behind-env-sibling-chat-absolute": "an env prefix can change what runs",
    "computed/floor-behind-env-sibling-chat-relative": "an env prefix can change what runs",
    "computed/floor-behind-env-sibling-chat-through-tilde": "an env prefix can change what runs",
    "computed/floor-behind-env-sibling-chat-through-home": "an env prefix can change what runs",
    "evasion/assignment-prefix-home": "an env prefix can change what runs",
    "evasion/assignment-prefix-read": "an env prefix can change what runs",
    # rg --pre runs the named program on every file it searches.
    "cloud_shell_fence/rg-pre": "rg --pre runs a program",
    "cloud_shell_fence/rg-pre-detached": "rg --pre runs a program",
    "table/fence-program-option/rg---pre": "rg --pre runs a program",
    # gh reads only in named two-level forms; a bare group is not one of them.
    "table/bash-subcommand-read/gh-auth": "a bare gh group is not a known read",
    "table/bash-subcommand-read/gh-issue": "a bare gh group is not a known read",
    "table/bash-subcommand-read/gh-pr": "a bare gh group is not a known read",
    "table/bash-subcommand-read/gh-repo": "a bare gh group is not a known read",
    "table/bash-subcommand-read/gh-run": "a bare gh group is not a known read",
    "table/bash-subcommand-read/terraform-fmt": "terraform fmt rewrites files",
    # less -o / -O / --log-file write a log file; the old classifier read less as a
    # pure observer.
    "table/fence-flag-writer/less---LOG-FILE": "less --LOG-FILE writes a file",
    "table/fence-flag-writer-detached/less---LOG-FILE": "less --LOG-FILE writes a file",
    "table/fence-flag-writer/less---log-file": "less --log-file writes a file",
    "table/fence-flag-writer-detached/less---log-file": "less --log-file writes a file",
    "table/fence-flag-writer/less--O": "less -O writes a file",
    "table/fence-flag-writer/less--o": "less -o writes a file",
    "evasion/less-o": "less -o writes a file",
    "evasion/less-log-file": "less --log-file writes a file",
    # A redirect the old walk never looked at: before the command, or nested
    # after a here-document.
    "evasion/redirect-before-command": "a redirect written before the command is a write",
    "evasion/heredoc-to-file": "a redirect after a here-document is a write",
    "evasion/redirect-read-write": "<> opens the file for writing too",
    # A wrapper whose value option the old stripper left standing as the command.
    "evasion/env-u": "env -u NAME takes a value; the inner command is rm",
    "evasion/ionice-c": "ionice -c N takes a value; the inner command is rm",
    "evasion/chrt-f": "chrt takes a priority; the inner command is rm",
    "evasion/timeout-s": "timeout -s SIG takes a value; the inner command is rm",
    "evasion/timeout-k": "timeout -k DUR takes a value; the inner command is rm",
    "evasion/stdbuf-o-L": "stdbuf -o MODE takes a value; the inner command is rm",
    "evasion/watch-n": "watch -n N takes a value; the inner command is rm",
    # Wrappers the old classifier did not know.
    "evasion/busybox": "busybox runs its argument as the command",
    "evasion/coproc": "coproc runs its argument as the command",
    "evasion/su-c": "su -c runs its script through a shell",
    "table/fence-wrapper/su": "su without -c opens a shell the model cannot read",
    "evasion/env-S": "env -S splits a string into a command the model cannot read",
    "evasion/sh-c-nested-thrice": "three nested shells are read through to rm",
    # xargs hands its inner command arguments from stdin, so a fetch through it
    # reaches hosts the model cannot see.
    "evasion/xargs-curl": "xargs curl reaches hosts read from stdin",
    # A program name the old walk took at its raw spelling, quotes and all.
    "evasion/quoted-name-single": "'rm' is rm",
    "evasion/quoted-name-double": '"rm" is rm',
    "evasion/quoted-name-concat": "r''m is rm",
    "evasion/quoted-name-concat-double": 'r"m" is rm',
    "evasion/escaped-name": "\\rm is rm",
    "evasion/escaped-name-inner": "r\\m is rm",
    # A computed program name with literal arguments was a heuristic write.
    "evasion/dynamic-name": "a computed command name is unknown",
    "evasion/dynamic-name-subst": "a computed command name is unknown",
    "evasion/dynamic-name-concat": "a computed command name is unknown",
    "evasion/subst-as-whole": "a computed command name is unknown",
    # Write forms the classifier's own program logic missed.
    "evasion/sed-w": "a sed w command writes a file",
    "evasion/sed-s-w": "a sed s///w flag writes a file",
    "evasion/find-fprint": "find -fprint writes a file",
    "evasion/find-fprintf": "find -fprintf writes a file",
    "evasion/find-fls": "find -fls writes a file",
    "appended/toybox-rm": "toybox runs its argument as the command",
    "appended/curl-o-tilde-local": "curl -o writes a file, even from this machine",
    # Found by trying to break the model.
    "adversarial/ansi-c-name": "a $'...' program name is computed",
    "adversarial/exec-a": "exec -a NAME takes a value; the program is rm",
    "adversarial/heredoc-append": "a redirect after a here-document is a write",
    "adversarial/crlf": "a redirect alone after a carriage return truncates its file",
    "adversarial/empty-command": "an empty command name runs nothing known",
    "adversarial/empty-subst": "an empty substitution as the command is computed",
    "adversarial/subst-alone": "a substitution as the whole command is computed",
    "adversarial/backtick-alone": "a backtick substitution as the whole command is computed",
    "adversarial/empty-string-name": "an empty command name runs nothing known",
    "adversarial/deep-nesting-200": "nesting past the limit is left unread",
}

#: Rows where the classifier on the model is LOOSER than the classifier was,
#: listed for the owner's sign-off. Every one is a wrapper whose value option
#: the old stripper left standing as the command name (``ionice -c 3 cat x``
#: read as a program called ``3``), or a wrapper it did not know; the real
#: command is a read, and a read is what it now is. The same stripping makes
#: ``ionice -c 3 rm -rf x`` a destroy (see TIGHTENED), which the old reading
#: let through as a recoverable write.
LOOSENED: dict[str, str] = {
    "appended/toybox-cat": "toybox cat is cat",
    "appended/ionice-c-read": "ionice -c 3 cat is cat",
    "appended/timeout-s-read": "timeout -s KILL 5 cat is cat",
    "appended/chrt-f-read": "chrt -f 10 cat is cat",
    "appended/watch-n-read": "watch -n 1 cat is cat",
    "appended/stdbuf-o-L-read": "stdbuf -o L cat is cat",
    "appended/env-C-read": "env -C /tmp cat is cat",
    "appended/env-u-read": "env -i -u PATH cat is cat",
    "appended/su-c-read": "su -c 'cat notes.md' runs cat",
    "appended/busybox-read": "busybox cat is cat",
    "appended/coproc-read": "coproc cat is cat",
}

#: Rows whose effect and confidence are unchanged but whose operation is: the
#: old wrapper stripper left a value standing as the command name.
OPERATION_CORRECTED: dict[str, tuple[str, str]] = {
    "evasion/env-C": ("-C", "tee"),
    "evasion/xargs-I": ("tmp", "cp"),
    "appended/xargs-I-read": ("xargs", "cat"),
    # The old reader took the word after ``> /dev/null`` for the redirect's
    # target, so the discard looked like a write and the real argument was lost.
    "adversarial/tee-after-redirect": ("echo", "tee"),
    "adversarial/sort-o-after-redirect": ("sort", "sort_output"),
    "adversarial/bare-redirect": ("unknown", "dynamic"),
}

#: The same misreading made ``cp >/dev/null a /etc/b`` and ``sort >/dev/null -o
#: /etc/passwd in`` look like write redirects, so their grants were scoped to the
#: exact text; a discard to ``/dev/null`` leaves them the plain cp and ``sort -o``
#: every other row of theirs is scoped as.
SCOPE_CORRECTED: frozenset[str] = frozenset(
    {"adversarial/cp-after-redirect", "adversarial/sort-o-after-redirect"}
)

#: Rows whose grant scope narrows from a family to the exact text: a write
#: redirect inside a nested shell now counts as the structure it is.
SCOPE_NARROWED: frozenset[str] = frozenset(
    {
        "cloud_write_fence/a-nested-shell",
        "cloud_write_fence/a-nested-sh",
        "cloud_shell_fence/a-nested-shell-that-writes",
        "cloud_shell_fence/a-write-off-the-floor",
        "restated/shell-fence/assignment-rebuilds-proc-and",
    }
)


def _unknown(confidence: str) -> bool:
    return confidence == "unknown"


@pytest.mark.parametrize("row", CASES)
def test_the_classifier_on_the_model_is_at_least_as_strict_as_it_was(row: dict[str, Any]) -> None:
    old = row["classifier"]
    new = classifier_verdict(row["command"])
    looser = SEVERITY[new["effect"]] < SEVERITY[old["effect"]] or (
        new["effect"] == old["effect"]
        and _unknown(old["confidence"])
        and not _unknown(new["confidence"])
    )
    if looser:
        assert row["id"] in LOOSENED, (old, new)
        assert (old["effect"], new["effect"]) == ("write", "read")
        assert old["confidence"] == "heuristic"
        return
    assert row["id"] not in LOOSENED, "listed as loosened but not looser"
    stricter = SEVERITY[new["effect"]] > SEVERITY[old["effect"]] or (
        _unknown(new["confidence"]) and not _unknown(old["confidence"])
    )
    if stricter:
        assert row["id"] in TIGHTENED, (old, new)
        return
    assert row["id"] not in TIGHTENED, "listed as tightened but unchanged"
    assert new["effect"] == old["effect"]
    if row["id"] in OPERATION_CORRECTED:
        assert (old["operation"], new["operation"]) == OPERATION_CORRECTED[row["id"]]
    else:
        assert new["operation"] == old["operation"], (old, new)
    if row["id"] in SCOPE_NARROWED:
        assert (old["scope"], new["scope"]) == ("operation", "command")
    elif row["id"] in SCOPE_CORRECTED:
        assert (old["scope"], new["scope"]) == ("command", "operation")
    else:
        assert new["scope"] == old["scope"], (old, new)


ESCAPED_CASES = [pytest.param(row, id=row["id"]) for row in ROWS if "\\" in row["command"]]


@pytest.mark.parametrize("row", ESCAPED_CASES)
def test_the_classifier_reads_a_backslash_the_same_on_a_windows_host(
    row: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``\\rm`` is ``rm`` to bash on any host. A verdict that moved with
    ``os.name`` would reopen the escaped-name evasion on one platform."""
    posix = classifier_verdict(row["command"])
    monkeypatch.setattr(os, "name", "nt")
    assert classifier_verdict(row["command"]) == posix


def test_every_listed_change_names_a_row() -> None:
    for row_id in [
        *TIGHTENED,
        *LOOSENED,
        *OPERATION_CORRECTED,
        *SCOPE_NARROWED,
        *SCOPE_CORRECTED,
    ]:
        assert row_id in BY_ID, row_id


# --------------------------------------------------------------------------- #
# The disagreements the corpus surfaced between the old readers
# --------------------------------------------------------------------------- #


def _unreadable(verdict: Any) -> bool:
    return isinstance(verdict, dict)


@pytest.mark.parametrize(
    ("row_id", "effect", "write_targets"),
    [
        # getopt reads ``-to`` as ``-t o``; the fence read the ``o`` as its output flag.
        pytest.param("permissions_bash_outputs/sort-separator-o-not-output", "read", ["file.txt"]),
        # ``uniq -f N`` consumes N; the fence counted N as an operand and wrote ``file``.
        pytest.param("permissions_bash_outputs/uniq-skip-fields-value", "read", ["file"]),
        pytest.param("permissions_bash_outputs/uniq-two-value-flags", "read", ["file"]),
        # ``less -o`` writes its log file; the classifier read ``less`` as an observer.
        pytest.param("evasion/less-o", "read", ["/tmp/out"]),
        # A redirect written before the command is a child of the ``command`` node,
        # not of a ``redirected_statement``, and the classifier never looked there.
        pytest.param("evasion/redirect-before-command", "read", ["/etc/x"]),
        # A redirect after a here-document nests inside the ``heredoc_redirect``.
        pytest.param("evasion/heredoc-to-file", "read", ["/etc/x"]),
    ],
)
def test_the_fence_found_a_write_the_classifier_called_a_read(
    row_id: str, effect: str, write_targets: list[str]
) -> None:
    row = BY_ID[row_id]
    assert row["classifier"]["effect"] == effect
    assert row["write_targets"] == write_targets


@pytest.mark.parametrize(
    ("row_id", "effect"),
    [
        # getopt_long accepts any unambiguous abbreviation of ``--output``.
        pytest.param("permissions_bash_outputs/sort-output-abbrev-eq", "write"),
        pytest.param("permissions_bash_outputs/sort-output-shortest-abbrev", "write"),
        # ``xxd IN OUT`` and ``jq -i`` write; the fence listed both as pure observers.
        pytest.param("permissions_bash_outputs/xxd-two-positionals", "write"),
        pytest.param("permissions_bash_outputs/jq-in-place-long", "write"),
        # ``>& file`` sends both streams to the file; the fence dropped it as a dup.
        pytest.param("permissions_bash_outputs/csh-both-to-file", "write"),
        # The fence joined lines before lexing, so a ``#`` ate every line after it.
        pytest.param("evasion/list-comment-then-line", "write"),
    ],
)
def test_the_classifier_found_a_write_the_fence_read_as_writing_nowhere(
    row_id: str, effect: str
) -> None:
    row = BY_ID[row_id]
    assert row["classifier"]["effect"] == effect
    assert row["write_targets"] == []


@pytest.mark.parametrize(
    ("row_id", "write_targets", "reason"),
    [
        # A destination the shell computes was read as a literal file name by the
        # write reader, so ``echo x > $OUT`` landed "inside" and was not refused.
        pytest.param(
            "permissions_bash_outputs/dynamic-var-target", ["$OUT"], "$OUT is not a value"
        ),
        pytest.param(
            "permissions_bash_outputs/dynamic-string-target", ["$x.log"], "$x is not a value"
        ),
        # A command substitution was invisible to shlex: the write reader saw no writes.
        pytest.param("permissions_bash/subst-destroy", [], "a subshell this fence cannot read"),
        pytest.param("permissions_bash/backtick-destroy", [], "runs a command of its own"),
        pytest.param("cloud_shell_fence/a-cd-in-a-pipeline", [], "a cd this fence cannot follow"),
        pytest.param("cloud_shell_fence/rg-pre", [], "runs a program this fence cannot read"),
    ],
)
def test_the_fences_two_readers_disagreed_with_each_other(
    row_id: str, write_targets: list[str], reason: str
) -> None:
    """``shell_write_targets`` (the write fence, the sandbox carve-out) read a
    command ``shell_locations`` (the shell gate) refused."""
    row = BY_ID[row_id]
    assert row["write_targets"] == write_targets
    assert _unreadable(row["locations"])
    assert reason in row["locations"]["unreadable"]


@pytest.mark.parametrize(
    "row_id",
    [
        # A nested shell, a wrapper, an interpreter: the classifier read through
        # them; the fence refused them whole.
        pytest.param("cloud_write_fence/a-nested-shell"),
        pytest.param("cloud_write_fence/a-command-behind-env"),
        pytest.param("cloud_write_fence/a-command-behind-sudo"),
        pytest.param("permissions_bash/sudo-ls-still-read"),
        pytest.param("permissions_bash/nice-make"),
        # A writer in no fence table: the classifier knew its effect, the fence
        # could not name its destination.
        pytest.param("cloud_write_fence/touch"),
        pytest.param("cloud_write_fence/mkdir"),
        pytest.param("permissions_bash/sed-inplace"),
    ],
)
def test_the_classifier_read_through_what_the_fence_refused(row_id: str) -> None:
    row = BY_ID[row_id]
    assert row["classifier"]["confidence"] in ("exact", "heuristic")
    assert _unreadable(row["write_targets"])
    assert _unreadable(row["locations"])


@pytest.mark.parametrize(
    ("row_id", "locations"),
    [
        # ``[`` was a fence reader but no ``command`` node, so the classifier saw
        # no statement at all and failed closed.
        pytest.param(
            "evasion/test-reads-path",
            [["/etc/passwd", False, [], False], ["]", False, [], False]],
        ),
        # A null byte was rejected by the classifier before parsing; the fence lexed it.
        pytest.param("evasion/null-byte", [["x\x00y", False, [], False]]),
        # A comment alone runs nothing; the classifier called a statement-less
        # source unparseable, the fence read an empty command.
        pytest.param("evasion/comment-only", []),
    ],
)
def test_the_fence_read_what_the_classifier_called_opaque(
    row_id: str, locations: list[list[Any]]
) -> None:
    row = BY_ID[row_id]
    assert row["classifier"]["confidence"] == "unknown"
    assert row["locations"] == locations


def test_the_corpus_covers_every_program_the_model_knows() -> None:
    """One row per reader and wrapper of the model, so a program added to the
    model without a corpus row fails here."""
    commands = {row["command"] for row in ROWS}
    for name in READERS:
        assert f"{name} in.txt" in commands or f"{name} in.txt out.txt" in commands, name
    for name in WRAPPERS:
        assert any(command.split(" ", 1)[0] == name for command in commands), name


def test_a_wrapper_added_to_the_model_reads_through_to_its_command() -> None:
    (inv,) = analyze_shell("toybox rm -rf x").invocations
    assert inv.program == "rm"
