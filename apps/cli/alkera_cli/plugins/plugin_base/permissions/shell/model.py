"""The shell-effect model: one reading of a shell command for every consumer.

:func:`analyze_shell` parses a command with tree-sitter-bash and reports what
it touches: every invocation (the program, its literal argv, the wrappers and
nested shells it was reached through, the redirects in force on it), every
location read or written with the ``cd`` chain in force and whether it is a
glob, the hosts the network programs are pointed at, and everything the model
could not resolve or could only look through. Two consumers read it:

* the permission classifier, which decides the EFFECT of each invocation from
  its own tables and escalates a read to a write when the model found an
  output destination or a redirect into a file;
* the workspace fence, which judges every location and refuses to vouch for a
  command with anything in ``opaque``.

The model takes no side in either policy. It never resolves a path on disk,
never decides what counts as this machine and never names an effect; it says
what the shell would do with the words, and where it cannot say, it says so.

Fail-closed by construction: a parse error, a null byte, an oversize command,
an argument the shell computes, a variable the environment does not carry, a
nested shell, an interpreter, a program in no table, a redirect with no
destination and a structure the model does not scope all land in ``opaque``
rather than in a guess of "nowhere". The sentences there are phrased for the
fence, the one consumer that shows them to the model.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Final, Literal

import tree_sitter_bash
from tree_sitter import Language, Node, Parser, Tree

from alkera_cli.plugins.plugin_base.permissions.shell.programs import (
    CODE_INTERPRETERS,
    SHELLS,
    WRAPPERS,
    Opaque,
    OpaqueKind,
    ProgramEffects,
    assignment_name,
    program_effects,
    shell_script,
    strip_wrapper,
)

_LANGUAGE = Language(tree_sitter_bash.language())
# A tree-sitter Parser is not safe for concurrent parse() calls, and the fence
# judges off the event loop in a worker thread while the classifier parses on
# it, so each thread owns a parser.
_THREAD = threading.local()

MAX_COMMAND_BYTES: Final = 100_000
"""Longer than any real command; past it the command is refused unparsed."""
MAX_WRAP_DEPTH: Final = 4
"""How many wrappers and nested shells are read through before the rest is
left opaque."""
_MAX_NESTING: Final = 120
"""How deep a parse tree is walked before the rest is left opaque; a command
nested past this is not one a person wrote. Each level of the tree costs a few
Python frames, so the bound sits well under the interpreter's own limit."""

Role = Literal["program", "wrapper", "shell", "eval", "interpreter"]


def _parser() -> Parser:
    parser = getattr(_THREAD, "parser", None)
    if parser is None:
        parser = Parser(_LANGUAGE)
        _THREAD.parser = parser
    assert isinstance(parser, Parser)
    return parser


def backslash_escapes_here() -> bool:
    """Whether this host's shell reads ``\\`` as an escape rather than as the
    path separator. Read at call time so a test can pin either dialect. Only a
    caller that places locations on the host asks for it; :func:`analyze_shell`
    reads the POSIX dialect unless told otherwise."""
    return os.name != "nt"


# --------------------------------------------------------------------------- #
# What the model reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ShellLocation:
    """One location a shell command names, as the shell would see it."""

    text: str
    writing: bool
    moved: tuple[str, ...] = ()
    """The ``cd`` operands in force when the command reaches this location, in
    order. A relative name resolves against where they lead."""
    glob: bool = False
    """Whether an UNQUOTED wildcard is in the name: a quoted ``*`` is a
    character, and a name that only carries one is not a pattern."""


@dataclass(frozen=True, slots=True)
class Redirect:
    """One redirection in force on an invocation."""

    operator: str
    target: str | None
    """The destination as the shell would see it, ``None`` when the shell
    computes it (and the model has reported why)."""
    writing: bool
    """Whether the operator opens the destination for writing."""
    plain: bool = True
    """Whether the destination was spelled as one plain word or quoted string,
    rather than assembled from pieces or expansions."""


@dataclass(frozen=True, slots=True)
class NetworkReach:
    """A program whose job is to contact a host, with where it was pointed."""

    program: str
    destinations: tuple[str, ...]
    """Every literal argument that is not an option. Which of them name this
    machine is the consumer's policy."""
    dynamic: bool
    """An argument the shell computes may name a host the model cannot see."""


