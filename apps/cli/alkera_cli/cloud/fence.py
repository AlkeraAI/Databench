"""The workspace fence: what a cloud session may read, and where it may write.

A cloud session runs on a box that also holds things that are not the
customer's project — the operator's gateway token in ``ALKERA_HOME/auth.yml``,
the box's own environment file, the rest of the filesystem, and every OTHER
chat's folder. Two bounds follow from that, and they are not the same bound:

* the READ fence is the workspace — every location a tool call names must
  resolve inside the project root, and never inside ``ALKERA_HOME`` (even when
  that directory was provisioned under the project);
* the WRITE fence is narrower and is this chat's own folder. A web chat's
  durable state is that folder and nothing else, so a write anywhere else —
  the project root included, another chat's folder especially — is refused in
  every permission mode. The mode decides whether a write inside the folder
  prompts or auto-allows; it never moves the boundary.

The write fence has to read shell commands, because a redirect is a write that
carries no path argument: ``echo x > /etc/hosts`` names its destination in the
command text, and a fence that only looked at an ``fs`` tool's ``file_path``
would let it straight through. The shell-effect model
(:func:`alkera_cli.plugins.plugin_base.permissions.shell.analyze_shell`, the
same reader the permission classifier uses) is what reads them: every location
a command reads or writes, with the ``cd`` chain in force, and everything it
could not resolve or could only look through. A command the model cannot vouch
for whole (a wrapper, a nested shell, an interpreter, a program in no table, a
destination the shell computes) is never allowed on the model's word alone:
the locations it did read are judged first, so an escape is refused whatever
else is in the command, and what remains is a question for a person, because a
fence that guesses in the permissive direction is not one.

This module is the pure decision. It takes a tool call — the tool's name and
its arguments, or the permission ask the harness raised for it — and answers
with the first location that escapes, or ``None``. It opens no file: deciding
that a path may not be read is a path computation (``~`` expansion, ``..``
normalisation, symlink resolution, the literal prefix of a glob, and — for a
relative name a harness may have spelled against a different root — whether
that name exists at all), and the token file is never read to learn that it is
off limits.

Where the paths come from depends on the tool. ``read`` / ``edit`` name a file
under ``file_path`` (or ``filePath``); ``glob`` and ``grep`` walk a directory
under ``path`` and take a pattern — a filename glob for ``glob``, a regular
expression for ``grep`` (so ``/api/v1/chats`` there is a thing to search for,
not a place, and is not judged as one); ``grep``'s ``include`` is a filename
glob; a shell tool has a ``cwd``. A permission ask carries the same locations
in its typed subject (``raw`` and the file ``targets``) and, for ``glob``, in
its ``patterns``.

A glob is judged by its literal prefix — the segments before the first
wildcard — which must be inside the root, and by its segments: a ``..``
anywhere in a pattern is refused outright, because ``*/../../etc`` climbs out
of the root only after the wildcard has matched, which no prefix check can
see. A plain path's ``..`` is resolved (``src/../README.md`` is inside).
Anything that cannot be resolved at all is treated as outside.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from glob import iglob
from pathlib import Path, PurePosixPath
from typing import Any

from alkera_core.chat_records import CHAT_RECORD_NAMES as SHARED_CHAT_RECORD_NAMES
from alkera_core.schemas.chat import PermissionRequest

from alkera_cli.plugins.plugin_base.permissions.shell import (
    PROCESS_TABLE,
    analyze_shell,
    backslash_escapes_here,
)

#: Argument keys under which a tool names a filesystem LOCATION — a file or a
#: directory — whatever the tool. Each vendored tool has its own spelling.
#: ``cwd`` comes first: it is the base the other relative locations resolve
#: against, so it is judged before them.
LOCATION_KEYS: tuple[str, ...] = (
    "cwd",
    "file_path",
    "filePath",
    "filepath",
    "path",
    "directory",
    "dir",
    "parentDir",
)

#: Keys that carry a filename GLOB, per tool — and only where they are one.
#: ``grep``'s ``pattern`` is a regular expression and is deliberately absent.
GLOB_KEYS: Mapping[str, tuple[str, ...]] = {
    "glob": ("pattern",),
    "grep": ("include",),
}

#: Tool / permission kinds whose text is a shell command, not a location. Its
#: ``cwd`` is a location; the command's own operands are read by the shell
#: model and judged by :class:`SessionFence`, because a shell READ
#: reaches the box's token and the other chats exactly as a file tool would.
SHELL_KINDS: frozenset[str] = frozenset({"bash", "shell"})

#: The daemon's own per-workspace state directory name (``<root>/.alkera``). It
#: holds other chats' transcripts, file-custody connector credentials, and the
#: team-connections manifest — none of it the customer's project — so the fence
#: refuses it even though it sits under the root, EXCEPT the session's own sandbox
#: (where plan/scratch files live), which is passed in explicitly.
ALKERA_STATE_DIRNAME = ".alkera"

_WILDCARDS = frozenset("*?[")

#: What separates one shell word from the next when the text is read with no
#: parse: whitespace, quotes, and every operator the shell gives meaning to.
_WORD_BREAK = re.compile(r"[\s'\"`;|&<>(),]+")
#: ``$NAME`` / ``${NAME}``, expanded from the command's own environment.
_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
#: A ``NAME=value`` assignment written IN the command (leading, or after a
#: separator / subshell open). Used only to expand a variable a later token
#: reassembles a floor path from — ``p=/pro; cat ${p}c/self/environ``.
_ASSIGNMENT = re.compile(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=([^\s;&|()]+)")
#: Shell quote characters, dropped before the raw-text floor scan so an empty or
#: adjacent quote pair (``alker""a-home``, ``"$p"/"name"``) does not split a floor
#: path into fragments the scan can no longer recognise.
_SHELL_QUOTE_CHARS = {ord(q): None for q in "'\"`"}


def _join_quoted_fragments(command: str) -> str:
    """``command`` with shell quote marks removed, so a floor path the shell would
    join across empty or adjacent quotes reads as the one word it becomes. Only the
    quotes go — whitespace and operators still separate words, so a quoted path with
    an internal space simply splits (the floor set has no such names)."""
    return command.translate(_SHELL_QUOTE_CHARS)


def _command_assignments(command: str) -> dict[str, str]:
    """Every ``NAME=value`` written in ``command``, the last spelling of a name
    winning. The text is not parsed, so this cannot tell an assignment from one
    spelled in an ``echo`` or a comment, nor which side of a read it runs on. The
    floor scan therefore checks a token under this map AS WELL AS under the
    environment alone, never instead of it."""
    return {match.group(1): match.group(2) for match in _ASSIGNMENT.finditer(command)}


def _floor_scan_readings(
    command: str, env: Mapping[str, str] | None
) -> Iterator[tuple[str, Mapping[str, str]]]:
    """Each ``(text, variables)`` reading the floor scan checks a command under.

    The command as written and with its quote fragments joined, each expanded from
    the environment alone and from the environment overlaid with the command's own
    assignments. A token is on the floor if ANY reading puts it there, so a
    reading added here can only turn an ``unknown`` into an ``escape``: an
    assignment the text happens to spell (``HOME=/tmp`` after the read, in a
    comment, inside an ``echo``) cannot hide what the environment's value names."""
    base = dict(env or {})
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for text in (command, _join_quoted_fragments(command)):
        for variables in (base, {**base, **_command_assignments(text)}):
            key = (text, tuple(sorted(variables.items())))
            if key not in seen:
                seen.add(key)
                yield text, variables


