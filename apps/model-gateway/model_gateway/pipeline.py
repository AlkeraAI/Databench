"""The request pipeline: resolve → admit → stream(tap) → settle.

Sessions are short-lived (never the request-scoped one) so no DB connection is
pinned across a long stream. The pipeline decides nothing about money: it asks
the registered meter (``model_gateway.meter``) to admit a request before the
provider call, keeps the admission alive while the relay runs, and settles it
exactly once via a guard — on normal completion, on upstream error, and in the
disconnect/cancellation path (shielded so the settlement isn't lost).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from alkera_core.auth import SessionClaims
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import get_entitlements
from alkera_core.gateway import split_model_variant
from alkera_core.llm_provider import Provider
from alkera_core.logging import get_logger
from alkera_core.model_catalog import ModelFlag
from alkera_core.models.model_catalog import Model, ModelRoute
from alkera_core.observability.sentry import capture_exception
from alkera_core.overflow import CONTEXT_LENGTH_EXCEEDED_CODE, is_context_overflow
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse

from model_gateway.adapters import (
    LINE_OVERFLOW_CODE,
    LINE_OVERFLOW_MESSAGE,
    Codec,
    ReasoningCaps,
    Transport,
    UpstreamError,
    UsageParser,
)
from model_gateway.meter import (
    MeterLease,
    MeterRefusal,
    MeterRequest,
    Settlement,
    gateway_meter,
)
from model_gateway.resolution import resolve_routes

log = get_logger(__name__)

#: Seam for the retry sleeps below — tests patch THIS, never asyncio.sleep
#: itself (that would be process-wide and swallow unrelated awaits).
_sleep = asyncio.sleep
_SSE_MEDIA_TYPE = "text/event-stream"

#: Leads the terminal event sent when a provider hangs up mid-stream. Named so a
#: caller can recognize the reason without matching on prose.
_UPSTREAM_DROPPED_MESSAGE = "upstream stream ended early"

#: Written to the client while the upstream is silent. An SSE COMMENT: the spec
#: has every consumer drop a line beginning with ":" without dispatching an
#: event, so this keeps bytes moving across the edge without entering the
#: transcript, and needs no support from the provider's own wire format.
_KEEPALIVE_COMMENT = b": keepalive\n\n"


class _StreamGate:
    """In-process backpressure: caps concurrent in-flight provider streams, both
    process-wide and per calling principal.

    The gateway is single-process-async, so plain counters are atomic between
    awaits (no lock needed). The limit is read live from settings so it can be
    tuned (and tested) without a restart. A slot is held for the full duration of
    a stream and released exactly once — either when an early path returns before
    streaming, or when the gated generator is exhausted/closed (or, for a stream
    the client has stopped reading, when its watchdog fires).
    """

    def __init__(self) -> None:
        self._active = 0
        self._by_principal: dict[str, int] = {}

    @property
    def active(self) -> int:
        return self._active

    def active_for(self, principal: str) -> int:
        return self._by_principal.get(principal, 0)

    def per_principal_limit(self) -> int:
        # A slot is pinned for the whole life of a stream, so without a
        # per-principal bound one account can open enough streams to shed every
        # other tenant with a 429 — and because a wedged stream issues no new
        # requests, the request-rate autoscaler sees an idle fleet while it
        # happens. The shipped quarter leaves room for an agent's parallel
        # subagents while keeping three quarters available to everyone else.
        return max(
            1,
            settings.gateway_max_concurrent_streams // settings.gateway_per_principal_stream_share,
        )

    def try_acquire(self, principal: str, *, shared: bool = False) -> bool:
        """Take a slot for ``principal``. ``shared`` marks a principal that fronts
        many end users (a self-hosted gateway's machine account relays its whole
        deployment through one identity), which is bounded by the process-wide cap
        alone — a per-user share there would throttle a whole customer to one
        user's worth of concurrency."""
        if self._active >= settings.gateway_max_concurrent_streams:
            return False
        if not shared and self.active_for(principal) >= self.per_principal_limit():
            return False
        self._active += 1
        self._by_principal[principal] = self.active_for(principal) + 1
        return True

    def release(self, principal: str) -> None:
        if self._active > 0:
            self._active -= 1
        held = self._by_principal.get(principal, 0)
        if held <= 1:
            self._by_principal.pop(principal, None)
        else:
            self._by_principal[principal] = held - 1


_GATE = _StreamGate()


