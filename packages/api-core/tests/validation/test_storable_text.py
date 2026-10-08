"""Unit cover for the storable-text scanners.

Pure data in, pure answer out — no app, no driver. The route-level proof that
nothing 5xxes lives in `apps/backend/tests/test_unstorable_text_surface.py`.
"""

from __future__ import annotations

import json

import pytest
from alkera_core.validation import storable_text
from alkera_core.validation.storable_text import (
    MAX_SCAN_DEPTH,
    NESTING_CODE,
    NESTING_MESSAGE,
    Reason,
    may_hold_unstorable,
    reason_for,
    scan_headers,
    scan_json_body,
    scan_path,
    scan_query_string,
    scan_value,
    storable,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("plain", None, id="plain"),
        pytest.param("", None, id="empty"),
        pytest.param("naïve — 日本語 🙂", None, id="astral-and-accents-are-storable"),
        pytest.param("a\x00b", Reason.NUL, id="embedded-nul"),
        pytest.param("\x00", Reason.NUL, id="bare-nul"),
        pytest.param("tail\x00", Reason.NUL, id="trailing-nul"),
        pytest.param("\ud800", Reason.SURROGATE, id="lone-high-surrogate"),
        pytest.param("\udfff", Reason.SURROGATE, id="lone-low-surrogate"),
        pytest.param("ok\ud83d", Reason.SURROGATE, id="truncated-pair"),
        # The characters next to the forbidden ranges must stay storable.
        pytest.param("\u0001", None, id="control-char-is-fine"),
        pytest.param("퟿", None, id="just-below-surrogate-range"),
        pytest.param("", None, id="just-above-surrogate-range"),
    ],
)
def test_reason_for_names_only_what_postgres_refuses(value: str, expected: str | None) -> None:
    assert reason_for(value) == expected


def test_storable_strings_survive_a_postgres_roundtrip_encode() -> None:
    """The predicate agrees with the thing it stands in for: anything it calls
    storable must actually encode to UTF-8, which is what the driver does."""
    for value in ("plain", "naïve — 日本語 🙂", "\u0001", "퟿", ""):
        assert reason_for(value) is None
        value.encode("utf-8")
    for value in ("a\x00b", "\ud800", "ok\ud83d"):
        if reason_for(value) == Reason.SURROGATE:
            with pytest.raises(UnicodeEncodeError):
                value.encode("utf-8")


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        pytest.param({"title": "a\x00b"}, "body.title", id="top-level-field"),
        pytest.param({"a": {"b": {"c": "\x00"}}}, "body.a.b.c", id="nested-field"),
        pytest.param({"xs": ["ok", "no\x00"]}, "body.xs[1]", id="inside-a-list"),
        pytest.param({"xs": [{"k": "\x00"}]}, "body.xs[0].k", id="object-in-a-list"),
        pytest.param({"a\x00": "v"}, "body.<key>", id="the-key-itself"),
        pytest.param({"title": "\ud800"}, "body.title", id="lone-surrogate-field"),
    ],
)
def test_scan_json_body_names_the_offending_field(
    payload: dict[str, object], location: str
) -> None:
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    found = scan_json_body(raw)
    assert found is not None
    assert found.location == location


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"title": "hello"}, id="plain"),
        pytest.param({"emoji": "🙂", "accents": "naïve"}, id="astral-escapes-are-a-pair"),
        pytest.param({"escaped": "line\nbreak\ttab \\u not-an-escape"}, id="literal-backslash-u"),
        pytest.param({"nested": {"xs": [1, 2, None, True]}}, id="non-strings"),
    ],
)
def test_scan_json_body_admits_storable_payloads(payload: dict[str, object]) -> None:
    assert scan_json_body(json.dumps(payload).encode("utf-8")) is None
    # …including when the writer escapes every non-ASCII character, which is the
    # form that makes an emoji arrive as a surrogate PAIR.
    assert scan_json_body(json.dumps(payload, ensure_ascii=True).encode("utf-8")) is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{not json", id="not-json-at-all"),
        pytest.param(b"not json at all\x00", id="not-json-carrying-a-nul"),
        pytest.param(b'{"a": "\x00\xff\xfe"}', id="not-even-utf8"),
        pytest.param(b"\x1f\x8b\x08\x00garbage", id="a-body-somebody-compressed"),
        pytest.param(
            b"\x1f\x8b\x08\x00" + b"[{" * MAX_SCAN_DEPTH + b"\x00",
            id="a-compressed-body-whose-bytes-hold-brackets-past-the-depth-bound",
        ),
        pytest.param(b'{"model": "m"\x00', id="truncated"),
    ],
)
def test_scan_json_body_leaves_a_body_it_cannot_parse_to_the_route(raw: bytes) -> None:
    """A body that is not JSON is not this scanner's to refuse.

    Every one of these is rejected downstream by the parser that owns the
    vocabulary the client is written against ("body must be valid JSON"), and
    none of them can become stored text on the way. Answering "remove the null
    characters" for a body that was merely compressed replaces a true
    diagnosis with a misleading one.
    """
    assert scan_json_body(raw) is None