def _path_like_tokens(command: str) -> Iterator[str]:
    """Every word of ``command`` that could be a location — it holds a ``/``,
    starts at a home (``~``), or is ``..`` — with an option's ``name=`` prefix
    dropped, so ``--file=/proc/self/environ`` names what it names."""
    for word in _WORD_BREAK.split(command):
        if not word:
            continue
        if "=" in word and not word.startswith(("/", "~", ".")):
            word = word.split("=", 1)[1]
        if "/" in word or word.startswith("~") or word == "..":
            yield word


def default_home() -> Path:
    """The ``ALKERA_HOME`` the fence protects, read at call time so an
    override (a custom install, a test) is honoured after import."""
    from alkera_cli.host import paths

    return paths.ALKERA_HOME


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


#: The ``/proc`` entries that name whichever process reads them.
_OWN_ENTRY_NAMES = ("self", "thread-self")
_OWN_PROCESS_ENTRIES = tuple(("proc", name) for name in _OWN_ENTRY_NAMES)


def _resolve(text: str, *, base: Path) -> Path | None:
    """``text`` as an absolute, symlink-free path — relative to ``base`` when it
    is relative — or ``None`` when it cannot be resolved at all."""
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        if path.parts[1:3] in _OWN_PROCESS_ENTRIES:
            # ``/proc/self`` is a link the kernel answers per reader: resolved
            # here it names the judging daemon's entry, not the command's own.
            return Path(os.path.normpath(path))
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


#: A spelling that names a drive and is anchored on it: ``C:\tools``,
#: ``c:/tools``. A drive letter with no separator behind it — ``C:tools`` —
#: names that drive's own working directory, which is not somewhere anything
#: can be bounded by.
_DRIVE_ROOTED = re.compile(r"^([A-Za-z]):[\\/]")


def _root_parts(text: str) -> tuple[str, tuple[str, ...]] | None:
    """``(drive, segments)`` for a spelling anchored at a filesystem root, or
    ``None`` for one anchored at nothing.

    Read the same way whatever platform is reading it, because the box a cloud
    chat runs on is not the box its settings were written on, and a reader that
    knew only one dialect dropped the other's roots on the floor: on Windows a
    rooted-but-driveless ``/usr`` is not an absolute path, so a box there ended
    up with no system read roots at all. The drive letter is case-folded
    because a drive is, and ``.`` / ``..`` are folded lexically — nothing below
    opens a file, so the answer is the same on a box that holds the path and
    one that does not.

    Which dialect a spelling is in is read from the spelling only where the
    spelling settles it. A drive letter does: ``C:\\tools`` names one place and
    nothing else, on any box. A LEADING BACKSLASH does not — on Windows it
    anchors at the current drive's root, but on POSIX it is the first character
    of an ordinary relative filename, one the agent can create in its own
    working directory. Reading it as an anchor off Windows let a name the agent
    could write be read as a location under a system root and skip being joined
    to the working directory at all, which is a spelling that walks through the
    read fence rather than a dialect being understood. So off Windows a leading
    backslash is part of the name, exactly as a backslash inside one is.
    """
    stripped = text.strip()
    slashed = stripped.replace("\\", "/")
    windows = _DRIVE_ROOTED.match(slashed) is not None or (
        os.name == "nt" and stripped.startswith("\\")
    )
    folded = slashed if windows or os.name == "nt" else stripped
    drive = _DRIVE_ROOTED.match(folded)
    if drive is not None:
        anchor, rest = drive.group(1).lower(), folded[2:]
    elif folded.startswith("/"):
        anchor, rest = "", folded
    else:
        return None
    segments: list[str] = []
    for segment in rest.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return anchor, tuple(segments)


