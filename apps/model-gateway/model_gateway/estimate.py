"""How many input tokens a request body is worth — the input half of the hold.

The admission hold is charged against the caller's balance *before* the request
runs, so the estimate feeding it decides who gets served. Two failure modes
bound it from either side:

* **Under-estimating** lets a request settle far past what it reserved. The
  settle's overdraft clamp bounds the damage per request, but a systematically
  low estimate turns admission control off.
* **Over-estimating** refuses a request the account can afford. A flat
  ``chars // 3`` divisor over the whole body does exactly that: measured against
  the providers' own reported ``input_tokens`` it runs a third to three-quarters
  high, so a genuinely funded caller is 402'd on a large prompt.

So the estimate is per provider family, and uses a real tokenizer where one is
available in-process. It is still an estimate on purpose: the alternative — the
provider's own count-tokens endpoint — is a network round trip on the admission
path, which would put a provider outage between a funded user and every request.

Calibration
-----------
``chars_per_token`` and ``tool_overhead_tokens`` below were measured ONCE from
the recorded real provider interactions under
``vendor/opencode/packages/llm/test/fixtures/recordings/`` — each recording pairs
a real request body with the ``input_tokens`` the provider reported for it. Over
those recordings, with the body measured by :func:`iter_body_text` (so attachment
payloads are already charged flat, not counted as base64):

===========  =========================  ==============================
family       chars per input token      tool-bearing request overhead
             (bulk-text recordings)
===========  =========================  ==============================
anthropic    4.46                       540-770 tokens beyond the body
                                        (Anthropic injects its own
                                        tool-use system prompt, which
                                        is billed but never shipped)
openai       5.74                       none measurable
===========  =========================  ==============================

The configured ratios sit BELOW the measured ones (3.6 and 4.0 against 4.46 and
5.74) so the estimate stays an over-estimate on ordinary prose while dropping
most of the excess the flat divisor carried: over the bulk-text recordings the
estimate went from 1.43x to 1.19x the real Anthropic bill and from 1.73x to
1.30x the real OpenAI one. The tool overhead is rounded up to the next power of
two above the largest measurement. Every recorded case is still covered — the
estimate is never under what the provider charged.

Recordings age, so the calibration is also checked against a live bill: the
``live_provider`` cases of the gateway's live tests
send one real ~24 KB turn per family and assert the hold lands between the
provider's own figure and 1.2x it. Measured Sep 2026, with no tokenizer loaded
(so these are the fallback ratios, the weaker of the two paths):

===========  ==========  ========  ======
family       estimated   billed    ratio
===========  ==========  ========  ======
anthropic    6467        5464      1.184x
openai       5819        5122      1.136x
bedrock      6467        5464      1.184x
===========  ==========  ========  ======

Re-measure with ``scripts/calibrate_token_estimate.py`` when the recordings are
refreshed or a family is added; the numbers are data, not tuning knobs.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import ceil
from typing import Any

from alkera_core.config import settings
from alkera_core.logging import get_logger

log = get_logger(__name__)

#: What an inline binary attachment is worth, as ``(floor tokens, chars per
#: token of payload)`` keyed by the block type carrying it — the charge is the
#: LARGER of the floor and the encoded payload's length over the divisor.
#:
#: A provider prices an attachment by its CONTENT, not by the bytes that carried
#: it, so charging the raw base64 would over-hold by orders of magnitude and 402 a
#: funded user for one screenshot; charging nothing under-holds. The floor is that
#: content price (an image is ~1.6k tokens whatever it depicts; a page of PDF far
#: more), and the divisor keeps the estimate MONOTONE in what was actually
#: uploaded so a max-size document can't be held at a small-document price.
#:
#: Stated in TOKENS, not characters: they are a property of the provider's
#: pricing, not of whatever divisor the text side happens to use, so changing the
#: text calibration must not silently move them.
_IMAGE_ATTACHMENT_TOKENS = (2_000, 192)
_DOCUMENT_ATTACHMENT_TOKENS = (20_000, 12)

ATTACHMENT_TOKEN_CHARGES: dict[str, tuple[int, int]] = {
    "image": _IMAGE_ATTACHMENT_TOKENS,
    "image_url": _IMAGE_ATTACHMENT_TOKENS,
    "input_image": _IMAGE_ATTACHMENT_TOKENS,
    "audio": _IMAGE_ATTACHMENT_TOKENS,
    "input_audio": _IMAGE_ATTACHMENT_TOKENS,
    "document": _DOCUMENT_ATTACHMENT_TOKENS,
    "input_file": _DOCUMENT_ATTACHMENT_TOKENS,
}

#: Keys of a request body whose presence means the provider will also bill its own
#: tool-use scaffolding — see ``tool_overhead_tokens``.
_TOOL_KEYS = ("tools", "toolConfig")


@dataclass(frozen=True)
class FamilyCalibration:
    """One provider family's measured relationship between a body and its bill."""

    #: Characters of request text per input token the provider reports.
    chars_per_token: float
    #: Tokens the provider bills for a tool-bearing request beyond the body itself.
    tool_overhead_tokens: int
    #: tiktoken encodings to try, most current first. Empty when no in-process
    #: tokenizer models this family — the ratio is then the whole estimate.
    encodings: tuple[str, ...] = ()


