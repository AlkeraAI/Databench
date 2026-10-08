"""The one error contract every Files caller spells in its own way.

The library was assembled from lanes that each raised the same failures with a
different call shape (a bare code, prose, ``code=``/``message=``, a code plus a
``detail`` bag). These tests pin the resolution rules that admit all of them,
the per-class status the route layer reads, and — with a scan over the source —
that ``KNOWN_CODES`` still names every code the library can emit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import alkera_core.files as files_package
import pytest
from alkera_core.files.errors import (
    CONFLICT_CODES,
    KNOWN_CODE_PREFIXES,
    KNOWN_CODES,
    Conflict,
    FilesError,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
    QuotaExceeded,
    ReadOnlyContent,
    looks_like_code,
)

FILES_ROOT = Path(files_package.__file__).parent

#: What a scanner reading the source treats as a Files error code: a
#: ``files.``-prefixed dotted identifier that fills a whole string literal, or
#: an f-string up to its first placeholder. The quotes are what separate a code
#: from the module path in ``from alkera_core.files.clock import Clock`` and
#: from a domain-separation tag such as ``b"files.marker.v1:"``.
CODE_IN_SOURCE = re.compile(r"""(?<=["'])files\.[a-z0-9_]+(?:\.[a-z0-9_]+)*\.?(?=["'{])""")


def codes_in(source: str) -> set[str]:
    """Every Files code literal in ``source``.

    An f-string that appends a generated tail (``f"files.invalid_name.{exc.code}"``)
    shows up as its stem alone; ``KNOWN_CODE_PREFIXES`` is what answers for those.
    """
    return {match.group(0) for match in CODE_IN_SOURCE.finditer(source)}


def unknown_codes(source: str) -> set[str]:
    """The codes in ``source`` that the catalogue does not name."""
    return {
        code
        for code in codes_in(source)
        if code not in KNOWN_CODES and code not in KNOWN_CODE_PREFIXES
    }


# ---- the class defaults ----------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "code", "status"),
    [
        pytest.param(FilesError, "files.error", 500, id="base"),
        pytest.param(NotFound, "files.not_found", 404, id="not-found"),
        pytest.param(PreconditionFailed, "files.precondition_failed", 412, id="precondition"),
        pytest.param(Conflict, "files.conflict", 409, id="conflict"),
        pytest.param(QuotaExceeded, "files.quota_exceeded", 507, id="quota"),
        pytest.param(InvalidRequest, "files.invalid_request", 422, id="invalid-request"),
        pytest.param(ReadOnlyContent, "files.read_only_content", 409, id="read-only"),
    ],
)
def test_a_bare_failure_carries_its_class_code_and_status(
    cls: type[FilesError], code: str, status: int
) -> None:
    """Every class answers with no arguments at all; the route reads ``status``."""
    raised = cls()
    assert raised.code == code
    assert raised.status == status
    assert raised.message is None
    assert raised.detail is None
    assert str(raised) == code
    assert isinstance(raised, FilesError)


def test_missing_idempotency_key_declares_the_status_that_asks_for_a_header() -> None:
    """428, not 400: the request becomes acceptable once the header is added."""
    from alkera_core.files.idempotency import MissingIdempotencyKey

    raised = MissingIdempotencyKey()
    assert raised.code == "files.idempotency_key_required"
    assert raised.status == 428
    assert isinstance(raised, FilesError)


def test_delta_expiry_declares_the_status_that_asks_for_a_resync() -> None:
    """410 with the resync flavour as the code, so the route needs no table."""
    from alkera_core.files.delta import RESYNC_UPLOAD, DeltaExpired

    raised = DeltaExpired(RESYNC_UPLOAD, "the feed was rebuilt")
    assert raised.code == RESYNC_UPLOAD
    assert raised.status == 410
    assert raised.retry_after > 0


# ---- the construction forms ------------------------------------------------


@pytest.mark.parametrize(
    ("build", "code", "message"),
    [
        pytest.param(NotFound, "files.not_found", None, id="bare"),
        pytest.param(
            lambda: NotFound("files.no_such_drive"), "files.no_such_drive", None, id="lone-code"
        ),
        pytest.param(
            lambda: NotFound("no operation 3f2a"),
            "files.not_found",
            "no operation 3f2a",
            id="lone-prose",
        ),
        pytest.param(
            lambda: NotFound(message="drive 3f2a"), "files.not_found", "drive 3f2a", id="message-kw"
        ),
        pytest.param(lambda: NotFound(code="files.gone"), "files.gone", None, id="code-kw"),
        pytest.param(
            lambda: InvalidRequest("files.bad_limit", "limit must be 1..1000"),
            "files.bad_limit",
            "limit must be 1..1000",
            id="code-and-message",
        ),
        pytest.param(
            lambda: InvalidRequest(code="files.bad_limit", message="limit must be 1..1000"),
            "files.bad_limit",
            "limit must be 1..1000",
            id="both-kw",
        ),
        pytest.param(
            # The delta lane's shape: prose positionally, the code explicit.
            lambda: FilesError("the feed was rebuilt", code="resync_upload_differences"),
            "resync_upload_differences",
            "the feed was rebuilt",
            id="prose-positional-with-code-kw",
        ),
    ],
)
def test_every_call_shape_the_library_uses_resolves_the_same_way(
    build: Callable[[], FilesError], code: str, message: str | None
) -> None:
    """A lone positional is a code by its shape, prose otherwise; two are (code, message)."""
    raised = build()
    assert raised.code == code
    assert raised.message == message
    assert str(raised) == (message if message else code)


@pytest.mark.parametrize(
    ("text", "is_code"),
    [
        pytest.param("files.bad_limit", True, id="dotted-identifier"),
        pytest.param("files.invalid_name.too_long", True, id="three-segments"),
        pytest.param("resync_apply_differences", False, id="no-dot-is-prose"),
        pytest.param("no operation 3f2a", False, id="has-spaces"),
        pytest.param("drive 3f2a vanished", False, id="prose-with-id"),
        pytest.param("Files.Bad", False, id="capitals"),
        pytest.param("a 1.5 second wait", False, id="dot-inside-prose"),
    ],
)
def test_a_lone_positional_is_read_as_a_code_only_when_it_is_spelled_as_one(
    text: str, is_code: bool
) -> None:
    """The rule the forgiving constructor rests on, pinned on its own."""
    assert looks_like_code(text) is is_code
    raised = NotFound(text)
    assert (raised.code == text) is is_code
    assert (raised.message == text) is not is_code


def test_an_empty_positional_changes_nothing() -> None:
    """``NotFound("")`` is the bare form, not a failure with a blank code."""
    raised = NotFound("")
    assert raised.code == "files.not_found"
    assert str(raised) == "files.not_found"


# ---- detail ----------------------------------------------------------------


def test_a_conflict_carries_detail_without_aliasing_the_caller_s_dict() -> None:
    """The client shows "who holds it"; mutating the source later cannot change it."""
    holder = {"holder": "3f2a", "machine": "laptop"}
    raised = Conflict("files.leased", detail=holder)
    assert raised.code == "files.leased"
    assert raised.detail == {"holder": "3f2a", "machine": "laptop"}
    holder["machine"] = "desktop"
    assert raised.detail == {"holder": "3f2a", "machine": "laptop"}


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        pytest.param("bytes", "files.quota_bytes", id="bytes"),
        pytest.param("nodes", "files.quota_nodes", id="nodes"),
    ],
)
def test_quota_kind_picks_the_code_and_is_readable_off_the_error(kind: str, code: str) -> None:
    """``kind`` is what tells "buy more bytes" from "delete some files"."""
    raised = QuotaExceeded(kind=kind)
    assert raised.code == code
    assert raised.kind == kind
    assert raised.detail == {"kind": kind}
    assert raised.status == 507


