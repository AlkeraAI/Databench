"""The rate-limit registry: buckets, keys, the route table, the refusal shape,
and the two endpoints an external assessment drove without a throttle.

Every case drives the clock explicitly. A burst timed by the wall clock refills
as fast as a slow request loop drains it, so "did it throttle?" would be a
question about the machine rather than about the bucket.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.auth import encode_cli_token
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from backend.api.rate_limit import (
    CLASSES,
    REGISTRY,
    DurableBusy,
    DurableWindow,
    RateLimitClass,
    TokenBucketLimiter,
    caller_key,
    enforce_rate_limit,
    limited,
    resolve_routes,
)
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from tests._suite_app import app as real_app
from tests.conftest import ManualClock, OrgWithAdmin, login, make_member
from tests.files._boxes import registered_box

pytestmark = pytest.mark.compute_rows


def _limiter(per_minute: int, burst: int) -> TokenBucketLimiter:
    return TokenBucketLimiter(per_minute=lambda: per_minute, burst=lambda: burst)


class TestTokenBucket:
    def test_a_burst_up_to_the_capacity_is_admitted_and_the_next_is_refused(self) -> None:
        limiter = _limiter(120, 40)
        decisions = [limiter.consume("k", now=0.0) for _ in range(41)]
        assert [d.allowed for d in decisions[:40]] == [True] * 40
        refused = decisions[40]
        assert not refused.allowed
        assert refused.retry_after >= 1.0
        assert refused.limit == 120
        # The minute still has room; only the burst is spent.
        assert refused.remaining == 80

    def test_past_the_burst_the_caller_is_paced_at_the_sustained_rate(self) -> None:
        """120 a minute is one token every half second: just short buys nothing,
        crossing it buys exactly one, never a fresh burst."""
        limiter = _limiter(120, 40)
        for _ in range(40):
            limiter.consume("k", now=0.0)
        assert not limiter.consume("k", now=0.4).allowed
        assert limiter.consume("k", now=0.5).allowed
        assert not limiter.consume("k", now=0.5).allowed

    def test_the_minute_window_caps_the_sustained_total_and_resets_on_the_boundary(
        self,
    ) -> None:
        """The literal finding: the 121st request inside one minute is refused
        even though the bucket, refilling at two a second, would have had tokens
        for it — and the next minute starts clean."""
        limiter = _limiter(120, 40)
        admitted = 0
        # A steady 2.5/s from the top of a minute: the bucket never empties.
        for i in range(140):
            if limiter.consume("k", now=60.0 + i * 0.4).allowed:
                admitted += 1
        assert admitted == 120
        refused = limiter.consume("k", now=60.0 + 140 * 0.4)
        assert not refused.allowed
        assert refused.remaining == 0
        # Told to wait for the window, not for a token.
        assert refused.retry_after == pytest.approx(120.0 - (60.0 + 56.0))
        assert limiter.consume("k", now=120.0).allowed

    def test_refill_never_exceeds_the_burst(self) -> None:
        """An idle caller comes back with a full burst, not an unbounded credit."""
        limiter = _limiter(120, 3)
        limiter.consume("k", now=0.0)
        assert [limiter.consume("k", now=1e6).allowed for _ in range(3)] == [True] * 3
        assert not limiter.consume("k", now=1e6).allowed

    def test_callers_are_counted_separately(self) -> None:
        limiter = _limiter(60, 1)
        assert limiter.consume("a", now=0.0).allowed
        assert limiter.consume("b", now=0.0).allowed
        assert not limiter.consume("a", now=0.0).allowed

    def test_the_burst_never_exceeds_the_minute(self) -> None:
        """A burst above the sustained rate would let the window refuse a
        request the bucket admitted — the smaller number is the burst."""
        limiter = _limiter(10, 50)
        assert limiter.burst == 10

    def test_key_table_is_bounded_and_the_hot_key_survives_eviction(self) -> None:
        """Keys are caller-controlled, so an unbounded table is a memory leak an
        attacker drives. Eviction only forgives the least-recently-seen key — by
        construction not the one spraying."""
        limiter = TokenBucketLimiter(per_minute=lambda: 60, burst=lambda: 1, max_keys=4)
        limiter.consume("attacker", now=0.0)
        for i in range(50):
            limiter.consume(f"noise-{i}", now=0.0)
            limiter.consume("attacker", now=0.0)
        assert len(limiter._buckets) == 4
        assert not limiter.consume("attacker", now=0.0).allowed

    def test_retry_after_is_at_least_a_whole_second(self) -> None:
        """The value goes out as an integer `Retry-After`; a sub-second float
        would floor to 0 and invite an immediate retry."""
        limiter = _limiter(6000, 100)
        for _ in range(100):
            limiter.consume("k", now=0.0)
        assert limiter.consume("k", now=0.0).retry_after == pytest.approx(1.0)

    def test_the_numbers_are_read_live(self) -> None:
        """A deployment tunes a class by env var and a test pins it by
        monkeypatch; a limiter that copied the numbers at construction would
        ignore both."""
        numbers = {"per_minute": 60, "burst": 2}
        limiter = TokenBucketLimiter(
            per_minute=lambda: numbers["per_minute"], burst=lambda: numbers["burst"]
        )
        limiter.consume("k", now=0.0)
        limiter.consume("k", now=0.0)
        # At one a second, half a second buys nothing...
        assert not limiter.consume("k", now=0.5).allowed
        # ...until the rate is raised to two a second, when it buys a token.
        numbers["per_minute"] = 120
        admitted = limiter.consume("k", now=0.75)
        assert admitted.allowed
        assert admitted.limit == 120


class TestCallerKey:
    def _request(self, headers: dict[str, str], peer: str | None = "10.0.0.1") -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 1234) if peer else None,
        }
        return Request(scope)

    def test_socket_peer_is_used_when_nothing_fronts_the_app(self) -> None:
        assert caller_key(self._request({})) == "10.0.0.1"

    def test_the_proxy_attested_hop_is_used_not_the_leftmost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller can write any LEFT-hand entries it likes, so counting the
        leftmost hop would hand an attacker a fresh quota per request just by
        rotating a header. Each trusted proxy APPENDS the peer it saw, so the
        attested address is read from the RIGHT."""
        monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
        key = caller_key(self._request({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 203.0.113.9"}))
        assert key == "203.0.113.9"

    def test_a_spoofed_prefix_cannot_split_a_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
        keys = {
            caller_key(self._request({"X-Forwarded-For": f"{i}.{i}.{i}.{i}, 203.0.113.9"}))
            for i in range(1, 20)
        }
        assert keys == {"203.0.113.9"}

    def test_a_short_header_falls_back_to_a_shared_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 3)
        assert caller_key(self._request({"X-Forwarded-For": "1.1.1.1"})) == "unattributed"

    def test_garbage_is_not_used_as_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
        assert caller_key(self._request({"X-Forwarded-For": "'; DROP TABLE"})) == "unattributed"


# --------------------------------------------------------------------------
# The route table: every route of the app resolves to a registered class
# --------------------------------------------------------------------------


def _resolved() -> dict[tuple[str, str], RateLimitClass]:
    return {(r.method, r.path): r.rate_class for r in resolve_routes(real_app.routes)}


@pytest.mark.parametrize("name", sorted(n for n, c in CLASSES.items() if not c.exempt))
def test_every_class_reads_settings_that_exist_and_admit_something(name: str) -> None:
    """A leg spells its settings by convention (``rate_limit_<stem>_…``), so a
    class whose stem never got its two settings raises at the first request it
    bounds rather than at import -- and a burst above the minute, or a minute of
    zero, is a knob that does not mean what it says: the limiter silently
    clamps the first and refuses everything on the second.
    """
    for leg in CLASSES[name].legs:
        per_minute, burst = leg.per_minute(), leg.burst()
        assert per_minute >= 1, f"{name}/{leg.kind} admits nothing"
        assert 1 <= burst <= per_minute, f"{name}/{leg.kind} bursts past its own minute"
        if leg.per_hour is not None:
            assert leg.per_hour() >= per_minute, f"{name}/{leg.kind} caps the hour below the minute"


def test_following_queued_work_is_budgeted_for_the_biggest_drop() -> None:
    """A drop polls one operation per file it lands, so this budget is sized by
    the drop, not by a person clicking: the largest batch the upload class is
    itself sized for has to fit inside a minute of polling, and its burst has to
    absorb the pile that lands while the lanes are all finishing at once."""
    operation = CLASSES["operation"].legs[0]
    upload = CLASSES["upload"].legs[0]
    assert operation.per_minute() >= upload.per_minute()
    assert operation.burst() >= upload.burst()
    assert operation.per_minute() > CLASSES["read"].legs[0].per_minute(), (
        "the poll is back on the read budget it was moved off"
    )


def test_every_route_resolves_to_a_registered_class() -> None:
    table = _resolved()
    assert len(table) > 300, "the walk did not reach the app's routes"
    for (method, path), rate_class in table.items():
        assert rate_class.name in CLASSES, f"{method} {path} resolved to {rate_class.name!r}"
    # ...and what the enforcer reads at request time is the same table.
    for entry in resolve_routes(real_app.routes):
        if entry.endpoint is not None and entry.method != "*":
            assert REGISTRY.class_for(entry.endpoint, entry.method) is entry.rate_class, (
                f"{entry.method} {entry.path} is pinned to a different class than the walk"
            )


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", "/api/v1/auth/login", "credential", id="login"),
        pytest.param("POST", "/api/v1/auth/signup", "credential", id="signup"),
        pytest.param("POST", "/api/v1/auth/password-reset/{token}", "credential", id="reset"),
        pytest.param("POST", "/api/v1/auth/device/token", "device_poll", id="device-poll"),
        pytest.param("POST", "/api/v1/auth/device/code", "credential", id="device-code-mint"),
        pytest.param("GET", "/api/v1/auth/oauth/{provider}/start", "credential", id="oauth"),
        pytest.param("POST", "/api/v1/auth/sso/{org_id}/saml/acs", "credential", id="saml"),
        pytest.param("POST", "/api/v1/teams/{team_id}/invitations", "mutation", id="invite"),
        pytest.param("GET", "/api/v1/auth/me", "read", id="me"),
        pytest.param("POST", "/api/v1/chats/{chat_id}/messages", "chat", id="prompt"),
        pytest.param("POST", "/api/v1/chats/{chat_id}/answer", "chat", id="answer"),
        pytest.param("GET", "/api/v1/chats/{chat_id}/messages", "chat", id="chat-read"),
        pytest.param("POST", "/api/v1/chats", "chat", id="chat-create"),
        pytest.param("POST", "/api/v1/files/uploads", "upload", id="upload-open"),
        pytest.param(
            "PUT", "/api/v1/files/uploads/{session_id}/parts/{part_no}", "upload_part", id="part"
        ),
        pytest.param(
            "POST", "/api/v1/files/drives/{drive_id}/items/{item_id}/tree", "upload", id="tree"
        ),
        pytest.param("POST", "/api/v1/files/drives/{drive_id}/bulk", "upload", id="bulk"),
        pytest.param("GET", "/api/v1/files/drives/{drive_id}/items/{item_id}", "read", id="item"),
        # A POST that reads: its ids do not fit a query string, and it must not
        # spend the mutation budget a folder drop needs for its writes.
        pytest.param("POST", "/api/v1/files/drives/{drive_id}/items/lookup", "read", id="lookup"),
        # Following queued work scales with the work, so the poll has its own
        # budget — and only the poll: cancelling and undoing are writes a
        # person makes one at a time, on the ordinary mutation budget.
        pytest.param(
            "GET",
            "/api/v1/files/drives/{drive_id}/operations/{operation_id}",
            "operation",
            id="operation-poll",
        ),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/operations/{operation_id}/cancel",
            "mutation",
            id="operation-cancel",
        ),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/operations/{operation_id}/undo",
            "mutation",
            id="operation-undo",
        ),
        pytest.param("GET", "/api/v1/events", "stream_open", id="sse"),
        pytest.param("WS", "/api/v1/ws", "stream_open", id="socket"),
        pytest.param("POST", "/api/v1/ws/tickets", "ws_ticket", id="ticket"),
        pytest.param("POST", "/api/v1/machines/{machine_id}/heartbeat", "machine", id="beat"),
        pytest.param("POST", "/api/v1/machines/register", "machine", id="register"),
        pytest.param("GET", "/api/v1/files/drives/{drive_id}/delta", "machine", id="delta"),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/heartbeat",
            "machine",
            id="lease-heartbeat",
        ),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/live",
            "lease_node",
            id="lease-live-report",
        ),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/tree",
            "lease_tree",
            id="lease-tree-report",
        ),
        pytest.param(
            "POST",
            "/api/v1/files/drives/{drive_id}/items/{item_id}/lease/tree/digests",
            "lease_tree",
            id="lease-tree-digests",
        ),
        pytest.param("POST", "/api/v1/org/audit-events/agent", "machine", id="audit-flush"),
        pytest.param("POST", "/api/v1/webhooks/github", "webhook", id="github"),
        pytest.param("GET", "/admin/v1/orgs", "admin", id="admin-read"),
        pytest.param("POST", "/admin/v1/bans/users", "admin", id="admin-write"),
        pytest.param("GET", "/health/live", "exempt", id="health"),
        pytest.param("GET", "/c/{token}", "exempt", id="content-origin"),
    ],
)
def test_the_route_resolves_to_its_class(method: str, path: str, expected: str) -> None:
    table = _resolved()
    assert (method, path) in table, f"{method} {path} is not a route of the app"
    assert table[(method, path)].name == expected


