"""The prefix class of the pre-buffer body guard.

Every Files upload route carries an id in its path, so the edge's exact-match
body exemption can never name one; the edge grows a
``STARTS_WITH prefix``/``ENDS_WITH suffix``/``method`` class instead, and
:data:`GUARDED_PREFIXES` is its app-side twin -- the same six patterns, plus the
per-pattern cap the edge does not impose. Two lists that drifted would fail open
in the worst direction (exempt at the edge, unbounded in the app); the drift
check against the exported edge artifact lives with the deployment's own tests.

Everything below decides from the ASGI scope. The inner app counts the body
bytes it was handed: a rejection that consumed a byte fails the test rather than
quietly passing, which is the whole claim of a PRE-buffer guard.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.auth.ci_token import mint_ci_token
from alkera_core.auth.pat_token import mint_pat_token
from alkera_core.auth.proxy_token import mint_proxy_token
from alkera_core.auth.tokens import encode_session_token
from alkera_core.config import settings
from alkera_core.schemas.objects.api import MAX_MESSAGE_TEXT_LENGTH
from backend.api.body_limit import (
    _ANON_PREFIX_MAX_BYTES,
    _CHAT_BODY_MAX_BYTES,
    GUARDED_PREFIXES,
    GateIngestBodyLimitMiddleware,
    GuardedPrefix,
)
from backend.api.routes.files.uploads import MAX_COMPLETE_PARTS

#: A concrete request path for each pattern, with real-looking ids where the
#: pattern's whole point is that an id sits between the prefix and the suffix.
_SAMPLES: dict[str, str] = {
    "/api/v1/files/uploads/|/parts/|": "/api/v1/files/uploads/sess_7f3a/parts/12",
    "/api/v1/files/uploads/||/complete": "/api/v1/files/uploads/sess_7f3a/complete",
    "/api/v1/files/drives/||/content": "/api/v1/files/drives/d1/items/i9/content",
    "/api/v1/files/drives/||/bulk": "/api/v1/files/drives/d1/bulk",
    "/api/v1/files/drives/||/tree": "/api/v1/files/drives/d1/tree",
    "/api/v1/files/drives/||/snapshots": "/api/v1/files/drives/d1/snapshots",
    "/api/v1/files/drives/||/lease/release": "/api/v1/files/drives/d1/items/i9/lease/release",
    "/api/v1/files/drives/||/lease/live": "/api/v1/files/drives/d1/items/i9/lease/live",
    "/api/v1/files/drives/||/lease/tree/digests": (
        "/api/v1/files/drives/d1/items/i9/lease/tree/digests"
    ),
    "/api/v1/files/drives/||/leases/heartbeat": "/api/v1/files/drives/d1/leases/heartbeat",
    "/api/v1/chats/||/messages": "/api/v1/chats/1f2e3d4c/messages",
    "/api/v1/chats/||/answer": "/api/v1/chats/1f2e3d4c/answer",
    "/api/v1/notebooks/||/events": "/api/v1/notebooks/d1/i9/events",
    "/api/v1/notebooks/||/ops": "/api/v1/notebooks/d1/i9/ops",
    "/api/v1/notebooks/||/comm": "/api/v1/notebooks/d1/i9/comm",
    "/api/v1/notebooks/||/rebase": "/api/v1/notebooks/d1/i9/rebase",
    "/api/v1/notebooks/||/runs": "/api/v1/notebooks/d1/i9/runs",
    "/api/v1/notebooks/||/frames": "/api/v1/notebooks/d1/i9/frames",
    "/api/v1/notebooks/||/outputs/clear": "/api/v1/notebooks/d1/i9/outputs/clear",
    "/api/v1/notebooks/|/env/|": "/api/v1/notebooks/d1/i9/env/install",
}


def _key(pattern: GuardedPrefix) -> str:
    return f"{pattern.prefix}|{pattern.contains}|{pattern.suffix}"


def _sample(pattern: GuardedPrefix) -> str:
    return _SAMPLES[_key(pattern)]


def _case_id(pattern: GuardedPrefix) -> str:
    return (pattern.suffix or pattern.contains).strip("/").replace("/", "-")


def _method(pattern: GuardedPrefix) -> str:
    """One method the edge actually exempts, for the cases that want the real
    request shape."""
    return sorted(pattern.methods)[0]


#: The middleware's global cap for these cases: above every per-pattern cap, so
#: a boundary case tests the PATTERN's cap rather than tripping the global one
#: first. Derived, because the largest pattern cap grows with the upload part
#: size and a literal here would go stale the next time it does.
_ANY_PATTERN_CAP = max(p.max_bytes for p in GUARDED_PREFIXES) + 1


def _scope(
    path: str, method: str, content_length: str | None, *, credential: bool = True
) -> dict[str, Any]:
    """A request scope. Carries a session cookie unless a case is specifically
    about the credential-less shape: every route these patterns cover requires
    authentication, so the anonymous request is the exception, not the norm."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"test")]
    if credential:
        headers.append((b"cookie", f"{COOKIE_NAME}=session-token".encode()))
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    return {"type": "http", "method": method, "path": path, "headers": headers}