def test_a_quota_failure_with_no_kind_says_so_rather_than_guessing() -> None:
    raised = QuotaExceeded("the drive is full")
    assert raised.code == "files.quota_exceeded"
    assert raised.kind is None
    assert raised.detail is None


def test_a_conflict_code_the_catalogue_has_never_seen_still_raises() -> None:
    """The set is open at runtime: a new lane's code must not become a crash."""
    raised = Conflict("files.some_future_refusal", "not yet catalogued")
    assert raised.code == "files.some_future_refusal"
    assert raised.status == 409
    assert isinstance(raised, FilesError)


# ---- the catalogue ---------------------------------------------------------


def test_every_code_the_library_raises_is_in_the_catalogue() -> None:
    """The grep-driven half: source grows a code → this fails until it is named."""
    offenders: dict[str, set[str]] = {}
    for path in sorted(FILES_ROOT.rglob("*.py")):
        unknown = unknown_codes(path.read_text(encoding="utf-8"))
        if unknown:
            offenders[str(path.relative_to(FILES_ROOT))] = unknown
    assert offenders == {}


def test_the_scan_reports_a_planted_code_the_catalogue_does_not_name(tmp_path: Path) -> None:
    """The negative twin: a scan that finds nothing would pass the test above vacuously."""
    planted = tmp_path / "planted.py"
    planted.write_text('raise Conflict("files.not_a_real_code", "planted")\n', encoding="utf-8")
    assert unknown_codes(planted.read_text(encoding="utf-8")) == {"files.not_a_real_code"}


def test_the_scan_accepts_a_generated_tail_only_under_a_declared_prefix() -> None:
    """``f"files.invalid_name.{code}"`` is fine; an undeclared stem is not."""
    good = 'raise InvalidRequest(f"files.invalid_name.{exc.code}", str(exc))\n'
    bad = 'raise InvalidRequest(f"files.invented_family.{exc.code}", str(exc))\n'
    assert unknown_codes(good) == set()
    assert unknown_codes(bad) == {"files.invented_family."}


def test_every_declared_prefix_has_at_least_one_full_member() -> None:
    """A prefix with no members would wave through any tail at all."""
    for prefix in KNOWN_CODE_PREFIXES:
        assert any(code.startswith(prefix) and code != prefix for code in KNOWN_CODES), prefix


def test_the_catalogue_names_no_code_the_library_never_raises() -> None:
    """The other direction: a code deleted from the source is deleted here too."""
    seen: set[str] = set()
    for path in FILES_ROOT.rglob("*.py"):
        seen |= codes_in(path.read_text(encoding="utf-8"))
    stems = seen & set(KNOWN_CODE_PREFIXES)
    dead = {
        code
        for code in KNOWN_CODES
        if code not in seen and not any(code.startswith(stem) for stem in stems)
    }
    assert dead == set()


def test_the_conflict_vocabulary_is_part_of_the_catalogue() -> None:
    """``CONFLICT_CODES`` is a view on ``KNOWN_CODES``, not a second source."""
    assert set(CONFLICT_CODES) <= KNOWN_CODES
    assert len(set(CONFLICT_CODES)) == len(CONFLICT_CODES)
