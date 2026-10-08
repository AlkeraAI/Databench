"""What each program reads and writes among its own arguments.

The tables and readers here are the model's knowledge of individual programs:
which names are wrappers around another command, which run code the model
cannot see into, and, for the observers and copiers it does know, where their
operands and options point. A program in none of these tables is one whose
reach the model does not read, and :func:`program_effects` says so rather than
guessing "nowhere".

Every sentence in an :class:`Opaque` is phrased for the one consumer that shows
it to the model: the workspace fence, which quotes it when it cannot vouch for
a command. The wording is kept stable because the fence's explanation keys off
it (a destination that can be respelled, a value the environment lacks).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final, Literal

OpaqueKind = Literal[
    "parse",
    "wrapper",
    "shell",
    "interpreter",
    "program",
    "process",
    "assignment",
    "structure",
    "substitution",
    "variable",
    "expansion",
    "redirect",
    "flag",
    "network",
]


_OPTIONS_END: Final = "--"
_STDIN: Final = "-"


@dataclass(frozen=True, slots=True)
class Opaque:
    """One thing the model could not read, or read only by looking through."""

    kind: OpaqueKind
    text: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProgramEffects:
    """What one invocation does with its own arguments, before redirects."""

    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    opaque: tuple[Opaque, ...] = ()
    unnamed_write: bool = False
    """The program writes a file its arguments do not name: an output option
    with no value, an in-place edit with no file. A write all the same."""
    network: tuple[str, ...] | None = None
    """For a program whose job is to reach a host: every argument that is not
    an option, which is where its destinations are spelled."""


# --------------------------------------------------------------------------- #
# Names: wrappers, shells, interpreters
# --------------------------------------------------------------------------- #

#: Programs whose remaining argv IS the real command, once their own leading
#: options are dropped. The model reads through them and reports each one.
WRAPPERS: Final[frozenset[str]] = frozenset(
    {
        "sudo",
        "doas",
        "timeout",
        "nice",
        "nohup",
        "stdbuf",
        "setsid",
        "ionice",
        "chrt",
        "time",
        "command",
        "builtin",
        "exec",
        "xargs",
        "env",
        "watch",
        "proxychains",
        "proxychains4",
        "busybox",
        "toybox",
        "coproc",
    }
)

#: Shells: ``sh -c '<inner>'`` is re-read as its inner script; a bare one reads
#: a script or stdin the model cannot see. ``su`` runs its ``-c`` through the
#: user's shell the same way.
SHELLS: Final[frozenset[str]] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "su"})

#: Programs that run code from ``-c`` / ``-e`` / a script, not statically
#: analysable: their effect is unknown and their reach is unknown.
CODE_INTERPRETERS: Final[frozenset[str]] = frozenset(
    {"python", "python2", "python3", "node", "deno", "bun", "ruby", "perl", "php", "Rscript"}
)

#: Programs that hand the rest of the line to something the model cannot see
#: through, or whose own program language can open files: ``awk`` and ``sed``
#: scripts write, ``find -exec`` runs a command per match, ``ssh`` runs a
#: command elsewhere, ``source`` runs a script in this shell.
OPAQUE_PROGRAMS: Final[frozenset[str]] = frozenset(
    {"awk", "sed", "gsed", "find", "ssh", "scp", "rsh", "script", "source", ".", "csh", "tcsh"}
)

#: Wrapper options that consume the NEXT argument, per wrapper, and the long
#: forms that carry their value after ``=``. An option missing here costs a
#: wrong inner command only when it takes a value, which these do.
_WRAPPER_VALUE_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "sudo": frozenset(
        {
            "-u",
            "-g",
            "-p",
            "-C",
            "-D",
            "-h",
            "-r",
            "-t",
            "-U",
            "-T",
            "-R",
            "--user",
            "--group",
            "--prompt",
            "--close-from",
            "--chdir",
            "--host",
            "--role",
            "--type",
            "--other-user",
            "--command-timeout",
            "--chroot",
        }
    ),
    "doas": frozenset({"-u", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid"}),
    "chrt": frozenset({"-p", "--pid"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "watch": frozenset({"-n", "--interval", "-d", "--differences"}),
    "xargs": frozenset(
        {"-I", "-n", "-P", "-d", "-E", "-s", "-L", "-a", "-i", "-l", "--max-args", "--max-procs"}
    ),
    "proxychains": frozenset({"-f"}),
    "proxychains4": frozenset({"-f"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "exec": frozenset({"-a"}),
}

#: Wrapper options that turn the wrapper into a shell the model cannot read:
#: ``sudo -s`` / ``sudo -i`` run a login shell, ``env -S`` splits a string
#: into a command the model would have to re-lex itself.
_WRAPPER_SHELL_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "sudo": frozenset({"-s", "-i", "--shell", "--login"}),
    "doas": frozenset({"-s"}),
    "env": frozenset({"-S", "--split-string"}),
}


@dataclass(frozen=True, slots=True)
class StrippedWrapper:
    """A wrapper's own arguments taken off: the inner argv, or why it could not
    be read, and whether the inner command's arguments arrive at run time."""

    inner: tuple[str, ...]
    opaque: Opaque | None = None
    dynamic: bool = False
    chdir: str | None = None
    """The directory the inner command runs in when the wrapper moves it
    there (``env -C DIR``)."""
    environment: tuple[str, ...] = ()
    """The variables the wrapper sets for the inner command (``env NAME=v``)."""