class _Harness:
    """The middleware over a recording inner app that counts the body bytes it
    was handed."""

    def __init__(self, *, body: bytes = b"", max_bytes: int = _ANY_PATTERN_CAP) -> None:
        self.inner_called = False
        self.consumed = 0
        self.sent: list[dict[str, Any]] = []
        self._body = body
        self.middleware = GateIngestBodyLimitMiddleware(
            self._inner, max_bytes=max_bytes, max_inflight_bytes=1 << 40
        )

    async def _inner(self, scope: Any, receive: Any, send: Any) -> None:
        self.inner_called = True
        message = await receive()
        self.consumed += len(message.get("body", b""))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def _receive(self) -> dict[str, Any]:
        return {"type": "http.request", "body": self._body, "more_body": False}

    async def call(self, scope: dict[str, Any]) -> None:
        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        await self.middleware(scope, self._receive, send)

    @property
    def status(self) -> int:
        return int(self.sent[0]["status"])


PATTERNS = [pytest.param(p, id=_case_id(p)) for p in GUARDED_PREFIXES]
#: The patterns whose cap lies above the anonymous allowance, where the
#: credential precondition is what bounds a credential-less body; at or below
#: it, the cap itself refuses anything larger.
ABOVE_ALLOWANCE = [
    pytest.param(p, id=_case_id(p))
    for p in GUARDED_PREFIXES
    if p.max_bytes > _ANON_PREFIX_MAX_BYTES
]
WITHIN_ALLOWANCE = [
    pytest.param(p, id=_case_id(p))
    for p in GUARDED_PREFIXES
    if p.max_bytes <= _ANON_PREFIX_MAX_BYTES
]


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_body_at_the_cap_is_admitted(pattern: GuardedPrefix) -> None:
    """The cap is inclusive: a body of exactly ``max_bytes`` reaches the app.
    The boundary is the interesting value -- an off-by-one here refuses the
    largest legal part of every multipart upload."""
    harness = _Harness()
    await harness.call(_scope(_sample(pattern), _method(pattern), str(pattern.max_bytes)))
    assert harness.inner_called
    assert harness.status == 200


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_one_byte_over_the_cap_is_refused_before_any_body_is_read(
    pattern: GuardedPrefix,
) -> None:
    """cap + 1 is a 413 decided from the declared length, with zero body bytes
    consumed. Counting the bytes is the point: without the guard FastAPI
    materializes the whole body before any dependency runs."""
    harness = _Harness(body=b"x" * 4096)
    await harness.call(_scope(_sample(pattern), _method(pattern), str(pattern.max_bytes + 1)))
    assert not harness.inner_called
    assert harness.consumed == 0
    assert harness.status == 413


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize(
    "content_length",
    [
        pytest.param(None, id="absent"),
        pytest.param("not-a-number", id="unparsable"),
        pytest.param("-1", id="negative"),
    ],
)
async def test_a_length_that_cannot_be_trusted_is_411(
    pattern: GuardedPrefix, content_length: str | None
) -> None:
    """A size the guard cannot trust is a 411, never an admitted body: without a
    parsable Content-Length the cap has nothing to decide on."""
    harness = _Harness(body=b"x" * 4096)
    await harness.call(_scope(_sample(pattern), _method(pattern), content_length))
    assert not harness.inner_called
    assert harness.consumed == 0
    assert harness.status == 411


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_transfer_encoding_is_411_even_with_a_content_length(
    pattern: GuardedPrefix,
) -> None:
    """A chunked body is framed by the coding, not by any Content-Length riding
    alongside, so the declared size is a lie the guard refuses to act on."""
    scope = _scope(_sample(pattern), _method(pattern), "10")
    scope["headers"].append((b"transfer-encoding", b"chunked"))
    harness = _Harness(body=b"x" * 4096)
    await harness.call(scope)
    assert not harness.inner_called
    assert harness.status == 411


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_a_get_on_a_guarded_prefix_is_bounded_too(pattern: GuardedPrefix) -> None:
    """The app-side bound ignores the method even though the edge exemption does
    not. FastAPI hands a body to a GET handler that declares one, so a
    method-gated guard would leave a GET with a 200 MiB body unbounded."""
    harness = _Harness(body=b"x" * 4096)
    await harness.call(_scope(_sample(pattern), "GET", str(pattern.max_bytes + 1)))
    assert not harness.inner_called
    assert harness.consumed == 0
    assert harness.status == 413


