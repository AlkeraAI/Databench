"""The naming contract: what a name may be, and what every rule refuses."""

from __future__ import annotations

import base64
import json
import unicodedata
from pathlib import Path

import pytest
from alkera_core.files.names import (
    BIDI_CONTROLS,
    FS_NAME_MAX_BYTES,
    NAME_MAX_BYTES,
    PULL_PART_SUFFIX,
    UNICODE_VERSION,
    InvalidName,
    NameFlags,
    display,
    escape_to_name,
    flags,
    folding_collisions,
    name_key,
    normalization_key,
    normalization_twin,
    parse_display,
    validate,
)
from hypothesis import given, settings
from hypothesis import strategies as st

FIXTURE = Path(__file__).parents[1] / "fixtures" / "files" / "names_v3.json"
SUPERSEDED_FIXTURE = Path(__file__).parents[1] / "fixtures" / "files" / "names_v1.json"
#: The corpus as it stood when a name could carry a bidirectional control.
BIDI_SUPERSEDED_FIXTURE = Path(__file__).parents[1] / "fixtures" / "files" / "names_v2.json"


# --------------------------------------------------------------------------- #
# Refusals are exactly Linux's.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "code"),
    [
        pytest.param(b"", "empty", id="empty"),
        pytest.param(b"\x00", "nul", id="nul-alone"),
        pytest.param(b"\x00abc", "nul", id="nul-leading"),
        pytest.param(b"a\x00b", "nul", id="nul-interior"),
        pytest.param(b"abc\x00", "nul", id="nul-trailing"),
        pytest.param(b"/", "separator", id="slash-alone"),
        pytest.param(b"/abc", "separator", id="slash-leading"),
        pytest.param(b"a/b", "separator", id="slash-interior"),
        pytest.param(b"abc/", "separator", id="slash-trailing"),
        pytest.param(b".", "dot", id="dot"),
        pytest.param(b"..", "dot", id="dotdot"),
        pytest.param(b"a" * (NAME_MAX_BYTES + 1), "too_long", id="one-byte-over"),
        pytest.param(
            "漢".encode() * (NAME_MAX_BYTES // 3) + b"a", "too_long", id="one-byte-over-cjk"
        ),
        pytest.param(b"a" * FS_NAME_MAX_BYTES, "too_long", id="the-filesystems-own-limit"),
        pytest.param(b"a\x01b", "control", id="c0-control"),
        pytest.param(b"a\tb", "control", id="tab"),
        pytest.param(b"a\x7fb", "control", id="delete"),
        pytest.param("a\x85b".encode(), "control", id="c1-next-line"),
        pytest.param(b"\x1f", "control", id="control-alone"),
        pytest.param(b" foo", "surrounding_space", id="leading-space"),
        pytest.param(b"foo ", "surrounding_space", id="trailing-space"),
        pytest.param(b"  foo  ", "surrounding_space", id="both-ends"),
        pytest.param("foo\u00a0".encode(), "surrounding_space", id="trailing-no-break-space"),
        pytest.param("\u3000foo".encode(), "surrounding_space", id="leading-ideographic-space"),
        pytest.param(b" ", "surrounding_space", id="one-space-alone"),
        pytest.param("report\u202egnp.exe".encode(), "bidi_control", id="u202e-rlo"),
        pytest.param("a\u202db".encode(), "bidi_control", id="u202d-lro"),
        pytest.param("a\u202ab".encode(), "bidi_control", id="u202a-lre"),
        pytest.param("a\u202cb".encode(), "bidi_control", id="u202c-pdf"),
        pytest.param("a\u2066b".encode(), "bidi_control", id="u2066-lri"),
        pytest.param("a\u2069b".encode(), "bidi_control", id="u2069-pdi"),
        pytest.param("a\u200eb".encode(), "bidi_control", id="u200e-lrm"),
        pytest.param("a\u200fb".encode(), "bidi_control", id="u200f-rlm"),
        pytest.param("a\u061cb".encode(), "bidi_control", id="u061c-alm"),
        pytest.param("\u202e".encode(), "bidi_control", id="bidi-alone"),
    ],
)
def test_validate_refuses_the_name_no_machine_could_hold(name: bytes, code: str) -> None:
    with pytest.raises(InvalidName) as excinfo:
        validate(name)
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(b"...", id="three-dots"),
        pytest.param(b".hidden", id="leading-dot"),
        pytest.param(b"..hidden", id="two-leading-dots"),
        pytest.param(b"a", id="one-byte"),
        pytest.param(b"a" * NAME_MAX_BYTES, id="at-the-cap"),
        pytest.param("漢".encode() * (NAME_MAX_BYTES // 3), id="at-the-cap-cjk"),
        pytest.param(b"f oo", id="an-interior-space-is-fine"),
        pytest.param(b"foo.", id="a-trailing-dot-is-fine"),
        pytest.param("a\u200bb".encode(), id="a-zero-width-space-is-not-whitespace"),
        pytest.param("\u200bfoo\u200b".encode(), id="a-surrounding-zero-width-space-is-fine"),
        pytest.param("a\u2010b".encode(), id="a-hyphen-is-not-a-control"),
        pytest.param("a\u202fb".encode(), id="a-narrow-no-break-space-is-not-bidi"),
        pytest.param("a\u2065b".encode(), id="the-code-point-before-the-isolates-is-not-bidi"),
        pytest.param("a\u206ab".encode(), id="the-code-point-after-the-isolates-is-not-bidi"),
        pytest.param("مرحبا".encode(), id="right-to-left-text-is-a-name"),
    ],
)
def test_validate_accepts_the_negative_twin_of_each_refusal(name: bytes) -> None:
    validate(name)  # must not raise


