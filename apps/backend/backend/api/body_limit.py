"""Pre-buffer body-size guard for the WAF-exempt upload endpoints.

The edge WAF exempts GUARDED_PATHS from its 8 KB body rule: the gate uploads
are far larger by design, the lookup bodies cross 8 KB at their contract
maxima, and a KB promote carries unbounded user prose. An extension adds its
own signed paths (a provider whose interaction carries a whole message back)
as :class:`~backend.api.extension_points.SignedBody` declarations on its
router, and the app factory hands them to this guard. That leaves these the
only unauthenticated-reachable routes with no upstream body bound, and
FastAPI buffers the ENTIRE body into memory before the auth dependency runs
(request.json() precedes solve_dependencies) and long before gate_service's
32 MiB artifact ceiling. This middleware restores a bound for free: it
decides from the declared Content-Length alone and never reads the body.

A size that cannot be trusted is a 411: a missing Content-Length (the CLI
always sends one, and unbounded chunked bodies would defeat the guard), an
unparsable one, or ANY Transfer-Encoding header
(:func:`_has_transfer_encoding`). The guard bounds EVERY method on a guarded
path -- the edge exemption is method-agnostic and FastAPI hands a body to
any method (`Body(...)` on a GET is legal), the hole a method allowlist
left; the CORS middleware outside this one answers preflight OPTIONS itself.

Three further bounds, all decided from the SCOPE alone (still no body read):

1. A CREDENTIAL PRECONDITION. Because the parse precedes the auth dependency,
   an anonymous caller could make the process materialize a max-size document
   and only then earn its 401 -- a memory-exhaustion primitive that needs no
   account. The guard answers that 401 itself, from the header shape alone
   (:data:`_CI_TOKEN_PATHS` want a ``Bearer alk_ci_…``; the rest want any
   session credential). It is a PRECONDITION, never the authorization: the real
   dependency still runs and still decides. Same envelope + ``WWW-Authenticate``
   the dependency would have sent, so a client cannot tell the two apart. A
   :data:`GUARDED_PREFIXES` match earns the same precondition once its declared
   body passes :data:`_ANON_PREFIX_MAX_BYTES`.
2. A PER-PATH CEILING. ``max_bytes`` sizes the gate's artifact uploads; a KB
   promote is prose and gets :data:`_PATH_MAX_BYTES`'s far smaller cap, which
   also bounds the row it becomes. :data:`GUARDED_PREFIXES` is a different
   class of body and carries its OWN caps, which ``max_bytes`` does not bound.
3. TWO IN-FLIGHT BUDGETS, ONE PER KIND OF BODY. The per-request cap says
   nothing about how many requests are in flight, so N concurrent max-size
   bodies is an OOM regardless of the per-request bound. Over either budget is
   a 503 + ``Retry-After``; both govern ONLY bodies larger than
   :data:`_BUDGET_EXEMPT_MAX_BYTES` -- a body at or under that is a lookup or a
   promote, a different class entirely, and is never charged or shed.

   * A MATERIALISED body -- the gate artifacts, a JSON completion, a content
     PUT, a bulk batch -- is buffered whole by FastAPI, and a JSON parse costs
     several times the wire size. It is charged by the bytes that have ARRIVED,
     not the Content-Length that announced them (:class:`_InflightMeter`): a
     body that was declared and never sent occupies no memory, so it must buy
     no reservation -- otherwise two sockets that announce a max-size upload
     and then stall would hold the whole budget for free and shed every real
     upload behind them.
   * A STREAMING body -- the proxied upload part (``GuardedPrefix.streams``) --
     is never materialised: the route hands each wire chunk to the store and
     releases it before it reads the next, so what a part costs the process is
     the store driver's staging buffer, ``FILES_UPLOAD_PART_RESIDENT_BYTES``,
     whatever its Content-Length. Charging its wire bytes bounded the wrong
     thing: 272 MiB admitted two 128 MiB parts per process for the whole fleet
     and shed the third person mid-body. A part is admitted by RESIDENT memory
     instead (:class:`PartAdmission`): a slot of that fixed cost, taken when
     its first body byte arrives -- which the route only reads once the caller
     and the session are authorized, so an anonymous or silent socket takes
     nothing -- and given back when the request unwinds. The slots are shared
     fairly in two tiers, the org first and its people inside it: an org may
     hold at most ``capacity / active orgs`` of them while other orgs are
     asking, and one of its people at most ``that / its active people``. Alone,
     each has the whole budget. The person's share rides every part response
     and every shed as ``X-Upload-Concurrency`` so a client can keep that many
     parts in flight and no more.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alkera_core.auth import COOKIE_NAME
from alkera_core.auth.ci_token import looks_like_ci_token
from alkera_core.config import FILES_UPLOAD_PART_RESIDENT_BYTES, settings
from alkera_core.notebooks import limits as nb_limits
from alkera_core.notebooks.schemas import MAX_REBASE_UPDATE_CHARS
from alkera_core.observability.context import get_trace_id
from alkera_core.observability.envelope import build_error_body
from alkera_core.observability.errors import ErrorCode
from alkera_core.schemas.files.lease_tree import TREE_MAX_BODY_BYTES
from starlette.requests import HTTPConnection

from backend.api.extension_points import SignedBody
from backend.api.rate_limit import org_keys, principal_keys
from backend.api.route_path import routed_path

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The exact request paths the WAF body exemption covers -- the ONLY copy of
#: the list. `make gen-waf-paths` emits it to the JSON a hosted deployment's
#: edge reads for its WAF body exemption, so a path exempted at the edge is a
#: path this middleware bounds, by construction; a path added on one side only would
#: otherwise fail open (exempt at the edge, unbounded in the app). Matched against
#: the ROUTE path (scope["path"] with root_path stripped), which is exactly what
#: the router routes on: under a `--root-path /prefix` deployment the raw
#: scope["path"] is "/prefix/api/v1/gate/reports" while the router (and the WAF's
#: canonical URI) see "/api/v1/gate/reports", so comparing the raw path would
#: silently unbind the guard. The WAF matches the raw URI path EXACTLY, and a raw
#: path carrying no percent-escapes decodes to itself, so no request can be
#: exempt at the edge yet unguarded here.
GUARDED_PATHS = frozenset(
    {
        "/api/v1/gate/reports",
        # NOT /api/v1/gate/reports/lookup: its request is nonce-based (repo +
        # sha + run_nonce, under 1 KB), so it needs no exemption -- the edge's
        # 8 KB rule covers it.
        "/api/v1/gate/snapshots",
        "/api/v1/gate/snapshots/lookup",
        "/api/v1/gate/waivers/lookup",
        "/api/v1/gate/receipts/lookup",
        "/api/v1/kb/promote",
        # A chart to export carries its rows inline, so any real chart crosses
        # the edge's 8 KB rule. Session or machine credential, like the promote.
        "/api/v1/charts/export",
    }
)


def edge_exempt_paths(signed_bodies: Iterable[SignedBody]) -> frozenset[str]:
    """The exact paths the edge exempts from its body rule: :data:`GUARDED_PATHS`
    and every signed path an installed extension declares. What the WAF
    exemption list is generated from, and what the guard bounds."""
    return GUARDED_PATHS | frozenset(body.path for body in signed_bodies)


@dataclass(frozen=True, slots=True)
class GuardedPrefix:
    """One WAF body exemption that no exact path can express.

    Every Files upload route carries an id in its path (``/uploads/{id}/parts/{n}``,
    ``/drives/{d}/items/{i}/content``), so the edge's exact-match exemption can
    never name it. The edge instead matches ``STARTS_WITH prefix`` AND
    ``ENDS_WITH suffix`` AND ``method`` -- and this triple is the app-side twin
    of that rule, carrying the cap the edge does not impose.

    ``suffix`` is ``""`` when the pattern is the prefix alone; ``contains`` is a
    path segment that must appear after the prefix, for the one route whose
    variable part is LAST (``…/parts/{n}``) and so has no suffix to anchor on.
    A pattern needs at least one of the two: prefix alone is how the streaming
    part PUT's 136 MiB cap once reached every JSON route under
    ``/api/v1/files/uploads/``, including the ``/complete`` body FastAPI parses
    into an object tree.

    ``methods`` describes the EDGE rule only. The app-side bound
    (:meth:`matches`) ignores the method for the same reason the exact-path
    guard bounds every method: FastAPI hands a body to any method, so a GET
    with a 200 MiB body on ``…/content`` would otherwise be unbounded here.
    Bounding more than the edge exempts is always safe; the converse is the
    hole.

    ``streams`` says which in-flight budget the body is admitted by: a
    streaming route forwards each chunk to the store and holds a fixed
    staging buffer (:class:`PartAdmission`); everything else is buffered whole
    and charged by arrived bytes (:class:`_InflightMeter`). It is a property of
    the ROUTE's code, not of the request, so it is declared here beside the
    cap and pinned by a test against ``put_part``'s streaming read.
    """

    prefix: str
    methods: frozenset[str]
    suffix: str
    max_bytes: int
    contains: str = ""
    streams: bool = False

    def matches(self, route_path: str) -> bool:
        if not route_path.startswith(self.prefix) or not route_path.endswith(self.suffix):
            return False
        # Searched in the part AFTER the prefix, so a segment that happens to
        # spell the prefix's own tail can never satisfy it.
        return self.contains in route_path[len(self.prefix) :]


_MIB = 1024 * 1024

#: The prefix+method+suffix body exemptions, the ONLY copy of the list, emitted
#: alongside :data:`GUARDED_PATHS` by `make gen-waf-paths` into the edge's JSON.
#: Same construction, same
#: guarantee: a pattern exempted at the edge is a pattern bounded here.
#:
#: These caps are the EFFECTIVE limit: ``max_bytes`` sizes the gate's JSON
#: artifacts and never clamps one of them. Two unrelated classes of body share
#: this middleware, and taking the smaller of the two would silently bound a
#: 128 MiB part by a 33 MiB gate ceiling -- refusing, pre-buffer and unexplained,
#: exactly the part the upload route just told the client to send.
#:
#: The caps are per pattern because the bodies are different in kind. A proxied
#: upload part is capped at the part size the deployment publishes and enforces
#: (``files_part_max_bytes``, 128 MiB) plus 8 MiB of envelope headroom, so a
#: part that the upload route would accept is never refused here first; a
#: single-call content PUT and a bulk multipart batch may carry a whole small
#: file set (64 MiB); a tree request, a snapshot request and a lease release are
#: JSON descriptions of work, not bytes, and get the small cap (8 MiB) that
#: bounds the rows they become.
#:
#: The upload cap is a LITERAL, not ``settings.files_part_max_bytes``, because
#: this table is the source `make gen-waf-paths` emits into the WAF's
#: exempt-path JSON: a number read from the environment would make the
#: generated artifact depend on whose shell ran it. It is pinned against the
#: setting by a test instead, and it moves with everything else the part size
#: is coupled to — see ``files_part_max_bytes`` in ``alkera_core.config``.
#:
#: These carry the same pre-auth credential precondition as :data:`GUARDED_PATHS`,
#: but only above :data:`_ANON_PREFIX_MAX_BYTES`. Below it a credential-less
#: request still reaches the route, so Files keeps answering the same opaque 404
#: everywhere while ``files_enabled`` is off (one answer for "not yours") for
#: every probe an existence test would actually send; above it the body is large
#: enough that buffering it to earn a 401 is itself the attack, and a prober who
#: has to send
#: megabytes to learn the surface exists has learned nothing cheaper than a
#: timing test already gives them. So what bounds the memory an UNAUTHENTICATED
#: PUT can cost:
#:
#:   * per connection, the cap above -- a 137 MiB declaration is refused from the
#:     Content-Length, before a byte is read -- and, past 1 MiB, a missing
#:     credential shape is refused there too;
#:   * across connections, the resident budget (:class:`PartAdmission`): a part
#:     takes a slot of ``FILES_UPLOAD_PART_RESIDENT_BYTES`` when its first byte
#:     arrives, so an announced body that never comes reserves nothing and can
#:     starve nobody, and the process holds at most ``capacity`` staging buffers
#:     however many parts are declared against it;
#:   * and the route itself never materializes an admitted part: `put_part`
#:     resolves the caller and the upload session BEFORE it touches
#:     `request.stream()`, so an anonymous PUT is a 401/404 that costs one
#:     chunk, not a part -- and, because the slot rides the first byte the ROUTE
#:     reads, not a slot either.
#: The largest JSON body a chat turn can be, derived from the character ceiling
#: the message schema enforces (``MAX_MESSAGE_TEXT_LENGTH`` /
#: ``settings.chat_message_max_chars``, 1,000,000 characters so a pasted log or
#: schema fits). A character costs at most 4 bytes as UTF-8 and at most 6 as a
#: ``\uXXXX`` escape, so the worst-case serialization of a ceiling-length message
#: is 6,000,000 bytes; 8 MiB carries that plus the rest of the envelope. Without
#: an entry here the edge answers first and opaquely: the WAF re-blocks the CRS
#: 8 KB body label off the exempt list, so every message past 8 KB was a 403 and
#: the typed text was lost. The two are pinned together by a test -- raising the
#: character ceiling without raising this cap re-creates that failure one layer
#: in.
_CHAT_BODY_MAX_BYTES = 8 * _MIB

# The holder's lease bodies. Each is a list a box fills at its cadence, and each
# crosses the edge's 8 KB body rule well inside its contract: a live batch at
# about 70 entries, a batched beat at about 70 chats, a digest walk at a few
# hundred folders. Off the exempt list the edge 403s them opaquely and the box
# resends the same body forever. Each cap is the largest body the route's own
# bounds admit, worked out below from those bounds, so the edge exemption lifts
# nothing the route would not take anyway.
#
# The arithmetic sizes the serialization a standard JSON encoder can produce:
# ``", "`` and ``": "`` separators (Python's defaults, the wider of the two
# spellings in use), fixed ASCII tokens (keys, UUIDs, digits, state names) at one
# byte a character, and free text at the most any encoder spends on one code
# point -- a character outside the BMP escaped as a surrogate pair,
# ``\uXXXX\uXXXX``, twelve bytes. An integer is at most a Postgres ``bigint``,
# nineteen digits. The route-side bounds restated here as literals (so the
# generated WAF artifact never depends on whose environment ran the generator)
# are pinned against their sources by
# ``apps/backend/tests/files/test_files_body_limit_prefixes.py``.
_JSON_MAX_BYTES_PER_TEXT_CHAR = 12
_BIGINT_DIGITS = 19
_UUID_CHARS = 36


def _json_string(chars: int, *, free_text: bool = False) -> int:
    """Bytes of a JSON string of ``chars`` characters, quotes included."""
    return 2 + chars * (_JSON_MAX_BYTES_PER_TEXT_CHAR if free_text else 1)


def _json_object(members: dict[str, int]) -> int:
    """Bytes of a JSON object whose values serialize to at most ``members[key]``
    bytes each: braces, each quoted key and its ``": "``, ``", "`` between."""
    body = sum(len(key) + 2 + 2 + size for key, size in members.items())
    return 2 + body + 2 * max(len(members) - 1, 0)


def _json_array(count: int, item: int) -> int:
    """Bytes of a JSON array of ``count`` items of at most ``item`` bytes each."""
    return 2 + count * item + 2 * max(count - 1, 0)


def _gzip_bound(decoded: int) -> int:
    """The most bytes gzip can spend on ``decoded`` bytes: zlib's
    ``compressBound`` (stored blocks for incompressible input, plus its own
    wrapper) and the 18 bytes of gzip framing."""
    return decoded + (decoded >> 12) + (decoded >> 14) + (decoded >> 25) + 13 + 18


#: ``HeartbeatBatchBody``: at most ``HEARTBEAT_BATCH_MAX`` (1,000) entries, each a
#: node UUID, a ``bigint`` epoch, an instance id no longer than the 128-character
#: ``file_leases.holder_instance_id`` it must equal, and a boolean.
#: 1,651 bytes an entry, 1,653,012 for the batch.
_HEARTBEAT_BATCH_ENTRIES = 1_000
_LEASE_INSTANCE_ID_CHARS = 128
_HEARTBEAT_BATCH_MAX_BYTES = _json_object(
    {
        "leases": _json_array(
            _HEARTBEAT_BATCH_ENTRIES,
            _json_object(
                {
                    "nodeId": _json_string(_UUID_CHARS),
                    "epoch": _BIGINT_DIGITS,
                    "instanceId": _json_string(_LEASE_INSTANCE_ID_CHARS, free_text=True),
                    "synced": len("false"),
                }
            ),
        )
    }
)

#: ``LiveBatchBody``: at most ``files_live_max_batch_entries`` (256) entries, each
#: a node UUID, a state (the longest, ``inbound_delete`` / ``inbound_rename``, is 14
#: characters), a ``bigint`` size, an ISO-8601 mtime (32 characters with
#: microseconds and an offset; 64 allowed for the other spellings the parser
#: takes), and a ``displaced`` name of at most 1,024 characters of free text.
#: 12,494 bytes an entry, 3,198,989 for the batch. Nearly all of it is the
#: ``displaced`` allowance: a real batch is about 115 bytes an entry.
_LIVE_BATCH_ENTRIES = 256
_LIVE_STATE_CHARS = 14
_LIVE_MTIME_CHARS = 64
_LIVE_DISPLACED_CHARS = 1_024
_LIVE_BATCH_MAX_BYTES = _json_object(
    {
        "entries": _json_array(
            _LIVE_BATCH_ENTRIES,
            _json_object(
                {
                    "nodeId": _json_string(_UUID_CHARS),
                    "state": _json_string(_LIVE_STATE_CHARS),
                    "boxSize": _BIGINT_DIGITS,
                    "boxMtime": _json_string(_LIVE_MTIME_CHARS),
                    "displaced": _json_string(_LIVE_DISPLACED_CHARS, free_text=True),
                }
            ),
        )
    }
)

#: ``DigestRequest``: up to 4,000 paths and 1,000 names, but the route holds the
#: DECODED body to ``TREE_MAX_BODY_BYTES`` (4 MiB) before it parses, gzip or not,
#: so that is the bound -- and on the wire a gzipped body may run past it by
#: gzip's worst-case expansion. 4,195,615 bytes.
_DIGEST_MAX_BYTES = _gzip_bound(TREE_MAX_BODY_BYTES)

#: The notebook routes a box, an agent or a person sends more than 8 KB to.
#: Every cap comes from the notebooks' one budget per hop
#: (``alkera_core.notebooks.limits``): the box's event post is one event at
#: its most plus a post's worth, so the edge takes exactly what the box sends
#: and the outbox and a reader's frame take what the edge let through.
#: ``/rebase`` carries a document update of at most ``MAX_REBASE_UPDATE_CHARS``
#: characters, each at most six bytes as JSON (a ``\uXXXX`` escape the parser
#: decodes before the schema counts it).
_NOTEBOOKS = "/api/v1/notebooks/"
_NOTEBOOK_PREFIXES: tuple[GuardedPrefix, ...] = (
    GuardedPrefix(_NOTEBOOKS, frozenset({"POST"}), "/events", nb_limits.POST_BODY_MAX_BYTES),
    GuardedPrefix(_NOTEBOOKS, frozenset({"POST"}), "/ops", nb_limits.OPS_BODY_MAX_BYTES),
    GuardedPrefix(_NOTEBOOKS, frozenset({"POST"}), "/comm", nb_limits.COMM_BODY_MAX_BYTES),
    GuardedPrefix(
        _NOTEBOOKS,
        frozenset({"POST"}),
        "/rebase",
        6 * MAX_REBASE_UPDATE_CHARS + nb_limits.REQUEST_BODY_MAX_BYTES,
    ),
    GuardedPrefix(_NOTEBOOKS, frozenset({"POST"}), "/runs", nb_limits.REQUEST_BODY_MAX_BYTES),
    GuardedPrefix(_NOTEBOOKS, frozenset({"POST"}), "/frames", nb_limits.REQUEST_BODY_MAX_BYTES),
    GuardedPrefix(
        _NOTEBOOKS, frozenset({"POST"}), "/outputs/clear", nb_limits.REQUEST_BODY_MAX_BYTES
    ),
    GuardedPrefix(
        _NOTEBOOKS,
        frozenset({"POST"}),
        "",
        nb_limits.REQUEST_BODY_MAX_BYTES,
        contains="/env/",
    ),
)

GUARDED_PREFIXES: tuple[GuardedPrefix, ...] = (
    GuardedPrefix(
        prefix="/api/v1/files/uploads/",
        methods=frozenset({"PUT"}),
        suffix="",
        contains="/parts/",
        max_bytes=136 * _MIB,
        streams=True,
    ),
    # The multipart completion: a JSON list of part descriptors FastAPI buffers
    # AND parses into an object tree before any dependency runs, so it is bounded
    # like the other JSON request bodies rather than like the part stream it
    # finishes. `CompleteUploadRequest.parts` bounds the tree itself at
    # `files_max_upload_parts` entries.
    GuardedPrefix(
        prefix="/api/v1/files/uploads/",
        methods=frozenset({"POST"}),
        suffix="/complete",
        max_bytes=8 * _MIB,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"PUT"}),
        suffix="/content",
        max_bytes=64 * _MIB,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/bulk",
        max_bytes=64 * _MIB,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/tree",
        max_bytes=8 * _MIB,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/snapshots",
        max_bytes=8 * _MIB,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/lease/release",
        max_bytes=8 * _MIB,
    ),
    # The holder's lease plane, each capped at its route's largest valid body
    # (see the arithmetic above the table). The digest walk ends in
    # `/tree/digests`, so the `/tree` pattern above never reaches it.
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/lease/live",
        max_bytes=_LIVE_BATCH_MAX_BYTES,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/lease/tree/digests",
        max_bytes=_DIGEST_MAX_BYTES,
    ),
    GuardedPrefix(
        prefix="/api/v1/files/drives/",
        methods=frozenset({"POST"}),
        suffix="/leases/heartbeat",
        max_bytes=_HEARTBEAT_BATCH_MAX_BYTES,
    ),
    # A typed chat turn and the answer to an agent's question. Both carry user
    # prose up to the message character ceiling, so both need the edge exemption
    # and the cap derived from it.
    GuardedPrefix(
        prefix="/api/v1/chats/",
        methods=frozenset({"POST"}),
        suffix="/messages",
        max_bytes=_CHAT_BODY_MAX_BYTES,
    ),
    GuardedPrefix(
        prefix="/api/v1/chats/",
        methods=frozenset({"POST"}),
        suffix="/answer",
        max_bytes=_CHAT_BODY_MAX_BYTES,
    ),
    *_NOTEBOOK_PREFIXES,
)


#: The guarded paths authenticated by a CI token (``Authorization: Bearer
#: alk_ci_…``, resolved by ``require_ci_token``). The remaining guarded paths are
#: ordinary user surfaces (cookie or Bearer session). The split only decides
#: which credential SHAPE the pre-auth precondition looks for; both still go on
#: to the real dependency. A guarded path missing from here is treated as the
#: weaker (any-credential) case, so adding one can never lock a route out.
_CI_TOKEN_PATHS = frozenset(
    {
        "/api/v1/gate/reports",
        "/api/v1/gate/snapshots",
        "/api/v1/gate/snapshots/lookup",
        "/api/v1/gate/waivers/lookup",
        "/api/v1/gate/receipts/lookup",
    }
)

#: Per-path ceilings that override the gate-sized default. A KB promote is human
#: prose, not a build artifact: it needs the WAF exemption (it crosses 8 KB) but
#: nothing like the artifact ceiling, and the value it carries is INSERTed
#: verbatim into a shared Postgres, so the cap doubles as the row bound the
#: schema does not yet impose.
_KB_PROMOTE_MAX_BYTES = 1024 * 1024
#: A chart export's ceiling. Its rows ride inline, so it crosses 8 KB, but it
#: stays a sub-budget body like the promote above: a chart past 1 MiB of rows
#: (tens of thousands of small rows) is aggregated or sampled by its writer
#: first, which the client does by default at 5,000 rows.
_CHART_EXPORT_MAX_BYTES = 1024 * 1024
_PATH_MAX_BYTES: dict[str, int] = {
    "/api/v1/kb/promote": _KB_PROMOTE_MAX_BYTES,
    "/api/v1/charts/export": _CHART_EXPORT_MAX_BYTES,
}

#: The largest prefix-matched body an anonymous caller may make this process
#: buffer. Every route :data:`GUARDED_PREFIXES` covers requires authentication,
#: so nothing legitimate is refused -- but FastAPI materializes (and, for the
#: JSON ones, parses) the whole body before the auth dependency runs, so without
#: this an account-less request could pin the pattern's whole cap per connection.
#: Small requests stay unchallenged so an existence probe still gets the route's
#: own answer rather than a 401 that confirms the surface.
_ANON_PREFIX_MAX_BYTES = 1024 * 1024

#: In-flight pre-auth bytes admitted per process, as a multiple of the per-request
#: ceiling. Small enough that the worst-case set of concurrent max-size parses
#: stays inside the container's memory, large enough that ordinary CI concurrency
#: (lookups are kilobytes) never notices.
_DEFAULT_INFLIGHT_BUDGET_MULTIPLIER = 2

#: The largest MATERIALISED body any pattern here admits -- a content PUT or a
#: bulk batch -- and so the largest single body the arrived-bytes budget ever
#: has to hold. The default budget is sized off this rather than off
#: ``max_bytes`` alone: a budget below it would shed 503 every request the
#: per-request cap had just allowed, which is the same outage as the cap being
#: too low, one layer further in. The streaming part is deliberately NOT a
#: candidate: it spends the resident budget, and sizing this one off its
#: 136 MiB cap is what made the process hold 272 MiB of wire bytes for two
#: uploads and refuse the third.
_MAX_MATERIALISED_PREFIX_BYTES = max(p.max_bytes for p in GUARDED_PREFIXES if not p.streams)

#: How long a principal that asked for a part keeps counting as active for the
#: fair share after its last request. Long enough to span the gap between one
#: part's answer and the next part's first byte on a slow link (a client backs
#: off up to a few seconds on a shed), short enough that a person who closed
#: the tab stops halving everyone else's share within the same minute.
_FAIR_SHARE_IDLE_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Uploader:
    """Who a part's slot is counted against: the tenant, and the person inside
    it. Both are the rate limiter's own keys, read from the request headers
    alone -- the body is never touched to find them.

    Two tiers on purpose. A budget divided per PERSON is not divided per
    customer: an org with fifty people streaming takes fifty shares while the
    org next door takes one, which is the starvation a fair share exists to
    prevent, one level up. A caller with no session -- a CI or personal-access
    token, an anonymous socket -- is a tenant of one, because nothing this
    early in the request says which org it belongs to.
    """

    tenant: str
    principal: str


class PartAdmission:
    """The resident-memory budget for streaming upload parts, as slots.

    Capacity is the budget over the fixed cost of one in-flight part, read
    live so a deployment tunes it by setting and a test pins it by monkeypatch.
    A slot is taken by :meth:`acquire` when a part's first body byte arrives
    and given back by :meth:`release` when its request unwinds -- never at the
    announcement, so a socket that declares a part and sends nothing holds
    nothing (the route reads the body only once the caller is authorized, so
    neither does an anonymous one).

    The share has two tiers, each ``capacity // active`` with a floor of one:
    a tenant may hold ``capacity // active tenants`` between all its people,
    and one of those people ``that // its tenant's active people``. Active
    means asked within :data:`_FAIR_SHARE_IDLE_SECONDS`, the asker included.
    Alone, a tenant has the whole budget and its one person has all of the
    tenant's; the moment a second tenant asks, each is held to half however
    many people it brought, and whoever is already over a new share is refused
    its next part until it drains -- which is what hands the freed slots to the
    one waiting. Nobody is ever told a share of zero: past the point where the
    slots divide, every share is one and the wait is for a slot rather than for
    a share.

    Every method runs to completion without an await, so the counters cannot
    interleave under asyncio.
    """

    def __init__(
        self,
        *,
        budget_bytes: Callable[[], int],
        part_bytes: int = FILES_UPLOAD_PART_RESIDENT_BYTES,
        idle_seconds: float = _FAIR_SHARE_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget_bytes = budget_bytes
        self.part_bytes = part_bytes
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._held: dict[str, int] = {}
        #: Slots held right now per tenant -- the live picture of the fair
        #: share, beside :attr:`total` and :attr:`peak`.
        self.held_by_tenant: dict[str, int] = {}
        self._seen: dict[str, float] = {}
        #: The tenant each active principal last asked as, so the active people
        #: of one tenant can be counted without a second table of memberships.
        self._tenant_of: dict[str, str] = {}
        self.total = 0
        #: The most slots ever held at once, for a load test to read back.
        self.peak = 0

    @property
    def capacity(self) -> int:
        return max(0, int(self._budget_bytes()) // self.part_bytes)

    def tenant_share(self, tenant: str) -> int:
        """How many parts everyone in ``tenant`` may hold between them."""
        active = len(set(self._tenant_of.values()) | {tenant})
        return max(1, self.capacity // active)

    def share(self, who: Uploader) -> int:
        """How many parts ``who`` may hold right now: the person's tier, and so
        the number their client is told to keep in flight."""
        self._touch(who)
        people = sum(1 for tenant in self._tenant_of.values() if tenant == who.tenant)
        return max(1, self.tenant_share(who.tenant) // max(1, people))

    def would_admit(self, who: Uploader) -> bool:
        """Whether a part from ``who`` could take a slot at this instant.
        Admission only -- nothing is reserved -- so the common overload is a
        clean 503 before the app runs, and a request that is admitted here but
        finds the slots gone by its first byte is shed there instead.

        Both tiers bind. The tenant's own ceiling is not implied by its people's
        shares: those floor at one, so an org that brings more people than it
        has slots would otherwise hold a slot each and take back exactly what
        the tenant tier was there to cap."""
        if self.total >= self.capacity:
            return False
        share = self.share(who)
        if self.held_by_tenant.get(who.tenant, 0) >= self.tenant_share(who.tenant):
            return False
        return self._held.get(who.principal, 0) < share

    def acquire(self, who: Uploader) -> bool:
        if not self.would_admit(who):
            return False
        self._held[who.principal] = self._held.get(who.principal, 0) + 1
        self.held_by_tenant[who.tenant] = self.held_by_tenant.get(who.tenant, 0) + 1
        self.total += 1
        self.peak = max(self.peak, self.total)
        return True

    def release(self, who: Uploader) -> None:
        _give_back(self._held, who.principal)
        _give_back(self.held_by_tenant, who.tenant)
        self.total -= 1

    def _touch(self, who: Uploader) -> None:
        now = self._clock()
        self._seen[who.principal] = now
        self._tenant_of[who.principal] = who.tenant
        horizon = now - self.idle_seconds
        # A principal that stopped asking and holds nothing stops counting; one
        # still holding a slot is active whatever the clock says. A tenant stops
        # counting when its last person does.
        for other, last in list(self._seen.items()):
            if last < horizon and other not in self._held:
                del self._seen[other]
                self._tenant_of.pop(other, None)


def _give_back(counts: dict[str, int], key: str) -> None:
    """One slot back, and the key itself once it holds none -- so a table of
    counters cannot grow with every principal a process has ever served."""
    left = counts.get(key, 0) - 1
    if left > 0:
        counts[key] = left
    else:
        counts.pop(key, None)


#: The largest declared body the in-flight budget ignores entirely: neither
#: charged against it nor ever shed by it.
#:
#: The budget exists for the artifact uploads. Every other guarded body is a
#: different class -- the lookups' contract maxima (500 waiver candidates, 1000
#: receipt keys) and the KB promote's own ceiling all sit at or under this value
#: -- and a single global counter that admitted on TOTAL bytes in flight would
#: shed those too: two max-size uploads fill the budget, and the kilobyte lookup
#: alongside them gets a 503 for a request that cost nothing to serve. That is not
#: a slow retry, it is a failed CI step: the CLI retries only its report upload,
#: while `publish_snapshot` and every lookup raise on the first 5xx.
#:
#: What the exemption gives up: a sub-threshold body is bounded per request but
#: not in aggregate. That is the same bound every non-guarded route in the app
#: has, on a body an order of magnitude under an upload.
_BUDGET_EXEMPT_MAX_BYTES = 1024 * 1024


class GateIngestBodyLimitMiddleware:
    """Reject a guarded request whose declared body size exceeds its ceiling
    (413), whose size cannot be trusted -- no Content-Length, or any
    Transfer-Encoding header -- (411), that carries no credential of the shape
    the path's auth requires (401), or that is an UPLOAD the process has no
    in-flight byte budget for (503). Every rejection above is decided from the
    ASGI scope alone, before a body byte is read; only the budget's own
    last-resort shed (:class:`_InflightMeter`) looks at arriving bytes, and it
    counts them without ever handing them up. Bounds every method on a guarded
    path; everything else passes through untouched."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        max_inflight_bytes: int | None = None,
        budget_exempt_max_bytes: int = _BUDGET_EXEMPT_MAX_BYTES,
        resident_budget_bytes: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        signed_bodies: Iterable[SignedBody] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        #: The extension paths whose bodies are signed, by path.
        self.signed = {body.path: body for body in signed_bodies}
        #: Every exact path this guard bounds: the open ones and the signed ones.
        self.guarded_paths = edge_exempt_paths(self.signed.values())
        self.max_inflight_bytes = (
            max(max_bytes, _MAX_MATERIALISED_PREFIX_BYTES) * _DEFAULT_INFLIGHT_BUDGET_MULTIPLIER
            if max_inflight_bytes is None
            else max_inflight_bytes
        )
        self.budget_exempt_max_bytes = budget_exempt_max_bytes
        #: The streaming parts' budget, in slots of one part's resident cost.
        self.parts = PartAdmission(
            budget_bytes=resident_budget_bytes
            or (lambda: settings.files_upload_resident_budget_bytes),
            clock=clock,
        )
        # Bytes of guarded request body currently materialized in this process.
        # A plain counter, not a semaphore: every read-modify-write below is a
        # single statement with no await inside it, so the event loop cannot
        # interleave another request into the gap.
        self._inflight_bytes = 0

    def _limit_for(self, guarded_path: str) -> int:
        signed = self.signed.get(guarded_path)
        if signed is not None:
            return min(self.max_bytes, signed.max_bytes)
        return min(self.max_bytes, _PATH_MAX_BYTES.get(guarded_path, self.max_bytes))

    def _has_expected_credential(self, scope: Scope, guarded_path: str | None) -> bool:
        signed = self.signed.get(guarded_path) if guarded_path is not None else None
        if signed is not None:
            return all(
                _header(scope, name.encode()) is not None for name in signed.signature_headers
            )
        return _has_expected_credential(scope, guarded_path)

    def _has_room_for(self, declared: int) -> bool:
        """Whether a body of ``declared`` bytes could still fit alongside what is
        already in flight. Admission only: it turns the common overload into a
        clean 503 before the app runs, and because the counter holds ARRIVED
        bytes, a request that announced a body it never sends counts for nothing
        here and so can never shed anybody else."""
        return self._inflight_bytes + declared <= self.max_inflight_bytes

    def _charge(self, nbytes: int) -> bool:
        """Add just-arrived bytes to the budget; False once they carry the
        process past it."""
        self._inflight_bytes += nbytes
        return self._inflight_bytes <= self.max_inflight_bytes

    def _release(self, nbytes: int) -> None:
        self._inflight_bytes -= nbytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Strip root_path exactly as Starlette's router does, so the guard and the
        # route agree on the path under a `--root-path` deployment.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Match a trailing-slash spelling too: a route explicitly registered at
        # `/lookup/` serves directly (no slash-redirect), so `/lookup` and `/lookup/`
        # must both be bounded. Normalize at most ONE trailing slash -- `rstrip("/")`
        # would also bound `/lookup//`, which is a different (unguarded) path.
        route_path = routed_path(scope)
        if route_path in self.guarded_paths:
            guarded_path: str | None = route_path
        elif route_path.endswith("/") and route_path[:-1] in self.guarded_paths:
            guarded_path = route_path[:-1]
        else:
            guarded_path = None
        # A prefix match brings the methods its edge rule exempts along with it:
        # those are the ones that carry a body, and so the ones a missing
        # Content-Length must refuse.
        body_methods: frozenset[str] = frozenset()
        # Streaming only when EVERY matching pattern streams: the tightest rule
        # that covers a body is the one that must hold, for the budget as for
        # the cap.
        streams = False
        if guarded_path is not None:
            limit = self._limit_for(guarded_path)
        else:
            # No exact match: the prefix patterns are the only other exemption,
            # and a path matching several takes the SMALLEST cap -- the tightest
            # rule that covers a body is the one that must hold. `max_bytes` is
            # NOT one of the candidates: it sizes the gate's artifacts, and
            # clamping a part to it refuses the body the upload route publishes.
            matched = [p for p in GUARDED_PREFIXES if p.matches(route_path)]
            if not matched:
                await self.app(scope, receive, send)
                return
            limit = min(p.max_bytes for p in matched)
            body_methods = frozenset().union(*(p.methods for p in matched))
            streams = all(p.streams for p in matched)

        if _has_transfer_encoding(scope):
            await _reject(
                send,
                status=411,
                message=(
                    "Transfer-Encoding is not accepted on this endpoint; send a Content-Length body"
                ),
            )
            return
        declared = _declared_length(scope)
        if declared is None:
            # A prefix pattern covers a whole family, GETs and DELETEs included,
            # and a request with NEITHER framing header provably carries no body
            # at all (RFC 9112 §6): there is nothing to buffer and nothing to
            # bound, so refusing it would only break `GET /uploads/{id}`. The
            # refusal still stands for every method the edge exempts -- those are
            # the ones that arrive with bytes -- and for a Content-Length that is
            # present but unparsable, whatever the method.
            if (
                guarded_path is None
                and scope["method"] not in body_methods
                and _header(scope, b"content-length") is None
            ):
                await self.app(scope, receive, send)
                return
            await _reject(
                send,
                status=411,
                message="Content-Length is required on this endpoint",
            )
            return
        if declared > limit:
            await _reject(
                send,
                status=413,
                message=(
                    f"Request body of {declared} bytes exceeds the "
                    f"{limit}-byte limit for this endpoint"
                ),
            )
            return
        # A prefix pattern is challenged only above the anonymous allowance: a
        # small request keeps the route's own answer (Files must stay opaque
        # while it is disabled), a large one is exactly the buffer this refusal
        # exists to prevent.
        needs_credential = (
            guarded_path is not None or declared > _ANON_PREFIX_MAX_BYTES
        ) and not self._has_expected_credential(scope, guarded_path)
        if needs_credential and guarded_path in self.signed:
            # The route answers an unsigned call with a bare 400, and so does this.
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if needs_credential:
            # Refused BEFORE the body is materialized -- the whole point. Same
            # code / status / challenge header the auth dependency would emit
            # (`require_ci_token` challenges Bearer, `current_user` Cookie), so a
            # client sees no difference between this and the real 401.
            challenge = b"Bearer" if guarded_path in _CI_TOKEN_PATHS else b"Cookie"
            await _reject(
                send,
                status=401,
                code=ErrorCode.auth_required,
                message="Authentication is required on this endpoint",
                headers=((b"www-authenticate", challenge),),
            )
            return
        # Only an UPLOAD goes through the budget. A body at or under the exempt
        # ceiling is a lookup or a promote: it costs nothing to serve, and the CLI
        # raises on its first 5xx, so shedding it turns a busy fleet into a failed
        # CI step. It is therefore never charged and can never be refused by
        # somebody else's bytes.
        if declared <= self.budget_exempt_max_bytes:
            await self.app(scope, receive, send)
            return
        if streams:
            await self._stream(scope, receive, send)
            return
        if not self._has_room_for(declared):
            await _shed_busy(send)
            return
        meter = _InflightMeter(self, receive, send)
        try:
            await self.app(scope, meter.receive, meter.send)
        except Exception:
            # A shed request unwinds through the stream we severed (Starlette
            # raises ClientDisconnect out of `request.body()`). We already
            # answered it 503, so that unwind is ours to absorb rather than a
            # second response.
            if not meter.shed:
                raise
        finally:
            self._release(meter.charged)

    async def _stream(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Admit a streaming part by the resident budget and the caller's share."""
        who = _uploader_of(scope)
        if not self.parts.would_admit(who):
            await _shed_busy(send, share=self.parts.share(who))
            return
        meter = _ResidentMeter(self.parts, who, receive, send)
        try:
            await self.app(scope, meter.receive, meter.send)
        except Exception:
            if not meter.shed:
                raise
        finally:
            meter.close()


class _InflightMeter:
    """Charges one upload's ARRIVED body bytes against the process budget, and
    sheds the request the moment they carry the process past it.

    Charging the DECLARED length at admission instead would hand any anonymous
    caller a free denial of the ingest path: a socket that announces a max-size
    body and then sends nothing holds its reservation until the read gives up,
    so two of them fill the budget and 503 every real upload -- for bytes that
    were never sent and never cost the memory the budget exists to bound. Bytes
    that have not arrived buy no reservation; bytes that have are held until the
    request unwinds, which is exactly how long the process holds them.

    Shedding mid-body answers the 503 here, from the raw ``send``, and then drops
    everything the app tries to send afterwards -- so the client gets the same
    503 + ``Retry-After`` an admission-time shed produces, not the 400 that
    FastAPI renders a severed body stream as.
    """

    def __init__(
        self, middleware: GateIngestBodyLimitMiddleware, receive: Receive, send: Send
    ) -> None:
        self._middleware = middleware
        self._receive = receive
        self._send = send
        #: Bytes charged so far, and so the amount to give back when the request ends.
        self.charged = 0
        #: Whether this request's body stream has been severed mid-body.
        self.shed = False
        #: Whether the 503 for that shed went out from here, so the app's own
        #: answer to the severed stream must be dropped rather than forwarded.
        self._answered = False
        self._response_started = False

    async def receive(self) -> Message:
        if self.shed:
            # Severed for good: the app must not go on collecting a body we have
            # already refused.
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] != "http.request":
            return message
        chunk = len(message.get("body", b""))
        if not chunk:
            return message
        # Charged whether or not it fits: it is in this process either way, and
        # it is given back with the rest when the request unwinds.
        self.charged += chunk
        if self._middleware._charge(chunk):
            return message
        # Over budget on bytes that are really here. This chunk is never handed
        # up, so the app's buffer stops growing where the budget ran out.
        self.shed = True
        if not self._response_started:
            # Nothing has gone out yet, so we own the response.
            self._answered = True
            await _shed_busy(self._send)
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        # Only once WE have answered: otherwise a response the app had already
        # begun would be truncated rather than replaced.
        if self._answered:
            return
        if message["type"] == "http.response.start":
            self._response_started = True
        await self._send(message)


