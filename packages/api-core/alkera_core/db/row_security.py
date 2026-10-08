"""The facts tenant isolation rests on, read from the database and judged.

Two sets of tables are policed, each by its own role and policy (an
:class:`IsolationTier`): the Files tables (``alkera_files_app``,
``files_tenant_isolation`` on ``alkera.org_id``) and the content tables every
authenticated request is held to (``alkera_tenant_app``, ``tenant_isolation``
on ``alkera_org_ids()``; see :mod:`alkera_core.db.tenant_session`).

Row-level security on a tier's tables binds only while three things hold, and
none of them is enforced by the code that relies on them:

* every table of the tier has row security ENABLED and FORCED, with the
  tier's policy on it;
* the role the tier's statements assume is neither a superuser nor
  ``BYPASSRLS`` — either would make every policy a no-op;
* the login the app connects as may assume that role (it is a member).

The login itself is a different matter: in production it is expected to
BYPASS row security (the platform windows and the cross-tenant seam read as
the login, and a login that is filtered reads every step-out window as empty).
For the Files tier its flags are recorded, not judged. For the content tier a
login that does not bypass is a problem of its own: the worker, the gateway and
every unauthenticated route read those tables as the login, and would read
them as empty.

The check is split so each half is testable on its own: :func:`take_snapshot`
reads the catalog (and makes one behavioural probe — a read under the tenant
role with no org set must see no row), and :func:`judge` is a pure function
from that snapshot to the list of problems. The backend runs both at boot and
from its readiness probe (``backend.services.infra.row_security_canary``).

:func:`app_login_statements` is the one spelling of the role setup a
deployment needs once, used by the ops bootstrap script and by the test tier
that runs the tenant-isolation suite as a non-superuser login.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The role every Files statement runs as (``SET LOCAL ROLE``). Spelled here as
#: well as in :mod:`alkera_core.files.repo`, which this module must not import
#: at load (the test harness imports it before the settings may be built); a
#: test pins that the two agree.
FILES_APP_ROLE = "alkera_files_app"
#: The policy every tenant table carries.
ISOLATION_POLICY = "files_tenant_isolation"
#: The GUC the policy compares ``org_team_id`` against.
ORG_SETTING = "alkera.org_id"
#: The Files tables that hold no tenant rows and so carry no policy on purpose.
PLATFORM_TABLES: frozenset[str] = frozenset({"file_stores", "file_sweep_shards", "file_platform"})
#: The Files tables whose names do not start with ``file_``.
UNPREFIXED_FILES_TABLES: frozenset[str] = frozenset(
    {"dedup_domains", "manifest_terms", "storage_usage_snapshots"}
)
#: The tenant table the behavioural probe reads.
PROBE_TABLE = "file_drives"

#: The role every authenticated request's transaction assumes. Spelled as well
#: in :mod:`alkera_core.db.tenant_session` and revision 0188; a test pins that
#: they agree.
TENANT_APP_ROLE = "alkera_tenant_app"
#: The policy every content table carries.
CONTENT_POLICY = "tenant_isolation"
#: The setting ``alkera_org_ids()`` reads.
ORG_IDS_SETTING = "alkera.org_ids"
#: Every content table and the column its policy compares with the request's
#: orgs. Adding a table here (with its policy in a revision) is all it takes
#: for the canary to hold the database to it. A private domain's table joins
#: through :func:`register_content_table`, beside its model, so the set always
#: matches the tables this process has loaded.
CONTENT_TABLES: dict[str, str] = {
    "workspace_objects": "org_team_id",
    "chat_messages": "org_team_id",
    "object_payload_rows": "org_team_id",
    "crdt_docs": "org_id",
    "crdt_updates": "org_id",
    "team_memberships": "org_team_id",
    "notebook_kernels": "org_id",
    "notebook_runs": "org_id",
    "notebook_edits": "org_id",
    "notebook_epoch_tails": "org_id",
    "notebook_peers": "org_id",
    "org_machines": "org_team_id",
    "org_machine_audiences": "org_team_id",
    "org_compute_settings": "org_team_id",
    "workspace_machine_moves": "org_team_id",
    "ssh_machine_endpoints": "org_team_id",
}
#: The content table the behavioural probe reads.
CONTENT_PROBE_TABLE = "workspace_objects"


def register_content_table(table: str, org_column: str) -> None:
    """Hold ``table`` to the content policy on ``org_column``. Registering the
    same pair again is a no-op; a second column for one table is refused."""
    current = CONTENT_TABLES.get(table)
    if current is not None and current != org_column:
        raise ValueError(f"{table} is already a content table on {current}")
    CONTENT_TABLES[table] = org_column


@dataclass(frozen=True, slots=True)
class IsolationTier:
    """One set of policed tables: the role their statements assume, the policy
    each carries, the setting that policy reads, the table the behavioural
    probe reads, and whether the login itself must bypass row security."""

    role: str
    policy: str
    setting: str
    probe_table: str
    login_must_bypass: bool


FILES_TIER = IsolationTier(
    role=FILES_APP_ROLE,
    policy=ISOLATION_POLICY,
    setting=ORG_SETTING,
    probe_table=PROBE_TABLE,
    login_must_bypass=False,
)
CONTENT_TIER = IsolationTier(
    role=TENANT_APP_ROLE,
    policy=CONTENT_POLICY,
    setting=ORG_IDS_SETTING,
    probe_table=CONTENT_PROBE_TABLE,
    login_must_bypass=True,
)


def tenant_tables(names: Iterable[str] | None = None) -> frozenset[str]:
    """Every table that must carry the isolation policy: the Files tables of
    the ORM metadata (or of ``names``) less the platform ones. A table added
    to the Files subsystem joins this set, and so the canary, by existing."""
    if names is None:
        from alkera_core.db.base import Base

        names = Base.metadata.tables
    return frozenset(
        name
        for name in names
        if (name.startswith("file_") or name in UNPREFIXED_FILES_TABLES)
        and name not in PLATFORM_TABLES
    )


def content_tables(names: Iterable[str] | None = None) -> frozenset[str]:
    """Every content table that must carry ``tenant_isolation``: those of
    :data:`CONTENT_TABLES` present in the ORM metadata (or in ``names``)."""
    if names is None:
        from alkera_core.db.base import Base

        names = Base.metadata.tables
    return frozenset(name for name in names if name in CONTENT_TABLES)


def content_tier_tables(names: Iterable[str] | None = None) -> frozenset[str]:
    """Every table the tenant role's ``tenant_isolation`` must be on: the
    content tables, and the Files tenant tables (which a request bound to
    several orgs, stamping no single ``alkera.org_id``, reaches through it)."""
    return content_tables(names) | tenant_tables(names)


@dataclass(frozen=True, slots=True)
class RoleFacts:
    name: str
    exists: bool
    superuser: bool
    bypass_rls: bool


@dataclass(frozen=True, slots=True)
class TableFacts:
    name: str
    exists: bool
    rls_enabled: bool
    rls_forced: bool
    has_policy: bool


@dataclass(frozen=True, slots=True)
class RowSecuritySnapshot:
    """What the catalog (and one probe) said. ``probe_saw_rows`` is ``None``
    when the probe could not run (the role is missing or not assumable)."""

    login: RoleFacts
    app_role: RoleFacts
    login_may_assume_app_role: bool
    tables: tuple[TableFacts, ...]
    probe_saw_rows: bool | None
    tier: IsolationTier = FILES_TIER


def judge(snapshot: RowSecuritySnapshot, *, expected: Iterable[str]) -> list[str]:
    """Every reason tenant isolation cannot be trusted on this database, in a
    stable order; empty when it can. Pure."""
    problems: list[str] = []
    tier = snapshot.tier
    role = snapshot.app_role
    login = snapshot.login
    if tier.login_must_bypass and not (login.superuser or login.bypass_rls):
        problems.append(
            f"login {login.name} does not bypass row-level security, so work outside "
            f"{role.name} reads the {tier.policy} tables as empty"
        )
    if not role.exists:
        problems.append(f"role {role.name} does not exist")
    else:
        if role.superuser:
            problems.append(f"role {role.name} is a superuser")
        if role.bypass_rls:
            problems.append(f"role {role.name} bypasses row-level security")
        if not snapshot.login_may_assume_app_role:
            problems.append(f"login {snapshot.login.name} may not assume role {role.name}")
    seen = {table.name: table for table in snapshot.tables}
    for name in sorted(expected):
        table = seen.get(name)
        if table is None or not table.exists:
            problems.append(f"tenant table {name} does not exist")
            continue
        if not table.rls_enabled:
            problems.append(f"tenant table {name} does not enable row-level security")
        if not table.rls_forced:
            problems.append(f"tenant table {name} does not force row-level security")
        if not table.has_policy:
            problems.append(f"tenant table {name} has no {tier.policy} policy")
    if snapshot.probe_saw_rows:
        problems.append(f"role {role.name} read {tier.probe_table} rows with no org set")
    return problems


async def take_snapshot(
    db: AsyncSession, *, expected: Iterable[str], tier: IsolationTier = FILES_TIER
) -> RowSecuritySnapshot:
    """Read the facts :func:`judge` decides on for ``tier``. The probe runs
    inside a savepoint that is always rolled back, so the caller's transaction
    is left as it was found (bar the savepoint's own statements)."""
    names = sorted(expected)
    login_row = (
        await db.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).one()
    login = RoleFacts(
        name=str(login_row[0]), exists=True, superuser=login_row[1], bypass_rls=login_row[2]
    )
    role_row = (
        await db.execute(
            text(
                "SELECT rolsuper, rolbypassrls, pg_has_role(current_user, oid, 'MEMBER') "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": tier.role},
        )
    ).one_or_none()
    app_role = (
        RoleFacts(name=tier.role, exists=False, superuser=False, bypass_rls=False)
        if role_row is None
        else RoleFacts(name=tier.role, exists=True, superuser=role_row[0], bypass_rls=role_row[1])
    )
    may_assume = bool(role_row[2]) if role_row is not None else False
    rows = (
        await db.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "  EXISTS (SELECT 1 FROM pg_policy p "
                "          WHERE p.polrelid = c.oid AND p.polname = :policy) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') "
                "  AND c.relname = ANY(:names)"
            ),
            {"policy": tier.policy, "names": names},
        )
    ).all()
    found = {
        str(r[0]): TableFacts(
            name=str(r[0]), exists=True, rls_enabled=r[1], rls_forced=r[2], has_policy=r[3]
        )
        for r in rows
    }
    tables = tuple(
        found.get(
            name,
            TableFacts(name, exists=False, rls_enabled=False, rls_forced=False, has_policy=False),
        )
        for name in names
    )
    probe: bool | None = None
    if app_role.exists and may_assume and tier.probe_table in found:
        _require_identifier(tier.role)
        _require_identifier(tier.probe_table)
        await db.execute(text("SAVEPOINT row_security_canary"))
        try:
            # Both names were just matched against a plain-identifier pattern.
            await db.execute(text(f"SET LOCAL ROLE {tier.role}"))
            await db.execute(text("SELECT set_config(:name, '', true)"), {"name": tier.setting})
            probe_sql = f"SELECT EXISTS (SELECT 1 FROM {tier.probe_table})"  # noqa: S608
            probe = bool((await db.execute(text(probe_sql))).scalar())
        finally:
            await db.execute(text("ROLLBACK TO SAVEPOINT row_security_canary"))
            await db.execute(text("RELEASE SAVEPOINT row_security_canary"))
    return RowSecuritySnapshot(
        login=login,
        app_role=app_role,
        login_may_assume_app_role=may_assume,
        tables=tables,
        probe_saw_rows=probe,
        tier=tier,
    )


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def app_login_statements(login: str, *, database: str, password: str | None = None) -> list[str]:
    """The idempotent SQL that makes ``login`` the runtime login a deployment
    expects: it may log in, is not a superuser, bypasses row security (the
    platform windows and the cross-tenant seam read as the login), may assume
    both tenant roles (the Files one and the one every authenticated request
    assumes), and holds DML on every table and sequence.

    Run once per database by a role that may grant ``BYPASSRLS`` (a superuser,
    or on RDS the master user), after ``alembic upgrade head`` — the tenant
    role is created by the migrations. Re-running changes nothing. Tenant
    isolation never rests on the login: it rests on the tenant role, which
    the boot canary checks.
    """
    return app_login_role_statements(login, password=password) + app_login_grant_statements(
        login, database=database
    )