def test_a_route_that_declares_nothing_takes_the_default_for_its_method() -> None:
    probe = FastAPI()

    @probe.get("/r")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/m")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    table = {(r.method, r.path): r.rate_class.name for r in resolve_routes(probe.routes)}
    assert table[("GET", "/r")] == "read"
    assert table[("POST", "/m")] == "mutation"


def test_declaring_an_unknown_class_is_refused_at_import() -> None:
    with pytest.raises(LookupError):
        limited("bespoke")


# --------------------------------------------------------------------------
# The enforcer: keys, the refusal shape, the flag
# --------------------------------------------------------------------------


def _probe_app() -> FastAPI:
    """A tiny app under the real enforcer, one route per class of interest."""
    probe = FastAPI(dependencies=[Depends(enforce_rate_limit)])

    @probe.post("/mutate")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/read")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/attempt", dependencies=[Depends(limited("credential"))])
    async def attempt() -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/chats/{chat_id}")
    async def chat(chat_id: str) -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/box", dependencies=[Depends(limited("machine"))])
    async def box() -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/held/{drive_id}/{item_id}", dependencies=[Depends(limited("lease_node"))])
    async def held(drive_id: str, item_id: str) -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/hook", dependencies=[Depends(limited("webhook"))])
    async def hook(request: Request) -> dict[str, int]:
        # A webhook verifies its signature over the RAW body, after the
        # enforcer has already keyed on it: the bytes must still be readable.
        return {"body_bytes": len(await request.body())}

    REGISTRY.install(probe)
    return probe


