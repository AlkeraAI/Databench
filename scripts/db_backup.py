#!/usr/bin/env python
"""Back up and restore the Postgres database, privileges included.

    python scripts/db_backup.py backup  --url <libpq url> --file alkera.dump
    python scripts/db_backup.py restore --url <libpq url> --file alkera.dump
    python scripts/db_backup.py restore --url ... --file ... --recreate --confirm <database>
    python scripts/db_backup.py regrant --url <libpq url>

Tenant isolation rests on two roles (``alkera_files_app``, ``alkera_tenant_app``)
and what they are granted: every Files statement and every authenticated
request runs as one of them. A dump taken without privileges restores tables
those roles cannot read, and the app answers 500 on everything they serve. So:

* ``backup`` keeps privileges, and writes the roles they name beside the dump
  (``<file>.roles.sql``). Roles belong to the cluster, not the database, so
  ``pg_dump`` cannot carry them.
* ``restore`` creates those roles when the target cluster lacks them, restores
  in one transaction and stops at the first error, so a restore either
  finishes or leaves the target as it found it. It then checks what it did:
  the schema revision is the dump's, and every role the dump granted to can
  read.
* ``restore`` never writes over a database that holds anything. Restoring a
  dump over a newer schema leaves a mix of the two (tables the dump cannot
  drop keep their newer rows and columns), which is worse than either. A
  non-empty target is refused unless ``--recreate --confirm <database>`` says
  to drop and recreate it first.
* ``regrant`` repairs a database restored from a dump that carried no
  privileges (one made with ``--no-privileges``, as this repo's ``make backup``
  did before). It builds an empty database at the same revision with the
  migrations, and copies that database's grants. The migrations stay the only
  place a grant is spelled.

The client tools must be the server's major version: a newer ``pg_restore``
sends settings an older server refuses. ``PG_BIN_DIR`` names a directory
holding ``pg_dump``, ``pg_restore`` and ``psql`` when the ones on ``PATH`` are
another version.

The decisions (what a role statement says, whether a target may be written,
which dump entries are grants) are plain functions over plain data. The shell
around them runs the tools and the queries.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parents[1]

#: Exit statuses a caller can tell apart.
EXIT_REFUSED = 3
EXIT_FAILED = 4

ROLES_SUFFIX = ".roles.sql"


class RefusedError(Exception):
    """The request was not carried out, and nothing was changed."""


class FailedError(Exception):
    """A step failed. The message says what state the target is in."""


# --------------------------------------------------------------------------- #
# Pure decisions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Role:
    """A role the database's privileges name, with the attributes that matter
    to row-level security."""

    name: str
    can_login: bool
    bypass_rls: bool


@dataclass(frozen=True)
class Membership:
    """``member`` may assume ``role``."""

    role: str
    member: str


def libpq_url(url: str) -> str:
    """A plain libpq URL from a SQLAlchemy one (``postgresql+psycopg://`` and
    the like), which the Postgres tools do not accept."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    if scheme not in {"postgresql", "postgres"}:
        raise RefusedError(f"not a Postgres URL: scheme {parts.scheme!r}")
    return urlunsplit(parts._replace(scheme=scheme))


def database_of(url: str) -> str:
    name = urlsplit(url).path.lstrip("/")
    if not name:
        raise RefusedError("the URL names no database")
    return name