def _nested(depth: int, leaf: str = '"x\\u0000"') -> bytes:
    return ('{"a":' * depth + leaf + "}" * depth).encode()


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(MAX_SCAN_DEPTH + 1, id="one-past-the-bound"),
        pytest.param(1000, id="past-the-parsers-own-budget"),
        pytest.param(20_000, id="far-past-everything"),
    ],
)
def test_scan_json_body_refuses_a_body_nested_past_the_bound(depth: int) -> None:
    """The one input that cannot be checked gets an answer, not an exception.

    The JSON parser recurses once per level, and this scan runs before routing
    on an unauthenticated request — so a body the parser cannot follow was a
    `RecursionError` on a 2 KB payload, i.e. exactly the 500 the boundary
    exists to remove. The bound is counted on the bytes, so where it refuses
    does not depend on how much stack the caller happened to leave.
    """
    found = scan_json_body(_nested(depth))
    assert found is not None
    assert found.reason == Reason.NESTING
    assert found.code == NESTING_CODE
    assert found.message == NESTING_MESSAGE
    assert found.location == "body"


def test_a_body_at_the_bound_is_still_checked() -> None:
    """The refusal starts exactly one level past the bound, not before it — a
    body at the limit is walked, poison and all."""
    found = scan_json_body(_nested(MAX_SCAN_DEPTH))
    assert found is not None
    assert found.reason == Reason.NUL
    assert scan_json_body(_nested(MAX_SCAN_DEPTH, '"clean"')) is None


@pytest.mark.parametrize(
    "leaf",
    [
        pytest.param('"[[[\\u0000" ', id="brackets-inside-a-string"),
        pytest.param('"\\"}]]\\u0000"', id="escaped-quotes-and-closers"),
    ],
)
def test_the_depth_bound_does_not_count_brackets_inside_strings(leaf: str) -> None:
    """A shallow body whose CONTENT is full of brackets — a code snippet, a
    serialized tree — is not mistaken for a deep one."""
    found = scan_json_body(('{"a":' * 5 + leaf + "}" * 5).encode())
    assert found is not None
    assert found.reason == Reason.NUL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(b'{"a": "plain"}', False, id="ascii"),
        pytest.param(b'{"a": "na\\u00efve \\u65e5\\u672c"}', False, id="ordinary-escapes"),
        pytest.param(b'{"a": "\\ud7ff"}', False, id="just-below-the-surrogates"),
        pytest.param(b'{"a": "\\ue000"}', False, id="just-above-the-surrogates"),
        pytest.param(b'{"a": "\\u0000"}', True, id="escaped-nul"),
        pytest.param(b'{"a": "\\ud800"}', True, id="upper-case-surrogate"),
        pytest.param(b'{"a": "\\udfff"}', True, id="lower-case-surrogate"),
        pytest.param(b'{"a": "\x00"}', True, id="raw-nul"),
    ],
)
def test_only_an_escape_that_can_decode_to_a_bad_character_is_worth_parsing(
    raw: bytes, expected: bool
) -> None:
    """The pre-filter is what keeps the scan off the critical path.

    It runs on every JSON body of both apps, so a filter that trips on any
    `\\u` escape parses and walks every payload an `ensure_ascii` writer emits
    — accents and CJK included — for nothing.
    """
    assert may_hold_unstorable(raw) is expected


