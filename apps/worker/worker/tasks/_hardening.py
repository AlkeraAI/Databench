"""Cross-cutting hardening for the background jobs: failure classification and the
overlap lock.

Every job is idempotent — grants key off a ledger ``ref``, emails off a
``billing_email_log`` row, window resets / reservation sweeps off conditional
``UPDATE``s, and each Stripe event off its ``ProcessedStripeEvent`` row — so a
Temporal retry re-runs it *safely* on top of that idempotency. This module adds the
two remaining guarantees:

- **transient-error classification** (:func:`is_transient_error`): a dropped DB
  connection or a Stripe network, rate-limit or 5xx blip is worth retrying (the
  activity interceptor re-raises it and Temporal applies the activity's retry
  policy), while a PERMANENT error (a programming bug, a Stripe
  ``InvalidRequestError``) is reported to Sentry and never retried, so no retry
  budget is burned on a poison call. A distribution whose jobs call an API of
  their own registers that client's transient error in
  :data:`RETRYABLE_ERRORS` (the product registers the GitHub App's);
- an **overlap lock** (:func:`run_locked`): two schedule ticks — or a nudge that lands
  while a slow sweep is still running — can't run the same sweep concurrently. It's a
  Postgres advisory lock held on a dedicated connection for the run: Postgres is
  always present, the lock auto-releases if the holding connection dies, and it
  never blocks (the claim is only tried).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TypeVar

import httpx
import stripe
from alkera_core.db import session as db_session
from alkera_core.db.locking import advisory_claim, advisory_key
from alkera_core.extensions import ExtensionPoint
from alkera_core.logging import get_logger
from sqlalchemy.exc import InterfaceError, OperationalError

log = get_logger(__name__)

T = TypeVar("T")

# Retry ONLY transient failures; anything else propagates (→ Sentry) rather than
# burning retries on a deterministic error. DB connection drops, Stripe's own
# network / rate-limit errors and network-level httpx failures are the transient
# classes; Stripe ``InvalidRequestError`` / ``AuthenticationError`` are permanent
# and intentionally absent.
_RETRYABLE: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    stripe.APIConnectionError,
    stripe.RateLimitError,
    # Stripe's 5xx class (Auth/InvalidRequest do NOT inherit from it): a short
    # Stripe incident must retry on the next pass, never burn the dead-letter
    # cap on money events.
    stripe.APIError,
    httpx.TransportError,
)

#: Transient error classes a distribution adds for the APIs its own jobs call:
#: an API client's 5xx / rate-limit error, registered from the worker's
#: composition root. Register only an error a retry can clear; a client's
#: 4xx error stays permanent by not being registered.
RETRYABLE_ERRORS: ExtensionPoint[type[Exception]] = ExtensionPoint("worker.retryable_errors")


def is_transient_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a transient (retryable) failure — a DB connection drop, a
    Stripe network / rate-limit error or a class in :data:`RETRYABLE_ERRORS` —
    versus a deterministic/poison one. Used by the
    activity interceptor to decide whether Temporal may retry an attempt, and by the
    Stripe-event drain (which swallows exceptions to stay alive) to decide whether a
    failure should burn the dead-letter attempts cap or just retry on the next pass."""
    return isinstance(exc, (*_RETRYABLE, *RETRYABLE_ERRORS.items()))


@asynccontextmanager
async def advisory_lock(name: str) -> AsyncIterator[bool]:
    """Hold the claim on job ``name`` across the block.

    Yields True if acquired, False if another run holds it (the caller should skip).

    The claim is held on a connection of its own for the whole block
    (:func:`alkera_core.db.locking.advisory_claim`), inside one transaction that
    connection keeps open, so it is released when the block ends, when the
    connection drops, or when the process dies, and never rides a pooled
    connection to its next borrower (which a session-level lock does behind a
    transaction pooler). The job body opens its own sessions on other pooled
    connections, unaffected by this one.
    """
    # Read the engine off the module (not an import-time name) so a rebind — a test
    # swapping in its own engine — is honoured.
    async with advisory_claim(db_session.engine, advisory_key("worker-job", name)) as got:
        yield got


class JobOverranError(TimeoutError):
    """A job ran past its budget and was stopped, its claim released."""


async def run_locked(
    name: str, body: Callable[[], Awaitable[T]], *, budget: timedelta | None = None
) -> T | None:
    """Run ``body`` under the :func:`advisory_lock` for ``name``; return its result,
    or None (and a ``task.skipped_locked`` log) if another run already holds the lock.

    ``budget`` bounds the run. A run that would outlast it is cancelled where it
    stands, logged as ``task.overran``, and its claim released, then
    :class:`JobOverranError` is raised: a run that hangs (a database waiting on
    its disk, a provider that stops answering) must not keep every later run of
    the same job stepping aside behind its claim for as long as the activity's
    own timeout, which the worker does not always learn of.
    """
    async with advisory_lock(name) as got:
        if not got:
            log.info("task.skipped_locked", task=name)
            return None
        if budget is None:
            return await body()
        try:
            async with asyncio.timeout(budget.total_seconds()):
                return await body()
        except TimeoutError as exc:
            log.warning("task.overran", task=name, budget_seconds=budget.total_seconds())
            raise JobOverranError(f"{name} ran past its {budget} budget") from exc
