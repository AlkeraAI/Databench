"""Thousands of corrupted updates through the real store and the real sandbox.

Every input is a mutation of a real update a client would send — bit flips,
truncations, random bytes, two updates spliced together, an update written as
somebody else's Loro peer, one that touches a container or an operation the
draft does not admit — fed to :meth:`CrdtDocs.apply` against real Postgres and
real worker processes.

The first ``STORE_CASES`` go through :meth:`CrdtDocs.apply`, a transaction
each, against real Postgres: the store answers with a refusal (or a
legitimate no-op), never an unexpected exception, and an input that is
refused leaves the stored document byte-identical. The rest go to a real
sandbox worker directly, where the danger lives (a malformed update that
panics Loro): every one is answered with a verdict or a refusal, a worker
that dies on one is replaced and the next is answered, and judging never
changes the document the worker holds. Afterwards the document still opens
whole in a fresh process, and a valid edit still lands.

The inputs are generated from a fixed seed, so a failure names an input that
reproduces.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import CrdtDoc
from backend.services.crdt.docs import CrdtDocs, CrdtError
from loro import ExportMode, LoroDoc, StyleConfigMap
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer, World, crdt_docs, make_world

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

SEED = 20261001
CASES = 5000
#: How many go through the whole store, a transaction each; the rest go to the worker.
STORE_CASES = 400
PEER = 5000


def _fingerprint(doc: CrdtDoc) -> tuple[Any, ...]:
    return (
        doc.epoch,
        doc.log_seq,
        bytes(doc.vv),
        bytes(doc.snapshot),
        doc.log_bytes,
        dict(doc.projection),
    )


async def _row(world: World) -> CrdtDoc:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id))
        ).scalar_one()


def _schema_breakers(base: bytes) -> list[bytes]:
    """Real Loro updates the draft's rules must refuse."""
    out: list[bytes] = []

    def edited(make: Any, peer: int = PEER) -> bytes:
        doc = LoroDoc()  # type: ignore[no-untyped-call]
        doc.peer_id = peer
        doc.import_(base)
        before = doc.oplog_vv
        make(doc)
        doc.commit()
        return bytes(doc.export(ExportMode.Updates(before)))

    out.append(edited(lambda d: d.get_map("evil").insert("k", 1)))
    out.append(edited(lambda d: d.get_list("draft").insert(0, 1)))
    out.append(edited(lambda d: d.get_movable_list("m").insert(0, 1)))
    out.append(edited(lambda d: d.get_tree("t").create()))
    out.append(edited(lambda d: d.get_counter("c").increment(1)))
    out.append(edited(lambda d: d.get_text("notes").insert(0, "x")))

    def mark(d: Any) -> None:
        styles = StyleConfigMap()
        styles.insert("bold", __import__("loro").ExpandType.After)
        d.config_text_style(styles)
        d.get_text("draft").insert(0, "ab")
        d.get_text("draft").mark(0, 1, "bold", True)

    out.append(edited(mark))
    # Written as somebody else, and as the server itself.
    out.append(edited(lambda d: d.get_text("draft").insert(0, "x"), peer=PEER + 1))
    out.append(edited(lambda d: d.get_text("draft").insert(0, "x"), peer=2))
    return out


def _mutations(
    rng: random.Random, valid: list[bytes], breakers: list[bytes]
) -> list[tuple[str, bytes]]:
    cases: list[tuple[str, bytes]] = []
    while len(cases) < CASES:
        source = rng.choice(valid)
        kind = rng.randrange(8)
        if kind == 0 and source:
            data = bytearray(source)
            for _ in range(rng.randint(1, 8)):
                data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
            cases.append(("bitflip", bytes(data)))
        elif kind == 1 and len(source) > 1:
            cases.append(("truncate", source[: rng.randrange(1, len(source))]))
        elif kind == 2:
            cases.append(("random", rng.randbytes(rng.randint(0, 600))))
        elif kind == 3:
            other = rng.choice(valid)
            cut = rng.randrange(len(source) + 1)
            cases.append(("splice", source[:cut] + other[rng.randrange(len(other) + 1) :]))
        elif kind == 4:
            cases.append(("schema", rng.choice(breakers)))
        elif kind == 5 and source:
            # The Loro header kept, the body replaced.
            cases.append(("body", source[:22] + rng.randbytes(rng.randint(1, 200))))
        elif kind == 6:
            cases.append(("duplicate", source))
        else:
            cases.append(("empty", b""))
    return cases


