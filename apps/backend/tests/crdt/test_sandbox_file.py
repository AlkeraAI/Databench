"""The sandbox's rules for a co-edited file, and the two things only a file's
session asks of it: a new epoch that keeps a version matching the file on the
drive, and merging a change made to that file outside the session.

Real Loro documents play the tabs, in this process (the backend process
itself never imports Loro; see ``test_sandbox_isolation``)."""

from __future__ import annotations

import hashlib
import random
import struct
import time
from pathlib import Path

import pytest
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import FILE, DocCache, SandboxError
from loro import EphemeralStore, ExpandType, ExportMode, LoroDoc, Side, StyleConfigMap

pytestmark = [pytest.mark.spread]

KEY = "org:file:node-1"
TAB = 5000
OTHER_TAB = 5001
SEED = 4000
MERGE = 4001


def _seed(text: str, *, base: str | None = None) -> tuple[DocCache, core.Seeded]:
    cache = DocCache()
    return cache, core.seed(cache, key=KEY, epoch=1, rules=FILE, text=text, peer=SEED, base=base)


def _tab(snapshot: bytes, peer: int = TAB) -> LoroDoc:
    doc = core.new_doc()
    doc.peer_id = peer
    doc.import_(snapshot)
    return doc


def _edit(
    tab: LoroDoc, *, insert: tuple[int, str] | None = None, delete: tuple[int, int] | None = None
) -> bytes:
    before = tab.oplog_vv
    text = tab.get_text("content")
    if delete is not None:
        text.delete(*delete)
    if insert is not None:
        text.insert(*insert)
    tab.commit()
    return bytes(tab.export(ExportMode.Updates(before)))


def _validate(cache: DocCache, update: bytes, *, log_seq: int = 0) -> core.Validated:
    verdict = core.validate(
        cache, key=KEY, epoch=1, log_seq=log_seq, rules=FILE, peers=frozenset({TAB}), update=update
    )
    assert verdict is not None
    return verdict


def _commit(cache: DocCache, verdict: core.Validated, log_seq: int) -> None:
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq, delta=verdict.delta)


def _content(cache: DocCache, log_seq: int, *, at: bytes | None = None) -> str:
    data = core.content(cache, key=KEY, epoch=1, log_seq=log_seq, rules=FILE, at=at)
    assert data is not None
    return data.decode("utf-8")


# ---------------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------------


def test_typing_and_deleting_in_the_files_text_is_admitted() -> None:
    cache, seeded = _seed("def f():\n    return 1\n")
    tab = _tab(seeded.snapshot)
    verdict = _validate(cache, _edit(tab, delete=(20, 1), insert=(20, "2")))
    assert verdict.outcome == "ok"
    _commit(cache, verdict, 1)
    assert _content(cache, 1) == "def f():\n    return 2\n"


def _foreign(seeded: core.Seeded, shape: str) -> bytes:
    tab = _tab(seeded.snapshot)
    before = tab.oplog_vv
    if shape == "the-draft":
        tab.get_text("draft").insert(0, "x")
    elif shape == "another-text":
        tab.get_text("notes").insert(0, "x")
    elif shape == "a-map":
        tab.get_map("meta").insert("k", 1)
    elif shape == "a-list":
        tab.get_list("content").insert(0, 1)
    tab.commit()
    return bytes(tab.export(ExportMode.Updates(before)))


@pytest.mark.parametrize("shape", ["the-draft", "another-text", "a-map", "a-list"])
def test_an_update_touching_anything_but_the_files_text_is_refused(shape: str) -> None:
    cache, seeded = _seed("body")
    verdict = _validate(cache, _foreign(seeded, shape))
    assert (verdict.outcome, verdict.reason) == ("reject", "container")
    assert _content(cache, 0) == "body"


