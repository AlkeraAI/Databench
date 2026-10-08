"""CLI-side cost metering for warehouse actions.

The gateway meters LLM spend but has no ``chat_id``, so warehouse-query cost is
metered here, at the same resolver chokepoint as permissions. Each gated SQL
action is **estimated** (a connector ``CostModel``: BigQuery dry-run, Snowflake
credits, DuckDB $0, a wall-clock fallback), **checked** against the per-query,
chat, day and week caps, and **settled** to the actual cost after execution.

Three windows of accumulated spend feed the check:

- **per-query**: the single estimate (no state).
- **chat**: summed from the per-chat ledger ``.alkera/chats/<id>/cost_ledger.jsonl``.
- **day / week**: a cross-chat rollup in ``.alkera/cost_state.json`` whose
  buckets reset on key change. A chat on a machine that serves several people
  meters against its owner's own rollup (:meth:`CostLedger.for_principal`), so
  one org's spend is never another's.

A single query over the per-query cap is rejected (it cannot be raised
mid-flight). A chat, day or week overage prompts to raise the cap, or rejects
when the cap is org-managed. An unknown or non-positive estimate never blocks;
only a positive estimate that provably exceeds a cap gates.

**Scope.** The dollar caps bind only connections that implement
``EstimateSQLCostCapability``. A connector without one estimates $0 and passes
every dollar cap by design (local DuckDB is free, and an unmeterable warehouse
degrades to "no estimate" rather than blocking every query). BigQuery has an
exact dry-run estimate and Snowflake a heuristic one. The defaults
(``DEFAULT_PER_QUERY_USD`` $1, chat $25, day $100, week $400) are overridable
per connection in ``permissions.yml`` ``cost:``.

**Concurrency.** :func:`gate_cost` checks and reserves under the ledger lock
before execution, :func:`settle_cost` reconciles the reservation to the actual
cost afterwards, and :func:`release_cost` refunds a failed run. The reservation
closes the check-then-act window so two concurrent queries cannot both pass a
window cap, and the lock serializes day/week accounting across daemons.
"""

from __future__ import annotations

import math
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alkera_core.atomic_io import append_line, write_json_atomic
from alkera_core.money import usd_display
from alkera_core.project.jsonl import iter_jsonl
from alkera_core.project.locking import FileLock, retrying_lock
from alkera_core.schemas.chat import PermissionOption, PermissionRequest
from alkera_core.versioning import VersionedModel
from pydantic import BaseModel, ValidationError

from alkera_cli.contracts.tool_types import ActionDescriptor, CostActual, CostEstimate

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

# ``CostActual`` is defined alongside ``CostEstimate`` in ``_types`` (both are
# transient result models) and re-exported here so the cost surface is one
# import root. It appears in ``__all__`` below.

# Built-in caps. Overridable in ``permissions.yml`` ``cost:``.
DEFAULT_PER_QUERY_USD = 1.0
DEFAULT_CHAT_USD = 25.0
DEFAULT_DAY_USD = 100.0
DEFAULT_WEEK_USD = 400.0


# ---------------------------------------------------------------------------
# The cap decision
# ---------------------------------------------------------------------------


class CostDecision(StrEnum):
    """The outcome of a cost check."""

    ALLOW = "allow"
    REJECT = "reject"
    """A hard cap that can't be raised mid-flight (per-query, or an org-managed
    window cap)."""
    PROMPT_RAISE = "prompt_raise"
    """Over a chat/day/week cap the user owns — offer to raise it."""


@dataclass(frozen=True, slots=True)
class CostCheck:
    """The result of :func:`check_cost` — the decision plus the evidence (which
    window, the cap, the projected spend) for a clear prompt / rejection."""

    decision: CostDecision
    window: str | None = None
    """"per_query" | "chat" | "day" | "week" — which cap drove the decision."""
    cap_usd: float = 0.0
    projected_usd: float = 0.0
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is CostDecision.ALLOW


_CAP_FIELDS: tuple[tuple[str, str, float], ...] = (
    # (config key, model field, default)
    ("per_query_usd_cap", "per_query_usd", DEFAULT_PER_QUERY_USD),
    ("chat_usd_cap", "chat_usd", DEFAULT_CHAT_USD),
    ("day_usd_cap", "day_usd", DEFAULT_DAY_USD),
    ("week_usd_cap", "week_usd", DEFAULT_WEEK_USD),
)

