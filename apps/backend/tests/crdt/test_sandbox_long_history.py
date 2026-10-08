"""What a cached document with a long history costs per keystroke and per read.

An update is imported into a copy of the cached document, never the document.
Making that copy afresh per keystroke paid for decoding the whole history on
the first import (a tenth of a second on a document of 100k edits), so the
copy stands between updates (the document's shadow). These pin that it is
cheap, and that a copy which took in anything the document did not commit (a
refused update, one waiting for history, a validated update the backend never
committed) is never checked on again: whatever it held would ride out in the
next update's canonical delta.

Reading the document at an earlier version checks it out there, which costs
the whole history too. The states the worker hands out (the latest read, a
merge's result and the state holding exactly what it merged) are kept as
they are made, so naming one later costs nothing.

Real Loro documents play the tabs, in this process."""

from __future__ import annotations

import random
import statistics
import time

import pytest
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import FILE, DocCache
from loro import ExportMode, LoroDoc

pytestmark = [pytest.mark.spread]

KEY = "org:file:node-1"
TAB = 5000
STRANGER = 5009
SEED = 4000


def _seeded(text: str = "hello\n") -> tuple[DocCache, bytes]:
    cache = DocCache()
    seeded = core.seed(cache, key=KEY, epoch=1, rules=FILE, text=text, peer=SEED)
    return cache, seeded.snapshot


def _tab(snapshot: bytes, peer: int = TAB) -> LoroDoc:
    doc = core.new_doc()
    doc.peer_id = peer
    doc.import_(snapshot)
    return doc


def _type(tab: LoroDoc, at: int, text: str, *, container: str = "content") -> bytes:
    before = tab.oplog_vv
    tab.get_text(container).insert(at, text)
    tab.commit()
    return bytes(tab.export(ExportMode.Updates(before)))


def _validate(cache: DocCache, update: bytes, log_seq: int) -> core.Validated:
    verdict = core.validate(
        cache, key=KEY, epoch=1, log_seq=log_seq, rules=FILE, peers=frozenset({TAB}), update=update
    )
    assert verdict is not None
    return verdict


def _commit(cache: DocCache, delta: bytes, log_seq: int) -> None:
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq, delta=delta)


def _text(cache: DocCache, log_seq: int) -> str:
    data = core.content(cache, key=KEY, epoch=1, log_seq=log_seq, rules=FILE)
    assert data is not None
    return data.decode("utf-8")


def _applied(snapshot: bytes, delta: bytes) -> str:
    """The content a client holding ``snapshot`` shows after the broadcast
    ``delta``: what everyone else sees of a commit."""
    doc = _tab(snapshot, peer=6000)
    doc.import_(delta)
    return str(doc.get_text("content").to_string())


@pytest.mark.parametrize(
    ("peer", "container", "reason"),
    [
        pytest.param(STRANGER, "content", "peer", id="a-peer-never-handed-out"),
        pytest.param(TAB, "other", "container", id="a-container-the-type-has-not"),
    ],
)
def test_a_refused_update_never_rides_out_in_the_next_commit(
    peer: int, container: str, reason: str
) -> None:
    cache, snapshot = _seeded()
    hostile = _tab(snapshot, peer=peer)
    refused = _validate(cache, _type(hostile, 0, "EVIL", container=container), 0)
    assert (refused.outcome, refused.reason) == ("reject", reason)

    tab = _tab(snapshot)
    verdict = _validate(cache, _type(tab, 5, "!"), 0)
    assert verdict.outcome == "ok"
    _commit(cache, verdict.delta, 1)

    assert _applied(snapshot, verdict.delta) == "hello!\n"
    assert _text(cache, 1) == "hello!\n"
    # And the copy the next update is checked on holds nothing of it either.
    after = _validate(cache, _type(tab, 0, ">"), 1)
    assert after.outcome == "ok"
    _commit(cache, after.delta, 2)
    assert _text(cache, 2) == ">hello!\n"


def test_an_update_waiting_for_history_is_not_applied_when_the_history_arrives() -> None:
    """Loro keeps an update it cannot apply yet and applies it the moment the
    missing history is imported. Checked on a copy that kept it, the history's
    own commit would carry the waiting update with it, unchecked."""
    cache, snapshot = _seeded()
    tab = _tab(snapshot)
    first = _type(tab, 0, "1")
    second = _type(tab, 1, "2")

    waiting = _validate(cache, second, 0)
    assert waiting.outcome == "resync"

    verdict = _validate(cache, first, 0)
    assert verdict.outcome == "ok"
    _commit(cache, verdict.delta, 1)
    assert _applied(snapshot, verdict.delta) == "1hello\n"
    assert _text(cache, 1) == "1hello\n"


def test_a_validated_update_the_backend_never_committed_is_not_in_the_next_one() -> None:
    cache, snapshot = _seeded()
    lost = _validate(cache, _type(_tab(snapshot), 0, "LOST"), 0)
    assert lost.outcome == "ok"

    # The commit failed; the next update is judged at the same position.
    other = _tab(snapshot)
    verdict = _validate(cache, _type(other, 5, "?"), 0)
    assert verdict.outcome == "ok"
    _commit(cache, verdict.delta, 1)
    assert _applied(snapshot, verdict.delta) == "hello?\n"
    assert _text(cache, 1) == "hello?\n"


