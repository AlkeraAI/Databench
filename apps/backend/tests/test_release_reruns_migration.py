"""This release's revisions run again over whatever a failed or repeated run left.

A revision that leaves its transaction (``backend.migration_safety``'s
validation, concurrent index builds and batched back-fills) commits everything
it did before that point, and is stamped only when it finishes. A deploy that
dies inside one -- a lock timeout, a killed task -- leaves the revision
unstamped with part of its DDL in place, and the runbook's answer is to run
``alembic upgrade head`` again. A database stamped under an earlier numbering of
these revisions is repaired by ``alembic stamp`` to the last shipped revision and
the same upgrade, which runs every revision again over objects that already
exist. Both only work if every statement is re-runnable, so this module makes
each run die exactly where it first leaves its transaction and runs it again,
then stamps a finished database back and upgrades it once more.

It also pins how the statements run: every DDL statement of these revisions
waits a bounded time for its lock, the ones after a committed step included, and
the operator-facing messages name the revision that raised them.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command, op
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from backend import migration_safety
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import (
    ScratchDatabase,
    alembic_config,
    migration_scratch,
    script_head,
)

_BACKEND = Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "alembic" / "versions"
#: The last revision a shared environment has run; everything after it is this
#: release.
_SHIPPED = "0176"
#: Every helper that leaves the revision's transaction, committing what came
#: before it.
_COMMITTING_HELPERS = (
    "validate_constraints",
    "create_index_concurrently",
    "drop_index_concurrently",
    "in_batches",
    "outside_transaction",
)


#: Revisions whose committing step runs only when they find something to
#: restore, which a database the corrected revisions built never offers.
_COMMITS_ONLY_TO_REPAIR = frozenset({"0194"})


def _release_revisions() -> list[Path]:
    return sorted(
        path
        for path in _VERSIONS.glob("0*.py")
        if path.name[:4].isdigit() and path.name[:4] > _SHIPPED
    )


def _upgrade_body(path: Path) -> str:
    source = path.read_text()
    body = re.search(r"^def upgrade\(\) -> None:\n(.*?)(?=^def |\Z)", source, re.M | re.S)
    assert body is not None, f"{path.name} has no upgrade()"
    return body.group(1)


def _commits_part_way(path: Path) -> bool:
    """Whether the revision's upgrade leaves its transaction, directly or through
    a function of its own module that does."""
    source = path.read_text()
    reached = _upgrade_body(path)
    for name in re.findall(r"^def (_\w+)\(", source, re.M):
        if re.search(rf"\b{name}\(", reached):
            body = re.search(rf"^def {name}\(.*?(?=^def |\Z)", source, re.M | re.S)
            assert body is not None
            reached += body.group(0)
    return any(re.search(rf"\b{helper}\(", reached) for helper in _COMMITTING_HELPERS)


class _RunDiedError(RuntimeError):
    """The deploy died just after the revision first committed part of itself."""


@contextmanager
def _dies_on_leaving_the_transaction() -> Iterator[None]:
    with op.get_context().autocommit_block():
        pass
    raise _RunDiedError("the run died as the revision left its transaction")
    yield  # pragma: no cover - never reached; a generator for @contextmanager


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


# --- a run that died part way is finished by running it again ----------------------


async def test_every_revision_runs_again_after_dying_where_it_first_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walk the release one revision at a time. Each revision that leaves its
    transaction is made to die right there, with what it did before committed and
    its stamp not written, then run again; it must finish."""
    revisions = [path.name[:4] for path in _release_revisions()]
    expected_to_die = {
        path.name[:4] for path in _release_revisions() if _commits_part_way(path)
    } - _COMMITS_ONLY_TO_REPAIR
    died: set[str] = set()
    refused: dict[str, str] = {}
    async with migration_scratch() as scratch:
        await scratch.downgrade(_SHIPPED)
        before = _SHIPPED
        for revision in revisions:
            with monkeypatch.context() as patch:
                patch.setattr(
                    migration_safety, "outside_transaction", _dies_on_leaving_the_transaction
                )
                try:
                    await scratch.upgrade(revision)
                except _RunDiedError:
                    died.add(revision)
            assert scratch.revision() == (before if revision in died else revision)
            try:
                await scratch.upgrade(revision)
            except Exception as error:  # every refusal is reported, not just the first
                refused[revision] = f"{type(error).__name__}: {error}"
                break
            assert scratch.revision() == revision
            before = revision
    assert refused == {}
    assert died == expected_to_die


