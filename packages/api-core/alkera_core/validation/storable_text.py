"""Text a Postgres ``text`` column cannot hold, refused at the request boundary.

Postgres stores `text` as UTF-8 and has no representation for U+0000: a string
carrying one raises `asyncpg.exceptions.CharacterNotInRepertoireError`
(SQLSTATE 22021) the moment it reaches a comparison or an INSERT — deep inside a
service, far from anything that knows it was the caller's fault. Lone surrogates
(U+D800 to U+DFFF outside a pair) are the same class of value: Python holds them,
UTF-8 cannot encode them, and the driver fails encoding the parameter before the
server is ever asked.

Pydantic's `str` accepts both, so nothing in the request pipeline noticed. This
module is the noticing: pure functions over already-decoded values, no web
framework and no driver, so the ASGI middleware, the gateway's own body parser
and a unit test can all reach the same answer.

The scan is exact, not heuristic, and it costs the traffic that does not carry
either character nothing: `may_hold_unstorable` searches the raw bytes for the
two escapes that can decode to one — `\\u0000` and the surrogate range — so an
ordinary `ensure_ascii` payload full of `\\u00e9` accents is handed on without
being parsed at all. Only a body that trips it is decoded and walked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl

from alkera_core.observability.errors import ErrorCode

#: What a refusal says. No numbers, no code points, and never the value itself —
#: the offending string may be a password or a token.
UNSTORABLE_MESSAGE: Final = (
    "A value in the request contains characters that cannot be stored. "
    "Remove the null or unpaired-surrogate characters and try again."
)

#: The coded error a refusal carries on the API surface.
UNSTORABLE_CODE: Final = ErrorCode.unstorable_text

#: What a body nested deeper than the parser can follow is told. It is not a
#: character refusal — the body could not be read at all — so it carries the
#: ordinary validation code rather than the unstorable-text one.
NESTING_MESSAGE: Final = "The request body is nested too deeply to be read."
NESTING_CODE: Final = ErrorCode.validation_error

_NUL: Final = "\x00"
_SURROGATE_LOW: Final = 0xD800
_SURROGATE_HIGH: Final = 0xDFFF


class Reason:
    """Why one value was refused — reported, never the value."""

    NUL: Final = "null_character"
    SURROGATE: Final = "unpaired_surrogate"
    ENCODING: Final = "invalid_utf8"
    NESTING: Final = "excessive_nesting"


@dataclass(frozen=True, slots=True)
class Unstorable:
    """One refusal: where it was found and what the caller is told about it."""

    #: A dotted/bracketed location a client can act on — ``body.title``,
    #: ``query.type``, ``path.item_id``, ``header.idempotency-key``.
    location: str
    reason: str
    #: The coded error and the sentence a caller reads. Defaulted, because
    #: almost every refusal here is the same one; a body that could not be read
    #: at all carries its own so the client is not told to remove characters
    #: from a body nobody parsed.
    code: ErrorCode = field(default=UNSTORABLE_CODE)
    message: str = field(default=UNSTORABLE_MESSAGE)

    def details(self) -> dict[str, str]:
        return {"field": self.location, "reason": self.reason}


def reason_for(value: str) -> str | None:
    """Why ``value`` cannot be stored, or ``None`` when it can."""
    if _NUL in value:
        return Reason.NUL
    if _has_lone_surrogate(value):
        return Reason.SURROGATE
    return None


#: What `storable` puts where a character Postgres cannot hold used to be.
REPLACEMENT_CHARACTER: Final = "\ufffd"


def storable(value: str, *, limit: int) -> str:
    """``value`` as a record can always hold it: never refused, never too long.

    For text the server writes about a request rather than on its behalf — an
    audit row naming the path that was asked for, the id a caller probed. Such
    a record must land whatever the caller sent, so instead of refusing, each
    NUL and surrogate becomes U+FFFD and the result is cut to ``limit``
    characters.
    """
    if reason_for(value):
        value = "".join(
            REPLACEMENT_CHARACTER
            if ch == _NUL or _SURROGATE_LOW <= ord(ch) <= _SURROGATE_HIGH
            else ch
            for ch in value
        )
    return value[:limit]


def _has_lone_surrogate(value: str) -> bool:
    """Whether ``value`` holds a surrogate code point.

    Any surrogate at all, paired or not: a Python `str` that round-trips
    through JSON keeps `"\\ud83d\\ude00"` as the single astral character, so a
    surrogate still present after decoding is by construction one the decoder
    could not pair — exactly the value `.encode("utf-8")` refuses.
    """
    return any(_SURROGATE_LOW <= ord(ch) <= _SURROGATE_HIGH for ch in value)


#: How deeply a body may nest and still be checked. The JSON parser recurses
#: once per level, and this scan runs before routing on an unauthenticated
#: request, so the depth a 2 KB body can reach (thousands) has to be refused
#: rather than attempted. Two orders of magnitude above anything the API
#: accepts — the deepest request schema in the app nests single digits — so a
#: body past it is a generator, not a client.
MAX_SCAN_DEPTH: Final = 200

#: Possessive, so the count stays linear on a hostile body: the alternation
#: cannot match an unescaped quote, so there is never anything to give back.
_STRINGS: Final = re.compile(rb'"(?:[^"\\]|\\.)*+"', re.S)
_NOT_BRACKETS: Final = re.compile(rb"[^\[\]{}]+")
_OPENERS: Final = frozenset(b"[{")
_CLOSERS: Final = frozenset(b"]}")


def _nests_deeper_than(raw: bytes, limit: int) -> bool:
    """Whether these bytes open more than ``limit`` containers at once.

    Counted on the raw bytes, before the parser is asked to follow them, with
    the strings removed first so a bracket inside one is not mistaken for
    structure. Over-counting a body that is not valid JSON costs nothing: such
    a body is not refused here at all, it is handed to the route's own parser.
    """
    structure = _NOT_BRACKETS.sub(b"", _STRINGS.sub(b"", raw))
    if len(structure) <= limit:
        return False
    depth = 0
    for byte in structure:
        if byte in _OPENERS:
            depth += 1
            if depth > limit:
                return True
        elif byte in _CLOSERS:
            depth -= 1
    return False


#: The only JSON escapes that can decode to a character Postgres cannot hold:
#: the escaped NUL, and the surrogate block U+D800 to U+DFFF (hex d800-dfff).
#: Matched in one C-level pass so a payload full of the ordinary escapes every
#: `ensure_ascii` writer emits is never parsed on their account.
_UNSTORABLE_ESCAPE: Final = re.compile(rb"\\u(?:0000|[dD][89a-fA-F][0-9a-fA-F]{2})")


def may_hold_unstorable(raw: bytes) -> bool:
    """Whether these bytes can possibly decode to a character Postgres refuses.

    A NUL arrives either as the byte itself or as a `\\u0000` escape; a
    surrogate only ever as a `\\uD8xx`-class escape. A payload with neither —
    nearly all of them, non-ASCII ones included — skips the parse entirely.
    Over-approximating is safe (a surrogate PAIR trips it, and the walk then
    correctly admits the emoji it decodes to); under-approximating is not.
    """
    return b"\x00" in raw or _UNSTORABLE_ESCAPE.search(raw) is not None


@dataclass(frozen=True, slots=True)
class SelfValidated:
    """The parts of a request one surface refuses in its own vocabulary.

    A surface that validates a value itself answers with a code its clients act
    on, so the boundary scan defers on exactly what is declared here and still
    checks the rest of the request. Declaring a part is a claim that the surface
    handles hostile text there; a value it misses still meets the driver-level
    refusal, so a wrong declaration costs a better message, never a 500.
    """

    #: The path prefix the surface is mounted at, matched on whole segments.
    prefix: str
    path: bool = False
    query: bool = False
    headers: bool = False
    #: The whole JSON body, whatever it holds.
    body: bool = False
    #: Body locations the surface validates itself, spelled as the scan reports
    #: them with list indexes left blank (``body.name``, ``body.items[].name``),
    #: each with the reasons (:class:`Reason`) it refuses in its own words. A
    #: location that names a container covers everything under it. A reason the
    #: surface does not name there is still the scan's to refuse.
    body_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def whole(cls, prefix: str) -> SelfValidated:
        """A surface that answers for every part of its requests."""
        return cls(prefix, path=True, query=True, headers=True, body=True)

    @property
    def everything(self) -> bool:
        return self.path and self.query and self.headers and self.body

    def covers(self, path: str) -> bool:
        """Whether ``path`` is under this surface. Whole segments only, so a
        prefix cannot capture a sibling that merely starts the same way."""
        return path == self.prefix or path.startswith(f"{self.prefix}/")


_LIST_INDEX: Final = re.compile(r"\[\d+\]")


def field_location(where: str) -> str:
    """A scan location with its list indexes left blank, the spelling
    :attr:`SelfValidated.body_fields` declares: ``body.items[3].name`` is
    ``body.items[].name``."""
    return _LIST_INDEX.sub("[]", where)


#: Every reason a character can be refused for, for a declaration that
#: answers for all of them.
CHARACTER_REASONS: Final = frozenset({Reason.NUL, Reason.SURROGATE})


def _reasons(value: str) -> list[str]:
    """Every reason ``value`` cannot be stored, in the order they are named."""
    found = []
    if _NUL in value:
        found.append(Reason.NUL)
    if _has_lone_surrogate(value):
        found.append(Reason.SURROGATE)
    return found


def scan_value(
    value: Any, location: str, *, deferred: Mapping[str, Collection[str]] | None = None
) -> Unstorable | None:
    """The first unstorable string at or under ``value``, in document order.

    Walks mappings (keys as well as values — a key becomes a column value in a
    JSONB document) and sequences. Non-string leaves are skipped. At or under a
    ``deferred`` location, a string refused only for reasons that location
    names is skipped too: the surface that declared it answers for those.

    Iterative on purpose: the walk runs before routing on every request of both
    apps, so a hostile body must cost it a bounded amount of stack. Recursing
    made a body nested a thousand deep — 2 KB, no credentials needed — a
    `RecursionError` the catch-all could only answer with a 500.
    """
    claims = deferred or {}
    nothing: frozenset[str] = frozenset()
    stack: list[tuple[Any, str, frozenset[str]]] = [(value, location, nothing)]
    while stack:
        current, where, owned = stack.pop()
        if claims:
            owned = owned | frozenset(claims.get(field_location(where), ()))
        if isinstance(current, str):
            refused = _first_not_owned(current, owned)
            if refused:
                return Unstorable(where, refused)
        elif isinstance(current, Mapping):
            for key, item in reversed(list(current.items())):
                if isinstance(key, str):
                    refused = _first_not_owned(key, owned)
                    if refused:
                        return Unstorable(f"{where}.<key>", refused)
                stack.append((item, f"{where}.{key}" if where else str(key), owned))
        elif isinstance(current, (bytes, bytearray)):
            continue
        elif isinstance(current, Sequence):
            for index, item in reversed(list(enumerate(current))):
                stack.append((item, f"{where}[{index}]", owned))
    return None


def _first_not_owned(value: str, owned: frozenset[str]) -> str | None:
    return next((reason for reason in _reasons(value) if reason not in owned), None)


def scan_json_body(
    raw: bytes,
    *,
    root: str = "body",
    max_depth: int = MAX_SCAN_DEPTH,
    deferred: Mapping[str, Collection[str]] | None = None,
) -> Unstorable | None:
    """Refuse a JSON request body that carries a character Postgres cannot store.

    A body that is not JSON — binary, truncated, or not even UTF-8 — is NOT
    this function's business: the route's own parser answers it in its own
    vocabulary, and telling a client that compressed its body to "remove the
    null characters" replaces a true diagnosis with a misleading one. Nothing
    is lost by standing aside, because no parser downstream will turn such a
    body into stored text.

    The one input that cannot be checked is a body nested deeper than the
    parser will follow. That one is refused — an answer the caller can act on —
    rather than attempted, because attempting it exhausts the stack.
    """
    if not may_hold_unstorable(raw):
        return None
    # Decoded before the depth is counted: bytes that are not UTF-8 (a
    # compressed body above all) are not JSON, and the brackets that turn up
    # in them by chance are not structure.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    too_deep = Unstorable(root, Reason.NESTING, code=NESTING_CODE, message=NESTING_MESSAGE)
    if _nests_deeper_than(raw, max_depth):
        return too_deep
    try:
        parsed = json.loads(text)
    except RecursionError:  # pragma: no cover - the depth bound answers first
        # Kept because the bound above is counted on the bytes while this one
        # is the parser's own budget: a shallower stack than the check assumed
        # must still produce an answer rather than an unauthenticated 500.
        return too_deep
    except ValueError:
        return None
    return scan_value(parsed, root, deferred=deferred)


def scan_query_string(query_string: bytes, *, root: str = "query") -> Unstorable | None:
    """Refuse a query string whose decoded keys or values cannot be stored.

    Decoded the way Starlette decodes it (latin-1 then percent-decoding), so
    what is checked is what the endpoint will read.
    """
    if not query_string:
        return None
    try:
        pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return Unstorable(root, Reason.ENCODING)
    for key, value in pairs:
        reason = reason_for(key)
        if reason:
            return Unstorable(f"{root}.<name>", reason)
        reason = reason_for(value)
        if reason:
            return Unstorable(f"{root}.{key}", reason)
    return None


def scan_path(path: str, *, root: str = "path") -> Unstorable | None:
    """Refuse a request path whose decoded segments cannot be stored.

    Invalid UTF-8 never reaches here as a surrogate — the server decodes the
    path with `errors="replace"` — so in practice this catches `%00`.
    """
    reason = reason_for(path)
    return Unstorable(root, reason) if reason else None


def scan_headers(
    headers: Iterable[tuple[bytes, bytes]], *, root: str = "header"
) -> Unstorable | None:
    """Refuse a header value carrying a NUL.

    HTTP framing already forbids it, so on a real socket this never fires — but
    an in-process ASGI caller (the daemon, a test, the extension bridge) writes
    the header list directly, and several of these headers are persisted
    verbatim (the idempotency key, the agent assertion).
    """
    for name, value in headers:
        if b"\x00" in value:
            return Unstorable(f"{root}.{name.decode('latin-1', 'replace')}", Reason.NUL)
    return None


__all__ = [
    "CHARACTER_REASONS",
    "MAX_SCAN_DEPTH",
    "NESTING_CODE",
    "NESTING_MESSAGE",
    "REPLACEMENT_CHARACTER",
    "UNSTORABLE_CODE",
    "UNSTORABLE_MESSAGE",
    "Reason",
    "SelfValidated",
    "Unstorable",
    "field_location",
    "may_hold_unstorable",
    "reason_for",
    "scan_headers",
    "scan_json_body",
    "scan_path",
    "scan_query_string",
    "scan_value",
    "storable",
]
