"""The sandbox core: what it admits, what it refuses, and that nothing it
refuses changes the document. Driven with real Loro documents playing the
clients, in this process (the backend process itself never imports Loro; see
``test_sandbox_isolation``)."""

from __future__ import annotations

import dataclasses
import secrets
from datetime import UTC, datetime
from typing import Any

import pytest
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import CHAT_WORKSPACE, DocCache, SandboxError
from freezegun import freeze_time
from loro import EphemeralStore, ExpandType, ExportMode, LoroDoc, Side, StyleConfigMap

KEY = "org:chat_draft:sess-1"
PEER = 5000
OTHER = 5001
#: The peer the store minted for this epoch's seed.
SEED = 4000


def _client(server_snapshot: bytes, peer: int = PEER) -> LoroDoc:
    doc = core.new_doc()
    doc.peer_id = peer
    doc.import_(server_snapshot)
    return doc


def _seeded(text: str = "seed", cache: DocCache | None = None) -> tuple[DocCache, core.Seeded]:
    cache = cache if cache is not None else DocCache()
    return cache, core.seed(cache, key=KEY, epoch=1, rules=CHAT_WORKSPACE, text=text, peer=SEED)


def _typed(client: LoroDoc, at: int, text: str) -> bytes:
    before = client.oplog_vv
    client.get_text("draft").insert(at, text)
    client.commit()
    return bytes(client.export(ExportMode.Updates(before)))


def _validate(
    cache: DocCache, update: bytes, *, log_seq: int = 0, peer: int = PEER
) -> core.Validated:
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=CHAT_WORKSPACE,
        peers=frozenset({peer}),
        update=update,
    )
    assert verdict is not None
    return verdict


def _cached_text(cache: DocCache, log_seq: int = 0) -> str:
    exported = core.snapshot(cache, key=KEY, epoch=1, log_seq=log_seq)
    assert exported is not None
    doc = core.new_doc()
    doc.import_(exported.data)
    return str(doc.get_text("draft").to_string())


# ---------------------------------------------------------------------------
# seed / load
# ---------------------------------------------------------------------------


def test_a_seed_is_cached_at_the_start_of_its_epoch_and_reloads_to_the_same_vector() -> None:
    cache, seeded = _seeded("hello")
    assert seeded.projection["text"] == "hello"
    assert cache.position(KEY) == (1, 0)
    other = DocCache()
    loaded = core.load(
        other,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        snapshot=seeded.snapshot,
        updates=[],
        expect_vv=seeded.vv,
    )
    assert core.decode_vv(loaded.vv) == core.decode_vv(seeded.vv)
    assert loaded.projection == seeded.projection


@pytest.mark.parametrize("text", ["", "kept"], ids=["empty", "with-text"])
def test_an_edit_from_another_epoch_is_missing_history_never_adopted(text: str) -> None:
    """Every epoch is seeded by its own peer and the seed always writes an
    operation, so a later epoch's edit depends on something no earlier epoch
    holds — even when the draft was empty."""
    cache = DocCache()
    earlier = core.seed(cache, key=KEY, epoch=1, rules=CHAT_WORKSPACE, text=text, peer=SEED)
    later = core.seed(cache, key=KEY, epoch=2, rules=CHAT_WORKSPACE, text=text, peer=SEED + 1)
    assert earlier.projection["text"] == later.projection["text"] == text
    edit = _typed(_client(later.snapshot), 0, "NEW")
    stale = _client(earlier.snapshot, peer=OTHER)
    status = stale.import_(edit)
    assert status.pending is not None and not status.pending.is_empty
    assert stale.get_text("draft").to_string() == text