class _ResidentMeter:
    """Holds one part's slot from its first body byte to the end of its request.

    The slot is taken on the first non-empty chunk, not at admission: the route
    reads the body only after the caller and the session are authorized, so a
    request that fails there -- or a socket that announced a part and went
    quiet -- never takes one. A part whose first byte finds the slots gone is
    shed here with the same 503 an admission-time shed produces, and its bytes
    never reach the handler. The admitted response carries the caller's share
    as ``X-Upload-Concurrency``, so a client learns how many parts to keep in
    flight from the answer to the one it just sent.
    """

    def __init__(self, parts: PartAdmission, who: Uploader, receive: Receive, send: Send) -> None:
        self._parts = parts
        self._who = who
        self._receive = receive
        self._send = send
        self.holding = False
        self.shed = False
        self._answered = False
        self._response_started = False

    async def receive(self) -> Message:
        if self.shed:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] != "http.request" or self.holding:
            return message
        if not message.get("body", b""):
            return message
        if self._parts.acquire(self._who):
            self.holding = True
            return message
        self.shed = True
        if not self._response_started:
            self._answered = True
            await _shed_busy(self._send, share=self._parts.share(self._who))
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if self._answered:
            return
        if message["type"] == "http.response.start":
            self._response_started = True
            headers = list(message.get("headers", []))
            headers.append((b"x-upload-concurrency", str(self._parts.share(self._who)).encode()))
            message = {**message, "headers": headers}
        await self._send(message)

    def close(self) -> None:
        if self.holding:
            self.holding = False
            self._parts.release(self._who)