def test_invalid_name_is_a_value_error_carrying_its_code() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate(b"")
    assert isinstance(excinfo.value, InvalidName)
    assert excinfo.value.code == "empty"
    assert str(excinfo.value)


# --------------------------------------------------------------------------- #
# The cap is the sidecar's reserve, not the filesystem's limit.
# --------------------------------------------------------------------------- #


def test_the_cap_leaves_room_for_the_sidecar_a_pull_writes_beside_the_file() -> None:
    """Every name the server accepts is a name a machine can be handed.

    A pull streams each file into ``<name>.alkera-part`` next to its target and
    promotes it once the hash matches, so a name that fills NAME_MAX has a
    sidecar one suffix over it: the server would take the name and the pull
    would die with ENAMETOOLONG on bytes nobody could ever fetch. The ceiling
    is derived from the suffix rather than written down twice, so lengthening
    the sidecar lowers the ceiling with it.
    """
    assert NAME_MAX_BYTES == FS_NAME_MAX_BYTES - len(PULL_PART_SUFFIX)
    assert len(b"a" * NAME_MAX_BYTES + PULL_PART_SUFFIX) == FS_NAME_MAX_BYTES
    assert len(b"a" * (NAME_MAX_BYTES + 1) + PULL_PART_SUFFIX) > FS_NAME_MAX_BYTES


# --------------------------------------------------------------------------- #
# The acceptance table: everything older designs refused, with its flags.
# --------------------------------------------------------------------------- #

