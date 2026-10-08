"""Link classification and relocation are lexical, and exact inverses of one another."""

from __future__ import annotations

import pytest
from alkera_core.files.links import (
    LinkKind,
    classify_link,
    materialize_link,
    strip_extended_prefix,
)
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# A segment is any byte string Linux accepts inside a path component: every byte
# but the separator and NUL. Drawn from those 254 bytes directly rather than by
# filtering ``st.binary``, which favours NUL heavily enough that most draws were
# thrown away and a loaded runner tripped Hypothesis's too-slow health check.
_COMPONENT_BYTES = [bytes([b]) for b in range(256) if b not in (0x00, ord("/"))]
_SEGMENT = st.lists(st.sampled_from(_COMPONENT_BYTES), min_size=1, max_size=6).map(b"".join)
_ROOT = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda s: b"/" + b"/".join(s))
_TAIL = st.lists(_SEGMENT, min_size=1, max_size=3).map(b"/".join)


# --- the five A11 example links -------------------------------------------------


@pytest.mark.parametrize(
    ("target", "root", "expected_kind", "expected_stored"),
    [
        pytest.param(
            b"../sibling/file.txt",
            b"/workspace",
            LinkKind.RELATIVE,
            b"../sibling/file.txt",
            id="relative-escaping-the-root-is-stored-verbatim",
        ),
        pytest.param(
            b"/workspace/Shared/x",
            b"/workspace",
            LinkKind.CANONICAL,
            b"/Shared/x",
            id="canonical-under-the-root-becomes-an-org-path",
        ),
        pytest.param(
            b"/usr/lib/libfoo.so",
            b"/workspace",
            LinkKind.HOST,
            b"/usr/lib/libfoo.so",
            id="host-outside-the-root-is-verbatim",
        ),
        pytest.param(
            b"/workspace/Shared/missing.txt",
            b"/workspace",
            LinkKind.CANONICAL,
            b"/Shared/missing.txt",
            id="dangling-target-still-classifies-lexically",
        ),
        pytest.param(
            b"loop",
            b"/workspace",
            LinkKind.RELATIVE,
            b"loop",
            id="self-referential-loop-is-an-ordinary-relative-link",
        ),
    ],
)
def test_a11_example_links_classify_as_specified(
    target: bytes, root: bytes, expected_kind: LinkKind, expected_stored: bytes
) -> None:
    assert classify_link(target, root) == (expected_kind, expected_stored)


# --- lexical only: no resolution of `..` or of symlinked components -------------


@pytest.mark.parametrize(
    ("target", "expected_stored"),
    [
        pytest.param(
            b"/workspace/../etc/passwd",
            b"/../etc/passwd",
            id="dotdot-escaping-the-root-stays-canonical-and-keeps-dotdot",
        ),
        pytest.param(
            b"/workspace/a/../b",
            b"/a/../b",
            id="interior-dotdot-is-not-collapsed",
        ),
        pytest.param(
            b"/workspace/./a",
            b"/./a",
            id="single-dot-is-not-collapsed",
        ),
        pytest.param(
            b"/workspace//a",
            b"//a",
            id="double-separator-is-not-collapsed",
        ),
        pytest.param(
            b"/workspace/symlinked-dir/target",
            b"/symlinked-dir/target",
            id="a-symlinked-component-is-rewritten-by-prefix-only",
        ),
    ],
)
def test_classification_never_normalises_the_remainder(
    target: bytes, expected_stored: bytes
) -> None:
    kind, stored = classify_link(target, b"/workspace")
    assert (kind, stored) == (LinkKind.CANONICAL, expected_stored)


# --- the prefix boundary and its negative twin ---------------------------------


@pytest.mark.parametrize(
    ("target", "expected_kind", "expected_stored"),
    [
        pytest.param(b"/workspace/x", LinkKind.CANONICAL, b"/x", id="under-the-root"),
        pytest.param(b"/workspace", LinkKind.CANONICAL, b"", id="the-root-itself"),
        pytest.param(b"/workspace/", LinkKind.CANONICAL, b"/", id="the-root-with-a-slash"),
        pytest.param(b"/workspace/", LinkKind.CANONICAL, b"/", id="the-root-with-slash"),
        pytest.param(
            b"/workspaces/x",
            LinkKind.HOST,
            b"/workspaces/x",
            id="a-sibling-sharing-the-prefix-is-NOT-under-the-root",
        ),
        pytest.param(
            b"/workspacex",
            LinkKind.HOST,
            b"/workspacex",
            id="no-separator-after-the-prefix-is-NOT-under-the-root",
        ),
    ],
)
def test_only_a_separator_bounded_prefix_is_canonical(
    target: bytes, expected_kind: LinkKind, expected_stored: bytes
) -> None:
    assert classify_link(target, b"/workspace") == (expected_kind, expected_stored)