def with_database(url: str, database: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path="/" + database))


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def roles_script(
    roles: Sequence[Role], memberships: Sequence[Membership], *, dumped_as: str
) -> str:
    """The idempotent SQL that makes a cluster ready for the dump's grants.

    A role that exists is left exactly as it is. A missing one is created with
    no password and never as a superuser: a login among them cannot be used
    until someone sets its password. A membership is granted only when both
    roles exist; the login the dump was taken as stands for whoever restores
    it, since that is the login the app's sessions step down from.
    """
    lines = [
        "-- Roles the dump's privileges name. Written by scripts/db_backup.py.",
        "-- Safe to run again: an existing role is not altered.",
    ]
    for name in (
        {r.name for r in roles} | {m.role for m in memberships} | {m.member for m in memberships}
    ):
        # The statements below sit inside a dollar-quoted block, which a name
        # carrying a dollar sign could close early.
        if "$" in name:
            raise RefusedError(f"a role name with a dollar sign cannot be scripted: {name!r}")
    for role in sorted(roles, key=lambda r: r.name):
        attributes = " ".join(
            [
                "LOGIN" if role.can_login else "NOLOGIN",
                "NOSUPERUSER",
                "BYPASSRLS" if role.bypass_rls else "NOBYPASSRLS",
            ]
        )
        # DDL takes no bind parameters; every name is quoted and checked above.
        lines.append(
            "DO $roles$ BEGIN "  # noqa: S608
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role.name)}) "
            f"THEN CREATE ROLE {quote_ident(role.name)} {attributes}; END IF; END $roles$;"
        )
    for grant in sorted(memberships, key=lambda m: (m.role, m.member)):
        if grant.member == dumped_as:
            lines.append(f"GRANT {quote_ident(grant.role)} TO CURRENT_USER;")
            continue
        lines.append(
            "DO $roles$ BEGIN "  # noqa: S608
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(grant.member)}) "
            f"THEN GRANT {quote_ident(grant.role)} TO {quote_ident(grant.member)}; "
            "END IF; END $roles$;"
        )
    return "\n".join(lines) + "\n"


def may_write(
    *, exists: bool, relations: int, recreate: bool, confirm: str | None, name: str
) -> str:
    """What a restore does to the target: ``create`` it, ``use`` it as it is
    (it is empty), or ``recreate`` it. Raises :class:`RefusedError` otherwise."""
    if recreate and confirm != name:
        raise RefusedError(
            f"--recreate drops the database {name!r} and everything in it. "
            f"Say so with --confirm {name}"
        )
    if not exists:
        return "create"
    if recreate:
        return "recreate"
    if relations:
        raise RefusedError(
            f"the database {name!r} is not empty ({relations} tables, views and sequences). "
            "A restore over existing tables leaves a mix of both. Restore into an empty "
            f"database, or pass --recreate --confirm {name} to drop and recreate this one."
        )
    return "use"


#: A ``pg_restore --list`` entry:
#: ``<id>; <catalog oid> <object oid> <TYPE...> <schema> <name> <owner>``.
_TOC_ENTRY = re.compile(r"^\d+; \d+ \d+ (?P<kind>DEFAULT ACL|ACL) ")


def grant_entries(listing: str) -> list[str]:
    """The lines of a ``pg_restore --list`` that are privileges (``ACL`` and
    ``DEFAULT ACL``), in order, as a list file ``pg_restore -L`` accepts."""
    return [line for line in listing.splitlines() if _TOC_ENTRY.match(line)]


def revision_in(copy_output: str) -> str | None:
    """The ``alembic_version`` row in a data-only dump of that table."""
    lines = copy_output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("COPY ") and "alembic_version" in line:
            value = lines[index + 1].strip() if index + 1 < len(lines) else ""
            return value if value and value != "\\." else None
    return None


def major(version_text: str) -> int:
    """The major version in ``pg_dump (PostgreSQL) 16.4`` or in a server's
    ``server_version_num`` (``160004``)."""
    text = version_text.strip()
    if text.isdigit():
        return int(text) // 10000
    found = re.search(r"\b(\d+)(?:\.\d+|[a-z]+\d*)", text)
    if found is None:
        raise FailedError(f"cannot read a Postgres version from {version_text!r}")
    return int(found.group(1))


# --------------------------------------------------------------------------- #
# The shell: tools and queries
# --------------------------------------------------------------------------- #


def _tool(name: str) -> str:
    directory = os.environ.get("PG_BIN_DIR")
    if directory:
        candidate = Path(directory) / name
        if not candidate.is_file():
            raise RefusedError(f"PG_BIN_DIR={directory} holds no {name}")
        return str(candidate)
    found = shutil.which(name)
    if found is None:
        raise RefusedError(f"{name} is not on PATH; install the Postgres client or set PG_BIN_DIR")
    return found