def read_root(entry: str) -> str | None:
    """One configured system read root, in the spelling roots are compared in,
    or ``None`` when the entry bounds nothing.

    A relative name is no root — it names a different place from every
    directory it is read against — and neither is a filesystem root itself, in
    either dialect: an allowlist of ``/``, or of ``C:\\``, is no fence.
    """
    parts = _root_parts(entry)
    if parts is None or not parts[1]:
        return None
    drive, segments = parts
    return (f"{drive}:" if drive else "") + "/" + "/".join(segments)


def within_root(root: str, path: str) -> bool:
    """Whether ``path`` is at or under ``root``, judged on the spelling alone.

    The counterpart of :func:`read_root`, and deliberately the same reading: a
    root parsed one way and compared another is a bound that holds for the
    operator who wrote it and not for the box that runs it. A root that names a
    drive bounds that drive only; one that names none (``/usr``) bounds the
    location on whichever drive the path is on, which is how the platform reads
    a rooted-but-driveless name itself. A relative ``path`` is under nothing: it
    names no location until it is joined to one.
    """
    bound = _root_parts(root)
    target = _root_parts(path)
    if bound is None or target is None or not bound[1]:
        return False
    if bound[0] and bound[0] != target[0]:
        return False
    head = target[1][: len(bound[1])]
    if len(head) != len(bound[1]):
        return False
    if os.name == "nt" or bound[0] or target[0]:
        # A drive names a Windows path, and a Windows filesystem does not tell
        # two spellings of one name apart.
        return [part.lower() for part in head] == [part.lower() for part in bound[1]]
    return head == bound[1]


def _has_wildcard(text: str) -> bool:
    return any(char in _WILDCARDS for char in text)


def _foreign_environ(text: str) -> bool:
    """Whether ``text`` names another process's ``environ`` under ``/proc``:
    ``/proc/1/environ``, ``/proc/*/environ``, ``/proc/all/environ`` (what a
    ``ps e`` reads), a thread's under ``task``. The process's own
    (``/proc/self``, ``/proc/thread-self``) is not another's."""
    parts = PurePosixPath(text.replace("\\", "/")).parts
    if len(parts) < 4 or parts[:2] != ("/", "proc") or parts[-1] != "environ":
        return False
    return parts[2] not in _OWN_ENTRY_NAMES


def _glob_prefix(pattern: str) -> str | None:
    """The literal part of a glob — every segment before the first one that
    carries a wildcard — or ``None`` when the pattern has a ``..`` segment
    anywhere, which no prefix can vouch for."""
    parts = Path(pattern).parts
    if ".." in parts:
        return None
    literal: list[str] = []
    for part in parts:
        if _has_wildcard(part):
            break
        literal.append(part)
    return str(Path(*literal)) if literal else "."


def _exists(path: Path) -> bool:
    """Whether the name exists, without following the last link and without
    opening anything."""
    try:
        return path.exists() or path.is_symlink()
    except (OSError, ValueError):
        return False


def _elsewhere(
    text: str, *, anchor: Path, resolved: Path, fenced: Path | None, base: Path | None
) -> bool:
    """Whether a RELATIVE name is really a location outside the fence.

    A harness spells a location relative to its own idea of the workspace, and
    that idea is not always ours: opencode reports paths relative to its
    ``worktree``, which for a project that is not a repository is ``/`` — so
    ``/etc/passwd`` arrives spelled ``etc/passwd``, and read against the project
    root it becomes a file that is simply not there. A fence a spelling can walk
    around is not a fence, so a name that is NOT where it says it is, but IS a
    real location when read as absolute, is judged where the file really is.

    A name that exists nowhere is left alone: it is a miss, and a read of a file
    that is not there discloses nothing.
    """
    if text.startswith(("/", "~")) or base is not None or _exists(resolved):
        return False
    alternate = _resolve("/" + text, base=anchor)
    if alternate is None or not _exists(alternate):
        return False
    if not _is_within(alternate, anchor):
        return True
    return fenced is not None and _is_within(alternate, fenced)


def path_escapes(
    text: str,
    *,
    root: Path,
    home: Path | None = None,
    base: Path | None = None,
    glob: bool = False,
    sandbox: Path | None = None,
    own: Sequence[Path] = (),
) -> bool:
    """Whether ``text`` names a location outside ``root``, or inside ``home``, or
    inside the daemon's own ``<root>/.alkera`` state (other than ``sandbox`` and
    the chat's ``own`` trees).

    ``base`` is what a relative location resolves against (the root when
    omitted). ``glob`` judges the text as a pattern; a text carrying a wildcard
    is judged as one regardless. ``sandbox`` is the session's own scratch dir
    under ``.alkera`` — the one location inside ``.alkera`` a session may still
    reach (plan/scratch files). An empty text names nothing and passes.
    """
    if not text:
        return False
    subject = text
    if glob or _has_wildcard(text):
        prefix = _glob_prefix(text)
        if prefix is None:
            return True
        subject = prefix
    anchor = _resolve(str(root), base=Path.cwd())
    if anchor is None:
        return True
    resolved = _resolve(subject, base=base or anchor)
    if resolved is None or not _is_within(resolved, anchor):
        return True
    fenced = _resolve(str(home if home is not None else default_home()), base=anchor)
    if fenced is not None and _is_within(resolved, fenced):
        return True
    # The daemon's own state under the workspace root is off limits (other chats'
    # transcripts, file-custody connector credentials, the connections manifest) —
    # even though it sits inside the root — except the session's own sandbox.
    state_dir = anchor / ALKERA_STATE_DIRNAME
    if _is_within(resolved, state_dir):
        allowed = [
            granted
            for tree in (*((sandbox,) if sandbox is not None else ()), *own)
            if (granted := _resolve(str(tree), base=anchor)) is not None
        ]
        if not any(_is_within(resolved, granted) for granted in allowed):
            return True
    return _elsewhere(subject, anchor=anchor, resolved=resolved, fenced=fenced, base=base)