def assignment_name(spelled: str) -> str:
    """The variable an assignment sets: ``LD_PRELOAD`` of ``LD_PRELOAD=/x.so``,
    ``PATH`` of ``PATH+=:/x``."""
    return spelled.split("=", 1)[0].removesuffix("+")


def strip_wrapper(name: str, args: tuple[str, ...]) -> StrippedWrapper:
    """``args`` with ``name``'s own options and their values dropped, so the
    inner command starts at the real program.

    ``nice -n 10``, ``timeout -s KILL 5``, ``ionice -c 3`` and ``watch -n 1``
    each take values before the command; a reader that only skipped the dashed
    tokens left the value standing as the "command" and the real one was never
    read. ``xargs`` builds its inner command's arguments from stdin, so they
    are dynamic.
    """
    values = _WRAPPER_VALUE_OPTIONS.get(name, frozenset())
    shells = _WRAPPER_SHELL_OPTIONS.get(name, frozenset())
    rest = list(args)
    chdir: str | None = None
    environment: list[str] = []
    if name == "env":
        while rest and "=" in rest[0] and not rest[0].startswith("-"):
            environment.append(assignment_name(rest.pop(0)))
    while rest and rest[0].startswith("-") and rest[0] != "-":
        option = rest.pop(0)
        if option == _OPTIONS_END:
            break
        head, separator, inline = option.partition("=")
        if option in shells or head in shells:
            return StrippedWrapper(
                (),
                Opaque(
                    "shell", f"{name} {option}", f"{name} carries a command this fence cannot read"
                ),
            )
        value: str | None = None
        if head in values:
            if separator:
                value = inline
            elif rest:
                value = rest.pop(0)
        if name == "env" and head in ("-C", "--chdir") and value is not None:
            chdir = value
        if name == "env":
            while rest and "=" in rest[0] and not rest[0].startswith("-"):
                environment.append(assignment_name(rest.pop(0)))
    if name == "timeout" and rest:
        rest.pop(0)  # the duration
    elif name == "chrt" and rest and _is_number(rest[0]):
        rest.pop(0)  # the priority
    return StrippedWrapper(
        tuple(rest), dynamic=name == "xargs", chdir=chdir, environment=tuple(environment)
    )


def _is_number(text: str) -> bool:
    return text.lstrip("-+").isdigit()


def shell_script(name: str, args: tuple[str, ...]) -> str | None:
    """The inline script a shell is handed, or ``None`` when it reads a file or
    stdin, or its script is not literal. ``args`` holds only the literal
    arguments, so ``sh -c "$CMD"`` is a ``-c`` with nothing after it."""
    del name
    for index, arg in enumerate(args):
        if arg == "-c" and index + 1 < len(args):
            return args[index + 1]
    return None


# --------------------------------------------------------------------------- #
# Observers and writers
# --------------------------------------------------------------------------- #

#: Pure observers: commands that create, truncate, move or link no file, so an
#: invocation of one writes only what its redirections (and, for the few that
#: take one, its output option) name. The ALLOW side of the fence: a command
#: outside every table here is refused rather than assumed harmless.
READERS: Final[frozenset[str]] = frozenset(
    {
        "[",
        "b2sum",
        "base64",
        "basename",
        "bat",
        "cat",
        "cd",
        "cksum",
        "cmp",
        "column",
        "comm",
        "cut",
        "date",
        "df",
        "diff",
        "dir",
        "dirname",
        "du",
        "echo",
        "egrep",
        "expand",
        "false",
        "fgrep",
        "file",
        "fold",
        "grep",
        "groups",
        "head",
        "hexdump",
        "hostname",
        "id",
        "jq",
        "less",
        "locale",
        "ls",
        "md5sum",
        "more",
        "nl",
        "nproc",
        "od",
        "paste",
        "pgrep",
        "printenv",
        "printf",
        "ps",
        "pwd",
        "readlink",
        "realpath",
        "rev",
        "rg",
        "seq",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "shasum",
        "sleep",
        "stat",
        "strings",
        "tac",
        "tail",
        "test",
        "tr",
        "tree",
        "true",
        "type",
        "uname",
        "unexpand",
        "uptime",
        "vdir",
        "wc",
        "whereis",
        "which",
        "whoami",
        "xxd",
    }
)