def app_login_role_statements(login: str, *, password: str | None = None) -> list[str]:
    """The cluster-wide half of :func:`app_login_statements`: the role itself
    and its membership of both tenant roles."""
    _require_identifier(login)
    secret = "" if password is None else " PASSWORD " + _literal(password)
    # The name was just matched against a plain-identifier pattern, which is
    # what makes spelling it into DDL safe: DDL takes no bind parameters.
    return [
        "DO $$ BEGIN "  # noqa: S608 - identifier validated above; DDL has no binds
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') THEN "
        f"CREATE ROLE {login} LOGIN; END IF; END $$",
        f"ALTER ROLE {login} LOGIN NOSUPERUSER NOCREATEROLE BYPASSRLS{secret}",
        f"GRANT {FILES_APP_ROLE} TO {login}",
        f"GRANT {TENANT_APP_ROLE} TO {login}",
    ]


def app_login_grant_statements(login: str, *, database: str) -> list[str]:
    """The per-database half of :func:`app_login_statements`: what the login
    may touch in ``database``. Run while connected to that database."""
    _require_identifier(login)
    _require_identifier(database)
    return [
        f"GRANT CONNECT ON DATABASE {database} TO {login}",
        f"GRANT USAGE ON SCHEMA public TO {login}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {login}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {login}",
        # Tables a later migration creates (as the role running this, which is
        # the role that migrates) reach the login without a second setup.
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO {login}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {login}",
    ]