@pytest.mark.parametrize(
    "revision", [path.name[:4] for path in _release_revisions()], ids=lambda r: r
)
async def test_a_finished_revision_runs_again_and_changes_nothing(revision: str) -> None:
    """The repair for a database stamped under an earlier numbering stamps the
    last shipped revision and upgrades, which runs every revision again over its
    own finished work. Each must succeed and leave every row as it was --
    including rows a careless data step would rewrite."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            await _seed_head(session)
            await session.commit()
        before = await _fingerprint(scratch)
        parent = ScriptDirectory.from_config(scratch.config).get_revision(revision).down_revision
        assert isinstance(parent, str)

        command.stamp(scratch.config, parent)
        await scratch.upgrade(revision)
        command.stamp(scratch.config, "head")

        assert await _fingerprint(scratch) == before


#: The rows the release's data steps rewrite, read in a stable order.
_FINGERPRINTED = (
    "SELECT id, is_active FROM users ORDER BY id",
    "SELECT user_id, org_team_id, status, credential_epoch, deactivated_at "
    "FROM org_memberships ORDER BY user_id, org_team_id",
    "SELECT id, user_id, team_id, org_team_id FROM team_memberships ORDER BY id",
    "SELECT id, scope, owner_user_id, org_team_id FROM billing_accounts ORDER BY id",
    "SELECT user_id, free_grant_org_team_id, free_granted_at "
    "FROM identity_billing_grants ORDER BY user_id",
    "SELECT id, user_id, org_team_id, workspace_key, plugin FROM connection_inventory ORDER BY id",
    "SELECT user_id, org_team_id, preferences FROM user_org_preferences "
    "ORDER BY user_id, org_team_id",
    "SELECT id, reasoning_format, reads_reasoning_formats FROM billing_models ORDER BY id",
)


async def _fingerprint(scratch: ScratchDatabase) -> dict[str, str]:
    async with scratch.session() as session:
        prints: dict[str, str] = {}
        for query in _FINGERPRINTED:
            rows = (await session.execute(text(query))).all()
            prints[query] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return prints


async def _seed_head(session: AsyncSession) -> None:
    """Rows of every kind the release's data steps touch, written at head the way
    this release writes them -- including the ones a careless re-run would
    rewrite: a platform-disabled identity whose org membership is still active,
    and a person the org deactivated whose identity is enabled."""
    org = await _scalar(
        session,
        "INSERT INTO teams (id, name, is_root) VALUES (gen_random_uuid(), "
        "'rerun-' || substr(md5(random()::text), 1, 8), true) RETURNING id",
    )
    # A reasoning model whose read list 0182 widened after 0181 back-filled it
    # (a re-derivation by 0181 would drop the read 0182 added).
    await session.execute(
        text(
            "INSERT INTO billing_models (id, display_name, family, supports_thinking, "
            "reasoning_format, reads_reasoning_formats) VALUES ("
            "'rerun-' || substr(md5(random()::text), 1, 8), 'Rerun', 'claude', true, "
            "'anthropic:claude-sonnet-5', ARRAY['anthropic:claude-haiku-4-5'])"
        )
    )
    disabled = await _scalar(
        session,
        "INSERT INTO users (id, org_team_id, email) VALUES (gen_random_uuid(), :o, "
        "'rerun-off-' || substr(md5(random()::text), 1, 8) || '@alkera.dev') RETURNING id",
        o=org,
    )
    await session.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": disabled})
    await session.execute(
        text(
            "INSERT INTO identity_security_events (id, user_id, event) "
            "VALUES (gen_random_uuid(), :u, 'platform.user_disabled')"
        ),
        {"u": disabled},
    )
    offboarded = await _scalar(
        session,
        "INSERT INTO users (id, org_team_id, email) VALUES (gen_random_uuid(), :o, "
        "'rerun-gone-' || substr(md5(random()::text), 1, 8) || '@alkera.dev') RETURNING id",
        o=org,
    )
    await session.execute(
        text(
            "UPDATE org_memberships SET status = 'deactivated', deactivated_at = now(), "
            "credential_epoch = credential_epoch + 1 WHERE user_id = :u"
        ),
        {"u": offboarded},
    )
    for user in (disabled, offboarded):
        await session.execute(
            text(
                "INSERT INTO billing_accounts (id, scope, owner_user_id, org_team_id) "
                "VALUES (gen_random_uuid(), 'user', :u, :o)"
            ),
            {"u": user, "o": org},
        )
        await session.execute(
            text(
                "INSERT INTO team_memberships (id, user_id, team_id) "
                "VALUES (gen_random_uuid(), :u, :o)"
            ),
            {"u": user, "o": org},
        )
        await session.execute(
            text(
                "INSERT INTO connection_inventory (id, user_id, org_team_id, workspace_key, "
                "plugin) VALUES (gen_random_uuid(), :u, :o, 'ws', 'postgres')"
            ),
            {"u": user, "o": org},
        )


# --- every DDL statement waits a bounded time for its lock ---------------------------

_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP|COMMENT|GRANT|REVOKE|UPDATE|INSERT|DELETE)\b", re.I)


async def test_every_statement_of_the_release_waits_a_bounded_time_for_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Including the statements that run after a step left the transaction and
    came back: an unbounded wait there queues every later reader of the table
    behind the migration for as long as some other transaction holds it."""
    unbounded: list[str] = []
    probed_statements = 0
    original: Callable[..., Any] = Operations.execute

    def probed(self: Operations, sqltext: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal probed_statements
        statement = str(sqltext)
        if _DDL.match(statement):
            probed_statements += 1
            bind = self.migration_context.connection
            assert bind is not None
            wait = bind.exec_driver_sql("SELECT current_setting('lock_timeout')").scalar()
            if wait == "0":
                unbounded.append(" ".join(statement.split())[:120])
        return original(self, sqltext, *args, **kwargs)

    async with migration_scratch() as scratch:
        await scratch.downgrade(_SHIPPED)
        monkeypatch.setattr(Operations, "execute", probed)
        await scratch.upgrade()
    assert unbounded == []
    # The probe saw the release's statements, not a path that bypasses it.
    assert probed_statements > 50


# --- what an operator reads names the revision that raised it ------------------------


def _string_constants(tree: ast.Module) -> Iterator[str]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


@pytest.mark.parametrize("path", _release_revisions(), ids=lambda path: path.name[:4])
def test_an_operator_facing_message_names_the_revision_that_raised_it(path: Path) -> None:
    """A message renumbered away from its revision sends whoever reads it during
    an incident to the wrong file."""
    tree = ast.parse(path.read_text())
    own = path.name[:4]
    # A message that opens with a revision number is that revision speaking;
    # one that mentions another revision further on is pointing at it.
    speaking = {
        match.group(1)
        for constant in _string_constants(tree)
        if (match := re.match(r"revision (\d{4})\b", constant))
    }
    assert speaking <= {own}, f"{path.name} speaks as revision(s) {sorted(speaking - {own})}"
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "require_unfiltered"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        ):
            assert call.args[0].value == own, (
                f"{path.name} reports as revision {call.args[0].value}"
            )