#: Observers whose operands are never locations: text, numbers, names of
#: programs. ``printenv`` prints the environment the box hands the command.
PATHLESS: Final[frozenset[str]] = frozenset(
    {
        "basename",
        "date",
        "dirname",
        "echo",
        "false",
        "groups",
        "hostname",
        "id",
        "locale",
        "nproc",
        "printenv",
        "printf",
        "pwd",
        "seq",
        "sleep",
        "tr",
        "true",
        "type",
        "uname",
        "uptime",
        "whereis",
        "which",
        "whoami",
    }
)

#: Observers of OTHER processes: they read ``/proc/<pid>/cmdline`` and, with
#: ``e``, ``/proc/<pid>/environ``, where the harness's own secrets live.
PROCESS_OBSERVERS: Final[frozenset[str]] = frozenset({"ps", "pgrep"})
PROCESS_TABLE: Final = "/proc"
#: What ``ps`` reads when asked for environments (BSD ``e``: ``ps e``, ``ps
#: eww``, ``ps axe``): every process's ``environ``, named as one location so a
#: fence that opens the process table to a sandbox can still keep the
#: environments shut.
PROCESS_ENVIRONS: Final = "/proc/all/environ"
#: Signalers: they touch no file and reach a process by pid or name. Where the
#: process table is the box's they are a question; a sandbox with a table of
#: its own places them (:class:`~alkera_cli.cloud.fence.SessionFence`).
PROCESS_SIGNALERS: Final[frozenset[str]] = frozenset({"kill", "pkill", "killall"})


def _ps_shows_environments(args: Sequence[str]) -> bool:
    """Whether a ``ps`` invocation prints environments: a BSD-style option
    word (no dash) carrying ``e``. UNIX ``-e`` selects every process and
    shows nothing of their environments."""
    return any(arg and not arg.startswith("-") and "e" in arg for arg in args)


#: Where a copying command's operands start writing: ``"last"`` writes its
#: final operand, ``"operands"`` all of them, ``"of"`` the ``of=`` operand,
#: ``"second"`` its second positional when it has one (``uniq IN OUT``).
WRITERS: Final[Mapping[str, str]] = {
    "cp": "last",
    "mv": "last",
    "install": "last",
    "rsync": "last",
    "tee": "operands",
    "dd": "of",
    "truncate": "last",
    "uniq": "second",
    "xxd": "second",
}

#: Commands that name a destination behind an option. The value is read as
#: ``-o out`` / ``--output=out`` / the tail of a short cluster (``-sSo out``).
FLAG_WRITERS: Final[Mapping[str, frozenset[str]]] = {
    "curl": frozenset(
        {
            "-o",
            "--output",
            "--output-dir",
            "-D",
            "--dump-header",
            "-c",
            "--cookie-jar",
            "--trace",
            "--trace-ascii",
            "--stderr",
            "--libcurl",
        }
    ),
    "sort": frozenset({"-o", "--output"}),
    "tree": frozenset({"-o"}),
    "less": frozenset({"-o", "-O", "--log-file", "--LOG-FILE"}),
    "wget": frozenset(
        {"-O", "--output-document", "-P", "--directory-prefix", "-a", "--append-output"}
    ),
}

#: Options that name a destination the model cannot compute: ``curl -O`` writes
#: the URL's own basename, a name only the server's answer settles.
OPAQUE_FLAGS: Final[Mapping[str, frozenset[str]]] = {
    "curl": frozenset({"-O", "--remote-name", "-J", "--remote-header-name"}),
}

#: Observers that write a file through an option the shell never sees as a
#: redirect: ``sort -o F``, ``uniq IN OUT``, ``yq -i``, ``xmllint -o F``,
#: ``xxd IN OUT``, ``tree -o F``. Their output form is a write; the plain
#: form a read. Read the way getopt reads them: a long option matches by any
#: unambiguous prefix (``sort --outp=F`` writes), and a short cluster is walked
#: so ``sort -uo F`` is an output file while ``sort -to`` is ``-t``'s value.
OUTPUT_OPTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "sort": ("-o", "--output"),
    "tree": ("-o", "--output"),
    "xmllint": ("-o", "--output"),
}
#: The other value-taking SHORT letters of each output-option command: the
#: first of these in a cluster swallows the rest of it as its value.
_OUTPUT_BUNDLE_LETTERS: Final[Mapping[str, str]] = {"sort": "ktST", "tree": "LPIH", "xmllint": ""}
#: In-place editors: ``-i`` / ``--inplace`` / ``--in-place`` rewrite the file
#: positionals in place.
INPLACE_EDITORS: Final[frozenset[str]] = frozenset({"yq", "jq"})