@pytest.mark.parametrize(
    "method", [pytest.param("GET", id="get"), pytest.param("DELETE", id="delete")]
)
async def test_a_bodyless_method_the_edge_does_not_exempt_passes_through(method: str) -> None:
    """A request with neither Content-Length nor Transfer-Encoding carries no
    body at all, so there is nothing for the guard to bound -- and the prefix
    covers a whole family, so 411-ing it would break `GET /uploads/{id}` and
    `DELETE /uploads/{id}`, which the edge never exempted in the first place."""
    harness = _Harness()
    await harness.call(_scope("/api/v1/files/uploads/sess_7f3a", method, None))
    assert harness.inner_called
    assert harness.status == 200


async def test_a_files_path_matching_no_pattern_is_untouched() -> None:
    """The guard bounds the exempted patterns, not the Files surface: a listing
    carries no edge exemption and must not need a Content-Length here."""
    harness = _Harness()
    await harness.call(_scope("/api/v1/files/drives/d1/items/i9/children", "GET", None))
    assert harness.inner_called
    assert harness.status == 200


async def test_the_smallest_matching_cap_wins() -> None:
    """``/tree`` and ``/bulk`` share a prefix and differ only by suffix. A path
    matching more than one pattern takes the tighter cap -- loosest-wins is how a
    small-JSON route would inherit an upload-sized bound."""
    tree = next(p for p in GUARDED_PREFIXES if p.suffix == "/tree")
    bulk = next(p for p in GUARDED_PREFIXES if p.suffix == "/bulk")
    assert tree.max_bytes < bulk.max_bytes
    harness = _Harness()
    await harness.call(_scope("/api/v1/files/drives/d1/tree", "POST", str(bulk.max_bytes)))
    assert not harness.inner_called
    assert harness.status == 413