@pytest.mark.parametrize(
    "peer",
    [
        pytest.param(0, id="zero"),
        pytest.param(1, id="a-reserved-id"),
        pytest.param(core.SERVER_PEER_MAX, id="the-last-reserved-id"),
    ],
)
def test_a_seed_is_never_written_by_a_reserved_peer(peer: int) -> None:
    with pytest.raises(SandboxError) as excinfo:
        core.seed(DocCache(), key=KEY, epoch=1, rules=CHAT_WORKSPACE, text="x", peer=peer)
    assert excinfo.value.code == "bad_request"


def test_a_load_replays_the_log_after_the_snapshot() -> None:
    _, seeded = _seeded("ab")
    client = _client(seeded.snapshot)
    first = _typed(client, 2, "c")
    second = _typed(client, 3, "d")
    cache = DocCache()
    loaded = core.load(
        cache,
        key=KEY,
        epoch=1,
        log_seq=2,
        rules=CHAT_WORKSPACE,
        snapshot=seeded.snapshot,
        updates=[first, second],
        expect_vv=bytes(client.oplog_vv.encode()),
    )
    assert loaded.projection["text"] == "abcd"
    assert cache.position(KEY) == (1, 2)


@pytest.mark.parametrize(
    "case",
    ["vector-disagrees", "truncated-update", "missing-history", "garbage-snapshot"],
)
def test_a_load_that_does_not_rebuild_the_stored_document_is_corrupt(case: str) -> None:
    _, seeded = _seeded("ab")
    client = _client(seeded.snapshot)
    first = _typed(client, 2, "c")
    second = _typed(client, 3, "d")
    snapshot, updates, expect = seeded.snapshot, [first, second], bytes(client.oplog_vv.encode())
    if case == "vector-disagrees":
        updates = [first]
    elif case == "truncated-update":
        updates = [first, second[:-4]]
    elif case == "missing-history":
        updates = [second]
    else:
        snapshot = b"not a loro snapshot"
    cache = DocCache()
    with pytest.raises(SandboxError) as excinfo:
        core.load(
            cache,
            key=KEY,
            epoch=1,
            log_seq=len(updates),
            rules=CHAT_WORKSPACE,
            snapshot=snapshot,
            updates=updates,
            expect_vv=expect,
        )
    assert excinfo.value.code == "corrupt"
    assert cache.position(KEY) is None


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_a_miss_asks_for_the_document() -> None:
    cache, _ = _seeded()
    for epoch, log_seq in [(1, 1), (2, 0)]:
        assert (
            core.validate(
                cache,
                key=KEY,
                epoch=epoch,
                log_seq=log_seq,
                rules=CHAT_WORKSPACE,
                peers=frozenset({PEER}),
                update=b"",
            )
            is None
        )


def test_an_edit_is_admitted_as_the_canonical_delta_and_leaves_the_cache_alone() -> None:
    cache, seeded = _seeded("hello")
    client = _client(seeded.snapshot)
    # The client sends everything it holds, the seed included: the server
    # commits only what it did not have.
    client.get_text("draft").insert(5, " world")
    client.commit()
    everything = bytes(client.export(ExportMode.Snapshot()))
    verdict = _validate(cache, everything)
    assert verdict.outcome == "ok"
    assert verdict.projection["text"] == "hello world"
    assert verdict.ops == len(" world")
    assert verdict.delta != everything and len(verdict.delta) < len(everything)
    assert core.decode_vv(verdict.vv) == client.oplog_vv
    # The cache has not moved: the backend has not committed anything yet.
    assert cache.position(KEY) == (1, 0)
    assert _cached_text(cache) == "hello"
    # The delta alone takes another copy of the seed to the same text.
    other = _client(seeded.snapshot, peer=OTHER)
    other.import_(verdict.delta)
    assert other.get_text("draft").to_string() == "hello world"


class _SimulatedPanic(BaseException):
    """Stands in for Loro's ``pyo3_runtime.PanicException`` — a ``BaseException``,
    not an ``Exception``, so a bare ``except Exception`` never catches it."""