def _run(argv: list[str], *, what: str, capture: bool = False) -> str:
    done = subprocess.run(  # noqa: S603 -- argv is a list, no shell
        argv, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE
    )
    if done.returncode != 0:
        raise FailedError(f"{what} failed (exit {done.returncode}):\n{done.stderr.strip()}")
    if done.stderr.strip():
        sys.stderr.write(done.stderr)
    return done.stdout or ""


def _connect(url: str) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(url, autocommit=True)


def _scalar(conn: psycopg.Connection[tuple[Any, ...]], query: str) -> Any:
    """The first column of the first row, or None when there is no row."""
    row = conn.execute(query).fetchone()
    return None if row is None else row[0]


def require_matching_tools(url: str) -> None:
    with _connect(url) as conn:
        server = major(str(_scalar(conn, "SHOW server_version_num")))
    for name in ("pg_dump", "pg_restore"):
        client = major(_run([_tool(name), "--version"], what=f"{name} --version", capture=True))
        if client != server:
            raise RefusedError(
                f"{name} is version {client} and the server is version {server}. Use the "
                f"client tools of Postgres {server} (set PG_BIN_DIR to their directory, or "
                "run this where they are installed, such as the database's own container)."
            )


_ROLES = """
WITH named AS (
    SELECT (aclexplode(c.relacl)).grantee AS oid FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
    UNION SELECT (aclexplode(n.nspacl)).grantee FROM pg_namespace n
      WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
    UNION SELECT (aclexplode(p.proacl)).grantee FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
    UNION SELECT (aclexplode(d.defaclacl)).grantee FROM pg_default_acl d
    UNION SELECT unnest(p.polroles) FROM pg_policy p
)
SELECT r.rolname, r.rolcanlogin, r.rolbypassrls
FROM pg_roles r
WHERE r.oid IN (SELECT oid FROM named WHERE oid <> 0)
  AND r.rolname <> current_user
  AND NOT r.rolsuper
  AND r.rolname NOT LIKE 'pg\\_%'
ORDER BY r.rolname
"""

_MEMBERSHIPS = """
SELECT granted.rolname, member.rolname
FROM pg_auth_members m
JOIN pg_roles granted ON granted.oid = m.roleid
JOIN pg_roles member ON member.oid = m.member
WHERE granted.rolname = ANY(%s)
ORDER BY 1, 2
"""

_RELATIONS = """
SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
"""

#: Roles that were granted something on a table and cannot read any table.
_ROLES_THAT_CANNOT_READ = """
SELECT r.rolname FROM pg_roles r
WHERE r.rolname = ANY(%s)
  AND NOT EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        AND has_table_privilege(r.oid, c.oid, 'SELECT')
  )
"""


def read_roles(url: str) -> tuple[list[Role], list[Membership], str]:
    with _connect(url) as conn:
        roles = [
            Role(str(n), bool(login), bool(bypass)) for n, login, bypass in conn.execute(_ROLES)
        ]
        memberships = [
            Membership(str(role), str(member))
            for role, member in conn.execute(_MEMBERSHIPS, ([r.name for r in roles],))
        ]
        dumped_as = str(_scalar(conn, "SELECT current_user"))
    return roles, memberships, dumped_as


def _database_state(url: str) -> tuple[bool, int]:
    """Whether the URL's database exists, and how many relations it holds."""
    name = database_of(url)
    with _connect(with_database(url, "postgres")) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if exists is None:
        return False, 0
    with _connect(url) as conn:
        return True, int(_scalar(conn, _RELATIONS))


def _create(url: str, *, drop_first: bool) -> None:
    name = sql.Identifier(database_of(url))
    with _connect(with_database(url, "postgres")) as admin:
        if drop_first:
            try:
                admin.execute(sql.SQL("DROP DATABASE {}").format(name))
            except psycopg.errors.ObjectInUse as error:
                raise FailedError(
                    f"the database is in use and was not dropped ({error.diag.message_detail}). "
                    "Stop the backend, the workers and the gateway, then run the restore again. "
                    "Nothing was changed."
                ) from error
        admin.execute(sql.SQL("CREATE DATABASE {}").format(name))