#: Programs the model knows a WRITE shape of without knowing their whole
#: reach: ``yq`` and ``xmllint`` take many more options than the output ones
#: above, and ``wget`` writes a server-named file when ``-O`` is absent. Their
#: writes are reported AND the program is reported as one the fence cannot
#: read, so a consumer that needs the whole reach does not take the part for it.
PARTIAL: Final[frozenset[str]] = frozenset({"yq", "xmllint", "wget"})

#: Options whose NEXT token is a value that is not a location (``cut -d /``,
#: ``head -n 5``). An option missing here costs a false refusal: its value is
#: then judged as the location it might be.
VALUE_FLAGS: Final[Mapping[str, frozenset[str]]] = {
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields"}),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "grep": frozenset({"-e", "-m", "-A", "-B", "-C", "--regexp", "--max-count"}),
    "egrep": frozenset({"-e", "-m", "-A", "-B", "-C", "--regexp", "--max-count"}),
    "fgrep": frozenset({"-e", "-m", "-A", "-B", "-C", "--regexp", "--max-count"}),
    "rg": frozenset({"-e", "-m", "-A", "-B", "-C", "-g", "-t", "-T", "--regexp", "--glob"}),
    "sort": frozenset({"-k", "-t", "-S", "--key", "--field-separator"}),
    "fold": frozenset({"-w", "--width"}),
    "column": frozenset({"-s", "-c", "-o"}),
    "od": frozenset({"-A", "-t", "-N", "-j", "-w"}),
    "xxd": frozenset({"-l", "-s", "-c", "-g"}),
    "stat": frozenset({"-c", "--format", "--printf"}),
    "du": frozenset({"-d", "--max-depth"}),
    "tree": frozenset({"-L", "-I", "-P"}),
    "truncate": frozenset({"-s", "--size"}),
    "jq": frozenset({"--indent"}),
    "uniq": frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}),
    "curl": frozenset(
        {
            "-H",
            "--header",
            "-X",
            "--request",
            "-A",
            "--user-agent",
            "-e",
            "--referer",
            "-m",
            "--max-time",
            "--connect-timeout",
            "--retry",
            "-w",
            "--write-out",
        }
    ),
}
#: ``xxd``'s value options as its WRITE reader counts positionals: ``-o`` is a
#: display offset here, not an output file, which is why ``xxd`` is read by
#: positional count instead of by ``-o``.
_XXD_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-c", "-g", "-l", "-o", "-s", "-cols", "-groupsize", "-len", "-seek"}
)

#: Options naming the pattern itself, after which no bare operand is one.
_PATTERN_FLAGS: Final[frozenset[str]] = frozenset({"-e", "-f", "--regexp", "--file"})
#: Commands whose first bare operand is a pattern or a filter, not a location.
PATTERN_FIRST: Final[frozenset[str]] = frozenset({"grep", "egrep", "fgrep", "rg", "jq", "yq"})
#: Options after which a pattern-first command takes NO pattern (``rg --files DIR``).
_NO_PATTERN_FLAGS: Final[frozenset[str]] = frozenset({"--files", "--type-list"})

#: Options that name a PROGRAM the command runs: ``rg --pre helper`` runs
#: ``helper`` over every file. An observer with one of these is a wrapper.
PROGRAM_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "rg": frozenset({"--pre"}),
    "sort": frozenset({"--compress-program"}),
    "install": frozenset({"--strip-program"}),
    "bat": frozenset({"--pager"}),
    "less": frozenset({"--pager"}),
    "rsync": frozenset({"-e", "--rsh", "--rsync-path"}),
    "tree": frozenset({"--fromfile"}),
}
#: Beside the table: any long option whose name says it takes a program, a
#: command, a pager or an exec is one, whatever the binary.
_PROGRAM_OPTION_SUFFIXES: Final[tuple[str, ...]] = (
    "-program",
    "-command",
    "pager",
    "-exec",
    "-execdir",
    "-pre",
    "-rsh",
)

#: Commands that take GNU's ``-t DIR`` / ``--target-directory=DIR``: the
#: destination comes first, as an option's argument, and every operand is a
#: source.
TARGET_DIRECTORY_COMMANDS: Final[frozenset[str]] = frozenset({"cp", "mv", "install"})
_TARGET_DIRECTORY_LONG: Final = "--target-directory"

#: Programs whose whole job is to contact a host: a fetch, a lookup, a probe.
#: Their non-option arguments are where the destinations are spelled; which of
#: those count as this machine is the consumer's policy.
NETWORK_PROGRAMS: Final[frozenset[str]] = frozenset(
    {
        "curl",
        "wget",
        "dig",
        "nslookup",
        "host",
        "ping",
        "ping6",
        "traceroute",
        "traceroute6",
        "tracepath",
    }
)