def test_a_panic_reading_the_validated_fork_is_refused_not_let_through() -> None:
    """A Loro call that aborts (a Rust panic) while reading the fork of an
    accepted update must be refused with a code the worker answers, never
    escape ``validate`` and take the worker down. The seam is a rules object
    whose projection raises a ``BaseException``; the panic itself was not
    reproduced from crafted bytes."""

    def boom(_: object) -> dict[str, Any]:
        raise _SimulatedPanic("simulated rust panic")

    cache, seeded = _seeded("hi")
    rules = dataclasses.replace(CHAT_WORKSPACE, project=boom)
    client = _client(seeded.snapshot)
    update = _typed(client, 2, "!")
    with pytest.raises(SandboxError) as excinfo:
        core.validate(
            cache, key=KEY, epoch=1, log_seq=0, rules=rules, peers=frozenset({PEER}), update=update
        )
    assert excinfo.value.code == "reject"
    # The cached document is untouched, so the worker keeps serving it.
    assert _cached_text(cache) == "hi"


@pytest.mark.parametrize(
    "message",
    [pytest.param("x", id="one-character"), pytest.param("M" * 200_000, id="large")],
)
def test_an_edit_carrying_a_commit_message_is_refused(message: str) -> None:
    """A commit message rides the canonical delta into the log and out to every
    tab; the composer never writes one, so any message is refused."""
    cache, seeded = _seeded("hi")
    client = _client(seeded.snapshot)
    before = client.oplog_vv
    client.get_text("draft").insert(2, "!")
    client.set_next_commit_message(message)
    client.commit()
    verdict = _validate(cache, bytes(client.export(ExportMode.Updates(before))))
    assert (verdict.outcome, verdict.reason) == ("reject", "message")
    assert _cached_text(cache) == "hi"


def _stamped(seeded: core.Seeded, timestamp: int) -> bytes:
    client = _client(seeded.snapshot)
    before = client.oplog_vv
    client.get_text("draft").insert(2, "!")
    client.set_next_commit_timestamp(timestamp)
    client.commit()
    return bytes(client.export(ExportMode.Updates(before)))


def test_an_edit_stamped_in_the_future_is_refused_until_the_clock_reaches_it() -> None:
    """Loro stamps every later change with at least the newest timestamp it
    depends on, so one far-future stamp would be copied onto everyone's edits
    after it. A stamp within the allowed clock skew is admitted; past it is not."""
    stamp = 2_000_000_000
    skew = core.MAX_TIMESTAMP_SKEW_SECONDS
    _, seeded = _seeded("hi")
    update = _stamped(seeded, stamp)
    with freeze_time(datetime.fromtimestamp(stamp - skew - 1, UTC)) as frozen:
        cache = DocCache()
        core.load(
            cache,
            key=KEY,
            epoch=1,
            log_seq=0,
            rules=CHAT_WORKSPACE,
            snapshot=seeded.snapshot,
            updates=[],
            expect_vv=None,
        )
        assert (_validate(cache, update).outcome, _validate(cache, update).reason) == (
            "reject",
            "timestamp",
        )
        frozen.move_to(datetime.fromtimestamp(stamp - skew, UTC))
        assert _validate(cache, update).outcome == "ok"


@pytest.mark.parametrize(
    "stamp", [pytest.param(0, id="unrecorded"), pytest.param(-5, id="negative-clamps-to-zero")]
)
def test_an_edit_stamped_in_the_past_is_admitted(stamp: int) -> None:
    cache, seeded = _seeded("hi")
    assert _validate(cache, _stamped(seeded, stamp)).outcome == "ok"


def test_an_update_the_server_already_holds_is_a_dup() -> None:
    cache, seeded = _seeded("hi")
    client = _client(seeded.snapshot)
    update = _typed(client, 2, "!")
    verdict = _validate(cache, update)
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=verdict.delta)
    again = _validate(cache, update, log_seq=1)
    assert (again.outcome, again.delta) == ("dup", b"")
    assert core.decode_vv(again.vv) == client.oplog_vv