def locations_of(tool: str, args: Mapping[str, Any]) -> Iterator[tuple[str, bool]]:
    """Every location a tool call names, as ``(text, is_glob)``, ``cwd`` first.
    Values that are not non-empty strings name nothing."""
    for key in LOCATION_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            yield value, False
    for key in GLOB_KEYS.get(tool, ()):
        value = args.get(key)
        if isinstance(value, str) and value:
            yield value, True


def first_escape(
    tool: str,
    args: Mapping[str, Any],
    *,
    root: Path,
    home: Path | None = None,
    sandbox: Path | None = None,
) -> str | None:
    """The first location of a tool call that escapes the workspace, spelled as
    the call spelled it (so a refusal can quote it), or ``None``."""
    base: Path | None = None
    for text, is_glob in locations_of(tool, args):
        if path_escapes(text, root=root, home=home, base=base, glob=is_glob, sandbox=sandbox):
            return text
        if base is None and args.get("cwd") == text:
            # A cwd inside the root is what the call's other relative
            # locations are relative to.
            base = _resolve(text, base=root)
    return None


def ask_escape(
    request: PermissionRequest,
    *,
    root: Path,
    home: Path | None = None,
    sandbox: Path | None = None,
    own: Sequence[Path] = (),
) -> str | None:
    """The first location a permission ask names that escapes the workspace,
    or ``None`` — for a shell ask, or one that names no location at all.

    The ask's typed subject carries the real location the harness is about to
    touch (``raw``, and the file ``targets``); a ``glob`` ask's ``patterns``
    carry the filename glob; the ``<dir>/*`` allow-globs a read-class ask
    carries in ``patterns`` are locations too and are judged as globs. A
    ``grep`` ask's ``patterns`` are regular expressions and are left alone.
    """
    kind = request.permission_kind
    subject = request.subject if isinstance(request.subject, Mapping) else {}
    shell = kind in SHELL_KINDS or subject.get("capability") == "shell"
    if shell:
        return None
    locations: list[tuple[str, bool]] = []
    raw = subject.get("raw")
    if isinstance(raw, str) and raw:
        locations.append((raw, kind == "glob"))
    targets = subject.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, Mapping) or target.get("kind") != "file":
                continue
            name = target.get("name")
            if isinstance(name, str) and name and name != raw:
                locations.append((name, False))
    if kind != "grep":
        locations.extend((pattern, True) for pattern in request.patterns if pattern)
    for text, is_glob in locations:
        if path_escapes(text, root=root, home=home, glob=is_glob, sandbox=sandbox, own=own):
            return text
    return None


# ---------------------------------------------------------------------------
# The write fence: this chat's folder, and nothing else
# ---------------------------------------------------------------------------

#: How many names one glob may match before this reader stops vouching for it.
GLOB_MATCH_LIMIT = 2048

#: Devices every command may read or write: they hold nothing and keep nothing.
HARMLESS_DEVICES: frozenset[str] = frozenset(
    {"/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/urandom"}
)


@dataclass(frozen=True, slots=True)
class FenceVerdict:
    """What the fence says about one action.

    ``inside`` — every location it names is in bounds. ``escape`` — ``target``
    is out of bounds, and the action is refused in every permission mode.
    ``unknown`` — the locations cannot be proved, so nothing may allow the
    action without a person: it is asked where a person is asked, and refused
    where nobody is.
    """

    outcome: str
    target: str | None = None
    writing: bool = False
    """Whether the ACTION is a write — which sentence the model needs."""
    bound: str = "read"
    """Which bound answered, ``"read"`` (the workspace) or ``"write"`` (the chat
    folder) — a write off the box is caught by the read bound first. An
    ``inside`` with ``"process"`` is a signal confined to the sandbox's own
    process table, which the gate admits in every stance."""
    reason: str | None = None
    """For ``unknown``: what the reader could not place, in its own words — a
    program it cannot read, a wrapper that carries another command, a
    destination the command decides later. The model is told this, not a
    guess at what to spell differently."""

    @property
    def escaped(self) -> bool:
        return self.outcome == "escape"

    @property
    def unknown(self) -> bool:
        return self.outcome == "unknown"


INSIDE = FenceVerdict("inside")