#: Every program the model has a reading of; the rest are opaque.
KNOWN: Final[frozenset[str]] = (
    READERS
    | PATHLESS
    | PROCESS_OBSERVERS
    | frozenset(WRITERS)
    | frozenset(FLAG_WRITERS)
    | frozenset(OUTPUT_OPTIONS)
    | INPLACE_EDITORS
)


def _names_a_program(name: str, option: str) -> bool:
    """Whether ``option`` (its name, before any ``=``) hands ``name`` a program."""
    if option in PROGRAM_OPTIONS.get(name, frozenset()):
        return True
    lowered = option.lower()
    return lowered.startswith("--") and (
        lowered.endswith(_PROGRAM_OPTION_SUFFIXES) or "exec" in lowered
    )


# --------------------------------------------------------------------------- #
# Per-program readers
# --------------------------------------------------------------------------- #


@dataclass
class _Found:
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    opaque: list[Opaque] = field(default_factory=list)
    unnamed_write: bool = False

    def refuse(self, kind: OpaqueKind, text: str, reason: str) -> None:
        entry = Opaque(kind, text, reason)
        if entry not in self.opaque:
            self.opaque.append(entry)

    def done(self, *, network: tuple[str, ...] | None = None) -> ProgramEffects:
        return ProgramEffects(
            tuple(self.reads), tuple(self.writes), tuple(self.opaque), self.unnamed_write, network
        )


def program_effects(name: str, args: tuple[str, ...]) -> ProgramEffects:
    """What ``name`` reads and writes among ``args``, or why it cannot be read.

    ``name`` is the program as resolved (a path-qualified command is judged by
    its basename, so ``/bin/rm`` is ``rm``). ``args`` are the literal
    arguments; an argument the shell computes is not among them and the caller
    has already reported it.
    """
    program = PurePosixPath(name).name or name
    found = _Found()
    network = (
        tuple(a for a in args if not a.startswith("-")) if program in NETWORK_PROGRAMS else None
    )
    if program in CODE_INTERPRETERS or program in OPAQUE_PROGRAMS:
        found.refuse("interpreter", program, f"{program} carries a command this fence cannot read")
        return found.done(network=network)
    if program in PROCESS_SIGNALERS:
        found.refuse("process", program, f"{program} signals a process in the box's process table")
        return found.done(network=network)
    if program not in KNOWN:
        found.refuse(
            "program", program, f"{program} is a program whose reach this fence cannot read"
        )
        return found.done(network=network)
    if program in PARTIAL:
        found.refuse(
            "program", program, f"{program} is a program whose reach this fence cannot read"
        )
    for token in args:
        if token == _OPTIONS_END:
            break
        if token.startswith("-") and _names_a_program(program, token.partition("=")[0]):
            found.refuse(
                "program",
                f"{program} {token}",
                f"{program} {token} runs a program this fence cannot read",
            )
    if program in PROCESS_OBSERVERS:
        found.reads.append(PROCESS_TABLE)
        if program == "ps" and _ps_shows_environments(args):
            found.reads.append(PROCESS_ENVIRONS)
        return found.done(network=network)
    if program in PATHLESS:
        return found.done(network=network)
    if program == "dd":
        found.reads.extend(t.split("=", 1)[1] for t in args if t.startswith("if="))
        found.writes.extend(t.split("=", 1)[1] for t in args if t.startswith("of="))
        return found.done()
    if program == "curl":
        _curl(args, found)
        return found.done(network=network)
    if program == "wget":
        _wget(args, found)
        return found.done(network=network)
    if program != "tee":  # tee reads stdin
        _reads(program, args, found)
    _writes(program, args, found)
    if program in OUTPUT_OPTIONS:
        _output_option(program, args, found)
    if program in INPLACE_EDITORS:
        _inplace(program, args, found)
    if program == "uniq" or program == "xxd":
        _second_positional(program, args, found)
    return found.done(network=network)