#: The family a codec declares. Bedrock is deliberately absent: it serves the
#: Anthropic Messages wire with Anthropic's own tokenizer, so it calibrates as
#: ``anthropic``. A family with no entry falls back to ``_UNCALIBRATED``.
CALIBRATIONS: dict[str, FamilyCalibration] = {
    "anthropic": FamilyCalibration(chars_per_token=3.6, tool_overhead_tokens=1_024),
    "openai": FamilyCalibration(
        chars_per_token=4.0,
        tool_overhead_tokens=256,
        # o200k_base is the current-generation encoding (gpt-4o and later);
        # cl100k_base is the fallback for a deployment pinned to an older model.
        encodings=("o200k_base", "cl100k_base"),
    ),
}

#: The pre-calibration divisor, kept for a family nobody has measured: a blunt
#: over-estimate is the only safe guess when the relationship is unknown.
_UNCALIBRATED = FamilyCalibration(chars_per_token=3.0, tool_overhead_tokens=0)

#: Model-id prefixes that predate o200k_base. Matched against the request's own
#: ``model`` field, which may carry the gateway's ``::effort::display`` suffix.
_LEGACY_OPENAI_PREFIXES = ("gpt-4-", "gpt-4.", "gpt-3.5", "text-davinci")


#: Every encoding any calibration may ask for, in the order they are warmed.
ENCODING_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(name for c in CALIBRATIONS.values() for name in c.encodings)
)

#: Encodings this process has loaded, written ONLY by the warm-up's worker
#: threads and read by the request path. A name absent from the mapping was
#: never loaded; a name mapped to ``None`` was tried and could not be.
_LOADED: dict[str, Any | None] = {}
_LOADED_LOCK = threading.Lock()


def _apply_cache_dir() -> None:
    """Point tiktoken at the baked ranks, unless the environment already did.

    The image sets ``TIKTOKEN_CACHE_DIR`` itself; the setting is what a source
    checkout and a self-hosted install configure. An env var already present
    wins, so an operator can redirect the cache without touching settings."""
    configured = settings.gateway_tokenizer_cache_dir.strip()
    if configured and not os.environ.get("TIKTOKEN_CACHE_DIR"):
        os.environ["TIKTOKEN_CACHE_DIR"] = configured


def _load_encoding(name: str) -> Any | None:
    """Build one tiktoken encoding, or return None if this process cannot.

    Runs on a warm-up thread, NEVER on the request path: tiktoken fetches the
    BPE ranks over the network the first time an encoding is used and there is
    no timeout on that fetch, so an egress-restricted install would otherwise
    block the event loop for a TCP connect timeout on the first OpenAI-family
    request after every start. Both failure shapes — tiktoken absent, ranks
    unreachable — are recorded once and leave the estimator on its ratio."""
    try:
        import tiktoken
    except ImportError:
        log.info("gateway.estimate.tokenizer_absent", encoding=name)
        return None
    try:
        return tiktoken.get_encoding(name)
    except Exception as exc:
        log.warning("gateway.estimate.tokenizer_unavailable", encoding=name, error=str(exc))
        return None


def _warm_one(name: str) -> None:
    result = _load_encoding(name)
    with _LOADED_LOCK:
        _LOADED[name] = result