def backup(url: str, file: Path) -> None:
    url = libpq_url(url)
    require_matching_tools(url)
    roles, memberships, dumped_as = read_roles(url)
    # No --no-privileges: the grants are what lets the tenant roles read.
    # --no-owner: whoever restores owns the objects, which is the login that
    # runs the migrations.
    _run([_tool("pg_dump"), "-Fc", "--no-owner", "-d", url, "-f", str(file)], what="pg_dump")
    roles_file = Path(str(file) + ROLES_SUFFIX)
    roles_file.write_text(roles_script(roles, memberships, dumped_as=dumped_as), encoding="utf-8")
    grants = len(grant_entries(_listing(file)))
    print(f"wrote {file} ({file.stat().st_size} bytes, {grants} privilege entries)")
    print(f"wrote {roles_file} ({len(roles)} roles). Keep the two files together.")


def _listing(file: Path) -> str:
    return _run([_tool("pg_restore"), "--list", str(file)], what="pg_restore --list", capture=True)


def _dump_revision(file: Path) -> str | None:
    out = _run(
        [_tool("pg_restore"), "--data-only", "--table", "alembic_version", "-f", "-", str(file)],
        what="reading the dump's schema revision",
        capture=True,
    )
    return revision_in(out)


def restore(url: str, file: Path, *, recreate: bool, confirm: str | None) -> None:
    url = libpq_url(url)
    if not file.is_file():
        raise RefusedError(f"no such dump: {file}")
    name = database_of(url)
    with _connect(with_database(url, "postgres")):
        pass  # the server answers before anything is decided
    require_matching_tools(with_database(url, "postgres"))
    exists, relations = _database_state(url)
    action = may_write(
        exists=exists, relations=relations, recreate=recreate, confirm=confirm, name=name
    )
    listing = _listing(file)
    wanted = _dump_revision(file)

    if action != "use":
        _create(url, drop_first=action == "recreate")
        print(f"{'recreated' if action == 'recreate' else 'created'} the database {name}")

    roles_file = Path(str(file) + ROLES_SUFFIX)
    if roles_file.is_file():
        _run(
            [
                _tool("psql"),
                "-X",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "--single-transaction",
                "-d",
                url,
                "-f",
                str(roles_file),
            ],
            what="creating the roles the dump grants to",
        )
    else:
        print(f"no {roles_file.name} beside the dump: the roles it names must already exist")

    try:
        _run(
            [
                _tool("pg_restore"),
                "--no-owner",
                "--exit-on-error",
                "--single-transaction",
                "-d",
                url,
                str(file),
            ],
            what="pg_restore",
        )
    except FailedError as error:
        raise FailedError(
            f"{error}\n\nThe restore ran in one transaction and was rolled back: the database "
            f"{name} holds nothing from this dump. Fix the cause and run it again."
        ) from error

    _verify(url, listing=listing, wanted=wanted)
    print(f"restored {name} from {file}; schema revision {wanted or 'unknown'}")
    print("Start the build that expects this revision, or run `make migrate` for a newer one.")


def _verify(url: str, *, listing: str, wanted: str | None) -> None:
    with _connect(url) as conn:
        if wanted is not None:
            found = _scalar(conn, "SELECT version_num FROM alembic_version")
            if found != wanted:
                raise FailedError(
                    f"the restored schema revision is {found!r}, and the dump's "
                    f"is {wanted!r}. Do not start the app on this database."
                )
        if not grant_entries(listing):
            raise FailedError(
                "the data is restored, but this dump carries no privileges (it was made with "
                "--no-privileges), so the roles the app runs its statements as can read "
                "nothing and the app will answer 500. From a checkout of the build this "
                "dump came from, run:\n\n"
                "    python scripts/db_backup.py regrant --url <this database>\n"
            )
        granted = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee NOT IN ('PUBLIC', current_user)"
            )
        ]
        blind = [str(row[0]) for row in conn.execute(_ROLES_THAT_CANNOT_READ, (granted,))]
        if blind:
            raise FailedError(
                f"these roles were granted to and still cannot read: {', '.join(blind)}"
            )