def _reads(name: str, arguments: tuple[str, ...], found: _Found) -> None:
    """The locations one command READS among its own operands: every bare
    operand of an observer, judged as the location it might be."""
    reads: list[str] = []
    value_flags = VALUE_FLAGS.get(name, frozenset())
    output_flags = FLAG_WRITERS.get(name, frozenset())
    target_flags = (
        frozenset({"-t", _TARGET_DIRECTORY_LONG})
        if name in TARGET_DIRECTORY_COMMANDS
        else frozenset()
    )
    bare: list[str] = []
    pattern_named = name in PATTERN_FIRST and any(token in _NO_PATTERN_FLAGS for token in arguments)
    into_target = False
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == _OPTIONS_END:
            bare.extend(arguments[index:])
            break
        if not token.startswith("-") or token == _STDIN:
            bare.append(token)
            continue
        head, separator, tail = token.partition("=")
        if token in _PATTERN_FLAGS or head in _PATTERN_FLAGS or token[:2] in _PATTERN_FLAGS:
            # ``-e PAT`` / ``-f FILE`` / ``--file=FILE`` / ``-fFILE``: the pattern is
            # named, so every bare operand is a file.
            pattern_named = True
        if separator:
            into_target = into_target or head == _TARGET_DIRECTORY_LONG
            if head not in value_flags | output_flags | target_flags and tail:
                reads.append(tail)
            continue
        if token in value_flags | output_flags | target_flags:
            into_target = into_target or token in target_flags
            index += 1
            continue
        if token.startswith("--") or len(token) <= 2 or token[:2] in value_flags:
            continue
        if token[-1] == "t" and target_flags:
            into_target = True
            index += 1
        elif any(c in token for c in "/~"):
            # A value glued to its short option (``-f/etc/shadow``).
            reads.append(token[2:])
    if name in PATTERN_FIRST and not pattern_named and bare:
        bare = bare[1:]
    if name == "rsync" and any(":" in token for token in bare):
        # A ``host:path`` operand, source or destination, is a remote.
        found.refuse("network", "rsync", "rsync names a remote this fence cannot read")
    if WRITERS.get(name) == "last" and not into_target:
        # The destination is a write; every operand before it is a source.
        bare = bare[:-1]
    found.reads.extend([*reads, *bare])


def _writes(name: str, arguments: tuple[str, ...], found: _Found) -> None:
    """The destinations a writing command names among its operands or behind
    an output option."""
    flags = FLAG_WRITERS.get(name)
    if flags is not None:
        targets = _flag_targets(name, arguments, flags, found)
        # For a program read the getopt way below, the cluster reader keeps only
        # its refusals: it would take ``sort -to`` for an output flag.
        if name not in OUTPUT_OPTIONS:
            found.writes.extend(targets)
    how = WRITERS.get(name)
    if how is None or how in ("of", "second"):
        return
    target_dir, rest = _split_operands(name, arguments, found)
    if target_dir is not None:
        # ``cp -t DIR a b`` writes every operand INTO ``DIR``.
        if not rest:
            found.refuse("flag", name, f"{name} names nothing to put in {target_dir}")
        found.writes.append(target_dir)
        if name == "mv":
            found.writes.extend(rest)
        return
    if how == "operands":
        found.writes.extend(rest)
        return
    if len(rest) < 2:
        # ``cp x`` with no destination is not a command that runs; refusing it
        # is both correct and the safe side of the guess.
        found.refuse("flag", name, f"{name} names no destination")
        return
    found.writes.append(rest[-1])
    if name == "mv":
        # A move takes its sources away: each is a write where it stood.
        found.writes.extend(rest[:-1])


def _flag_targets(
    name: str, arguments: tuple[str, ...], flags: frozenset[str], found: _Found
) -> list[str]:
    """The destinations a command names behind one of ``flags``."""
    opaque = OPAQUE_FLAGS.get(name, frozenset())
    letters = {flag[1] for flag in flags if len(flag) == 2 and flag.startswith("-")}
    opaque_letters = {flag[1] for flag in opaque if len(flag) == 2 and flag.startswith("-")}
    targets: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == _OPTIONS_END:
            break
        if token in opaque:
            found.refuse(
                "flag", f"{name} {token}", f"{name} {token} names a destination it decides later"
            )
            continue
        if token in flags:
            if index >= len(arguments):
                found.refuse("flag", f"{name} {token}", f"{name} {token} names no destination")
                continue
            targets.append(arguments[index])
            index += 1
            continue
        head, separator, tail = token.partition("=")
        if separator and head in flags:
            targets.append(tail)
            continue
        if not token.startswith("-") or token.startswith("--"):
            continue
        # A short cluster (``curl -sSo out``): the destination follows only when
        # its letter is the last of the cluster, and any other spelling of it is
        # a destination this reader cannot place.
        cluster = token[1:]
        if opaque_letters & set(cluster):
            found.refuse(
                "flag", f"{name} {token}", f"{name} {token} names a destination it decides later"
            )
            continue
        if not letters & set(cluster):
            continue
        if cluster[-1] not in letters:
            found.refuse("flag", f"{name} {token}", f"{name} {token} hides where it writes")
            continue
        if index >= len(arguments):
            found.refuse("flag", f"{name} {token}", f"{name} {token} names no destination")
            continue
        targets.append(arguments[index])
        index += 1
    return targets