_C0 = b"a\x01b"
_RLO = "re‮sume".encode()
_BOM = "name﻿".encode()
_TAG = "tag\U000e0001".encode()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(b"aux.h", NameFlags(False, False), id="aux.h"),
        pytest.param(b"con.c", NameFlags(False, False), id="con.c"),
        pytest.param(b"NUL.txt", NameFlags(False, False), id="NUL.txt"),
        pytest.param("COM¹".encode(), NameFlags(True, False), id="COM-superscript-one"),
        pytest.param(b"foo.", NameFlags(False, False), id="trailing-dot"),
        pytest.param(b"a:b", NameFlags(False, False), id="colon"),
        pytest.param(b"a\\b", NameFlags(False, False), id="backslash"),
        pytest.param(b"~1", NameFlags(True, False), id="tilde-one"),
        pytest.param(_BOM, NameFlags(True, True), id="ufeff-bom"),
        pytest.param(_TAG, NameFlags(True, True), id="ue0001-language-tag"),
        pytest.param(b"caf\xe9", NameFlags(True, True), id="invalid-utf8"),
        pytest.param(
            "漢".encode() * (NAME_MAX_BYTES // 3), NameFlags(True, False), id="cjk-at-the-cap"
        ),
    ],
)
def test_acceptance_table_is_accepted_with_the_expected_flags(
    name: bytes, expected: NameFlags
) -> None:
    validate(name)
    assert flags(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(b"foo ", NameFlags(False, False), id="trailing-space"),
        pytest.param(b" foo", NameFlags(True, False), id="leading-space"),
        pytest.param(_C0, NameFlags(False, True), id="c0-control"),
        pytest.param(_RLO, NameFlags(True, True), id="u202e-rlo"),
        pytest.param(b"a" * FS_NAME_MAX_BYTES, NameFlags(True, False), id="over-the-cap"),
    ],
)
def test_a_name_the_rule_no_longer_takes_is_still_one_the_flags_describe(
    name: bytes, expected: NameFlags
) -> None:
    """A row already in the tree is read, listed and renamed — never re-judged.

    These are names the drive took before the rule tightened. Nothing
    migrates or renames them, so every surface that shows a row has to be able
    to describe one: :func:`flags` answers for any byte string at all, and only
    :func:`validate` — which runs on a name a caller is *proposing* — refuses.
    """
    with pytest.raises(InvalidName):
        validate(name)
    assert flags(name) == expected
    assert parse_display(display(name)) == name


@pytest.mark.parametrize(
    ("name", "windows_safe"),
    [
        pytest.param(b"COM1", False, id="COM1-reserved"),
        pytest.param(b"COM9", False, id="COM9-reserved"),
        pytest.param(b"com1", False, id="com1-lowercase-reserved"),
        pytest.param(b"COM10", True, id="COM10-not-reserved"),
        pytest.param(b"COM0", True, id="COM0-not-reserved"),
        pytest.param(b"LPT9", False, id="LPT9-reserved"),
        pytest.param(b"LPT10", True, id="LPT10-not-reserved"),
        pytest.param(b"CON", False, id="CON-reserved"),
        pytest.param(b"CON1", True, id="CON1-not-reserved"),
        pytest.param(b"CONx", True, id="CONx-not-reserved"),
        pytest.param(b"xCON", True, id="xCON-not-reserved"),
        pytest.param(b"f.oo", True, id="interior-dot-fine"),
        pytest.param(b"f oo", True, id="interior-space-fine"),
        pytest.param(b"a<b", False, id="angle-open"),
        pytest.param(b'a"b', False, id="double-quote"),
        pytest.param(b"a|b", False, id="pipe"),
        pytest.param(b"a?b", False, id="question"),
        pytest.param(b"a*b", False, id="star"),
        pytest.param(b"ab", True, id="plain"),
    ],
)
def test_windows_safe_is_the_windows_rule_and_its_negative_twin(
    name: bytes, windows_safe: bool
) -> None:
    assert flags(name).windows_safe is windows_safe


@pytest.mark.parametrize(
    ("name", "warns"),
    [
        pytest.param("a‪b".encode(), True, id="u202a-lre"),
        pytest.param("a⁩b".encode(), True, id="u2069-pdi"),
        pytest.param("a\u200bb".encode(), True, id="u200b-zwsp"),
        pytest.param("a⁠b".encode(), True, id="u2060-word-joiner"),
        pytest.param("a\U000e0020b".encode(), True, id="tag-space"),
        pytest.param(b"a\x7fb", True, id="delete-control"),
        pytest.param("ab".encode(), True, id="c1-nel"),
        pytest.param(b"\xff", True, id="lone-invalid-byte"),
        pytest.param("a⁰b".encode(), False, id="u2070-superscript-zero-is-benign"),
        pytest.param("a\u2010b".encode(), False, id="u2010-hyphen-is-benign"),
        pytest.param("café".encode(), False, id="accented-is-benign"),
        pytest.param("你好".encode(), False, id="cjk-is-benign"),
    ],
)
def test_display_warning_marks_the_invisible_and_nothing_beside_it(
    name: bytes, warns: bool
) -> None:
    assert flags(name).display_warning is warns