#: The cap fields allowed inside a ``cost:`` block (and each override entry).
#: ``run_queries_cap`` is consumed by the gate's per-run governor
#: (in the product's gate), not by :class:`CostLimits`.
_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {k for k, _f, _d in _CAP_FIELDS} | {"org_managed", "run_queries_cap"}
)
#: Every recognized top-level ``cost:`` key.
KNOWN_COST_KEYS: frozenset[str] = _OVERRIDE_KEYS | {"connection_overrides", "tool_overrides"}


def unknown_cost_keys(cost: Mapping[str, Any] | None) -> list[str]:
    """Cost-config keys that will SILENTLY DO NOTHING — a hand-edit typo like
    ``chat_usd_cpa`` (instead of ``chat_usd_cap``). Checks the top level + every
    ``connection_overrides`` / ``tool_overrides`` entry. Surfaced ON DEMAND by
    ``/cost`` (we don't warn/log on load) so a cap you thought you set but didn't
    is at least visible when you inspect your spend."""
    if not cost:
        return []
    bad: list[str] = [str(k) for k in cost if k not in KNOWN_COST_KEYS]
    for group in ("connection_overrides", "tool_overrides"):
        overrides = cost.get(group)
        if isinstance(overrides, Mapping):
            for handle, ov in overrides.items():
                if isinstance(ov, Mapping):
                    bad.extend(f"{group}.{handle}.{k}" for k in ov if k not in _OVERRIDE_KEYS)
    return sorted(bad)


def _override_sources(
    cost: Mapping[str, Any] | None, *, connection: str | None, tool: str | None
) -> tuple[dict[str, Any], ...]:
    """The lookup layers of a ``cost:`` block, tightest first: the tool
    override, the connection override, then the base mapping."""
    base: dict[str, Any] = dict(cost or {})
    conn_overlay: dict[str, Any] = {}
    if connection is not None:
        ov = (base.get("connection_overrides") or {}).get(connection)
        if isinstance(ov, Mapping):
            conn_overlay = dict(ov)
    tool_overlay: dict[str, Any] = {}
    if tool is not None:
        tv = (base.get("tool_overrides") or {}).get(tool)
        if isinstance(tv, Mapping):
            tool_overlay = dict(tv)
    return (tool_overlay, conn_overlay, base)


def cost_config_value(
    cost: Mapping[str, Any] | None,
    key: str,
    *,
    connection: str | None = None,
    tool: str | None = None,
) -> Any:
    """The raw value of ``key`` under the same precedence
    :meth:`CostLimits.from_config` uses (tool override → connection override →
    base), or ``None`` when unset everywhere. For ``cost:`` keys that other
    layers consume (the gate's ``run_queries_cap``) without widening
    :class:`CostLimits` itself."""
    for src in _override_sources(cost, connection=connection, tool=tool):
        if key in src:
            return src[key]
    return None


class CostLimits(BaseModel):
    """The effective spend caps for one action — defaults overlaid by the
    ``permissions.yml`` ``cost:`` block (and a per-connection override)."""

    per_query_usd: float = DEFAULT_PER_QUERY_USD
    chat_usd: float = DEFAULT_CHAT_USD
    day_usd: float = DEFAULT_DAY_USD
    week_usd: float = DEFAULT_WEEK_USD
    org_managed: bool = False
    """When the caps are org-managed, a window overage is REJECTED rather than
    offered for raise — a user-set cap is the user's to lift."""

    @classmethod
    def from_config(
        cls,
        cost: Mapping[str, Any] | None,
        *,
        connection: str | None = None,
        tool: str | None = None,
    ) -> CostLimits:
        """Build limits from a ``permissions.yml`` ``cost:`` block. Recognized
        keys: ``per_query_usd_cap`` / ``chat_usd_cap`` / ``day_usd_cap`` /
        ``week_usd_cap`` / ``org_managed``, a ``connection_overrides: {<handle>:
        {…}}`` map, and a ``tool_overrides: {<tool>: {…}}`` map. Precedence
        (tightest first): tool override → connection override → base."""
        sources = _override_sources(cost, connection=connection, tool=tool)

        def _pick(config_key: str, default: float) -> float:
            for src in sources:
                if config_key in src:
                    try:
                        value = float(src[config_key])
                    except (TypeError, ValueError):
                        return default
                    # A NaN cap makes every `projected > cap` comparison False —
                    # the window would be SILENTLY uncapped. The /cost-set write
                    # path rejects these, but permissions.yml is hand-editable,
                    # so the ENFORCEMENT input must guard too. Negative is
                    # nonsense (and would hard-reject everything); both fall
                    # back to the default cap. `inf` stands: an explicit,
                    # well-ordered "no limit".
                    if math.isnan(value) or value < 0:
                        return default
                    return value
            return default

        org_managed = any(bool(src.get("org_managed", False)) for src in sources)
        return cls(
            **{field: _pick(key, default) for key, field, default in _CAP_FIELDS},
            org_managed=org_managed,
        )