def test_an_update_on_history_the_server_lacks_asks_for_a_resync() -> None:
    cache, seeded = _seeded("a")
    client = _client(seeded.snapshot)
    _typed(client, 1, "b")  # never sent
    later = _typed(client, 2, "c")
    verdict = _validate(cache, later)
    assert verdict.outcome == "resync"
    assert _cached_text(cache) == "a"


@pytest.mark.parametrize(
    "update",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00", id="one-byte"),
        pytest.param(b"garbage" * 20, id="garbage"),
        pytest.param(secrets.token_bytes(512), id="random"),
    ],
)
def test_bytes_that_do_not_decode_are_refused(update: bytes) -> None:
    cache, _ = _seeded("a")
    verdict = _validate(cache, update)
    assert (verdict.outcome, verdict.reason) == ("reject", "undecodable")
    assert _cached_text(cache) == "a"


def test_a_truncated_or_bit_flipped_update_is_refused_and_changes_nothing() -> None:
    cache, seeded = _seeded("a")
    client = _client(seeded.snapshot)
    good = _typed(client, 1, "bcdefghij")
    flipped = bytearray(good)
    flipped[len(flipped) // 2] ^= 0x40
    for bad in (good[:-3], bytes(flipped)):
        verdict = _validate(cache, bad)
        assert verdict.outcome == "reject"
        assert _cached_text(cache) == "a"


def _edited_by(seeded: core.Seeded, peer: int) -> bytes:
    client = _client(seeded.snapshot, peer=peer)
    return _typed(client, 0, "x")


@pytest.mark.parametrize(
    "author",
    [
        pytest.param(OTHER, id="another-client"),
        pytest.param(SEED, id="the-seed-peer"),
        pytest.param(7, id="a-server-peer"),
    ],
)
def test_an_update_written_as_any_peer_but_the_senders_is_refused(author: int) -> None:
    cache, seeded = _seeded("a")
    verdict = _validate(cache, _edited_by(seeded, author))
    assert (verdict.outcome, verdict.reason) == ("reject", "peer")


def test_a_writer_may_carry_edits_made_under_an_earlier_peer_of_their_own() -> None:
    """A tab that reconnected under a fresh Loro peer still holds the edits it
    made as the old one; both are this person's and the update is admitted.
    Somebody else's peer in the same update still is not."""
    cache, seeded = _seeded("a")
    tab = _client(seeded.snapshot, peer=PEER)
    _typed(tab, 1, "b")  # never acknowledged
    tab.peer_id = OTHER  # the fresh peer the reconnect handed it
    _typed(tab, 2, "c")
    update = bytes(tab.export(ExportMode.Updates(core.decode_vv(seeded.vv))))
    mine = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        peers=frozenset({PEER, OTHER}),
        update=update,
    )
    assert mine is not None and mine.outcome == "ok" and mine.projection["text"] == "abc"
    only_new = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        peers=frozenset({OTHER}),
        update=update,
    )
    assert only_new is not None and (only_new.outcome, only_new.reason) == ("reject", "peer")


def test_an_update_mixing_the_senders_edits_with_anothers_is_refused() -> None:
    cache, seeded = _seeded("a")
    mine = _client(seeded.snapshot)
    theirs = _client(seeded.snapshot, peer=OTHER)
    _typed(theirs, 0, "t")
    mine.import_(theirs.export(ExportMode.Snapshot()))
    _typed(mine, 0, "m")
    verdict = _validate(cache, bytes(mine.export(ExportMode.Updates(core.decode_vv(seeded.vv)))))
    assert (verdict.outcome, verdict.reason) == ("reject", "peer")


