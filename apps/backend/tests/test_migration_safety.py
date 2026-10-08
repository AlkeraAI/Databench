"""Revisions 0136 onward as production runs them, against throwaway databases.

The per-revision modules beside this one prove WHAT each revision does to the
rows. This one proves the ways a migration goes wrong that only show on a
database with history, a bound login, other runners and live traffic:

* a login that row-level security binds must be REFUSED by a back-fill, with
  the remedy, and must leave the database exactly as it found it — never report
  success having matched nothing;
* the migration login must transform the rows: duplicates collapsed, boxes
  re-kinded, links copied, and on the way down links written back;
* a revision that died after committing half of itself runs again;
* a table somebody else has locked fails the run fast instead of queueing;
* two ``alembic upgrade head`` at once serialise, and both exit 0;
* a link written through the previous release's array during a roll becomes a
  row, and a detach made by this release is not undone by it.

Every database here is an ``alkera_migtest_*`` scratch database cloned from one
seeded template, driven through the real ``alembic`` console script and the real
``env.py``; the suite's own database is never touched.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alkera_core.config import settings
from backend.migration_safety import lint_revision
from sqlalchemy.engine import make_url

_BACKEND = Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "alembic" / "versions"
_BASE = make_url(settings.database_url_sync)
#: The last revision production has already run; everything after it is what
#: this module drives.
_SHIPPED = "0135"
_SHARE_INDEX = "uq_file_shares_live_principal"
_PROXY_INDEX = "ix_billing_proxy_requests_last_seen_at"
#: Repeated from ``backend.migration_runner``: the test holds the same lock a runner takes.
_MIGRATION_LOCK_KEY = 0x616C6B72

# The seeded template database is built once per module; spread over workers,
# each would build and drop its own.
pytestmark = pytest.mark.xdist_group("migration_safety")


def _alembic_script() -> str:
    resolved = shutil.which("alembic", path=str(Path(sys.executable).parent))
    assert resolved, "the alembic console script is not in the active venv"
    return resolved


def _libpq(database: str, *, user: str | None = None, password: str | None = None) -> str:
    url = _BASE.set(drivername="postgresql", database=database)
    if user is not None:
        url = url.set(username=user, password=password)
    return url.render_as_string(hide_password=False)


@dataclass
class _Seed:
    """The ids the template database was seeded with."""

    org: uuid.UUID = field(default_factory=uuid.uuid4)
    user: uuid.UUID = field(default_factory=uuid.uuid4)
    shared_node: uuid.UUID = field(default_factory=uuid.uuid4)
    box_node: uuid.UUID = field(default_factory=uuid.uuid4)
    person_node: uuid.UUID = field(default_factory=uuid.uuid4)
    box: uuid.UUID = field(default_factory=uuid.uuid4)
    chat: uuid.UUID = field(default_factory=uuid.uuid4)
    first_link: uuid.UUID = field(default_factory=uuid.uuid4)
    second_link: uuid.UUID = field(default_factory=uuid.uuid4)
    third_link: uuid.UUID = field(default_factory=uuid.uuid4)
    #: Chats whose ``attachments`` key holds something that is not an array — a
    #: string, an object, JSON null — and one with no such key at all. Today's
    #: writer cannot produce them; a hand edit or an import can, and a walk that
    #: meets one must step over it rather than abort.
    odd_chats: dict[str, uuid.UUID] = field(
        default_factory=lambda: {
            shape: uuid.uuid4() for shape in ("string", "object", "null", "absent")
        }
    )


class _Runner:
    """A subprocess whose output is read as it is written, from the moment it starts.

    Two runners on one database finish in lock order, so the runner a test
    waits on first may be the one waiting for the other. Collected one at a
    time with ``communicate()``, that other runner's output sat unread in a
    pipe, and a pipe holds four kilobytes on Windows -- less than a full run's
    log since revision 0165. The winner stopped inside a write with the
    migration lock held, the loser kept waiting for the lock, and the test kept
    waiting for the loser, until the per-test timeout ended the whole xdist
    worker. With a reader thread per runner the pipe is never anyone's to
    drain, and the order a test collects runners in cannot matter.
    """

    def __init__(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str]) -> None:
        self.argv = list(argv)
        self.process = subprocess.Popen(
            self.argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._lines: list[str] = []
        self._failure: Exception | None = None
        self._reader = threading.Thread(
            target=self._read, name=f"runner-{self.process.pid}-output", daemon=True
        )
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._lines.append(line)
        except Exception as error:
            # Carried to the thread that calls finish(): a reader that died
            # quietly would hand back a truncated log as if it were the whole.
            self._failure = error
        finally:
            self.process.stdout.close()

    def poll(self) -> int | None:
        return self.process.poll()

    def finish(self, timeout: float = 240) -> tuple[int, str]:
        """The exit code and everything the runner wrote.

        A runner still going at ``timeout`` is killed before the error is
        raised, so it cannot outlive the test on a database about to be dropped.
        """
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            self._reader.join(timeout=10)
            raise subprocess.TimeoutExpired(
                self.argv, timeout, output="".join(self._lines)
            ) from None
        self._reader.join(timeout=60)
        if self._failure is not None:
            raise self._failure
        return code, "".join(self._lines)


@dataclass
class _Database:
    name: str
    seed: _Seed
    login: str | None = None
    password: str | None = None

    def connect(self) -> psycopg.Connection[Any]:
        """As the migration login, autocommit."""
        return psycopg.connect(_libpq(self.name), autocommit=True)

    def _env(self, overrides: dict[str, str]) -> dict[str, str]:
        def dsn(driver: str) -> str:
            url = _BASE.set(drivername=driver, database=self.name)
            if self.login is not None:
                url = url.set(username=self.login, password=self.password)
            return url.render_as_string(hide_password=False)

        return {
            **os.environ,
            "DATABASE_URL_SYNC": dsn("postgresql+psycopg"),
            "DATABASE_URL": dsn("postgresql+asyncpg"),
            **overrides,
        }

    def start(self, *args: str, **overrides: str) -> _Runner:
        return _Runner([_alembic_script(), *args], cwd=_BACKEND, env=self._env(overrides))

    def alembic(self, *args: str, **overrides: str) -> tuple[int, str]:
        return self.start(*args, **overrides).finish()

    def must(self, *args: str, **overrides: str) -> str:
        code, output = self.alembic(*args, **overrides)
        assert code == 0, f"alembic {' '.join(args)} failed:\n{output}"
        return output

    def scalar(self, sql: str, *params: Any) -> Any:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return None if row is None else row[0]

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def revision(self) -> str:
        return str(self.scalar("SELECT version_num FROM alembic_version"))


def _drop(admin: psycopg.Connection[Any], name: str) -> None:
    admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )
    admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata) VALUES (%s, %s, %s, %s, NULL, 'folder', NULL, NULL, %s, "
    "'{}'::jsonb, CAST(%s AS ltree), 0, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, 0, false, '{}'::jsonb)"
)

#: A lease as the shipped schema took it: every holder ``'user'``, a box told
#: apart only by carrying its own id as its machine label.
_INSERT_LEASE = (
    "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
    "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
    "VALUES (%s, %s, 1, 'user', %s, %s, %s, 'chat', now() + interval '1 hour')"
)

_INSERT_SHARE = (
    "INSERT INTO file_shares (id, org_team_id, node_id, principal_kind, principal_id, "
    "role, granted_by) VALUES (%s, %s, %s, 'org', %s, %s, %s)"
)


def _seed(conn: psycopg.Connection[Any], seed: _Seed) -> None:
    """Rows of the shapes production holds at the shipped revision."""
    conn.execute("INSERT INTO teams (id, name) VALUES (%s, 'Safety Org')", (seed.org,))
    conn.execute(
        "INSERT INTO users (id, org_team_id, email) VALUES (%s, %s, %s)",
        (seed.user, seed.org, f"safety-{seed.user.hex}@example.test"),
    )
    store, domain, drive = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
        "VALUES (%s, 'filesystem', %s, '{}'::jsonb, ARRAY['proxied'])",
        (store, f"safety-{store.hex}"),
    )
    conn.execute(
        "INSERT INTO dedup_domains (id, org_team_id, store_id, chunker_seed) "
        "VALUES (%s, %s, %s, '\\x00'::bytea)",
        (domain, seed.org, store),
    )
    conn.execute(
        "INSERT INTO file_drives (id, org_team_id, store_id, dedup_domain_id, quota_bytes, "
        "quota_nodes, next_ino) VALUES (%s, %s, %s, %s, 0, 0, 1)",
        (drive, seed.org, store, domain),
    )
    for ino, node in enumerate((seed.shared_node, seed.box_node, seed.person_node), start=1):
        conn.execute(
            _INSERT_NODE,
            (node, ino, drive, seed.org, f"n{ino}".encode(), str(node).replace("-", "_")),
        )
    # The pair the old two-step role change left behind: the weaker rung first.
    for role in ("reader", "writer"):
        conn.execute(
            _INSERT_SHARE, (uuid.uuid4(), seed.org, seed.shared_node, seed.org, role, seed.user)
        )
    conn.execute(_INSERT_LEASE, (seed.box_node, seed.org, seed.box, uuid.uuid4(), str(seed.box)))
    conn.execute(
        _INSERT_LEASE, (seed.person_node, seed.org, seed.user, uuid.uuid4(), "somebody's laptop")
    )
    # An array as a real one looks: a repeat, and a value that was never an id.
    links = [
        str(seed.first_link),
        str(seed.second_link),
        str(seed.first_link),
        "not-a-node",
        str(seed.third_link),
    ]
    odd_specs = {
        "string": {"attachments": "nope"},
        "object": {"attachments": {"first": str(seed.first_link)}},
        "null": {"attachments": None},
        "absent": {"last_seq": 1},
    }
    for shape, chat in seed.odd_chats.items():
        conn.execute(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
            "visibility_scope, spec) VALUES (%s, %s, %s, 'chat', %s, 'private', %s::jsonb)",
            (chat, seed.org, f"chat-{chat.hex}", seed.user, json.dumps(odd_specs[shape])),
        )
    conn.execute(
        "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
        "visibility_scope, spec) VALUES (%s, %s, %s, 'chat', %s, 'private', %s::jsonb)",
        (
            seed.chat,
            seed.org,
            f"chat-{seed.chat.hex}",
            seed.user,
            json.dumps({"attachments": links}),
        ),
    )


@pytest.fixture(scope="module")
def _template() -> Iterator[tuple[str, _Seed]]:
    """One database migrated to the shipped revision and seeded; tests clone it."""
    name = f"alkera_migtest_safety_{secrets.token_hex(5)}"
    seed = _Seed()
    admin = psycopg.connect(_libpq("postgres"), autocommit=True)
    try:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}"')
        database = _Database(name, seed)
        database.must("upgrade", _SHIPPED)
        with database.connect() as conn:
            _seed(conn, seed)
        yield name, seed
    finally:
        _drop(admin, name)
        admin.close()


@pytest.fixture
def database(_template: tuple[str, _Seed]) -> Iterator[_Database]:
    template, seed = _template
    name = f"{template}_{secrets.token_hex(3)}"
    admin = psycopg.connect(_libpq("postgres"), autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        yield _Database(name, seed)
    finally:
        _drop(admin, name)
        admin.close()


@pytest.fixture
def bound_login(database: _Database) -> Iterator[_Database]:
    """The same database through a login row-level security binds.

    A member of the owning role, so it may run every ``ALTER TABLE`` a revision
    issues — but neither a superuser nor ``BYPASSRLS``, and neither attribute is
    inherited through membership. This is the shape of an RDS master or a
    least-privilege migration user.
    """
    login = f"alkera_migtest_bound_{secrets.token_hex(4)}"
    password = secrets.token_hex(8)
    admin = psycopg.connect(_libpq("postgres"), autocommit=True)
    try:
        admin.execute(f"CREATE ROLE {login} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'")
        admin.execute(f'GRANT "{_BASE.username}" TO {login}')
        yield _Database(database.name, database.seed, login=login, password=password)
    finally:
        # The role owns whatever it created in the scratch database, so that
        # goes first; the ``database`` fixture's own drop is then a no-op.
        _drop(admin, database.name)
        admin.execute(f"DROP ROLE IF EXISTS {login}")
        admin.close()


def _live_shares(database: _Database) -> list[str]:
    return [
        role
        for (role,) in database.rows(
            "SELECT role FROM file_shares WHERE node_id = %s AND revoked_at IS NULL ORDER BY role",
            database.seed.shared_node,
        )
    ]


def _index_state(database: _Database, name: str) -> tuple[bool, bool] | None:
    """``(valid, unique)``, or ``None`` when there is no such index."""
    found = database.rows(
        "SELECT indisvalid, indisunique FROM pg_index WHERE indexrelid = to_regclass(%s)", name
    )
    return (bool(found[0][0]), bool(found[0][1])) if found else None


def _columns(database: _Database, table: str) -> set[str]:
    return {
        name
        for (name,) in database.rows(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", table
        )
    }


def _links(database: _Database) -> list[tuple[str, int]]:
    return [
        (str(node), int(position))
        for node, position in database.rows(
            "SELECT node_id, position FROM chat_attachments WHERE chat_id = %s "
            "ORDER BY position, node_id",
            database.seed.chat,
        )
    ]


def _legacy_array(database: _Database) -> list[str]:
    return list(
        database.scalar(
            "SELECT spec -> 'attachments' FROM workspace_objects WHERE id = %s", database.seed.chat
        )
    )


# --- a bound login is refused, loudly, and changes nothing ---------------------------


@pytest.mark.parametrize(
    ("parent", "target", "table"),
    [
        pytest.param("0138", "0139", "file_leases", id="0139-lease-kinds"),
        pytest.param("0141", "0142", "file_shares", id="0142-share-collapse"),
    ],
)
def test_a_back_fill_refuses_a_login_that_cannot_see_the_rows(
    database: _Database, bound_login: _Database, parent: str, target: str, table: str
) -> None:
    database.must("upgrade", parent)

    # The premise: through this login the tenant rows are simply not there, so
    # an UPDATE would match nothing and say it succeeded.
    with psycopg.connect(
        _libpq(bound_login.name, user=bound_login.login, password=bound_login.password)
    ) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert database.scalar(f"SELECT count(*) FROM {table}") > 0

    code, output = bound_login.alembic("upgrade", target)

    assert code != 0, f"the back-fill ran blind and reported success:\n{output}"
    assert "RowSecurityBlocksMigrationError" in output
    assert table in output
    assert "BYPASSRLS" in output
    assert "does NOT bypass row-level security" in output
    # Refused before it touched anything: still stamped at the parent, the
    # duplicates still both live, no half of the schema change left behind.
    assert database.revision() == parent
    assert _live_shares(database) == ["reader", "writer"]
    assert _index_state(database, _SHARE_INDEX) is None
    if target == "0139":
        assert "holder_kind" not in _columns(database, "file_leases")


def test_a_bound_login_still_runs_the_revisions_that_rewrite_no_tenant_rows(
    database: _Database, bound_login: _Database
) -> None:
    # The refusal is per back-fill, not a blanket ban: 0138 only adds a column.
    output = bound_login.must("upgrade", "0138")

    assert "does NOT bypass row-level security" in output
    assert "lease_holder" in _columns(database, "file_upload_sessions")


# --- the migration login transforms the data, up and down ----------------------------


def test_the_migration_login_transforms_every_seeded_shape(database: _Database) -> None:
    seed = database.seed
    output = database.must("upgrade", "head")

    assert "bypasses row-level security" in output
    assert _live_shares(database) == ["writer"]
    assert (
        database.scalar(
            "SELECT count(*) FROM file_shares WHERE node_id = %s AND revoked_at IS NOT NULL",
            seed.shared_node,
        )
        == 1
    )
    assert _index_state(database, _SHARE_INDEX) == (True, True)
    assert dict(database.rows("SELECT node_id, holder_kind FROM file_leases")) == {
        seed.box_node: "machine",
        seed.person_node: "user",
    }
    assert database.scalar(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_file_leases_holder_kind'"
    )
    assert _index_state(database, _PROXY_INDEX) == (True, False)
    for name in ("ck_event_outbox_payload_size", "ck_chat_messages_payload_size"):
        definition, validated = database.rows(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conname = %s",
            name,
        )[0]
        assert str(2 * 1024 * 1024) in definition
        assert validated
    # The copy's three promises. A link's position is its place in the array,
    # so the third link keeps 5 and is not renumbered to 3; the node the array
    # named twice is one row at its FIRST mention; the value that was never a
    # node id is no row at all.
    assert _links(database) == [
        (str(seed.first_link), 1),
        (str(seed.second_link), 2),
        (str(seed.third_link), 5),
    ]
    # ...and the chats whose key is a string, an object, null or missing were
    # stepped over: no rows, no abort.
    assert database.scalar("SELECT count(*) FROM chat_attachments") == 3
    assert (
        database.scalar(
            "SELECT count(*) FROM chat_attachments WHERE chat_id = ANY(%s)",
            list(seed.odd_chats.values()),
        )
        == 0
    )


def test_a_back_fill_walks_the_table_in_ranges_and_reaches_every_row(
    database: _Database,
) -> None:
    # One row per committed statement: the walk has to cross every range edge
    # without dropping or repeating a row.
    seed = database.seed
    chats = {seed.chat: [str(seed.first_link), str(seed.second_link), str(seed.third_link)]}
    with database.connect() as conn:
        for _ in range(7):
            chat, node = uuid.uuid4(), uuid.uuid4()
            chats[chat] = [str(node)]
            conn.execute(
                "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, "
                "owner_user_id, visibility_scope, spec) "
                "VALUES (%s, %s, %s, 'chat', %s, 'private', %s::jsonb)",
                (
                    chat,
                    seed.org,
                    f"chat-{chat.hex}",
                    seed.user,
                    json.dumps({"attachments": [str(node)]}),
                ),
            )

    database.must("upgrade", "head", MIGRATION_BATCH_ROWS="1")

    copied: dict[uuid.UUID, list[str]] = {}
    for chat, node in database.rows(
        "SELECT chat_id, node_id FROM chat_attachments ORDER BY chat_id, position"
    ):
        copied.setdefault(chat, []).append(str(node))
    assert copied == chats


def test_the_downgrade_writes_the_links_back_where_the_previous_release_reads_them(
    database: _Database,
) -> None:
    seed = database.seed
    database.must("upgrade", "head")
    added = uuid.uuid4()
    with database.connect() as conn:
        # What this release does: an attach is a row, a detach deletes one. The
        # array is not touched by either.
        conn.execute(
            "INSERT INTO chat_attachments (chat_id, node_id, position) VALUES (%s, %s, 6)",
            (seed.chat, added),
        )
        conn.execute(
            "DELETE FROM chat_attachments WHERE chat_id = %s AND node_id = %s",
            (seed.chat, seed.first_link),
        )

    database.must("downgrade", _SHIPPED)

    survivors = [str(seed.second_link), str(seed.third_link), str(added)]
    assert _legacy_array(database) == survivors
    assert database.scalar("SELECT to_regclass('chat_attachments')") is None
    assert "holder_kind" not in _columns(database, "file_leases")
    assert _index_state(database, _SHARE_INDEX) is None
    assert "last_seen_at" not in _columns(database, "billing_proxy_requests")
    # ...and the way back up is still open.
    database.must("upgrade", "head")
    assert [node for node, _ in _links(database)] == survivors


# --- a link written through the previous release during the roll ---------------------


def _rewrite_array(database: _Database, links: list[str]) -> None:
    """An attach or detach as the previous release makes it: the whole array."""
    with database.connect() as conn:
        conn.execute(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{attachments}', %s::jsonb) "
            "WHERE id = %s",
            (json.dumps(links), database.seed.chat),
        )


def test_the_previous_release_attaching_and_detaching_during_the_roll_reaches_the_rows(
    database: _Database,
) -> None:
    seed = database.seed
    database.must("upgrade", "head")
    first, second, third = str(seed.first_link), str(seed.second_link), str(seed.third_link)
    late = str(uuid.uuid4())

    _rewrite_array(database, [first, second, third, late])
    assert _links(database) == [(first, 1), (second, 2), (third, 5), (late, 6)]

    _rewrite_array(database, [second, third, late])
    assert _links(database) == [(second, 2), (third, 5), (late, 6)]


def test_a_detach_by_this_release_is_not_undone_by_a_later_write_of_the_same_spec(
    database: _Database,
) -> None:
    seed = database.seed
    database.must("upgrade", "head")
    first, second, third = str(seed.first_link), str(seed.second_link), str(seed.third_link)
    late = str(uuid.uuid4())
    with database.connect() as conn:
        conn.execute(
            "DELETE FROM chat_attachments WHERE chat_id = %s AND node_id = %s", (seed.chat, first)
        )
        # The array still names the detached node. A write that leaves the
        # array as it was, and one that appends to it, must both leave it gone.
        conn.execute(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{last_seq}', '7') WHERE id = %s",
            (seed.chat,),
        )
    assert [node for node, _ in _links(database)] == [second, third]

    _rewrite_array(database, [first, second, first, "not-a-node", third, late])
    assert _links(database) == [(second, 2), (third, 5), (late, 6)]


def test_a_link_made_between_the_copy_and_the_trigger_is_adopted(database: _Database) -> None:
    seed = database.seed
    database.must("upgrade", "0144")
    gap = str(uuid.uuid4())
    # The previous release attaches after 0144 copied this chat and before 0145
    # put the trigger on: the link exists in the array only.
    before = [str(seed.first_link), str(seed.second_link), str(seed.third_link)]
    _rewrite_array(database, [*before, gap])
    assert gap not in [node for node, _ in _links(database)]

    database.must("upgrade", "0145")

    assert _links(database) == [(before[0], 1), (before[1], 2), (before[2], 5), (gap, 6)]


def test_a_write_that_carries_no_array_changes_no_link(database: _Database) -> None:
    # A future "strip the deprecated key" write, or a key holding something
    # else, is not a statement about the links; only an array is.
    seed = database.seed
    database.must("upgrade", "head")
    held = _links(database)
    assert len(held) == 3
    with database.connect() as conn:
        conn.execute(
            "UPDATE workspace_objects SET spec = spec - 'attachments' WHERE id = %s", (seed.chat,)
        )
        assert _links(database) == held
        conn.execute(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{attachments}', '\"nope\"') "
            "WHERE id = %s",
            (seed.chat,),
        )
        assert _links(database) == held
        conn.execute(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{attachments}', 'null') "
            "WHERE id = %s",
            (seed.chat,),
        )
        assert _links(database) == held
    # An array coming back after a non-array is measured against nothing, and
    # every id it names is already a row.
    _rewrite_array(database, [node for node, _ in held])
    assert _links(database) == held


def test_an_attach_by_the_previous_release_between_the_two_downgrade_steps_survives(
    database: _Database,
) -> None:
    # The documented rollback rolls the servers back first, so old servers are
    # attaching through the array while `downgrade 0143` runs. 0145's step
    # commits before 0144's write-back walks the chats; an attach in between
    # must reach the array the walk leaves behind, which it only does if the
    # trigger is still mirroring it into the rows the walk reads.
    seed = database.seed
    database.must("upgrade", "head")
    database.must("downgrade", "0144")
    late = str(uuid.uuid4())
    _rewrite_array(
        database, [str(seed.first_link), str(seed.second_link), str(seed.third_link), late]
    )
    assert late in [node for node, _ in _links(database)], "the trigger came off too early"

    database.must("downgrade", "0143")

    assert _legacy_array(database) == [
        str(seed.first_link),
        str(seed.second_link),
        str(seed.third_link),
        late,
    ]
    assert database.scalar("SELECT to_regclass('chat_attachments')") is None
    assert (
        database.scalar(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'chat_attachments_mirror_legacy_array'"
        )
        == 0
    )
    assert database.scalar("SELECT to_regproc('chat_attachments_adopt')") is None


# --- a revision that died half way runs again ----------------------------------------


def test_an_upgrade_that_died_half_way_is_finished_by_running_it_again(
    database: _Database,
) -> None:
    seed = database.seed
    database.must("upgrade", "0138")
    with database.connect() as conn:
        # 0139 died after its schema half committed.
        conn.execute(
            "ALTER TABLE file_leases ADD COLUMN holder_kind VARCHAR(16) NOT NULL DEFAULT 'user'"
        )
        conn.execute(
            "ALTER TABLE file_leases ADD CONSTRAINT ck_file_leases_holder_kind "
            "CHECK (holder_kind IN ('user')) NOT VALID"
        )
        # 0142's build met a duplicate that landed mid-build: the statement
        # fails and Postgres leaves an INVALID index under the name.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY {_SHARE_INDEX} ON file_shares "
                "(node_id, principal_kind, principal_id) WHERE revoked_at IS NULL"
            )
        # 0143 died after its column committed and before its index was built.
        conn.execute("ALTER TABLE billing_proxy_requests ADD COLUMN last_seen_at TIMESTAMPTZ")
        # 0144 died inside its copy: the table and the two columns are there,
        # no row has been copied, and the stamp still says 0143.
        conn.execute(
            "CREATE TABLE chat_attachments (chat_id UUID NOT NULL, node_id UUID NOT NULL, "
            "position BIGINT NOT NULL, created_at TIMESTAMPTZ DEFAULT now() NOT NULL, "
            "CONSTRAINT pk_chat_attachments PRIMARY KEY (chat_id, node_id), "
            "CONSTRAINT fk_chat_attachments_chat_id_workspace_objects FOREIGN KEY (chat_id) "
            "REFERENCES workspace_objects (id) ON DELETE CASCADE)"
        )
        conn.execute(
            "CREATE INDEX ix_chat_attachments_chat_id_position ON chat_attachments "
            "(chat_id, position, node_id)"
        )
        conn.execute("ALTER TABLE realtime_docs ADD COLUMN turn_state VARCHAR(32)")
        conn.execute("ALTER TABLE realtime_docs ADD COLUMN turn_state_at VARCHAR(64)")
    assert _index_state(database, _SHARE_INDEX) == (False, True)

    database.must("upgrade", "head")

    assert _index_state(database, _SHARE_INDEX) == (True, True)
    assert _live_shares(database) == ["writer"]
    assert _index_state(database, _PROXY_INDEX) == (True, False)
    assert (
        database.scalar("SELECT holder_kind FROM file_leases WHERE node_id = %s", seed.box_node)
        == "machine"
    )
    definition = database.scalar(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_file_leases_holder_kind'"
    )
    assert "machine" in definition
    assert [node for node, _ in _links(database)] == [
        str(seed.first_link),
        str(seed.second_link),
        str(seed.third_link),
    ]


_ALLOCATION_STATE_CHECK = "ck_compute_allocations_state"


def _allocation_states(database: _Database) -> str:
    return str(
        database.scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            _ALLOCATION_STATE_CHECK,
        )
    )


def test_a_run_that_fails_part_way_leaves_the_stamp_on_the_schema_it_committed(
    database: _Database,
) -> None:
    """The stamp never falls behind DDL an earlier revision already committed.

    0162's downgrade narrows the allocation-state CHECK and then validates it
    outside the transaction, which commits what the run has done so far. 0155's
    downgrade, further down the same run, refuses a history row the older
    vocabulary cannot name. With one transaction for the whole run, that refusal
    rolled the stamp back to where 0162's commit left it -- 0162, still
    "applied" -- over a CHECK already narrowed, and the next ``upgrade head``
    stepped over 0162 and never put ``asleep`` back.
    """
    database.must("upgrade", "head")
    assert "asleep" in _allocation_states(database)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, acting_principal) "
            "VALUES (%s, %s, %s, 1, 'conflict', %s)",
            (uuid.uuid4(), database.seed.org, database.seed.shared_node, database.seed.user),
        )

    code, output = database.alembic("downgrade", "0132")

    assert code != 0
    assert "cannot narrow file_history.kind: conflict=1" in output
    # Every revision above the one that refused is undone and stamped undone;
    # the one that refused is exactly as it was, and stamped applied.
    assert database.revision() == "0155"
    assert "asleep" not in _allocation_states(database)

    database.must("upgrade", "head")
    assert "asleep" in _allocation_states(database)


# --- locks ---------------------------------------------------------------------------


def test_a_locked_table_fails_the_run_fast_and_the_re_run_succeeds(database: _Database) -> None:
    database.must("upgrade", "0142")
    blocker = database.connect()
    try:
        blocker.execute("BEGIN")
        blocker.execute("LOCK TABLE billing_proxy_requests IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        code, output = database.alembic("upgrade", "0143", MIGRATION_LOCK_TIMEOUT_MS="300")
        waited = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert code != 0
    assert "lock timeout" in output
    # The interpreter's start-up dominates; without the bound this never returns.
    assert waited < 60
    assert database.revision() == "0142"
    assert "last_seen_at" not in _columns(database, "billing_proxy_requests")

    database.must("upgrade", "0143")
    assert _index_state(database, _PROXY_INDEX) == (True, False)


def _revisions_after_shipped() -> list[Path]:
    return sorted(
        path
        for path in _VERSIONS.glob("0*.py")
        if path.name[:4].isdigit() and path.name[:4] > _SHIPPED
    )


@pytest.mark.parametrize("path", _revisions_after_shipped(), ids=lambda path: path.name[:4])
def test_every_revision_bounds_its_lock_wait_and_builds_nothing_under_a_blocking_lock(
    path: Path,
) -> None:
    source = path.read_text()
    for direction in ("upgrade", "downgrade"):
        body = re.search(rf"^def {direction}\(\) -> None:\n(.*?)(?=^def |\Z)", source, re.M | re.S)
        assert body is not None, f"{path.name} has no {direction}()"
        statements = re.sub(r'""".*?"""', "", body.group(1), count=1, flags=re.S)
        statements = "\n".join(
            line for line in statements.splitlines() if not line.strip().startswith("#")
        ).strip()
        assert statements.startswith("bound_lock_wait()"), (
            f"{path.name} {direction}() takes locks before it bounds the wait for them"
        )
    # A validating CHECK scans under ACCESS EXCLUSIVE and a plain index build
    # holds SHARE for its whole length; both go through backend.migration_safety.
    for blocking in (
        "op.create_index(",
        "op.create_check_constraint(",
        "op.create_unique_constraint(",
    ):
        assert blocking not in source, f"{path.name} calls {blocking}…) on a live table"


#: The head when :func:`backend.migration_safety.lint_revision` landed: every
#: revision numbered above it passes the whole lint.
_LINTED_AFTER = "0204"


@pytest.mark.parametrize(
    "path",
    [path for path in _revisions_after_shipped() if path.name[:4] > _LINTED_AFTER],
    ids=lambda path: path.name[:4],
)
def test_every_new_revision_passes_the_lint(path: Path) -> None:
    assert lint_revision(path.read_text()) == []


_CLEAN_REVISION = """
from backend.migration_safety import bound_lock_wait, outside_transaction