def test_marks_on_the_files_text_are_refused() -> None:
    cache, seeded = _seed("abc")
    tab = _tab(seeded.snapshot)
    before = tab.oplog_vv
    styles = StyleConfigMap()
    styles.insert("bold", ExpandType.After)
    tab.config_text_style(styles)
    tab.get_text("content").mark(0, 2, "bold", True)
    tab.commit()
    verdict = _validate(cache, bytes(tab.export(ExportMode.Updates(before))))
    assert (verdict.outcome, verdict.reason) == ("reject", "op_type")


def test_the_file_may_not_grow_past_two_mib_but_may_always_shrink() -> None:
    cap = FILE.text_caps["content"]
    assert cap == 2 * 1024 * 1024
    cache, seeded = _seed("x" * (cap - 10))
    tab = _tab(seeded.snapshot)
    over = _validate(cache, _edit(tab, insert=(0, "y" * 11)))
    assert (over.outcome, over.reason) == ("reject", "text_too_large")
    fresh = _tab(seeded.snapshot, peer=TAB)
    at_cap = _validate(cache, _edit(fresh, insert=(0, "y" * 10)))
    assert at_cap.outcome == "ok"


def test_the_projection_is_a_digest_and_a_size_never_the_text() -> None:
    text = "line one\nline two\n" + "x" * (300 * 1024)
    _, seeded = _seed(text)
    assert seeded.projection == {
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "bytes": len(text.encode()),
        "lines": 3,
    }


def _caret(peer: int, value: object) -> bytes:
    store = EphemeralStore(60_000)
    store.set(str(peer), value)
    return bytes(store.encode_all())


def _cursor(seeded: core.Seeded, container: str) -> bytes:
    tab = _tab(seeded.snapshot)
    text = tab.get_text(container)
    if container != "content":
        text.insert(0, "x")
        tab.commit()
    cursor = text.get_cursor(1, Side.Middle)
    assert cursor is not None
    return bytes(cursor.encode())


def test_a_caret_in_the_files_text_is_relayed() -> None:
    _, seeded = _seed("hello")
    cursor = _cursor(seeded, "content")
    out = core.ephemeral(
        rules=FILE, peer=TAB, data=_caret(TAB, {"anchor": cursor, "focus": cursor})
    )
    store = EphemeralStore(60_000)
    store.apply(out)
    assert store.get_all_states() == {str(TAB): {"anchor": cursor, "focus": cursor}}


@pytest.mark.parametrize("container", ["draft", "notes"])
def test_a_caret_anywhere_else_is_refused(container: str) -> None:
    _, seeded = _seed("hello")
    cursor = _cursor(seeded, container)
    with pytest.raises(SandboxError) as excinfo:
        core.ephemeral(rules=FILE, peer=TAB, data=_caret(TAB, {"anchor": cursor, "focus": cursor}))
    assert excinfo.value.code == "ephemeral"


# ---------------------------------------------------------------------------
# A new epoch that keeps the drive's version
# ---------------------------------------------------------------------------


def test_a_seed_with_a_base_holds_the_text_and_a_version_holding_the_base() -> None:
    cache, seeded = _seed("drive text, then a live edit", base="drive text")
    assert _content(cache, 0) == "drive text, then a live edit"
    assert _content(cache, 0, at=seeded.base_vv) == "drive text"


def test_a_seed_without_a_base_names_its_whole_content_as_the_base() -> None:
    cache, seeded = _seed("all of it")
    assert _content(cache, 0, at=seeded.base_vv) == "all of it"


def test_reading_a_version_the_document_does_not_hold_is_refused() -> None:
    cache, _ = _seed("mine")
    _, other = _seed("someone else's")
    with pytest.raises(SandboxError) as excinfo:
        core.content(cache, key=KEY, epoch=1, log_seq=0, rules=FILE, at=other.vv)
    assert excinfo.value.code == "bad_base"


# ---------------------------------------------------------------------------
# Merging a change made outside the session
# ---------------------------------------------------------------------------