def _jwt(user_id: Any | None = None, org_id: Any | None = None) -> str:
    token, _ = encode_cli_token(
        user_id=user_id or uuid4(),
        email=f"{secrets.token_hex(4)}@alkera.dev",
        org_team_id=org_id or uuid4(),
        platform_role=None,
    )
    return token


@pytest.fixture
def probe(pin_clock: ManualClock) -> AsyncClient:
    del pin_clock
    return AsyncClient(transport=ASGITransport(app=_probe_app()), base_url="http://probe")


def _pin(monkeypatch: pytest.MonkeyPatch, name: str, *, per_minute: int, burst: int) -> None:
    monkeypatch.setattr(settings, f"rate_limit_{name}_per_minute", per_minute)
    monkeypatch.setattr(settings, f"rate_limit_{name}_burst", burst)


Headers = dict[str, str]


@pytest.fixture
def owner(org_admin: OrgWithAdmin) -> Headers:
    """The person, on a session of their own (not any box's)."""
    return {"Authorization": f"Bearer {_jwt(org_admin.admin_id, org_admin.org_id)}"}


@pytest.fixture
def new_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> Callable[[], Awaitable[Headers]]:
    """A box the person registered: its own session token plus the assertion
    naming the machine registered on it -- the one pair that verifies."""

    async def make() -> Headers:
        token, machine_id = await registered_box(
            real_session,
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_id=org_admin.org_id,
        )
        return {"Authorization": f"Bearer {token}", **agent_headers(machine_id)}

    return make