def _split_operands(
    name: str, arguments: tuple[str, ...], found: _Found
) -> tuple[str | None, list[str]]:
    """``(target_directory, operands)`` of one command's arguments. ``--`` ends
    the options; a short cluster carrying ``t`` anywhere but its end makes
    GNU's parser read the rest of the cluster as the directory, and that
    spelling is refused rather than guessed at."""
    takes_target = name in TARGET_DIRECTORY_COMMANDS
    target: str | None = None
    rest: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == _OPTIONS_END:
            rest.extend(arguments[index:])
            break
        if not token.startswith("-") or token == _STDIN:
            rest.append(token)
            continue
        if not takes_target:
            continue
        if token.startswith(_TARGET_DIRECTORY_LONG + "="):
            target = token.split("=", 1)[1]
        elif token == _TARGET_DIRECTORY_LONG or (not token.startswith("--") and token[-1] == "t"):
            if index >= len(arguments):
                found.refuse("flag", f"{name} {token}", f"{name} {token} names no directory")
                continue
            target = arguments[index]
            index += 1
        elif not token.startswith("--") and "t" in token[1:]:
            found.refuse("flag", f"{name} {token}", f"{name} {token} hides where it writes")
    return target, rest


def _option_value(args: tuple[str, ...], *flags: str, bundled: str = "") -> str | None:
    """The value of the first matching option (``-o V`` / ``-oV`` / ``-uo V`` /
    ``--output V`` / ``--output=V`` / ``--outp=V``), or ``None`` when absent. A
    present-but-empty option returns ``""``: still present, so a trailing
    ``sort -o`` is a write to a file it did not name.

    A long option matches by prefix, which is getopt_long's own rule.
    ``bundled`` names the command's OTHER value-taking short letters so a
    cluster is walked the way getopt does: in ``sort -uo out`` the ``o`` is the
    output flag, in ``sort -to`` it is ``-t``'s value."""
    short = {f[1] for f in flags if len(f) == 2 and f.startswith("-") and not f.startswith("--")}
    longs = [f for f in flags if f.startswith("--")]
    for index, token in enumerate(args):
        following = args[index + 1] if index + 1 < len(args) else ""
        if token.startswith("--"):
            name, sep, inline = token.partition("=")
            if len(name) > 2 and any(f.startswith(name) for f in longs):
                return inline if sep else following
            continue
        if not token.startswith("-") or token == _STDIN:
            continue
        for position, char in enumerate(token[1:], start=1):
            if char in short:
                return token[position + 1 :] or following
            if char in bundled:
                break
    return None


def _output_option(name: str, args: tuple[str, ...], found: _Found) -> None:
    value = _option_value(args, *OUTPUT_OPTIONS[name], bundled=_OUTPUT_BUNDLE_LETTERS[name])
    if value is None:
        return
    if value:
        if value not in found.writes:
            found.writes.append(value)
    else:
        found.unnamed_write = True
        found.refuse("flag", f"{name} -o", f"{name} -o names no destination")


def _has_short_flag(args: tuple[str, ...], letter: str) -> bool:
    """A short flag present alone (``-i``) or bundled (``-iP``)."""
    return any(
        token.startswith("-") and not token.startswith("--") and letter in token[1:]
        for token in args
    )


def _inplace(name: str, args: tuple[str, ...], found: _Found) -> None:
    if not (
        _has_short_flag(args, "i") or any(a.startswith(("--inplace", "--in-place")) for a in args)
    ):
        return
    files = _positionals(name, args)
    if name in PATTERN_FIRST and files:
        files = files[1:]
    if files:
        found.writes.extend(f for f in files if f not in found.writes)
    else:
        found.unnamed_write = True
        found.refuse("flag", f"{name} -i", f"{name} -i names no file to edit")


def _positionals(name: str, args: tuple[str, ...]) -> list[str]:
    """Non-option arguments, skipping the value of a known value-taking option.
    A bare ``-`` (stdin) counts as a positional, so ``uniq - out`` still writes."""
    value_flags = _XXD_VALUE_FLAGS if name == "xxd" else VALUE_FLAGS.get(name, frozenset())
    out: list[str] = []
    skip = False
    for index, token in enumerate(args):
        if skip:
            skip = False
            continue
        if token == _OPTIONS_END:
            out.extend(args[index + 1 :])
            break
        if token.startswith("-") and token != _STDIN:
            if token in value_flags:
                skip = True
            continue
        out.append(token)
    return out


def _second_positional(name: str, args: tuple[str, ...], found: _Found) -> None:
    positionals = _positionals(name, args)
    if len(positionals) > 1 and positionals[1] not in found.writes:
        found.writes.append(positionals[1])