@pytest.mark.parametrize(
    "root",
    [
        pytest.param(b"/workspace", id="plain"),
        pytest.param(b"/workspace/", id="one-trailing-slash"),
        pytest.param(b"/workspace///", id="several-trailing-slashes"),
    ],
)
def test_trailing_slash_roots_are_normalised_once(root: bytes) -> None:
    assert classify_link(b"/workspace/Shared/x", root) == (
        LinkKind.CANONICAL,
        b"/Shared/x",
    )
    assert materialize_link(LinkKind.CANONICAL, b"/Shared/x", root) == b"/workspace/Shared/x"


def test_filesystem_root_is_a_usable_local_root() -> None:
    assert classify_link(b"/Shared/x", b"/") == (LinkKind.CANONICAL, b"/Shared/x")
    assert materialize_link(LinkKind.CANONICAL, b"/Shared/x", b"/") == b"/Shared/x"
    assert materialize_link(LinkKind.CANONICAL, b"/", b"/") == b"/"


def test_the_root_itself_round_trips_to_the_new_root() -> None:
    kind, stored = classify_link(b"/workspace", b"/workspace")
    assert (kind, stored) == (LinkKind.CANONICAL, b"")
    assert materialize_link(kind, stored, b"/mnt/alkera") == b"/mnt/alkera"


@pytest.mark.parametrize(
    ("target", "root"),
    [
        pytest.param(b"/workspace", b"/workspace", id="the-root-itself"),
        pytest.param(b"/workspace/", b"/workspace", id="the-root-with-a-trailing-slash"),
        pytest.param(b"/workspace//", b"/workspace", id="the-root-with-two-trailing-slashes"),
        pytest.param(b"/workspace/x/", b"/workspace", id="a-directory-with-a-trailing-slash"),
        pytest.param(b"/", b"/", id="the-filesystem-root-as-its-own-root"),
    ],
)
def test_a_target_naming_the_root_keeps_every_trailing_separator_byte(
    target: bytes, root: bytes
) -> None:
    """`/workspace/` and `/workspace` are different bytes and must stay different.

    Both name the same directory, so a lossy classification looks harmless — but
    a link target is bytes, and pointing a materialized link at `/workspace`
    when the export said `/workspace/` is a silent rewrite of the user's data.
    """
    kind, stored = classify_link(target, root)

    assert kind is LinkKind.CANONICAL
    assert materialize_link(kind, stored, root) == target


def test_the_root_and_the_root_with_a_slash_do_not_share_a_stored_form() -> None:
    """The stored forms must differ, or no relocation can tell them apart."""
    _, bare = classify_link(b"/workspace", b"/workspace")
    _, slashed = classify_link(b"/workspace/", b"/workspace")

    assert bare != slashed
    assert materialize_link(LinkKind.CANONICAL, bare, b"/mnt") == b"/mnt"
    assert materialize_link(LinkKind.CANONICAL, slashed, b"/mnt") == b"/mnt/"


def test_a_canonical_target_that_is_not_an_org_path_is_refused() -> None:
    with pytest.raises(ValueError, match="canonical"):
        materialize_link(LinkKind.CANONICAL, b"Shared/x", b"/workspace")


# --- relocation properties -----------------------------------------------------


@settings(max_examples=400)
@given(root_a=_ROOT, root_b=_ROOT, tail=_TAIL)
def test_canonical_targets_relocate_to_the_corresponding_path_under_the_new_root(
    root_a: bytes, root_b: bytes, tail: bytes
) -> None:
    target = root_a + b"/" + tail
    kind, stored = classify_link(target, root_a)
    assert kind is LinkKind.CANONICAL
    # The expectation is built from the strategy inputs, not from the module.
    assert materialize_link(kind, stored, root_b) == root_b + b"/" + tail
    assert materialize_link(kind, stored, root_a) == target


@settings(max_examples=400)
@given(root_a=_ROOT, root_b=_ROOT, tail=_TAIL)
def test_relative_targets_are_byte_identical_under_every_root(
    root_a: bytes, root_b: bytes, tail: bytes
) -> None:
    kind, stored = classify_link(tail, root_a)
    assert kind is LinkKind.RELATIVE
    assert stored == tail
    assert materialize_link(kind, stored, root_b) == tail