# --------------------------------------------------------------------------- #
# display / parse_display are exact inverses.
# --------------------------------------------------------------------------- #


@settings(max_examples=400)
@given(st.binary(min_size=0, max_size=NAME_MAX_BYTES))
def test_parse_display_inverts_display_for_any_bytes_at_all(name: bytes) -> None:
    """Not only for a name the rule takes.

    The pair has to invert for a name the tree ALREADY holds — one stored
    before a rule tightened, one carrying a control byte or an undecodable
    run — because that is the name a listing has to render and a person has to
    be able to paste back. Filtering the draws through :func:`validate` would
    have stopped exercising exactly those.
    """
    assert parse_display(display(name)) == name


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(b"\\", id="lone-backslash"),
        pytest.param(rb"\x41", id="literal-backslash-x-41"),
        pytest.param(rb"\\x41", id="doubled-backslash-x-41"),
        pytest.param(b"\\u202e", id="literal-backslash-u-escape"),
        pytest.param("‮".encode(), id="real-rlo"),
        pytest.param(b"\xed\xa0\x80", id="utf8-encoded-surrogate"),
        pytest.param(b"\xf4\x90\x80\x80", id="above-max-code-point"),
        pytest.param("漢字".encode(), id="cjk"),
    ],
)
def test_display_round_trips_the_escape_characters_themselves(name: bytes) -> None:
    assert parse_display(display(name)) == name


def test_a_right_to_left_override_cannot_display_as_a_benign_name() -> None:
    hostile = "cv‮gnp.exe".encode()
    rendered = display(hostile)
    assert "‮" not in rendered
    assert "\\u202e" in rendered
    assert parse_display(rendered) == hostile
    # and the escaped rendering is distinguishable from a name that really
    # contains those six characters
    assert display(b"cv\\u202egnp.exe") != rendered


def test_display_escapes_a_literal_backslash_so_the_escape_is_unambiguous() -> None:
    assert display(rb"\x41") == "\\\\x41"
    assert display(b"\x41") == "A"
    assert parse_display("\\\\x41") == rb"\x41"
    assert parse_display("\\x41") == b"A"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\\", id="trailing-lone-backslash"),
        pytest.param("\\q41", id="unknown-escape-marker"),
        pytest.param("\\x4", id="short-hex-byte"),
        pytest.param("\\xZZ", id="non-hex-byte"),
        pytest.param("\\xAB", id="uppercase-hex-never-emitted"),
        pytest.param("\\u202", id="short-hex-code-point"),
        pytest.param("\\ud800", id="surrogate-code-point"),
        pytest.param("\\U00110000", id="above-max-code-point"),
    ],
)
def test_parse_display_refuses_a_sequence_display_never_emits(text: str) -> None:
    with pytest.raises(ValueError):
        parse_display(text)