def _merge(cache: DocCache, base_vv: bytes, text: str, *, log_seq: int) -> core.Validated:
    verdict = core.merge(
        cache, key=KEY, epoch=1, log_seq=log_seq, rules=FILE, peer=MERGE, base_vv=base_vv, text=text
    )
    assert verdict is not None
    return verdict


def test_an_outside_change_lands_around_the_live_edits_made_since() -> None:
    drive = "alpha\nbeta\ngamma\n"
    cache, seeded = _seed(drive)
    tab = _tab(seeded.snapshot)
    live = _validate(cache, _edit(tab, insert=(0, "# header\n")))
    _commit(cache, live, 1)
    # The agent edited the file on the box: beta became BETA and a line was added.
    outside = "alpha\nBETA\ngamma\ndelta\n"
    merged = _merge(cache, seeded.base_vv, outside, log_seq=1)
    assert merged.outcome == "ok"
    _commit(cache, merged, 2)
    assert _content(cache, 2) == "# header\nalpha\nBETA\ngamma\ndelta\n"
    # The version the merge names as the source's now holds exactly the outside text.
    assert _content(cache, 2, at=merged.base_vv) == outside
    # And the tab, given the merged delta, converges on the same text.
    tab.import_(merged.delta)
    assert tab.get_text("content").to_string() == "# header\nalpha\nBETA\ngamma\ndelta\n"


def test_a_merge_that_changes_nothing_is_a_dup_and_moves_nothing() -> None:
    cache, seeded = _seed("same")
    verdict = _merge(cache, seeded.base_vv, "same", log_seq=0)
    assert verdict.outcome == "dup"
    assert verdict.delta == b""
    assert _content(cache, 0) == "same"


def test_an_outside_change_past_the_cap_is_refused() -> None:
    cache, seeded = _seed("small")
    verdict = _merge(cache, seeded.base_vv, "x" * (FILE.text_caps["content"] + 1), log_seq=0)
    assert (verdict.outcome, verdict.reason) == ("reject", "text_too_large")
    assert _content(cache, 0) == "small"


def test_a_merge_from_a_version_the_document_does_not_hold_is_refused() -> None:
    cache, _ = _seed("mine")
    _, other = _seed("theirs")
    with pytest.raises(SandboxError) as excinfo:
        _merge(cache, other.vv, "changed", log_seq=0)
    assert excinfo.value.code == "bad_base"


def test_a_merge_is_never_written_by_a_reserved_peer() -> None:
    cache, seeded = _seed("x")
    with pytest.raises(SandboxError) as excinfo:
        core.merge(
            cache, key=KEY, epoch=1, log_seq=0, rules=FILE, peer=1, base_vv=seeded.base_vv, text="y"
        )
    assert excinfo.value.code == "bad_request"


def test_a_merge_misses_like_validate() -> None:
    cache, seeded = _seed("x")
    assert (
        core.merge(
            cache, key=KEY, epoch=1, log_seq=7, rules=FILE, peer=MERGE, base_vv=seeded.vv, text="y"
        )
        is None
    )


def test_a_large_outside_change_merges_by_line() -> None:
    lines = [f"row {n}\n" for n in range(9000)]
    drive = "".join(lines)
    assert len(lines) ** 2 > core.DIFF_BUDGET
    cache, seeded = _seed(drive)
    tab = _tab(seeded.snapshot)
    _commit(cache, _validate(cache, _edit(tab, insert=(0, "top\n"))), 1)
    lines[4500] = "row changed\n"
    merged = _merge(cache, seeded.base_vv, "".join(lines), log_seq=1)
    assert merged.outcome == "ok"
    _commit(cache, merged, 2)
    assert _content(cache, 2) == "top\n" + "".join(lines)