@dataclass(frozen=True, slots=True)
class SessionFence:
    """One cloud chat's bounds, and the ONE judge every decision path asks.

    The harness's permission chokepoint, the in-tool shell gate, the mirror's
    prompt resolver and its relayed-allow check all call :meth:`judge_ask` or
    :meth:`judge_shell`; none of them reads a path on its own, so a stance that
    stops prompting (``auto``, ``bypass``, an always-allow rule) reaches exactly
    what ``default`` reaches.

    ``root`` is the workspace a file tool may read, ``folder`` the one directory
    a write may land in (a cloud chat's working directory, so a ``..`` onto the
    chat's own records is a write outside it), ``working_dir`` the directory the
    agent runs in — the whole of what its SHELL may read, beside
    ``system_roots``. Every name is judged on the path it resolves to after
    joining the directory it is relative to, with ``..`` folded and symlinks
    followed, so a link inside the bound that points out of it is outside.
    """

    root: Path
    folder: Path
    working_dir: Path
    home: Path | None = None
    system_roots: tuple[str, ...] = ()
    aliases: tuple[tuple[str, Path], ...] = ()
    """Paths the agent sees a bound directory at that are not its host path —
    the sandbox's ``/home/alkera``, where the working directory is mounted —
    each with the host path it is bound to. A name under an alias is judged as
    the host path it names; a ``..`` that climbs out of the alias climbs out of
    the host directory, exactly as it does on the host. Empty when the agent
    sees host paths only, so a real ``/home/alkera`` on a box that never
    mounted one is the foreign path it is."""
    own_trees: tuple[Path, ...] = ()
    """Trees outside the working directory that are this chat's alone and that
    it may read and write like its folder: the default Python environment the
    sandbox makes under the chat's runtime state (the environment and the uv,
    pip and mamba caches beside it). Nothing else of the runtime state is in
    here — the agent's own database there is the chat's record — and no other
    chat's tree ever is. Never synced: the drive treats the same tree as this
    box's local state."""
    sandboxed: bool = False
    """Whether the shell runs in a sandbox of its own (gVisor), whose mounts
    and uid bound every command whatever this judge can read of it. Two
    things follow. The container's process table holds the agent server and
    the chat's own commands and nothing of the box, so a shell command may
    list it, read ``/proc/self`` and signal what it finds (``ps``, ``pgrep``,
    ``kill``, ``pkill``); only another process's ``environ`` stays refused,
    since PID 1 is the agent server and its environment carries its loopback
    credential. And a command whose reach the judge cannot prove (an
    interpreter, a loop, a program it does not model) is not a question for a
    reader but a command for the stance ladder, confined like any other. The
    file tools' asks are judged against the workspace as before, so ``/proc``
    is outside for them whatever this says. Off where the shell runs on the
    box itself (a ``none`` host): there the floor and the questions stand."""

    # -- aliases --------------------------------------------------------------

    def spell(self, path: Path | str) -> str:
        """A host ``path`` spelled as the agent sees it: under the alias bound
        to the deepest aliased host directory that holds it, the host path
        itself when none does. The inverse of :meth:`canonical`, for what the
        fence says BACK to the model and what a tool answers it with: a
        boundary, a spilled output, a materialized file must be a path the
        model can type, and on a box the host path is one it cannot."""
        host = Path(path)
        deepest = sorted(self.aliases, key=lambda pair: len(pair[1].parts), reverse=True)
        for alias, real in deepest:
            try:
                rest = host.relative_to(real)
            except ValueError:
                continue
            prefix = alias.rstrip("/") or "/"
            return str(PurePosixPath(prefix) / rest.as_posix())
        return _host_str(host)

    def canonical(self, text: str) -> str:
        """``text`` with a mounted alias it starts with replaced by the host
        path bound there, lexically: the alias must be the whole first
        segments (``/home/alkera/x`` maps; ``/home/alkeraX/x`` does not), and
        what follows must stay under it. A ``..`` that climbs out of the alias
        lands in the container's own root, which is no host directory the
        fence can vouch for, so the text is left as spelled and judged as the
        host path it would be — outside. What stays under the alias keeps its
        spelling (``x/../y`` included), so the usual resolution judges where
        it really lands."""
        for alias, real in self.aliases:
            prefix = alias.rstrip("/")
            if not prefix:
                continue
            if text == prefix:
                return _host_str(real)
            if not text.startswith(prefix + "/"):
                continue
            tail = text[len(prefix) :]
            depth = 0
            for segment in tail.split("/"):
                if segment == "..":
                    depth -= 1
                    if depth < 0:
                        return text
                elif segment not in ("", "."):
                    depth += 1
            return _host_str(real) + tail
        return text

    def _canonical_request(
        self, request: PermissionRequest
    ) -> tuple[PermissionRequest, dict[str, str]]:
        """``request`` with every location it names spelled as the host path,
        and the way back: each host spelling to the spelling the ask used, so
        a refusal quotes what the model wrote."""
        if not self.aliases:
            return request, {}
        spelled: dict[str, str] = {}

        def canon(text: str) -> str:
            mapped = self.canonical(text)
            if mapped == text and text and not text.startswith(("/", "~")):
                # opencode spells an ask's patterns relative to its worktree,
                # which in the sandbox is ``/``: ``/home/alkera`` arrives as
                # ``home/alkera``. Read with its root restored, a name under
                # the alias is the working directory — and nothing else can
                # come of this, since ``canonical`` maps only what stays
                # under the alias.
                rooted = "/" + text
                restored = self.canonical(rooted)
                if restored != rooted:
                    spelled.setdefault(restored, rooted)
                    return restored
            if mapped != text:
                spelled.setdefault(mapped, text)
            return mapped

        update: dict[str, Any] = {}
        if isinstance(request.subject, Mapping):
            subject = dict(request.subject)
            raw = subject.get("raw")
            if isinstance(raw, str):
                subject["raw"] = canon(raw)
            targets = subject.get("targets")
            if isinstance(targets, list):
                mapped: list[Any] = []
                for target in targets:
                    if isinstance(target, Mapping) and isinstance(target.get("name"), str):
                        mapped.append({**target, "name": canon(target["name"])})
                    else:
                        mapped.append(target)
                subject["targets"] = mapped
            update["subject"] = subject
        if request.patterns:
            update["patterns"] = [canon(pattern) for pattern in request.patterns]
        return (request.model_copy(update=update) if update else request), spelled

    # -- asks -----------------------------------------------------------------

    def judge_ask(self, request: PermissionRequest, *, writing: bool) -> FenceVerdict:
        """One permission ask. ``writing`` is whether allowing it would let a
        write-class action run."""
        subject = request.subject if isinstance(request.subject, Mapping) else {}
        if request.permission_kind in SHELL_KINDS or subject.get("capability") == "shell":
            raw = subject.get("raw")
            if not isinstance(raw, str) or not raw:
                # The parent-hosted shell's ask names no command; the tool's own
                # gate judges the real text before anything runs.
                return INSIDE
            return self.judge_shell(raw, cwd=self.working_dir, writing=writing)
        request, spelled = self._canonical_request(request)
        outside = ask_escape(
            request, root=self.root, home=self.home, sandbox=self.folder, own=self.own_trees
        )
        if outside is not None:
            return FenceVerdict("escape", spelled.get(outside, outside), writing, "read")
        if writing:
            elsewhere = ask_write_escape(
                request, folder=self.folder, base=self.working_dir, own=self.own_trees
            )
            if elsewhere is not None:
                return FenceVerdict("escape", spelled.get(elsewhere, elsewhere), True, "write")
        return INSIDE

    def refuses_a_write(self, request: PermissionRequest, escape: str, writing: bool) -> bool:
        """Whether refusing ``escape`` in ``request`` refuses a write, which is
        the sentence the model needs. ``writing`` is the classifier's word on
        the whole action.

        For a shell command that word is not the location's: ``cat
        /proc/version; dmesg`` is a write to the classifier (``dmesg`` is in no
        table), yet ``/proc/version`` is a word ``cat`` only reads, and a model
        told its read was a write retries the read from its sandbox. The
        verdict carries the role the command gives the location it caught, so
        it answers whenever it names ``escape``; ``writing`` answers otherwise.
        """
        if not escape:
            return writing
        verdict = self.judge_ask(request, writing=writing)
        return verdict.writing if verdict.escaped and verdict.target == escape else writing

    def explain(self, verdict: FenceVerdict) -> str:
        """What the model is told about a verdict that is not ``inside``."""
        from alkera_cli.cloud.refusal import fence_reason, quote_statement

        if verdict.unknown:
            where = quote_statement(verdict.target or "") or "this command"
            why = f": {verdict.reason}" if verdict.reason else ""
            # Advice only where it is true. A destination the reader could not
            # place can be spelled plainly; a program it cannot read cannot be
            # respelled into one it can, and telling the model to try is what
            # sends it looking for an absolute path that changes nothing.
            respellable = (
                "destination",
                "not a value this fence can read",
                "hides where it writes",
            )
            advice = (
                " Spell the destination plainly, or use the read/edit tools."
                if verdict.reason and any(mark in verdict.reason for mark in respellable)
                else " Use the read/edit tools where they do the job."
            )
            return (
                f"The workspace policy could not tell where {where} reads or writes{why}; "
                "it runs only with a person's approval or in a stance that runs everything "
                f"without asking.{advice}"
            )
        # The one directory the model can type: its root, spelled the way it
        # sees it. A write's bound is the folder and a read's the working
        # directory; on a cloud chat both are that root.
        boundary = self.spell(self.working_dir)
        return fence_reason(writing=verdict.writing, target=verdict.target or "", boundary=boundary)

    # -- the shell ------------------------------------------------------------

    def judge_shell(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        writing: bool = False,
    ) -> FenceVerdict:
        """One shell command, run in ``cwd`` with ``env``.

        Judged on the path the name RESOLVES to, so a link inside the working
        directory that points out of it is the location it points at. The shell
        opens the name again when it runs, which a judge cannot hold still; the
        unprivileged user the command runs as is the bound that survives that.

        ``writing`` is the classifier's word on the command, used only to phrase
        an ``unknown`` verdict: what the model cannot read, the fence cannot class.
        """
        if self._read_escapes(self.canonical(str(cwd)), base=self.working_dir):
            return FenceVerdict("escape", str(cwd), False)
        effects = analyze_shell(command, env=env, backslash_escapes=backslash_escapes_here())
        # Every location the model read is judged, whatever else is in the
        # command: a write through ``sudo`` or inside ``bash -c`` that lands
        # outside is an escape, not a question.
        for location in effects.locations:
            base: Path | None = Path(self.canonical(str(cwd)))
            for step in location.moved:
                base = _resolve(self.canonical(step), base=base) if base is not None else None
            bound = "write" if location.writing else "read"
            if base is None:
                return FenceVerdict("escape", location.text, location.writing, bound)
            text = self.canonical(location.text)
            if location.writing:
                if text not in HARMLESS_DEVICES and (
                    location.glob
                    or write_escapes(text, folder=self.folder, base=base, own=self.own_trees)
                ):
                    return FenceVerdict("escape", location.text, True, "write")
            elif self._read_escapes(text, base=base, glob=location.glob):
                return FenceVerdict("escape", location.text, False, "read")
        if not effects.readable:
            # What the model cannot read, the floor still reads: a name of the
            # box's credential home, another process's state or a sibling chat
            # anywhere in the text is refused whatever the stance, because a
            # one-token wrapper (``bash -c``, ``eval``, ``python3 -c``) must
            # not turn an escape into a question. Everything else the model
            # could not place stays a question, asked with the model's reason.
            named = self._floor_named_in(command, cwd=cwd, env=env)
            if named is not None:
                return FenceVerdict("escape", named, writing, "write" if writing else "read")
            if self.sandboxed and all(entry.kind == "process" for entry in effects.opaque):
                # A signal reaches only the sandbox's own processes: inside,
                # and named as such so the gate admits it in every stance.
                return FenceVerdict("inside", command, writing, "process")
            return FenceVerdict(
                "unknown",
                command,
                writing,
                "write" if writing else "read",
                reason=effects.opaque[0].reason,
            )
        return INSIDE

    def _read_escapes(self, text: str, *, base: Path, glob: bool = False) -> bool:
        if not text or text in HARMLESS_DEVICES or text == "-":
            return False
        if glob:
            if self.sandboxed and _foreign_environ(text):
                # ``/proc/*/environ`` names every environment, the agent
                # server's included, whatever the pattern would match here.
                return True
            prefix = _glob_prefix(text)
            if prefix is None or self._read_escapes(prefix, base=base):
                return True
            anchor = Path(text) if Path(text).is_absolute() else base / text
            matched = 0
            try:
                # Lazily, and no further than the cap: a pattern under a permitted
                # system root could otherwise walk the whole tree.
                for match in iglob(str(anchor), recursive=True):
                    matched += 1
                    if matched > GLOB_MATCH_LIMIT or self._read_escapes(match, base=base):
                        return True
            except (OSError, ValueError):
                return True
            return False
        resolved = _resolve(text, base=base)
        if resolved is None:
            return True
        if self._always_refused(resolved):
            return True
        if self.sandboxed and _is_within(resolved, Path(PROCESS_TABLE)):
            # The sandbox's own process table, with the other processes'
            # environments already refused above.
            return False
        bound = _resolve(str(self.working_dir), base=Path.cwd())
        if bound is not None and _is_within(resolved, bound):
            return False
        if self._in_own_tree(resolved):
            return False
        # A system root is judged on the name as spelled as well as on what it
        # resolves to: ``/usr/share/zoneinfo`` is a link the system itself
        # planted (to ``/var/db/timezone`` on macOS), and the agent cannot plant
        # one under a root it cannot write. What the link reaches still has to
        # clear the always-refused set above. The spelling is compared the way
        # the roots were parsed, so a bound holds on the box that runs it and
        # not only on the one it was written on.
        spelled = text if _root_parts(text) is not None else str(base / text)
        for allowed in self.system_roots:
            if read_root(allowed) is None:
                continue
            granted = _resolve(allowed, base=Path("/"))
            if granted is not None and granted != Path(granted.anchor):
                if _is_within(resolved, granted):
                    return False
            if within_root(allowed, spelled):
                return False
        return True

    def _floor_named_in(
        self, command: str, *, cwd: Path, env: Mapping[str, str] | None
    ) -> str | None:
        """The first path-like token of ``command`` that lands on the floor no
        allowlist can open (see :meth:`_always_refused`), read off the raw text
        with no parse, under every reading :func:`_floor_scan_readings` gives: as
        written and with adjacent/empty quotes joined, each expanded from ``env``
        alone and from ``env`` plus the command's own ``NAME=value`` assignments,
        ``~`` from ``HOME``, a relative name taken from ``cwd``. ``None`` when no
        reading names the floor.

        The quote-join and the in-command assignments are the two cheap floor
        tightenings: a wrapped command (``bash -c '…'``) whose reach the parser
        cannot prove is still refused when it NAMES the floor, even if it split the
        path with empty quotes or rebuilt it from an assignment. What remains
        beyond them — a path assembled inside a command substitution
        (``cat $(echo … | base64 -d)``) — a lexical scan cannot compute; the
        per-chat unprivileged uid is that closure."""
        for text, variables in _floor_scan_readings(command, env):
            named = self._floor_token(text, variables, cwd=cwd)
            if named is not None:
                return named
        return None

    def _floor_token(self, command: str, variables: Mapping[str, str], *, cwd: Path) -> str | None:
        """The first path-like token of one reading of a command that lands on
        the floor, expanded from ``variables``, or ``None``."""
        home = variables.get("HOME")
        for raw in _path_like_tokens(command):
            text = _VARIABLE.sub(lambda m: variables.get(m.group(1) or m.group(2) or "", ""), raw)
            if text == "~" or text.startswith("~/"):
                if home:
                    text = home + text[1:]
            elif text.startswith("~"):
                continue  # another user's home is the parser's question, not this floor's
            # The process table is the BOX's ``/proc``, matched on the spelling
            # in the box's dialect whatever host reads it: on a Windows host
            # ``_resolve`` joins a driveless ``/proc`` onto the host's drive and
            # the floor below no longer sees it. A sandbox with a table of its
            # own keeps only the other processes' environments on the floor.
            if within_root(PROCESS_TABLE, text) and (not self.sandboxed or _foreign_environ(text)):
                return raw
            # A name under one of the sandbox's mounts is the host tree it is
            # bound to: ``/opt/alkera/harness/data/agent/agent.db`` is the
            # chat's own session database, on the floor like its host path.
            resolved = _resolve(self.canonical(text), base=Path(self.canonical(str(cwd))))
            if resolved is not None and self._always_refused(resolved):
                return raw
        return None

    def _always_refused(self, resolved: Path) -> bool:
        """Locations no allowlist can open: the box's credential home, another
        process's state, and the daemon's own ``.alkera`` beside this chat."""
        fenced = _resolve(
            str(self.home if self.home is not None else default_home()), base=Path("/")
        )
        if fenced is not None and _is_within(resolved, fenced):
            return True
        if _is_within(resolved, Path(PROCESS_TABLE)):
            return not self.sandboxed or _foreign_environ(resolved.as_posix())
        state = _resolve(str(self.root / ALKERA_STATE_DIRNAME), base=Path.cwd())
        folder = _resolve(str(self.folder), base=Path.cwd())
        if state is not None and _is_within(resolved, state):
            if folder is not None and _is_within(resolved, folder):
                return False
            return not self._in_own_tree(resolved)
        return False

    def _in_own_tree(self, resolved: Path) -> bool:
        """Whether ``resolved`` lies in one of :attr:`own_trees`, each judged on
        the path it resolves to — a link planted in one that points out of it
        is the location it points at."""
        for tree in self.own_trees:
            granted = _resolve(str(tree), base=Path.cwd())
            if granted is not None and _is_within(resolved, granted):
                return True
        return False