@dataclass(frozen=True, slots=True)
class Invocation:
    """One program run, with its arguments as the shell hands them over."""

    name: str
    """argv[0] as resolved (quotes and escapes removed), ``""`` when the shell
    computes it."""
    program: str
    """The basename of ``name``: ``/bin/rm`` runs ``rm``."""
    argv: tuple[str, ...]
    """The literal arguments. One the shell computes is left out; ``dynamic``
    says so."""
    dynamic: bool
    """An argument was not plainly literal: an expansion, a substitution, a
    word assembled from pieces, or an assignment prefix that changes what the
    words expand to."""
    role: Role
    """``program`` for an ordinary run; ``wrapper`` for a wrapper with nothing
    after it; ``shell`` for a shell whose script the model could not read;
    ``eval`` for an eval of text it could not read; ``interpreter`` for a
    program that runs code from its arguments."""
    depth: int
    """How many wrappers and nested shells it was reached through."""
    via_shell: bool
    """Reached by re-reading a nested shell's ``-c`` string."""
    redirects: tuple[Redirect, ...]
    """The file redirects in force on it, its own and its enclosing
    statements'."""
    piped: bool
    """It reads a pipe on stdin (it is not the head of its pipeline)."""
    reads: tuple[str, ...]
    """The locations its own arguments read."""
    writes: tuple[str, ...]
    """The destinations its own arguments and options name."""
    unnamed_write: bool
    """It writes a file its arguments do not name (an output option with no
    value, an in-place edit with no file)."""
    network: NetworkReach | None
    environment: tuple[str, ...] = ()
    """The names of the variables an assignment prefix sets for it
    (``LD_PRELOAD=x ls``, ``env PAGER=y man ls``), its own and those of the
    wrappers and shells it was reached through. A variable can change what an
    observer runs, so the classifier weighs them."""


@dataclass(frozen=True, slots=True)
class ShellEffects:
    """Everything the model read off one command."""

    command: str
    invocations: tuple[Invocation, ...]
    locations: tuple[ShellLocation, ...]
    """Every location in judging order: a statement's redirects, then its
    command's reads, then its writes."""
    opaque: tuple[Opaque, ...]
    """What the model could not resolve, or could only look through (a wrapper,
    a nested shell), in the order met. A consumer that reads through wrappers
    skips the ``wrapper`` and ``shell`` kinds; one that does not treats every
    entry as a reason it cannot vouch for the command."""
    statements: int
    """Top-level statements in the command (comments aside)."""
    compound_structure: bool
    """A pipeline, a list, a loop, a branch, a subshell, a group, a function or
    a substitution: the command is more than one plain invocation."""
    parse_error: bool
    """The parser could not read all of the command; what it did read is here."""

    @property
    def writes(self) -> tuple[ShellLocation, ...]:
        return tuple(location for location in self.locations if location.writing)

    @property
    def reads(self) -> tuple[ShellLocation, ...]:
        return tuple(location for location in self.locations if not location.writing)

    @property
    def wrappers(self) -> tuple[str, ...]:
        """The wrappers and nested shells the model read through."""
        return tuple(entry.text for entry in self.opaque if entry.kind in ("wrapper", "shell"))

    @property
    def network(self) -> tuple[NetworkReach, ...]:
        return tuple(inv.network for inv in self.invocations if inv.network is not None)

    @property
    def readable(self) -> bool:
        """Whether every location was resolved and nothing was merely looked
        through: the fence's bar for vouching for a command."""
        return not self.opaque


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

_WILDCARDS: Final = frozenset("*?[")
_WRITE_OPERATORS: Final = frozenset({">", ">>", "&>", "&>>", ">|"})
_SEQUENTIAL: Final = frozenset({"", ";", "&&"})
_SEQUENCES: Final = frozenset(
    {"program", "list", "compound_statement", "do_group", "case_item", "elif_clause", "else_clause"}
)
_STRUCTURES: Final[Mapping[str, tuple[str, str]]] = {
    "for_statement": ("for", "a loop this fence cannot read"),
    "c_style_for_statement": ("for", "a loop this fence cannot read"),
    "while_statement": ("while", "a loop this fence cannot read"),
    "if_statement": ("if", "a branch this fence cannot read"),
    "case_statement": ("case", "a branch this fence cannot read"),
    "function_definition": ("function", "a function definition this fence cannot read"),
}
_ASSIGNMENTS: Final = frozenset({"variable_assignment", "declaration_command", "unset_command"})
_REDIRECT_NODES: Final = frozenset({"file_redirect", "heredoc_redirect", "herestring_redirect"})
_WORD_NODES: Final = frozenset(
    {
        "word",
        "string",
        "raw_string",
        "number",
        "concatenation",
        "simple_expansion",
        "expansion",
        "command_substitution",
        "test_operator",
    }
)
_DOUBLE_QUOTE_ESCAPES: Final = frozenset('"$`\\')


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class _Word:
    """One argument as the shell will hand it on."""

    text: str | None
    """``None`` when the shell computes it and the model cannot."""
    glob: bool = False
    literal: bool = True
    """Spelled as one plain word, number or quoted string with no expansion
    and no pieces: what a reader with no environment can take at face value."""


