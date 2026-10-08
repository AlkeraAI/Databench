"""Every lock is taken through the one module that knows the order.

A row lock, an advisory lock or a lock-wait override written at a call site is
a lock nobody ranked: it can be taken in any order against the others, held
across a network call, or wait for as long as the caller's patience. The rules
for all three live in :mod:`alkera_core.db.locking`, and this scan keeps new
code going through it. It walks the server-side packages with :mod:`ast` and
reports, per function:

* a ``.with_for_update(...)`` call, or ``with_for_update=`` passed to
  ``Session.get`` or ``refresh``;
* SQL text that locks rows (``FOR UPDATE``, ``FOR NO KEY UPDATE``,
  ``FOR SHARE``, ``FOR KEY SHARE``);
* an advisory lock function (``pg_advisory_*``, ``pg_try_advisory_*``) or a
  key hashed in SQL (``hashtext``), as SQL text or as ``func.<name>``;
* a lock wait set by hand (``SET LOCAL lock_timeout``, ``set_config`` on it);
* a declared escape from the I/O rule (``io_outside_locks.allow``).

The sites that predate the module are listed in :data:`ALLOWED`, each with the
rank it belongs to (or what it is), so the list says where each one is going.
The list may only shrink: a site that is gone, or a function that now has
fewer, fails until its entry is removed or lowered.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: The server-side code that talks to the app's database.
SCANNED: Final[tuple[Path, ...]] = (
    REPO_ROOT / "packages/api-core/alkera_core",
    REPO_ROOT / "apps/backend/backend",
    REPO_ROOT / "apps/backend/alembic/env.py",
    REPO_ROOT / "apps/worker/worker",
    REPO_ROOT / "apps/model-gateway/model_gateway",
)

#: The owner: the one module allowed to spell any of it.
OWNER: Final[Path] = REPO_ROOT / "packages/api-core/alkera_core/db/locking.py"

#: Today's sites, ``(module, function): (spellings, the rank or role it has)``.
#: Only shrinks.
ALLOWED: Final[dict[tuple[str, str], tuple[int, str]]] = {
    ("packages/api-core/alkera_core/files/content.py", "ContentService.put_first_version"): (
        1,
        "I/O escape: a new file's first bytes go up in the transaction that makes its "
        "row, so the row never commits without them",
    ),
    ("apps/backend/backend/migration_runner.py", "_acquire_migration_lock"): (
        1,
        "migration runner: a session lock on its own direct connection for the whole upgrade",
    ),
    ("apps/backend/backend/migration_runner.py", "_migrate"): (
        1,
        "migration runner: releases the session lock it took",
    ),
    ("apps/backend/backend/api/rate_limit.py", "DurableWindow._purge"): (
        1,
        "lock wait: the durable rate window gives up fast rather than queue a request",
    ),
    ("apps/backend/backend/api/rate_limit.py", "DurableWindow.hit"): (
        1,
        "lock wait: the durable rate window gives up fast rather than queue a request",
    ),
    ("apps/backend/backend/api/routes/compute/notices.py", "shutdown_notice"): (1, "allocation"),
    ("apps/backend/backend/migration_safety.py", "bound_lock_wait"): (
        1,
        "migrations: the bounded wait a revision runs its DDL under",
    ),
    ("apps/backend/backend/migration_safety.py", "outside_transaction"): (
        1,
        "migrations: the bounded wait a revision runs its DDL under",
    ),
    ("apps/backend/backend/services/billing/enterprise_orgs.py", "update_plan"): (
        1,
        "billing: enterprise plan row",
    ),
    ("apps/backend/backend/services/billing/org_purchases.py", "_locked_plan"): (
        1,
        "billing: enterprise plan row",
    ),
    ("apps/backend/backend/services/billing/subscribe.py", "_seat_subscription"): (
        1,
        "billing: subscription row",
    ),
    ("apps/backend/backend/services/billing/usage_reset.py", "_refund_member_limits"): (
        1,
        "billing: pool member limits",
    ),
    ("apps/backend/backend/services/compute/offerings.py", "_get"): (
        1,
        "compute: offering row, before any allocation",
    ),
    ("apps/backend/backend/services/compute/org_compute_settings.py", "update_settings"): (
        1,
        "compute: org settings row, before any org machine",
    ),
    ("apps/backend/backend/services/compute/service.py", "refresh_allocation"): (1, "allocation"),
    ("apps/backend/backend/services/compute/service.py", "terminate_allocation"): (1, "allocation"),
    ("apps/backend/backend/services/connection_oauth/__init__.py", "_token_row_for_update"): (
        1,
        "connections: token row",
    ),
    ("apps/backend/backend/services/crdt/notebook_peers.py", "NotebookPeers._peer_of"): (
        1,
        "crdt peer row",
    ),
    ("apps/backend/backend/services/crdt/sweeper.py", "expire_dormant"): (
        1,
        "queue claim: dormant crdt documents",
    ),
    ("apps/backend/backend/services/crdt/sweeper.py", "park"): (1, "crdt document row"),
    ("apps/backend/backend/services/crdt/sweeper.py", "unsaved"): (
        1,
        "queue claim: unsaved crdt documents",
    ),
    ("apps/backend/backend/services/gate/github_install.py", "_lock_registry_row"): (
        1,
        "gate: installation row",
    ),
    ("apps/backend/backend/services/gate/github_install.py", "unclaim_installation"): (
        1,
        "gate: installation row",
    ),
    ("apps/backend/backend/services/identity/device_authorization.py", "redeem"): (
        1,
        "identity: device code row",
    ),
    ("apps/backend/backend/services/identity/mfa.py", "lock_account"): (1, "identity: user row"),
    ("apps/backend/backend/services/identity/sso_link.py", "_live"): (
        1,
        "identity: link request row",
    ),
    ("apps/backend/backend/services/kb/promotion.py", "_locked_family"): (
        1,
        "kb: item family rows, after the family's advisory lock",
    ),
    ("apps/backend/backend/services/notebooks/feed.py", "accept_events"): (
        2,
        "notebook kernel rows, after the notebook's advisory lock",
    ),
    ("apps/backend/backend/services/notebooks/runs.py", "end_unanswered"): (1, "notebook run row"),
    ("apps/backend/backend/services/notebooks/service.py", "NotebookService._end_unsent"): (
        1,
        "notebook run row",
    ),
    ("apps/worker/worker/tasks/notebooks.py", "_end_where"): (
        1,
        "notebook run rows, a sweep's claim that skips held ones",
    ),
    ("apps/backend/backend/services/slack/drain.py", "_claim"): (1, "queue claim: slack events"),
    ("apps/backend/backend/services/slack/relay.py", "_claim"): (
        1,
        "queue claim: slack thread chats",
    ),
    ("apps/backend/backend/services/slack/relay.py", "_rotate"): (
        1,
        "queue claim: slack thread chats",
    ),
    ("apps/backend/backend/services/slack/relay.py", "_settle_card"): (1, "slack thread ask row"),
    ("apps/backend/backend/services/workspaces/workspace_service.py", "_heal_in_own_session"): (
        1,
        "workspace_object (skip locked)",
    ),
    ("apps/backend/backend/services/workspaces/workspace_service.py", "_held_for_filing"): (
        1,
        "lock wait: filing gives up fast on a held workspace",
    ),
    ("apps/backend/backend/services/workspaces/workspace_service.py", "record_sandbox_report"): (
        1,
        "workspace_object (share)",
    ),
    (
        "apps/backend/backend/services/workspaces/workspace_service.py",
        "tombstone_adopted_for_chat",
    ): (1, "workspace_object"),
    ("apps/worker/worker/tasks/_inbox.py", "drain_inbox"): (1, "queue claim: webhook inbox row"),
    ("apps/worker/worker/billing/tasks/billing_emails.py", "dispatch_pending_billing_emails"): (
        1,
        "queue claim: billing emails",
    ),
    ("apps/worker/worker/tasks/connections.py", "_read_connection"): (
        1,
        "connections: connection row",
    ),
    ("apps/worker/worker/tasks/connections.py", "_read_record"): (
        1,
        "connections: verification row",
    ),
    ("apps/worker/worker/tasks/connections.py", "recover_verifications"): (
        1,
        "queue claim: verifications",
    ),
    ("apps/worker/worker/tasks/connections.py", "refresh_oauth_tokens"): (
        2,
        "queue claim: oauth tokens",
    ),
    ("apps/worker/worker/billing/tasks/enterprise_enrollment.py", "_enroll_one"): (
        1,
        "billing: subscription row",
    ),
    ("apps/worker/worker/gate/tasks/github_checks.py", "_binding_still_holds"): (
        1,
        "gate: installation row",
    ),
    ("apps/worker/worker/gate/tasks/github_checks.py", "_upsert_check_row"): (
        1,
        "gate: check run row",
    ),
    ("apps/worker/worker/gate/tasks/github_events.py", "_sync_installation_repos"): (
        1,
        "gate: installation row",
    ),
    ("apps/worker/worker/billing/tasks/stripe_events.py", "_find_subscription"): (
        1,
        "billing: subscription row",
    ),
    ("apps/worker/worker/billing/tasks/stripe_events.py", "_reconcile_one"): (
        1,
        "billing: subscription row",
    ),
    ("apps/worker/worker/billing/tasks/stripe_events.py", "_seat_subscription"): (
        1,
        "billing: subscription row",
    ),
    ("packages/api-core/alkera_core/account/erasure.py", "_tombstone"): (1, "account: user row"),
    ("packages/api-core/alkera_core/account/erasure.py", "erase"): (1, "account: user row"),
    ("packages/api-core/alkera_core/account_export/lifecycle.py", "_claim"): (
        1,
        "queue claim: account exports",
    ),
    ("packages/api-core/alkera_core/account/lifecycle.py", "_record_block"): (
        1,
        "account: deletion request row",
    ),
    ("packages/api-core/alkera_core/account_export/lifecycle.py", "expire_exports"): (
        1,
        "queue claim: account exports",
    ),
    ("packages/api-core/alkera_core/account/lifecycle.py", "_ledger_erasure"): (
        1,
        "I/O escape: the erasure ledger entry is written before the erasure commits",
    ),
    ("packages/api-core/alkera_core/account/lifecycle.py", "process_deletion"): (
        1,
        "account: deletion request row",
    ),
    ("packages/api-core/alkera_core/account/lifecycle.py", "reerase_one"): (
        1,
        "account: deletion request row",
    ),
    ("packages/api-core/alkera_core/account_export/lifecycle.py", "run_export"): (
        2,
        "account: export request row",
    ),
    ("packages/api-core/alkera_core/auth/last_used.py", "stamp_last_used"): (
        1,
        "queue claim: credential last-used stamp (skip locked)",
    ),
    ("packages/api-core/alkera_core/billing/cycles.py", "_locked_subscription"): (
        1,
        "billing: subscription row",
    ),
    ("packages/api-core/alkera_core/billing/engine.py", "settle"): (
        1,
        "billing: credit balance row",
    ),
    ("packages/api-core/alkera_core/billing/enterprise.py", "enroll_org"): (
        1,
        "billing: enterprise plan row",
    ),
    ("packages/api-core/alkera_core/billing/enterprise.py", "unenroll_org"): (
        1,
        "billing: enterprise plan row",
    ),
    ("packages/api-core/alkera_core/billing/expiry.py", "_expire_account_class"): (
        1,
        "billing: credit balance row",
    ),
    ("packages/api-core/alkera_core/billing/expiry.py", "reclaim_refunded_lapsed_credit"): (
        1,
        "billing: credit balance row",
    ),
    ("packages/api-core/alkera_core/billing/recurring.py", "_issue_one"): (
        1,
        "billing: recurring grant row",
    ),
    ("packages/api-core/alkera_core/billing/gateway_settlement.py", "_settle_reclaimed"): (
        1,
        "billing: proxy request row, so the sweep that settles an abandoned row's record "
        "waits for this settle",
    ),
    ("packages/api-core/alkera_core/billing/sweeper.py", "_settle_owed_records"): (
        1,
        "billing: proxy request row, re-read under the lock the reclaimed settle takes",
    ),
    ("packages/api-core/alkera_core/billing/windows.py", "reset_pool_windows"): (
        1,
        "billing: pool member limits",
    ),
    ("packages/api-core/alkera_core/billing/compute_meter.py", "_bill_remaining"): (
        1,
        "allocation",
    ),
    ("packages/api-core/alkera_core/billing/compute_meter.py", "_locked_member_limits"): (
        1,
        "billing: pool member limits",
    ),
    ("packages/api-core/alkera_core/billing/compute_meter.py", "_overdraw_storage"): (
        1,
        "billing: storage hold row",
    ),
    ("packages/api-core/alkera_core/billing/compute_meter.py", "open_drain_hold"): (
        1,
        "compute: drain hold row",
    ),
    ("packages/api-core/alkera_core/idempotency.py", "replay_or_claim"): (
        1,
        "files: idempotency key row",
    ),
    ("packages/api-core/alkera_core/files/lease_live.py", "LiveEntriesService.clear"): (
        1,
        "files_lease (live entries)",
    ),
    ("packages/api-core/alkera_core/files/leases.py", "LeaseService._overlap"): (1, "files_lease"),
    ("packages/api-core/alkera_core/files/repo.py", "FilesRepo.claim_stats_deltas"): (
        1,
        "queue claim: stats deltas",
    ),
    ("packages/api-core/alkera_core/files/sweepers.py", "<module>"): (
        1,
        "files_lease (expired lease sweep)",
    ),
    ("packages/api-core/alkera_core/objects/chat_spares.py", "lock_spare"): (
        1,
        "workspace_object (spare chat, skip locked)",
    ),
    ("packages/api-core/alkera_core/objects/workspace_end.py", "<module>"): (2, "workspace_object"),
}

_ROW_LOCK_SQL: Final = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b")
_ADVISORY_SQL: Final = re.compile(r"\bpg_(?:try_)?advisory_\w+|\bhashtext(?:extended)?\s*\(")
_WAIT_SQL: Final = re.compile(
    r"\bSET\s+(?:LOCAL\s+)?lock_timeout\b|set_config\(\s*'lock_timeout'", re.IGNORECASE
)
_ADVISORY_FUNC: Final = re.compile(r"^pg_(?:try_)?advisory_\w+$|^hashtext(?:extended)?$")


def _files() -> Iterator[Path]:
    for root in SCANNED:
        if root.is_file():
            yield root
        elif root.exists():
            yield from sorted(root.rglob("*.py"))


def _docstrings(tree: ast.AST) -> set[int]:
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


class _Scanner(ast.NodeVisitor):
    def __init__(self, docstrings: set[int]) -> None:
        self._docstrings = docstrings
        self._scope: list[str] = []
        self.found: Counter[str] = Counter()

    def _where(self) -> str:
        return ".".join(self._scope) or "<module>"

    def _enter(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            # ``session.get(Model, id, with_for_update=True)`` and ``refresh``.
            if keyword.arg == "with_for_update" and not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value in (False, None)
            ):
                self.found[self._where()] += 1
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "with_for_update":
                self.found[self._where()] += 1
            elif func.attr == "allow" and _name_of(func.value) == "io_outside_locks":
                self.found[self._where()] += 1
            elif _ADVISORY_FUNC.match(func.attr) and _name_of(func.value) == "func":
                self.found[self._where()] += 1
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self._docstrings:
            text = node.value
            hits = (
                len(_ROW_LOCK_SQL.findall(text))
                + len(_ADVISORY_SQL.findall(text))
                + len(_WAIT_SQL.findall(text))
            )
            if hits:
                self.found[self._where()] += hits


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def lock_sites(source: str) -> Counter[str]:
    """``{function: how many lock spellings it holds}`` for one module."""
    tree = ast.parse(source)
    scanner = _Scanner(_docstrings(tree))
    scanner.visit(tree)
    return scanner.found


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else path.name


def scan() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in _files():
        if path == OWNER:
            continue
        for where, count in lock_sites(path.read_text(encoding="utf-8")).items():
            found[(_rel(path), where)] = count
    return found


def test_no_lock_is_taken_outside_the_locking_module() -> None:
    """A new lock goes through :mod:`alkera_core.db.locking`; it is never
    added here."""
    found = scan()
    new = [
        f"{module} {function}: {count} (allowed {ALLOWED.get((module, function), (0, ''))[0]})"
        for (module, function), count in sorted(found.items())
        if count > ALLOWED.get((module, function), (0, ""))[0]
    ]
    assert not new, (
        "Locks taken outside alkera_core.db.locking (use lock_rows, lock_text, "
        "advisory_key and advisory_xact_lock):\n" + "\n".join(new)
    )


def test_the_allowlist_only_shrinks() -> None:
    """An entry whose site is gone, or that allows more than the function
    still holds, is stale: lower it or remove it."""
    found = scan()
    stale = [
        f"{module} {function}: allowed {allowed}, found {found.get((module, function), 0)}"
        for (module, function), (allowed, _) in sorted(ALLOWED.items())
        if found.get((module, function), 0) < allowed
    ]
    assert not stale, "Stale allowlist entries:\n" + "\n".join(stale)


def test_every_allowed_site_says_where_it_is_going() -> None:
    assert all(reason.strip() for _, reason in ALLOWED.values())


DECOYS: Final[dict[str, str]] = {
    "orm": "async def f(db, stmt):\n    return await db.execute(stmt.with_for_update())\n",
    "get": "async def f(db, M):\n    return await db.get(M, 1, with_for_update=True)\n",
    "refresh": "async def f(db, row):\n    await db.refresh(row, with_for_update=True)\n",
    "sql": 'Q = "SELECT id FROM t WHERE id = :id FOR NO KEY UPDATE"\n',
    "sql_share": "def f():\n    return f\"SELECT 1 FROM t {'x'} FOR SHARE OF t\"\n",
    "advisory_sql": 'Q = "SELECT pg_try_advisory_xact_lock(1)"\n',
    "advisory_func": "def f(func):\n    return func.pg_advisory_xact_lock(1)\n",
    "hashtext": 'Q = "SELECT hashtext(:k)"\n',
    "hashtext_func": "def f(func, k):\n    return func.hashtextextended(k, 0)\n",
    "wait": 'Q = "SET LOCAL lock_timeout = 100"\n',
    "wait_config": "Q = \"SELECT set_config('lock_timeout', '1s', true)\"\n",
    "escape": (
        "def f(io_outside_locks):\n    with io_outside_locks.allow(reason='x'):\n        pass\n"
    ),
}


@pytest.mark.parametrize("source", [pytest.param(src, id=name) for name, src in DECOYS.items()])
def test_the_scan_catches_a_planted_lock(tmp_path: Path, source: str) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(source, encoding="utf-8")
    assert sum(lock_sites(planted.read_text(encoding="utf-8")).values()) == 1


def test_prose_about_locks_is_not_a_lock() -> None:
    source = (
        '"""Takes a row FOR UPDATE and a pg_advisory_xact_lock."""\n'
        "# SELECT ... FOR UPDATE\n"
        "def f(db, row):\n"
        '    """Never FOR SHARE, never hashtext(x)."""\n'
        "    return db.get(row, 1, with_for_update=False)\n"
    )
    assert lock_sites(source) == Counter()


def test_sites_are_named_by_their_enclosing_function() -> None:
    source = (
        "class Repo:\n"
        "    async def lock(self, stmt):\n"
        "        return stmt.with_for_update()\n"
        'Q = "SELECT 1 FOR UPDATE"\n'
    )
    assert lock_sites(source) == Counter({"Repo.lock": 1, "<module>": 1})