def _host_str(path: Path) -> str:
    """``path`` spelled the way the host it was composed on spells it."""
    return str(path)


#: The chat's own records, at the top of its folder: the manifest that pins the
#: agent session, the transcript and the decision and cost logs, the trace
#: digest, and the harness's runtime state. They sit inside the write fence,
#: one level above the directory the agent runs in, and they are the box's to
#: write, never the model's — a tool that rewrote the manifest would re-pin its
#: own session, and one that rewrote the transcript would write its own
#: history. Refused in every mode, like a write outside the folder, because the
#: mode governs prompting and not reach. The set is the one the drive marks a
#: record by, so the fence and the server refuse the same names.
CHAT_RECORD_NAMES: frozenset[str] = SHARED_CHAT_RECORD_NAMES


def _is_chat_record(resolved: Path, fence: Path) -> bool:
    """Whether ``resolved`` is one of the chat's records, or lies under one."""
    try:
        head = resolved.relative_to(fence).parts[0]
    except (ValueError, IndexError):
        return False
    return head in CHAT_RECORD_NAMES


def write_escapes(
    text: str, *, folder: Path, base: Path | None = None, own: Sequence[Path] = ()
) -> bool:
    """Whether writing ``text`` would land outside ``folder`` and outside every
    one of the chat's ``own`` trees — or on one of the chat's own records at
    the folder's top level, which the fence keeps for the box.

    ``base`` is what a relative name resolves against: the directory the agent
    runs in, which is the chat's working directory rather than the fenced
    folder itself. A wildcard is refused outright rather than judged by its
    prefix: a write whose destination is decided by what a glob matches is a
    destination no check made now can vouch for.
    """
    if not text:
        return False
    if _has_wildcard(text):
        return True
    fence = _resolve(str(folder), base=Path.cwd())
    if fence is None:
        return True
    resolved = _resolve(text, base=base or fence)
    if resolved is None:
        return True
    if not _is_within(resolved, fence):
        for tree in own:
            granted = _resolve(str(tree), base=Path.cwd())
            if granted is not None and _is_within(resolved, granted):
                return False
        return True
    return _is_chat_record(resolved, fence)