# --------------------------------------------------------------------------- #
# name_key: folding for search and collisions, never for uniqueness.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(b"README", b"readme", id="ascii-case"),
        pytest.param(b"README.md", b"ReadMe.MD", id="ascii-case-with-extension"),
        pytest.param(
            unicodedata.normalize("NFD", "café").encode(),
            unicodedata.normalize("NFC", "café").encode(),
            id="nfd-vs-nfc",
        ),
        pytest.param("straße".encode(), b"STRASSE", id="sharp-s-vs-ss-full-casefold"),
        pytest.param("ΣΟΣ".encode(), "σος".encode(), id="greek-final-sigma"),
    ],
)
def test_name_key_folds_the_families_a_client_would_confuse(left: bytes, right: bytes) -> None:
    assert left != right
    assert name_key(left) == name_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param("İstanbul".encode(), b"istanbul", id="turkish-dotted-I-does-not-fold-to-i"),
        pytest.param(
            "\u0131stanbul".encode(), b"istanbul", id="turkish-dotless-i-does-not-fold-to-i"
        ),
        pytest.param("İstanbul".encode(), "\u0131stanbul".encode(), id="dotted-vs-dotless"),
        pytest.param(b"readme", b"read_me", id="different-names"),
        pytest.param("\uff21\uff22".encode(), b"AB", id="fullwidth-is-not-nfc-folded"),
        pytest.param("a\u200bb".encode(), b"ab", id="zero-width-space-stays-visible"),
    ],
)
def test_name_key_keeps_apart_what_default_casefold_keeps_apart(left: bytes, right: bytes) -> None:
    assert name_key(left) != name_key(right)


def test_name_key_pins_the_turkish_i_spellings() -> None:
    # Full, locale-independent casefold: U+0130 becomes "i" + COMBINING DOT
    # ABOVE, and U+0131 folds to itself. Neither equals plain "i".
    assert name_key("İ".encode()) == "i̇"
    assert name_key("\u0131".encode()) == "\u0131"
    assert name_key(b"I") == "i"


def test_name_key_pins_sharp_s() -> None:
    assert name_key("ß".encode()) == "ss"
    assert name_key(b"SS") == "ss"


# --------------------------------------------------------------------------- #
# folding_collisions
# --------------------------------------------------------------------------- #


def test_folding_collisions_reports_the_one_pair_a_folding_client_cannot_hold() -> None:
    assert folding_collisions([b"README", b"readme", b"other"]) == [(b"README", b"readme")]


def test_folding_collisions_is_empty_when_no_two_siblings_fold_together() -> None:
    assert folding_collisions([b"a", b"b"]) == []


def test_folding_collisions_reports_every_pair_once_in_encounter_order() -> None:
    siblings = [b"ReadMe", b"README", b"other", b"readme"]
    assert folding_collisions(siblings) == [
        (b"ReadMe", b"README"),
        (b"ReadMe", b"readme"),
        (b"README", b"readme"),
    ]


def test_folding_collisions_ignores_byte_equal_repeats() -> None:
    assert folding_collisions([b"README", b"README"]) == []


def test_folding_collisions_reports_normalization_as_well_as_case() -> None:
    nfd = unicodedata.normalize("NFD", "café").encode()
    nfc = unicodedata.normalize("NFC", "café").encode()
    assert folding_collisions([nfc, nfd]) == [(nfc, nfd)]


def test_folding_collisions_on_an_empty_folder_is_empty() -> None:
    assert folding_collisions([]) == []


_NFC = unicodedata.normalize("NFC", "café").encode()
_NFD = unicodedata.normalize("NFD", "café").encode()


@pytest.mark.parametrize(
    ("siblings", "name", "twin"),
    [
        pytest.param([_NFC], _NFD, _NFC, id="nfd-against-an-nfc-sibling"),
        pytest.param([_NFD], _NFC, _NFD, id="nfc-against-an-nfd-sibling"),
        pytest.param([_NFC], _NFC, None, id="the-same-bytes-are-the-indexs-to-refuse"),
        pytest.param([b"README"], b"readme", None, id="a-case-pair-is-not-a-twin"),
        pytest.param([b"notes.txt", _NFC], _NFD, _NFC, id="found-among-others"),
        pytest.param([], _NFD, None, id="an-empty-folder"),
        pytest.param([b"\xff\xfe"], b"\xff\xfe", None, id="non-utf8-bytes-are-total"),
    ],
)
def test_normalization_twin(siblings: list[bytes], name: bytes, twin: bytes | None) -> None:
    assert normalization_twin(siblings, name) == twin