def test_the_patterns_are_exactly_the_edge_rule_table() -> None:
    """The caps and the method sets are the contract the WAF rule table names:
    the streaming part PUT 136 MiB, completion 8 MiB, content 64 MiB, bulk
    64 MiB, tree 8 MiB, snapshots 8 MiB, lease release 8 MiB, the three lease
    plane bodies at their derived maxima (live batch, digest walk, batched beat),
    and the two chat turn bodies 8 MiB. Pinned as a table because each value is a
    separately-negotiated bound, not a formula.

    The upload prefix is TWO patterns, not one: the 136 MiB cap belongs to the
    streamed part alone, and a single prefix-wide rule handed it to
    ``/complete`` -- a JSON body FastAPI parses into an object tree before any
    dependency runs."""
    mib = 1024 * 1024
    assert [
        (p.prefix, sorted(p.methods), p.contains, p.suffix, p.max_bytes) for p in GUARDED_PREFIXES
    ] == [
        ("/api/v1/files/uploads/", ["PUT"], "/parts/", "", 136 * mib),
        ("/api/v1/files/uploads/", ["POST"], "", "/complete", 8 * mib),
        ("/api/v1/files/drives/", ["PUT"], "", "/content", 64 * mib),
        ("/api/v1/files/drives/", ["POST"], "", "/bulk", 64 * mib),
        ("/api/v1/files/drives/", ["POST"], "", "/tree", 8 * mib),
        ("/api/v1/files/drives/", ["POST"], "", "/snapshots", 8 * mib),
        ("/api/v1/files/drives/", ["POST"], "", "/lease/release", 8 * mib),
        ("/api/v1/files/drives/", ["POST"], "", "/lease/live", 3_198_989),
        ("/api/v1/files/drives/", ["POST"], "", "/lease/tree/digests", 4_195_615),
        ("/api/v1/files/drives/", ["POST"], "", "/leases/heartbeat", 1_653_012),
        ("/api/v1/chats/", ["POST"], "", "/messages", 8 * mib),
        ("/api/v1/chats/", ["POST"], "", "/answer", 8 * mib),
        # The notebook family, each cap from alkera_core.notebooks.limits.
        ("/api/v1/notebooks/", ["POST"], "", "/events", 8_978_432),
        ("/api/v1/notebooks/", ["POST"], "", "/ops", 8 * mib),
        ("/api/v1/notebooks/", ["POST"], "", "/comm", 8 * mib),
        ("/api/v1/notebooks/", ["POST"], "", "/rebase", 8_662_144),
        ("/api/v1/notebooks/", ["POST"], "", "/runs", 262_144),
        ("/api/v1/notebooks/", ["POST"], "", "/frames", 262_144),
        ("/api/v1/notebooks/", ["POST"], "", "/outputs/clear", 262_144),
        ("/api/v1/notebooks/", ["POST"], "/env/", "", 262_144),
    ]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/v1/files/uploads/sess_7f3a/complete", id="complete"),
        pytest.param("/api/v1/files/uploads/sess_7f3a", id="session"),
    ],
)
def test_the_upload_part_cap_reaches_only_the_part_route(path: str) -> None:
    """The 136 MiB cap sizes ONE body: a streamed part. It once reached every
    route under the uploads prefix, so an anonymous `POST …/complete` could pin
    that much raw JSON plus the object tree parsed from it."""
    parts = GUARDED_PREFIXES[0]
    assert parts.max_bytes == 136 * 1024 * 1024
    assert not parts.matches(path)


@pytest.mark.parametrize("pattern", ABOVE_ALLOWANCE)
async def test_a_body_past_the_anonymous_allowance_needs_a_credential_shape(
    pattern: GuardedPrefix,
) -> None:
    """Every route these patterns cover requires authentication, but FastAPI
    buffers -- and for the JSON ones parses -- the whole body before the auth
    dependency runs. A large credential-less request is therefore refused from
    the header shape alone, with nothing read: otherwise an account-less caller
    could make the process materialize the pattern's whole cap."""
    harness = _Harness(body=b"x" * 4096)
    declared = min(pattern.max_bytes, _ANON_PREFIX_MAX_BYTES * 2)
    assert declared > _ANON_PREFIX_MAX_BYTES
    await harness.call(_scope(_sample(pattern), _method(pattern), str(declared), credential=False))
    assert not harness.inner_called
    assert harness.consumed == 0
    assert harness.status == 401
    headers = dict(harness.sent[0]["headers"])
    assert headers[b"www-authenticate"] == b"Cookie"


def _session_jwt() -> str:
    """A real session JWT, minted the way the login route mints one."""
    token, _claims = encode_session_token(
        user_id=uuid.uuid4(),
        email="body-limit@alkera.test",
        org_team_id=uuid.uuid4(),
        platform_role=None,
    )
    return token