def _uploader_of(scope: Scope) -> Uploader:
    """The tenant and the person a part's slot is counted against -- the same
    two keys the rate limiter charges: the org and the user of a valid session;
    for a token-shaped bearer its digest as both, and for anything else the
    attested address as both. Read from the headers alone; the body is never
    touched here, and a forged or expired token buys neither key."""
    conn = HTTPConnection(scope)
    return Uploader(tenant=org_keys(conn)[0], principal=principal_keys(conn)[0])


def _has_transfer_encoding(scope: Scope) -> bool:
    """Whether the request carries a Transfer-Encoding header. When one is
    present the body is framed by the coding, not by any Content-Length that
    rides alongside (h11 does exactly that when both arrive), so the declared
    length cannot be trusted."""
    return any(name.lower() == b"transfer-encoding" for name, _ in scope.get("headers", []))


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return bytes(value)
    return None


def _has_expected_credential(scope: Scope, guarded_path: str | None) -> bool:
    """Whether the request carries a credential of the SHAPE this path's auth
    dependency needs. ``guarded_path`` is None for a prefix match, whose routes
    are all ordinary user surfaces and so want any session credential. Shape
    only -- validity, revocation, scope, and expiry all
    stay with the dependency; this exists solely so an anonymous caller cannot
    make the process buffer and JSON-decode a max-size document to earn a 401.

    Deliberately permissive at the margin (any non-empty bearer, any session
    cookie present): a false ACCEPT costs one buffered body that the real
    dependency then rejects, while a false REJECT would break a legitimate
    client. The CI paths can afford the tighter ``alk_ci_`` prefix check because
    ``require_ci_token`` demands exactly that prefix too.
    """
    header = _header(scope, b"authorization")
    bearer: str | None = None
    if header is not None:
        parts = header.decode("latin-1").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            bearer = parts[1].strip() or None
    if guarded_path in _CI_TOKEN_PATHS:
        return bearer is not None and looks_like_ci_token(bearer)
    if bearer is not None:
        return True
    cookie = _header(scope, b"cookie")
    return cookie is not None and f"{COOKIE_NAME}=" in cookie.decode("latin-1")


def _declared_length(scope: Scope) -> int | None:
    """The parsed Content-Length, or None when absent or unparsable (uvicorn
    rejects an unparsable length at the protocol level, so that branch is
    defensive)."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


async def _shed_busy(send: Send, *, share: int | None = None) -> None:
    """The 503 an upload gets when the process has no in-flight budget left for
    it -- identical whether it is refused at admission or mid-body, so a client
    cannot tell the two apart and the CLI's 5xx retry covers both. A streaming
    part's shed also names the caller's share, so the client that was sending
    too many at once learns how many to keep in flight."""
    headers: tuple[tuple[bytes, bytes], ...] = ((b"retry-after", b"1"),)
    if share is not None:
        headers += ((b"x-upload-concurrency", str(share).encode()),)
    await _reject(
        send,
        status=503,
        code=ErrorCode.unavailable,
        message="The server is busy handling uploads; retry shortly",
        headers=headers,
    )


async def _reject(
    send: Send,
    *,
    status: int,
    message: str,
    code: ErrorCode = ErrorCode.bad_request,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    body = json.dumps(
        build_error_body(code=code, message=message, trace_id=get_trace_id() or "", status=status)
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