def test_a_duplicate_leaves_the_copy_usable_and_clean() -> None:
    cache, snapshot = _seeded()
    tab = _tab(snapshot)
    update = _type(tab, 0, "a")
    verdict = _validate(cache, update, 0)
    _commit(cache, verdict.delta, 1)
    assert _validate(cache, update, 1).outcome == "dup"

    after = _validate(cache, _type(tab, 1, "b"), 1)
    assert after.outcome == "ok"
    _commit(cache, after.delta, 2)
    assert _text(cache, 2) == "abhello\n"


def test_commits_learned_from_the_log_keep_the_copy_in_step() -> None:
    """A delta committed elsewhere (another worker, or a validation this
    worker's pending fork no longer matches) reaches the cached document
    without a validation here. The copy the next update is checked on must
    take it too, or every later update would look like it depends on history
    the server lacks."""
    cache, snapshot = _seeded()
    tab = _tab(snapshot)
    position = 0
    for step in range(12):
        update = _type(tab, step, str(step % 10))
        if step % 3 == 2:
            # Committed by someone else: this worker only hears the delta.
            delta = update
        else:
            verdict = _validate(cache, update, position)
            assert verdict.outcome == "ok", step
            delta = verdict.delta
        position += 1
        _commit(cache, delta, position)
    assert _text(cache, position) == str(tab.get_text("content").to_string())


def _long(rng: random.Random) -> tuple[DocCache, LoroDoc, bytes]:
    """A cached document of 30k edits, the writer's own copy, and its snapshot."""
    long = core.new_doc()
    long.peer_id = 4100
    text = long.get_text("content")
    for _ in range(30_000):
        text.insert(rng.randint(0, text.len_unicode), rng.choice("ab \n"))
        long.commit()
    snapshot = bytes(long.export(ExportMode.Snapshot()))
    cache = DocCache()
    core.load(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=FILE,
        snapshot=snapshot,
        updates=[],
        expect_vv=None,
    )
    return cache, long, snapshot


def _checkout_cost(long: LoroDoc, version: bytes) -> float:
    """What reading ``long`` at ``version`` costs when nothing kept it."""
    started = time.perf_counter()
    copy = long.fork_at(long.vv_to_frontiers(core.decode_vv(version)))
    copy.get_text("content").to_string()
    return time.perf_counter() - started


def test_typing_on_a_long_history_does_not_pay_for_the_history() -> None:
    """Each keystroke validated and committed on a document of 30k edits costs
    a small fraction of what importing it into a freshly made copy costs (the
    per-keystroke price before the copy stood, measured here on this machine
    so the bound holds on a slow one)."""
    rng = random.Random(7)
    cache, long, snapshot = _long(rng)
    tab = _tab(snapshot)

    fresh: list[float] = []
    for _ in range(3):
        probe = _tab(snapshot, peer=TAB + 1)
        update = _type(probe, 0, "x")
        copy = long.fork()
        started = time.perf_counter()
        copy.import_(update)
        fresh.append(time.perf_counter() - started)

    costs: list[float] = []
    for position in range(40):
        update = _type(tab, rng.randint(0, 100), "k")
        started = time.perf_counter()
        verdict = _validate(cache, update, position)
        assert verdict.outcome == "ok"
        _commit(cache, verdict.delta, position + 1)
        costs.append(time.perf_counter() - started)

    assert statistics.median(costs) < min(fresh) / 4, (statistics.median(costs), min(fresh))
    assert _text(cache, 40) == str(tab.get_text("content").to_string())


def test_the_states_handed_out_are_read_back_later_without_a_checkout() -> None:
    """A text peer's submit names the state it was handed and is answered
    with the states its merge made; a base search compares the states handed
    out lately. Each was checked out again for every such read once the
    document had moved on (a fifth of a second on a soaked file, dozens per
    search). Read back now, after more typing: the same text, at a small
    fraction of a checkout's cost (measured here, so the bound holds on a
    slow machine)."""
    rng = random.Random(11)
    cache, long, snapshot = _long(rng)
    tab = _tab(snapshot)

    handed = core.latest(cache, key=KEY, epoch=1, log_seq=0, rules=FILE)
    assert handed is not None
    handed_text, handed_vv = handed
    agent = handed_text.decode("utf-8") + "agent\n"
    merged = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=FILE,
        peer=4200,
        base_vv=handed_vv,
        text=agent,
    )
    assert merged is not None and merged.outcome == "ok"
    _commit(cache, merged.delta, 1)
    tab.import_(merged.delta)
    for position in range(1, 6):
        verdict = _validate(cache, _type(tab, 0, "k"), position)
        _commit(cache, verdict.delta, position + 1)

    states = {
        handed_vv: handed_text,
        merged.vv: agent.encode("utf-8"),
        merged.base_vv: agent.encode("utf-8"),
    }
    costs: list[float] = []
    for version, expected in states.items():
        started = time.perf_counter()
        read = core.content(cache, key=KEY, epoch=1, log_seq=6, rules=FILE, at=version)
        costs.append(time.perf_counter() - started)
        assert read == expected
    # The writer's own copy, moved past the handed state as the cache did.
    long.get_text("content").insert(0, "moved on")
    long.commit()
    checkout = min(_checkout_cost(long, handed_vv) for _ in range(3))
    assert max(costs) < checkout / 4, (costs, checkout)