def cost_overview(
    ledger: CostLedger, session_id: str, limits: CostLimits, *, now: datetime
) -> dict[str, Any]:
    """A JSON-friendly snapshot of the chat/day/week spend vs caps — the shared
    shape behind the CLI ``/cost`` view and the daemon ``get_cost_state`` RPC."""
    chat, day, week = ledger.windows(session_id, now=now)
    return {
        "spent": {"chat": chat, "day": day, "week": week},
        "caps": {
            "per_query": limits.per_query_usd,
            "chat": limits.chat_usd,
            "day": limits.day_usd,
            "week": limits.week_usd,
        },
        "org_managed": limits.org_managed,
    }


#: Fraction of a window cap at which a (non-blocking) budget warning fires.
BUDGET_WARN_THRESHOLD = 0.8


def budget_warnings(
    limits: CostLimits,
    *,
    chat_spent_usd: float,
    day_spent_usd: float,
    week_spent_usd: float,
    threshold: float = BUDGET_WARN_THRESHOLD,
) -> list[tuple[str, str]]:
    """Return ``(window, message)`` for each window now in ``[threshold, 1.0)`` of
    its cap — a heads-up that you're close, distinct from the over-cap gate. A
    window already OVER its cap is the gate's job (prompt/reject), not a warning."""
    out: list[tuple[str, str]] = []
    for window, spent, cap in (
        ("chat", chat_spent_usd, limits.chat_usd),
        ("day", day_spent_usd, limits.day_usd),
        ("week", week_spent_usd, limits.week_usd),
    ):
        if cap <= 0:
            continue
        frac = spent / cap
        if threshold <= frac < 1.0:
            out.append(
                (
                    window,
                    f"{window} spend is at {frac * 100:.0f}% of the "
                    f"{usd_display(cap)} cap ({usd_display(spent)} used)",
                )
            )
    return out


def check_cost(
    estimate_usd: float,
    *,
    limits: CostLimits,
    chat_spent_usd: float = 0.0,
    day_spent_usd: float = 0.0,
    week_spent_usd: float = 0.0,
) -> CostCheck:
    """Decide whether an action estimated at ``estimate_usd`` may run, given how
    much has already been spent in each window.

    Fail-safe: a non-positive (unknown / free / $0) estimate always ALLOWs. A
    single query over its own per-query cap is a hard REJECT. Each
    accumulating window (chat → day → week, checked in that order) projects
    ``spent + estimate``; the first to exceed its cap drives a PROMPT_RAISE (or
    REJECT when ``org_managed``)."""
    if estimate_usd <= 0:
        return CostCheck(CostDecision.ALLOW)

    # A refusal names the estimate and the projection to four decimals, not the
    # two the money reading uses elsewhere: it explains why a figure crossed a cap,
    # and at cents "$1.00 projected vs $1.00 allowed" would state no reason at all.
    if estimate_usd > limits.per_query_usd:
        return CostCheck(
            CostDecision.REJECT,
            window="per_query",
            cap_usd=limits.per_query_usd,
            projected_usd=estimate_usd,
            reason=(
                f"estimated ${estimate_usd:.4f} exceeds the per-query cap "
                f"of ${limits.per_query_usd:.2f}"
            ),
        )

    over = CostDecision.REJECT if limits.org_managed else CostDecision.PROMPT_RAISE
    for window, spent, cap in (
        ("chat", chat_spent_usd, limits.chat_usd),
        ("day", day_spent_usd, limits.day_usd),
        ("week", week_spent_usd, limits.week_usd),
    ):
        projected = spent + estimate_usd
        if projected > cap:
            verb = "would exceed" if not limits.org_managed else "exceeds the org-managed"
            return CostCheck(
                over,
                window=window,
                cap_usd=cap,
                projected_usd=projected,
                reason=(
                    f"this query (${estimate_usd:.4f}) {verb} the {window} cap: "
                    f"${projected:.4f} projected vs ${cap:.2f} allowed"
                ),
            )
    return CostCheck(CostDecision.ALLOW)


# ---------------------------------------------------------------------------
# Persistence — per-chat ledger + cross-chat day/week state
# ---------------------------------------------------------------------------