@pytest.mark.parametrize("path", _release_revisions(), ids=lambda path: path.name[:4])
def test_a_constraint_is_validated_outside_the_transaction_that_added_it(path: Path) -> None:
    """``NOT VALID`` exists to put the scan where it blocks no writer. Validated in
    the transaction that added it, the scan runs under the ``ACCESS EXCLUSIVE``
    the ``ADD`` took, and ``NOT VALID`` bought nothing."""
    assert "VALIDATE CONSTRAINT" not in path.read_text().upper(), (
        f"{path.name} validates a constraint itself; use validate_constraints()"
    )


def test_the_release_walk_reaches_the_head() -> None:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert [path.name[:4] for path in _release_revisions()][-1] == head


# --- a lock wait on one table holds none of the tables before it ---------------------


async def test_policing_a_table_holds_none_of_the_tables_policed_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row security goes on table by table, each statement committing on its own.
    A reader holding the last table makes the run fail fast there -- and the
    tables before it are already policed and released, not held under ``ACCESS
    EXCLUSIVE`` while the run waits; the re-run finishes the rest."""
    from alkera_core.config import get_settings
    from sqlalchemy import create_engine, pool

    monkeypatch.setattr(get_settings(), "migration_lock_timeout_ms", 500)
    async with migration_scratch() as scratch:
        await scratch.downgrade("0187")
        reader = create_engine(scratch.sync_url, poolclass=pool.NullPool)
        try:
            with reader.connect() as held:
                held.execute(text("LOCK TABLE team_memberships IN ACCESS SHARE MODE"))
                with pytest.raises(Exception, match="lock timeout"):
                    await scratch.upgrade("0188")
                held.rollback()
        finally:
            reader.dispose()
        assert scratch.revision() == "0187"
        async with scratch.session() as session:
            policed = {
                str(row[0])
                for row in await session.execute(
                    text(
                        "SELECT c.relname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                        "WHERE p.polname = 'tenant_isolation' AND c.relforcerowsecurity"
                    )
                )
            }
        assert {"workspace_objects", "chat_messages", "kb_items"} <= policed
        assert "team_memberships" not in policed

        await scratch.upgrade()
        assert scratch.revision() == script_head(scratch.config)


async def test_a_database_an_earlier_build_migrated_is_repaired_by_running_the_release_again() -> (
    None
):
    """An earlier build of these revisions dropped the previous release's inventory
    key and added no seat fill, and its revisions were numbered differently. A
    database it migrated is stamped at a head that names other revisions, so the
    repair is to stamp the last shipped revision and upgrade: every revision of
    the release runs again over its own work and brings back what the earlier
    build left out, and an identity its org deactivated stays deactivated."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            org = await _scalar(
                session,
                "INSERT INTO teams (id, name, is_root) VALUES (gen_random_uuid(), "
                "'earlier-' || substr(md5(random()::text), 1, 8), true) RETURNING id",
            )
            person = await _scalar(
                session,
                "INSERT INTO users (id, org_team_id, email) VALUES (gen_random_uuid(), :o, "
                "'earlier-' || substr(md5(random()::text), 1, 8) || '@alkera.dev') RETURNING id",
                o=org,
            )
            # What the earlier build left behind: an org deactivation, the
            # missing trigger and the missing key.
            await session.execute(
                text("UPDATE users SET is_active = false WHERE id = :u"), {"u": person}
            )
            await session.execute(
                text("UPDATE org_memberships SET status = 'deactivated' WHERE user_id = :u"),
                {"u": person},
            )
            await session.execute(
                text("DROP TRIGGER trg_billing_accounts_seat_org ON billing_accounts")
            )
            await session.execute(
                text(
                    "ALTER TABLE connection_inventory "
                    "DROP CONSTRAINT uq_connection_inventory_user_workspace_plugin"
                )
            )
            await session.commit()

        command.stamp(scratch.config, _SHIPPED)
        await scratch.upgrade()

        async with scratch.session() as session:
            assert await _scalar(
                session,
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_billing_accounts_seat_org'",
            )
            assert await _scalar(
                session,
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'uq_connection_inventory_user_workspace_plugin'",
            )
            assert (
                await _scalar(session, "SELECT is_active FROM users WHERE id = :u", u=person)
                is False
            )
            seat_org = await _scalar(
                session,
                "INSERT INTO billing_accounts (id, scope, owner_user_id) "
                "VALUES (gen_random_uuid(), 'user', :u) RETURNING org_team_id",
                u=person,
            )
            assert seat_org == org
            await session.rollback()