def test_an_outside_change_beside_a_live_insert_keeps_the_insert_on_its_line() -> None:
    """The person typed on an empty line; the outside change edited the lines
    around it. A diff that deleted and retyped the unchanged line breaks (as
    a raw character diff may) left the person's insert beside nothing, and the
    merge moved it onto the next line."""
    base = "a\n \n\nb wv\n"
    cache, seeded = _seed(base)
    tab = _tab(seeded.snapshot)
    # The empty line is the third: its start is just after "a\n \n".
    _commit(cache, _validate(cache, _edit(tab, insert=(4, "w"))), 1)
    merged = _merge(cache, seeded.base_vv, "a\nAG\n\nAGb w\n", log_seq=1)
    _commit(cache, merged, 2)
    assert _content(cache, 2) == "a\nAG\nw\nAGb w\n"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        pytest.param("one\ntwo\n", "one\n2\ntwo\n", id="a-line-added"),
        pytest.param("a\n\n\nb", "AG\n\n\nAG", id="identical-lines"),
        pytest.param("x😀y\r\n", "x😀Zy\r\n", id="multibyte-and-crlf"),
        pytest.param("", "new\n", id="from-nothing"),
        pytest.param("gone\n", "", id="to-nothing"),
    ],
)
def test_text_edits_turn_one_text_into_the_other(current: str, target: str) -> None:
    out = current
    for at, remove, insert in reversed(core.text_edits(current, target)):
        out = out[:at] + insert + out[at + remove :]
    assert out == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        pytest.param("x" * 30_000, "y" * 30_000, id="one-line-rewritten"),
        pytest.param(
            "".join(random.Random(1).choices("abcd", k=18_000)),
            "".join(random.Random(2).choices("abcd", k=18_000)),
            id="one-line-rewritten-from-few-letters",
        ),
        pytest.param(
            "".join(f"{n}\n" for n in range(40_000)),
            "".join(f"{n}!\n" for n in range(40_000)),
            id="every-line-changed",
        ),
        pytest.param(
            "head\n" + "a\n" * 30_000 + "tail\n",
            "head\n" + "b\n" * 30_000 + "tail\n",
            id="repeated-lines",
        ),
    ],
)
def test_a_large_change_is_diffed_in_bounded_time(current: str, target: str) -> None:
    """A change too large to diff finely is still an exact edit, made in well
    under a worker's deadline: one large outside change can never hold the
    sandbox past it (and so never poison the worker)."""
    started = time.monotonic()
    edits = core.text_edits(current, target)
    assert time.monotonic() - started < 1.0
    text = current
    for at, remove, insert in reversed(edits):
        text = text[:at] + insert + text[at + remove :]
    assert text == target


def test_a_named_version_is_read_from_a_worker_that_moved_past_it() -> None:
    """A session pass read the row before people typed on: its read names the
    version it saw and is answered from the worker's later position, where a
    read of the position itself (no version named) is still a miss."""
    cache, seeded = _seed("first\n")
    tab = _tab(seeded.snapshot)
    before = core.encode_vv(tab)
    _commit(cache, _validate(cache, _edit(tab, insert=(0, "typed ")), log_seq=0), 1)
    assert _content(cache, 0, at=before) == "first\n"
    assert core.content(cache, key=KEY, epoch=1, log_seq=0, rules=FILE) is None
    assert core.content(cache, key=KEY, epoch=1, log_seq=2, rules=FILE, at=before) is None
    assert _content(cache, 1, at=core.encode_vv(tab)) == "typed first\n"
    # Read again after the document moved on: the same text (a version of
    # an epoch never changes, so the worker answers it from what it read).
    _commit(cache, _validate(cache, _edit(tab, insert=(0, "more ")), log_seq=1), 2)
    assert _content(cache, 2, at=before) == "first\n"
    assert _content(cache, 2) == "more typed first\n"
    # The latest state the worker holds is answered with its own vector, for
    # a reader that named an earlier position, without an older checkout.
    found = core.latest(cache, key=KEY, epoch=1, log_seq=1, rules=FILE)
    assert found is not None
    assert found[0] == b"more typed first\n" and core.same_vv(tab, found[1])
    assert core.latest(cache, key=KEY, epoch=1, log_seq=3, rules=FILE) is None