def regrant(url: str) -> None:
    """Copy the grants of an empty database built by the migrations, at the
    target's own revision, onto the target."""
    url = libpq_url(url)
    require_matching_tools(url)
    with _connect(url) as conn:
        has_table = _scalar(conn, "SELECT to_regclass('alembic_version') IS NOT NULL")
        found = _scalar(conn, "SELECT version_num FROM alembic_version") if has_table else None
    if found is None:
        raise RefusedError("the database has no alembic_version row; it was not built by this app")
    revision = str(found)
    scratch = with_database(url, f"alkera_regrant_{secrets.token_hex(4)}")
    reference = Path(os.environ.get("TMPDIR", "/tmp")) / f"{database_of(scratch)}.dump"  # noqa: S108
    _create(scratch, drop_first=False)
    try:
        sqlalchemy_url = scratch.replace("postgresql://", "postgresql+psycopg://", 1)
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=REPO / "apps" / "backend",
            env={
                **os.environ,
                "DATABASE_URL_SYNC": sqlalchemy_url,
                "DATABASE_URL": scratch.replace("postgresql://", "postgresql+asyncpg://", 1),
            },
            text=True,
            capture_output=True,
        )
        if done.returncode != 0:
            raise FailedError(
                f"this checkout could not build revision {revision} (is it the build the dump "
                f"came from?):\n{done.stdout[-2000:]}{done.stderr[-2000:]}"
            )
        _run(
            [
                _tool("pg_dump"),
                "-Fc",
                "--schema-only",
                "--no-owner",
                "-d",
                scratch,
                "-f",
                str(reference),
            ],
            what="pg_dump of the reference schema",
        )
        entries = grant_entries(_listing(reference))
        if not entries:
            raise FailedError(f"the migrations grant nothing at revision {revision}")
        listing = Path(str(reference) + ".list")
        listing.write_text("\n".join(entries) + "\n", encoding="utf-8")
        roles, memberships, dumped_as = read_roles(scratch)
        script = Path(str(reference) + ROLES_SUFFIX)
        script.write_text(roles_script(roles, memberships, dumped_as=dumped_as), encoding="utf-8")
        _run(
            [
                _tool("psql"),
                "-X",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "--single-transaction",
                "-d",
                url,
                "-f",
                str(script),
            ],
            what="creating the roles",
        )
        _run(
            [
                _tool("pg_restore"),
                "--no-owner",
                "--exit-on-error",
                "--single-transaction",
                "-L",
                str(listing),
                "-d",
                url,
                str(reference),
            ],
            what="applying the grants",
        )
        print(f"applied {len(entries)} privilege entries of revision {revision}")
    finally:
        for leftover in (
            reference,
            Path(str(reference) + ".list"),
            Path(str(reference) + ROLES_SUFFIX),
        ):
            leftover.unlink(missing_ok=True)
        with _connect(with_database(url, "postgres")) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_of(scratch)))
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="db_backup.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore", "regrant"):
        command = commands.add_parser(name)
        command.add_argument("--url", required=True, help="the database (libpq or SQLAlchemy URL)")
        if name != "regrant":
            command.add_argument("--file", required=True, type=Path)
        if name == "restore":
            command.add_argument(
                "--recreate", action="store_true", help="drop and recreate the target"
            )
            command.add_argument("--confirm", help="the database name, to confirm --recreate")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            backup(args.url, args.file)
        elif args.command == "restore":
            restore(args.url, args.file, recreate=args.recreate, confirm=args.confirm)
        else:
            regrant(args.url)
    except RefusedError as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except FailedError as error:
        print(f"failed: {error}", file=sys.stderr)
        return EXIT_FAILED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
