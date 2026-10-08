"""What every revision that touches a live table goes through.

A migration runs once, against production, with nobody watching the rows. The
ways it goes wrong there are few and none of them show on a developer database:

* **A back-fill that sees nothing.** The Files tenant tables carry ``FORCE ROW
  LEVEL SECURITY``. A login that neither is a superuser nor holds ``BYPASSRLS``
  reads zero rows from them, so an ``UPDATE`` matches nothing and reports
  success. :func:`require_unfiltered` asks Postgres itself whether a policy
  would filter the tables a revision is about to rewrite, and refuses with the
  remedy when it would.
* **DDL queued behind a long reader.** An ``ALTER TABLE`` that waits for its
  lock makes every later statement on that table wait behind it.
  :func:`bound_lock_wait` makes the wait short, so a deploy fails fast and is
  re-run instead of stalling the fleet.
* **A scan or an index build under a lock that stops writers.**
  :func:`add_check_not_valid` adds a constraint without a scan and
  :func:`validate_constraints` scans it in a transaction of its own, and
  :func:`create_index_concurrently` builds outside any transaction, clearing the
  invalid index a failed earlier attempt left.
* **One statement that holds a whole table's rows until the run commits.**
  :func:`in_batches` walks a table by primary-key range, one committed
  statement per range.

:func:`lint_revision` reads a revision's source for the shapes that break
these rules before it ever runs, and the suite runs it over every revision
numbered above the head this module was last changed at.

Everything here that leaves the surrounding transaction commits what the
revision did before it. That is why every statement a revision issues is written
to be re-run: a revision that died half way is stamped as not applied, and the
next ``alembic upgrade`` runs the whole of it again.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Final

import sqlalchemy as sa
from alembic import op

#: Where an operator reads what to do about a refusal raised here.
RUNBOOK = "the deployment guide, 'The migration login'"


class RowSecurityBlocksMigrationError(RuntimeError):
    """The login running the migration cannot see the rows it has to rewrite."""


def _offline() -> bool:
    """``alembic upgrade --sql`` has no connection to ask anything of."""
    return bool(op.get_context().as_sql)


def _setting(name: str) -> int:
    """One ``MIGRATION_*`` value, read when a revision RUNS, never at import.

    Alembic imports every revision script to find the head, and a test session
    does that before it has pointed the environment at its own database; a
    settings singleton built that early binds every engine the process opens
    later to the wrong one. So this module holds no settings, and a revision
    that is only imported builds none.
    """
    from alkera_core.config import get_settings

    return int(getattr(get_settings(), name))


def bound_lock_wait() -> None:
    """Wait ``MIGRATION_LOCK_TIMEOUT_MS`` for a lock, then fail instead of queueing.

    ``SET LOCAL``: it ends with the transaction, so it cannot leak into whatever
    borrows the connection next when a test drives Alembic in-process.
    """
    op.execute(f"SET LOCAL lock_timeout = {_setting('migration_lock_timeout_ms')}")


@contextmanager
def outside_transaction() -> Iterator[None]:
    """Run the body with every statement committing on its own.

    Entering commits what the revision has done so far, which is the point: a
    lock taken by the DDL above is released before a scan or a build starts.
    ``SET LOCAL`` means nothing without a transaction, so the bounded wait is
    set on the session for the length of the block and handed back to the
    transaction Alembic opens afterwards.
    """
    with op.get_context().autocommit_block():
        op.execute(f"SET lock_timeout = {_setting('migration_lock_timeout_ms')}")
        yield
        op.execute("RESET lock_timeout")
    bound_lock_wait()


def require_unfiltered(revision: str, tables: Sequence[str]) -> None:
    """Refuse to run a back-fill over rows a policy would hide from this login.

    ``row_security_active`` is Postgres's own answer for the current user: it
    accounts for superuser, ``BYPASSRLS``, table ownership and ``FORCE`` in one
    place, so this does not re-derive any of them. ``row_security = off`` is the
    second line: from here to the end of the run a statement a policy would
    filter raises rather than quietly matching nothing, including on a table
    this call was not told about.
    """
    if _offline():
        return
    bind = op.get_bind()
    filtered = [
        table
        for table in tables
        if bind.execute(
            sa.text("SELECT coalesce(row_security_active(to_regclass(:name)), false)"),
            {"name": table},
        ).scalar_one()
    ]
    if filtered:
        login = bind.execute(sa.text("SELECT current_user")).scalar_one()
        raise RowSecurityBlocksMigrationError(
            f"revision {revision} rewrites rows in {', '.join(filtered)}, and row-level "
            f"security hides them from the login {login!r}: its statements would match no "
            "rows and report success. Run `alembic upgrade` as a login that is a superuser "
            f"or holds BYPASSRLS (ALTER ROLE {login} BYPASSRLS), then re-run. Nothing was "
            f"changed by this revision. See {RUNBOOK}."
        )
    op.execute("SET row_security = off")


def login_bypasses_row_security(bind: sa.Connection) -> bool:
    """Whether this login reads every row of a ``FORCE ROW LEVEL SECURITY`` table."""
    return bool(
        bind.execute(
            sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar()
    )


def add_check_not_valid(table: str, name: str, expression: str) -> None:
    """Add (or replace) a CHECK without scanning the table.

    ``NOT VALID`` still binds every row written from here on; only the scan of
    what is already there is put off to :func:`validate_constraints`, which does
    not block writers.
    """
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")


def validate_constraints(constraints: Sequence[tuple[str, str]]) -> None:
    """Validate ``(table, constraint)`` pairs, each in a transaction of its own.

    ``VALIDATE CONSTRAINT`` takes ``SHARE UPDATE EXCLUSIVE``, which admits every
    reader and writer — but only if the ``ACCESS EXCLUSIVE`` the ``ALTER TABLE``
    before it took has been released, which is what leaving the transaction does.
    """
    with outside_transaction():
        for table, name in constraints:
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def drop_constraint_if_exists(table: str, name: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def create_index_concurrently(
    name: str,
    table: str,
    columns: str,
    *,
    unique: bool = False,
    where: str | None = None,
) -> None:
    """Build an index without blocking the table's writers, re-runnably.

    A ``CONCURRENTLY`` build that fails — a lock timeout, a duplicate that
    arrived mid-build, a killed deploy — leaves an INVALID index under the name,
    which ``IF NOT EXISTS`` would then accept as the finished article. So an
    invalid index of this name is dropped first, and a valid one is kept.
    """
    statement = (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX CONCURRENTLY IF NOT EXISTS {name} "
        f"ON {table} ({columns})" + (f" WHERE {where}" if where else "")
    )
    with outside_transaction():
        if not _offline():
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) "
                        "AND NOT indisvalid"
                    ),
                    {"name": name},
                )
                .scalar()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(statement)


def drop_index_concurrently(name: str) -> None:
    with outside_transaction():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


#: Where :func:`in_batches` writes the range a statement has to keep itself to.
RANGE = "__RANGE__"


def in_batches(
    table: str,
    key: str,
    statement: str,
    *,
    key_type: str = "uuid",
    where: str = "TRUE",
    params: dict[str, Any] | None = None,
) -> int:
    """Run ``statement`` once per primary-key range of ``table``; answer the rows touched.

    ``statement`` names the walked table's key as ``key`` and carries the
    :data:`RANGE` token inside its ``WHERE``; each pass replaces it with the
    range being visited. Each range is its own committed statement, so the row
    locks of one are released before the next is taken and a failure keeps what
    was already done — which is why ``statement`` must be safe to run twice over
    the same range.

    ``where`` narrows the walk to the rows worth visiting, so a back-fill that
    concerns one row in a million does not issue a statement per empty range.
    """
    if _offline():
        raise RuntimeError(f"a batched back-fill of {table} needs a live connection")
    if RANGE not in statement:
        raise ValueError("a batched statement has to keep itself to the range it is given")
    bind = op.get_bind()
    low_bound = f"CAST(:low AS {key_type})"
    # Every identifier and predicate here is a revision's own literal.
    edge = sa.text(
        f"SELECT k FROM (SELECT {key} AS k FROM {table} "  # noqa: S608
        f"WHERE ({where}) AND ({low_bound} IS NULL OR {key} > {low_bound}) "
        f"ORDER BY {key} LIMIT :size) AS page ORDER BY k DESC LIMIT 1"
    )
    ranged = sa.text(
        statement.replace(
            RANGE,
            f"(({low_bound} IS NULL OR {key} > {low_bound}) "
            f"AND {key} <= CAST(:high AS {key_type}))",
        )
    )
    touched = 0
    low: Any = None
    with outside_transaction():
        while True:
            high = bind.execute(
                edge, {"low": low, "size": _setting("migration_batch_rows")}
            ).scalar()
            if high is None:
                break
            result = bind.execute(ranged, {**(params or {}), "low": low, "high": high})
            touched += max(result.rowcount or 0, 0)
            low = high
    return touched


# --------------------------------------------------------------------------- #
# The lint
# --------------------------------------------------------------------------- #

#: Alembic operations that take ``ACCESS EXCLUSIVE`` on a live table: every
#: read and write of the table queues behind one that waits.
ACCESS_EXCLUSIVE_OPS: Final = frozenset(
    {
        "add_column",
        "alter_column",
        "drop_column",
        "drop_table",
        "rename_table",
        "create_foreign_key",
        "drop_constraint",
        "drop_index",
    }
)
#: Operations that hold a blocking lock for a whole scan or build; the
#: helpers above do each without one.
BLOCKING_OPS: Final = frozenset(
    {"create_index", "create_check_constraint", "create_unique_constraint"}
)

_ALTER_SQL: Final = re.compile(r"\b(ALTER|DROP|LOCK)\s+TABLE\b", re.IGNORECASE)
_NOT_VALID: Final = re.compile(r"\bNOT\s+VALID\b|add_check_not_valid\s*\(", re.IGNORECASE)
_VALIDATE: Final = re.compile(r"\bVALIDATE\s+CONSTRAINT\b|validate_constraints\s*\(", re.IGNORECASE)


def _op_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "op":
            return func.attr
    return None


def _call_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        func = node.value.func
        if isinstance(func, ast.Name):
            return func.id
    return None


def _takes_access_exclusive(call: ast.Call) -> bool:
    name = _op_name(call)
    if name in ACCESS_EXCLUSIVE_OPS:
        return True
    if name == "execute":
        return any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and bool(_ALTER_SQL.search(arg.value))
            for arg in ast.walk(call)
        )
    return False


def _unbounded_autocommit(with_node: ast.With | ast.AsyncWith) -> bool:
    """A block that leaves the transaction without :func:`outside_transaction`,
    which is what sets the bound on the session for the block's length."""
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "autocommit_block"
        for item in with_node.items
    )