def _shaped(seeded: core.Seeded, shape: str) -> bytes:
    client = _client(seeded.snapshot)
    before = client.oplog_vv
    if shape == "map":
        client.get_map("evil").insert("k", 1)
    elif shape == "other-text":
        client.get_text("notes").insert(0, "x")
    elif shape == "list":
        client.get_list("draft").insert(0, 1)
    elif shape == "movable-list":
        client.get_movable_list("items").insert(0, 1)
    elif shape == "tree":
        client.get_tree("t").create()
    elif shape == "counter":
        client.get_counter("c").increment(1)
    elif shape == "nested":
        client.get_map("draft").insert_container("inner", core.loro.LoroText())
    client.commit()
    return bytes(client.export(ExportMode.Updates(before)))


@pytest.mark.parametrize(
    "shape", ["map", "other-text", "list", "movable-list", "tree", "counter", "nested"]
)
def test_an_update_touching_anything_but_the_draft_text_is_refused(shape: str) -> None:
    cache, seeded = _seeded("a")
    verdict = _validate(cache, _shaped(seeded, shape))
    assert (verdict.outcome, verdict.reason) == ("reject", "container")
    assert _cached_text(cache) == "a"


def test_marks_on_the_draft_are_refused() -> None:
    cache, seeded = _seeded("abc")
    client = _client(seeded.snapshot)
    before = client.oplog_vv
    styles = StyleConfigMap()
    styles.insert("bold", ExpandType.After)
    client.config_text_style(styles)
    client.get_text("draft").mark(0, 2, "bold", True)
    client.commit()
    verdict = _validate(cache, bytes(client.export(ExportMode.Updates(before))))
    assert (verdict.outcome, verdict.reason) == ("reject", "op_type")


def test_the_draft_may_not_grow_past_its_cap_but_may_always_shrink() -> None:
    cap = CHAT_WORKSPACE.text_caps["draft"]
    # Four bytes a character: the cap is on UTF-8 bytes, not characters.
    cache, seeded = _seeded("😀" * (cap // 4))
    client = _client(seeded.snapshot)
    grow = _typed(client, 0, "x")
    assert _validate(cache, grow).reason == "text_too_large"

    # A document already over the cap (seeded under a larger one) can be cut down.
    big_cache, big = _seeded("y" * (cap + 10))
    shrinker = _client(big.snapshot)
    before = shrinker.oplog_vv
    shrinker.get_text("draft").delete(0, 5)
    shrinker.commit()
    verdict = _validate(big_cache, bytes(shrinker.export(ExportMode.Updates(before))))
    assert verdict.outcome == "ok"


def test_an_update_with_too_many_operations_is_refused() -> None:
    small = dataclasses.replace(CHAT_WORKSPACE, max_update_ops=3)
    cache, seeded = _seeded("a")
    client = _client(seeded.snapshot)
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=small,
        peers=frozenset({PEER}),
        update=_typed(client, 1, "four"),
    )
    assert verdict is not None
    assert (verdict.outcome, verdict.reason) == ("reject", "too_many_ops")


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------


def test_advance_moves_the_cache_to_the_committed_delta() -> None:
    cache, seeded = _seeded("a")
    client = _client(seeded.snapshot)
    verdict = _validate(cache, _typed(client, 1, "b"))
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=verdict.delta)
    assert cache.position(KEY) == (1, 1)
    assert _cached_text(cache, log_seq=1) == "ab"
    nxt = _validate(cache, _typed(client, 2, "c"), log_seq=1)
    assert nxt.outcome == "ok" and nxt.projection["text"] == "abc"


def test_advance_follows_a_delta_another_replica_committed() -> None:
    """The cache was not the one that validated this delta (another replica
    did): it imports the committed bytes instead of promoting a fork."""
    cache, seeded = _seeded("a")
    elsewhere = DocCache()
    core.load(
        elsewhere,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        snapshot=seeded.snapshot,
        updates=[],
        expect_vv=None,
    )
    client = _client(seeded.snapshot)
    verdict = _validate(elsewhere, _typed(client, 1, "b"))
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=verdict.delta)
    assert _cached_text(cache, log_seq=1) == "ab"


