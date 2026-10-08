"""Repo-root pytest configuration: the database guard + per-xdist-worker isolation.

Two things happen at this module's import, before anything else in the session:
first the database this run is pointed at is proven DISPOSABLE (see
``alkera_core.db.testing``), then — on an xdist worker — a private database is
provisioned for it.

The guard comes first because the rest of this file, and the suite it configures,
are destructive: fixtures commit real rows and several clear whole tables, none of
them knowing which database they are aimed at. ``make`` generates ``.env.workspace``,
which points ``DATABASE_URL`` at THIS worktree's dev database, so a bare ``pytest``
with no DSN exported would run all of that against the database ``make dev-all``
serves. It has: one such run recreated ``compute_machine_types`` with fresh ids,
deleted a live compute grant, and left thousands of test orgs behind. So a database
whose name is not on the allowlist (and is not named exactly by
``ALKERA_TEST_ALLOW_DB``) aborts the session here, at conftest import — before a
single fixture, and therefore before a single row, can be touched.

The default suite has no per-test transaction rollback — tests commit real rows
and lean on uniquely-suffixed names to coexist on one shared database. That is
safe serially, but parallel xdist workers writing to a single DB would race on
unique constraints, row counts, and visibility. So each worker gets its OWN
database (``<base>_<worker id>``) on the SAME shared Postgres instance — the
standard, lightest isolation. It is a CLONE of the database this session was
pointed at, which every ``make`` pytest target has already migrated to head, and
a worker that finds its clone behind head migrates it itself so a stale base can
never make the suite prove against an old schema. The mechanism, and why copying
beats replaying 137 revisions per worker, is in ``alkera_core.db.testing``.

How it stays zero-touch for production code: xdist runs each worker as its own
process, so the import-time ``engine`` singleton in ``alkera_core.db.session``
(built from ``settings.database_url``) is per-worker. We only need the worker's
DB URL in the environment *before* that worker first imports
``alkera_core.config`` — OS env vars outrank ``.env`` files in pydantic-settings,
so setting ``DATABASE_URL`` / ``DATABASE_URL_SYNC`` early is enough. We
deliberately do NOT import ``alkera_core.config`` ourselves (that would create +
cache a settings instance bound to the wrong DB); the base URL is resolved by
reading the dotenv files directly. No app code, fixtures, or alembic env.py
change.

The provisioning MUST run at this module's import, not in ``pytest_configure``:
every ``testpaths`` entry is an *initial-conftest anchor*, so pytest imports each
suite's own ``conftest.py`` BEFORE any ``pytest_configure`` hook fires — and
several of those (e.g. ``apps/backend/tests/conftest.py``) import app modules at
module level, freezing the settings singleton. This root conftest is the first
initial conftest loaded (rootward chain), and xdist exports ``PYTEST_XDIST_WORKER``
into the worker's environment before it parses config (``xdist/remote.py`` sets it
just before ``_prepareconfig``), so import time here is the one spot that is both
per-worker and guaranteed to precede every app import.

Serial runs (bare ``uv run pytest``, no ``-n``) have no worker id, so this is a
no-op and the suite uses the canonical ``alkera`` DB exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import freezegun
import pytest
from alkera_core.db.testing import (
    NonDisposableDatabaseError,
    WorkerDatabase,
    assert_disposable_database,
    drop_migration_template,
    migration_template_requested,
    provision_migration_template,
    provision_worker_database,
    publish_migration_template,
    published_migration_template,
    server_reachable,
)
from alkera_core.env_files import env_files
from alkera_core.utils.abandoned_tasks import (
    format_report,
    is_pytest_timeout,
    pending_tasks,
    reap_abandoned_tasks,
)

#: Test layers a distribution built on this tree adds. Every module of a
#: ``conftest_layers`` package beside this file is loaded as a plugin right
#: after this conftest and before any suite's own, so a layer can install its
#: extensions before a suite builds an app or reads an extension point. The
#: open tree ships none: its suites run the open composition.
pytest_plugins = [
    f"conftest_layers.{layer.stem}"
    for layer in sorted((Path(__file__).parent / "conftest_layers").glob("*.py"))
    if layer.stem != "__init__"
]


class _SuiteSeams:
    """Hooks a test layer implements to take part in a suite's fixtures."""

    @pytest.hookspec
    def pytest_alkera_gateway_seed(self, seed: Any) -> Any:
        """Called by the gateway suite's ``seed`` fixture after it has added the
        person, the model and its route to ``seed.session`` and before it
        commits. A layer returns an awaitable that adds its own rows for the
        same seed (billing adds the seat's account, its balance and the prices)
        and records what it made on ``seed``."""

    @pytest.hookspec
    def pytest_alkera_architecture_allowlist(self, name: str) -> dict[str, Any] | None:
        """Called by an architecture ratchet that walks a package's source tree.
        A layer whose distribution adds modules to that tree returns the entries
        its own modules hold, keyed like the ratchet's allowlists (``name`` names
        the ratchet); the ``architecture_allowlist`` fixture merges them with the
        ratchet's own, so the open allowlists name open modules only."""


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(_SuiteSeams)


ArchitectureAllowlist = Callable[[str, Mapping[str, Any]], dict[str, Any]]