#: Every credential shape ``current_principal`` resolves, spelled as the header
#: a real client sends it in. Each raw secret comes from the minter that issues
#: it, so a prefix or an encoding the issuer changes shows up here rather than
#: as a fleet of 401s on bodies the route would have accepted.
CREDENTIAL_FORMS = [
    pytest.param(
        lambda: (b"cookie", f"{COOKIE_NAME}={_session_jwt()}".encode()), id="session-cookie"
    ),
    pytest.param(lambda: (b"authorization", f"Bearer {_session_jwt()}".encode()), id="bearer-jwt"),
    pytest.param(
        lambda: (b"authorization", f"Bearer {mint_ci_token()[0]}".encode()), id="ci-token"
    ),
    pytest.param(
        lambda: (b"authorization", f"Bearer {mint_proxy_token()[0]}".encode()), id="proxy-token"
    ),
    pytest.param(
        lambda: (b"authorization", f"Bearer {mint_pat_token()[0]}".encode()),
        id="personal-access-token",
    ),
    # The scheme is matched case-insensitively, exactly as `_extract_bearer`
    # matches it, so a client that spells it `bearer` is not locked out.
    pytest.param(
        lambda: (b"authorization", f"bearer {_session_jwt()}".encode()), id="lowercase-scheme"
    ),
]


@pytest.mark.parametrize("header", CREDENTIAL_FORMS)
async def test_every_credential_form_the_app_accepts_passes_the_precondition(
    header: Callable[[], tuple[bytes, bytes]],
) -> None:
    """The precondition runs before the auth dependency, so a shape it does not
    recognize is a 401 on a request the dependency would have admitted — an
    outage for that client on every body past the allowance, and one no test of
    the anonymous case can see. Every shape ``current_principal`` resolves is
    therefore driven through it."""
    pattern = _uploads_pattern()
    harness = _Harness(body=b"x" * 4096)
    declared = _ANON_PREFIX_MAX_BYTES * 2
    assert declared <= pattern.max_bytes
    scope = _scope(_sample(pattern), _method(pattern), str(declared), credential=False)
    scope["headers"].append(header())
    await harness.call(scope)
    assert harness.status == 200
    assert harness.inner_called


@pytest.mark.parametrize(
    "header",
    [
        pytest.param((b"authorization", b"Bearer"), id="scheme-with-no-token"),
        pytest.param((b"authorization", b"Bearer   "), id="blank-token"),
        pytest.param((b"authorization", b"Basic dXNlcjpwYXNz"), id="wrong-scheme"),
        pytest.param((b"cookie", b"other_cookie=session-token"), id="some-other-cookie"),
        pytest.param((b"x-api-key", b"alk_pat_notaheaderweread"), id="secret-in-an-unread-header"),
    ],
)
async def test_a_header_that_only_looks_like_a_credential_is_still_refused(
    header: tuple[bytes, bytes],
) -> None:
    """The negative twin: a precondition that admitted anything header-shaped
    would pass the case above while bounding nothing at all."""
    pattern = _uploads_pattern()
    harness = _Harness(body=b"x" * 4096)
    scope = _scope(
        _sample(pattern), _method(pattern), str(_ANON_PREFIX_MAX_BYTES * 2), credential=False
    )
    scope["headers"].append(header)
    await harness.call(scope)
    assert harness.status == 401
    assert not harness.inner_called
    assert harness.consumed == 0


@pytest.mark.parametrize("pattern", ABOVE_ALLOWANCE)
async def test_a_small_credential_less_request_still_reaches_the_route(
    pattern: GuardedPrefix,
) -> None:
    """The precondition bounds memory, it does not authenticate. A body at the
    anonymous allowance costs nothing to buffer, so it keeps the route's own
    answer -- which is how Files stays opaque (the same 404 everywhere) while it
    is disabled, instead of a 401 confirming the surface exists."""
    harness = _Harness()
    await harness.call(
        _scope(_sample(pattern), _method(pattern), str(_ANON_PREFIX_MAX_BYTES), credential=False)
    )
    assert harness.inner_called
    assert harness.status == 200


@pytest.mark.parametrize("pattern", WITHIN_ALLOWANCE)
async def test_a_small_cap_refuses_a_larger_credential_less_body_with_nothing_read(
    pattern: GuardedPrefix,
) -> None:
    """A pattern capped within the anonymous allowance bounds a credential-less
    body by its cap alone: one byte over is refused from the declared length,
    and a body at the cap still gets the route's own answer."""
    over = _Harness(body=b"x" * 4096)
    await over.call(
        _scope(_sample(pattern), _method(pattern), str(pattern.max_bytes + 1), credential=False)
    )
    assert (over.inner_called, over.consumed, over.status) == (False, 0, 413)
    at = _Harness()
    await at.call(
        _scope(_sample(pattern), _method(pattern), str(pattern.max_bytes), credential=False)
    )
    assert at.inner_called and at.status == 200


