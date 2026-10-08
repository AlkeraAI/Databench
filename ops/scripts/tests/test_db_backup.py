"""The decisions inside ``scripts/db_backup.py``, on plain data.

What the script does to a real database (a dump that keeps its grants, a
restore the tenant roles can read through, the refusal of a target that holds
anything) is driven against Postgres in
``apps/backend/tests/test_db_backup_restore.py``. These pin the pure half:
which targets a restore may write, what the roles file says, which dump entries
are privileges, and how versions and revisions are read.
"""

from __future__ import annotations

import db_backup
import pytest


@pytest.mark.parametrize(
    ("exists", "relations", "recreate", "confirm", "expected"),
    [
        pytest.param(False, 0, False, None, "create", id="missing-database-is-created"),
        pytest.param(True, 0, False, None, "use", id="empty-database-is-used"),
        pytest.param(True, 41, True, "alkera", "recreate", id="confirmed-recreate"),
        pytest.param(True, 0, True, "alkera", "recreate", id="recreate-of-an-empty-one"),
        pytest.param(False, 0, True, "alkera", "create", id="recreate-of-a-missing-one"),
    ],
)
def test_what_a_restore_does_to_its_target(
    exists: bool, relations: int, recreate: bool, confirm: str | None, expected: str
) -> None:
    assert (
        db_backup.may_write(
            exists=exists, relations=relations, recreate=recreate, confirm=confirm, name="alkera"
        )
        == expected
    )


@pytest.mark.parametrize(
    ("relations", "recreate", "confirm", "says"),
    [
        pytest.param(1, False, None, r"is not empty \(1 tables", id="one-table-is-not-empty"),
        pytest.param(412, False, "alkera", "is not empty", id="a-confirm-alone-drops-nothing"),
        pytest.param(412, True, None, "--confirm alkera", id="recreate-without-a-confirm"),
        pytest.param(412, True, "alkera_prod", "--confirm alkera", id="confirm-names-another"),
        pytest.param(0, True, "", "--confirm alkera", id="empty-confirm"),
    ],
)
def test_a_restore_refuses_a_target_it_would_damage(
    relations: int, recreate: bool, confirm: str | None, says: str
) -> None:
    with pytest.raises(db_backup.RefusedError, match=says):
        db_backup.may_write(
            exists=True, relations=relations, recreate=recreate, confirm=confirm, name="alkera"
        )


def test_the_roles_file_creates_missing_roles_and_alters_none() -> None:
    script = db_backup.roles_script(
        [
            db_backup.Role("alkera_tenant_app", can_login=False, bypass_rls=False),
            db_backup.Role("alkera_app", can_login=True, bypass_rls=True),
        ],
        [
            db_backup.Membership("alkera_tenant_app", "alkera_app"),
            db_backup.Membership("alkera_tenant_app", "migrator"),
        ],
        dumped_as="migrator",
    )
    statements = [line for line in script.splitlines() if not line.startswith("--")]
    assert statements == [
        "DO $roles$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alkera_app') "
        'THEN CREATE ROLE "alkera_app" LOGIN NOSUPERUSER BYPASSRLS; END IF; END $roles$;',
        "DO $roles$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        "'alkera_tenant_app') THEN CREATE ROLE \"alkera_tenant_app\" NOLOGIN NOSUPERUSER "
        "NOBYPASSRLS; END IF; END $roles$;",
        "DO $roles$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alkera_app') "
        'THEN GRANT "alkera_tenant_app" TO "alkera_app"; END IF; END $roles$;',
        # Whoever restores stands where the dumping login stood.
        'GRANT "alkera_tenant_app" TO CURRENT_USER;',
    ]
    assert "ALTER ROLE" not in script
    assert "PASSWORD" not in script
    assert " SUPERUSER" not in script


def test_the_roles_file_quotes_names() -> None:
    script = db_backup.roles_script(
        [db_backup.Role('odd"name', can_login=False, bypass_rls=False)], [], dumped_as="m"
    )
    assert "rolname = 'odd\"name'" in script
    assert 'CREATE ROLE "odd""name" NOLOGIN' in script
    quoted = db_backup.roles_script(
        [db_backup.Role("o'brien", can_login=False, bypass_rls=False)], [], dumped_as="m"
    )
    assert "rolname = 'o''brien'" in quoted