class CostLedgerEntry(VersionedModel):
    """One settled (or estimated-only) warehouse charge in a chat's ledger.

    Append-only at ``.alkera/chats/<id>/cost_ledger.jsonl``. ``actual_usd`` is
    ``None`` until the connector settles; ``charged_usd`` falls back to the
    estimate so an unsettled charge still counts toward the windows."""

    SCHEMA_VERSION = "1.0.0"

    entry_id: str = ""
    connection: str = ""
    operation: str = ""
    estimate_usd: float = 0.0
    actual_usd: float | None = None
    wallet_currency: str = "usd"
    at: float = 0.0
    """Unix epoch seconds when the charge was recorded."""

    @property
    def charged_usd(self) -> float:
        """The amount this entry contributes to a window — the settled actual if
        known, else the pre-execution estimate (never undercount in-flight)."""
        return self.actual_usd if self.actual_usd is not None else self.estimate_usd


def _day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class CostState(VersionedModel):
    """Cross-chat day/week spend accumulators (``.alkera/cost_state.json``).

    Each window keeps a key (the calendar day / ISO week it covers) and a
    running total; when the key turns over the bucket zeroes — the same
    self-resetting-window idea as the gateway's ``reset_pool_windows`` but
    file-local and chat-agnostic."""

    SCHEMA_VERSION = "1.0.0"

    day_key: str = ""
    day_usd: float = 0.0
    week_key: str = ""
    week_usd: float = 0.0

    def rolled(self, *, now: datetime) -> CostState:
        """A copy with any window whose key advanced reset to zero. Pure — reading
        the rolled state never persists the roll (the next ``with_charge`` write
        does).

        FORWARD-only: a writer holding a stale ``now`` (a settle whose timestamp
        predates the last roll, or clock skew between two daemons sharing the
        file) must never reset a NEWER persisted window back to its own older
        period — that write would erase every charge the new period has already
        accumulated. Both key formats sort lexically, so "older" is a plain
        string compare."""
        out = self.model_copy(deep=True)
        day, week = _day_key(now), _week_key(now)
        if out.day_key != day and day > out.day_key:
            out.day_key, out.day_usd = day, 0.0
        if out.week_key != week and week > out.week_key:
            out.week_key, out.week_usd = week, 0.0
        return out

    def with_charge(
        self, amount: float, *, now: datetime, charged_at: datetime | None = None
    ) -> CostState:
        """Roll to ``now`` then add ``amount`` to each window the charge belongs
        to, clamped at 0.

        ``charged_at`` is when the ORIGINAL charge was booked — a settle/release
        delta reconciles an earlier reservation, and if a window has rolled since
        (the reservation was made before midnight, the query finished after), that
        delta belongs to a period that is over: applying it to the FRESH window
        corrupts the new period in either direction (a refund inflates its cap, an
        upward settle eats it). Such a delta skips the rolled window entirely.
        ``None`` (a fresh charge) applies to both windows. The clamp guards the
        residual cross-period leak that key comparison can't see (clock skew)."""
        out = self.rolled(now=now)
        if charged_at is None or _day_key(charged_at) == out.day_key:
            out.day_usd = max(0.0, out.day_usd + amount)
        if charged_at is None or _week_key(charged_at) == out.week_key:
            out.week_usd = max(0.0, out.week_usd + amount)
        return out