# --- a database the earlier build of these revisions migrated ------------------------

_EARLIER_GRANT_LOOKUP = (
    "SELECT org_team_id INTO owner_org FROM team_connections WHERE id = NEW.team_connection_id;"
)


async def _earlier_build_objects(session: AsyncSession) -> dict[str, Any]:
    return {
        "seat fill": await _scalar(
            session,
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_billing_accounts_seat_org'",
        ),
        "inventory key": await _scalar(
            session,
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'uq_connection_inventory_user_workspace_plugin'",
        ),
        "grant fallback": await _scalar(
            session,
            "SELECT pg_get_functiondef('fn_user_oauth_tokens_org'::regproc)",
        ),
    }


async def test_a_database_the_earlier_build_left_at_its_head_is_brought_to_the_corrected_one() -> (
    None
):
    """No stamp, no operator: a database an earlier build of 0187-0189 migrated
    is stamped at a head the corrected revisions also pass, so they never run on
    it again. The upgrade that follows restores the seat fill, the grant
    fallback and the previous release's inventory key, after which the schema
    matches the models; on a database the corrected revisions built, it changes
    nothing."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            corrected = await _earlier_build_objects(session)
        head = script_head(scratch.config)

        # On a corrected database the revision is a no-op.
        command.stamp(scratch.config, "0193")
        await scratch.upgrade()
        async with scratch.session() as session:
            assert await _earlier_build_objects(session) == corrected

        # The earlier build's shape: no seat fill, no fallback, no key.
        async with scratch.session() as session:
            definition = corrected["grant fallback"]
            assert "team_root_of" in definition
            earlier = re.sub(
                r"SELECT coalesce\(org_team_id, team_root_of\(team_id\)\) INTO owner_org\s+"
                r"FROM team_connections WHERE id = NEW\.team_connection_id;",
                _EARLIER_GRANT_LOOKUP,
                definition,
            )
            assert "team_root_of" not in earlier
            await session.execute(text(earlier))
            await session.execute(
                text("DROP TRIGGER trg_billing_accounts_seat_org ON billing_accounts")
            )
            await session.execute(text("DROP FUNCTION fn_billing_accounts_seat_org()"))
            await session.execute(
                text(
                    "ALTER TABLE connection_inventory "
                    "DROP CONSTRAINT uq_connection_inventory_user_workspace_plugin"
                )
            )
            await session.commit()
        command.stamp(scratch.config, "0193")

        await scratch.upgrade()

        assert scratch.revision() == head
        async with scratch.session() as session:
            restored = await _earlier_build_objects(session)
        assert restored["seat fill"] == 1
        assert restored["inventory key"] == 1
        assert "team_root_of" in restored["grant fallback"]
        # The schema matches the models again (raises on any drift).
        command.check(scratch.config)