@pytest.mark.asyncio
async def test_a_refusal_carries_the_headers_and_the_body(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(monkeypatch, "mutation", per_minute=3, burst=3)
    for _ in range(3):
        assert (await probe.post("/mutate")).status_code == 200
    refused = await probe.post("/mutate")
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["ratelimit-limit"] == "3"
    assert refused.headers["ratelimit-remaining"] == "0"
    assert 1 <= int(refused.headers["ratelimit-reset"]) <= 60
    body = refused.json()["detail"]
    assert body["code"] == "rate_limited"
    assert body["retry_after"] == int(refused.headers["retry-after"])
    assert body["message"] == CLASSES["mutation"].message


@pytest.mark.asyncio
async def test_two_principals_do_not_share_a_bucket(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(monkeypatch, "mutation", per_minute=1, burst=1)
    alice = {"Authorization": f"Bearer {_jwt()}"}
    bob = {"Authorization": f"Bearer {_jwt()}"}
    assert (await probe.post("/mutate", headers=alice)).status_code == 200
    assert (await probe.post("/mutate", headers=alice)).status_code == 429
    assert (await probe.post("/mutate", headers=bob)).status_code == 200


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_keyed_by_its_attested_address(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(monkeypatch, "read", per_minute=1, burst=1)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
    one = {"X-Forwarded-For": "203.0.113.1"}
    two = {"X-Forwarded-For": "203.0.113.2"}
    assert (await probe.get("/read", headers=one)).status_code == 200
    assert (await probe.get("/read", headers=one)).status_code == 429
    assert (await probe.get("/read", headers=two)).status_code == 200


@pytest.mark.asyncio
async def test_a_forged_session_token_does_not_mint_a_fresh_key(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer we did not sign is not a principal: rotating garbage JWTs must
    not buy a budget per request. Only a real credential (a token we can't
    verify here is counted by its digest, one budget per credential)."""
    _pin(monkeypatch, "read", per_minute=1, burst=1)
    forged = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.forgedforgedforged"
    assert (await probe.get("/read", headers={"Authorization": f"Bearer {forged}"})).status_code
    second = await probe.get("/read", headers={"Authorization": f"Bearer {forged}x"})
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_the_box_is_keyed_by_its_machine_credential_not_its_owner(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    owner: Headers,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """The daemon calls with the owner's JWT plus the agent assertion. Its
    budget is the machine's: exhausting it leaves the owner's own calls — the
    same JWT without the assertion — untouched, and a second box is separate."""
    _pin(monkeypatch, "machine", per_minute=1, burst=1)
    box_a, box_b = await new_box(), await new_box()
    assert (await probe.post("/box", headers=box_a)).status_code == 200
    assert (await probe.post("/box", headers=box_a)).status_code == 429
    assert (await probe.post("/box", headers=owner)).status_code == 200
    assert (await probe.post("/box", headers=box_b)).status_code == 200


@pytest.mark.asyncio
async def test_a_box_on_its_owner_routes_never_spends_the_owner_budget(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    owner: Headers,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """The daemon reads chats, lists folders and opens the event stream with the
    owner's JWT plus the agent assertion, for every chat it runs. Thirty idle
    chats' polling once spent the owner's own reads and the owner's browser lost
    its live updates. An asserted call on a principal-keyed class is counted on
    the agent class instead: the owner's bucket is untouched either way round."""
    _pin(monkeypatch, "read", per_minute=1, burst=1)
    _pin(monkeypatch, "machine", per_minute=3, burst=3)
    box = await new_box()
    # The box spends its own budget first: the owner's single read is still there.
    for _ in range(3):
        assert (await probe.get("/read", headers=box)).status_code == 200
    assert (await probe.get("/read", headers=box)).status_code == 429
    assert (await probe.get("/read", headers=owner)).status_code == 200
    # And the owner spending theirs leaves a fresh box session its own.
    assert (await probe.get("/read", headers=owner)).status_code == 429
    other = await new_box()
    assert (await probe.get("/read", headers=other)).status_code == 200


@pytest.mark.asyncio
async def test_a_box_budget_is_per_resource_it_works_on(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """A box runs hundreds of chats; the chat it polls hardest must not silence
    the others, so the resource the route names is part of the key."""
    _pin(monkeypatch, "machine", per_minute=1, burst=1)
    box = await new_box()
    chat_a, chat_b = uuid4(), uuid4()
    assert (await probe.get(f"/chats/{chat_a}", headers=box)).status_code == 200
    assert (await probe.get(f"/chats/{chat_a}", headers=box)).status_code == 429
    assert (await probe.get(f"/chats/{chat_b}", headers=box)).status_code == 200


@pytest.mark.asyncio
async def test_naming_a_new_resource_every_time_still_spends_the_persons_machine_budget(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """The resource in the key is a path segment the caller writes, so it alone
    would never bound a caller that rotates it; the per-person leg does, across
    every box the person runs -- and a box that names a new resource every time
    is still one of them."""
    _pin(monkeypatch, "machine", per_minute=100, burst=100)
    _pin(monkeypatch, "agent_principal", per_minute=3, burst=3)
    box_a, box_b = await new_box(), await new_box()
    assert (await probe.get(f"/chats/{uuid4()}", headers=box_a)).status_code == 200
    assert (await probe.get(f"/chats/{uuid4()}", headers=box_b)).status_code == 200
    assert (await probe.get(f"/chats/{uuid4()}", headers=box_a)).status_code == 200
    assert (await probe.get(f"/chats/{uuid4()}", headers=box_b)).status_code == 429
    assert (await probe.get(f"/chats/{uuid4()}", headers=box_a)).status_code == 429


@pytest.mark.asyncio
async def test_the_per_person_leg_bounds_every_machine_on_the_machine_class_too(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """Each verified box has a machine budget of its own; how many of them one
    person runs does not multiply what that person's machines may send."""
    _pin(monkeypatch, "machine", per_minute=100, burst=100)
    _pin(monkeypatch, "agent_principal", per_minute=2, burst=2)
    boxes = [await new_box() for _ in range(3)]
    assert [(await probe.post("/box", headers=b)).status_code for b in boxes] == [200, 200, 429]


@pytest.mark.asyncio
async def test_a_malformed_assertion_is_counted_as_the_principal(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half an assertion buys no budget of its own: the call is the person's
    (the route refuses the pair later, in its own vocabulary)."""
    _pin(monkeypatch, "read", per_minute=1, burst=1)
    owner = {"Authorization": f"Bearer {_jwt()}"}
    halved = dict(agent_headers("sess-box-a"))
    halved.pop(next(k for k in halved if k.lower().endswith("-id")), None)
    assert (await probe.get("/read", headers=owner)).status_code == 200
    assert (await probe.get("/read", headers={**owner, **halved})).status_code == 429


@pytest.mark.asyncio
async def test_the_strict_classes_are_never_rekeyed_by_an_assertion(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box has no business minting or signing in: an assertion on a strict
    class changes nothing about who is counted."""
    _pin(monkeypatch, "credential", per_minute=1, burst=1)
    _pin(monkeypatch, "credential_ip", per_minute=100, burst=100)
    monkeypatch.setattr(settings, "rate_limit_credential_per_hour", 100)
    monkeypatch.setattr(settings, "rate_limit_credential_ip_per_hour", 100)
    owner = {"Authorization": f"Bearer {_jwt()}"}
    box = {**owner, **agent_headers("sess-box-a")}
    assert (await probe.post("/attempt", headers=owner)).status_code == 200
    assert (await probe.post("/attempt", headers=box)).status_code == 429


@pytest.mark.asyncio
async def test_a_live_plane_is_keyed_by_the_folder_a_box_holds_not_by_the_box(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """A box holds one lease per chat and reports on each of them continuously.
    On the machine's own budget the chat writing hardest would spend it and
    silence the others, so the folder is part of the key — while the holder's
    *instance* deliberately is not: a mirror that restarted is still the same
    folder's traffic and must not buy itself a second budget for it."""
    _pin(monkeypatch, "lease_node", per_minute=1, burst=1)
    box = await new_box()
    drive, kickoff, retro = uuid4(), uuid4(), uuid4()
    first = f"/held/{drive}/{kickoff}"
    second = f"/held/{drive}/{retro}"

    mirror_one = {**box, "X-Alkera-Lease-Instance": "mirror-1", "X-Alkera-Lease-Epoch": "3"}
    mirror_two = {**box, "X-Alkera-Lease-Instance": "mirror-2", "X-Alkera-Lease-Epoch": "3"}
    assert (await probe.post(first, headers=mirror_one)).status_code == 200
    # The same folder from a second instance of the same holder: one budget.
    assert (await probe.post(first, headers=mirror_two)).status_code == 429
    # The same box's other chat has its own.
    assert (await probe.post(second, headers=mirror_one)).status_code == 200
    # ...and so does another box on the folder the first one exhausted.
    other_box = await new_box()
    assert (await probe.post(first, headers=other_box)).status_code == 200


@pytest.mark.asyncio
async def test_naming_a_new_folder_every_time_still_spends_one_shared_budget(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder in the key is a path segment the caller writes.

    Every request naming a folder nobody has named before is a first request on
    its own budget, so on the per-folder leg alone a caller rotating the segment
    is never counted at all — and the route it reaches resolves the drive, walks
    the caller's roles and reads the node before it can answer that there is no
    such folder. The address is the one part of the key nothing in the request
    can rotate, and it is what bounds that caller.
    """
    _pin(monkeypatch, "lease_node", per_minute=600, burst=300)
    _pin(monkeypatch, "lease_node_ip", per_minute=3, burst=3)
    box = {"Authorization": f"Bearer {_jwt()}", **agent_headers("sess-box-a")}
    drive = uuid4()

    admitted = [
        (await probe.post(f"/held/{drive}/{uuid4()}", headers=box)).status_code for _ in range(3)
    ]
    assert admitted == [200, 200, 200]
    refused = await probe.post(f"/held/{drive}/{uuid4()}", headers=box)
    assert refused.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "budget"),
    [
        pytest.param("POST", "/mutate", "mutation", id="mutation"),
        pytest.param("GET", "/read", "read", id="read"),
        pytest.param("GET", "/chats/{id}", "read", id="read-naming-a-resource"),
        pytest.param("POST", "/box", "machine", id="machine"),
    ],
)
async def test_asserting_a_new_machine_every_time_stays_on_the_persons_own_budget(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    owner: Headers,
    method: str,
    path: str,
    budget: str,
) -> None:
    """The agent assertion is two headers any member can write on their own
    session. Taken at its word, a fresh id on every request was a fresh budget
    every time -- the person's 120 writes a minute became the 9,000 an address
    had. It is counted as a machine only once it verifies as one the person
    registered on this very session; anything else -- a made-up id, a
    colleague's real box -- is simply the person, on the budget they would
    have without the header, so rotating the id buys nothing."""
    _pin(monkeypatch, budget, per_minute=3, burst=3)
    colleague, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    _token, colleagues_box = await registered_box(
        real_session, user_id=colleague.id, email=colleague.email, org_id=org_admin.org_id
    )
    asserted = [uuid4().hex, str(uuid4()), colleagues_box, "sess-box-a"]

    def send(agent_id: str | None) -> Awaitable[Any]:
        extra = agent_headers(agent_id) if agent_id is not None else {}
        return probe.request(method, path.format(id=uuid4()), headers={**owner, **extra})

    admitted = [(await send(agent_id)).status_code for agent_id in asserted[:3]]
    assert admitted == [200, 200, 200]
    refused = await send(asserted[3])
    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "rate_limited"
    # The same budget the person spends without any header: already gone.
    assert (await send(None)).status_code == 429


@pytest.mark.asyncio
async def test_a_verified_machine_gets_the_machine_budget_and_its_owner_keeps_theirs(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    owner: Headers,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """The other side of the same line: the box the person registered, speaking
    on the session it registered with, is counted on the machine's numbers,
    and the owner's own writes are never its to spend."""
    _pin(monkeypatch, "mutation", per_minute=1, burst=1)
    _pin(monkeypatch, "machine", per_minute=4, burst=4)
    box = await new_box()
    assert (await probe.post("/mutate", headers=owner)).status_code == 200
    assert (await probe.post("/mutate", headers=owner)).status_code == 429
    assert [(await probe.post("/mutate", headers=box)).status_code for _ in range(4)] == [200] * 4
    assert (await probe.post("/mutate", headers=box)).status_code == 429


@pytest.mark.asyncio
async def test_the_boxs_machine_id_on_the_owners_other_session_is_the_owner(
    probe: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    owner: Headers,
    new_box: Callable[[], Awaitable[Headers]],
) -> None:
    """The registration is bound to the credential the box registered with: the
    owner's browser or laptop session naming the box's id is still the owner,
    and cannot spend its way onto the box's budget."""
    _pin(monkeypatch, "mutation", per_minute=1, burst=1)
    box = await new_box()
    machine_id = box["X-Alkera-Agent-Id"]
    posing = {**owner, **agent_headers(machine_id)}
    assert (await probe.post("/mutate", headers=posing)).status_code == 200
    assert (await probe.post("/mutate", headers=owner)).status_code == 429
    assert (await probe.post("/mutate", headers=box)).status_code == 200


@pytest.mark.asyncio
async def test_a_folder_segment_that_is_not_a_node_id_spends_the_boxs_own_budget(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is built before the endpoint coerces the segment, so free text
    reaches it. A request whose folder is not a node id cannot be counted per
    folder — it names none — so it is counted against the box that sent it,
    and two of them share that budget rather than buying one each."""
    _pin(monkeypatch, "lease_node", per_minute=1, burst=1)
    box = {"Authorization": f"Bearer {_jwt()}", **agent_headers("sess-box-a")}
    drive = uuid4()

    assert (await probe.post(f"/held/{drive}/not-a-node", headers=box)).status_code == 200
    assert (await probe.post(f"/held/{drive}/nor-is-this-one", headers=box)).status_code == 429
    # ...while a real folder this box holds still has its own.
    assert (await probe.post(f"/held/{drive}/{uuid4()}", headers=box)).status_code == 200


@pytest.mark.asyncio
async def test_a_credential_attempt_is_keyed_by_the_account_and_by_the_address(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both legs must pass. The same account from two addresses shares one
    budget (a spray from many hosts at one victim is still one account), and
    one address naming many accounts shares the other."""
    _pin(monkeypatch, "credential", per_minute=1, burst=1)
    _pin(monkeypatch, "credential_ip", per_minute=2, burst=2)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
    here = {"X-Forwarded-For": "203.0.113.1"}
    there = {"X-Forwarded-For": "203.0.113.2"}
    victim = {"email": "Victim@Example.com", "password": "x"}
    other = {"email": "other@example.com", "password": "x"}
    assert (await probe.post("/attempt", json=victim, headers=here)).status_code == 200
    # Same account, different case, different address: the account leg refuses.
    lowered = {"email": "victim@example.com", "password": "x"}
    assert (await probe.post("/attempt", json=lowered, headers=there)).status_code == 429
    # A different account from the first address passes its account leg...
    assert (await probe.post("/attempt", json=other, headers=here)).status_code == 200
    # ...and the address leg (two per minute) now refuses a third account.
    third = {"email": "third@example.com", "password": "x"}
    assert (await probe.post("/attempt", json=third, headers=here)).status_code == 429
    assert (await probe.post("/attempt", json=third, headers=there)).status_code == 200


@pytest.mark.asyncio
async def test_a_webhook_is_keyed_by_its_source_address_never_by_its_body(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enforcer runs before any signature is checked, so a workspace id in
    the body is whatever the sender wrote: naming a fresh one buys no fresh
    budget, and naming a victim's spends none of the victim's."""
    _pin(monkeypatch, "webhook_ip", per_minute=1, burst=1)
    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 1)
    here = {"X-Forwarded-For": "203.0.113.1"}
    there = {"X-Forwarded-For": "203.0.113.2"}
    first = await probe.post("/hook", json={"team_id": "T1"}, headers=here)
    assert first.status_code == 200
    # ...and keying the request left the raw body for the signature check.
    assert first.json()["body_bytes"] > 0
    assert (await probe.post("/hook", json={"team_id": "T2"}, headers=here)).status_code == 429
    payload = '{"team": {"id": "T3"}, "type": "block_actions"}'
    form = await probe.post("/hook", data={"payload": payload}, headers=here)
    assert form.status_code == 429
    assert (await probe.post("/hook", json={"team_id": "T1"}, headers=there)).status_code == 200


@pytest.mark.parametrize("name", ["webhook_tenant", "webhook_reject"])
def test_a_class_its_route_charges_cannot_be_declared(name: str) -> None:
    """Their keys are trustworthy only at a point inside the handler; declared
    on a route, the enforcer would compute them from the unverified request."""
    with pytest.raises(LookupError):
        limited(name)


@pytest.mark.asyncio
async def test_the_exempt_routes_are_never_counted(
    client: AsyncClient, pin_clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness has to answer while the app is being hammered."""
    del pin_clock
    _pin(monkeypatch, "read", per_minute=1, burst=1)
    for _ in range(5):
        assert (await client.get("/health/live")).status_code == 200


@pytest.mark.asyncio
async def test_the_flag_turns_it_off_wholesale(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the switch is proven; production refuses to run this way
    (see the settings suite)."""
    _pin(monkeypatch, "mutation", per_minute=1, burst=1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(5):
        assert (await probe.post("/mutate")).status_code == 200


# --------------------------------------------------------------------------
# The two endpoints of the finding, through the real app
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_login_spray_at_one_account_is_refused_and_told_how_long(
    client: AsyncClient, org_admin: OrgWithAdmin, pin_clock: ManualClock
) -> None:
    """Which of the two throttles on this route answers a password spray, and
    what it tells the caller.

    Two mechanisms can refuse the same run of wrong passwords at one address
    from one client: the credential class here, which counts REQUESTS, and the
    account lockout, which counts consecutive FAILURES. The lockout is the one
    that must answer. It is the only one of the two that knows the attempt was
    wrong, so it is the only one a patient attacker cannot simply pace under —
    and its counter lives in Postgres, so it holds across every task, while a
    token bucket is per process.

    So the sequence is fixed, and it is the lockout's schedule, not the
    bucket's: every attempt up to and including the one that reaches the
    threshold is an ordinary 401, and from the next one on the answer is 429
    with the code ``account_locked``. The shipped threshold sits below the
    shipped burst precisely so it lands there — a burst smaller than the
    threshold would put the volume limiter in front and make the brute-force
    control unreachable.

    ``account_locked`` is safe to name. It is not an account-existence oracle:
    an address with no usable account keeps the same counter, on the same
    schedule, and answers the same status, code and wait at the same attempt.

    And that refusal carries ``Retry-After``, counting down the cool-off that
    was just stamped. The person behind a lockout is usually the account's
    owner who mistyped; with no number their only way to learn when they may
    sign in is to keep knocking on the endpoint the lock exists to protect.
    """
    del pin_clock  # the bucket must not refill mid-spray
    threshold = settings.auth_lockout_threshold
    # What keeps the lockout reachable at all: the bucket must still have room
    # on the attempt that trips it, or the 429 below would be the wrong one.
    assert threshold < settings.rate_limit_credential_burst
    wrong = {"email": f"nobody-{secrets.token_hex(4)}@alkera.dev", "password": "not-it"}
    statuses = [
        (await client.post("/api/v1/auth/login", json=wrong)).status_code for _ in range(threshold)
    ]
    assert statuses == [401] * threshold, statuses

    refused = await client.post("/api/v1/auth/login", json=wrong)
    assert refused.status_code == 429
    envelope = refused.json()["error"]
    assert envelope["code"] == "account_locked"
    # The wait is the cool-off that was just stamped, not a constant and not the
    # bucket's much shorter window.
    cool_off = settings.auth_lockout_duration_seconds
    retry_after = int(refused.headers["retry-after"])
    assert envelope["details"]["retry_after"] == retry_after
    assert cool_off - 60 <= retry_after <= cool_off, retry_after


# --------------------------------------------------------------------------
# The durable hourly window behind the strict classes
# --------------------------------------------------------------------------


class TestDurableWindow:
    @pytest.mark.asyncio
    async def test_the_hour_is_counted_across_sessions(self, pin_clock: ManualClock) -> None:
        window = DurableWindow()
        key = f"acct:{secrets.token_hex(8)}"
        now = pin_clock.wall()
        assert [await window.hit("credential", key, now=now) for _ in range(3)] == [1, 2, 3]
        # A fresh instance (another task) sees the same count: the store is
        # the table, not the process.
        assert await DurableWindow().hit("credential", key, now=now) == 4
        # The next hour starts clean; classes do not share a row.
        assert await window.hit("credential", key, now=now.replace(hour=now.hour + 1)) == 1
        assert await window.hit("mint", key, now=now) == 1

    @pytest.mark.asyncio
    async def test_the_ceiling_refuses_with_a_growing_bounded_wait(
        self, client: AsyncClient, org_admin: OrgWithAdmin, pin_clock: ManualClock, monkeypatch
    ) -> None:
        """Past the hourly ceiling every further attempt on that account waits
        longer — doubling from a minute — but never past fifteen minutes and
        never past the end of the hour, so a stranger naming the account can
        impose a bounded, self-expiring wait and nothing permanent."""
        monkeypatch.setattr(settings, "rate_limit_credential_per_hour", 2)
        monkeypatch.setattr(settings, "rate_limit_credential_per_minute", 1000)
        monkeypatch.setattr(settings, "rate_limit_credential_burst", 1000)
        wrong = {"email": org_admin.admin_email, "password": "not-it"}
        pin_clock.now = 0.0  # 10:00, the top of an hour
        assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 401
        assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 401
        waits = []
        for _ in range(6):
            refused = await client.post("/api/v1/auth/login", json=wrong)
            assert refused.status_code == 429
            waits.append(int(refused.headers["retry-after"]))
        assert waits == [60, 120, 240, 480, 900, 900]
        # Inside the last minute of the hour the wait is the minute that is left.
        pin_clock.now = 3600.0 - 30.0
        late = await client.post("/api/v1/auth/login", json=wrong)
        assert late.status_code == 429
        assert int(late.headers["retry-after"]) <= 30
        # The window rolls over and the account is usable again, with the
        # right password too.
        pin_clock.now = 3600.0
        good = {"email": org_admin.admin_email, "password": org_admin.admin_password}
        assert (await client.post("/api/v1/auth/login", json=good)).status_code == 200

    @pytest.mark.asyncio
    async def test_concurrent_hits_on_one_key_each_count_once(self, pin_clock: ManualClock) -> None:
        """The upsert is atomic: a burst of attempts on one key, each on its own
        session, lands as one count apiece, with none lost and none doubled."""
        key = f"acct:{secrets.token_hex(8)}"
        now = pin_clock.wall()
        counts = await asyncio.gather(
            *(DurableWindow().hit("credential", key, now=now) for _ in range(12))
        )
        assert sorted(counts) == list(range(1, 13))

    @pytest.mark.asyncio
    async def test_a_held_counter_row_is_a_bounded_wait_not_a_queue(
        self, pin_clock: ManualClock
    ) -> None:
        """Another attempt holds the key's row (its commit is slow). The next
        hit gives up within its short wait and says so, instead of queueing the
        request behind the row until the request's own lock timeout; the
        attempt it gave up on is not counted."""
        key = f"acct:{secrets.token_hex(8)}"
        now = pin_clock.wall()
        assert await DurableWindow().hit("credential", key, now=now) == 1
        async with AsyncSessionLocal() as holder:
            await holder.execute(
                text(
                    "SELECT count FROM rate_limit_windows "
                    "WHERE rate_class = 'credential' AND key = :k FOR UPDATE"
                ),
                {"k": key},
            )
            with pytest.raises(DurableBusy):
                await asyncio.wait_for(DurableWindow().hit("credential", key, now=now), timeout=5)
            await holder.rollback()
        assert await DurableWindow().hit("credential", key, now=now) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("refuse_when_busy", "status"),
        [
            pytest.param(True, 429, id="a-class-guarding-a-secret-refuses"),
            pytest.param(False, 200, id="any-other-class-admits"),
        ],
    )
    async def test_a_busy_counter_answers_by_the_class_policy(
        self,
        pin_clock: ManualClock,
        monkeypatch: pytest.MonkeyPatch,
        refuse_when_busy: bool,
        status: int,
    ) -> None:
        """A busy counter is never a 500: the class says whether the attempt
        is refused with a one-second wait or admitted on the in-memory legs."""

        class _Busy:
            async def hit(self, *_a: Any, **_k: Any) -> int:
                raise DurableBusy("credential")

            def reset(self) -> None:
                return None

        monkeypatch.setattr(REGISTRY, "durable", _Busy())
        monkeypatch.setitem(
            CLASSES,
            "credential",
            replace(CLASSES["credential"], refuse_when_busy=refuse_when_busy),
        )
        _pin(monkeypatch, "credential", per_minute=100, burst=100)
        async with AsyncClient(
            transport=ASGITransport(app=_probe_app()), base_url="http://probe"
        ) as client:
            answer = await client.post("/attempt", json={"email": "someone@example.com"})
        assert answer.status_code == status, answer.text
        if status == 429:
            assert answer.headers["retry-after"] == "1"

    @pytest.mark.asyncio
    async def test_an_unreachable_store_leaves_the_in_memory_leg_deciding(
        self, probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable leg is a second opinion, never an availability
        dependency: with it down, the bucket still admits and still refuses."""

        class _Down:
            async def hit(self, *_a: Any, **_k: Any) -> int:
                raise ConnectionError("store down")

            def reset(self) -> None:
                return None

        monkeypatch.setattr(REGISTRY, "durable", _Down())
        _pin(monkeypatch, "credential", per_minute=2, burst=2)
        body = {"email": "someone@example.com"}
        assert (await probe.post("/attempt", json=body)).status_code == 200
        assert (await probe.post("/attempt", json=body)).status_code == 200
        assert (await probe.post("/attempt", json=body)).status_code == 429


# --------------------------------------------------------------------------
# Refusals are on record — without the credential
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_is_logged_with_a_digest_never_the_account(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api import rate_limit as module

    seen: list[dict[str, Any]] = []

    class _Log:
        def warning(self, event: str, **fields: Any) -> None:
            seen.append({"event": event, **fields})

    monkeypatch.setattr(module, "log", _Log())
    _pin(monkeypatch, "credential", per_minute=1, burst=1)
    body = {"email": "victim@example.com", "password": "hunter2"}
    await probe.post("/attempt", json=body)
    assert (await probe.post("/attempt", json=body)).status_code == 429
    refusals = [s for s in seen if s["event"] == "ratelimit.refused"]
    assert len(refusals) == 1
    entry = refusals[0]
    assert entry["rate_class"] == "credential"
    assert entry["route"] == "/attempt"
    flat = repr(entry)
    assert "victim@example.com" not in flat
    assert "hunter2" not in flat
    assert len(entry["key_digest"]) == 16


# --------------------------------------------------------------------------
# The per-folder cap on asking a machine for bytes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_folder_asked_past_its_minute_is_told_throttled_and_asks_nothing(
    uvicorn_server: str,
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``files_promote_per_minute`` bounds how often one process asks a folder's
    machine for bytes. Past it a reader is not refused a 429 — the reader did
    nothing wrong, the folder is busy — but told, in the same ``live_pending``
    the browser already retries on, that the machine was not asked
    (``throttled``), with the same ``Retry-After``; and nothing is published.

    Two files under one lease, a cap of one: the first reader's request goes
    out, the second reader's does not."""
    import asyncio

    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.scoped import FilesystemScoped
    from backend.services.files.store import set_store_factory
    from tests.files._files_kit import FilesFixtures
    from tests.files._promote_world import (
        drained,
        held_world,
        reported,
        requests_heard,
    )

    root = tmp_path / "files-store"
    root.mkdir()
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)
    monkeypatch.setattr(settings, "files_promote_per_minute", 1)
    monkeypatch.setattr(settings, "files_promote_ack_seconds", 0.3)
    try:
        admin = await login(client, org_admin.admin_email, org_admin.admin_password)
        world = await held_world(
            admin,
            FilesFixtures(real_session, org_admin.org_id, org_admin.admin_id),
            real_session,
            lambda: {"Idempotency-Key": uuid4().hex},
            org_id=org_admin.org_id,
            admin_id=org_admin.admin_id,
            admin_email=org_admin.admin_email,
        )
        await world.report(reported("a.csv", b"a\n"), reported("b.csv", b"b\n"))
        first, second = await world.node_at(b"a.csv"), await world.node_at(b"b.csv")
        with requests_heard() as heard:
            asked = await admin.get(world.content(first))
            loop = asyncio.get_running_loop()
            started = loop.time()
            throttled = await admin.get(world.content(second))
            elapsed = loop.time() - started
            await asyncio.sleep(0.3)
            assert len(drained(heard)) == 1, "the throttled reader published nothing"
        assert asked.status_code == 409, asked.text
        assert asked.json()["detail"]["outcome"] == "timed_out"
        assert throttled.status_code == 409, throttled.text
        assert throttled.headers["retry-after"] == "2"
        assert throttled.json()["detail"]["outcome"] == "throttled"
        assert elapsed < 0.3, "a throttled reader is answered at once"
    finally:
        set_store_factory(None)


# The device grant polls by design — a patient login is not a spray
# --------------------------------------------------------------------------


async def _start_device_login(client: AsyncClient) -> Any:
    return await client.post("/api/v1/auth/device/code", data={"client_id": "alkera-cli"})


@pytest.mark.asyncio
async def test_a_device_login_polled_until_it_expires_is_never_throttled(
    client: AsyncClient, pin_clock: ManualClock
) -> None:
    """The client polls `/token` at the interval the server itself issued, for
    as long as the code lives. That cadence is the protocol, so neither the
    minute nor the hour may refuse it — and it must not spend the budget the
    same address needs to start its next login."""
    started = await _start_device_login(client)
    assert started.status_code == 200, started.text
    device = started.json()
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device["device_code"],
        "client_id": "alkera-cli",
    }
    refused = []
    for _ in range(device["expires_in"] // device["interval"]):
        pin_clock.advance(device["interval"])
        polled = await client.post("/api/v1/auth/device/token", data=form)
        if polled.status_code == 429:
            refused.append(pin_clock.now)
    assert refused == []
    again = await _start_device_login(client)
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_starting_a_device_login_is_not_charged_for_the_address_other_sign_ins(
    client: AsyncClient, pin_clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/code` names no account, so its account leg falls back to the address.
    That fallback is its own hourly row: sharing the address leg's row would
    count each start twice and judge everything the office did that hour
    against the (smaller) account ceiling."""
    monkeypatch.setattr(settings, "rate_limit_credential_per_hour", 3)
    monkeypatch.setattr(settings, "rate_limit_credential_ip_per_hour", 1000)
    for i in range(5):
        pin_clock.advance(10)
        wrong = {"email": f"nobody-{i}-{secrets.token_hex(4)}@alkera.dev", "password": "not-it"}
        assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 401
    statuses = []
    for _ in range(4):
        pin_clock.advance(10)
        statuses.append((await _start_device_login(client)).status_code)
    # Three starts an hour, exactly — still bounded, and by its own count.
    assert statuses == [200, 200, 200, 429]


def test_the_poll_class_has_no_hourly_leg() -> None:
    """An hourly ceiling on a request the protocol repeats every second is a
    login that dies mid-approval, and a Postgres write per poll."""
    assert [leg.per_hour for leg in CLASSES["device_poll"].legs] == [None, None]


@pytest.mark.asyncio
async def test_a_poll_flood_at_one_device_code_is_still_refused(
    client: AsyncClient, pin_clock: ManualClock
) -> None:
    del pin_clock
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": secrets.token_urlsafe(32),
        "client_id": "alkera-cli",
    }
    burst = settings.rate_limit_device_poll_burst
    statuses = [
        (await client.post("/api/v1/auth/device/token", data=form)).status_code
        for _ in range(burst + 1)
    ]
    assert 429 not in statuses[:burst]
    assert statuses[burst] == 429


# --------------------------------------------------------------------------
# Verifying the assertion before any route runs
# --------------------------------------------------------------------------


class TestMachineAssertionCache:
    """The throttles ask this cache, not the header, whether a request is a
    machine. Its two promises are what keep that cheap without making it a new
    thing to abuse: an answer is re-read once its time is up, and a caller
    spraying made-up ids runs out of lookups, not the database of reads."""

    @staticmethod
    async def _box(real_session: AsyncSession, org_admin: OrgWithAdmin) -> dict[str, str]:
        from alkera_core.auth import decode_session_token

        token, machine_id = await registered_box(
            real_session,
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_id=org_admin.org_id,
        )
        claims = decode_session_token(token)
        return {
            "machine_id": machine_id,
            "user_id": str(org_admin.admin_id),
            "org_id": str(org_admin.org_id),
            "credential_id": str(claims.jti),
        }

    @pytest.mark.asyncio
    async def test_a_verified_machine_is_trusted_for_its_window_then_read_again(
        self, real_session: AsyncSession, org_admin: OrgWithAdmin
    ) -> None:
        from backend.services.compute.assertion import (
            POSITIVE_TTL_SECONDS,
            MachineAssertionCache,
        )
        from sqlalchemy import text

        now = [1000.0]
        cache = MachineAssertionCache(clock=lambda: now[0])
        box = await self._box(real_session, org_admin)
        assert await cache.speaks(**box) is True
        # The box re-registers on another credential: this one no longer speaks.
        await real_session.execute(
            text("UPDATE compute_allocations SET registered_jti = 'rotated' WHERE id = :id"),
            {"id": box["machine_id"]},
        )
        await real_session.commit()
        now[0] += POSITIVE_TTL_SECONDS - 1
        assert await cache.speaks(**box) is True, "inside its window the answer is the cache's"
        now[0] += 2
        assert await cache.speaks(**box) is False, "past its window the answer is re-read"

    @pytest.mark.asyncio
    async def test_a_spray_of_made_up_ids_runs_out_of_lookups(
        self, real_session: AsyncSession, org_admin: OrgWithAdmin
    ) -> None:
        from backend.services.compute.assertion import LOOKUPS_PER_MINUTE, MachineAssertionCache

        now = [1000.0]
        cache = MachineAssertionCache(clock=lambda: now[0])
        box = await self._box(real_session, org_admin)
        for _ in range(LOOKUPS_PER_MINUTE):
            assert await cache.speaks(**{**box, "machine_id": str(uuid4())}) is False
        # The principal's lookups for this minute are spent: even its real box
        # is not read (and so not verified) until the minute turns.
        assert await cache.speaks(**box) is False
        now[0] += 60
        assert await cache.speaks(**box) is True


# --------------------------------------------------------------------------
# A box on its own machine credential is counted on box-sized budgets
# --------------------------------------------------------------------------


def _machine_bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer alk_machine_{secrets.token_urlsafe(24)}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [pytest.param("GET", "/read", id="read"), pytest.param("POST", "/mutate", id="mutation")],
)
async def test_a_box_on_its_machine_credential_is_not_held_to_a_persons_budget(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """A box syncing an agent's burst makes several requests per file. On the
    person-sized classes it was throttled against its own API; on its own
    credential it is counted on the `box` budget instead, and a person on the
    same route still meets theirs."""
    _pin(monkeypatch, "read", per_minute=2, burst=2)
    _pin(monkeypatch, "mutation", per_minute=2, burst=2)
    _pin(monkeypatch, "box", per_minute=100, burst=100)
    box = _machine_bearer()
    statuses = [(await probe.request(method, path, headers=box)).status_code for _ in range(5)]
    assert statuses == [200] * 5
    person = [(await probe.request(method, path)).status_code for _ in range(3)]
    assert person == [200, 200, 429]


@pytest.mark.asyncio
async def test_the_box_budget_still_refuses_past_its_own_numbers(
    probe: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Box-sized, not unlimited: past its own budget a box is refused too."""
    _pin(monkeypatch, "box", per_minute=2, burst=2)
    box = _machine_bearer()
    statuses = [(await probe.get("/read", headers=box)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