@dataclass(frozen=True, slots=True)
class _Redirected:
    """A redirect read off the tree, with the location it names (``None`` when
    the shell computes it), still to be placed in the ``cd`` chain."""

    redirect: Redirect
    location: ShellLocation | None
    trailing: tuple[Node, ...] = ()
    """Words tree-sitter attached to the redirect that are really arguments
    of the command it applies to (``tee > /dev/null FILE``)."""


@dataclass(frozen=True, slots=True)
class _Context:
    """Where in the command a node sits."""

    redirects: tuple[Redirect, ...] = ()
    depth: int = 0
    nesting: int = 0
    via_shell: bool = False
    wrapped: bool = False
    piped: bool = False
    in_pipeline: bool = False
    before: str = ""
    after: str = ""
    trailing: tuple[Node, ...] = ()
    """Arguments a redirect carried for the last simple command under it."""
    environment: tuple[str, ...] = ()
    """Variables an assignment prefix or ``env`` set for what runs here."""

    def inside(
        self,
        *,
        redirects: tuple[Redirect, ...] | None = None,
        piped: bool | None = None,
        in_pipeline: bool | None = None,
        before: str | None = None,
        after: str | None = None,
        trailing: tuple[Node, ...] | None = None,
    ) -> _Context:
        """The context one level further into the tree."""
        return _Context(
            redirects=self.redirects if redirects is None else redirects,
            depth=self.depth,
            nesting=self.nesting + 1,
            via_shell=self.via_shell,
            wrapped=self.wrapped,
            piped=self.piped if piped is None else piped,
            in_pipeline=self.in_pipeline if in_pipeline is None else in_pipeline,
            before=self.before if before is None else before,
            after=self.after if after is None else after,
            trailing=self.trailing if trailing is None else trailing,
            environment=self.environment,
        )