def test_advance_that_cannot_follow_drops_the_document() -> None:
    cache, seeded = _seeded("a")
    client = _client(seeded.snapshot)
    _typed(client, 1, "b")
    skipped = _typed(client, 2, "c")
    # Not the next position, or a delta on missing history: the next request loads.
    assert not core.advance(cache, key=KEY, epoch=1, log_seq=2, delta=skipped)
    assert cache.position(KEY) is None
    cache, _ = _seeded("a", cache)
    assert not core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=skipped)
    assert cache.position(KEY) is None


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_sends_a_peer_only_what_its_vector_lacks() -> None:
    cache, seeded = _seeded("hello")
    client = _client(seeded.snapshot)
    verdict = _validate(cache, _typed(client, 5, "!"))
    core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=verdict.delta)
    behind = _client(seeded.snapshot, peer=OTHER)
    exported = core.export(
        cache, key=KEY, epoch=1, log_seq=1, since=bytes(behind.oplog_vv.encode())
    )
    assert exported is not None and exported.mode == "updates"
    behind.import_(exported.data)
    assert behind.get_text("draft").to_string() == "hello!"
    assert core.decode_vv(exported.vv) == behind.oplog_vv


@pytest.mark.parametrize("since", [None, b"\xff\xff not a vector"])
def test_export_sends_the_whole_document_without_a_readable_vector(since: bytes | None) -> None:
    cache, _ = _seeded("hello")
    exported = core.export(cache, key=KEY, epoch=1, log_seq=0, since=since)
    assert exported is not None and exported.mode == "snapshot"
    fresh = core.new_doc()
    fresh.import_(exported.data)
    assert fresh.get_text("draft").to_string() == "hello"


def test_export_misses_like_validate() -> None:
    cache, _ = _seeded()
    assert core.export(cache, key=KEY, epoch=1, log_seq=3, since=None) is None
    assert core.snapshot(cache, key="other", epoch=1, log_seq=0) is None


# ---------------------------------------------------------------------------
# ephemeral
# ---------------------------------------------------------------------------


def _caret(seeded: core.Seeded, key: str, value: object) -> bytes:
    store = EphemeralStore(60_000)
    store.set(key, value)
    return bytes(store.encode_all())


def _cursor_bytes(seeded: core.Seeded, container: str = "draft") -> bytes:
    client = _client(seeded.snapshot)
    text = client.get_text(container)
    if container != "draft":
        text.insert(0, "x")
        client.commit()
    cursor = text.get_cursor(0, Side.Middle)
    assert cursor is not None
    return bytes(cursor.encode())


def test_a_peer_publishes_its_own_caret_re_encoded() -> None:
    _, seeded = _seeded("hello")
    cursor = _cursor_bytes(seeded)
    out = core.ephemeral(
        rules=CHAT_WORKSPACE,
        peer=PEER,
        data=_caret(seeded, str(PEER), {"anchor": cursor, "focus": cursor}),
    )
    store = EphemeralStore(60_000)
    store.apply(out)
    assert store.get_all_states() == {str(PEER): {"anchor": cursor, "focus": cursor}}