async def test_thousands_of_corrupted_updates_never_hurt_the_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    rng = random.Random(SEED)
    async with crdt_docs(workers=2) as docs:
        tab = Peer(PEER)
        async with AsyncSessionLocal() as db:
            sync = await docs.sync(db, world.ref, since=None, epoch_seen=None)
            await db.commit()
        tab.receive(sync.data)
        base = tab.since(Peer(9999).vv)
        # A pool of real updates to mutate: each built on the last, so most
        # mutations of a later one also depend on history the server lacks.
        valid = [tab.type(0, "seed ")]
        await _apply(docs, world, valid[0])
        for word in ("alpha ", "beta ", "gamma ", "😀 ", "delta "):
            valid.append(tab.type(0, word))
        breakers = _schema_breakers(tab.since(Peer(9999).vv) or base)

        cases = _mutations(rng, valid, breakers)
        outcomes: dict[str, int] = {}
        for index, (kind, data) in enumerate(cases[:STORE_CASES]):
            before = _fingerprint(await _row(world))
            try:
                applied = await _apply(docs, world, data, update_id=f"f{index}")
            except CrdtError as exc:
                outcomes[exc.code] = outcomes.get(exc.code, 0) + 1
                after = _fingerprint(await _row(world))
                assert after == before, f"case {index} ({kind}) changed a document it was refused"
                continue
            outcomes["accepted" if applied else "no-op"] = (
                outcomes.get("accepted" if applied else "no-op", 0) + 1
            )
        assert sum(outcomes.values()) == STORE_CASES
        assert outcomes.get("crdt_rejected", 0) > STORE_CASES // 2, outcomes

        # The rest straight to a real worker, at the row's position.
        stored = _fingerprint(await _row(world))
        verdicts = await _judge_on_a_worker(docs, world, cases[STORE_CASES:])
        assert sum(verdicts.values()) == CASES - STORE_CASES, verdicts
        assert verdicts.get("reject", 0) + verdicts.get("refused", 0) > (CASES - STORE_CASES) // 2
        assert _fingerprint(await _row(world)) == stored

    # A fresh process opens what the store holds, whole, and it still takes an edit.
    async with crdt_docs() as docs:
        fresh = Peer(PEER + 2)
        async with AsyncSessionLocal() as db:
            await docs.access(
                db, world.ref, user=world.owner.user, ent=world.owner.ent, agent_id=None
            )
            sync = await docs.sync(db, world.ref, since=None, epoch_seen=None)
            await db.commit()
        fresh.receive(sync.data)
        row = await _row(world)
        assert fresh.text == row.projection["text"]
        assert await _apply(docs, world, fresh.type(0, "still works "), peer=PEER + 2)


async def _apply(
    docs: CrdtDocs, world: World, data: bytes, *, update_id: str = "u", peer: int = PEER
) -> bool:
    async with AsyncSessionLocal() as db:
        try:
            applied = await docs.apply(
                db,
                world.ref,
                user=world.owner.user,
                ent=world.owner.ent,
                agent_id=None,
                epoch=(await _row(world)).epoch,
                peer=peer,
                update_id=update_id,
                update=data,
                socket_peer_id="p:fuzz",
            )
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
    await docs.after_commit(world.ref, applied)
    return applied.changed


async def _judge_on_a_worker(
    docs: CrdtDocs, world: World, cases: list[tuple[str, bytes]]
) -> dict[str, int]:
    """Each case's verdict from a real worker holding the document at the
    row's position. A worker that dies on one is replaced, the document is
    brought back up, and the next case is judged; the cases are judged
    against the same position throughout, because judging changes nothing."""
    from backend.services.crdt.sandbox.pool import SandboxCrashedError, SandboxRefusedError

    async def bring_up() -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            await docs.sync(db, world.ref, since=None, epoch_seen=None)
            await db.commit()
        row = await _row(world)
        return {
            "op": "validate",
            "key": docs.cache_key(world.ref, row),
            "epoch": row.epoch,
            "log_seq": row.log_seq,
            "rules": "chat_draft",
            "peers": [PEER],
        }

    header = await bring_up()
    verdicts: dict[str, int] = {}
    for index, (kind, data) in enumerate(cases):
        for _attempt in range(3):
            try:
                reply = await docs.pool.request(world.ref.key, header, [data], budget_seconds=10)
            except SandboxRefusedError:
                outcome = "refused"
                break
            except SandboxCrashedError:
                verdicts["crashed"] = verdicts.get("crashed", 0) + 1
                header = await bring_up()
                continue
            if reply.header.get("need") is True:
                header = await bring_up()
                continue
            outcome = str(reply.header.get("outcome"))
            break
        else:
            raise AssertionError(f"case {index} ({kind}) was never judged")
        verdicts[outcome] = verdicts.get(outcome, 0) + 1
    verdicts.pop("crashed", None)
    return verdicts