@dataclass
class _Walk:
    env: Mapping[str, str] | None
    backslash_escapes: bool
    home_as_text: bool = False
    invocations: list[Invocation] = field(default_factory=list)
    locations: list[ShellLocation] = field(default_factory=list)
    opaque: list[Opaque] = field(default_factory=list)
    deferred: list[tuple[Node, _Context]] = field(default_factory=list)
    """Substitution bodies met while reading a command's words, visited once
    the command itself is on record: the command comes first, as it is read,
    and each body runs in a subshell of its own that moves nothing outside."""
    moved: tuple[str, ...] = ()
    compound_structure: bool = False
    parse_error: bool = False

    def refuse(self, kind: OpaqueKind, text: str, reason: str) -> None:
        entry = Opaque(kind, text, reason)
        if entry not in self.opaque:
            self.opaque.append(entry)

    def flush(self) -> None:
        while self.deferred:
            node, ctx = self.deferred.pop(0)
            moved = self.moved
            self.visit_children(node, ctx)
            self.moved = moved

    # -- words ---------------------------------------------------------------

    def word(self, node: Node, ctx: _Context) -> _Word:
        """``node`` as an argument: its text, whether it globs, and whether it
        was plainly spelled. Anything the shell computes is reported."""
        kind = node.type
        if kind == "word":
            return self._bare_word(_text(node))
        if kind in ("number", "test_operator"):
            return _Word(_text(node))
        if kind == "raw_string":
            return _Word(_text(node)[1:-1])
        if kind == "string":
            return self._string(node, ctx)
        if kind in ("simple_expansion", "expansion"):
            return _Word(self._variable(node), literal=False)
        if kind == "command_substitution":
            self.refuse("substitution", _text(node), f"{_text(node)} runs a command of its own")
            self.compound_structure = True
            self.deferred.append((node, _Context(depth=ctx.depth, nesting=ctx.nesting + 1)))
            return _Word(None, literal=False)
        if kind == "process_substitution":
            self.refuse("structure", _text(node), "a subshell this fence cannot read")
            self.compound_structure = True
            self.deferred.append((node, _Context(depth=ctx.depth, nesting=ctx.nesting + 1)))
            return _Word(None, literal=False)
        if kind == "concatenation":
            return self._concatenation(node, ctx)
        self.refuse("expansion", _text(node), f"{_text(node)} is expanded by the shell")
        return _Word(None, literal=False)

    def _bare_word(self, raw: str) -> _Word:
        if raw in ("{", "}"):
            # Brace expansion arrives as separate ``{`` and ``}`` words around
            # its body. Kept as text, so a destination it names can still be
            # judged, and reported, so nobody vouches for it.
            self.refuse("expansion", raw, f"{raw} is expanded by the shell")
            return _Word(raw, literal=False)
        text, glob = self._unescape(raw)
        if raw.startswith("~"):
            home = self._home(raw)
            if home is None:
                return _Word(None, literal=False)
            text = home + text[1:]
        return _Word(text, glob)

    def _unescape(self, raw: str) -> tuple[str, bool]:
        """``raw`` with backslash escapes applied (in the POSIX dialect) and
        whether an unescaped wildcard remains. In the Windows dialect ``\\`` is
        the path separator and stays."""
        if not self.backslash_escapes:
            return raw, any(char in _WILDCARDS for char in raw)
        out: list[str] = []
        glob = False
        index = 0
        while index < len(raw):
            char = raw[index]
            if char == "\\" and index + 1 < len(raw):
                out.append(raw[index + 1])
                index += 2
                continue
            if char in _WILDCARDS:
                glob = True
            out.append(char)
            index += 1
        return "".join(out), glob

    def _home(self, raw: str) -> str | None:
        """The directory a leading ``~`` names: ``HOME`` from the environment
        for ``~`` and ``~/...``; another user's home is not something the model
        can place. A reader that judges no locations keeps the ``~`` as the
        character it is."""
        home = self.env.get("HOME") if self.env is not None else None
        if home is None and self.home_as_text:
            return "~"
        if home is None or raw[1:2] not in ("", "/"):
            self.refuse("expansion", raw, f"{raw} names a home this fence cannot read")
            return None
        return home

    def _variable(self, node: Node) -> str | None:
        # ``$NAME`` is two tokens and ``${NAME}`` three; anything else in the
        # node (``${#NAME}``, ``${NAME:-x}``, ``${NAME%.*}``) is a parameter
        # form the model does not compute.
        plain = (node.type == "simple_expansion" and len(node.children) == 2) or (
            node.type == "expansion" and len(node.children) == 3
        )
        children = [child for child in node.children if child.is_named]
        if not plain or len(children) != 1 or children[0].type != "variable_name":
            self.refuse("expansion", _text(node), f"{_text(node)} is expanded by the shell")
            return None
        name = _text(children[0])
        if not (name[:1].isalpha() or name[:1] == "_"):
            self.refuse("expansion", _text(node), f"{_text(node)} is expanded by the shell")
            return None
        value = self.env.get(name) if self.env is not None else None
        if value is None:
            self.refuse("variable", f"${name}", f"${name} is not a value this fence can read")
            return None
        return value

    def _string(self, node: Node, ctx: _Context) -> _Word:
        parts: list[str] = []
        literal = True
        for child in node.children:
            if child.type in ("string_content", "escape_sequence"):
                parts.append(self._decode_string_content(_text(child)))
            elif child.type in ('"', "'"):
                continue
            else:
                literal = False
                inner = self.word(child, ctx)
                if inner.text is None:
                    return _Word(None, literal=False)
                parts.append(inner.text)
        return _Word("".join(parts), literal=literal)

    def _decode_string_content(self, text: str) -> str:
        if not self.backslash_escapes:
            return text
        out: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\\" and index + 1 < len(text) and text[index + 1] in _DOUBLE_QUOTE_ESCAPES:
                out.append(text[index + 1])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)

    def _concatenation(self, node: Node, ctx: _Context) -> _Word:
        parts: list[str] = []
        glob = False
        unresolved = False
        for position, child in enumerate(node.children):
            raw = _text(child)
            if (
                position > 0
                and child.type == "word"
                and raw.startswith("~")
                and raw not in ("{", "}")
            ):
                # ``a/~/b``: a tilde past the first character is a character.
                text, is_glob = self._unescape(raw)
                inner = _Word(text, is_glob)
            else:
                inner = self.word(child, ctx)
            if inner.text is None:
                unresolved = True
            else:
                parts.append(inner.text)
            glob = glob or inner.glob
        return _Word(None if unresolved else "".join(parts), glob, literal=False)

    # -- statements ----------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Context) -> None:
        self.sequence([child for child in node.children if child.type != "comment"], ctx)

    def sequence(self, children: list[Node], ctx: _Context) -> None:
        """Visit statements in order, telling each which separator precedes and
        follows it: a ``cd`` is followed only across ``;`` / ``&&`` / a
        newline (two statements with no token between them)."""
        visits: list[tuple[Node, str, str]] = []
        operator: str | None = None
        for child in children:
            if not child.is_named:
                operator = _text(child)
                continue
            if visits:
                separator = operator if operator is not None else ";"
                if separator in (";;", ";&", ";;&"):
                    # A case terminator between two statements outside a case
                    # is text the shell refuses to run.
                    self.parse_error = True
                    self.refuse("parse", separator, "the command could not be parsed")
                previous, before, _after = visits[-1]
                visits[-1] = (previous, before, separator)
                visits.append((child, separator, ctx.after))
            else:
                visits.append((child, ctx.before, ctx.after))
            operator = None
        for index, (node, before, after) in enumerate(visits):
            trailing = ctx.trailing if index == len(visits) - 1 else ()
            self.visit(node, ctx.inside(before=before, after=after, trailing=trailing))

    def visit(self, node: Node, ctx: _Context) -> None:
        if ctx.nesting > _MAX_NESTING:
            self.parse_error = True
            self.refuse("parse", _text(node)[:40], "nested too deep to read")
            return
        kind = node.type
        if kind == "comment":
            return
        if kind in _SEQUENCES:
            if kind in ("list", "compound_statement"):
                self.compound_structure = True
            if kind == "compound_statement":
                self.refuse("structure", "{", "a subshell this fence cannot read")
                self._unplaceable(ctx)
                self.visit_children(
                    node, ctx.inside(in_pipeline=False, before="", after="", trailing=())
                )
                return
            self.visit_children(node, ctx)
            return
        if kind == "subshell":
            self.compound_structure = True
            self.refuse("structure", "(", "a subshell this fence cannot read")
            self._unplaceable(ctx)
            moved = self.moved
            self.visit_children(
                node, ctx.inside(in_pipeline=False, before="", after="", trailing=())
            )
            self.moved = moved
            return
        if kind == "pipeline":
            self.compound_structure = True
            members = [
                child for child in node.children if child.is_named and child.type != "comment"
            ]
            for position, member in enumerate(members):
                last = position == len(members) - 1
                self.visit(
                    member,
                    ctx.inside(
                        piped=position > 0, in_pipeline=True, trailing=ctx.trailing if last else ()
                    ),
                )
            return
        if kind == "redirected_statement":
            self._redirected(node, ctx)
            return
        if kind == "command":
            self._command(node, ctx)
            return
        if kind == "negated_command":
            self.visit_children(node, ctx)
            return
        if kind in _STRUCTURES:
            keyword, reason = _STRUCTURES[kind]
            self.compound_structure = True
            self.refuse("structure", keyword, reason)
            self._unplaceable(ctx)
            self.visit_children(node, ctx.inside(trailing=()))
            return
        if kind == "test_command":
            self._test_command(node, ctx)
            return
        if kind in _ASSIGNMENTS:
            spelled = _text(node).split("\n", 1)[0]
            self.refuse(
                "assignment", spelled, f"{spelled} is an assignment this fence cannot follow"
            )
            self._assignment_values(node, ctx)
            self.flush()
            return
        if kind == "ERROR":
            self.parse_error = True
            self.visit_children(node, ctx)
            return
        if kind in ("command_substitution", "process_substitution", "arithmetic_expansion"):
            self.word(node, ctx)
            return
        if node.is_named:
            self.visit_children(node, ctx)

    def _assignment_values(self, node: Node, ctx: _Context) -> None:
        """The values an assignment computes are read for the commands they run."""
        for child in node.children:
            if child.type == "variable_assignment":
                self._assignment_values(child, ctx)
            elif child.is_named and child.type not in ("variable_name", "word", "number"):
                self.word(child, ctx)

    def _test_command(self, node: Node, ctx: _Context) -> None:
        """``[ ... ]`` reads its operands like the observer it is; ``[[ ... ]]``
        is a shell construct the model does not read. Neither is an invocation."""
        operands: list[str] = []
        globbed: set[str] = set()
        for leaf in _leaves(node):
            if leaf.type not in _WORD_NODES:
                continue
            word = self.word(leaf, ctx)
            if word.text is not None:
                operands.append(word.text)
                if word.glob:
                    globbed.add(word.text)
        opener = _text(node.children[0]) if node.children else "["
        if opener != "[":
            self.refuse(
                "program", opener, f"{opener} is a program whose reach this fence cannot read"
            )
        self._place(program_effects("[", (*operands, "]")), globbed)
        self.flush()

    def _redirected(self, node: Node, ctx: _Context) -> None:
        found: list[_Redirected] = []
        body: Node | None = None
        for child in node.children:
            if child.type in _REDIRECT_NODES:
                found.extend(self._redirect(child, ctx))
            elif child.is_named and child.type != "comment":
                body = child
        writing = tuple(entry.redirect for entry in found if entry.redirect.writing)
        trailing = tuple(node for entry in found for node in entry.trailing)
        inner = ctx.inside(
            redirects=(*ctx.redirects, *writing), trailing=(*ctx.trailing, *trailing)
        )
        if body is not None and body.type == "list":
            # tree-sitter binds ``a && b > f`` as ``(a && b) > f``. The shell
            # redirects ``b`` alone, after ``a`` has run (and moved), so the
            # destination is placed once the statements before the last are
            # read. Every statement still carries the redirect, which is the
            # strict side for an effect.
            self._redirected_list(body, found, inner)
            return
        self._place_redirects(found)
        if body is not None:
            self.visit(body, inner)
        else:
            # ``> file`` alone: no command, and the file is truncated all the
            # same. An invocation of nothing, carrying the redirect.
            self._unplaceable(inner)
            self._invoke("", "", (), False, "program", inner, ProgramEffects())
        self.flush()

    def _unplaceable(self, ctx: _Context) -> None:
        """Words a redirect carried that no simple command is left to take."""
        for node in ctx.trailing:
            self.word(node, ctx)
            self.refuse(
                "redirect",
                _text(node),
                f"{_text(node)} follows a redirection this fence cannot place",
            )

    def _redirected_list(self, node: Node, found: list[_Redirected], ctx: _Context) -> None:
        children = [child for child in node.children if child.type != "comment"]
        statements = [child for child in children if child.is_named]
        if not statements:
            self._place_redirects(found)
            return
        self.compound_structure = True
        last = statements[-1]
        separator = _text(children[-2]) if len(children) >= 2 and not children[-2].is_named else ";"
        self.sequence(
            [child for child in children if child is not last], ctx.inside(after=separator)
        )
        if last.type == "list":
            self._redirected_list(last, found, ctx.inside(before=separator))
            return
        self._place_redirects(found)
        self.visit(last, ctx.inside(before=separator))

    def _place_redirects(self, found: list[_Redirected]) -> None:
        for entry in found:
            if entry.location is not None:
                self.locations.append(replace(entry.location, moved=self.moved))

    def _redirect(self, node: Node, ctx: _Context) -> list[_Redirected]:
        """The redirections one redirect node carries: a file redirect, or a
        here-document (whose body is text, and whose own trailing redirects
        nest inside it). The destination comes back beside its redirect for
        the caller to place once the ``cd`` chain in force is settled."""
        kind = node.type
        if kind == "herestring_redirect":
            for child in node.children:
                if child.is_named:
                    self.word(child, ctx)
            return []
        if kind == "heredoc_redirect":
            found: list[_Redirected] = []
            start = next((c for c in node.children if c.type == "heredoc_start"), None)
            quoted = start is not None and _text(start)[:1] in ("'", '"')
            for child in node.children:
                if child.type == "file_redirect":
                    found.extend(self._redirect(child, ctx))
                elif child.type == "heredoc_body":
                    body = _text(child)
                    if not quoted and ("$" in body or "`" in body):
                        self.refuse("redirect", "<<", "a here-document the shell expands")
                    self.visit_children(child, _Context(depth=ctx.depth, nesting=ctx.nesting + 1))
            return found
        operator = ""
        target: Node | None = None
        trailing: list[Node] = []
        garbled = False
        for child in node.children:
            if child.type == "file_descriptor":
                continue
            if child.type == "ERROR":
                # ``<>`` (open to read and write) is the one form the parser
                # trips on; whatever the operator was, the file is opened.
                garbled = True
            elif not child.is_named:
                operator = child.type
            elif target is None:
                target = child
            else:
                trailing.append(child)
        if garbled:
            self.parse_error = True
            operator = "<>"
        if operator in (">&-", "<&-", "<&"):
            return []
        if operator == ">&" and (target is None or target.type == "number"):
            return []  # a descriptor duplicated, no file touched
        writing = operator in _WRITE_OPERATORS or operator in (">&", "<>")
        if not writing and operator != "<":
            self.refuse("redirect", operator, f"{operator} is a redirection this fence cannot read")
            return []
        if target is None:
            self.parse_error = True
            self.refuse("redirect", operator, "a redirection that names nothing")
            return [_Redirected(Redirect(operator, None, writing, plain=False), None)]
        # tree-sitter attaches a word that follows ``> file`` to the redirect;
        # it is an argument of the command the redirect applies to.
        kept = tuple(trailing)
        word = self.word(target, ctx)
        if word.text is None:
            return [_Redirected(Redirect(operator, None, writing, plain=False), None, kept)]
        if not word.text:
            self.parse_error = True
            self.refuse("redirect", operator, "a redirection that names nothing")
            return [_Redirected(Redirect(operator, None, writing, plain=False), None, kept)]
        if operator == "<>":
            self.locations.append(ShellLocation(word.text, False, self.moved, word.glob))
        return [
            _Redirected(
                Redirect(operator, word.text, writing, plain=word.literal),
                ShellLocation(word.text, writing, self.moved, word.glob),
                kept,
            )
        ]

    def _command(self, node: Node, ctx: _Context) -> None:
        name: _Word | None = None
        assignments: list[str] = []
        argv: list[str] = []
        dynamic = False
        globbed: set[str] = set()
        own: list[Redirect] = []
        for child in (*node.children, *ctx.trailing):
            kind = child.type
            if kind == "command_name":
                name = self.word(child.children[0] if child.children else child, ctx)
            elif kind == "variable_assignment":
                assignments.append(_text(child))
                self._assignment_values(child, ctx)
            elif kind in _REDIRECT_NODES:
                found = self._redirect(child, ctx)
                self._place_redirects(found)
                own.extend(entry.redirect for entry in found)
            elif not child.is_named and _text(child) == "$":
                # The ``$`` of ``$"..."`` (a translated string) stands alone in
                # the tree; the string after it is not the literal it looks.
                self.refuse("expansion", "$", f"{_text(node)} is expanded by the shell")
                dynamic = True
            elif kind == "comment" or not child.is_named:
                continue
            else:
                word = self.word(child, ctx)
                dynamic = dynamic or not word.literal
                if word.text is None:
                    continue
                argv.append(word.text)
                if word.glob:
                    globbed.add(word.text)
        for spelled in assignments:
            # The prefix can change what the command's own words expand to.
            self.refuse(
                "assignment", spelled, f"{spelled} is an assignment this fence cannot follow"
            )
        writing = tuple(r for r in own if r.writing)
        if name is not None and name.text == "":
            self.refuse("program", "''", "a command with no name")
        self._dispatch(
            name.text or "" if name is not None else "",
            tuple(argv),
            dynamic or bool(assignments),
            globbed,
            replace(
                ctx,
                redirects=(*ctx.redirects, *writing),
                trailing=(),
                environment=(*ctx.environment, *map(assignment_name, assignments)),
            ),
        )
        self.flush()

    # -- programs ------------------------------------------------------------

    def _dispatch(
        self, name: str, argv: tuple[str, ...], dynamic: bool, globbed: set[str], ctx: _Context
    ) -> None:
        program = PurePosixPath(name).name or name
        if not name:
            self._invoke("", "", argv, True, "program", ctx, ProgramEffects())
            return
        if program in WRAPPERS and ctx.depth < MAX_WRAP_DEPTH:
            self._wrapper(name, program, argv, dynamic, globbed, ctx)
            return
        if program in SHELLS:
            script = shell_script(program, argv) if ctx.depth < MAX_WRAP_DEPTH else None
            if script is None:
                self.refuse(
                    "interpreter", program, f"{program} carries a command this fence cannot read"
                )
                self._invoke(name, program, argv, dynamic, "shell", ctx, ProgramEffects())
                return
            self.refuse(
                "shell", f"{program} -c", f"{program} carries a command this fence cannot read"
            )
            self._nested(script, ctx, via_shell=True)
            return
        if program == "eval":
            if dynamic or not argv or ctx.depth >= MAX_WRAP_DEPTH:
                self.refuse("interpreter", "eval", "eval carries a command this fence cannot read")
                self._invoke(name, program, argv, dynamic, "eval", ctx, ProgramEffects())
                return
            self.refuse("shell", "eval", "eval carries a command this fence cannot read")
            self._nested(" ".join(argv), ctx, via_shell=ctx.via_shell)
            return
        if program == "cd" and not ctx.wrapped:
            self._cd(name, argv, dynamic, globbed, ctx)
            return
        effects = program_effects(program, argv)
        self._place(effects, globbed)
        role: Role = "interpreter" if program in CODE_INTERPRETERS else "program"
        self._invoke(name, program, argv, dynamic, role, ctx, effects)

    def _wrapper(
        self,
        name: str,
        program: str,
        argv: tuple[str, ...],
        dynamic: bool,
        globbed: set[str],
        ctx: _Context,
    ) -> None:
        stripped = strip_wrapper(program, argv)
        if stripped.opaque is not None:
            self.opaque.append(stripped.opaque)
            self._invoke(name, program, argv, dynamic, "shell", ctx, ProgramEffects())
            return
        if not stripped.inner:
            if program != "env":  # a bare ``env`` prints the environment and runs nothing
                self.refuse(
                    "wrapper", program, f"{program} carries a command this fence cannot read"
                )
            self._invoke(name, program, argv, dynamic, "wrapper", ctx, ProgramEffects())
            return
        self.refuse("wrapper", program, f"{program} carries a command this fence cannot read")
        moved = self.moved
        if stripped.chdir is not None:
            self.locations.append(ShellLocation(stripped.chdir, False, self.moved))
            self.moved = (*self.moved, stripped.chdir)
        self._dispatch(
            stripped.inner[0],
            stripped.inner[1:],
            dynamic or stripped.dynamic,
            globbed,
            replace(
                ctx,
                depth=ctx.depth + 1,
                wrapped=True,
                environment=(*ctx.environment, *stripped.environment),
            ),
        )
        self.moved = moved

    def _cd(
        self, name: str, argv: tuple[str, ...], dynamic: bool, globbed: set[str], ctx: _Context
    ) -> None:
        # ``cd -`` goes back to a directory only the shell remembers.
        bare = [arg for arg in argv if not arg.startswith("-")]
        followable = (
            not dynamic
            and len(bare) == 1
            and not ctx.in_pipeline
            and ctx.before in _SEQUENTIAL
            and ctx.after in _SEQUENTIAL
        )
        if followable:
            self.locations.append(ShellLocation(bare[0], False, self.moved, bare[0] in globbed))
            self.moved = (*self.moved, bare[0])
        else:
            self.refuse("structure", "cd", "a cd this fence cannot follow")
        self._invoke(name, "cd", argv, dynamic, "program", ctx, ProgramEffects())

    def _place(self, effects: ProgramEffects, globbed: set[str]) -> None:
        self.opaque.extend(effects.opaque)
        self.locations.extend(
            ShellLocation(t, False, self.moved, t in globbed) for t in effects.reads
        )
        self.locations.extend(
            ShellLocation(t, True, self.moved, t in globbed) for t in effects.writes
        )

    def _invoke(
        self,
        name: str,
        program: str,
        argv: tuple[str, ...],
        dynamic: bool,
        role: Role,
        ctx: _Context,
        effects: ProgramEffects,
    ) -> None:
        network = (
            NetworkReach(program, effects.network, dynamic) if effects.network is not None else None
        )
        self.invocations.append(
            Invocation(
                name,
                program,
                argv,
                dynamic,
                role,
                ctx.depth,
                ctx.via_shell,
                ctx.redirects,
                ctx.piped,
                effects.reads,
                effects.writes,
                effects.unnamed_write,
                network,
                ctx.environment,
            )
        )

    def _nested(self, source: str, ctx: _Context, *, via_shell: bool) -> None:
        """Read a nested shell's script in the directory the outer shell is
        in; a ``cd`` inside it moves nothing outside."""
        moved = self.moved
        tree = _parse(source)
        if tree is None:
            self.parse_error = True
            self.refuse("parse", source[:40], "a null byte or an oversize command")
            return
        if tree.root_node.has_error:
            self.parse_error = True
        self.visit(
            tree.root_node,
            _Context(
                redirects=ctx.redirects,
                depth=ctx.depth + 1,
                nesting=ctx.nesting + 1,
                via_shell=via_shell,
                piped=ctx.piped,
                environment=ctx.environment,
            ),
        )
        self.moved = moved