def merge_allowlists(own: Mapping[str, Any], extras: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``own`` with each of ``extras`` added: a list allowlist gains the extra
    entries, a mapping allowlist gains the extra keys. A key both sides hold, or
    an allowlist only an extra names, is refused: a layer adds its own modules'
    entries and never overrides the open ones."""
    merged: dict[str, Any] = {
        key: list(value) if isinstance(value, list) else dict(value) for key, value in own.items()
    }
    for extra in extras:
        for key, value in extra.items():
            assert key in merged, f"a layer names an allowlist the ratchet does not hold: {key}"
            if isinstance(merged[key], list):
                merged[key].extend(value)
                continue
            clash = sorted(set(merged[key]) & set(value))
            assert not clash, f"a layer repeats entries of {key}: {clash}"
            merged[key].update(value)
    return merged


@pytest.fixture(scope="session")
def architecture_allowlist(pytestconfig: pytest.Config) -> ArchitectureAllowlist:
    """The allowlists of the ratchet ``name``: its own (``own``) plus what every
    test layer returns from ``pytest_alkera_architecture_allowlist``."""

    def allowlist(name: str, own: Mapping[str, Any]) -> dict[str, Any]:
        extras = pytestconfig.hook.pytest_alkera_architecture_allowlist(name=name)
        return merge_allowlists(own, extras)

    return allowlist


# ``freeze_time`` sweeps every loaded module to patch its time references. The warehouse
# driver stacks (Cython/C extensions and their large pure-Python trees) load into the same
# xdist workers as the freezegun cost/TTL tests, and on Windows that sweep hard-crashed the
# worker ("node down: Not properly terminated") once these packages were present. None of
# them ever needs a frozen clock in this suite — skip them so the sweep stays within the
# code actually under test. The list lives in the ROOT conftest rather than a per-app one
# because every suite that imports these packages must be covered: the Windows shard that
# runs the backend / worker / api-core suites never loads the CLI conftest, and the Temporal
# SDK (a Rust core behind a large generated tree) is imported by exactly those suites.
freezegun.configure(
    extend_ignore_list=[
        "clickhouse_connect",
        "duckdb",
        "google",
        "grpc",
        "pyarrow",
        "pymysql",
        "psycopg",
        "snowflake",
        "sqlalchemy",
        "temporalio",
        "trino",
    ]
)

# pandas cannot be imported while a clock is frozen. ``freeze_time`` replaces
# ``datetime.datetime`` with its fake for the whole process, and pandas' Cython
# ``Timestamp`` subclasses ``datetime.datetime`` at import: built on the fake it gets a
# layout the interpreter cannot garbage-collect, and the process dies in the import — a
# segmentation fault on macOS and Linux, "Windows fatal exception: stack overflow" on
# Windows, no test named either way. lancedb imports pandas, the knowledge store imports
# lancedb the first time it needs it, and a knowledge test under ``freeze_time`` was that
# first time on a Windows worker once its module stopped being pinned to one. Importing
# pandas before any test runs, on every worker whose collection includes a frozen clock,
# takes that first time out of every frozen block.
FROZEN_CLOCK_NAMES = ("freeze_time", "freezegun")


def modules_that_freeze_the_clock(modules: Iterable[Any]) -> list[str]:
    """The names of the collected modules that bound ``freeze_time`` or ``freezegun``."""
    return sorted(
        {
            module.__name__
            for module in modules
            if any(hasattr(module, name) for name in FROZEN_CLOCK_NAMES)
        }
    )


def import_pandas_ahead_of_frozen_clocks(modules: Iterable[Any]) -> bool:
    """Import pandas now if any of ``modules`` will freeze the clock. Returns whether
    it did; a suite with no frozen clock, or an environment without pandas, is left
    alone."""
    if not modules_that_freeze_the_clock(modules):
        return False
    if importlib.util.find_spec("pandas") is None:
        return False
    importlib.import_module("pandas")
    return True


def pytest_report_header() -> list[str]:
    """Say which store driver this session is on, in the run's own header.

    The decision is taken at this module's import, long before a terminal
    reporter exists, and anything printed there is swallowed by pytest's
    capture — so a CI log would show a suite that quietly ran on a directory
    with nothing saying so. The header is the one place every run prints.
    """
    if _FILES_STORE_FALLBACK is None:
        return []
    endpoint, root = _FILES_STORE_FALLBACK
    return [
        f"files store: {endpoint or '<unset>'} is not listening — this session runs the "
        f"filesystem driver under {root}"
    ]


def pytest_collection_finish(session: pytest.Session) -> None:
    modules = {item.module for item in session.items if getattr(item, "module", None)}
    import_pandas_ahead_of_frozen_clocks(modules)
    _prepare_migration_template(session.config)


def _prepare_migration_template(config: pytest.Config) -> None:
    """Make the copy the migration tests will copy from, while the copy can still be made.

    A migration test runs on a throwaway copy of a database at head, and Postgres
    copies a database only while nothing else is connected to it. The session's own
    database stops qualifying at the first test: every engine keeps its checked-in
    connections open between tests, so an idle pool from any earlier test would block
    the copy for the rest of the session. The end of collection is the last moment
    that is both after every test module's import — which is when the migration
    harness says it is loaded — and before any fixture has opened a connection.

    On an xdist worker the template is normally already published: the base the
    worker was cloned from is at head, and nothing connects to it during a parallel
    run. A copy is made only when no template is published yet and a migration test
    module is loaded, so a session with no such module never pays for one. A bare
    ``--collect-only`` runs no test and makes none either.
    """
    if published_migration_template() is not None or not migration_template_requested():
        return
    if config.getoption("collectonly"):
        return
    publish_migration_template(provision_migration_template(_resolve_env_url("DATABASE_URL_SYNC")))


# Filled in by _provision_worker_db on a worker; read back by pytest_unconfigure.
_WORKER_DB: WorkerDatabase | None = None

_REPO_ROOT = Path(__file__).resolve().parent
# The directory holding `alembic.ini` and `alembic/`, which is where a migration runs from.
_BACKEND_ROOT = _REPO_ROOT / "apps" / "backend"
# Same files + precedence pydantic-settings uses (later file wins); OS env wins
# over all of them. Keep in sync with Settings.model_config.env_file.
_DEFAULT_SYNC_URL = "postgresql+psycopg://alkera:alkera@localhost:5432/alkera"
_DEFAULT_ASYNC_URL = "postgresql+asyncpg://alkera:alkera@localhost:5432/alkera"
_DEFAULT_URLS = {"DATABASE_URL": _DEFAULT_ASYNC_URL, "DATABASE_URL_SYNC": _DEFAULT_SYNC_URL}


def _resolve_env_url(setting: str) -> str:
    """Resolve one DATABASE_URL* the way the app would, WITHOUT importing
    ``alkera_core.config`` (which would cache a settings instance bound to the
    canonical DB before we can redirect it). OS env > dotenv files > default."""
    env = os.environ.get(setting)
    if env:
        return env
    value = _DEFAULT_URLS[setting]
    prefix = f"{setting}="
    for path in env_files(_REPO_ROOT):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(prefix):
                value = line.split("=", 1)[1].strip().strip("\"'")
    return value


def _resolve_setting(name: str, default: str = "") -> str:
    """One setting's value the way the app would read it, WITHOUT importing
    ``alkera_core.config`` — OS env > dotenv files (later file wins) > default."""
    env = os.environ.get(name)
    if env:
        return env
    value = default
    prefix = f"{name}="
    for path in env_files(_REPO_ROOT):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                value = stripped.split("=", 1)[1].strip().strip("\"'")
    return value


def _resolve_base_sync_url() -> str:
    return _resolve_env_url("DATABASE_URL_SYNC")


def _require_disposable_database() -> str:
    """Refuse to run when this session is pointed at a database it may not destroy.

    Checked for BOTH DSNs — the app engine reads ``DATABASE_URL``, alembic and the
    provisioning below read ``DATABASE_URL_SYNC``, and a run that exported only one
    of them is exactly the mistake worth catching.

    The one exemption: a server nothing is listening on holds no data to protect, so
    a run with no Postgres at all (the file-semantics subsets, the snapshot
    generators) is not aborted — a test that does need the database still fails at
    its first connection, as it always has. Reachability is only ever consulted to
    WAIVE a refusal; an unreadable URL counts as reachable and is refused.
    """
    for setting in ("DATABASE_URL", "DATABASE_URL_SYNC"):
        url = _resolve_env_url(setting)
        try:
            assert_disposable_database(url, setting=setting)
        except NonDisposableDatabaseError:
            if server_reachable(url):
                raise
    return _resolve_env_url("DATABASE_URL")


def _provision_worker_db() -> None:
    """On an xdist worker, provision a private database at head for it.

    Runs at MODULE IMPORT (see the module docstring): initial conftests of the
    ``testpaths`` anchors load before ``pytest_configure``, and some import app
    modules at module level — the env override must land before any of them.
    """
    global _WORKER_DB

    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        # Serial run or the xdist controller: use the database the session names.
        return

    _WORKER_DB = provision_worker_database(
        _resolve_base_sync_url(), worker, alembic_root=_BACKEND_ROOT
    )

    # Point the environment at the worker DB BEFORE any app import. OS env vars
    # outrank the .env files, so the single settings instance the app builds
    # lazily binds to this DB.
    os.environ["DATABASE_URL"] = _WORKER_DB.async_url
    os.environ["DATABASE_URL_SYNC"] = _WORKER_DB.sync_url

    # Provisioning imports every revision script to find the head. A revision whose
    # imports built the settings singleton bound it to the BASE — and with it the
    # engine every test on this worker opens — so all the workers would share one
    # database and fail each other in ways no single test names. Refuse now.

    if "alkera_core.config" in sys.modules:
        raise RuntimeError(
            f"xdist worker {worker}: alkera_core.config was imported while the worker "
            "database was provisioned, before DATABASE_URL was redirected at it; a "
            "revision script (or something it imports) builds the settings singleton "
            "at import — read the settings when the revision runs instead"
        )

    # The base this worker was just copied from is at head (the copy came out at
    # head) and nothing connects to it for the rest of the run, so it is the database
    # the worker's migration tests copy — no second copy of anything is needed.
    if _WORKER_DB.template is not None:
        publish_migration_template(
            provision_migration_template(_WORKER_DB.sync_url, borrow=_WORKER_DB.template)
        )


#: When set, the app engine (``DATABASE_URL``) connects as this login instead of
#: the superuser the suite otherwise runs as — the tier that proves tenant
#: isolation does not lean on superuser privileges (``make test-rls-login``).
#: The login is made the way a deployment makes its runtime login
#: (:func:`alkera_core.db.row_security.app_login_statements`): not a superuser,
#: ``BYPASSRLS``, a member of the Files tenant role, DML on every table. The
#: sync DSN stays the superuser's, for provisioning and alembic.
APP_LOGIN_ENV = "ALKERA_TEST_APP_LOGIN"
#: A lane-local test password; the login exists only on a disposable server.
_APP_LOGIN_PASSWORD = "alkera-test-app-login"  # noqa: S105 - disposable test login


def _connect_as_app_login() -> None:
    """Point the app engine at :data:`APP_LOGIN_ENV`'s login, creating it on
    this server and granting it this database first. Idempotent, and
    serialized across xdist workers by an advisory lock on the maintenance
    database they all share (roles are cluster-wide)."""
    login = os.environ.get(APP_LOGIN_ENV, "").strip()
    if not login:
        return
    import psycopg
    from alkera_core.db.row_security import (
        app_login_grant_statements,
        app_login_role_statements,
    )

    sync_url = _resolve_env_url("DATABASE_URL_SYNC")
    parts = urlsplit(sync_url)
    database = parts.path.lstrip("/")
    plain = sync_url.replace("postgresql+psycopg://", "postgresql://", 1)
    maintenance = plain.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext('alkera_test_app_login'))")
        try:
            for statement in app_login_role_statements(login, password=_APP_LOGIN_PASSWORD):
                conn.execute(statement)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('alkera_test_app_login'))")
    with psycopg.connect(plain, autocommit=True) as conn:
        for statement in app_login_grant_statements(login, database=database):
            conn.execute(statement)
    async_url = _resolve_env_url("DATABASE_URL")
    target = urlsplit(async_url)
    host = target.hostname or "localhost"
    port = f":{target.port}" if target.port else ""
    os.environ["DATABASE_URL"] = (
        f"{target.scheme}://{quote(login)}:{quote(_APP_LOGIN_PASSWORD)}@{host}{port}{target.path}"
        + (f"?{target.query}" if target.query else "")
    )


def _pin_fast_password_hashing() -> None:
    """Hash passwords at argon2's floor for the whole session.

    A typical backend test is three or four argon2 operations deep before its
    first assertion — the org admin's password, a member's, and a real
    ``POST /auth/login`` that verifies one — and at the production work factor
    each of those is tens of milliseconds of deliberate CPU burn, paid per test,
    on a box already running one xdist worker per core. Nothing in the suite
    asserts on the cost, so all of it is waiting.

    ``fast`` changes the work factor and nothing else: every path still runs the
    real argon2 hash and the real argon2 verify, and argon2 writes its parameters
    into the hash, so a credential minted here verifies at any profile. A test
    that DOES pin the production cost sets the profile itself — ``setdefault``
    leaves an exported value alone, and so does a ``monkeypatch`` of the setting.

    Set at module import for the same reason the worker DB is: the app modules
    imported by the per-suite conftests build the settings singleton, and an
    override that lands after that one is an override with no effect.
    """
    os.environ.setdefault("PASSWORD_HASH_PROFILE", "fast")


def _run_as_local() -> None:
    """The suite is a local environment, and says so.

    The published dev secrets stand in for an unset AUTH_JWT_SECRET, pepper or
    content key only when APP_ENV=local is set on purpose; a deployment that
    leaves APP_ENV unset must refuse to sign with them. The suite runs without
    an env file in CI, so it sets local here, before the settings singleton is
    built. An exported APP_ENV is left alone.
    """
    os.environ.setdefault("APP_ENV", "local")


def _scope_unsaved_session_sweeps() -> None:
    """No backend a test starts sweeps the whole database for live file sessions.

    A deployment's replicas sweep every unsaved session so a process that
    stopped mid-edit loses nothing. A worker database holds every earlier
    test's sessions, so a sweep there writes back (and locks the drives of)
    residue no test owns, and the tests running beside it time out on locks.
    A test that needs the sweep starts one scoped to its own org (the
    restartable backend does).

    Set on the settings singleton, not in the environment: a production-shaped
    ``Settings(...)`` a test builds reads the environment, and production
    refuses to run with the sweep off.
    """
    from alkera_core.config import settings

    settings.realtime_crdt_unsaved_sweep_enabled = False


# --- the suite never writes into the deployment's Files bucket ----------------------

#: How an isolated bucket announces itself. A name that does not start with this
#: is a deployment's bucket, not a run's.
FILES_TEST_BUCKET_PREFIX = "alkera-test-"

#: Where the deployment's own bucket name is parked once this process has
#: replaced it. An xdist worker inherits the CONTROLLER's environment, redirect
#: and all, so without this a worker would read the controller's test bucket as
#: if it were the deployment's — and would name its own bucket after it.
_DEPLOYMENT_BUCKET_ENV = "ALKERA_TEST_DEPLOYMENT_FILES_BUCKET"

#: The bucket the environment actually names — read BEFORE the override below
#: replaces it, so a test can prove the suite is not pointed at it.
_DEPLOYMENT_FILES_BUCKET: str | None = None

#: The bucket this run owns, created and dropped by the session fixture.
_FILES_BUCKET: str | None = None


def files_test_bucket_name(configured: str | None, *, worker: str, token: str) -> str | None:
    """The bucket this run owns, or ``None`` when no bucket is configured at all.

    DNS-shaped and inside S3's 63-byte bound, and carrying the xdist worker id:
    workers run in their own processes against one endpoint, so a bucket shared
    between them would make one worker's teardown delete another's objects
    mid-test — the same reason each worker gets its own database.
    """
    if not configured:
        return None
    safe = "".join(ch if ch.isalnum() else "-" for ch in worker.lower()) or "main"
    return f"{FILES_TEST_BUCKET_PREFIX}{token}-{safe}"[:63].rstrip("-")


def _enable_mock_oauth() -> None:
    """Turn on the mock OAuth provider for the suite.

    It is a test and local-development switch only, so no shipped example
    turns it on; the tests that drive the OAuth flow rely on it, and an
    explicit value in the environment still wins."""
    os.environ.setdefault("OAUTH_MOCK_ENABLED", "true")


def _isolate_local_boxes() -> None:
    """Point this run's local developer boxes at a Docker project of their own.

    The ``localdev`` compute provider lists, starts and removes the containers
    labelled with ``COMPOSE_PROJECT_NAME``. A test process inherits the dev
    stack's ``.env.workspace``, so a test that reaches the real provider (the
    reconcile pass, the backend's keep-alive at startup) would act on the boxes
    the developer is using: one did remove a dev box. Like the Files bucket, the
    name is replaced in the environment before any app import, so nothing a test
    can reach ever sees a box this run did not create.
    """
    from uuid import uuid4

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    os.environ["COMPOSE_PROJECT_NAME"] = f"alkera-test-boxes-{uuid4().hex[:10]}-{worker}"


def _enforce_lock_rules() -> None:
    """Make every broken locking rule a failure for the whole run.

    ``alkera_core.db.locking`` checks two rules as locks are taken: ranks go
    up, never down, and nothing leaves Postgres while a row lock is held. A
    deployment counts and logs a break; the suite raises it, so a test that
    walks an inverted order or awaits a provider under a lock fails where it
    happens, and the code's own paths are what prove the rules hold. A run can
    still set the variable to something else to see the breaks as warnings.
    """
    os.environ.setdefault("ALKERA_LOCK_CHECKS", "1")


def _public_node_address() -> None:
    """Give the run a public node callback address.

    A deployment whose ``node_api_url`` is loopback refuses to rent a machine
    elsewhere (``alkera_core.compute.node_reach``): the machine could never
    connect back. A developer's ``.env`` points it at localhost, so every test
    that buys a RunPod or EC2 machine would meet that refusal. The run gets an
    address only a test reaches; a test of the refusal sets its own.
    """
    os.environ["ALKERA_NODE_API_URL"] = "https://api.alkera.test"


def _isolate_files_bucket() -> None:
    """Point this run's Files store at a bucket of its own.

    ``make dev-all`` and the suite read the same ``.env.workspace``, so a test
    that goes through the real store factory writes into the bucket the running
    dev stack serves — and leaves its objects there, under a dedup domain whose
    database is dropped when the run ends, where nothing will ever collect them
    (that is exactly what ``files.gc`` exists to clean up after the fact).

    So the name is replaced in the ENVIRONMENT, before any app import builds the
    settings singleton, exactly as the worker database is. Everything that opens
    a store — the backend's factory, the CLI's admin handle, the worker's
    bootstrap — reads ``settings.files_store_bucket``, so redirecting the one
    setting redirects all of them, and no production code learns it is in a test.
    """
    global _DEPLOYMENT_FILES_BUCKET, _FILES_BUCKET
    from uuid import uuid4

    _DEPLOYMENT_FILES_BUCKET = os.environ.get(_DEPLOYMENT_BUCKET_ENV) or _resolve_setting(
        "FILES_STORE_BUCKET"
    )
    name = files_test_bucket_name(
        _DEPLOYMENT_FILES_BUCKET,
        worker=os.environ.get("PYTEST_XDIST_WORKER", "main"),
        token=uuid4().hex[:10],
    )
    if name is None:
        return
    os.environ[_DEPLOYMENT_BUCKET_ENV] = _DEPLOYMENT_FILES_BUCKET
    os.environ["FILES_STORE_BUCKET"] = name
    _FILES_BUCKET = name


#: The S3-shaped providers, which are the only ones that talk to an endpoint.
_ENDPOINT_PROVIDERS = ("s3_compatible", "aws")

#: Where the fallback below writes its objects, and the endpoint it stood in
#: for. ``None`` when the configured store was reachable (or was already the
#: filesystem driver), which is what the fixture reports.
_FILES_STORE_FALLBACK: tuple[str, str] | None = None


def files_endpoint_reachable(endpoint: str | None) -> bool:
    """Whether the configured Files endpoint accepts a connection right now.

    Fails OPEN, the opposite of the database guard: an endpoint we cannot read
    a host and a port out of is treated as reachable, because the cost of being
    wrong here is silently running a live-store suite against a local directory
    instead of aborting. The port is taken from the URL, or from the scheme —
    ``server_reachable`` would otherwise fall back to Postgres's.
    """
    if not endpoint:
        return True
    parsed = urlsplit(endpoint.strip())
    if not parsed.hostname:
        return True
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return server_reachable(f"files://{parsed.hostname}:{port}")


def _fall_back_to_filesystem_store() -> None:
    """Point this run at a local directory when its object store is not there.

    Almost nothing in the default suite is about S3: the Files suites install a
    filesystem factory of their own, and every other test that reaches the
    store does so because PRODUCTION code does — making an org stamps its new
    dedup domain's prefix, and that is a store write on a request that has
    nothing to do with Files. Pointed at a dead endpoint, each of those pays a
    connect ladder: the same eight SCIM tests take 0.8 s against a directory and
    40 s against a refused port, and on a CI runner with no object store at all
    that arithmetic is what turns a 45-minute job into one that never finishes.

    So a session whose endpoint is not listening runs on the filesystem driver
    instead. It is the same :class:`ObjectStore` contract — the one the suite's
    own Files fixtures already use — under a directory of this run's own, and
    a session that HAS an endpoint is left on it untouched.
    """
    global _FILES_STORE_FALLBACK

    provider = _resolve_setting("FILES_STORE_PROVIDER")
    if provider not in _ENDPOINT_PROVIDERS:
        return
    endpoint = _resolve_setting("FILES_STORE_ENDPOINT")
    if files_endpoint_reachable(endpoint):
        return
    root = tempfile.mkdtemp(prefix="alkera-files-fallback-")
    os.environ["FILES_STORE_PROVIDER"] = "filesystem"
    os.environ["FILES_STORE_ROOT"] = root
    # `direct` needs a driver that can presign, which the filesystem one cannot;
    # a deployment configured for presigned URLs would otherwise fail to boot.
    os.environ["FILES_TRANSFER_MODE"] = "proxied"
    _FILES_STORE_FALLBACK = (endpoint or "", root)


# The two environment mutations go FIRST: each touches nothing but ``os.environ``
# and the dotenv files, while both calls below import app modules (the guard's
# disposable-database check, and alembic inside the worker provisioning) and the
# first of those builds the settings singleton that has to see the new bucket
# name and the new hash profile.
_pin_fast_password_hashing()
_run_as_local()
_enforce_lock_rules()
_isolate_files_bucket()
_isolate_local_boxes()
_enable_mock_oauth()
_public_node_address()
_fall_back_to_filesystem_store()
_require_disposable_database()
_provision_worker_db()
# Before anything that builds the settings singleton: it rewrites DATABASE_URL, and
# a singleton built first keeps the superuser's engine for the whole session.
_connect_as_app_login()
_scope_unsaved_session_sweeps()


def _s3_test_client() -> Any:
    """A boto3 client for the configured endpoint, or ``None`` for no endpoint.

    Built here rather than through the store drivers on purpose: creating and
    dropping a bucket is not on the :class:`ObjectStore` surface, and it must
    keep working even when a driver is the thing under test.
    """
    provider = _resolve_setting("FILES_STORE_PROVIDER", "filesystem")
    endpoint = _resolve_setting("FILES_STORE_ENDPOINT")
    if provider == "filesystem" or not endpoint:
        return None
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=_resolve_setting("FILES_STORE_REGION", "us-east-1"),
        aws_access_key_id=_resolve_setting("FILES_STORE_ACCESS_KEY") or None,
        aws_secret_access_key=_resolve_setting("FILES_STORE_SECRET_KEY") or None,
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=15,
            retries={"max_attempts": 1},
        ),
    )


def _files_test_store() -> FilesTestStore | None:
    """This session's handle on the configured store, or ``None`` for no endpoint.

    ``None`` too when the endpoint was found dead and the session fell back to
    the filesystem driver: there is no bucket to make, and boto3's own connect
    ladder against a refused port is exactly the wait the fallback exists to
    stop paying.
    """
    if _FILES_STORE_FALLBACK is not None:
        return None
    client = _s3_test_client()
    if client is None:
        return None
    return FilesTestStore(
        client, admin_endpoint=_resolve_setting("FILES_STORE_ADMIN_ENDPOINT") or None
    )


class NotATestBucketError(AssertionError):
    """Raised when something asks the helper below to remove a name that is not
    a test run's. It is an ``AssertionError`` because the only way to reach it is
    a bug in the suite: the one caller that matters here deletes buckets on a
    store a deployment also writes to."""


class FilesTestStore:
    """The bucket-level admin surface the suite needs and ``ObjectStore`` has not.

    Creating a bucket, emptying it, dropping it, and — the part S3 has no word
    for — dropping the SeaweedFS *collection* behind it are not object-store
    operations, and they have to keep working while a driver is the thing under
    test. So they live here, over a boto3 client and the store's admin API,
    rather than on the production store surface.

    Every removal goes through :meth:`delete_bucket_and_collection`, which
    refuses a name that is not a test run's — a wrong name on a dev endpoint is
    a deployment's data.
    """

    def __init__(self, client: Any, *, admin_endpoint: str | None, opener: Any = None) -> None:
        self._client = client
        self._admin = (admin_endpoint or "").rstrip("/") or None
        self._opener = opener or _urlopen

    def create_bucket(self, name: str) -> None:
        self._client.create_bucket(Bucket=name)

    def buckets(self) -> list[tuple[str, datetime | None]]:
        """Every bucket on the endpoint with the time it was created."""
        return [
            (str(row.get("Name", "")), row.get("CreationDate"))
            for row in self._client.list_buckets().get("Buckets", ())
        ]

    def delete_bucket_and_collection(self, name: str) -> None:
        """Remove a test run's bucket AND the collection its objects live in.

        Both halves matter. The S3 call empties the bucket and removes it, which
        is all S3 can express — but SeaweedFS files a bucket's objects under a
        collection of the same name, and the collection's volume files survive
        the bucket by themselves. That is what filled the dev store's disk: one
        collection per worker per run, none of them ever collected.

        The collection goes whether or not the S3 half succeeded. A store under
        the very pressure this exists to prevent answers DeleteBucket with an
        InternalError — a filer that cannot write cannot remove a directory
        either — and a full disk is exactly when the volume files have to go.
        An empty bucket costs a directory entry; its collection costs gigabytes.
        """
        if not name.startswith(FILES_TEST_BUCKET_PREFIX):
            raise NotATestBucketError(
                f"refusing to delete {name!r}: only buckets named {FILES_TEST_BUCKET_PREFIX}* "
                "belong to a test run, and this store also serves a deployment"
            )
        with contextlib.suppress(Exception):
            self._empty_bucket(name)
            self._client.delete_bucket(Bucket=name)
        self._delete_collection(name)

    def _empty_bucket(self, name: str) -> None:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": name, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kwargs)
            for row in page.get("Contents", ()):
                with contextlib.suppress(Exception):
                    self._client.delete_object(Bucket=name, Key=row["Key"])
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated"):
                break

    def _delete_collection(self, name: str) -> None:
        """Drop the collection on the master. Best effort: an endpoint whose
        master is not published still gets its bucket removed, and the sweep
        below takes the collection the next time a run finds a master."""
        if self._admin is None:
            return
        with contextlib.suppress(Exception):
            self._opener(f"{self._admin}/col/delete?collection={quote(name, safe='')}")


def _urlopen(url: str) -> None:
    from urllib.request import urlopen

    with urlopen(url, timeout=15):  # noqa: S310 - a fixed http URL built from settings
        return


#: How old a run's bucket has to be before another run may remove it. The
#: teardown below already drops this session's bucket; this age is only about
#: the sessions that never reached a teardown — a killed run, a crashed worker,
#: a host that rebooted mid-suite. Hours rather than minutes because the only
#: thing that must never happen is reaping a bucket a CONCURRENT session is
#: still writing into, and no suite in this repo runs for hours.
STALE_TEST_BUCKET_AGE = timedelta(hours=6)


def reap_stale_test_buckets(
    store: FilesTestStore,
    *,
    keep: str,
    now: datetime,
    older_than: timedelta = STALE_TEST_BUCKET_AGE,
) -> list[str]:
    """Remove the buckets — and collections — that runs which never finished left.

    Returns what it removed. A session that is killed — the exact way the dev
    store filled up with volume files nothing will ever read in the first place
    — never runs its teardown, so "the suite cleans up after itself" is only
    true while nothing goes wrong. This makes it true afterwards as well,
    without ever touching a bucket that is not this suite's or that could still
    be in use.
    """
    reaped: list[str] = []
    cutoff = now - older_than
    for name, created in store.buckets():
        if name == keep or not name.startswith(FILES_TEST_BUCKET_PREFIX):
            continue
        if created is None or created > cutoff:
            continue
        with contextlib.suppress(Exception):
            store.delete_bucket_and_collection(name)
            reaped.append(name)
    return reaped


@pytest.fixture(scope="session")
def stale_test_bucket_reaper() -> Any:
    """The reaper itself, for the cases that pin which buckets it may take."""
    return reap_stale_test_buckets


@pytest.fixture(scope="session")
def files_test_store_class() -> Any:
    """The admin helper itself, for the cases that pin what it removes."""
    return FilesTestStore


@pytest.fixture(scope="session", autouse=True)
def files_deployment_bucket() -> str | None:
    """The bucket the environment named before this session took it over.

    ``None`` when nothing configured one. A test asserts the suite is not
    pointed at it; nothing in the suite may write to it.
    """
    return _DEPLOYMENT_FILES_BUCKET or None


@pytest.fixture(scope="session")
def files_endpoint_probe() -> Any:
    """The reachability check itself, for the cases that pin what it decides."""
    return files_endpoint_reachable


@pytest.fixture(scope="session")
def files_store_header() -> Any:
    """The header line builder, for the case that pins what a run announces."""
    return pytest_report_header


@pytest.fixture(scope="session", autouse=True)
def files_store_fallback() -> Generator[tuple[str, str] | None, None, None]:
    """``(unreachable endpoint, local root)`` when this session stood the
    filesystem driver in for an absent object store, else ``None``.

    A test that needs to know which driver it is actually running on asks for
    this rather than re-deciding; the directory goes at the end of the session.
    """
    fallback = _FILES_STORE_FALLBACK
    try:
        yield fallback
    finally:
        if fallback is not None:
            shutil.rmtree(fallback[1], ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def files_test_bucket() -> Generator[str | None, None, None]:
    """This run's own Files bucket: created on the configured store, dropped at
    the end of the session.

    The redirect itself already happened at this module's import — nothing can
    reach the deployment's bucket whether or not this fixture manages to create
    anything — so every store call here is best effort: a session with no
    SeaweedFS running simply has no bucket, and the Files tests that need one
    fail on their own first call rather than taking the whole session down.
    """
    name = _FILES_BUCKET
    if name is None:
        yield None
        return
    store = _files_test_store()
    if store is not None:
        with contextlib.suppress(Exception):
            store.create_bucket(name)
        with contextlib.suppress(Exception):
            reap_stale_test_buckets(store, keep=name, now=datetime.now(UTC))
    try:
        yield name
    finally:
        if store is not None:
            with contextlib.suppress(Exception):
                store.delete_bucket_and_collection(name)


@pytest.fixture(scope="session", autouse=True)
def disposable_database() -> str:
    """The database this session is cleared to write to.

    The refusal already happened at this module's import — nothing reaches a fixture
    without passing it. This re-proves it at setup so a fixture that clears tables can
    SAY what it depends on (``real_session`` does), and so an environment mutated
    mid-session cannot quietly open a door the session started with closed.
    """
    return _require_disposable_database()


# --- a timed-out async test does not outlive itself ------------------------------------

#: How long the loop may run after a timed-out test to let the tasks it abandoned
#: take their cancellation and run their cleanup — close the served stub, stop the
#: service. Bounded so a task that ignores cancellation is named, not waited on.
ABANDONED_TASK_DRAIN_SECONDS = 15.0


def _asyncio_test_loop(item: pytest.Item) -> asyncio.AbstractEventLoop | None:
    """The loop pytest-asyncio runs ``item`` on, or ``None`` for a sync test.

    Resolved the way the plugin's own ``runtest`` does — the runner fixture of the
    test's loop scope — so this never guesses at a "current" loop.
    """
    scope = getattr(item, "_loop_scope", None)
    request = getattr(item, "_request", None)
    if not isinstance(scope, str) or request is None:
        return None
    try:
        runner = request.getfixturevalue(f"_{scope}_scoped_runner")
        loop: asyncio.AbstractEventLoop = runner.get_loop()
    except Exception:
        return None
    return None if loop.is_closed() else loop


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Reap what a test pytest-timeout ended left running on the shared loop.

    The timeout's signal raises out of the loop's selector poll, so the test
    fails but the task running its coroutine is neither finished nor cancelled:
    it stays pending, and the session loop resumes it under the next test. Its
    ``finally`` blocks never run, and the server it was serving, the service it
    started and the sockets it opened all outlive it — one such test kept a
    mirror service heartbeating against its stub for the rest of a two-hour run,
    and the tests that shared the worker with that zombie went red by the dozen.

    So the tasks pending before the call are noted, and after a timeout every
    task that appeared since is cancelled and drained, which runs the cleanup
    the test would have. The report names each one, and any that ignored the
    cancellation.
    """
    loop = _asyncio_test_loop(item)
    before = pending_tasks(loop) if loop is not None else frozenset()
    try:
        return (yield)
    except BaseException as exc:
        if loop is not None and is_pytest_timeout(exc):
            reaped = reap_abandoned_tasks(loop, before, drain_seconds=ABANDONED_TASK_DRAIN_SECONDS)
            if reaped:
                item.add_report_section(
                    "call",
                    "asyncio tasks the timeout abandoned",
                    format_report(reaped, drain_seconds=ABANDONED_TASK_DRAIN_SECONDS),
                )
        raise


@pytest.fixture(autouse=True)
def _saas_billing_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the billing engine to SaaS mode (Stripe configured) for the default
    suite, so the result is deterministic regardless of whether a developer's
    ``.env.local`` happens to set Stripe keys or CI leaves them unset.

    ``settings.is_self_hosted`` is exactly ``not stripe_configured`` (which needs
    BOTH the secret and the webhook secret). When it is True the cycle engine
    provisions every seat on the ENTERPRISE plan (a per-user allotment synced from
    Alkera) instead of FREE — the wrong baseline for the large body of free-tier /
    SaaS billing + gateway tests, which then see Enterprise allotments, no pool
    funding, and absent meters. Locally a dev's ``.env.local`` Stripe keys make
    the suite SaaS and green; CI has no keys and would otherwise flip the whole
    suite self-hosted. Force SaaS here so the two agree.

    The self-hosted / Enterprise tests clear these explicitly in their own
    fixtures/bodies (``monkeypatch.setattr(settings, "stripe_secret_key", None)``);
    a later setattr on the shared instance wins, so this default never overrides
    a test that deliberately opts into self-hosted. Imported lazily inside the
    fixture (never at module load — see this module's docstring on why importing
    ``alkera_core.config`` early would cache a settings instance bound to the
    wrong worker DB)."""
    from alkera_core.config import settings

    # Pin ONLY when unset: the goal is `stripe_configured == True` (SaaS mode),
    # not replacing real credentials — the opt-in `stripe_live` tier dials real
    # Stripe with `settings.stripe_secret_key` and must keep the `.env.local` key.
    if not settings.stripe_secret_key:
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_suite", raising=False)
    if not settings.stripe_webhook_secret:
        monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_suite", raising=False)


REAL_STRIPE_MARKER = "stripe_live"
"""The tier that dials Stripe on purpose; every other test is refused the wire."""


@pytest.fixture(autouse=True)
def _stripe_never_dialled(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test outside the ``stripe_live`` tier reaches Stripe.

    The suite pins Stripe as configured (above) with a key Stripe rejects, so a
    path that builds the production gateway from settings instead of taking the
    fake through its seam goes on the wire and fails with an authentication
    error that names nothing. The production gateway's transport is the SDK's
    ``HTTPXClient``; its sends are replaced with a refusal that names the seam
    to use. A recorded transport (``_stripe_recorded``) is its own client class
    and is untouched."""
    if request.node.get_closest_marker(REAL_STRIPE_MARKER) is not None:
        return
    if importlib.util.find_spec("stripe") is None:
        return
    import stripe

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "a test tried to reach Stripe through the production gateway. Tests never "
            "dial Stripe: install a FakeStripeGateway through the seam the code path reads "
            "(the backend's build_stripe_gateway override, set_erasure_gateway, or the "
            f"gateway argument), or mark the test @pytest.mark.{REAL_STRIPE_MARKER}."
        )

    async def refuse_async(*args: Any, **kwargs: Any) -> Any:
        refuse(*args, **kwargs)

    for name in ("request", "request_stream"):
        monkeypatch.setattr(stripe.HTTPXClient, name, refuse)
    for name in ("request_async", "request_stream_async"):
        monkeypatch.setattr(stripe.HTTPXClient, name, refuse_async)