def upgrade() -> None:
    \"\"\"Add a column.\"\"\"
    bound_lock_wait()
    op.add_column("t", sa.Column("c", sa.Integer()))


def downgrade() -> None:
    bound_lock_wait()
    op.drop_column("t", "c")
"""


@pytest.mark.parametrize(
    ("source", "complaint"),
    [
        pytest.param(
            _CLEAN_REVISION.replace(
                "    bound_lock_wait()\n    op.add_column", "    op.add_column"
            ),
            "upgrade() takes locks before it bounds the wait for them",
            id="no-bound",
        ),
        pytest.param(
            _CLEAN_REVISION.replace(
                '    op.drop_column("t", "c")',
                '    with op.get_context().autocommit_block():\n        op.drop_column("t", "c")',
            ),
            "downgrade() runs an ACCESS EXCLUSIVE operation with no bound on its lock wait",
            id="access-exclusive-outside-the-bound",
        ),
        pytest.param(
            _CLEAN_REVISION.replace(
                '    op.drop_column("t", "c")',
                "    with op.get_context().autocommit_block():\n"
                '        op.execute("ALTER TABLE t DROP c")',
            ),
            "downgrade() runs an ACCESS EXCLUSIVE operation with no bound on its lock wait",
            id="raw-alter-outside-the-bound",
        ),
        pytest.param(
            _CLEAN_REVISION.replace(
                '    op.add_column("t", sa.Column("c", sa.Integer()))',
                '    op.create_index("ix", "t", ["c"])',
            ),
            "upgrade() calls op.create_index() on a live table",
            id="blocking-index-build",
        ),
        pytest.param(
            _CLEAN_REVISION.replace(
                '    op.add_column("t", sa.Column("c", sa.Integer()))',
                '    add_check_not_valid("t", "ck", "c > 0")\n'
                '    validate_constraints([("t", "ck")])',
            ),
            "adds a constraint NOT VALID and validates it in the same revision",
            id="not-valid-and-validate-together",
        ),
    ],
)
def test_the_lint_catches_a_planted_revision(tmp_path: Path, source: str, complaint: str) -> None:
    planted = tmp_path / "0999_planted.py"
    planted.write_text(source, encoding="utf-8")
    found = lint_revision(planted.read_text(encoding="utf-8"))
    assert any(line.startswith(complaint) for line in found), found


def test_a_clean_revision_passes_the_lint() -> None:
    assert lint_revision(_CLEAN_REVISION) == []


def test_ddl_inside_outside_transaction_is_bounded_and_passes() -> None:
    """``outside_transaction`` sets the bound on the session for its block."""
    bounded = _CLEAN_REVISION.replace(
        '    op.drop_column("t", "c")',
        '    with outside_transaction():\n        op.drop_column("t", "c")',
    )
    assert lint_revision(bounded) == []


# --- one runner at a time ------------------------------------------------------------


def _hold_migration_lock(database: _Database) -> psycopg.Connection[Any]:
    holder = database.connect()
    assert holder.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,)).fetchone() == (
        True,
    )
    return holder


def _runners_waiting(database: _Database) -> int:
    """Sessions of this database that tried the migration lock and did not get it.

    ``pg_try_advisory_lock`` leaves no waiter in ``pg_locks``, so the runners are
    counted by what they are: other sessions on this scratch database.
    """
    return int(
        database.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
            "AND application_name = ''",
            database.name,
        )
    )


def test_two_upgrades_at_once_serialise_and_both_exit_zero(database: _Database) -> None:
    # Both replicas' init containers start while something else is migrating, so
    # both are provably waiting before either may run: the race is then between
    # the two of them, which is the one that matters.
    holder = _hold_migration_lock(database)
    runners = [database.start("upgrade", "head") for _ in range(2)]
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            # The holder's own session is the third.
            if _runners_waiting(database) >= 3:
                # Connected is not yet refused: give each its first try.
                time.sleep(1.5)
                break
            time.sleep(0.2)
        else:
            pytest.fail("the two runners never reached the migration lock")
        assert all(runner.poll() is None for runner in runners)
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        holder.close()
    finished = [runner.finish() for runner in runners]
    outputs = [output for _code, output in finished]

    assert [code for code, _output in finished] == [0, 0], "\n---\n".join(outputs)
    for output in outputs:
        assert "holds the migration lock on this database; waiting" in output
    # Each revision ran exactly once, in whichever runner won; the other found
    # the schema at head and had nothing to do.
    applied = [output.count("Running upgrade") for output in outputs]
    assert sorted(applied) == [0, len(_revisions_after_shipped())]
    assert _live_shares(database) == ["writer"]
    assert _index_state(database, _SHARE_INDEX) == (True, True)
    code, check = database.alembic("check")
    assert code == 0, check
    assert "No new upgrade operations detected" in check


#: What a winning runner does to a losing one, with the volume made explicit: a
#: mebibyte on the pipe -- past the four kilobytes Windows holds and the
#: sixty-four Linux does -- and only then the signal that lets the other go.
_WRITER = """
import pathlib, sys
for _ in range(1024):
    sys.stdout.write("x" * 1023 + "\\n")