def lint_revision(source: str) -> list[str]:
    """What is wrong with one revision's source, as sentences; empty when
    nothing is.

    * ``upgrade()`` and ``downgrade()`` each bound the lock wait first
      (:func:`bound_lock_wait`), so every ``ACCESS EXCLUSIVE`` operation after
      it fails fast instead of queueing the table behind it;
    * no ``ACCESS EXCLUSIVE`` operation runs in a block that leaves the
      transaction any way but :func:`outside_transaction`: the bound set by
      ``SET LOCAL`` ended with the transaction, and only that helper sets it
      again on the session for the block;
    * no blocking build or validating constraint (:data:`BLOCKING_OPS`): the
      helpers here do each without the lock;
    * a constraint added ``NOT VALID`` is validated by a later revision, not
      this one: the validation scans the table, and a deploy whose scan runs
      long or fails should not be the deploy that changed the schema.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for direction in ("upgrade", "downgrade"):
        fn = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == direction
            ),
            None,
        )
        if fn is None:
            found.append(f"{direction}() is missing")
            continue
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if not body or _call_name(body[0]) != "bound_lock_wait":
            found.append(f"{direction}() takes locks before it bounds the wait for them")
        for node in ast.walk(fn):
            if isinstance(node, ast.With | ast.AsyncWith) and _unbounded_autocommit(node):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and _takes_access_exclusive(inner):
                        found.append(
                            f"{direction}() runs an ACCESS EXCLUSIVE operation with no bound on "
                            "its lock wait"
                        )
            if isinstance(node, ast.Call) and _op_name(node) in BLOCKING_OPS:
                found.append(f"{direction}() calls op.{_op_name(node)}() on a live table")
    if _NOT_VALID.search(source) and _VALIDATE.search(source):
        found.append("adds a constraint NOT VALID and validates it in the same revision")
    return found
