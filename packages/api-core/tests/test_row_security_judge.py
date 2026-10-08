"""The tenant-isolation canary's judgement, as a pure function of a snapshot.

Every fact the judge reads is turned off here one at a time from a snapshot
that passes, and the problem it names is pinned — so a check that stopped
reading a fact, or read it inverted, fails by name.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from alkera_core.db.row_security import (
    CONTENT_POLICY,
    CONTENT_PROBE_TABLE,
    CONTENT_TABLES,
    CONTENT_TIER,
    FILES_APP_ROLE,
    PLATFORM_TABLES,
    PROBE_TABLE,
    TENANT_APP_ROLE,
    RoleFacts,
    RowSecuritySnapshot,
    TableFacts,
    app_login_statements,
    content_tables,
    judge,
    tenant_tables,
)

EXPECTED = ("file_drives", "file_nodes")


def _table(name: str, **over: bool) -> TableFacts:
    facts = {"exists": True, "rls_enabled": True, "rls_forced": True, "has_policy": True, **over}
    return TableFacts(name=name, **facts)


HEALTHY = RowSecuritySnapshot(
    login=RoleFacts(name="alkera_app", exists=True, superuser=False, bypass_rls=True),
    app_role=RoleFacts(name=FILES_APP_ROLE, exists=True, superuser=False, bypass_rls=False),
    login_may_assume_app_role=True,
    tables=tuple(_table(name) for name in EXPECTED),
    probe_saw_rows=False,
)


def _with_table(name: str, **over: bool) -> RowSecuritySnapshot:
    tables = tuple(_table(t.name, **over) if t.name == name else t for t in HEALTHY.tables)
    return replace(HEALTHY, tables=tables)


def test_a_healthy_database_has_no_problems() -> None:
    assert judge(HEALTHY, expected=EXPECTED) == []


@pytest.mark.parametrize(
    "login",
    [
        pytest.param(RoleFacts("root", True, True, True), id="superuser-login"),
        pytest.param(RoleFacts("app", True, False, False), id="filtered-login"),
    ],
)
def test_the_login_is_recorded_not_judged(login: RoleFacts) -> None:
    """Isolation rests on the tenant role; the login is expected to bypass row
    security in production and is not what makes the policies bind."""
    assert judge(replace(HEALTHY, login=login), expected=EXPECTED) == []


@pytest.mark.parametrize(
    ("snapshot", "problem"),
    [
        pytest.param(
            replace(HEALTHY, app_role=replace(HEALTHY.app_role, exists=False)),
            f"role {FILES_APP_ROLE} does not exist",
            id="role-missing",
        ),
        pytest.param(
            replace(HEALTHY, app_role=replace(HEALTHY.app_role, superuser=True)),
            f"role {FILES_APP_ROLE} is a superuser",
            id="role-superuser",
        ),
        pytest.param(
            replace(HEALTHY, app_role=replace(HEALTHY.app_role, bypass_rls=True)),
            f"role {FILES_APP_ROLE} bypasses row-level security",
            id="role-bypassrls",
        ),
        pytest.param(
            replace(HEALTHY, login_may_assume_app_role=False),
            f"login alkera_app may not assume role {FILES_APP_ROLE}",
            id="login-not-a-member",
        ),
        pytest.param(
            _with_table("file_nodes", exists=False),
            "tenant table file_nodes does not exist",
            id="table-missing",
        ),
        pytest.param(
            _with_table("file_nodes", rls_enabled=False),
            "tenant table file_nodes does not enable row-level security",
            id="rls-disabled",
        ),
        pytest.param(
            _with_table("file_nodes", rls_forced=False),
            "tenant table file_nodes does not force row-level security",
            id="rls-not-forced",
        ),
        pytest.param(
            _with_table("file_nodes", has_policy=False),
            "tenant table file_nodes has no files_tenant_isolation policy",
            id="policy-missing",
        ),
        pytest.param(
            replace(HEALTHY, probe_saw_rows=True),
            f"role {FILES_APP_ROLE} read {PROBE_TABLE} rows with no org set",
            id="probe-saw-rows",
        ),
        pytest.param(
            replace(HEALTHY, tables=HEALTHY.tables[:1]),
            "tenant table file_nodes does not exist",
            id="table-absent-from-the-snapshot",
        ),
    ],
)
def test_each_fact_names_its_own_problem(snapshot: RowSecuritySnapshot, problem: str) -> None:
    assert judge(snapshot, expected=EXPECTED) == [problem]


def test_an_unrun_probe_is_not_a_problem_of_its_own() -> None:
    """The probe cannot run when the role is missing; the missing role is the
    problem, not a second invented one."""
    snapshot = replace(
        HEALTHY, app_role=replace(HEALTHY.app_role, exists=False), probe_saw_rows=None
    )
    assert judge(snapshot, expected=EXPECTED) == [f"role {FILES_APP_ROLE} does not exist"]


def test_the_registry_is_every_files_table_less_the_platform_ones() -> None:
    names = [
        "file_nodes",
        "file_stores",
        "dedup_domains",
        "storage_usage_snapshots",
        "users",
        "workspace_objects",
        "file_platform",
    ]
    assert tenant_tables(names) == {"file_nodes", "dedup_domains", "storage_usage_snapshots"}
    assert not PLATFORM_TABLES & tenant_tables()
    assert PROBE_TABLE in tenant_tables()


def test_the_role_name_is_the_one_the_files_repo_assumes() -> None:
    from alkera_core.files.repo import APP_ROLE

    assert APP_ROLE == FILES_APP_ROLE


def test_the_login_setup_is_idempotent_and_never_a_superuser() -> None:
    statements = app_login_statements("alkera_app", database="alkera", password="s'cret")
    joined = "\n".join(statements)
    assert "IF NOT EXISTS" in statements[0], "creating the role twice must not fail"
    assert "NOSUPERUSER" in joined and "BYPASSRLS" in joined
    assert f"GRANT {FILES_APP_ROLE} TO alkera_app" in statements
    assert "PASSWORD 's''cret'" in joined, "a quote in the password is escaped"
    assert "PASSWORD" not in "\n".join(app_login_statements("alkera_app", database="alkera"))


@pytest.mark.parametrize(
    ("login", "database"),
    [
        pytest.param("app; DROP TABLE users", "alkera", id="login"),
        pytest.param("alkera_app", "alkera'--", id="database"),
        pytest.param("Alkera", "alkera", id="uppercase"),
    ],
)
def test_the_login_setup_refuses_a_name_that_is_not_a_plain_identifier(
    login: str, database: str
) -> None:
    with pytest.raises(ValueError, match="plain identifier"):
        app_login_statements(login, database=database)


def test_the_login_setup_reaches_tables_a_later_migration_creates() -> None:
    joined = "\n".join(app_login_statements("alkera_app", database="alkera"))
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in joined
    assert "ON TABLES TO alkera_app" in joined and "ON SEQUENCES TO alkera_app" in joined


def test_the_script_entry_point_prints_the_statements_for_psql(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alkera_core.db.row_security import main

    assert main(["--login", "alkera_app", "--database", "alkera"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == [
        f"{statement};" for statement in app_login_statements("alkera_app", database="alkera")
    ]
    assert not any("PASSWORD" in line for line in printed), "no secret on a command line"


# ---- the content tier ---------------------------------------------------------

CONTENT_EXPECTED = ("workspace_objects", "chat_messages")

CONTENT_HEALTHY = RowSecuritySnapshot(
    login=RoleFacts(name="alkera_app", exists=True, superuser=False, bypass_rls=True),
    app_role=RoleFacts(name=TENANT_APP_ROLE, exists=True, superuser=False, bypass_rls=False),
    login_may_assume_app_role=True,
    tables=tuple(_table(name) for name in CONTENT_EXPECTED),
    probe_saw_rows=False,
    tier=CONTENT_TIER,
)


def test_a_healthy_content_tier_has_no_problems() -> None:
    assert judge(CONTENT_HEALTHY, expected=CONTENT_EXPECTED) == []


@pytest.mark.parametrize(
    ("snapshot", "problem"),
    [
        pytest.param(
            replace(
                CONTENT_HEALTHY,
                login=RoleFacts("alkera_app", True, superuser=False, bypass_rls=False),
            ),
            "login alkera_app does not bypass row-level security, so work outside "
            f"{TENANT_APP_ROLE} reads the {CONTENT_POLICY} tables as empty",
            id="login-filtered",
        ),
        pytest.param(
            replace(CONTENT_HEALTHY, app_role=replace(CONTENT_HEALTHY.app_role, bypass_rls=True)),
            f"role {TENANT_APP_ROLE} bypasses row-level security",
            id="role-bypassrls",
        ),
        pytest.param(
            replace(CONTENT_HEALTHY, login_may_assume_app_role=False),
            f"login alkera_app may not assume role {TENANT_APP_ROLE}",
            id="login-not-a-member",
        ),
        pytest.param(
            replace(
                CONTENT_HEALTHY,
                tables=(CONTENT_HEALTHY.tables[0], _table("chat_messages", has_policy=False)),
            ),
            f"tenant table chat_messages has no {CONTENT_POLICY} policy",
            id="policy-missing",
        ),
        pytest.param(
            replace(CONTENT_HEALTHY, probe_saw_rows=True),
            f"role {TENANT_APP_ROLE} read {CONTENT_PROBE_TABLE} rows with no org set",
            id="probe-saw-rows",
        ),
    ],
)
def test_each_content_fact_names_its_own_problem(
    snapshot: RowSecuritySnapshot, problem: str
) -> None:
    assert judge(snapshot, expected=CONTENT_EXPECTED) == [problem]


def test_a_superuser_login_passes_the_content_tier() -> None:
    login = RoleFacts("root", True, superuser=True, bypass_rls=False)
    assert judge(replace(CONTENT_HEALTHY, login=login), expected=CONTENT_EXPECTED) == []


def test_the_content_registry_is_exactly_the_policed_tables() -> None:
    from alkera_core.db.base import Base

    assert content_tables() == set(CONTENT_TABLES)
    assert set(CONTENT_TABLES) <= set(Base.metadata.tables)
    assert content_tables(["workspace_objects", "users", "file_nodes"]) == {"workspace_objects"}
    assert not content_tables() & tenant_tables(), "a table answers to one tier"
    for table, column in CONTENT_TABLES.items():
        assert column in Base.metadata.tables[table].c, (table, column)


def test_the_content_role_name_is_the_one_the_binding_assumes() -> None:
    from alkera_core.db.tenant_session import TENANT_ROLE

    assert TENANT_ROLE == TENANT_APP_ROLE == CONTENT_TIER.role


def test_the_login_setup_makes_the_login_a_member_of_both_tenant_roles() -> None:
    statements = app_login_statements("alkera_app", database="alkera")
    assert f"GRANT {FILES_APP_ROLE} TO alkera_app" in statements
    assert f"GRANT {TENANT_APP_ROLE} TO alkera_app" in statements