@settings(max_examples=400)
@given(root_a=_ROOT, root_b=_ROOT, tail=_TAIL)
def test_host_targets_are_byte_identical_under_every_root(
    root_a: bytes, root_b: bytes, tail: bytes
) -> None:
    target = b"/" + tail
    assume(target != root_a and not target.startswith(root_a.rstrip(b"/") + b"/"))
    kind, stored = classify_link(target, root_a)
    assert kind is LinkKind.HOST
    assert stored == target
    assert materialize_link(kind, stored, root_b) == target


# --- the Windows flavour, and the POSIX bytes it must not touch ----------------

_WINDOWS_ROOT = rb"C:\Users\runner\root-a"
_UNC_ROOT = rb"\\server\share\root-a"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(
            rb"\\?\C:\Users\runner\root-a\data\notes.txt",
            rb"C:\Users\runner\root-a\data\notes.txt",
            id="a-drive-substitute-name-loses-the-prefix",
        ),
        pytest.param(
            rb"\\?\UNC\server\share\root-a\data\notes.txt",
            rb"\\server\share\root-a\data\notes.txt",
            id="a-share-substitute-name-comes-back-as-a-unc-path",
        ),
        pytest.param(
            rb"\\?\unc\server\share\x",
            rb"\\server\share\x",
            id="the-unc-marker-is-matched-without-regard-to-case",
        ),
        pytest.param(
            rb"C:\Users\runner\root-a\data\notes.txt",
            rb"C:\Users\runner\root-a\data\notes.txt",
            id="an-unprefixed-path-is-untouched",
        ),
        pytest.param(
            rb"\\server\share\x",
            rb"\\server\share\x",
            id="a-plain-unc-path-is-untouched",
        ),
        pytest.param(b"", b"", id="the-empty-path"),
    ],
)
def test_the_extended_length_prefix_is_dropped_on_windows(path: bytes, expected: bytes) -> None:
    assert strip_extended_prefix(path, windows=True) == expected


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(rb"\\?\C:\Users\runner\x", id="a-posix-name-shaped-like-a-substitute-name"),
        pytest.param(rb"\\?\UNC\server\share\x", id="a-posix-name-shaped-like-a-share"),
        pytest.param(b"/workspace/x", id="an-ordinary-posix-target"),
    ],
)
def test_a_posix_name_keeps_every_byte_however_it_is_spelled(path: bytes) -> None:
    """`\\` and `?` are ordinary bytes in a POSIX name, and one may be a file.

    The rewrite is gated on the caller's flavour rather than on the shape of
    the path precisely so a file literally named this way is not renamed.
    """
    assert strip_extended_prefix(path) == path
    assert strip_extended_prefix(path, windows=False) == path


@pytest.mark.parametrize(
    ("target", "root", "expected_kind", "expected_stored"),
    [
        pytest.param(
            rb"C:\Users\runner\root-a\data\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.CANONICAL,
            b"/data/notes.txt",
            id="a-drive-absolute-under-the-root-becomes-an-org-path",
        ),
        pytest.param(
            rb"C:/Users/runner/root-a/data/notes.txt",
            _WINDOWS_ROOT,
            LinkKind.CANONICAL,
            b"/data/notes.txt",
            id="both-separators-name-the-same-separator",
        ),
        pytest.param(
            rb"c:\users\RUNNER\root-a\data\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.CANONICAL,
            b"/data/notes.txt",
            id="the-root-matches-without-regard-to-case",
        ),
        pytest.param(
            rb"C:\Users\runner\root-a",
            _WINDOWS_ROOT,
            LinkKind.CANONICAL,
            b"",
            id="the-root-itself",
        ),
        pytest.param(
            rb"C:\Users\runner\root-a\\",
            _WINDOWS_ROOT,
            LinkKind.CANONICAL,
            b"//",
            id="the-root-keeps-its-trailing-separators",
        ),
        pytest.param(
            rb"\\server\share\root-a\data\notes.txt",
            _UNC_ROOT,
            LinkKind.CANONICAL,
            b"/data/notes.txt",
            id="a-share-absolute-under-the-share-root",
        ),
        pytest.param(
            rb"D:\other\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.HOST,
            rb"D:\other\notes.txt",
            id="another-drive-is-a-host-link",
        ),
        pytest.param(
            rb"C:\Users\runner\root-and-more\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.HOST,
            rb"C:\Users\runner\root-and-more\notes.txt",
            id="a-sibling-sharing-the-prefix-is-NOT-under-the-root",
        ),
        pytest.param(
            rb"..\data\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.RELATIVE,
            rb"..\data\notes.txt",
            id="a-relative-target-is-stored-verbatim",
        ),
        pytest.param(
            rb"\data\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.RELATIVE,
            rb"\data\notes.txt",
            id="rooted-without-a-drive-names-nothing-on-another-machine",
        ),
        pytest.param(
            b"/usr/bin/env",
            _WINDOWS_ROOT,
            LinkKind.RELATIVE,
            b"/usr/bin/env",
            id="a-posix-absolute-path-is-drive-relative-here-not-a-host-link",
        ),
        pytest.param(
            rb"\\?\C:\Users\runner\root-a\data\notes.txt",
            _WINDOWS_ROOT,
            LinkKind.HOST,
            rb"\\?\C:\Users\runner\root-a\data\notes.txt",
            id="the-classifier-is-lexical-and-strips-no-prefix-of-its-own",
        ),
    ],
)
def test_a_windows_target_classifies_against_a_windows_root(
    target: bytes, root: bytes, expected_kind: LinkKind, expected_stored: bytes
) -> None:
    assert classify_link(target, root, windows=True) == (expected_kind, expected_stored)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(rb"C:\Users\runner\root-a\data\notes.txt", id="drive-absolute"),
        pytest.param(rb"\\?\C:\Users\runner\root-a\data\notes.txt", id="extended-length"),
        pytest.param(rb"\\server\share\root-a\data\notes.txt", id="unc"),
    ],
)
def test_a_windows_target_read_by_the_posix_rules_stays_relative(target: bytes) -> None:
    """The POSIX half is byte-identical whatever it is handed.

    This is the classification the server does, and the bug the flavour fixes:
    a Windows absolute target that falls through to `relative` is stored with
    the pusher's own drive in it and written back unchanged on every machine.
    """
    assert classify_link(target, _WINDOWS_ROOT) == (LinkKind.RELATIVE, target)
    assert materialize_link(LinkKind.RELATIVE, target, b"/mnt/alkera") == target