sys.stdout.flush()
pathlib.Path(sys.argv[1]).touch()
"""
#: The losing runner: it exits only once the writer is done.
_WAITER = """
import pathlib, sys, time
gate = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 120
while not gate.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.05)
print("released")
"""


def test_a_runner_waited_on_first_cannot_stall_the_one_it_waits_for(tmp_path: Path) -> None:
    """The order two runners are collected in is not allowed to matter.

    The waiter is collected FIRST, which is the shape the two-runner test takes
    whenever the runner it waits on first is the one that lost the lock; the
    writer meanwhile puts more on its pipe than any platform's pipe holds
    before it lets the waiter exit.
    """
    gate = tmp_path / "released"
    python = [sys.executable, "-c"]
    waiter = _Runner([*python, _WAITER, str(gate)], cwd=tmp_path, env=dict(os.environ))
    writer = _Runner([*python, _WRITER, str(gate)], cwd=tmp_path, env=dict(os.environ))

    waited = waiter.finish(timeout=60)
    written = writer.finish(timeout=60)

    assert waited == (0, "released\n")
    assert written[0] == 0
    assert len(written[1]) == 1024 * 1024

    # The control: the same pair on plain pipes, collected in the same order,
    # stalls on this platform too -- both still alive, the writer inside a write
    # nobody reads and the waiter behind it. That is what makes the pass above
    # the reader thread's doing and not the pipe's.
    gate.unlink()
    plain = [
        subprocess.Popen(
            [*python, script, str(gate)], cwd=tmp_path, stdout=subprocess.PIPE, text=True
        )
        for script in (_WAITER, _WRITER)
    ]
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            plain[0].communicate(timeout=2)
        assert [process.poll() for process in plain] == [None, None]
    finally:
        for process in plain:
            process.kill()
            process.communicate()


def test_a_runner_gives_up_on_a_lock_that_is_never_released(database: _Database) -> None:
    holder = _hold_migration_lock(database)
    try:
        code, output = database.alembic("upgrade", "head", MIGRATION_RUNNER_WAIT_SECONDS="1")
    finally:
        holder.close()

    assert code != 0
    assert "gave up waiting for the migration lock after 1s" in output
    assert database.revision() == _SHIPPED


# --- 0149: the org-wide "Can view" every template save wrote unasked ----------------

_INSERT_TEMPLATE_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata) VALUES (%s, %s, %s, %s, NULL, 'folder', %s, %s, %s, "
    "'{}'::jsonb, CAST(%s AS ltree), 0, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, 0, false, '{}'::jsonb)"
)


@dataclass
class _TemplateGrants:
    """Three templates and a chat, as the pre-fix save left them.

    ``unasked`` are the two grants the old save wrote on its own: an org
    ``reader`` on the template's folder, granted by the owner, with no history
    row. ``deliberate`` is the same row on a third template with the ``acl``
    history row the share dialog writes. ``chat`` is an org grant on a chat's
    folder — never a template, so never the revoke's business.
    """

    unasked: dict[uuid.UUID, uuid.UUID]
    deliberate: tuple[uuid.UUID, uuid.UUID]
    chat: tuple[uuid.UUID, uuid.UUID]
    paths: dict[uuid.UUID, str]


def _seed_template_grants(database: _Database) -> _TemplateGrants:
    seed = database.seed
    with database.connect() as conn:
        drive = conn.execute(
            "SELECT drive_id FROM file_nodes WHERE id = %s", (seed.shared_node,)
        ).fetchone()
        assert drive is not None
        rows: list[tuple[str, uuid.UUID, uuid.UUID]] = []
        paths: dict[uuid.UUID, str] = {}
        kinds = ("chat_template", "chat_template", "chat_template", "chat")
        for ino, kind in enumerate(kinds, start=40):
            obj, node, share = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conn.execute(
                "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
                "visibility_scope, spec) VALUES (%s, %s, %s, %s, %s, 'org', '{}'::jsonb)",
                (obj, seed.org, f"{kind}-{obj.hex}", kind, seed.user),
            )
            path = str(node).replace("-", "_")
            conn.execute(
                _INSERT_TEMPLATE_NODE,
                (node, ino, drive[0], seed.org, kind, obj, f"t{ino}".encode(), path),
            )
            conn.execute(_INSERT_SHARE, (share, seed.org, node, seed.org, "reader", seed.user))
            paths[node] = path
            rows.append((kind, node, share))
        first, second, third, chat = rows
        conn.execute(
            "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, acting_principal, "
            "after) VALUES (%s, %s, %s, 1, 'acl', %s, %s::jsonb)",
            (uuid.uuid4(), seed.org, third[1], seed.user, json.dumps({"granted": str(third[2])})),
        )
    return _TemplateGrants(
        unasked={first[1]: first[2], second[1]: second[2]},
        deliberate=(third[1], third[2]),
        chat=(chat[1], chat[2]),
        paths=paths,
    )


def _revoked(database: _Database, share: uuid.UUID) -> bool:
    return bool(
        database.scalar("SELECT revoked_at IS NOT NULL FROM file_shares WHERE id = %s", share)
    )


def test_0149_revokes_only_the_grants_a_template_save_wrote_unasked(database: _Database) -> None:
    database.must("upgrade", "0148")
    grants = _seed_template_grants(database)

    database.must("upgrade", "0149")

    assert all(_revoked(database, share) for share in grants.unasked.values())
    assert not _revoked(database, grants.deliberate[1])
    assert not _revoked(database, grants.chat[1])
    # The interned ACL copy is not trusted until the queued rewrite lands.
    assert dict(
        database.rows(
            "SELECT id, state FROM file_nodes WHERE id = ANY(%s)",
            [*grants.unasked, grants.deliberate[0], grants.chat[0]],
        )
    ) == {
        **{node: "acl_rewriting" for node in grants.unasked},
        grants.deliberate[0]: "live",
        grants.chat[0]: "live",
    }
    queued = database.rows(
        "SELECT result_node_id, progress->>'root_path_ids' FROM file_ops "
        "WHERE kind = 'acl_rewrite' AND state = 'queued' ORDER BY result_node_id"
    )
    assert queued == sorted((node, grants.paths[node]) for node in grants.unasked)
    # Running it again finds nothing left to revoke and queues nothing more.
    database.must("downgrade", "0148")
    database.must("upgrade", "0149")
    assert database.scalar("SELECT count(*) FROM file_ops WHERE kind = 'acl_rewrite'") == 2


def test_0149_refuses_a_login_that_cannot_see_the_grants(
    database: _Database, bound_login: _Database
) -> None:
    database.must("upgrade", "0148")
    grants = _seed_template_grants(database)

    code, output = bound_login.alembic("upgrade", "0149")

    assert code != 0, f"the revoke ran blind and reported success:\n{output}"
    assert "RowSecurityBlocksMigrationError" in output
    assert "file_shares" in output
    assert database.revision() == "0148"
    assert not any(_revoked(database, share) for share in grants.unasked.values())
    assert database.scalar("SELECT count(*) FROM file_ops WHERE kind = 'acl_rewrite'") == 0