def _leaves(node: Node) -> Iterator[Node]:
    for child in node.children:
        if child.type in ("unary_expression", "binary_expression", "parenthesized_expression"):
            yield from _leaves(child)
        elif child.is_named:
            yield child


def _parse(source: str) -> Tree | None:
    data = source.encode("utf-8", "ignore")
    if b"\x00" in data or len(data) > MAX_COMMAND_BYTES:
        return None
    return _parser().parse(data)


def analyze_shell(
    command: str,
    *,
    env: Mapping[str, str] | None = None,
    backslash_escapes: bool = True,
    home_as_text: bool = False,
) -> ShellEffects:
    """Read ``command`` the way the shell will run it.

    ``env`` is the environment the command runs in: ``$NAME`` and a leading
    ``~`` expand from it, and a name it does not carry is reported, never
    guessed. With no environment every expansion is unresolved, except that a
    reader which judges effects rather than locations may ask, with
    ``home_as_text``, for a leading ``~`` to stay the character it is.
    ``backslash_escapes`` picks the dialect. The default is the POSIX shell's,
    which reads ``\\`` as an escape, whatever the host: ``\\rm`` runs ``rm``
    in bash wherever bash runs, and a reading that changed with the host would
    let an escaped name past a judge on one platform. A caller that places
    locations on a Windows host and wants ``\\`` kept as the path separator
    asks for it with ``backslash_escapes=backslash_escapes_here()``.
    """
    walk = _Walk(env, backslash_escapes, home_as_text)
    tree = _parse(command)
    if tree is None:
        walk.parse_error = True
        walk.refuse("parse", command[:40], "a null byte or an oversize command")
        return _effects(command, walk, statements=0)
    root = tree.root_node
    if root.has_error:
        walk.parse_error = True
    try:
        walk.visit(root, _Context())
        walk.flush()
    except RecursionError:
        # The nesting guard counts tree levels; a tree the parser built deeper
        # than the interpreter can walk is refused whole rather than crashing.
        walk.parse_error = True
        walk.refuse("parse", command.strip()[:40], "nested too deep to read")
    statements = sum(1 for child in root.named_children if child.type != "comment")
    if walk.parse_error and not any(entry.kind == "parse" for entry in walk.opaque):
        walk.refuse("parse", command.strip()[:40], "the command could not be parsed")
    if not walk.invocations and command.strip() and not walk.parse_error:
        walk.refuse("parse", command.strip()[:40], "the command runs nothing this fence can see")
    return _effects(command, walk, statements=statements)


def _effects(command: str, walk: _Walk, *, statements: int) -> ShellEffects:
    return ShellEffects(
        command,
        tuple(walk.invocations),
        tuple(walk.locations),
        tuple(walk.opaque),
        statements,
        walk.compound_structure,
        walk.parse_error,
    )