def write_escape(
    tool: str,
    args: Mapping[str, Any],
    *,
    folder: Path,
    base: Path | None = None,
) -> str | None:
    """The first destination a tool call would write outside ``folder``.

    For a shell tool that is every destination in its command, read against its
    own ``cwd`` when it named one; for every other tool it is the locations the
    call names. A relative destination resolves against ``base`` — where the
    agent runs — when the call names no ``cwd`` of its own. ``None`` means
    every write this call makes lands in the chat's own folder.
    """
    cwd = args.get("cwd")
    base = _resolve(cwd, base=base or folder) if isinstance(cwd, str) and cwd else base
    if tool in SHELL_KINDS:
        command = next(
            (
                value
                for key in ("command", "cmd", "script")
                if isinstance(value := args.get(key), str) and value
            ),
            "",
        )
        return _shell_write_escape(command, folder=folder, base=base)
    for text, _is_glob in locations_of(tool, args):
        if text == cwd:
            # Where a call runs is not where it writes.
            continue
        if write_escapes(text, folder=folder, base=base):
            return text
    return None


def _shell_write_escape(
    command: str, *, folder: Path, base: Path | None, own: Sequence[Path] = ()
) -> str | None:
    """The first destination a shell command writes outside ``folder``: every
    write the model read, judged where the command's ``cd`` chain puts it;
    then, for a command the model could not read whole, the command itself,
    which is what a refusal quotes."""
    if not command:
        return None
    effects = analyze_shell(command, backslash_escapes=backslash_escapes_here())
    for location in effects.writes:
        where: Path | None = base if base is not None else _resolve(str(folder), base=Path.cwd())
        for step in location.moved:
            where = _resolve(step, base=where) if where is not None else None
        if where is None or write_escapes(location.text, folder=folder, base=where, own=own):
            return location.text
    if not effects.readable:
        return command
    return None