@pytest.mark.parametrize(
    ("stored", "root", "expected"),
    [
        pytest.param(
            b"/data/notes.txt",
            rb"C:\Users\runner\root-b",
            rb"C:\Users\runner\root-b\data\notes.txt",
            id="an-org-path-is-written-with-the-native-separator",
        ),
        pytest.param(
            b"/data/notes.txt",
            rb"C:\Users\runner\root-b\\",
            rb"C:\Users\runner\root-b\data\notes.txt",
            id="trailing-separators-on-the-root-are-normalised-once",
        ),
        pytest.param(
            b"/data/notes.txt",
            _UNC_ROOT,
            rb"\\server\share\root-a\data\notes.txt",
            id="a-share-root-is-a-usable-local-root",
        ),
        pytest.param(
            b"", rb"C:\Users\runner\root-b", rb"C:\Users\runner\root-b", id="the-root-itself"
        ),
    ],
)
def test_a_canonical_target_is_re_anchored_the_way_windows_spells_it(
    stored: bytes, root: bytes, expected: bytes
) -> None:
    assert materialize_link(LinkKind.CANONICAL, stored, root, windows=True) == expected


@pytest.mark.parametrize(
    ("target", "root"),
    [
        pytest.param(rb"C:\Users\runner\root-a\data\notes.txt", _WINDOWS_ROOT, id="drive"),
        pytest.param(rb"\\server\share\root-a\data\notes.txt", _UNC_ROOT, id="share"),
        pytest.param(rb"C:\Users\runner\root-a", _WINDOWS_ROOT, id="the-root-itself"),
        pytest.param(
            rb"C:\Users\runner\root-a\a\..\b", _WINDOWS_ROOT, id="dotdot-is-not-collapsed"
        ),
    ],
)
def test_the_windows_pair_is_an_inverse(target: bytes, root: bytes) -> None:
    kind, stored = classify_link(target, root, windows=True)
    assert kind is LinkKind.CANONICAL
    assert materialize_link(kind, stored, root, windows=True) == target


def test_a_windows_canonical_target_relocates_to_the_root_that_pulled_it() -> None:
    """The whole point: nothing of the machine that pushed it survives."""
    kind, stored = classify_link(
        rb"C:\Users\runner\root-a\data\notes.txt", _WINDOWS_ROOT, windows=True
    )

    assert (kind, stored) == (LinkKind.CANONICAL, b"/data/notes.txt")
    # A machine of either kind can re-anchor what the other stored.
    assert (
        materialize_link(kind, stored, rb"D:\pull\root-b", windows=True)
        == rb"D:\pull\root-b\data\notes.txt"
    )
    assert materialize_link(kind, stored, b"/mnt/alkera") == b"/mnt/alkera/data/notes.txt"