def _require_identifier(name: str) -> None:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"not a plain identifier: {name!r}")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def describe(snapshot: RowSecuritySnapshot) -> dict[str, object]:
    """The snapshot as log fields: names and flags only, never a DSN."""
    return {
        "tier": snapshot.tier.role,
        "login": snapshot.login.name,
        "login_superuser": snapshot.login.superuser,
        "login_bypass_rls": snapshot.login.bypass_rls,
        "app_role_exists": snapshot.app_role.exists,
        "login_may_assume_app_role": snapshot.login_may_assume_app_role,
        "tenant_tables": len(snapshot.tables),
    }


def main(argv: list[str] | None = None) -> int:
    """Print :func:`app_login_statements` as a script for ``psql``.

    ``python -m alkera_core.db.row_security --login alkera_app --database alkera``
    — pipe it into ``psql`` as the master user once per database, after the
    first ``alembic upgrade head``. The password is not taken here: set it
    with ``\\password`` in the same session, so it is never in a shell
    history or a process list."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m alkera_core.db.row_security")
    parser.add_argument("--login", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    for statement in app_login_statements(args.login, database=args.database):
        print(f"{statement};")
    return 0


__all__ = [
    "CONTENT_POLICY",
    "CONTENT_PROBE_TABLE",
    "CONTENT_TABLES",
    "CONTENT_TIER",
    "FILES_APP_ROLE",
    "FILES_TIER",
    "ISOLATION_POLICY",
    "ORG_IDS_SETTING",
    "ORG_SETTING",
    "PLATFORM_TABLES",
    "PROBE_TABLE",
    "TENANT_APP_ROLE",
    "UNPREFIXED_FILES_TABLES",
    "IsolationTier",
    "RoleFacts",
    "RowSecuritySnapshot",
    "TableFacts",
    "app_login_grant_statements",
    "app_login_role_statements",
    "app_login_statements",
    "content_tables",
    "content_tier_tables",
    "describe",
    "judge",
    "main",
    "register_content_table",
    "take_snapshot",
    "tenant_tables",
]


if __name__ == "__main__":
    raise SystemExit(main())