def test_the_chat_cap_covers_the_worst_case_serialization_of_a_ceiling_message() -> None:
    """The byte cap and the character ceiling are one decision in two units. A
    character costs at most 4 bytes as UTF-8 and at most 6 escaped as \\uXXXX, so
    a ceiling-length message can serialize to 6 bytes per character; a cap below
    that refuses, pre-buffer, exactly the message the schema accepts."""
    assert settings.chat_message_max_chars == MAX_MESSAGE_TEXT_LENGTH
    assert _CHAT_BODY_MAX_BYTES >= MAX_MESSAGE_TEXT_LENGTH * 6
    chat_patterns = [p for p in GUARDED_PREFIXES if p.prefix == "/api/v1/chats/"]
    assert {p.suffix for p in chat_patterns} == {"/messages", "/answer"}
    assert all(p.max_bytes == _CHAT_BODY_MAX_BYTES for p in chat_patterns)


def test_the_completion_part_list_is_bounded_at_the_deployments_part_ceiling() -> None:
    """The JSON tree a completion builds is bounded by the count, not only by the
    byte cap: `max_length` refuses the list before pydantic validates 10,001
    descriptors. Pinned against the setting the upload session itself enforces so
    a raised part ceiling cannot leave the schema behind."""
    assert MAX_COMPLETE_PARTS == settings.files_max_upload_parts


async def test_a_chat_message_body_a_paste_produces_is_admitted_when_authenticated() -> None:
    """The failure this pairing exists to prevent: the message ceiling was raised
    to a million characters so a pasted log fits, and a multi-MiB turn has to
    reach the route rather than dying at a body guard."""
    harness = _Harness(body=b"x" * 4096)
    await harness.call(_scope("/api/v1/chats/1f2e3d4c/messages", "POST", str(3 * 1024 * 1024)))
    assert harness.inner_called
    assert harness.status == 200


#: The extreme turn: an agent whose tool returned fifty megabytes, or a person
#: who pasted a core dump. Both chat doors are named because a cap on one of
#: them is not a cap on the other.
_EXTREME_CHAT_PATHS = [
    pytest.param("/api/v1/chats/1f2e3d4c/messages", id="messages"),
    pytest.param("/api/v1/chats/1f2e3d4c/answer", id="answer"),
]

#: Fifty megabytes, the size the extreme tool-result work measured end to end.
_FIFTY_MIB = 50 * 1024 * 1024


@pytest.mark.parametrize("path", _EXTREME_CHAT_PATHS)
@pytest.mark.parametrize(
    "credential",
    [pytest.param(True, id="authenticated"), pytest.param(False, id="anonymous")],
)
async def test_a_fifty_megabyte_chat_turn_never_reaches_the_route(
    path: str, credential: bool
) -> None:
    """The refusal costs one header read, not fifty megabytes of heap.

    Without the chat patterns in the table the 413 comes from the route's own
    ceiling -- which FastAPI reaches only AFTER Starlette has buffered the whole
    body and pydantic has built a second copy of it as a validated model. The
    credential-less caller is asserted alongside the signed-in one because this
    far over the cap the size decides first: an account-less request is refused
    as too large rather than as unauthenticated, so the refusal costs nothing
    and tells a prober nothing about the route.
    """
    harness = _Harness(body=b"x" * 4096)
    await harness.call(_scope(path, "POST", str(_FIFTY_MIB), credential=credential))

    assert not harness.inner_called
    assert harness.consumed == 0
    assert harness.status == 413


def test_the_upload_cap_admits_one_whole_part_of_the_size_the_app_publishes() -> None:
    """A part PUT is the one body here whose size the app itself decides.

    The app publishes `files_part_max_bytes` on `POST /uploads` and cuts every
    session into parts that big, so a guard below it would refuse, pre-buffer
    and unexplained, exactly the body the app just told the client to send. The
    cap carries headroom over it rather than equalling it, because what crosses
    the wire is the part plus its request line and headers.
    """
    uploads = next(p for p in GUARDED_PREFIXES if p.prefix == "/api/v1/files/uploads/")
    assert uploads.max_bytes > settings.files_part_max_bytes
    assert uploads.max_bytes - settings.files_part_max_bytes >= 4 * 1024 * 1024