class _Settlements:
    """Every settlement in flight, so the process can wait for them before it exits.

    A settlement is shielded from the request's cancellation: a client that hangs
    up as its last bytes arrive cancels the response, and the settle carries on
    in its own task after the HTTP exchange has ended. Nothing else holds that
    task. Left untracked, a process told to stop finishes its open connections,
    returns from the server and has the event loop cancel whatever is still
    pending, which is a settle half written: the hold waits for the sweeper and
    the usage the provider already charged for is never billed. The lifespan
    drains this set on the way out.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, settlement: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(settlement)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self, grace_seconds: float) -> int:
        """Wait up to ``grace_seconds`` for every settlement in flight; return
        how many were still running when it ran out (each one is logged)."""
        loop = asyncio.get_running_loop()
        pending = {t for t in self._tasks if not t.done() and t.get_loop() is loop}
        if not pending:
            return 0
        _, unfinished = await asyncio.wait(pending, timeout=grace_seconds)
        if unfinished:
            log.error(
                "gateway.settle.unfinished_at_shutdown",
                count=len(unfinished),
                grace_seconds=grace_seconds,
            )
        return len(unfinished)


_settlements = _Settlements()

#: How long a stopping gateway waits for settlements still in flight. One is a
#: few short transactions; this bounds a database that has stopped answering.
SETTLEMENT_DRAIN_SECONDS = 20.0


async def drain_settlements(grace_seconds: float = SETTLEMENT_DRAIN_SECONDS) -> int:
    """Wait for every settlement still in flight (see :class:`_Settlements`)."""
    return await _settlements.drain(grace_seconds)


async def _gated(
    gen: AsyncGenerator[bytes, None], gate: _StreamGate, principal: str
) -> AsyncIterator[bytes]:
    """Wrap the relay generator so the backpressure slot is freed when the
    stream finishes or the client disconnects (generator close).

    A stream is ALSO torn down by a watchdog once it has outlived
    ``gateway_max_stream_seconds``. The relay's own deadline can only fire while
    the generator is running, and a client that simply stops reading parks it
    inside a ``yield`` under ASGI back-pressure, where no code of ours runs again
    until the connection dies. The watchdog is what stops a deliberately silent
    reader from pinning capacity for the whole connection idle timeout.

    The watchdog CLOSES the relay before it frees the slot, and in that order.
    Freeing the slot on its own would only correct the bookkeeping: the parked
    relay still holds an open provider stream, so a replacement request admitted
    into the freed slot runs against capacity this stream never gave back, and
    the counters stop bounding the thing they exist to bound.
    """
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            released = True
            gate.release(principal)

    loop = asyncio.get_running_loop()

    async def expire() -> None:
        """Close the upstream, then free the slot.

        The close is retried for `gateway_stream_expiry_close_seconds` because a
        relay mid-step cannot be closed from outside; it parks again within a
        chunk. The relay's own in-band deadline fires a settle margin earlier, so
        anything still running here is wedged inside an await, and the upstream's
        own timeouts are the remaining backstop.
        """
        deadline = loop.time() + settings.gateway_stream_expiry_close_seconds
        while True:
            try:
                await gen.aclose()
                break
            except RuntimeError:
                # The relay is mid-step, so it cannot be closed from outside yet.
                # It parks again within a chunk; past the deadline, stop waiting
                # and free the slot rather than pin capacity indefinitely.
                if loop.time() >= deadline:
                    log.warning(
                        "gateway.stream.expired_while_running",
                        principal=principal,
                        seconds=settings.gateway_max_stream_seconds,
                    )
                    break
                await asyncio.sleep(settings.gateway_stream_expiry_retry_seconds)
            except Exception:
                # A close that raises has still torn the upstream down.
                break
        release_once()

    expiry: asyncio.Task[None] | None = None

    def start_expiry() -> None:
        nonlocal expiry
        expiry = loop.create_task(expire())

    watchdog = loop.call_later(max(1.0, float(settings.gateway_max_stream_seconds)), start_expiry)
    try:
        async for chunk in gen:
            yield chunk
    finally:
        watchdog.cancel()
        if expiry is not None and not expiry.done():
            expiry.cancel()
        release_once()


#: Flipped the moment the process is told to stop, so no further stream is
#: admitted while the ones already open drain. See ``begin_drain``.
_draining = False


def begin_drain() -> None:
    """Stop admitting new streams; the ones already open keep running."""
    global _draining
    _draining = True


def end_drain() -> None:
    """Admit streams again (the process was not, after all, going away)."""
    global _draining
    _draining = False


def draining() -> bool:
    return _draining


def _error_json(
    status_code: int, message: str, *, retry_after_seconds: int | None = None
) -> JSONResponse:
    """``retry_after_seconds`` emits a ``Retry-After`` header so a caller that
    honors provider pacing (a proxy-mode gateway retries THIS gateway's 429s
    through the same Retry-After floor as any provider's) gets a real hint
    instead of falling back to blind backoff."""
    headers = {"retry-after": str(retry_after_seconds)} if retry_after_seconds is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": "gateway_error", "message": message}},
        headers=headers,
    )


def _refusal_response(refusal: MeterRefusal) -> JSONResponse:
    """A meter's refusal: its own body when it has one, else the gateway envelope."""
    if refusal.body is not None:
        return JSONResponse(status_code=refusal.status_code, content=dict(refusal.body))
    return _error_json(refusal.status_code, refusal.message)


def _no_credentials_message(model_id: str) -> str:
    """Actionable 403 body when every candidate route's provider lacks usable
    credentials under BYOK — names the fix, and the entitlement state when THAT
    is the real problem (never a 402: clients must not show top-up/billing UI)."""
    state = get_entitlements().state()
    if state in ("expired", "invalid"):
        return (
            f"no provider credentials usable for model '{model_id}': the deployment's "
            f"entitlement is {state}; renew it to use these credentials"
        )
    return (
        f"no provider credentials configured for model '{model_id}' — an org admin can "
        "add keys under Organization → Deployment → Model providers, or the operator "
        "can set instance provider keys"
    )


async def proxy_stream(
    *,
    claims: SessionClaims,
    body: dict[str, Any],
    transport_factory: Callable[[ModelRoute], Transport],
    codec: Codec,
    compatible_providers: Sequence[Provider],
    request_id: str | None = None,
    thinking_display: str | None = None,
    usage_meter: str | None = None,
    provider_available: Callable[[Provider], bool] | None = None,
    shared_principal: bool = False,
) -> Response:
    """Route, admit through the meter, then relay. ``shared_principal`` marks a
    caller that fronts many end users (see :meth:`_StreamGate.try_acquire`); the
    ingress decides it from the credential."""
    model_field = body.get("model")
    if not isinstance(model_field, str) or not model_field:
        return _error_json(400, "missing 'model'")
    # Reasoning knobs ride in the model field as "<model_id>::<effort>::<display>".
    # Resolve routing/pricing/metering on the BASE model_id; effort + display are
    # per-request knobs the transport injects into the upstream body. `display`
    # also arrives via the THINKING_DISPLAY_HEADER (opencode's wire — it can't put
    # a 3rd model-field component); the header wins when both are present.
    model_id, effort, model_display = split_model_variant(model_field)
    display = thinking_display or model_display

    # --- draining (before any DB work, and before a slot is taken) ---
    # The process has been told to stop. Admitting a stream now would open a
    # provider request the graceful-shutdown window is going to cut mid-body, and
    # a torn transport is not a shape the caller can tell apart from a failed
    # step — it ends the whole chat turn. A 503 with Retry-After is, so the
    # caller reconnects to a task that is not going away and the turn costs one
    # step instead. Streams already open are untouched; they drain.
    if _draining:
        log.info("gateway.draining.refused", model_id=model_id)
        return _error_json(
            503,
            "gateway is draining, retry",
            retry_after_seconds=settings.gateway_shed_retry_after_seconds,
        )

    # --- backpressure (before any DB work, so a shed costs nothing) ---
    # A self-hosted gateway relays every one of its users through a single machine
    # principal, so it is bounded by the process-wide cap only (see try_acquire).
    principal = claims.user_id.hex
    if not _GATE.try_acquire(principal, shared=shared_principal):
        log.warning(
            "gateway.backpressure.shed",
            active=_GATE.active,
            active_for_principal=_GATE.active_for(principal),
            limit=settings.gateway_max_concurrent_streams,
            per_principal_limit=_GATE.per_principal_limit(),
            model_id=model_id,
        )
        # Slots free continuously as streams end, so the shipped one second means
        # what it says — and it gives a proxy-mode caller a real pacing hint
        # instead of blind backoff.
        return _error_json(
            429,
            "gateway at capacity, retry shortly",
            retry_after_seconds=settings.gateway_shed_retry_after_seconds,
        )

    slot_held = True
    try:
        request_id = request_id or uuid.uuid4().hex

        # --- resolve (short session) ---
        async with AsyncSessionLocal() as db:
            routes = await resolve_routes(db, model_id, compatible_providers)
            if not routes:
                return _error_json(404, f"no route for model '{model_id}'")
            if provider_available is not None:
                routes = [r for r in routes if provider_available(r.provider)]
                if not routes:
                    return _error_json(403, _no_credentials_message(model_id))
            # Resolve the model's reasoning capability from the catalog (NOT a
            # model-id string match). Threaded to the transport so it knows how to
            # engage reasoning when an effort is chosen (thinking_mode). A
            # routable-but-uncataloged model → no caps (thinking_mode=None).
            model = await db.get(Model, model_id)
            if effort is not None:
                allowed = list(model.reasoning_efforts) if model is not None else []
                if effort not in allowed:
                    return _error_json(400, f"unsupported effort '{effort}' for model '{model_id}'")
            # The catalog's own output ceiling for THIS model, read while the
            # session is open. It sizes the output half of the hold when the
            # request names no ceiling of its own; 0 means "not recorded".
            catalog_max_output = model.max_output_tokens if model is not None else 0
            # The provider's minimum cacheable prefix for THIS model, read from
            # the catalog rather than inferred from the model id. It decides
            # whether a caller's `cache_control` breakpoint took effect at all,
            # which the settle-time estimate needs when the stream reported no
            # usage. None (uncataloged model, or one whose figure nobody has
            # recorded) means the estimate does not split the prompt.
            catalog_cache_min = model.cache_min_tokens if model is not None else None
            caps = ReasoningCaps(
                thinking_mode=model.thinking_mode if model is not None else None,
                haiku_thinking=(
                    model is not None and ModelFlag.ANTHROPIC_HAIKU_STYLE_THINKING in model.flags
                ),
            )

        # --- admission (the meter commits its hold before the provider call) ---
        admitted = await gateway_meter().admit(
            MeterRequest(
                claims=claims,
                request_id=request_id,
                model_id=model_id,
                routes=routes,
                input_tokens=codec.estimate_input_tokens(body),
                max_output_tokens=codec.max_output_tokens(body, default=catalog_max_output),
                forwarded_meter=usage_meter,
            )
        )
        if isinstance(admitted, MeterRefusal):
            return _refusal_response(admitted)

        generator = _relay_and_settle(
            transport_factory=transport_factory,
            codec=codec,
            routes=routes,
            body=body,
            model_field=model_field,
            lease=admitted,
            effort=effort,
            caps=caps,
            cache_min_tokens=catalog_cache_min,
            display=display,
            request_id=request_id,
        )
        # Hand the backpressure slot to the gated generator — it releases when the
        # stream completes or the client disconnects.
        slot_held = False
        return StreamingResponse(_gated(generator, _GATE, principal), media_type=_SSE_MEDIA_TYPE)
    finally:
        if slot_held:
            _GATE.release(principal)


async def _open_route(
    *,
    transport: Transport,
    route: ModelRoute,
    body: dict[str, Any],
    codec: Codec,
    model_field: str,
    meter: str | None = None,
    effort: str | None = None,
    caps: ReasoningCaps | None = None,
    display: str | None = None,
    request_id: str | None = None,
) -> tuple[str, Any, AsyncIterator[bytes] | None, bytes | None]:
    """Open one route's upstream stream with bounded retries (only before the
    first byte). Returns ``(outcome, cm, chunks, error_sse)``:

    - ``("opened", cm, chunks, None)``       — relay from ``chunks``, then aexit ``cm``
    - ``("failover", None, None, sse)``      — retryable / connection failure
      exhausted on this route; the caller should try the next candidate
    - ``("terminal", None, None, sse)``      — non-retryable (e.g. 400); stop, do
      not fail over (the request itself is bad, another provider won't help)
    """
    # Proxy mode is a THIN pass-through: forward the client's ORIGINAL model string
    # (``model_field`` = ``<slug>::<effort>::<display>``) untouched, so Alkera re-splits
    # + does its own slug->provider routing exactly as for a normal client — no
    # re-encoding seam to drift. A direct provider call instead needs the provider's own
    # model id (route.upstream_model_id).
    forward_model_id = model_field if settings.gateway_proxy_mode else route.upstream_model_id
    max_attempts = settings.gateway_upstream_max_attempts
    for attempt in range(1, max_attempts + 1):
        cm = transport.stream(
            upstream_model_id=forward_model_id,
            region=route.region,
            body=body,
            effort=effort,
            caps=caps,
            display=display,
            meter=meter,
            request_id=request_id,
        )
        try:
            chunks = await cm.__aenter__()
            return "opened", cm, chunks, None
        except UpstreamError as exc:
            hint = exc.retry_after or 0.0
            if (
                exc.retryable
                and attempt < max_attempts
                and hint <= settings.gateway_retry_after_cap_seconds
            ):
                log.warning(
                    "gateway.upstream.retry",
                    provider=route.provider,
                    model=route.upstream_model_id,
                    region=route.region,
                    status=exc.status_code,
                    attempt=attempt,
                    retry_after=exc.retry_after,
                )
                # The provider's Retry-After FLOORS our backoff: every task in
                # the fleet throttles against the same account limit, so
                # retrying sooner than the provider asked just feeds the storm.
                # A hint ABOVE the cap skips the branch — waiting that long
                # while holding a gate slot is worse than trying another route.
                await _sleep(max(_backoff(attempt), hint))
                continue
            outcome = "failover" if exc.retryable else "terminal"
            log.warning(
                "gateway.upstream.error",
                provider=route.provider,
                model=route.upstream_model_id,
                region=route.region,
                status=exc.status_code,
                outcome=outcome,
                attempts=attempt,
                retry_after=exc.retry_after,
            )
            # Surface the provider's real error message (not just a status code) so
            # the CLI/daemon/user sees something actionable. Classify context-window
            # overflows ONCE here (the provider-agnostic chokepoint) and stamp a
            # structured `error.code` so opencode auto-compacts + the harness can
            # recover — instead of every downstream re-matching free text.
            overflow = is_context_overflow(exc.status_code, exc.detail, code=exc.error_code)
            err_code = CONTEXT_LENGTH_EXCEEDED_CODE if overflow else None
            return (
                outcome,
                None,
                None,
                codec.error_sse(f"upstream error {exc.status_code}: {exc.detail}", code=err_code),
            )
        except httpx.HTTPError as exc:
            if attempt < max_attempts:
                log.warning(
                    "gateway.upstream.connection_retry",
                    provider=route.provider,
                    model=route.upstream_model_id,
                    region=route.region,
                    attempt=attempt,
                    error=str(exc),
                )
                await _sleep(_backoff(attempt))
                continue
            log.warning(
                "gateway.upstream.connection_failed",
                provider=route.provider,
                model=route.upstream_model_id,
                region=route.region,
                attempts=attempt,
                error=str(exc),
            )
            return "failover", None, None, codec.error_sse(f"upstream connection failed: {exc}")
    return "failover", None, None, codec.error_sse("upstream unavailable")


@dataclass(frozen=True)
class _RouteOpen:
    """What the candidate loop settled on, once it is no longer running inline.

    The loop moved onto its own task so the client can be kept warm while it
    runs, which means its result has to come back as a value instead of as
    local variables the generator body assigns.

    ``outcome`` is ``opened`` (``cm`` / ``chunks`` / ``route`` are set),
    ``terminal`` (a provider said no in a way no other candidate can fix) or
    ``unavailable`` (every candidate failed over). ``settle_route`` is the route
    whose price the settlement uses: the serving route when one opened, else the
    one whose refusal is being reported, so a refused request still settles on a
    real route.
    """

    outcome: str
    cm: Any
    chunks: AsyncIterator[bytes] | None
    route: ModelRoute | None
    settle_route: ModelRoute
    error_sse: bytes | None


class _Progress:
    """What the relay knows about the request so far: the route a settlement
    would price, and the usage the codec has parsed. Read by the liveness ticker
    while the relay runs, and by the one settlement when it ends."""

    def __init__(self, parser: UsageParser, route: ModelRoute) -> None:
        self.parser = parser
        self.route = route

    def settlement(self, *, failed: bool) -> Settlement:
        return Settlement(
            route=self.route,
            usage=self.parser.usage(),
            estimated=self.parser.usage_is_estimated(),
            failed=failed,
        )


async def _relay_and_settle(
    *,
    transport_factory: Callable[[ModelRoute], Transport],
    codec: Codec,
    routes: Sequence[ModelRoute],
    body: dict[str, Any],
    model_field: str,
    lease: MeterLease,
    effort: str | None,
    caps: ReasoningCaps,
    cache_min_tokens: int | None,
    display: str | None,
    request_id: str,
) -> AsyncGenerator[bytes, None]:
    """Keep the reservation's liveness warm for the WHOLE request, then relay.

    The hold exists from admission, and the sweeper decides a request is dead
    from the age of that liveness stamp — not from whether a route has opened.
    Opening one is itself unbounded: a provider buffering a long prelude, a cold
    Bedrock invoke, bounded retries each honouring a Retry-After, and every
    failover candidate in turn, all bounded only by the per-step read timeout.
    A stamp that waited for the first upstream byte therefore let the sweeper
    reclaim a perfectly healthy request mid-open, after which the answer streams
    in full and settles nothing at all. So the ticker starts here, above every
    exit the relay can take, and is cancelled exactly once when the relay ends.
    """
    progress = _Progress(codec.usage_parser(body, cache_min_tokens=cache_min_tokens), routes[0])
    toucher = asyncio.create_task(lease.keep_alive(lambda: progress.settlement(failed=False)))
    relay = _relay_stream(
        transport_factory=transport_factory,
        codec=codec,
        routes=routes,
        body=body,
        model_field=model_field,
        lease=lease,
        progress=progress,
        effort=effort,
        caps=caps,
        display=display,
        request_id=request_id,
    )
    try:
        async for chunk in relay:
            yield chunk
    finally:
        # The ticker is stopped first because of the disconnect case: there the
        # relay has NOT settled yet, and `aclose()` below is what runs its own
        # shielded settle, so cancelling here keeps a stamp off a row settle is
        # about to claim. On an ordinary finish the relay already settled inside
        # its own `finally` before this line ran; a stamp that raced it is a
        # no-op either way, since the touch UPDATE only matches PENDING or
        # STREAMING. `aclose()` itself must not be left to the garbage collector.
        toucher.cancel()
        await relay.aclose()


async def _relay_stream(
    *,
    transport_factory: Callable[[ModelRoute], Transport],
    codec: Codec,
    routes: Sequence[ModelRoute],
    body: dict[str, Any],
    model_field: str,
    lease: MeterLease,
    progress: _Progress,
    effort: str | None,
    caps: ReasoningCaps,
    display: str | None,
    request_id: str,
) -> AsyncGenerator[bytes, None]:
    parser = progress.parser
    done = False
    # `progress.route` is the route we settle on. It starts as the primary and is
    # reassigned to the serving route once one opens, so provider_cost / provider /
    # region on the UsageRecord reflect the route that was actually billed by the
    # provider.
    # The whole request's clock, started BEFORE the first route is opened. The
    # reservation sweeper measures its TTL from the ProxyRequest's created_at,
    # which is stamped at admission, so anything spent opening a route — bounded
    # retries, each able to honour a provider Retry-After hint — has to come out
    # of the same budget. Anchoring the relay deadline after the open instead lets
    # slow opens push finalize past the sweeper's cutoff, where the row is already
    # ABANDONED and the request settles nothing at all.
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def finalize(*, failed: bool) -> None:
        nonlocal done
        if done:
            return
        done = True
        # A departed client cancels the response through Starlette's anyio task
        # group, and anyio re-delivers that cancellation at every await inside
        # the cancelled scope. `asyncio.shield` alone only keeps the settle alive
        # as an orphan: the await raises at once, the handler returns, and the
        # commit lands after the request is over (or never, if the process stops
        # first), leaving the row in flight and the hold held. The anyio shield
        # keeps the settle inside the request; `asyncio.shield` still covers a
        # bare `Task.cancel()`, which an anyio scope does not intercept. The
        # settle is also tracked, so a shutdown that lands mid-settle waits for
        # it instead of tearing the loop down under it.
        with anyio.CancelScope(shield=True):
            await asyncio.shield(
                _settlements.start(lease.settle(progress.settlement(failed=failed)))
            )

    # Try each candidate in priority order; fail over on retryable / connection
    # errors before any byte streams. Once streaming starts (or on a
    # non-retryable error) the outcome is terminal — no failover.
    async def open_candidates() -> _RouteOpen:
        last_error_sse: bytes | None = None
        for route in routes:
            outcome, cm, chunks, error_sse = await _open_route(
                transport=transport_factory(route),
                route=route,
                body=body,
                codec=codec,
                model_field=model_field,
                meter=lease.usage_meter,
                effort=effort,
                caps=caps,
                display=display,
                request_id=request_id,
            )
            if outcome == "opened":
                return _RouteOpen("opened", cm, chunks, route, route, None)
            if outcome == "terminal":
                return _RouteOpen("terminal", None, None, None, route, error_sse)
            last_error_sse = error_sse  # "failover" — try the next candidate
        return _RouteOpen("unavailable", None, None, None, routes[0], last_error_sse)

    # A step may now be silent for hours, and an ALB / proxy / CDN between the
    # client and here closes a connection it has seen no bytes on for its own
    # idle window — cutting a step both the gateway and the provider consider
    # healthy. An SSE comment while the upstream is quiet keeps those bytes
    # flowing; no conformant consumer dispatches it, so the transcript is
    # unchanged. 0 (or less) turns it off, which production refuses.
    keepalive = settings.gateway_client_keepalive_seconds
    # The comment has to flow from the moment the client's response starts, not
    # from the first upstream byte: OPENING a route is itself unbounded on the
    # client's clock. A provider that buffers a long reasoning prelude, or a cold
    # Bedrock invoke, can withhold response headers for the whole per-step
    # silence budget, and the gateway retries that across attempts — while the
    # edge, which has seen no byte of this response, counts down a far shorter
    # idle window and cuts the connection under both of us. So run the candidate
    # loop as a task and keep the line warm for as long as it is pending, across
    # every attempt and every failover, until the route opens or gives up.
    opening = asyncio.ensure_future(open_candidates())
    try:
        while True:
            open_done, _ = await asyncio.wait(
                {opening}, timeout=keepalive if keepalive > 0 else None
            )
            if open_done:
                break
            yield _KEEPALIVE_COMMENT
    except BaseException:
        # The reader went away (or the task running us was cancelled) while an
        # open was in flight. Unwind the half-opened upstream (leaving the task to
        # run would hold a provider connection nothing will ever read), then
        # settle: admission already reserved a hold, and returning without a
        # settle leaves the row PENDING and the credit held until the sweeper's
        # TTL. No upstream opened, so nothing was metered and the hold is released.
        opening.cancel()
        with contextlib.suppress(BaseException):
            await opening
        await finalize(failed=True)
        raise
    opened = opening.result()

    if opened.outcome == "terminal":
        progress.route = opened.settle_route
        yield opened.error_sse or codec.error_sse("upstream error")
        await finalize(failed=True)
        return
    if opened.outcome == "unavailable":
        log.error("gateway.upstream.all_unavailable", request_id=request_id)
        yield opened.error_sse or codec.error_sse("all upstreams unavailable")
        await finalize(failed=True)
        return

    serving_cm: Any = opened.cm
    serving_chunks: AsyncIterator[bytes] | None = opened.chunks
    serving_route: ModelRoute | None = opened.route
    progress.route = opened.settle_route
    assert serving_cm is not None and serving_chunks is not None and serving_route is not None
    log.info(
        "gateway.upstream.opened",
        request_id=request_id,
        provider=serving_route.provider,
        model=serving_route.upstream_model_id,
        region=serving_route.region,
    )
    # From here the prompt is on the provider's bill — input is charged on
    # acceptance, not on completion — so a stream that dies before the wire ever
    # reports usage settles against the request-side estimate rather than free.
    parser.upstream_opened()
    try:
        # Records that estimate on the row at once, so a gateway that dies
        # before its next liveness stamp still owes the prompt.
        await lease.mark_streaming(progress.settlement(failed=False))
    except Exception as exc:
        # The stream is what the caller is paying for; the ticker records the
        # progress again on its next stamp, and the settle claims the row either way.
        log.warning("gateway.stream.mark_failed", request_id=request_id, error=str(exc))
    # The stream must NOT outlive the reservation sweeper's TTL: the sweeper
    # reclaims in-flight holds older than gateway_max_stream_seconds, after which
    # this request's finalize would find its row ABANDONED and settle NOTHING —
    # the remaining usage would be served free. Enforce the deadline from the
    # request's own start, with a margin so finalize lands before the sweeper's
    # cutoff (measured from created_at, which precedes the stream opening).
    deadline = started + max(
        1.0, settings.gateway_max_stream_seconds - settings.gateway_stream_settle_margin_seconds
    )
    pending: asyncio.Task[bytes] | None = None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            if pending is None:
                pending = asyncio.ensure_future(anext(serving_chunks))
            wait_for = min(remaining, keepalive) if keepalive > 0 else remaining
            # asyncio.wait, not wait_for: a lapsed wait must leave the read
            # RUNNING, or every keepalive would cancel the chunk in flight.
            done_now, _ = await asyncio.wait({pending}, timeout=wait_for)
            if not done_now:
                yield _KEEPALIVE_COMMENT
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            parser.feed(chunk)
            yield chunk
            if parser.overflowed():
                # The upstream sent a line larger than the parser will buffer, so
                # it dropped it and stopped. Reading on would relay content
                # nothing can be billed from — and on a BYOK route the upstream
                # is a tenant's own server, which makes "read on" a way to spend
                # this shared process on one org. End the step in band and let
                # `finally` settle what was metered before the overflow.
                log.warning(
                    "gateway.stream.line_buffer_overflow",
                    request_id=request_id,
                    provider=str(serving_route.provider),
                    model=serving_route.upstream_model_id,
                    limit_bytes=settings.gateway_max_request_body_bytes,
                )
                yield codec.error_sse(LINE_OVERFLOW_MESSAGE, code=LINE_OVERFLOW_CODE)
                break
    except TimeoutError:
        log.error(
            "gateway.stream.max_duration_exceeded",
            request_id=request_id,
            provider=serving_route.provider,
            max_seconds=settings.gateway_max_stream_seconds,
        )
        yield codec.error_sse("stream exceeded the maximum allowed duration")
    except httpx.HTTPError as exc:
        # A provider hanging up mid-stream is an operational event, not a gateway
        # fault. The 200 and its headers are already on the wire, so re-raising
        # can only abort the response body — the client gets a truncated stream
        # with no reason, and the ASGI server reports a crash we then page on.
        # Tell the client in-band, same as the deadline branch above; `finally`
        # still bills what actually streamed.
        log.warning(
            "gateway.stream.upstream_error",
            request_id=request_id,
            provider=serving_route.provider,
            error=str(exc),
        )
        yield codec.error_sse(f"{_UPSTREAM_DROPPED_MESSAGE}: {exc}")
    except Exception as exc:
        # Truly unexpected (parser/codec bug, etc.) — capture to Sentry.
        log.error(
            "gateway.stream.unexpected_error",
            request_id=request_id,
            provider=serving_route.provider,
            exc_info=exc,
        )
        capture_exception(exc, provider=str(serving_route.provider), request_id=request_id)
        raise
    finally:
        # Covers normal completion AND client disconnect / cancellation: bill
        # whatever was actually produced (we're charged for it). finalize() MUST
        # run even if closing the upstream stream raises — otherwise the hold
        # would linger until the sweeper reclaims it instead of releasing now.
        # A read left in flight by a keepalive wait (client disconnect, deadline)
        # has to go before the upstream response is closed under it.
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await serving_cm.__aexit__(None, None, None)
        finally:
            # OpenAI/Anthropic can report an upstream error IN-BAND on an already-200
            # stream (`response.failed` / `error`) instead of as an HTTP status — e.g.
            # a deprecated model, moderation refusal, overload, or quota. Settle FAILED
            # in that case, otherwise a failed turn is booked as a 0-usage SETTLED
            # success and fires a false `gateway.settle.no_usage_parsed` drift alarm.
            # A bare mid-stream disconnect (no failure event) stays failed=False, so we
            # still bill what actually streamed.
            upstream_failed = parser.stream_failed()
            if upstream_failed:
                log.warning(
                    "gateway.stream.upstream_in_band_failure",
                    request_id=request_id,
                    provider=str(serving_route.provider),
                    model=serving_route.upstream_model_id,
                )
            elif sides := parser.estimated_sides():
                # The stream was cut before the provider reported usage (client
                # disconnect / max-duration), so this settles against a server-side
                # estimate instead of releasing to zero. Loud because the figure is
                # billed and only approximate.
                #
                # An estimate of the prompt ALONE is its own event: the wire
                # reported nothing whatsoever about a request the provider has
                # already charged in full, so a rise in it is both a money signal
                # and the signature of someone cutting streams before the first
                # frame. A truncated tail carries the request-side input figure
                # too, but it is ordinary traffic on a wire that reports usage
                # only at the end — counting it here would bury the sharper line
                # under the routine one. Neither is `no_usage_parsed`, which is
                # what an empty response makes.
                event = (
                    "gateway.settle.estimated_prompt"
                    if sides == ("input",)
                    else "gateway.settle.estimated_usage"
                )
                log.warning(
                    event,
                    request_id=request_id,
                    provider=str(serving_route.provider),
                    model=serving_route.upstream_model_id,
                    sides=list(sides),
                )
            await finalize(failed=upstream_failed)


def _backoff(attempt: int) -> float:
    base = min(
        settings.gateway_retry_backoff_base_seconds * (2.0 ** (attempt - 1)),
        settings.gateway_retry_backoff_max_seconds,
    )
    return base + random.uniform(0, settings.gateway_retry_backoff_jitter_seconds)  # noqa: S311