def warm_encodings(*, timeout: float | None = None) -> dict[str, bool]:
    """Load the token encodings before the process serves its first request.

    Called from the gateway lifespan. Each encoding is loaded on its own daemon
    thread and the call waits up to ``timeout`` (default
    ``GATEWAY_TOKENIZER_WARM_TIMEOUT_SECONDS``) for all of them. The deadline
    bounds the BOOT, not the load: a thread still running when it expires keeps
    going and publishes its encoding when it lands, so a slow mirror costs a few
    early requests their tokenizer rather than the whole process. A thread that
    never lands costs nothing — the estimator falls back to the family's
    calibrated chars-per-token ratio, which is the same path an install without
    tiktoken takes.

    Returns which encodings were ready when the wait ended. Never raises: a
    tokenizer is an accuracy improvement over the ratio, never a boot
    requirement."""
    _apply_cache_dir()
    deadline = settings.gateway_tokenizer_warm_timeout_seconds if timeout is None else timeout
    pending = [name for name in ENCODING_NAMES if name not in _LOADED]
    threads = [
        threading.Thread(target=_warm_one, args=(name,), name=f"tiktoken-warm-{name}", daemon=True)
        for name in pending
    ]
    for thread in threads:
        thread.start()
    # One shared deadline, not one per thread: the encodings load in parallel, so
    # waiting each out in turn would multiply the worst case by their number.
    ends_at = time.monotonic() + max(0.0, deadline)
    for thread in threads:
        thread.join(timeout=max(0.0, ends_at - time.monotonic()))
    with _LOADED_LOCK:
        ready = {name: _LOADED.get(name) is not None for name in ENCODING_NAMES}
    missing = sorted(name for name, ok in ready.items() if not ok)
    if missing:
        log.warning(
            "gateway.estimate.tokenizer_warm_incomplete",
            missing=missing,
            timeout_seconds=deadline,
        )
    else:
        log.info("gateway.estimate.tokenizer_warm_done", encodings=sorted(ready))
    return ready


def reset_encodings_for_tests() -> None:
    """Forget every warmed encoding (test isolation between cases)."""
    with _LOADED_LOCK:
        _LOADED.clear()


def _encoding(name: str) -> Any | None:
    """The encoding warmed for ``name``, or None when this process has none.

    A pure lookup on purpose. The admission path reads it once per request, and
    the load it used to do lazily could reach for the network — so a process
    that never warmed (a test, an import-only consumer, a boot whose ranks were
    unreachable) estimates by ratio instead of paying for a fetch here."""
    return _LOADED.get(name)


def _resolve_encoding(calibration: FamilyCalibration, model: str) -> Any | None:
    if not calibration.encodings:
        return None
    names = calibration.encodings
    if len(names) > 1 and model.startswith(_LEGACY_OPENAI_PREFIXES):
        names = names[1:]
    for name in names:
        enc = _encoding(name)
        if enc is not None:
            return enc
    return None


def iter_body_text(body: Any) -> Iterator[tuple[str, str | None]]:
    """Every billable string in a request body, with the attachment kind carrying it.

    The admission hold is the only thing keeping a request's settled cost near its
    reservation, so the walk must be monotone in every field the provider bills as
    input. Walking only ``messages[].content`` text misses the tool-schema array,
    ``tool_result`` / ``tool_use`` payloads and attachments — all of which bill as
    input tokens — which lets a tool-heavy request be admitted against a hold
    thousands of times smaller than it settles for.

    Every string is yielded with ``None`` EXCEPT one narrowly-scoped position: the
    payload slot of a declared binary attachment block, which yields that block's
    kind so the caller can apply the flat content charge. Scoping matters — a
    charge applied to any base64-*shaped* string would let an attacker relocate
    ordinary billable text behind it with a ``data:x;base64,`` prefix and pay
    nothing for it.

    Iterative (not recursive) so an adversarially deep body can't blow the stack.
    """
    stack: list[tuple[Any, str | None]] = [(body, None)]
    while stack:
        value, attachment = stack.pop()
        if isinstance(value, str):
            yield value, attachment
        elif isinstance(value, dict):
            declared = value.get("type")
            declared = declared if isinstance(declared, str) else None
            if attachment is not None and declared not in (None, *_BINARY_SOURCE_TYPES):
                attachment = None  # a text-bearing source inside an attachment slot
            kind = attachment or (declared if declared in ATTACHMENT_TOKEN_CHARGES else None)
            for key, item in value.items():
                yield key, None
                stack.append((item, kind if key in _ATTACHMENT_PAYLOAD_KEYS else None))
        elif isinstance(value, list):
            stack.extend((item, attachment) for item in value)
        elif value is None or isinstance(value, bool):
            # The JSON literal the provider actually tokenizes: "null"/"true" are
            # 4 characters, "false" 5. Close enough either way, and never zero.
            yield "null", None
        elif isinstance(value, int | float):
            yield str(value), None