def test_a_merge_as_a_peer_that_wrote_since_its_base_is_refused() -> None:
    """A writer reuses its peer while its bases hold all the peer wrote. From
    a base before the peer's last merge, a fork would write operation ids the
    document already holds, and Loro would take the second change for the
    first (the agent's edit silently dropped): refused, the writer mints a
    peer for it instead."""
    cache, seeded = _seed("one\n")
    first = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=FILE,
        peer=MERGE,
        base_vv=seeded.base_vv,
        text="one\ntwo\n",
    )
    assert first is not None and first.outcome == "ok"
    _commit(cache, first, 1)
    with pytest.raises(SandboxError) as stale:
        core.merge(
            cache,
            key=KEY,
            epoch=1,
            log_seq=1,
            rules=FILE,
            peer=MERGE,
            base_vv=seeded.base_vv,
            text="zero\none\n",
        )
    assert stale.value.code == "stale_peer"
    again = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=1,
        rules=FILE,
        peer=MERGE,
        base_vv=first.vv,
        text="zero\none\ntwo\n",
    )
    assert again is not None and again.outcome == "ok"
    _commit(cache, again, 2)
    assert _content(cache, 2) == "zero\none\ntwo\n"


@pytest.mark.parametrize(
    ("drive", "outside", "expected"),
    [
        pytest.param("keep\ndrop\n", "keep\nnew\n", "keep\ndrop\nnew\n", id="a-rewritten-line"),
        pytest.param("one\r\ntwo", "one\r\nTWO", "one\r\ntwo\r\nTWO", id="a-last-line-crlf"),
        pytest.param("a\nb\n", "a\nx\nb\n", "a\nx\nb\n", id="an-added-line"),
    ],
)
def test_a_merge_that_keeps_everything_adds_whole_lines_beside_what_stays(
    drive: str, outside: str, expected: str
) -> None:
    """A change whose writer could not name its base adds what it adds and
    removes nothing; what it rewrote lands as whole lines beside the old
    ones, never character by character inside a word that stays."""
    cache, seeded = _seed(drive)
    verdict = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=FILE,
        peer=MERGE,
        base_vv=seeded.base_vv,
        text=outside,
        keep=True,
    )
    assert verdict is not None and verdict.outcome == "ok"
    _commit(cache, verdict, 1)
    assert _content(cache, 1) == expected


#: A live file's stored state from a ten-minute chaos soak (two people typing
#: into it, the agent's edits merged through its box): the snapshot, the
#: stored version vector, then the 495 logged updates after the snapshot.
SOAK_LOG = Path(__file__).parent / "fixtures" / "soak_file_log.bin"


def _blobs(path: Path) -> list[bytes]:
    data = path.read_bytes()
    count, offset, found = struct.unpack(">I", data[:4])[0], 4, []
    for _ in range(count):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        found.append(data[offset + 4 : offset + 4 + size])
        offset += 4 + size
    return found


def test_a_soaked_file_s_log_loads_well_inside_the_load_budget() -> None:
    """Imported one update at a time, each paid for the whole document again:
    this log took 12 s to load, past the 10 s budget, and the document could
    not be opened again (every load timed out and killed its worker). Imported
    as one batch it takes milliseconds; the bound leaves room for a slow box."""
    snapshot, vv, *updates = _blobs(SOAK_LOG)
    assert len(updates) == 495
    started = time.monotonic()
    loaded = core.load(
        DocCache(),
        key=KEY,
        epoch=1,
        log_seq=len(updates),
        rules=FILE,
        snapshot=snapshot,
        updates=updates,
        expect_vv=vv,
    )
    assert time.monotonic() - started < 3.0
    assert loaded.projection["lines"] == 238