#: One chunk of a streamed part body. The cases below drive a whole 128 MiB part
#: through the middleware; a single reused buffer keeps the test process at one
#: chunk of memory rather than the part it is pushing.
_CHUNK = b"x" * (1024 * 1024)

_PART_PATH = _SAMPLES["/api/v1/files/uploads/|/parts/|"]


class _ProductionHarness:
    """The middleware wired exactly as ``create_app`` wires it: the gate's own
    global cap, and the default in-flight budget.

    The harness above relaxes both knobs -- a global above every pattern cap and
    a 1 TiB budget -- so no case there can see an effective limit the running app
    imposes and the pattern does not. This one does. The inner app drains a
    chunked body without holding it, so ``consumed`` is proof the bytes really
    reached the app rather than a length the guard merely allowed.
    """

    def __init__(self, *, body_bytes: int = 0) -> None:
        self.inner_called = False
        self.consumed = 0
        self.sent: list[dict[str, Any]] = []
        self._remaining = body_bytes
        self.middleware = GateIngestBodyLimitMiddleware(
            self._inner, max_bytes=settings.gate_ingest_max_body_bytes
        )

    async def _inner(self, scope: Any, receive: Any, send: Any) -> None:
        self.inner_called = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.consumed += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def _receive(self) -> dict[str, Any]:
        chunk = min(len(_CHUNK), self._remaining)
        self._remaining -= chunk
        return {
            "type": "http.request",
            "body": _CHUNK[:chunk],
            "more_body": self._remaining > 0,
        }

    async def call(self, scope: dict[str, Any]) -> None:
        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        await self.middleware(scope, self._receive, send)

    @property
    def status(self) -> int:
        return int(self.sent[0]["status"])


def _uploads_pattern() -> GuardedPrefix:
    return next(p for p in GUARDED_PREFIXES if p.prefix == "/api/v1/files/uploads/")


async def test_a_whole_part_streams_through_the_middleware_the_app_builds() -> None:
    """The request the product actually makes: the gate's 33 MiB global, the
    default in-flight budget, and a part of exactly the size `POST /uploads`
    publishes and the part route enforces.

    Every file above `files_single_put_max_bytes` is this request repeated, so a
    guard that refuses it refuses the entire published ceiling -- pre-buffer, and
    with a message the upload route never gets to author. Both bounds are on
    trial here: the per-request ceiling the pattern names, and the byte budget
    that decides whether one whole part may be in flight at all.
    """
    part = settings.files_part_max_bytes
    harness = _ProductionHarness(body_bytes=part)
    await harness.call(_scope(_PART_PATH, "PUT", str(part)))
    assert harness.status == 200
    assert harness.inner_called
    assert harness.consumed == part


async def test_one_mib_past_the_upload_pattern_cap_is_still_refused() -> None:
    """Lifting the global off the pattern leaves the PATTERN as the ceiling, not
    no ceiling: a body past it is a 413 decided from the declared length alone."""
    harness = _ProductionHarness()
    await harness.call(_scope(_PART_PATH, "PUT", str(_uploads_pattern().max_bytes + 1024 * 1024)))
    assert harness.status == 413
    assert not harness.inner_called
    assert harness.consumed == 0


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(settings.gate_ingest_max_body_bytes + 1, id="one-past-the-global"),
        pytest.param(_uploads_pattern().max_bytes, id="at-the-upload-pattern-cap"),
    ],
)
async def test_the_gate_global_still_bounds_an_exact_guarded_path(declared: int) -> None:
    """The prefix caps are independent of the global, not a replacement for it.
    A gate artifact is a JSON document FastAPI materializes whole before any
    dependency runs, and `gate_ingest_max_body_bytes` is the only thing bounding
    it -- an upload-sized body on a gate path stays a 413."""
    harness = _ProductionHarness()
    await harness.call(_scope("/api/v1/gate/reports", "POST", str(declared)))
    assert harness.status == 413
    assert not harness.inner_called