def test_a_role_name_that_could_close_the_block_is_refused() -> None:
    with pytest.raises(db_backup.RefusedError, match="dollar sign"):
        db_backup.roles_script(
            [db_backup.Role("x$roles$; DROP", can_login=False, bypass_rls=False)],
            [],
            dumped_as="m",
        )


_LISTING = """\
;
; Archive created at 2026-10-06 01:34:01 PDT
;     dbname: alkera
;
215; 1259 16402 TABLE public file_drives alkera
4012; 0 0 ACL public TABLE file_drives alkera
4013; 0 0 ACL - SCHEMA public pg_database_owner
3890; 0 16402 TABLE DATA public file_drives alkera
2301; 826 17001 DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES alkera
3601; 3256 17050 POLICY public file_drives files_tenant_isolation alkera
3602; 0 16402 ROW SECURITY public file_drives alkera
4100; 0 0 COMMENT public TABLE acl_notes alkera
"""


def test_the_privilege_entries_of_a_dump_are_its_acls_and_default_acls() -> None:
    assert db_backup.grant_entries(_LISTING) == [
        "4012; 0 0 ACL public TABLE file_drives alkera",
        "4013; 0 0 ACL - SCHEMA public pg_database_owner",
        "2301; 826 17001 DEFAULT ACL public DEFAULT PRIVILEGES FOR TABLES alkera",
    ]


def test_a_dump_made_without_privileges_has_no_privilege_entries() -> None:
    without = "\n".join(line for line in _LISTING.splitlines() if " ACL " not in line)
    assert db_backup.grant_entries(without) == []


@pytest.mark.parametrize(
    ("output", "revision"),
    [
        pytest.param(
            "SET x = 1;\nCOPY public.alembic_version (version_num) FROM stdin;\n0176\n\\.\n",
            "0176",
            id="a-stamped-dump",
        ),
        pytest.param(
            "COPY public.alembic_version (version_num) FROM stdin;\n\\.\n", None, id="no-row"
        ),
        pytest.param("SET x = 1;\n", None, id="no-table"),
    ],
)
def test_the_revision_is_read_from_the_dump(output: str, revision: str | None) -> None:
    assert db_backup.revision_in(output) == revision


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("pg_dump (PostgreSQL) 16.4", 16, id="client"),
        pytest.param("pg_restore (PostgreSQL) 18.6 (Homebrew)", 18, id="client-with-a-suffix"),
        pytest.param("pg_dump (PostgreSQL) 17beta2", 17, id="beta"),
        pytest.param("160014", 16, id="server-version-num"),
        pytest.param("90624", 9, id="old-server"),
    ],
)
def test_major_version(text: str, expected: int) -> None:
    assert db_backup.major(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "postgresql+psycopg://u:p@h:5432/alkera", "postgresql://u:p@h:5432/alkera", id="psycopg"
        ),
        pytest.param(
            "postgresql+asyncpg://u:p@h/alkera?sslmode=require",
            "postgresql://u:p@h/alkera?sslmode=require",
            id="asyncpg-with-a-query",
        ),
        pytest.param("postgresql://u@h/alkera", "postgresql://u@h/alkera", id="already-plain"),
    ],
)
def test_a_sqlalchemy_url_becomes_one_the_tools_accept(url: str, expected: str) -> None:
    assert db_backup.libpq_url(url) == expected


def test_a_url_for_another_engine_is_refused() -> None:
    with pytest.raises(db_backup.RefusedError, match="not a Postgres URL"):
        db_backup.libpq_url("mysql://u@h/alkera")


def test_the_database_is_read_from_and_swapped_in_the_url() -> None:
    url = "postgresql://u:p@h:5432/alkera?sslmode=require"
    assert db_backup.database_of(url) == "alkera"
    assert (
        db_backup.with_database(url, "postgres")
        == "postgresql://u:p@h:5432/postgres?sslmode=require"
    )
    with pytest.raises(db_backup.RefusedError, match="names no database"):
        db_backup.database_of("postgresql://u@h")