@pytest.fixture(autouse=True)
def release_host() -> Generator[Any, None, None]:
    """The release host every bootstrap render reads, in memory: a stable
    release with every command this checkout has. A test that needs another
    build or channel edits ``release_host.documents``; nothing reaches the
    real host."""
    from alkera_core.compute.box_builds import use_release_host
    from alkera_test_support.compute.fake_release_host import FakeReleaseHost

    host = FakeReleaseHost.serving()
    previous = use_release_host(host.fetcher)
    try:
        yield host
    finally:
        use_release_host(previous)


@pytest.fixture(autouse=True)
def _readiness_never_latched() -> Generator[None, None, None]:
    """Every test starts in a process that has never answered a ready probe.

    The readiness latch is per process (``alkera_core.readiness``): once a probe
    succeeds, a later failed one answers 200 inside the grace window. Left to
    carry over, whether a test's dead database reads 503 would depend on which
    test ran before it. A test of the grace itself records the ready probe it
    needs. Imported lazily for the same reason as the settings above."""
    from alkera_core.readiness import probe_latch

    probe_latch.reset()
    yield
    probe_latch.reset()


# --- no test dials an SMTP relay ------------------------------------------------------


@pytest.fixture(autouse=True)
def smtp_outbox(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Every email the suite sends lands here instead of on a socket.

    ``alkera_core.email._send`` is the one SMTP path, and it swallows a failed
    send (logs a warning, returns), so no caller can tell a relay that accepted
    from one that was never there. What differs is the time: with no relay on the
    box a connect to ``localhost:1025`` is refused at once on Linux and after two
    seconds on Windows, and a billing journey that sends eighteen emails spent
    thirty-seven seconds there on the Windows worker shard. The relay is the
    nondeterministic boundary, so the suite fakes it: ``aiosmtplib.send`` becomes
    a recorder of ``(message, keyword arguments)``. Request the fixture by name to
    assert on what was sent; a test of the send path itself may still replace
    ``aiosmtplib.send`` with its own fake, which wins over this one.
    """
    import aiosmtplib

    sent: list[tuple[Any, dict[str, Any]]] = []

    async def _record(message: Any, **kwargs: Any) -> None:
        sent.append((message, kwargs))

    monkeypatch.setattr(aiosmtplib, "send", _record)
    return sent


# --- no test reaches a real Temporal server -----------------------------------------

REAL_TEMPORAL_MARKER = "real_temporal_client"
"""Opt out of the guard below — the test drives a real client on purpose."""


@dataclass(frozen=True, slots=True)
class RecordedNudge:
    """One nudge, as the seam saw it: what would have been started, where, with what."""

    workflow: str
    workflow_id: str
    task_queue: str
    args: tuple[Any, ...]
    signal: str | None


class NudgeRecorder:
    """The client a nudge gets instead of a connection: it records and never dials.

    It answers the one method ``start_workflow_best_effort`` calls on a real
    ``temporalio.client.Client``, so a nudge still runs its whole production path
    — the id it builds, the queue it targets, the conflict policy, the budget —
    and stops at the wire.
    """

    def __init__(self) -> None:
        self.nudges: list[RecordedNudge] = []

    async def start_workflow(self, workflow: str, **kwargs: Any) -> object:
        self.nudges.append(
            RecordedNudge(
                workflow=workflow,
                workflow_id=kwargs["id"],
                task_queue=kwargs["task_queue"],
                args=tuple(kwargs.get("args", ())),
                signal=kwargs.get("start_signal"),
            )
        )
        return object()

    def for_workflow(self, workflow: str) -> list[RecordedNudge]:
        """Every nudge recorded for one workflow type, in the order they were sent."""
        return [nudge for nudge in self.nudges if nudge.workflow == workflow]


@pytest.fixture
def nudge_recorder() -> NudgeRecorder:
    """What the backend's nudges did instead of reaching an orchestrator.

    Every test has one installed (the autouse guard below); ask for it when the
    nudge is part of what the test is asserting.
    """
    return NudgeRecorder()


@pytest.fixture(autouse=True)
def _no_test_reaches_a_real_temporal_server(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, nudge_recorder: NudgeRecorder
) -> None:
    """Fail closed: a test opens a Temporal connection only when it says so.

    The backend's nudges are fired by ordinary service code — a team connection
    saved, rotated or revalidated, a gate report ingested, a Stripe webhook
    recorded — so a test that drives one of those routes reaches the
    ``temporal_client_provider`` seam without ever mentioning Temporal. Left at
    its production default that provider dials ``settings.temporal_address``,
    which on a developer's machine (``.env.workspace``) names the workspace's own
    compose server: the test starts a REAL keyed workflow for a row that exists
    only in its own database, nothing polls that queue while the suite runs, and
    the next worker started drains the backlog and fails every one of them four
    times over rows it can never see. Where nothing answers the address the nudge
    starts nothing but spends its budget failing instead.

    So the provider is replaced for every test with a recorder that never
    connects, and ``connect_client`` — the single place a connection is opened
    from settings — refuses outright, which catches any future caller that dials
    implicitly rather than through the nudge seam.

    Two ways to opt in. A test that wants a real dev-server client behind the
    nudges just sets the provider itself: this fixture is autouse and therefore
    runs first, so a later ``monkeypatch.setattr`` wins (``test_task_queue_live``
    and ``apps/worker/tests/test_nudges`` do exactly that). A test that asserts on
    the production default itself, or calls ``connect_client``, marks itself
    ``@pytest.mark.real_temporal_client``.
    """
    if request.node.get_closest_marker(REAL_TEMPORAL_MARKER) is not None:
        return

    from alkera_core.temporal import client as temporal_client_module
    from backend.services.infra import task_queue

    async def provide() -> NudgeRecorder:
        return nudge_recorder

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "a test tried to open a Temporal connection from settings "
            "(alkera_core.temporal.client.connect_client). Tests never reach a real server: "
            "drive the client a fixture hands you, or mark the test "
            f"@pytest.mark.{REAL_TEMPORAL_MARKER} when it really means to."
        )

    monkeypatch.setattr(task_queue, "temporal_client_provider", provide)
    monkeypatch.setattr(temporal_client_module, "connect_client", refuse)


def _temporal_binary() -> str | None:
    """The `temporal` CLI the dev server runs from: an explicit ALKERA_TEMPORAL_BIN,
    else one on PATH, else None (the SDK then downloads its pinned build)."""
    import shutil

    explicit = os.environ.get("ALKERA_TEMPORAL_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("temporal")


@pytest.fixture(scope="session")
async def temporal_env() -> AsyncIterator[Any]:
    """One local Temporal dev server per test session (per xdist worker, like the
    per-worker database), built with the same payload converter every client and
    Worker in the codebase uses. Lives in the ROOT conftest so the backend, worker
    and api-core suites all share it. Imported lazily: a suite that never asks for
    it never loads the SDK."""
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    path = _temporal_binary()
    if path is None and os.environ.get("CI"):
        pytest.fail(
            "the temporal CLI is missing on a CI runner; run ops/scripts/ensure-temporal-cli.sh"
        )
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=path,
        dev_server_log_level="warn",
        download_dest_dir=str(Path.home() / ".cache" / "alkera" / "temporal-cli" / "downloads"),
        data_converter=pydantic_data_converter,
    )
    try:
        yield env
    finally:
        await env.shutdown()


SINGLETON_REAP_REASON = "the test that left this singleton open has ended"


async def reap_open_singletons(client: Any) -> list[str]:
    """Terminate every run still open under a singleton id; return those ids.

    A singleton id (``drain_workflow_id``: the type name itself) is one identity
    on the session's dev server, and the production queue names it runs on are
    shared by every test on that server too. A test that nudges a singleton and
    ends before the run does — the drain's email hand-off is never awaited by
    design — leaves it open, with its next task waiting on the literal queue for
    whichever later test serves it. That test's own nudge then finds the run
    already going and signals it instead of starting one, and it inherits a
    stranger's run with an extra pass. Per-id terminate, not a visibility query:
    a run started milliseconds before teardown may not be listed yet, but the
    execution itself answers at once.
    """
    from alkera_core.temporal import QUEUE_FOR
    from temporalio.service import RPCError, RPCStatusCode

    async def _terminate(workflow_id: str) -> str | None:
        try:
            await client.get_workflow_handle(workflow_id).terminate(reason=SINGLETON_REAP_REASON)
        except RPCError as exc:
            # Never started, or already closed: nothing is left open under it.
            if exc.status is RPCStatusCode.NOT_FOUND:
                return None
            raise
        return workflow_id

    # Every type a nudge can name, the platform's and every registered family's.
    ids = sorted({str(t) for t in QUEUE_FOR})
    reaped = await asyncio.gather(*(_terminate(i) for i in ids))
    return [i for i in reaped if i is not None]


@pytest.fixture
async def temporal_client(temporal_env: Any) -> AsyncIterator[Any]:
    """The session dev server's client (``temporalio.client.Client``).

    Function-scoped over the session server so every test that touched the
    server hands it back with no singleton run left open (see
    :func:`reap_open_singletons`): the next test on this xdist worker starts
    from a server on which its own nudge is the first.
    """
    yield temporal_env.client
    await reap_open_singletons(temporal_env.client)


@pytest.fixture
def open_singleton_reaper() -> Any:
    """:func:`reap_open_singletons`, for the test that pins it."""
    return reap_open_singletons


# --- one module, one worker — unless the module says it can spread --------------------

SPREAD_MARKER = "spread"
"""``pytestmark = [pytest.mark.spread]``: this module's tests share nothing."""

GROUP_MARKER = "xdist_group"
"""xdist's own mark. An item that already carries one keeps the group it names."""


def module_group_name(nodeid: str) -> str:
    """The group every test in one module shares: the module's path in the node id."""
    return nodeid.partition("::")[0]


def assign_module_groups(items: list[pytest.Item]) -> None:
    """Give every ungrouped item its module's path as an xdist group.

    Skipped for an item that already names a group (its own or its module's) and
    for one whose chain carries ``spread``. Written as a plain function so it can
    be driven directly by a test; the hook below is the only caller.
    """
    marks: dict[str, pytest.MarkDecorator] = {}
    for item in items:
        grouped = spread = False
        for mark in item.iter_markers():
            if mark.name == GROUP_MARKER:
                grouped = True
                break
            if mark.name == SPREAD_MARKER:
                spread = True
        if grouped or spread:
            continue
        group = module_group_name(item.nodeid)
        mark = marks.get(group)
        if mark is None:
            mark = marks[group] = pytest.mark.xdist_group(name=group)
        item.add_marker(mark)


def interleave_ungrouped_items(items: list[pytest.Item]) -> None:
    """Space the items that carry no group evenly through the collection.

    Only a ``spread`` module's tests reach here ungrouped — everything else was
    just given its module as a group — and those are the long ones: the module
    that opts out does so because its tests are worth handing out one at a time.
    Adjacent in the file, they are adjacent in the collection too, and the
    scheduler reads that adjacency as "give these to the same worker".

    Rewritten in place, keeping the grouped items in their collection order, with
    the k-th ungrouped item (counting from one) placed after the first
    ``k * len(grouped) // len(ungrouped)`` of them. Consecutive ungrouped items
    are then separated by a whole ``len(grouped) // len(ungrouped)`` stretch of
    other tests, so a contiguous batch shorter than that stretch can carry at
    most one of them.

    Written as a plain function so it can be driven directly by a test; the hook
    below is the only caller.
    """
    grouped: list[pytest.Item] = []
    ungrouped: list[pytest.Item] = []
    for item in items:
        has_group = next(item.iter_markers(GROUP_MARKER), None) is not None
        (grouped if has_group else ungrouped).append(item)
    if not ungrouped:
        return

    spaced: list[pytest.Item] = []
    placed = 0
    for k, item in enumerate(ungrouped, start=1):
        upto = k * len(grouped) // len(ungrouped)
        spaced.extend(grouped[placed:upto])
        spaced.append(item)
        placed = upto
    spaced.extend(grouped[placed:])
    items[:] = spaced


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep each module's tests on one worker unless the module opts out.

    ``make test-py`` distributes with ``--dist loadgroup``, which hands out work one
    TEST at a time and keeps together only the tests that carry an ``xdist_group``
    mark. Per-test distribution is what stops a single slow module from deciding how
    long the run takes — but it also interleaves two modules' tests on one worker,
    and this suite has no per-test rollback: fixtures commit real rows, and a seed
    test reads what an earlier test in its own module wrote. Those tests fail the
    moment a neighbour's test lands between them, and finding every such module by
    reading 1,400 files is not something anyone can do reliably.

    So the default is the safe one — the behaviour ``--dist loadscope`` used to give:
    an item that names no group is given its own module's path as one, which pins the
    module whole to a worker. A module that provably shares nothing across its tests
    declares ``pytestmark = [pytest.mark.spread]`` and keeps distributing per test;
    ``ops/scripts/tests/test_xdist_groups.py`` refuses that declaration on a module
    that owns a module- or class-scoped fixture.

    ``tryfirst`` because xdist's worker hook (``xdist/remote.py``) reads the mark in
    its OWN ``pytest_collection_modifyitems`` — it appends ``@<group>`` to each item's
    node id, and the scheduler groups on that suffix. A mark added after that hook has
    run would never be seen. Serial runs keep the marks and nothing reads them.

    The ORDER handed on matters as much as the marks. That same xdist hook derives
    every group from the node ids in the order it receives them, and
    ``LoadGroupScheduling`` then builds its queue of work units in exactly that
    order and hands each worker the next ones off the front — topping every worker
    up to about two units at a time, so a worker can be given two or three
    consecutive units before it has finished the first. An ungrouped test is a work
    unit of one, so a run of them adjacent in the collection is a run of them on one
    worker, back to back: the Files corpus module's 14 half-minute tests did that to
    three workers, and the shard's last sixty seconds were those three chains alone.
    Spacing them out is what makes "hand these out one at a time" mean what it says.
    """
    assign_module_groups(items)
    interleave_ungrouped_items(items)


def _xdist_scheduling() -> Any:
    """``scripts/xdist_scheduling.py``, loaded by path: ``scripts/`` is not a
    package and is not put on ``sys.path`` (its ``files/`` would shadow the CLI
    tests' ``files`` helpers)."""
    name = "xdist_scheduling"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_make_scheduler(config: pytest.Config, log: Any) -> Any | None:
    """Distribute ``--dist loadgroup`` runs with the scheduler that survives a
    worker crash (``scripts/xdist_scheduling.py``).

    xdist's own keeps a crashed worker's finished units and hands them out again
    as empty commands, which parks every free worker at the end of the run with
    nothing executing — the 99 % hang — and it dies with an INTERNALERROR when
    the replacement worker collected differently. Any other ``--dist`` mode is
    left to xdist (``None`` falls through to its default), and ``optionalhook``
    because a ``-p no:xdist`` run has no such hook to implement.
    """
    return _xdist_scheduling().scheduler_for(config, log)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Leave the worker database where it is; drop the migration template this
    session made.

    Dropping the worker database here raced every other worker still running: a
    `DROP DATABASE` is a cluster-wide event, and a test elsewhere whose migration
    downgrade drops a role scans the shared dependency catalog at that moment and
    fails with "cache lookup failed for database". The next run's provisioning
    drops a leftover before it clones, so nothing accumulates across runs, and the
    session's async engine is not disposed either: its pooled connections are
    bound to the session loop, which is already closed by now.

    The migration template is different: a session that MADE one (the serial
    path, where the session's own database carries the pools) is the only
    session on that database, so there is no other worker to race, and a
    borrowed one is not this session's to drop. A template a crashed session
    left behind is replaced by name the next time one is made.
    """
    del config
    template = published_migration_template()
    if template is not None:
        drop_migration_template(template)