def test_an_ordinary_escaped_body_is_never_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The saving is real, not theoretical: the parse never happens."""

    def _must_not_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a body with no suspect escape must not be parsed")

    monkeypatch.setattr(storable_text.json, "loads", _must_not_parse)
    assert scan_json_body(json.dumps({"a": "naïve 日本語"}, ensure_ascii=True).encode()) is None
    # …and the guard is not vacuous: a body that DOES carry one is parsed.
    with pytest.raises(AssertionError):
        scan_json_body(b'{"a": "\\u0000"}')


@pytest.mark.parametrize(
    ("query", "location"),
    [
        pytest.param(b"type=chat%00", "query.type", id="value"),
        pytest.param(b"a%00=chat", "query.<name>", id="name"),
        pytest.param(b"ok=1&bad=%00", "query.bad", id="second-pair"),
    ],
)
def test_scan_query_string_names_the_parameter(query: bytes, location: str) -> None:
    found = scan_query_string(query)
    assert found is not None
    assert found.location == location


@pytest.mark.parametrize(
    "query",
    [b"", b"type=chat", b"limit=50&cursor=abc%3D%3D", b"q=na%C3%AFve"],
)
def test_scan_query_string_admits_ordinary_queries(query: bytes) -> None:
    assert scan_query_string(query) is None


def test_scan_path_refuses_a_decoded_nul() -> None:
    assert scan_path("/api/v1/objects/\x00") is not None
    assert scan_path("/api/v1/objects/abc") is None


def test_scan_headers_refuses_a_nul_and_names_the_header() -> None:
    found = scan_headers([(b"idempotency-key", b"a\x00b")])
    assert found is not None
    assert found.location == "header.idempotency-key"
    assert scan_headers([(b"idempotency-key", b"ab")]) is None


def test_scan_value_walks_plain_python_structures() -> None:
    assert scan_value({"a": ["x", {"b": "\x00"}]}, "root") is not None
    assert scan_value({"a": ["x", {"b": "ok"}]}, "root") is None
    assert scan_value(b"\x00raw-bytes", "root") is None


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        pytest.param("plain", 10, "plain", id="storable-text-is-untouched"),
        pytest.param("naïve 🙂", 10, "naïve 🙂", id="astral-and-accents-survive"),
        pytest.param("a\x00b", 10, "a\ufffdb", id="nul-replaced"),
        pytest.param("a\ud800b", 10, "a\ufffdb", id="lone-surrogate-replaced"),
        pytest.param("\x00\x00", 10, "\ufffd\ufffd", id="every-nul-replaced"),
        pytest.param("x" * 300, 255, "x" * 255, id="cut-to-the-limit"),
        pytest.param("\x00" + "x" * 300, 4, "\ufffdxxx", id="replaced-then-cut"),
    ],
)
def test_storable_makes_any_text_a_record_can_hold(value: str, limit: int, expected: str) -> None:
    result = storable(value, limit=limit)
    assert result == expected
    assert reason_for(result) is None


def test_a_deferred_location_is_skipped_with_everything_under_it() -> None:
    value = {
        "name": "a\x00b",
        "entries": [{"path": "ok", "note": "fine"}, {"path": "b\udcffad", "note": "c\x00d"}],
        "report": {"deep": ["e\x00f"]},
    }
    every = storable_text.CHARACTER_REASONS
    deferred = {"body.name": every, "body.entries[].path": every, "body.report": every}

    found = storable_text.scan_value(value, "body", deferred=deferred)

    assert found is not None
    assert (found.location, found.reason) == ("body.entries[1].note", "null_character")
    assert storable_text.scan_value(value, "body") is not None
    value["entries"][1]["note"] = "fine"
    assert storable_text.scan_value(value, "body", deferred=deferred) is None


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        pytest.param("body.name", "body.name", id="no-index"),
        pytest.param("body.items[12].name", "body.items[].name", id="one-index"),
        pytest.param("body.a[0][3].b[7]", "body.a[][].b[]", id="nested-indexes"),
    ],
)
def test_field_location_leaves_list_indexes_blank(where: str, expected: str) -> None:
    assert storable_text.field_location(where) == expected


def test_a_whole_surface_declares_every_part_and_matches_whole_segments() -> None:
    whole = storable_text.SelfValidated.whole("/own")
    partial = storable_text.SelfValidated("/own", path=True, query=True, headers=True)

    assert whole.everything
    assert not partial.everything
    assert whole.covers("/own") and whole.covers("/own/words")
    assert not whole.covers("/owner")


def test_a_deferred_location_skips_only_the_reasons_it_names() -> None:
    nul_only = {"body.name": frozenset({storable_text.Reason.NUL})}

    assert storable_text.scan_value({"name": "a\x00b"}, "body", deferred=nul_only) is None
    for value in ("a\udcffb", "a\x00\udcffb"):
        found = storable_text.scan_value({"name": value}, "body", deferred=nul_only)
        assert found is not None
        assert (found.location, found.reason) == ("body.name", "unpaired_surrogate")