@pytest.mark.parametrize(
    "shape",
    [
        "someone-elses-key",
        "two-keys",
        "not-a-caret",
        "extra-field",
        "empty-caret",
        "oversized-position",
        "undecodable-position",
        "caret-in-another-container",
        "garbage",
    ],
)
def test_anything_but_the_senders_own_caret_is_refused(shape: str) -> None:
    _, seeded = _seeded("hello")
    cursor = _cursor_bytes(seeded)
    caret = {"anchor": cursor, "focus": cursor}
    data: bytes
    if shape == "someone-elses-key":
        data = _caret(seeded, str(OTHER), caret)
    elif shape == "two-keys":
        store = EphemeralStore(60_000)
        store.set(str(PEER), caret)
        store.set(str(OTHER), caret)
        data = bytes(store.encode_all())
    elif shape == "not-a-caret":
        data = _caret(seeded, str(PEER), [1, 2, 3])
    elif shape == "extra-field":
        data = _caret(seeded, str(PEER), {**caret, "name": "Mallory"})
    elif shape == "empty-caret":
        data = _caret(seeded, str(PEER), {})
    elif shape == "oversized-position":
        data = _caret(seeded, str(PEER), {"anchor": b"x" * 600})
    elif shape == "undecodable-position":
        data = _caret(seeded, str(PEER), {"anchor": b"zz"})
    elif shape == "caret-in-another-container":
        data = _caret(seeded, str(PEER), {"anchor": _cursor_bytes(seeded, "notes")})
    else:
        data = b"\x00\x01garbage"
    with pytest.raises(SandboxError) as excinfo:
        core.ephemeral(rules=CHAT_WORKSPACE, peer=PEER, data=data)
    assert excinfo.value.code == "ephemeral"


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def test_the_cache_evicts_the_least_recently_used_first() -> None:
    cache = DocCache(max_docs=2)
    for key in ("a", "b"):
        core.seed(cache, key=key, epoch=1, rules=CHAT_WORKSPACE, text=key, peer=SEED)
    assert cache.get("a", 1, 0) is not None  # "a" is now the most recent
    core.seed(cache, key="c", epoch=1, rules=CHAT_WORKSPACE, text="c", peer=SEED)
    assert (cache.position("a"), cache.position("b"), cache.position("c")) == (
        (1, 0),
        None,
        (1, 0),
    )


def test_the_cache_keeps_under_its_byte_budget_but_always_holds_the_newest() -> None:
    cache = DocCache(max_bytes=1)
    for key in ("a", "b"):
        core.seed(cache, key=key, epoch=1, rules=CHAT_WORKSPACE, text=key * 50, peer=SEED)
    assert len(cache) == 1
    assert cache.position("b") == (1, 0)


def _many_peers() -> tuple[bytes, bytes, bytes]:
    """A document many Loro peers wrote, its snapshot, and its version vector
    encoded by a DIFFERENT document holding the same history: equal vectors,
    and (with enough peers) different bytes, because the encoding follows a
    hash map's order."""
    src = core.new_doc()
    src.peer_id = 1
    for peer in range(1100, 1140):
        tab = _client(bytes(src.export(ExportMode.Snapshot())), peer=peer)
        _typed(tab, 0, f"<{peer}>")
        src.import_(tab.export(ExportMode.Updates(src.oplog_vv)))
    snapshot = bytes(src.export(ExportMode.Snapshot()))
    rebuilt = core.new_doc()
    rebuilt.import_(snapshot)
    return snapshot, bytes(src.oplog_vv.encode()), bytes(rebuilt.oplog_vv.encode())


def test_a_load_compares_version_vectors_not_their_bytes() -> None:
    snapshot, stored, rebuilt = _many_peers()
    assert stored != rebuilt, "the fixture must encode one vector two ways"
    loaded = core.load(
        DocCache(),
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        snapshot=snapshot,
        updates=[],
        expect_vv=stored,
    )
    assert core.decode_vv(loaded.vv) == core.decode_vv(stored)


def test_a_snapshot_is_checked_against_the_stored_vector() -> None:
    snapshot, stored, _ = _many_peers()
    cache = DocCache()
    core.load(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=CHAT_WORKSPACE,
        snapshot=snapshot,
        updates=[],
        expect_vv=None,
    )
    assert core.snapshot(cache, key=KEY, epoch=1, log_seq=0, expect_vv=stored) is not None
    other = core.new_doc()
    other.peer_id = 2
    other.get_text("draft").insert(0, "x")
    other.commit()
    with pytest.raises(SandboxError) as excinfo:
        core.snapshot(cache, key=KEY, epoch=1, log_seq=0, expect_vv=bytes(other.oplog_vv.encode()))
    assert excinfo.value.code == "corrupt"