# --------------------------------------------------------------------------- #
# curl
# --------------------------------------------------------------------------- #

#: The schemes ``curl`` fetches over the network; every other scheme names
#: something on this box (``file:``) or something this reader cannot place.
_REMOTE_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_CURL_FILE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "-T",
        "--upload-file",
        "-K",
        "--config",
        "-b",
        "--cookie",
        "-E",
        "--cert",
        "--key",
        "--cacert",
        "--netrc-file",
    }
)
_CURL_BODY_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "-d",
        "--data",
        "--data-binary",
        "--data-raw",
        "--data-ascii",
        "--data-urlencode",
        "-F",
        "--form",
        "--json",
    }
)
_CURL_URL_FLAGS: Final[frozenset[str]] = frozenset({"--url"})


def _url_read(value: str, found: _Found) -> str | None:
    """The local path a ``curl`` URL operand reads, ``None`` for a remote one."""
    scheme, separator, rest = value.partition(":")
    if not separator or "/" in scheme or not scheme.isalpha():
        # No scheme: curl assumes http, but a bare name is judged as the path it
        # might be, which errs toward refusing.
        return value
    lowered = scheme.lower()
    if lowered in _REMOTE_SCHEMES:
        return None
    if lowered != "file":
        found.refuse("network", value, f"curl {scheme}: is a scheme this fence cannot read")
        return None
    return _file_url_path(rest, found)


def _file_url_path(rest: str, found: _Found) -> str | None:
    """The local file a ``file:`` URL names. ``file:/p``, ``file:///p`` and
    ``file://localhost/p`` all name one file, and so do the three spellings a
    drive brings: a drive letter where the authority sits is a path, not a
    host."""
    if not rest.startswith("//"):
        return rest
    body = rest[2:]
    if _drive_rooted(body):
        return body
    host, _slash, path = body.partition("/")
    if host not in ("", "localhost"):
        found.refuse(
            "network", f"file://{host}", f"curl file://{host} names a host this fence cannot read"
        )
        return None
    return path if _drive_rooted(path) else "/" + path


def _drive_rooted(text: str) -> bool:
    return len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] in "\\/"


#: ``wget`` options whose value is a local file it reads: a body it posts, a
#: list of URLs, cookies and credentials it sends, a config it loads.
_WGET_FILE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "-i",
        "--input-file",
        "--post-file",
        "--body-file",
        "--load-cookies",
        "--certificate",
        "--private-key",
        "--ca-certificate",
        "--config",
    }
)


def _wget(arguments: tuple[str, ...], found: _Found) -> None:
    """``wget``: the files it sends or configures itself from, and the
    destinations its output options name. Its operands are URLs, not files,
    and without ``-O`` it writes a server-named file, which is why the program
    stays partly known."""
    found.writes.extend(_flag_targets("wget", arguments, FLAG_WRITERS["wget"], found))
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == _OPTIONS_END:
            break
        head, separator, tail = token.partition("=")
        if separator and head in _WGET_FILE_FLAGS:
            found.reads.append(tail)
        elif token in _WGET_FILE_FLAGS:
            if index >= len(arguments):
                found.refuse("flag", f"wget {token}", f"wget {token} names nothing")
                continue
            found.reads.append(arguments[index])
            index += 1


def _curl(arguments: tuple[str, ...], found: _Found) -> None:
    """``curl``: the files it sends, configures itself from or fetches
    (``file://``), and the destinations its output options name."""
    found.writes.extend(_flag_targets("curl", arguments, FLAG_WRITERS["curl"], found))
    value_flags = VALUE_FLAGS["curl"] | FLAG_WRITERS["curl"]
    named = _CURL_FILE_FLAGS | _CURL_BODY_FLAGS | _CURL_URL_FLAGS
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == _OPTIONS_END:
            for rest in arguments[index:]:
                read = _url_read(rest, found)
                if read is not None:
                    found.reads.append(read)
            break
        head, separator, tail = token.partition("=")
        if separator and head in named:
            flag, value = head, tail
        elif token in named:
            if index >= len(arguments):
                found.refuse("flag", f"curl {token}", f"curl {token} names nothing")
                continue
            flag, value = token, arguments[index]
            index += 1
        elif token in value_flags:
            index += 1
            continue
        elif token.startswith("-") and token != _STDIN:
            continue
        else:
            read = _url_read(token, found)
            if read is not None:
                found.reads.append(read)
            continue
        if flag in _CURL_FILE_FLAGS:
            found.reads.append(value)
        elif flag in _CURL_URL_FLAGS:
            read = _url_read(value, found)
            if read is not None:
                found.reads.append(read)
        elif "@" in value or "<" in value:
            found.reads.append(value.split("@", 1)[-1].split("<", 1)[-1].split(";", 1)[0])