def test_normalization_key_keeps_case() -> None:
    assert normalization_key(b"README") != normalization_key(b"readme")
    assert normalization_key(_NFC) == normalization_key(_NFD)


# --------------------------------------------------------------------------- #
# The conformance fixture.
# --------------------------------------------------------------------------- #


def _fixture_entries() -> list[dict[str, object]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    entries = payload["entries"]
    assert isinstance(entries, list)
    return entries


def test_the_fixture_pins_the_unicode_version_the_interpreter_reports() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert UNICODE_VERSION == unicodedata.unidata_version
    assert payload["unicode_version"] == UNICODE_VERSION


def test_the_fixture_is_a_conformance_corpus_not_a_handful_of_cases() -> None:
    entries = _fixture_entries()
    assert len(entries) >= 250
    refused = {e["refused"] for e in entries if e["refused"] is not None}
    assert refused == {
        "empty",
        "nul",
        "separator",
        "dot",
        "too_long",
        "control",
        "surrounding_space",
        "bidi_control",
    }


def test_the_superseded_fixture_differs_only_where_the_rule_was_tightened() -> None:
    """The whole diff between v1 and today, named — and nothing beside it.

    v1 is the corpus as it stood when a name could be 255 bytes and could carry
    a control character or a surrounding space. Replaying it against the
    current rule says exactly what changed: some entries it recorded as
    accepted are now refused, each by one of the three rules this version
    tightened, and *everything else* — every display string, every fold key,
    every ``windows_safe`` — still replays byte for byte. A normalizer change
    that rode along with the rule change would land here.
    """
    payload = json.loads(SUPERSEDED_FIXTURE.read_text(encoding="utf-8"))
    tightened: set[str] = set()
    for entry in payload["entries"]:
        name = base64.b64decode(str(entry["name_b64"]))
        try:
            validate(name)
        except InvalidName as exc:
            # Either v1 refused it for the same reason, or this is one of the
            # names the tightened rule took away.
            if entry["refused"] is None:
                assert exc.code in {"too_long", "control", "surrounding_space", "bidi_control"}, (
                    name
                )
                tightened.add(exc.code)
            else:
                assert exc.code == entry["refused"], name
            continue
        assert entry["refused"] is None, name
        assert flags(name) == NameFlags(
            windows_safe=bool(entry["windows_safe"]),
            display_warning=bool(entry["display_warning"]),
        )
        assert display(name) == entry["display"]
        assert name_key(name) == entry["name_key"]

    assert tightened == {"too_long", "control", "surrounding_space", "bidi_control"}


def test_the_bidi_superseded_fixture_differs_only_where_bidi_controls_were_refused() -> None:
    """v2 is the corpus as it stood when a name could carry a bidirectional
    control. Replayed against today's rule, the only entries it recorded as
    accepted that are now refused carry one (and nothing else about any
    entry moved), and at least one such entry exists, so this is no vacuous
    pass."""
    payload = json.loads(BIDI_SUPERSEDED_FIXTURE.read_text(encoding="utf-8"))
    taken: list[bytes] = []
    for entry in payload["entries"]:
        name = base64.b64decode(str(entry["name_b64"]))
        try:
            validate(name)
        except InvalidName as exc:
            if entry["refused"] is None:
                assert exc.code == "bidi_control", name
                assert any(ord(ch) in BIDI_CONTROLS for ch in name.decode("utf-8", "replace"))
                taken.append(name)
            else:
                assert exc.code == entry["refused"], name
            continue
        assert entry["refused"] is None, name
        assert display(name) == entry["display"]
        assert name_key(name) == entry["name_key"]
    assert taken


@pytest.mark.parametrize(
    "entry",
    _fixture_entries(),
    ids=[str(e["name_b64"]) for e in _fixture_entries()],
)
def test_every_fixture_entry_replays(entry: dict[str, object]) -> None:
    name = base64.b64decode(str(entry["name_b64"]))
    if entry["refused"] is not None:
        with pytest.raises(InvalidName) as excinfo:
            validate(name)
        assert excinfo.value.code == entry["refused"]
        return
    validate(name)
    assert flags(name) == NameFlags(
        windows_safe=bool(entry["windows_safe"]),
        display_warning=bool(entry["display_warning"]),
    )
    assert display(name) == entry["display"]
    assert name_key(name) == entry["name_key"]
    assert parse_display(str(entry["display"])) == name


# --------------------------------------------------------------------------- #
# escape_to_name: a name derived from text nobody proposed as one.
# --------------------------------------------------------------------------- #


def _unescape(name: bytes) -> bytes:
    """Read every ``%XX`` back as its byte -- the inverse the escape promises."""
    out = bytearray()
    i = 0
    while i < len(name):
        if name[i : i + 1] == b"%":
            out.append(int(name[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(name[i])
            i += 1
    return bytes(out)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(b"alice@x.com", b"alice@x.com", id="a-valid-name-is-itself"),
        pytest.param(b"a/b@x.com", b"a%2Fb@x.com", id="separator"),
        pytest.param(b"/", b"%2F", id="only-a-separator"),
        pytest.param(b"a%2Fb@x.com", b"a%252Fb@x.com", id="the-escape-escapes-itself"),
        pytest.param(b"a\x00b", b"a%00b", id="nul"),
        pytest.param(b"a\tb", b"a%09b", id="control"),
        pytest.param("a\u202eb".encode(), b"a%E2%80%AEb", id="bidi-control-every-byte"),
        pytest.param("a\u0085b".encode(), b"a%C2%85b", id="c1-control-every-byte"),
        pytest.param(b" a ", b"%20a%20", id="surrounding-space"),
        pytest.param(b"a b", b"a b", id="an-inner-space-is-kept"),
        pytest.param(b".", b"%2E", id="dot"),
        pytest.param(b"..", b"%2E%2E", id="dot-dot"),
        pytest.param(b"...", b"...", id="three-dots-are-a-name"),
        pytest.param(b"a\xffb", b"a\xffb", id="a-byte-that-is-not-utf8-is-kept"),
        pytest.param("R\u2215D".encode(), "R\u2215D".encode(), id="division-slash-is-a-name"),
    ],
)
def test_escape_to_name_escapes_exactly_what_validate_refuses(raw: bytes, expected: bytes) -> None:
    assert escape_to_name(raw) == expected
    validate(escape_to_name(raw))


@settings(max_examples=300, deadline=None)
@given(st.binary(min_size=1, max_size=80))
def test_escape_to_name_is_valid_and_invertible_for_any_bytes(raw: bytes) -> None:
    name = escape_to_name(raw)
    validate(name)
    assert _unescape(name) == raw


@settings(max_examples=300, deadline=None)
@given(st.binary(min_size=1, max_size=40), st.binary(min_size=1, max_size=40))
def test_two_inputs_never_share_an_escape(left: bytes, right: bytes) -> None:
    if left != right:
        assert escape_to_name(left) != escape_to_name(right)


def test_an_escape_too_long_to_hold_is_cut_and_ends_in_a_digest_of_the_input() -> None:
    first = b"/" * 200 + b"a"
    second = b"/" * 200 + b"b"
    one, two = escape_to_name(first), escape_to_name(second)
    for name in (one, two):
        validate(name)
        assert len(name) <= NAME_MAX_BYTES
    # The same cut prefix, told apart by the digest of what was cut.
    assert one != two
    assert one.rsplit(b"~", 1)[0] == two.rsplit(b"~", 1)[0]
    # The cut never splits an escape: the prefix is whole triples.
    prefix = one.rsplit(b"~", 1)[0]
    assert len(prefix) % 3 == 0 and _unescape(prefix) == b"/" * (len(prefix) // 3)


def test_an_escape_is_never_cut_inside_a_character() -> None:
    raw = "\u00e9".encode() * 200
    name = escape_to_name(raw)
    validate(name)
    name.rsplit(b"~", 1)[0].decode("utf-8")