def ask_write_escape(
    request: PermissionRequest,
    *,
    folder: Path,
    base: Path | None = None,
    own: Sequence[Path] = (),
) -> str | None:
    """The first destination a permission ask would write outside ``folder``.

    The counterpart of :func:`ask_escape` for the write side: the ask's subject
    carries the command for a shell ask and the file locations for every other,
    and a write outside this chat's folder is refused whatever the permission
    mode says, because the mode governs prompting and the folder governs reach.
    ``base`` is the directory the agent runs in, which a relative destination
    is judged against.
    """
    subject = request.subject if isinstance(request.subject, Mapping) else {}
    kind = request.permission_kind
    shell = kind in SHELL_KINDS or subject.get("capability") == "shell"
    if shell:
        raw = subject.get("raw")
        command = raw if isinstance(raw, str) else ""
        return _shell_write_escape(command, folder=folder, base=base, own=own)
    locations: list[str] = []
    raw = subject.get("raw")
    if isinstance(raw, str) and raw:
        locations.append(raw)
    targets = subject.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, Mapping) or target.get("kind") != "file":
                continue
            name = target.get("name")
            if isinstance(name, str) and name and name not in locations:
                locations.append(name)
    for text in locations:
        if write_escapes(text, folder=folder, base=base, own=own):
            return text
    return None


__all__ = [
    "CHAT_RECORD_NAMES",
    "GLOB_KEYS",
    "GLOB_MATCH_LIMIT",
    "HARMLESS_DEVICES",
    "INSIDE",
    "LOCATION_KEYS",
    "SHELL_KINDS",
    "FenceVerdict",
    "SessionFence",
    "ask_escape",
    "ask_write_escape",
    "default_home",
    "first_escape",
    "locations_of",
    "path_escapes",
    "read_root",
    "within_root",
    "write_escape",
    "write_escapes",
]