#: Keys that hold an attachment payload inside a block. The attachment charge
#: applies ONLY in these positions: a string anywhere else is text the provider
#: bills per character, so payload can't be moved behind the charge by dressing it
#: up as a data URL or wrapping it in a base64-shaped dict.
_ATTACHMENT_PAYLOAD_KEYS = frozenset(
    {"audio", "data", "file_data", "file_url", "image_url", "input_audio", "source", "url"}
)

#: Source kinds that are genuinely binary. An attachment slot can also carry PLAIN
#: TEXT — Anthropic's ``{"type": "text"}`` / ``{"type": "content"}`` document source
#: puts a whole file's characters in ``data`` — which the provider tokenizes per
#: character, so those clear the flat charge and get counted in full.
_BINARY_SOURCE_TYPES = frozenset({"base64", "file", "file_id", "url"})


def _has_tools(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    return any(bool(body.get(key)) for key in _TOOL_KEYS)


def text_tokenizer(body: dict[str, Any], *, family: str) -> Callable[[str], int] | None:
    """The family's REAL tokenizer for this body's model, or None when none loaded.

    The one place an encoding is resolved, so every figure that has to agree with
    the admission hold — most of all the settle-time estimate for a stream that
    died before the provider reported usage — counts the same strings the same
    way. ``None`` means no encoding was available in this process, and the caller
    falls back to its own ratio.
    """
    calibration = CALIBRATIONS.get(family, _UNCALIBRATED)
    model = body.get("model") if isinstance(body, dict) else None
    enc = _resolve_encoding(calibration, model if isinstance(model, str) else "")
    if enc is None:
        return None
    return lambda text: len(enc.encode_ordinary(text))


def estimate_input_tokens(body: dict[str, Any], *, family: str) -> int:
    """Input tokens this body is estimated to bill, for the admission hold.

    Text goes through the family's tokenizer when one loaded, and through its
    measured characters-per-token ratio otherwise. Attachment payloads are
    charged flat in tokens (they are priced by content, not by carrier bytes),
    and a tool-bearing request adds the scaffolding the provider bills but the
    body never shows. Never returns zero — an unpriced request is an unbounded one.
    """
    calibration = CALIBRATIONS.get(family, _UNCALIBRATED)
    measure = text_tokenizer(body, family=family)
    if measure is None:
        ratio = calibration.chars_per_token
        measure = lambda text: ceil(len(text) / ratio)  # noqa: E731

    total = 0
    for text, attachment in iter_body_text(body):
        if attachment is None:
            total += measure(text)
            continue
        floor, divisor = ATTACHMENT_TOKEN_CHARGES[attachment]
        total += max(floor, len(text) // divisor)
    if _has_tools(body):
        total += calibration.tool_overhead_tokens
    return max(1, total)


def resolve_max_output_tokens(asked: Any, *, default: int | None, floor: int) -> int:
    """The output tokens to reserve: what the request asked for, else the model
    catalog's own maximum, else the codec's floor.

    A flat floor for every model reserves 4096 output tokens for a model that can
    emit sixteen times that, so a long answer settles past its own hold on every
    request. ``default`` is the catalog figure; ``0`` there means "not recorded",
    which is not a ceiling of zero."""
    if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0:
        return asked
    if default is not None and default > 0:
        return default
    return floor


__all__ = [
    "ATTACHMENT_TOKEN_CHARGES",
    "CALIBRATIONS",
    "ENCODING_NAMES",
    "FamilyCalibration",
    "estimate_input_tokens",
    "iter_body_text",
    "reset_encodings_for_tests",
    "resolve_max_output_tokens",
    "warm_encodings",
]
