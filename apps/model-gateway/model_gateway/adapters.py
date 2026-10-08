"""Provider adapters: BodyCodec x Transport.

The codec is the model-family wire protocol (Anthropic Messages); the transport
is how we reach a specific provider (Anthropic-direct now, Bedrock next). They
compose: the same `AnthropicMessagesCodec` drives both transports, because the
Anthropic Messages body + SSE event shape is identical across them.

ZDR: nothing here logs request/response bodies — only the transport reads them,
and only the codec's usage parser inspects them (token counts, never content).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from alkera_core.config import settings
from alkera_core.gateway import (
    DISPLAY_SUMMARIZED,
    EFFORT_BETA_FLAG,
    HAIKU_THINKING_BUDGETS,
    IDEMPOTENCY_KEY_HEADER,
    THINKING_DISPLAY_HEADER,
    THINKING_MODE_ADAPTIVE,
    THINKING_MODE_EFFORT_BETA,
    USAGE_METER_HEADER,
)
from alkera_core.logging import get_logger
from alkera_core.model_catalog import Usage
from botocore.exceptions import ClientError
from botocore.tokens import FrozenAuthToken

from model_gateway.estimate import (
    ATTACHMENT_TOKEN_CHARGES,
    estimate_input_tokens,
    iter_body_text,
    resolve_max_output_tokens,
    text_tokenizer,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class ReasoningCaps:
    """Per-model reasoning capability, resolved from the catalog (``Model``) and
    threaded to the transport so it NEVER string-sniffs the upstream model id.

    - ``thinking_mode`` — how to engage reasoning on the Anthropic wire when an
      effort is chosen: ``"adaptive"`` (Claude 4.6+), ``"effort_beta"`` (Opus 4.5),
      or ``None`` (OpenAI reasoning.effort / no effort-engagement). This is the
      ONLY knob a transport acts on; the catalog's ``supports_thinking`` flag is
      purely informational (surfaced in ``/v1/models``) and is deliberately NOT
      carried here — the retired chat-completions path was its only consumer.
    """

    thinking_mode: str | None = None
    haiku_thinking: bool = False
    """When True (the model carries the ANTHROPIC_HAIKU_STYLE_THINKING flag), the
    Anthropic transports engage reasoning via ``thinking:{type:"enabled",
    budget_tokens:N}`` (N mapped from the effort) instead of
    ``output_config.effort`` / adaptive — both of which Haiku 4.5 400s on. The
    ``none`` effort (or no effort) sends a clean body with no thinking params."""


#: The default (no-capability) caps — a reusable frozen singleton so transport
#: signatures can take ``caps: ReasoningCaps | None = None`` without a mutable
#: default or a per-call allocation.
DEFAULT_CAPS = ReasoningCaps()


def _parse_retry_after(raw: str | None) -> float | None:
    """The provider's own throttling hint from ``Retry-After``, in seconds.

    Numeric-seconds form only — the HTTP-date form isn't something the LLM
    providers emit. ``None`` when absent, malformed, non-finite ("inf"/"nan"
    would otherwise leak into structured logs as non-RFC JSON tokens), or
    negative, so the caller falls back to its own backoff. A sibling parser
    (same numeric-seconds policy) lives in
    ``alkera_cli.plugins.plugin_base.http_retry`` — the CLI can't import the
    gateway, so keep their accepted forms aligned when changing either."""
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    return _parse_retry_after(response.headers.get("retry-after"))


async def _raise_upstream(response: httpx.Response) -> None:
    """Shared non-200 exit for every httpx transport — ONE site carrying the
    Retry-After hint, so a new transport can't forget it. (The Bedrock
    transport has no httpx response and extracts its own from botocore.)"""
    error_body = await response.aread()
    raise UpstreamError(
        response.status_code, error_body, retry_after=_retry_after_seconds(response)
    )


class UpstreamError(Exception):
    """A non-200 from the provider before any tokens streamed."""

    def __init__(self, status_code: int, body: bytes, retry_after: float | None = None) -> None:
        super().__init__(f"upstream returned {status_code}")
        self.status_code = status_code
        self.body = body
        # The provider's Retry-After hint (seconds), when it sent one. Every
        # task retries against the same shared account-level rate limit, so
        # honoring the provider's own pacing beats a blind exponential guess.
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        # 408 Request Timeout + 429 Too Many Requests + any 5xx are transient —
        # safe to retry (we only retry before the first byte streams).
        return self.status_code in (408, 429) or 500 <= self.status_code < 600

    @property
    def detail(self) -> str:
        """The provider's human-readable error message, extracted from its error
        body (OpenAI/Anthropic both nest it under ``error.message``). Falls back to
        a trimmed raw body so something meaningful always propagates upstream."""
        try:
            obj: Any = json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                nested = err.get("message")
                if isinstance(nested, str):
                    return nested
            if isinstance(err, str):
                return err
            top = obj.get("message")
            if isinstance(top, str):
                return top
        text = self.body.decode("utf-8", "replace").strip()
        return (
            text[: settings.gateway_upstream_error_excerpt_chars]
            if text
            else f"HTTP {self.status_code}"
        )

    @property
    def error_code(self) -> str | None:
        """The provider's machine-readable error code (OpenAI nests it under
        ``error.code`` — e.g. ``context_length_exceeded``; Anthropic omits it), or
        None when absent/unparseable. Lets the overflow classifier key off a
        structured code instead of only free-text."""
        try:
            obj: Any = json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                if isinstance(code, str):
                    return code
        return None


class Transport(Protocol):
    """Opens an upstream stream and yields raw SSE bytes (the provider's native
    wire format). Implementations own the connection lifecycle.

    ``effort`` is the resolved reasoning-effort variant (already validated
    against the model's allowed set), or None. ``caps`` carries the model's
    catalog-resolved reasoning capability. ``display`` is the per-turn thinking
    display (``"summarized"`` to surface readable thinking text, else None — the
    Anthropic-adaptive default omits it). Each transport translates them into
    its provider's native knobs when building the upstream body. ``request_id``
    is the gateway's id for the request; only a hop to another gateway sends it
    on, so the next gateway sees a retried hop as the same request."""

    def stream(
        self,
        *,
        upstream_model_id: str,
        region: str | None,
        body: dict[str, Any],
        effort: str | None = None,
        caps: ReasoningCaps | None = None,
        display: str | None = None,
        meter: str | None = None,
        request_id: str | None = None,
    ) -> Any:  # AbstractAsyncContextManager[AsyncIterator[bytes]]
        ...


class UsageParser(Protocol):
    """Accumulates token usage from a streamed response. `feed` is called with
    arbitrary byte slices; `usage()` is read once the stream ends (or is cut)."""

    def feed(self, chunk: bytes) -> None: ...
    def overflowed(self) -> bool:
        """Did an upstream line outgrow the partial-line buffer's ceiling? The
        parser has then dropped that line and stopped; the stream cannot be
        billed from it and must be ended rather than read on forever."""
        ...

    def upstream_opened(self) -> None:
        """The provider answered with a 200 and the relay is about to read it.

        That is the moment the prompt became the provider's to charge for: input
        is billed on acceptance, not on completion. Until it is called, a parser
        holding nothing bills nothing — a request every candidate refused must
        never settle against a request-side estimate.
        """
        ...

    def usage(self) -> Usage: ...
    def usage_is_estimated(self) -> bool:
        """Is ``usage()`` a server-side estimate rather than the provider's own
        reported figures? Equivalent to ``bool(estimated_sides())``; drives an ops
        log line, never a billing decision."""
        ...

    def estimated_sides(self) -> tuple[str, ...]:
        """Which halves of ``usage()`` this gateway guessed, in ``input`` /
        ``output`` order.

        ``output`` is the ordinary truncation case — the stream was cut before the
        provider's terminal count, so what was relayed is estimated from its
        characters. ``input`` is the sharper one: the wire never reported the
        prompt at all (it died inside Anthropic's ``message_start``, or before an
        OpenAI Responses stream said anything), so the figure being billed is the
        request-side estimate. They alert differently, so they are named apart.
        """
        ...

    def stream_failed(self) -> bool:
        """Did the stream end in an explicit upstream *failure* event (OpenAI
        ``response.failed`` / Anthropic ``error``)? Distinct from a normal
        completion and from a bare mid-stream disconnect; drives the settle
        ``failed`` flag so an in-band failure isn't booked as a 0-usage success."""
        ...


class Codec(Protocol):
    """The model-family wire protocol: request estimation (for the admission
    hold), a fresh per-stream usage parser, and the client-facing in-band SSE
    error shape. One codec drives N transports of the same family."""

    #: The provider family whose tokenizer / calibration prices this wire.
    FAMILY: str
    #: Output tokens reserved when neither the request nor the catalog says.
    DEFAULT_MAX_OUTPUT: int

    def estimate_input_tokens(self, body: dict[str, Any]) -> int: ...
    def max_output_tokens(self, body: dict[str, Any], *, default: int | None = None) -> int:
        """Output tokens to reserve: the request's own ceiling, else the model
        catalog's ``default``, else this codec's floor."""
        ...

    def usage_parser(
        self, body: dict[str, Any], *, cache_min_tokens: int | None = None
    ) -> UsageParser:
        """A fresh parser for one stream.

        ``cache_min_tokens`` is the provider's minimum cacheable prefix for this
        model as the catalog records it (``None`` when it does not). A wire whose
        caching is opt-in needs it to decide whether a caller's breakpoint took
        effect at all; a wire that caches automatically on a published wire-wide
        floor ignores it.
        """
        ...

    def error_sse(self, message: str, *, code: str | None = None) -> bytes: ...


#: What an inline binary attachment is charged in CHARACTERS, for the settle-side
#: estimate below. The admission hold states the same charges in tokens (see
#: ``estimate.ATTACHMENT_TOKEN_CHARGES``); these are that table at the historical
#: chars/3 divisor, and they feed only the chars/4 fallback used when a stream
#: dies before the provider reported usage.
_ATTACHMENT_CHAR_CHARGES: dict[str, tuple[int, int]] = {
    kind: (floor * 3, divisor // 3) for kind, (floor, divisor) in ATTACHMENT_TOKEN_CHARGES.items()
}

#: Chars per token for the settle-time OUTPUT estimate, and for the flat charge
#: an inline attachment carries. Both are measured over content this gateway
#: relayed or a payload whose size the provider caps, not over a caller's free
#: choice of script, so the central ratio is the right figure there.
_ESTIMATE_CHARS_PER_TOKEN = 4

# What a settle-time estimate of the PROMPT assumes a character is worth — as a
# CEILING, for the reason stated above the cache split: it is billed, the stream
# died before the provider reported anything, and the caller chose both the text
# and the cut. One chars/N ratio cannot be that ceiling. N is 4-5 on English
# prose, near 2 on source code, and below 1 on CJK, where both wires bill roughly
# a token per character — thirty thousand Chinese characters priced at chars/4
# settle at 7 500 tokens against something near 30 000, which is a 2-4x
# under-bill available to anyone who writes in Chinese and closes the socket.
#
# So the fallback counts by script, each class at the dense end of its own range.
# Measured against `o200k_base`, it is at or above the real count over NATURAL
# TEXT in every script — prose, source code, CJK, emoji, mixed. It is NOT a bound
# in general: ASCII's true bound is one token per character, which alternating
# punctuation reaches, and that shape still settles up to a third under. Closing
# it would bill ordinary English at four times its real count on every cut
# stream, the same error in the other direction as billing three tokens per
# Chinese character. The gap is pinned as a known one rather than left to be
# rediscovered (see `test_the_prompt_estimate_is_a_ceiling_over_natural_text`).

#: ASCII bills two tokens per three characters — the dense end of what either
#: tokenizer produces on real ASCII (prose runs 4-5 chars/token, source code
#: near 2, and only adversarial punctuation-only text goes denser).
_CEILING_ASCII_TOKENS_PER_CHARS = (2, 3)

#: A non-ASCII character inside the BMP — CJK, Hangul, Cyrillic, accented Latin
#: — bills at least a token. Anthropic's own guidance puts CJK near one token
#: per character, and a BPE cannot merge below that reliably across scripts.
_CEILING_BMP_TOKENS_PER_CHAR = 1

#: A character outside the BMP is four UTF-8 bytes, and a byte-level BPE gives
#: an emoji two to four tokens. Three is the dense end short of the byte bound.
_CEILING_ASTRAL_TOKENS_PER_CHAR = 3


@dataclass(frozen=True)
class _PromptSize:
    """A request's billable text, split the way the ceiling prices it.

    ``attachment_chars`` are kept apart: an inline image or audio payload is
    charged a flat figure the provider itself caps, so it converts at the
    central ratio rather than at a per-script ceiling that would treat base64 as
    prose and bill a 400 KB image several times over.
    """

    ascii_chars: int = 0
    bmp_chars: int = 0
    astral_chars: int = 0
    attachment_chars: int = 0

    def __add__(self, other: _PromptSize) -> _PromptSize:
        return _PromptSize(
            self.ascii_chars + other.ascii_chars,
            self.bmp_chars + other.bmp_chars,
            self.astral_chars + other.astral_chars,
            self.attachment_chars + other.attachment_chars,
        )

    def tokens(self) -> int:
        per, chars = _CEILING_ASCII_TOKENS_PER_CHARS
        return (
            -(-self.ascii_chars * per // chars)  # ceiling division
            + self.bmp_chars * _CEILING_BMP_TOKENS_PER_CHAR
            + self.astral_chars * _CEILING_ASTRAL_TOKENS_PER_CHAR
            + self.attachment_chars // _ESTIMATE_CHARS_PER_TOKEN
        )


def _prompt_size(body: Any) -> _PromptSize:
    """Every billable string in a request body, counted by script.

    The same walk the admission estimate uses (:func:`iter_body_text`) — so the
    two can never disagree about WHICH strings a request bills for, only about
    what each is worth.
    """
    size = _PromptSize()
    for text, attachment in iter_body_text(body):
        if attachment is not None:
            floor, divisor = _ATTACHMENT_CHAR_CHARGES[attachment]
            size += _PromptSize(attachment_chars=max(floor, len(text) // divisor))
        else:
            size += _script_counts(text)
    return size


def _script_counts(text: str) -> _PromptSize:
    """One string's characters, split the way the ceiling prices them."""
    ascii_chars = bmp = astral = 0
    for char in text:
        point = ord(char)
        if point < 128:
            ascii_chars += 1
        elif point <= 0xFFFF:
            bmp += 1
        else:
            astral += 1
    return _PromptSize(ascii_chars, bmp, astral)


#: Counts one body, or one block of it, in tokens.
TokenMeter = Callable[[Any], int]


def _token_meter(body: dict[str, Any], *, family: str) -> TokenMeter:
    """How this request's prompt is counted at settle time.

    The family's REAL tokenizer when one loaded — the same resolution
    :func:`estimate_input_tokens` uses for the admission hold, so the hold and
    the settle count the same strings the same way instead of disagreeing by a
    multiple. That matters because a cut stream settles against a hold that was
    taken from the tokenizer: a settle figure several times larger does not land
    on the reservation at all, it lands on the overdraft clamp, and the caller
    who paid it is far more often an agent SDK that timed out than an adversary.

    Only when no encoding resolves in this process does the per-script ceiling
    below take over — the fallback has to stay a ceiling precisely because it is
    unverified.
    """
    encode = text_tokenizer(body, family=family)

    def meter(node: Any) -> int:
        size = _PromptSize()
        tokens = 0
        for text, attachment in iter_body_text(node):
            if attachment is not None:
                floor, divisor = _ATTACHMENT_CHAR_CHARGES[attachment]
                size += _PromptSize(attachment_chars=max(floor, len(text) // divisor))
            elif encode is not None:
                tokens += encode(text)
            else:
                size += _script_counts(text)
        return tokens + size.tokens()

    return meter


def _body_char_count(body: Any) -> int:
    """Billable CHARACTERS over the whole body — the blunt measure, with no view
    on what a character is worth. Kept for the admission estimator's own
    calibration checks, which compare a real tokenizer against raw size."""
    size = _prompt_size(body)
    return size.ascii_chars + size.bmp_chars + size.astral_chars + size.attachment_chars


# How a settle-time input estimate splits across the cache rate kinds.
#
# AN ESTIMATE ON A CUT STREAM IS A CEILING, NEVER A GUESS. This path runs only
# when the provider reported no usage at all, and the caller is the one who chose
# where to cut — so every ambiguity resolves toward the HIGHER price. Charging a
# genuinely cached prefix too much is a refundable error a customer can raise;
# charging a prompt the provider billed in full at a tenth of it is an under-bill
# nothing downstream ever surfaces, and one a caller can elect with a JSON key.
#
# Concretely:
#   - a prefix is split off only where the provider's own behaviour says it was
#     cached: OpenAI caches a long enough prefix with no opt-in, Anthropic caches
#     nothing except what a `cache_control` breakpoint covers;
#   - on Anthropic that prefix bills at the cache-WRITE rate, never the read
#     rate. A breakpoint says the caller asked for the prefix to be cached, not
#     that it already was; the first such request pays the write (1.25x the input
#     rate, or 2x for a one-hour TTL) and only a later one pays the read (0.1x).
#     Nothing in a stream that died before `message_start` tells the two apart,
#     so the estimate takes the write;
#   - and a breakpoint under the model's minimum cacheable prefix is ignored by
#     the provider entirely, so those tokens bill as plain input.

#: OpenAI's automatic prompt-cache floor: it caches nothing below this many
#: tokens, so a shorter request is entirely fresh input however it is shaped.
#: Wire-wide and published as one figure, unlike Anthropic's per-model minimum.
_OPENAI_AUTO_CACHE_MIN_TOKENS = 1024

#: The one non-default ``cache_control.ttl`` Anthropic accepts. It writes the
#: prefix at 2x the input rate instead of 1.25x, so an estimate has to carry it.
_ANTHROPIC_CACHE_TTL_1H = "1h"


@dataclass(frozen=True)
class MarkedPrefix:
    """The part of an Anthropic request a ``cache_control`` breakpoint covers.

    ``ttl_1h`` carries the TTL the breakpoint asked for, because the two write
    rates differ by 1.6x and the caller names which one it wants.
    """

    tokens: int = 0
    ttl_1h: bool = False


def _auto_cached_prefix_tokens(
    body: dict[str, Any], turns_key: str, *, total: int, meter: TokenMeter
) -> int:
    """Tokens of an OpenAI request its automatic prompt cache had already held.

    OpenAI caches every request prefix over its floor with no opt-in, so on a
    continued conversation the turns before the newest one were served from cache
    and charged at the cache-read rate. A single-turn request has no such prefix,
    and a short one is below the floor — both are fully fresh input, which is what
    keeps a one-shot giant prompt from being estimated at the cheap rate.

    ``total`` is the whole body's token ceiling. Measured over the TURNS only,
    never `total`. `total` is the whole body, which
    includes `tools` and the instructions/system block, and those can change on any
    request — a caller that ships a new tool schema on turn five is sending fresh
    input, not replaying a cached prefix. Deriving the prefix from `total` would
    price that as cache-read and settle the request under its real cost.
    """
    if total < _OPENAI_AUTO_CACHE_MIN_TOKENS:
        return 0
    turns = body.get(turns_key)
    if not isinstance(turns, list) or len(turns) < 2:
        return 0
    prefix = sum(meter(turn) for turn in turns[:-1])
    return min(prefix, total)


def _marked_cache_prefix(
    body: dict[str, Any], *, total: int, min_tokens: int | None, meter: TokenMeter
) -> MarkedPrefix:
    """The part of an Anthropic request a ``cache_control`` breakpoint covers.

    Anthropic caches nothing on its own. A prefix is cached only where the caller
    placed a breakpoint, and a breakpoint covers everything up to and including
    the block carrying it. So a body with no breakpoint — an ordinary multi-turn
    conversation from any client that does not opt in — asked for nothing to be
    cached, and the whole prompt is fresh input at the full rate.

    ``min_tokens`` is the provider's minimum cacheable prefix for THIS model,
    from the catalog. A breakpoint over a shorter prefix is ignored and those
    tokens are charged as ordinary input, so the floor gates the COVERED prefix,
    not the whole prompt — a small marked prefix inside a huge request is not
    cached just because the request is long.

    ``total`` is the whole body's token ceiling, which bounds what the split can
    claim. ``None`` means the catalog does not record the figure for this model, and it
    is read as a floor of ZERO: the marked prefix bills as a write. Not splitting
    at all would bill it as plain input, which is 1.0x against a write's 1.25x or
    2.0x — the cheap side of the ambiguity, on the one input a caller controls
    completely. Where the card carries no write price the kind re-rates to input
    anyway, with the unpriced-usage alert, so the unrecorded case degrades to the
    old figure instead of guessing under it.

    Blocks are measured with the same character walk as ``total``, so the two
    sides of the split can never disagree about what a string is worth; only the
    message envelope around a content block goes uncounted, which can shrink the
    covered prefix by a few tokens and never grow it.
    """
    floor = 0 if min_tokens is None else min_tokens
    covered = 0
    ttl_1h = False
    walked = 0
    for block in _anthropic_cache_blocks(body):
        walked += meter(block)
        marker = block.get("cache_control") if isinstance(block, dict) else None
        if marker:
            covered = walked
            # ACCUMULATED, never overwritten: a prefix holding both TTLs was
            # written partly at the dearer one, and the last breakpoint's TTL is
            # not the price of everything before it. One 1h marker anywhere in
            # the covered prefix prices the prefix at 1h.
            ttl_1h = ttl_1h or (
                isinstance(marker, dict) and marker.get("ttl") == _ANTHROPIC_CACHE_TTL_1H
            )
    if covered < floor:
        return MarkedPrefix()
    return MarkedPrefix(tokens=min(covered, total), ttl_1h=ttl_1h)


def _anthropic_cache_blocks(body: dict[str, Any]) -> Iterator[Any]:
    """Every block a ``cache_control`` breakpoint can sit on, in prompt order.

    Anthropic hashes the prompt as tools, then system, then messages, so that is
    the order a breakpoint's prefix is measured in — regardless of where the keys
    happen to appear in the request JSON.
    """
    for key in ("tools", "system", "messages"):
        section = body.get(key)
        if not isinstance(section, list):
            if section is not None:
                yield section
            continue
        for item in section:
            content = item.get("content") if isinstance(item, dict) else None
            if key == "messages" and isinstance(content, list):
                yield from content
            else:
                yield item


#: Keys of an Anthropic ``content_block_delta`` payload that carry BILLED content:
#: assistant text, a tool call's streamed arguments, and thinking. ``signature`` is
#: a cryptographic attestation of a thinking block, not generated content, so it is
#: deliberately absent.
_ANTHROPIC_DELTA_CONTENT_KEYS = ("text", "partial_json", "thinking")

#: What the client is told when an upstream line outgrew the buffer ceiling.
#: Named so a caller can recognize the reason without matching on prose.
LINE_OVERFLOW_CODE = "upstream_line_too_long"
LINE_OVERFLOW_MESSAGE = "upstream sent a line larger than the gateway will buffer"


class _SseLineBuffer:
    """Reassembles SSE lines across arbitrary chunk boundaries, under a ceiling.

    A parser has to hold a partial line until its newline arrives, and an
    upstream decides when that is. The upstream is not always one we chose: an
    org running BYOK supplies its own ``base_url``, so a tenant can point a
    route at a server that streams megabytes with no newline in them — in a
    process that is shared with every other tenant's in-flight stream. Without a
    ceiling that is one tenant growing the whole gateway's memory.

    So the partial line is capped at the same figure that bounds a request body
    (``gateway_max_request_body_bytes``): no legitimate provider frame comes
    near it, and a deployment that moves one has moved the other. Past the cap
    the partial line is dropped and the parser stops — its counts can no longer
    be trusted to describe the stream, so the caller settles on what was metered
    up to that point and ends the step rather than reading on forever.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._overflowed = False

    def feed(self, chunk: bytes, handle_line: Callable[[bytes], None]) -> None:
        if self._overflowed:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            handle_line(line)
        # Checked AFTER draining: what is left is one genuinely unterminated
        # line, never a backlog of whole frames that arrived in a single chunk.
        if len(self._buf) > settings.gateway_max_request_body_bytes:
            self._buf = b""
            self._overflowed = True

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def pending_bytes(self) -> int:
        """How much partial line is held right now — the memory a hostile
        upstream can make this parser occupy."""
        return len(self._buf)


class AnthropicUsageParser:
    """Accumulates token usage from a streamed Anthropic Messages response.

    Robust to arbitrary chunk boundaries (buffers partial SSE lines). Input +
    cache tokens arrive in ``message_start``; the real ``output_tokens`` arrives
    ONLY in the terminal ``message_delta`` Anthropic emits after the last content
    block (``message_start`` seeds the output count at 1). Reasoning/thinking
    tokens are folded into ``output_tokens`` by Anthropic, so they're billed as
    output.

    Content itself rides ``content_block_delta`` frames, which carry no counts, so
    a stream cut before the terminal frame has delivered tokens the provider
    charges us for with nothing to bill them from. The parser therefore also
    counts the characters it relays and falls back to a chars/token estimate of
    them. Without that fallback a client can read the whole answer and close the
    socket one frame early to settle at one output token — the entire completion
    for the price of its input, on the expensive side of every rate card.

    The INPUT side has the same hole one frame earlier. ``message_start`` is a
    single SSE line, held by the line buffer until its newline, so a socket cut
    inside it leaves the parser with no input figure at all — while the provider,
    which bills input on acceptance, has already charged for the whole prompt. So
    a codec hands this parser the request-side estimate too, used for the input
    side only when ``message_start`` never reported and the upstream had actually
    opened.
    """

    def __init__(
        self, *, estimated_input_tokens: int = 0, marked_prefix: MarkedPrefix | None = None
    ) -> None:
        self._lines = _SseLineBuffer()
        self._input = 0
        self._output = 0
        self._cache_read = 0
        self._cache_write_5m = 0
        self._cache_write_1h = 0
        self._failed = False
        self._reported = False
        self._started = False
        self._opened = False
        self._streamed_chars = 0
        self._estimated_input = max(0, estimated_input_tokens)
        marked = marked_prefix or MarkedPrefix()
        self._marked = MarkedPrefix(
            tokens=min(max(0, marked.tokens), self._estimated_input), ttl_1h=marked.ttl_1h
        )

    def feed(self, chunk: bytes) -> None:
        self._lines.feed(chunk, self._handle_line)
        if self._lines.overflowed:
            self._failed = True

    def overflowed(self) -> bool:
        return self._lines.overflowed

    def pending_line_bytes(self) -> int:
        return self._lines.pending_bytes

    def _handle_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        kind = obj.get("type")
        if kind == "error":
            # Anthropic streams a terminal `error` event (overloaded_error, api_error,
            # …) in-band on an HTTP-200 stream. Flag it so settle records FAILED.
            self._failed = True
        elif kind == "message_start":
            self._apply_start(obj.get("message", {}).get("usage", {}) or {})
        elif kind == "message_delta":
            usage = obj.get("usage", {}) or {}
            if "output_tokens" in usage:
                self._output = int(usage["output_tokens"] or 0)
                self._reported = True
        elif kind == "content_block_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict):
                for key in _ANTHROPIC_DELTA_CONTENT_KEYS:
                    piece = delta.get(key)
                    if isinstance(piece, str):
                        self._streamed_chars += len(piece)

    def _apply_start(self, usage: dict[str, Any]) -> None:
        self._started = True
        self._input = int(usage.get("input_tokens", 0) or 0)
        self._cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        created = int(usage.get("cache_creation_input_tokens", 0) or 0)
        breakdown = usage.get("cache_creation") or {}
        self._cache_write_5m = int(breakdown.get("ephemeral_5m_input_tokens", 0) or 0)
        self._cache_write_1h = int(breakdown.get("ephemeral_1h_input_tokens", 0) or 0)
        if not breakdown and created:
            self._cache_write_5m = created  # no per-ttl breakdown → attribute to 5m
        if "output_tokens" in usage:
            self._output = int(usage["output_tokens"] or 0)

    def upstream_opened(self) -> None:
        self._opened = True

    def usage(self) -> Usage:
        if self._input_is_estimated():
            # `message_start` never landed, but the provider accepted the prompt
            # and charged for it. Bill the request-side estimate: the prefix the
            # caller marked for caching at the WRITE rate its TTL names, the rest
            # as plain input. Nothing here can show the cache was already warm,
            # and a ceiling does not assume the cheapest of the three rates.
            written = self._marked.tokens
            return Usage(
                input=self._estimated_input - written,
                output=self._settled_output(),
                cache_write_1h=written if self._marked.ttl_1h else 0,
                cache_write_5m=0 if self._marked.ttl_1h else written,
            )
        return Usage(
            input=self._input,
            output=self._settled_output(),
            cache_read=self._cache_read,
            cache_write_5m=self._cache_write_5m,
            cache_write_1h=self._cache_write_1h,
        )

    def _input_is_estimated(self) -> bool:
        """The input figure is this gateway's guess rather than Anthropic's.

        Only once the upstream opened — a refused request never reached the
        provider's meter — and only when the stream did not end in an explicit
        in-band failure, which is the provider declining the turn rather than
        truncating it. Both of those would otherwise bill a full prompt for a
        request nobody was charged for.
        """
        return self._opened and not self._started and not self._failed

    def _settled_output(self) -> int:
        """Anthropic's own figure when the terminal ``message_delta`` arrived; else
        the estimate over what actually streamed. Never BELOW the reported figure —
        ``message_start``'s seed is a floor, not a ceiling."""
        if self._reported or not self._streamed_chars:
            return self._output
        return max(self._output, self._streamed_chars // _ESTIMATE_CHARS_PER_TOKEN)

    def usage_is_estimated(self) -> bool:
        return bool(self.estimated_sides())

    def estimated_sides(self) -> tuple[str, ...]:
        sides = []
        if self._input_is_estimated():
            sides.append("input")
        if not self._reported and self._streamed_chars:
            sides.append("output")
        return tuple(sides)

    def stream_failed(self) -> bool:
        return self._failed


class AnthropicMessagesCodec:
    """Request estimation + a fresh usage parser per stream."""

    FAMILY = "anthropic"
    DEFAULT_MAX_OUTPUT = 4096

    def estimate_input_tokens(self, body: dict[str, Any]) -> int:
        # Calibrated over the WHOLE body (system + messages + tools + tool results
        # + attachments), so the hold can't be defeated by moving payload out of
        # the message text. The hold is admission-control only — actual usage from
        # the stream is what gets billed, and the over-hold is released on settle.
        return estimate_input_tokens(body, family=self.FAMILY)

    def max_output_tokens(self, body: dict[str, Any], *, default: int | None = None) -> int:
        return resolve_max_output_tokens(
            body.get("max_tokens"), default=default, floor=self.DEFAULT_MAX_OUTPUT
        )

    def usage_parser(
        self, body: dict[str, Any], *, cache_min_tokens: int | None = None
    ) -> AnthropicUsageParser:
        # The Anthropic wire reports real input tokens in its very first frame
        # (`message_start`) — but only if that frame arrives AND completes, and it
        # is one SSE line, so a cut inside it leaves no input figure at all for a
        # prompt the provider has already charged for. Hand the parser a
        # request-side estimate (the central chars/4 ratio, not the conservative
        # hold divisor — this number can end up billed) to fall back on; a
        # reported figure always wins. Only a prefix the caller explicitly marked
        # for caching is split off, because that is all this wire ever caches,
        # and only when the catalog records the minimum that makes a breakpoint
        # take effect on this model.
        meter = _token_meter(body, family=self.FAMILY)
        total = meter(body)
        estimated = max(1, total)
        return AnthropicUsageParser(
            estimated_input_tokens=estimated,
            marked_prefix=_marked_cache_prefix(
                body, total=total, min_tokens=cache_min_tokens, meter=meter
            ),
        )

    def error_sse(self, message: str, *, code: str | None = None) -> bytes:
        # Prefix the error with a minimal `message_start` so the downstream
        # Anthropic stream parser (opencode's @ai-sdk/anthropic) is initialized and
        # treats the `error` event as TERMINAL. A bare error-first frame (no
        # message_start) leaves the harness's turn hanging instead of surfacing the
        # error — real Anthropic streams always open with message_start.
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_gateway_error",
                "type": "message",
                "role": "assistant",
                "model": "",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        # `code` (when set — e.g. "context_length_exceeded") is the structured signal
        # opencode's `parseStreamError` switches on (→ native compaction) and the
        # Alkera harness translator classifies on. Anthropic carries no native code,
        # so we stamp it on the error object; omitting it keeps the byte-identical
        # legacy shape for every non-classified error.
        error_obj: dict[str, Any] = {"type": "api_error", "message": message}
        if code is not None:
            error_obj["code"] = code
        err = {"type": "error", "error": error_obj}
        return (
            f"event: message_start\ndata: {json.dumps(start)}\n\n"
            f"event: error\ndata: {json.dumps(err)}\n\n"
        ).encode()


class OpenAIResponsesUsageParser:
    """Accumulates usage from an OpenAI **Responses API** stream.

    Usage appears exactly once, in the terminal ``response.completed`` (or
    ``response.incomplete``) event, under ``response.usage``. Each streamed event's
    ``data:`` JSON self-describes via a ``type`` field, so we key off that (no need
    to track the ``event:`` line). Disjoint billable buckets (same as the chat
    parser, so billing is unchanged):

    - ``input``      = ``input_tokens - cached_tokens - cache_write_tokens`` (the
      uncached prompt that was not written to the cache either)
    - ``cache_read`` = ``input_tokens_details.cached_tokens``
    - ``cache_write_5m`` = ``input_tokens_details.cache_write_tokens`` — the prefix
      OpenAI wrote to its prompt cache on this request, which newer models (GPT-6,
      GPT-5.6) report and bill at their cache-write price (1.25x input). OpenAI has
      one write price, carried on the standard write kind. The three are disjoint
      parts of ``input_tokens`` ("input tokens are either Input, Cached Input, or
      Cache Write"), so none is counted twice.
    - ``output``     = ``output_tokens`` (OpenAI already folds reasoning tokens
      into ``output_tokens`` — ``output_tokens_details.reasoning_tokens`` is a
      SUBSET — so we do **not** add a separate ``reasoning`` count.

    A stream cut before that terminal event (a client disconnect, the max-duration
    cut-off) still delivered tokens the provider charges us for, so the parser also
    accumulates a FALLBACK estimate as it relays: every ``response.*.delta`` payload
    feeds a running output-character count, and ``estimated_input_tokens`` (the
    request-side estimate the codec passes in) supplies the input side the wire never
    echoes. The fallback is used ONLY when the authoritative usage never arrived and
    something was actually produced — the terminal event always wins.

    ``estimated_cached_tokens`` is the part of that input estimate a prompt cache
    plausibly served (the conversation prefix — see
    :func:`OpenAIResponsesCodec.usage_parser`). Attributing it to ``cache_read``
    matters because an estimate that gets BILLED must not charge the full uncached
    rate for input OpenAI auto-cached: an agent aborting a turn on a long
    conversation is routine, and charging it as fresh input over-bills by a multiple
    of the real cost.

    Robust to arbitrary chunk boundaries (buffers partial SSE lines).
    """

    _TERMINAL = frozenset({"response.completed", "response.incomplete"})

    def __init__(
        self, *, estimated_input_tokens: int = 0, estimated_cached_tokens: int = 0
    ) -> None:
        self._lines = _SseLineBuffer()
        self._input = 0
        self._output = 0
        self._cache_read = 0
        self._cache_write = 0
        self._failed = False
        self._reported = False
        self._estimated_input = max(0, estimated_input_tokens)
        self._estimated_cached = min(max(0, estimated_cached_tokens), self._estimated_input)
        self._streamed_chars = 0
        self._opened = False

    def feed(self, chunk: bytes) -> None:
        self._lines.feed(chunk, self._handle_line)
        if self._lines.overflowed:
            self._failed = True

    def overflowed(self) -> bool:
        return self._lines.overflowed

    def upstream_opened(self) -> None:
        self._opened = True

    def pending_line_bytes(self) -> int:
        return self._lines.pending_bytes

    def _handle_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        type_ = obj.get("type")
        # OpenAI reports upstream errors (deprecation, moderation, overload, quota)
        # IN-BAND on an HTTP-200 stream — a terminal `response.failed` (or a bare
        # `error` event), never a completion. Flag it so settle records FAILED
        # instead of a 0-usage SETTLED success (which also fires a false drift alarm).
        if type_ in ("response.failed", "error"):
            self._failed = True
        if type_ in self._TERMINAL:
            usage = (obj.get("response") or {}).get("usage")
            if isinstance(usage, dict):
                self._apply(usage)
        elif isinstance(type_, str) and type_.endswith(".delta"):
            # Every incremental payload the Responses wire emits (output text,
            # reasoning summary, refusal, tool-call arguments) rides in `delta`.
            # Counting them as they relay is what makes a truncated stream billable.
            delta = obj.get("delta")
            if isinstance(delta, str):
                self._streamed_chars += len(delta)

    def _apply(self, usage: dict[str, Any]) -> None:
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        details = usage.get("input_tokens_details") or {}
        cached = int(details.get("cached_tokens", 0) or 0)
        cached = min(cached, inp)  # defensive: cached is a subset of input
        written = int(details.get("cache_write_tokens", 0) or 0)
        written = min(max(0, written), inp - cached)  # and so is what was written
        self._cache_read = cached
        self._cache_write = written
        self._input = inp - cached - written
        self._output = out
        self._reported = True

    def usage(self) -> Usage:
        if self._reported:
            return Usage(
                input=self._input,
                output=self._output,
                cache_read=self._cache_read,
                cache_write_5m=self._cache_write,
            )
        if self._streamed_chars:
            # Cut before the terminal event, but tokens WERE delivered: bill the
            # estimate rather than releasing the hold to zero, which would hand out
            # the relayed completion for free to anyone who closes the connection
            # one frame early. The conversation prefix bills at the cache-read rate
            # because OpenAI auto-caches it and charged us accordingly.
            return Usage(
                input=self._estimated_input - self._estimated_cached,
                output=max(1, self._streamed_chars // _ESTIMATE_CHARS_PER_TOKEN),
                cache_read=self._estimated_cached,
            )
        if self._input_is_estimated():
            # Nothing was relayed at all, but the provider accepted the request and
            # charges input on acceptance. Bill the prompt; guess no output, since
            # none was seen.
            return Usage(
                input=self._estimated_input - self._estimated_cached,
                output=0,
                cache_read=self._estimated_cached,
            )
        # A stream that never opened, or one the provider failed in band having
        # produced nothing — neither is a prompt anyone was charged for.
        return Usage(input=self._input, output=self._output, cache_read=self._cache_read)

    def _input_is_estimated(self) -> bool:
        """Is the input side of :meth:`usage` this gateway's own guess? Only once
        the upstream opened (a refused request never reached the provider's meter)
        and only short of an in-band failure, which is the turn being declined
        rather than truncated."""
        return self._opened and not self._reported and not self._failed

    def usage_is_estimated(self) -> bool:
        """True when :meth:`usage` is a fallback rather than the provider's own
        figures — the signal the pipeline logs on."""
        return bool(self.estimated_sides())

    def estimated_sides(self) -> tuple[str, ...]:
        if self._reported:
            return ()
        if self._streamed_chars:
            # The whole fallback shape: the request-side input estimate AND the
            # relayed-character output estimate.
            return ("input", "output") if self._estimated_input else ("output",)
        return ("input",) if self._input_is_estimated() else ()

    def stream_failed(self) -> bool:
        return self._failed


class OpenAIResponsesCodec:
    """Request estimation + a fresh usage parser per OpenAI Responses stream."""

    FAMILY = "openai"
    DEFAULT_MAX_OUTPUT = 4096

    def estimate_input_tokens(self, body: dict[str, Any]) -> int:
        # Calibrated over the WHOLE body (instructions + input + tools +
        # function-call outputs + attachments); the hold is admission-control only.
        return estimate_input_tokens(body, family=self.FAMILY)

    def max_output_tokens(self, body: dict[str, Any], *, default: int | None = None) -> int:
        return resolve_max_output_tokens(
            body.get("max_output_tokens"), default=default, floor=self.DEFAULT_MAX_OUTPUT
        )

    def usage_parser(
        self, body: dict[str, Any], *, cache_min_tokens: int | None = None
    ) -> OpenAIResponsesUsageParser:
        # The Responses wire only reports usage in its TERMINAL event, so a stream
        # cut short carries no input figure at all. Hand the parser a request-side
        # estimate (the central chars/4 ratio, not the conservative hold divisor —
        # this number can end up billed) to fall back on, split into the fresh turn
        # and the conversation prefix an OpenAI prompt cache would have served.
        # `cache_min_tokens` is unused: OpenAI caches automatically above one
        # published wire-wide floor, with no per-model minimum to record.
        meter = _token_meter(body, family=self.FAMILY)
        total = meter(body)
        estimated = max(1, total)
        return OpenAIResponsesUsageParser(
            estimated_input_tokens=estimated,
            estimated_cached_tokens=_auto_cached_prefix_tokens(
                body, "input", total=total, meter=meter
            ),
        )

    def error_sse(self, message: str, *, code: str | None = None) -> bytes:
        # Emit `response.created` (so the AI-SDK Responses parser initializes its
        # metadata) then a top-level `error` event. The SDK's Responses stream
        # transform special-cases any chunk with a top-level `error` key — it
        # enqueues a terminal stream `error` so the harness surfaces it as a failed
        # turn (NOT a clean finish). `response.failed` alone only sets an "error"
        # finishReason, which opencode treats as an empty idle turn — so we must use
        # the `error` event. Shape matches the SDK's error-chunk schema
        # (`type`/`sequence_number`/`error:{type,code,message}`).
        created = {
            "type": "response.created",
            "response": {
                "id": "resp_gateway_error",
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        }
        # `code` carries the structured classification (e.g. "context_length_exceeded")
        # opencode's `parseStreamError` + the harness translator key off; the default
        # "gateway_error" preserves the legacy shape for unclassified errors.
        error = {
            "type": "error",
            "sequence_number": 0,
            "error": {
                "type": "gateway_error",
                "code": code or "gateway_error",
                "message": message,
            },
        }
        return (
            f"event: response.created\ndata: {json.dumps(created)}\n\n"
            f"event: error\ndata: {json.dumps(error)}\n\n"
        ).encode()


def _apply_anthropic_effort(payload: dict[str, Any], effort: str | None) -> None:
    """Map a reasoning-effort variant onto the Anthropic Messages body via the
    `output_config.effort` knob. Valid values are model-dependent (Claude 4.6+:
    low/medium/high/max; Opus 4.5: low/medium/high) — the gateway forwards the
    catalog-validated value as-is. Shared by the direct + Bedrock transports."""
    if not effort:
        return
    existing = payload.get("output_config")
    output_config = dict(existing) if isinstance(existing, dict) else {}
    output_config["effort"] = effort
    payload["output_config"] = output_config


def _apply_anthropic_thinking(
    payload: dict[str, Any], effort: str | None, caps: ReasoningCaps, display: str | None = None
) -> None:
    """Engage adaptive reasoning on the Anthropic wire, per the model's
    ``thinking_mode`` (verified live, see the reasoning-effort matrix):

    - ``adaptive`` (Claude 4.6+): inject ``thinking:{type:"adaptive"}`` when EITHER
      an effort is chosen (``output_config.effort`` ALONE is a no-op — it engages
      reasoning only when paired with this block) OR the user asked to SEE the
      thinking text (``display == "summarized"``). The display arm matters because a
      Show-thoughts-ON turn can reach here with NO effort (a request that omits the
      ``::effort`` knob); without engaging the block the ``display`` request would be
      silently dropped and the model would surface no thinking — the exact "thoughts
      never appear" failure. The model still picks its own budget when no effort is set.
    - ``effort_beta`` (Opus 4.5): effort works standalone; the beta flag is
      carried by the transport (header on direct / body on Bedrock), NOT here.

    Both the direct and Bedrock transports call this; the beta-flag placement is
    the only wire-specific bit (handled in each transport)."""
    if caps.thinking_mode == THINKING_MODE_ADAPTIVE and (effort or display == DISPLAY_SUMMARIZED):
        thinking: dict[str, Any] = {"type": "adaptive"}
        # Claude 4.8/4.7/4.6 adaptive thinking OMITS the thinking text by default; opt
        # in with display:"summarized" so a user with Show-thoughts ON sees it. Off →
        # leave it omitted: billing is identical either way (the full thinking tokens
        # are always billed; the summary is free), but omitting skips streaming the
        # thinking deltas, so the answer's first text token arrives sooner.
        if display == DISPLAY_SUMMARIZED:
            thinking["display"] = DISPLAY_SUMMARIZED
        payload["thinking"] = thinking


def _request_max_tokens(body: dict[str, Any], default: int = 4096) -> int:
    """The Anthropic request's `max_tokens` (the API requires it), else ``default``.
    Single source for the codec's admission estimate AND the Haiku thinking-budget
    clamp (which must stay strictly below it)."""
    value = body.get("max_tokens")
    return value if isinstance(value, int) and value > 0 else default


def _apply_haiku_thinking(payload: dict[str, Any], effort: str | None, max_tokens: int) -> None:
    """Engage Haiku 4.5-style extended thinking for a model flagged
    ANTHROPIC_HAIKU_STYLE_THINKING: map the chosen effort to a hardcoded budget
    (``HAIKU_THINKING_BUDGETS``) and inject ``thinking:{type:"enabled", budget_tokens:N}``.

    `none` / no effort / an unmapped value → return WITHOUT touching the payload:
    no `thinking`, no `output_config` — a completely clean Haiku body. Unlike the
    other Anthropic paths this NEVER sets `output_config.effort` (Haiku 400s on it).
    `budget_tokens` must be < `max_tokens` (Anthropic min 1024), so clamp to
    `max_tokens - 1` and skip thinking entirely if that drops below the 1024 floor."""
    budget = HAIKU_THINKING_BUDGETS.get(effort or "")
    if budget is None:
        return
    budget = min(budget, max_tokens - 1)
    if budget < 1024:
        return
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}


def _apply_anthropic_reasoning(
    payload: dict[str, Any],
    effort: str | None,
    caps: ReasoningCaps,
    body: dict[str, Any],
    display: str | None = None,
) -> bool:
    """Engage reasoning on the Anthropic Messages body — the shared dispatch for
    BOTH the direct + Bedrock transports. Returns True iff the effort-beta flag
    must be carried by the transport (header on direct / ``anthropic_beta`` body
    field on Bedrock) — the only wire-specific bit. A Haiku-style model is handled
    entirely here (enabled-thinking with a mapped budget; never output_config.effort
    and never a beta flag), so the haiku and effort/adaptive paths can't diverge
    between the two wires."""
    if caps.haiku_thinking:
        _apply_haiku_thinking(payload, effort, _request_max_tokens(body))
        return False
    _apply_anthropic_effort(payload, effort)
    _apply_anthropic_thinking(payload, effort, caps, display)
    return bool(effort and caps.thinking_mode == THINKING_MODE_EFFORT_BETA)


class AnthropicDirectTransport:
    """POSTs the native Anthropic Messages body to `api.anthropic.com` (or a
    base-URL override) and streams the SSE response back."""

    def __init__(
        self, *, base_url: str, api_key: str, anthropic_version: str, client: httpx.AsyncClient
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._version = anthropic_version
        self._client = client

    @asynccontextmanager
    async def stream(
        self,
        *,
        upstream_model_id: str,
        region: str | None,
        body: dict[str, Any],
        effort: str | None = None,
        caps: ReasoningCaps | None = None,
        display: str | None = None,
        meter: str | None = None,  # proxy-only; a direct provider call ignores it
        request_id: str | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        caps = caps or DEFAULT_CAPS
        payload = {**body, "model": upstream_model_id, "stream": True}
        needs_beta = _apply_anthropic_reasoning(payload, effort, caps, body, display)
        headers = {
            "anthropic-version": self._version,
            "content-type": "application/json",
        }
        # Opus 4.5: the effort beta flag rides in the `anthropic-beta` HEADER on
        # the direct wire (Bedrock has no header surface — it uses a body field).
        if needs_beta:
            headers["anthropic-beta"] = EFFORT_BETA_FLAG
        if self._api_key:
            # Omit the credential header entirely when unconfigured — sending an
            # empty "x-api-key" is malformed, not "anonymous".
            headers["x-api-key"] = self._api_key
        async with self._client.stream(
            "POST", f"{self._base_url}/v1/messages", json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                await _raise_upstream(response)
            yield response.aiter_bytes()


# Alkera's hosted-gateway ingress paths, keyed by wire protocol.
_PROXY_INGRESS = {"anthropic": "/anthropic/v1/messages", "openai": "/openai/v1/responses"}


class ProxyUpstreamTransport:
    """A THIN pass-through to Alkera's HOSTED gateway, authenticated with an
    org-scoped proxy token. Used by a self-hosted gateway in
    ``GATEWAY_UPSTREAM=proxy`` mode: the local gateway still authenticates the user
    + meters them against the local ledger; only the upstream call leaves the VPC —
    to Alkera, never the raw provider.

    Forwarding is deliberately faithful: the caller passes the client's ORIGINAL
    model string (``<id>::<effort>::<display>``, untouched) as ``upstream_model_id``
    and we forward the client's body verbatim, adding only ``stream=true`` + the
    auth / thinking-display headers. Alkera's gateway re-splits the model string and
    applies the provider-specific reasoning + routing itself — so this transport
    translates NOTHING (that translation seam is the bug class we're avoiding).
    ``effort`` / ``caps`` are accepted for interface parity but unused (the effort is
    already inside the model string); ``region`` is provider-specific and dropped
    (Alkera picks the upstream region)."""

    def __init__(self, *, base_url: str, token: str, wire: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._wire = wire
        self._client = client

    def hop_key(self, request_id: str) -> str:
        """The idempotency key this deployment sends upstream for ``request_id``.

        The same on every attempt of one request, so the upstream gateway refuses
        a retried hop it already admitted instead of billing the prompt twice.
        Derived with the deployment's proxy token rather than forwarded raw, so
        two deployments' request ids never meet in the upstream's request table.
        """
        digest = hashlib.sha256(f"{self._token}\n{request_id}".encode()).hexdigest()
        return f"hop-{digest}"

    @asynccontextmanager
    async def stream(
        self,
        *,
        upstream_model_id: str,
        region: str | None,
        body: dict[str, Any],
        effort: str | None = None,
        caps: ReasoningCaps | None = None,
        display: str | None = None,
        meter: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        # Thin: forward the client's body as-is with the original model string
        # untouched, only forcing the stream flag. Alkera re-splits + routes.
        payload = {**body, "model": upstream_model_id, "stream": True}
        headers = {
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }
        if display:
            headers[THINKING_DISPLAY_HEADER] = display
        # Carry the self-hosted billing meter across the trust boundary — it's the only
        # channel the enterprise-vs-additional split reaches Alkera on.
        if meter:
            headers[USAGE_METER_HEADER] = meter
        if request_id:
            headers[IDEMPOTENCY_KEY_HEADER] = self.hop_key(request_id)
        path = _PROXY_INGRESS[self._wire]
        async with self._client.stream(
            "POST", f"{self._base_url}{path}", json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                await _raise_upstream(response)
            yield response.aiter_bytes()


# Map Bedrock exception codes to the HTTP status we surface. Transient codes get
# a retryable status (429/5xx → `UpstreamError.retryable`); genuine client errors
# (ValidationException, AccessDeniedException, ResourceNotFoundException, …) fall
# through to 400 (terminal). This keeps the status semantically honest AND drives
# the retry/failover decision off it — a server fault is 5xx (retried), not 400.
_BEDROCK_STATUS = {
    "ThrottlingException": 429,
    "ModelNotReadyException": 503,
    "ServiceUnavailableException": 503,
    "InternalServerException": 500,
    "ModelTimeoutException": 504,
}

#: An ARN — and the 12-digit AWS account id inside it — names the gateway's OWN
#: infrastructure. botocore renders the full caller ARN into the message of an
#: authorization failure ("User: arn:aws:sts::<account>:assumed-role/<role>/<id> is
#: not authorized to ..."), and that message is relayed to the client as the
#: upstream error body, which would hand any authenticated caller the account
#: number and the exact IAM role to target.
_AWS_ARN_RE = re.compile(r"arn:aws[a-z0-9-]*:[^\s\"']+")
_AWS_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _scrub_aws_identifiers(text: str) -> str:
    """Redact AWS ARNs + account ids from provider error text, keeping the rest of
    the wording intact — the context-overflow classifier reads that wording (Bedrock
    signals an over-long prompt only in prose), so replacing the whole message would
    cost the client its auto-compaction signal."""
    return _AWS_ACCOUNT_ID_RE.sub("[redacted]", _AWS_ARN_RE.sub("[redacted-arn]", text))


class BedrockInvokeTransport:
    """Calls Bedrock ``InvokeModelWithResponseStream`` with the **native
    Anthropic Messages body**, de-frames the AWS event stream, and re-emits
    Anthropic SSE bytes — so the same codec/usage-parser and downstream relay
    work unchanged.

    `client_factory` returns an async context manager yielding a
    ``bedrock-runtime`` client (aioboto3 in prod; a fake in tests), so the
    Bedrock-specific logic is exercised without AWS.
    """

    def __init__(self, *, client_factory: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._client_factory = client_factory

    @asynccontextmanager
    async def stream(
        self,
        *,
        upstream_model_id: str,
        region: str | None,
        body: dict[str, Any],
        effort: str | None = None,
        caps: ReasoningCaps | None = None,
        display: str | None = None,
        meter: str | None = None,  # proxy-only; a direct provider call ignores it
        request_id: str | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        caps = caps or DEFAULT_CAPS
        payload = {k: v for k, v in body.items() if k != "model"}
        payload["anthropic_version"] = "bedrock-2023-05-31"
        payload.pop("stream", None)  # the response-stream API is inherently streaming
        # Bedrock speaks the Anthropic Messages wire. The effort beta flag (Opus
        # 4.5) rides in the BODY (`anthropic_beta`) here, since invoke_model has no
        # header surface (without it Bedrock 400s "output_config.effort not permitted").
        if _apply_anthropic_reasoning(payload, effort, caps, body, display):
            betas = payload.get("anthropic_beta")
            betas = list(betas) if isinstance(betas, list) else []
            if EFFORT_BETA_FLAG not in betas:
                betas.append(EFFORT_BETA_FLAG)
            payload["anthropic_beta"] = betas
        try:
            async with self._client_factory() as client:
                response = await client.invoke_model_with_response_stream(
                    modelId=upstream_model_id,
                    body=json.dumps(payload).encode(),
                    contentType="application/json",
                    accept="application/json",
                )
                yield _bedrock_events_to_sse(response["body"])
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = _BEDROCK_STATUS.get(code, 400)
            # botocore surfaces the HTTP response headers (lowercased) here —
            # the only place this non-httpx transport can read Retry-After.
            headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            # The pipeline relays this body to the caller, so the exception text
            # goes out scrubbed of our own AWS identifiers; the unredacted line
            # stays in the server log where operators need it.
            log.warning("gateway.bedrock.client_error", code=code, error=str(exc))
            body = {
                "error": {
                    "type": code or "bedrock_error",
                    "message": _scrub_aws_identifiers(str(exc)),
                }
            }
            raise UpstreamError(
                status,
                json.dumps(body).encode(),
                retry_after=_parse_retry_after(headers.get("retry-after")),
            ) from exc


async def _bedrock_events_to_sse(event_stream: Any) -> AsyncIterator[bytes]:
    """Each Bedrock chunk's bytes are a native Anthropic event JSON; re-frame
    them as ``event: <type>\\ndata: <json>\\n\\n`` SSE."""
    async for event in event_stream:
        chunk = event.get("chunk") if isinstance(event, dict) else None
        if not chunk:
            continue
        raw = chunk.get("bytes")
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        event_type = obj.get("type", "message")
        yield f"event: {event_type}\ndata: {json.dumps(obj)}\n\n".encode()


class _ClientBearerToken:
    """A token provider bound to ONE botocore session.

    botocore's default chain reads the Bedrock bearer from the process-wide
    ``AWS_BEARER_TOKEN_BEDROCK`` env var, which every client in the process
    would then share. Registering this provider on the session a single client
    is built from keeps the token on that client alone."""

    def __init__(self, token: str) -> None:
        self._token = FrozenAuthToken(token)

    def load_token(self, **_kwargs: Any) -> FrozenAuthToken:
        return self._token


def aioboto3_bedrock_client_factory(
    *,
    region: str,
    endpoint_url: str | None = None,
    read_timeout: int | None = None,
    bearer_token: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """Production `client_factory`: a fresh aioboto3 bedrock-runtime client with
    a long read timeout (Claude's inference timeout far exceeds boto's 60s
    default) and adaptive retries.

    ``read_timeout`` is read from ``gateway_bedrock_read_timeout_seconds`` when
    the caller names none, and at call time rather than at import, so the setting
    is what the client is built with.

    Every client's auth is bound to that client and nothing else. The process
    environment is never written, and the signature version is always pinned
    in code, so a bearer token in the environment (``AWS_BEARER_TOKEN_BEDROCK``,
    which botocore otherwise prefers over SigV4) can never sign a request the
    caller meant to sign with other credentials:
      1. ``bearer_token`` — a Bedrock API key, held by this client's own
         session token provider; the request is signed ``Bearer``.
      2. otherwise SigV4, with the explicit IAM access-key pair when given, or
         the standard credential chain (the IAM task role in prod).
    """

    def factory() -> AbstractAsyncContextManager[Any]:
        import aioboto3
        import aiobotocore.session
        from botocore.config import Config

        seconds = (
            read_timeout
            if read_timeout is not None
            else settings.gateway_bedrock_read_timeout_seconds
        )
        botocore_session = aiobotocore.session.get_session()
        creds: dict[str, str] = {}
        if bearer_token:
            botocore_session.register_component("token_provider", _ClientBearerToken(bearer_token))
            signature_version = "bearer"
        else:
            signature_version = "v4"
            if aws_access_key_id and aws_secret_access_key:
                creds["aws_access_key_id"] = aws_access_key_id
                creds["aws_secret_access_key"] = aws_secret_access_key
                if aws_session_token:
                    creds["aws_session_token"] = aws_session_token
        session = aioboto3.Session(botocore_session=botocore_session)
        return cast(
            "AbstractAsyncContextManager[Any]",
            session.client(
                "bedrock-runtime",
                region_name=region,
                endpoint_url=endpoint_url,
                config=Config(
                    read_timeout=seconds,
                    retries={"mode": "adaptive"},
                    signature_version=signature_version,
                ),
                **creds,
            ),
        )

    return factory


class OpenAIResponsesTransport:
    """POSTs the native OpenAI **Responses API** body to ``api.openai.com/v1/responses``
    (or a base-URL override) and streams the SSE response back.

    A faithful proxy: the body opencode's ``@ai-sdk/openai`` sends is forwarded
    untouched EXCEPT for the gateway's invariants:
      - ``store=false`` — ZDR (no server-side retention by OpenAI).
      - ``include`` carries ``reasoning.encrypted_content`` — so the model's
        encrypted reasoning item comes back and opencode can ship it forward next
        turn (stateless reasoning CONTINUITY, the whole point of the Responses wire).
      - the catalog-selected ``::effort`` is injected into ``reasoning.effort``.
    Nothing is stripped (unlike the retired chat-completions path).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        organization_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._organization_id = organization_id
        self._client = client

    @asynccontextmanager
    async def stream(
        self,
        *,
        upstream_model_id: str,
        region: str | None,
        body: dict[str, Any],
        effort: str | None = None,
        caps: ReasoningCaps | None = None,
        display: str | None = None,  # OpenAI surfaces reasoning summaries natively; ignored here
        meter: str | None = None,  # proxy-only; a direct provider call ignores it
        request_id: str | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        payload = {**body, "model": upstream_model_id, "stream": True, "store": False}
        # Ensure the encrypted reasoning item is returned (ZDR-safe continuity).
        existing_include = payload.get("include")
        include = list(existing_include) if isinstance(existing_include, list) else []
        if "reasoning.encrypted_content" not in include:
            include.append("reasoning.encrypted_content")
        payload["include"] = include
        # Inject the catalog effort into reasoning.effort (preserve summary etc.).
        if effort:
            existing_reasoning = payload.get("reasoning")
            reasoning = dict(existing_reasoning) if isinstance(existing_reasoning, dict) else {}
            reasoning["effort"] = effort
            payload["reasoning"] = reasoning
        headers = {"content-type": "application/json"}
        if self._api_key:
            # Omit the credential header entirely when unconfigured — sending an
            # empty "Bearer " value is malformed (httpcore rejects it outright).
            headers["authorization"] = f"Bearer {self._api_key}"
        if self._organization_id:
            # BYOK orgs may pin the OpenAI organization the key bills under.
            headers["OpenAI-Organization"] = self._organization_id
        async with self._client.stream(
            "POST", f"{self._base_url}/responses", json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                await _raise_upstream(response)
            yield response.aiter_bytes()