class CostLedger:
    """The CLI-side cost store: a per-chat append-only ledger + the cross-chat
    day/week state.

    The ledger is crash-safe JSONL (the partial-tail-tolerant reader). The
    ``cost_state.json`` day/week buckets are mutated under this store's OWN
    ``FileLock`` (locks are never inherited), retrying briefly on
    contention like the scheduler: the lock is held only for a single
    read-modify-write, so a held lock is contention to wait out, not an error.
    """

    def __init__(
        self,
        project: ProjectDirectory,
        *,
        ledger_root: Path | None = None,
        lock_timeout_seconds: float = 2.0,
        scope: str | None = None,
    ) -> None:
        self._project = project
        self._ledger_root = ledger_root
        if scope is None:
            self._state_path = project.cost_state_path
            self._lock = FileLock(project.path / ".cost_state.lock")
        else:
            self._state_path = project.scoped_cost_state_path(scope)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = FileLock(self._state_path.with_name(f".{self._state_path.stem}.lock"))
        self._lock_timeout = lock_timeout_seconds
        # The per-principal ledgers handed out by ``for_principal``, kept so a
        # principal's budget warnings dedupe across calls like the root's do.
        self._principals: dict[str, CostLedger] = {}
        self._principals_lock = threading.Lock()
        # In-process serialization in FRONT of the FileLock. FileLock is itself
        # thread-safe (and guards other processes — per-worktree daemons, CLI +
        # VS Code), but contending threads from the daemon's `asyncio.to_thread`
        # pool would otherwise poll-spin its fail-fast acquire at 5ms intervals
        # and can starve one past `lock_timeout` under load. Queueing on a real
        # blocking mutex first is fair and cheap: exactly one thread touches the
        # FileLock at a time, and in-process waiters never burn the timeout.
        self._tlock = threading.Lock()
        # In-memory dedup of budget warnings so a window crossing 80% warns ONCE
        # per period, not on every query. Keyed by (window, period_key); a new
        # day/week (or chat) gets a fresh key and re-warns.
        self._warned: set[tuple[str, str]] = set()

    def for_principal(self, principal: str) -> CostLedger:
        """This ledger as the chats of one principal (a chat owner's user id)
        meter against it: their own day and week totals, beside the same
        per-chat ledgers.

        A machine that serves the chats of several people and orgs keeps one
        project, so a single day/week total would let one org's spend show in
        another org's warnings and block its queries. Spend is attributed to the
        chat's owner: a person belongs to exactly one org, so the owner key keeps
        orgs apart by construction, and it is also the grain the server budgets
        members at. A chat that names no owner meters in a bucket of its own,
        never in anyone's."""
        key = f"principal:{principal}" if principal else "unattributed"
        with self._principals_lock:
            ledger = self._principals.get(key)
            if ledger is None:
                ledger = CostLedger(
                    self._project,
                    ledger_root=self._ledger_root,
                    lock_timeout_seconds=self._lock_timeout,
                    scope=key,
                )
                self._principals[key] = ledger
            return ledger

    def new_budget_warnings(
        self, session_id: str, *, limits: CostLimits, now: datetime
    ) -> list[str]:
        """Budget warnings for windows newly in the 80%+ band (deduped per period
        in-memory). Cheap reads — call after a reserve to surface to the user."""
        chat, day, week = self.windows(session_id, now=now)
        st = self.state(now=now)
        period_key = {"chat": session_id, "day": st.day_key, "week": st.week_key}
        out: list[str] = []
        for window, message in budget_warnings(
            limits, chat_spent_usd=chat, day_spent_usd=day, week_spent_usd=week
        ):
            key = (window, period_key.get(window, ""))
            if key in self._warned:
                continue
            self._warned.add(key)
            out.append(message)
        return out

    # --- paths ---------------------------------------------------------

    def _ledger_path(self, session_id: str) -> Path:
        # The default lives beside the chat it meters; an explicit root (the
        # gate's per-run ledgers) keeps non-chat sessions out of `chats/`,
        # where every directory surfaces in the chat list UI.
        root = self._ledger_root if self._ledger_root is not None else self._project.chats_path
        return root / session_id / "cost_ledger.jsonl"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # Threads serialize here; the FileLock then guards across processes.
        with self._tlock, retrying_lock(self._lock, timeout_seconds=self._lock_timeout):
            yield

    def _read_state(self) -> CostState:
        try:
            raw = self._state_path.read_text()
        except FileNotFoundError:
            return CostState()
        try:
            return CostState.model_validate_json(raw)
        except ValidationError:
            return CostState()  # corrupt → start fresh rather than block every query

    # --- reads ---------------------------------------------------------

    def chat_spent(self, session_id: str) -> float:
        """Sum of charged amounts across a chat's ledger (settled actual where
        known, else the estimate)."""
        total = 0.0
        for raw in iter_jsonl(self._ledger_path(session_id)):
            try:
                total += CostLedgerEntry.model_validate(raw).charged_usd
            except ValidationError:
                continue  # skip an unreadable row rather than crash the gate
        return total

    def read_entries(self, session_id: str) -> list[CostLedgerEntry]:
        """Every readable ledger entry for a chat, oldest-first (a torn trailing
        line is skipped by the reader; an unreadable mid-file row is dropped).
        Read-only — no lock: the ledger is append-only crash-safe JSONL, so a
        concurrent append at most adds a row the reader hasn't reached."""
        entries: list[CostLedgerEntry] = []
        for raw in iter_jsonl(self._ledger_path(session_id)):
            try:
                entries.append(CostLedgerEntry.model_validate(raw))
            except ValidationError:
                continue
        return entries

    def state(self, *, now: datetime) -> CostState:
        """The day/week state rolled to ``now`` (buckets zeroed if the calendar
        day / ISO week turned over). Read-only — does not persist the roll."""
        return self._read_state().rolled(now=now)

    def windows(self, session_id: str, *, now: datetime) -> tuple[float, float, float]:
        """``(chat_spent, day_spent, week_spent)`` — the three accumulators
        :func:`check_cost` consumes, in one call."""
        st = self.state(now=now)
        return self.chat_spent(session_id), st.day_usd, st.week_usd

    # --- mutation ------------------------------------------------------

    def record(self, session_id: str, entry: CostLedgerEntry, *, now: datetime) -> None:
        """Append ``entry`` to the chat ledger and add its charge to the rolled
        day/week state (under the state lock)."""
        with self._locked():
            self._append(session_id, entry)
            st = self._read_state().with_charge(entry.charged_usd, now=now)
            write_json_atomic(self._state_path, st.model_dump(mode="json"))

    def _append(self, session_id: str, entry: CostLedgerEntry) -> None:
        ledger = self._ledger_path(session_id)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        append_line(ledger, entry.model_dump_json())

    def reserve(
        self,
        session_id: str,
        *,
        estimate_usd: float,
        limits: CostLimits,
        now: datetime,
        connection: str,
        operation: str,
    ) -> tuple[CostCheck, CostLedgerEntry | None]:
        """Atomically CHECK the caps AND reserve (append a pending entry + bump
        day/week state) under ONE lock — closing the check-then-act window so two
        concurrent queries in a chat can't both pass a window cap. Returns
        ``(decision, reservation)``; ``reservation`` is ``None`` when the check
        didn't allow (nothing was written)."""
        with self._locked():
            chat = self.chat_spent(session_id)
            st = self._read_state().rolled(now=now)
            check = check_cost(
                estimate_usd,
                limits=limits,
                chat_spent_usd=chat,
                day_spent_usd=st.day_usd,
                week_spent_usd=st.week_usd,
            )
            if not check.allowed:
                return check, None
            return check, self._reserve_locked(session_id, estimate_usd, now, connection, operation)

    def reserve_forced(
        self,
        session_id: str,
        *,
        estimate_usd: float,
        now: datetime,
        connection: str,
        operation: str,
    ) -> CostLedgerEntry:
        """Reserve WITHOUT a cap check — used only after a prompt-to-raise the
        human approved (the user chose to exceed the cap)."""
        with self._locked():
            return self._reserve_locked(session_id, estimate_usd, now, connection, operation)

    def _reserve_locked(
        self, session_id: str, estimate_usd: float, now: datetime, connection: str, operation: str
    ) -> CostLedgerEntry:
        entry = CostLedgerEntry(
            entry_id=secrets.token_hex(8),
            connection=connection,
            operation=operation,
            estimate_usd=estimate_usd,
            actual_usd=None,  # pending — the reservation charges the estimate
            at=now.timestamp(),
        )
        self._append(session_id, entry)
        st = self._read_state().with_charge(estimate_usd, now=now)
        write_json_atomic(self._state_path, st.model_dump(mode="json"))
        return entry

    def settle_reservation(
        self, session_id: str, reservation: CostLedgerEntry, *, actual_usd: float, now: datetime
    ) -> None:
        """Reconcile a reservation to its actual cost: append a DELTA entry (so
        the chat ledger sum nets to ``actual_usd``) + bump day/week state by the
        delta. A delta of 0 is a no-op."""
        delta = actual_usd - reservation.estimate_usd
        if delta == 0.0:
            return
        adj = CostLedgerEntry(
            entry_id=f"{reservation.entry_id}:settle",
            connection=reservation.connection,
            operation=reservation.operation,
            estimate_usd=0.0,
            actual_usd=delta,  # the reconciliation delta (may be negative)
            at=now.timestamp(),
        )
        with self._locked():
            self._append(session_id, adj)
            # The delta reconciles the ORIGINAL reservation — windows that rolled
            # since it was booked must not absorb it (with_charge skips them).
            charged_at = datetime.fromtimestamp(reservation.at, tz=UTC)
            st = self._read_state().with_charge(delta, now=now, charged_at=charged_at)
            write_json_atomic(self._state_path, st.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Two-phase metering — the estimate→gate→(execute)→settle orchestration that
# the resolver chokepoint (``SqlQueryTool.run``) drives around execution.
# ---------------------------------------------------------------------------


class CostDeniedError(Exception):
    """A query refused on cost grounds — over a hard cap, or a prompt-to-raise
    the user declined. Carries the human-readable reason for the tool error."""


_RAISE_OPTIONS = [
    PermissionOption(option_id="allow_once", name="Raise the cap, run once"),
    PermissionOption(option_id="reject_once", name="Cancel"),
]


async def _estimate(descriptor: ActionDescriptor, cost_capability: Any) -> CostEstimate:
    """Pre-execution estimate. A connection with an ``EstimateSQLCostCapability``
    estimates; without one (DuckDB/local) the cost is $0 — fail-safe."""
    if cost_capability is None:
        return CostEstimate()
    estimate: CostEstimate = await cost_capability.estimate(descriptor)
    # The number comes from PLUGIN code — the very thing the cost layer bounds.
    # A NaN slips every cap comparison AND poisons the chat ledger sum for the
    # rest of the session; a negative inflates the chat cap. Both degrade to the
    # documented "$0 / unknown never blocks" semantic instead.
    if math.isnan(estimate.usd_amount) or estimate.usd_amount < 0:
        return estimate.model_copy(update={"usd_amount": 0.0})
    return estimate


async def _ask_raise(
    broker: Any,
    descriptor: ActionDescriptor,
    check: CostCheck,
    session_id: str,
    tool_call_id: str | None,
) -> bool:
    request = PermissionRequest(
        event_id=secrets.token_hex(10),
        time=datetime.now(UTC),
        session_id=session_id,
        request_id=secrets.token_hex(10),
        tool_call_id=tool_call_id,
        permission_kind="cost",
        canonical_kind="other",
        patterns=[check.reason],
        subject=descriptor.model_dump(mode="json"),
        options=list(_RAISE_OPTIONS),
    )
    option = await broker.resolve(request)
    return str(option).startswith("allow")


@dataclass(frozen=True, slots=True)
class CostReservation:
    """A reserved (pending) charge returned by :func:`gate_cost` — the ledger
    entry that already counts toward the windows, plus its estimate. Threaded
    into :func:`settle_cost` / :func:`release_cost` to reconcile."""

    entry: CostLedgerEntry
    estimate: CostEstimate
    warnings: tuple[str, ...] = ()
    """Non-blocking budget warnings (e.g. "day spend is at 80% of the cap") to
    surface to the user — distinct from the over-cap gate."""


async def gate_cost(
    descriptor: ActionDescriptor,
    *,
    limits: CostLimits,
    ledger: CostLedger,
    session_id: str,
    connection: str = "",
    operation: str = "query",
    cost_capability: Any = None,
    broker: Any = None,
    now: datetime,
    tool_call_id: str | None = None,
) -> CostReservation:
    """Estimate the cost and, atomically, CHECK the caps AND RESERVE it BEFORE
    execution — so concurrent queries can't both slip past a window cap.

    Returns the :class:`CostReservation` (already counting toward the windows).
    Raises :class:`CostDeniedError` on a hard cap (per-query, or an org-managed
    window) or a declined / unanswerable prompt-to-raise. A $0 / unknown
    estimate always passes (and reserves $0).
    """
    estimate = await _estimate(descriptor, cost_capability)
    check, entry = ledger.reserve(
        session_id,
        estimate_usd=estimate.usd_amount,
        limits=limits,
        now=now,
        connection=connection,
        operation=operation,
    )
    if entry is not None:  # ALLOW → reserved atomically
        warnings = tuple(ledger.new_budget_warnings(session_id, limits=limits, now=now))
        return CostReservation(entry=entry, estimate=estimate, warnings=warnings)
    if check.decision is CostDecision.REJECT:
        raise CostDeniedError(check.reason)
    # PROMPT_RAISE — ask the human (fail-closed without a broker), then reserve.
    if broker is None or not await _ask_raise(broker, descriptor, check, session_id, tool_call_id):
        raise CostDeniedError(check.reason)
    # Fresh time after the prompt: the human wait is unbounded, and reserving
    # with the pre-prompt `now` after a day/week boundary would book the charge
    # into a window that already rolled (and could roll it backward).
    post_prompt = datetime.now(UTC)
    forced = ledger.reserve_forced(
        session_id,
        estimate_usd=estimate.usd_amount,
        now=post_prompt,
        connection=connection,
        operation=operation,
    )
    warnings = tuple(ledger.new_budget_warnings(session_id, limits=limits, now=post_prompt))
    return CostReservation(entry=forced, estimate=estimate, warnings=warnings)


async def reserve_or_refuse(
    descriptor: ActionDescriptor,
    *,
    limits: CostLimits,
    ledger: CostLedger,
    session_id: str,
    connection: str = "",
    operation: str = "query",
    cost_capability: Any = None,
    now: datetime,
) -> CostReservation | str:
    """The NON-prompting variant of :func:`gate_cost`, for a detached/background
    job that has no human in the loop.

    Estimates + atomically reserves exactly like ``gate_cost`` — but on anything
    that ``gate_cost`` would resolve by PROMPTING the human (a ``PROMPT_RAISE``
    over a chat/day/week cap) OR a hard ``REJECT`` (per-query / org-managed cap),
    it returns the refusal REASON string instead of asking. The caller turns that
    into a model-visible "run it in the foreground to approve the cost" error, so
    a background job never blocks on (or silently bypasses) a cost prompt. A $0 /
    unknown estimate reserves $0 and is always allowed."""
    estimate = await _estimate(descriptor, cost_capability)
    check, entry = ledger.reserve(
        session_id,
        estimate_usd=estimate.usd_amount,
        limits=limits,
        now=now,
        connection=connection,
        operation=operation,
    )
    if entry is not None:  # ALLOW → reserved atomically
        warnings = tuple(ledger.new_budget_warnings(session_id, limits=limits, now=now))
        return CostReservation(entry=entry, estimate=estimate, warnings=warnings)
    # REJECT or PROMPT_RAISE — both refuse here (no prompt for a detached job).
    return check.reason or "estimated cost exceeds a spending cap"


async def settle_cost(
    descriptor: ActionDescriptor,
    result: Any,
    reservation: CostReservation,
    *,
    ledger: CostLedger,
    session_id: str,
    cost_capability: Any = None,
    now: datetime,
) -> float:
    """Reconcile a reservation to the ACTUAL cost after a successful execution,
    and return what was charged.

    A capability with a post-hoc settle (e.g. Snowflake credits from
    QUERY_HISTORY) refines the estimate; otherwise the estimate stands. The
    delta (actual minus reserved) is netted into the chat ledger + day/week
    state. The returned amount is this ONE action's charge, which is how a
    caller attributes its own spend without re-reading a ledger it shares with
    every other run in the session."""
    actual: CostActual
    if cost_capability is not None and hasattr(cost_capability, "settle"):
        actual = await cost_capability.settle(descriptor, result, reservation.estimate)
    else:
        actual = CostActual(
            usd_amount=reservation.estimate.usd_amount,
            wallet_currency=reservation.estimate.wallet_currency,
        )
    # Plugin-supplied number — sanitize before it enters the ledger: a
    # non-finite actual poisons the chat sum (NaN never compares over a cap
    # again), so the estimate stands; a negative actual would *refund more than
    # was reserved*, driving chat_spent negative and inflating the chat cap.
    actual_usd = actual.usd_amount
    if not math.isfinite(actual_usd):
        actual_usd = reservation.estimate.usd_amount
    elif actual_usd < 0:
        actual_usd = 0.0
    ledger.settle_reservation(session_id, reservation.entry, actual_usd=actual_usd, now=now)
    return actual_usd


def release_cost(
    reservation: CostReservation, *, ledger: CostLedger, session_id: str, now: datetime
) -> None:
    """Execution FAILED — release the reservation (settle to $0). The reservation
    made the in-flight charge visible (so it never silently vanished); the
    release nets it back out, so a query that didn't complete isn't billed. (A
    failure that nonetheless scanned billable bytes is intentionally under-billed
    rather than over-billing every syntax error.)"""
    ledger.settle_reservation(session_id, reservation.entry, actual_usd=0.0, now=now)


__all__ = [
    "BUDGET_WARN_THRESHOLD",
    "DEFAULT_CHAT_USD",
    "DEFAULT_DAY_USD",
    "DEFAULT_PER_QUERY_USD",
    "DEFAULT_WEEK_USD",
    "KNOWN_COST_KEYS",
    "CostActual",
    "CostCheck",
    "CostDecision",
    "CostDeniedError",
    "CostLedger",
    "CostLedgerEntry",
    "CostLimits",
    "CostReservation",
    "CostState",
    "budget_warnings",
    "check_cost",
    "cost_config_value",
    "cost_overview",
    "gate_cost",
    "release_cost",
    "reserve_or_refuse",
    "settle_cost",
    "unknown_cost_keys",
]
