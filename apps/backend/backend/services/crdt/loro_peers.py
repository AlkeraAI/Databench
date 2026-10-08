"""The Loro peers a document's writers hold: who may write as which peer.

Every person writing a document is handed Loro peer ids from one sequence,
remembered per person and document, and held by the socket writing with them
(a lease renewed on its keepalive), so two sockets never write as one peer and
nobody writes as anybody else's. :class:`LoroPeers` is the half of the
document store (:class:`~backend.services.crdt.docs.CrdtDocs`) that keeps them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from alkera_core.models import CrdtPeer, User
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.registry import DocRef
from backend.services.infra import now as _now

#: How long a socket's hold on a Loro peer lasts unrenewed.
PEER_LEASE = timedelta(seconds=90)
#: The Loro peers one person keeps per document. Past it, minting another
#: forgets their oldest peers no socket holds; a held one is never forgotten.
PEERS_PER_PERSON = 64


class LoroPeers:
    """Minting, claiming, renewing and forgetting writers' Loro peers."""

    @staticmethod
    async def peers_of(db: AsyncSession, ref: DocRef, *, user: User) -> frozenset[int]:
        """Every Loro peer this person was ever handed for this document: a tab
        that reconnected under a fresh peer still holds edits it made as the
        old one, and they are its own. Nobody else's peer, and never a seed's,
        may be written as. A socket reads this once, at its hello: a peer
        minted afterwards belongs to another socket, so a set read then is
        complete for what this one writes."""
        return frozenset(
            (
                await db.execute(
                    select(CrdtPeer.loro_peer).where(
                        CrdtPeer.org_id == ref.org_id,
                        CrdtPeer.doc_type == ref.stored_type,
                        CrdtPeer.doc_id == ref.doc_id,
                        CrdtPeer.user_id == user.id,
                    )
                )
            ).scalars()
        )

    async def claim_peer(
        self,
        db: AsyncSession,
        ref: DocRef,
        *,
        user: User,
        offered: int | None,
        holder: str,
    ) -> int:
        """The Loro peer this socket writes ``ref`` as.

        The one it offers when that peer was minted for this person and this
        document and no other live socket holds it; a fresh one from the
        sequence otherwise. A client offers a peer only while it still holds
        the document it wrote with it, so reusing one never restarts its
        counters; the hold keeps two sockets (a duplicated tab) off one peer."""
        now = _now()
        if offered is not None:
            claimed = (
                await db.execute(
                    update(CrdtPeer)
                    .where(
                        CrdtPeer.loro_peer == offered,
                        CrdtPeer.org_id == ref.org_id,
                        CrdtPeer.doc_type == ref.stored_type,
                        CrdtPeer.doc_id == ref.doc_id,
                        CrdtPeer.user_id == user.id,
                        (CrdtPeer.held_by.is_(None))
                        | (CrdtPeer.held_by == holder)
                        | (CrdtPeer.held_until < now),
                    )
                    .values(held_by=holder, held_until=now + PEER_LEASE, last_seen_at=now)
                    .returning(CrdtPeer.loro_peer)
                )
            ).scalar_one_or_none()
            if claimed is not None:
                return int(claimed)
        minted = (
            await db.execute(
                pg_insert(CrdtPeer)
                .values(
                    org_id=ref.org_id,
                    doc_type=ref.stored_type,
                    doc_id=ref.doc_id,
                    user_id=user.id,
                    held_by=holder,
                    held_until=now + PEER_LEASE,
                    last_seen_at=now,
                )
                .returning(CrdtPeer.loro_peer)
            )
        ).scalar_one()
        await self._forget_oldest_peers(db, ref, user=user, now=now)
        return int(minted)

    @staticmethod
    async def _forget_oldest_peers(
        db: AsyncSession, ref: DocRef, *, user: User, now: datetime
    ) -> None:
        """Keep at most :data:`PEERS_PER_PERSON` of this person's peers for
        ``ref``: every held one, then the most recently seen. A forgotten peer
        is never minted again, so nobody else can write as it; a tab still
        holding unsent edits made as one is refused and offered its text back,
        as after any reset."""
        held = (
            CrdtPeer.held_by.is_not(None)
            & CrdtPeer.held_until.is_not(None)
            & (CrdtPeer.held_until >= now)
        )
        ranked = (
            select(
                CrdtPeer.loro_peer,
                held.label("held"),
                func.row_number()
                .over(
                    order_by=(
                        held.desc(),
                        CrdtPeer.last_seen_at.desc(),
                        CrdtPeer.loro_peer.desc(),
                    )
                )
                .label("rank"),
            )
            .where(
                CrdtPeer.org_id == ref.org_id,
                CrdtPeer.doc_type == ref.stored_type,
                CrdtPeer.doc_id == ref.doc_id,
                CrdtPeer.user_id == user.id,
            )
            .subquery()
        )
        await db.execute(
            delete(CrdtPeer).where(
                CrdtPeer.loro_peer.in_(
                    select(ranked.c.loro_peer).where(
                        ranked.c.rank > PEERS_PER_PERSON, ranked.c.held.is_(False)
                    )
                )
            )
        )

    async def renew_peer(self, db: AsyncSession, *, peer: int, holder: str) -> bool:
        """Extend this socket's hold; ``False`` when another socket took it."""
        now = _now()
        renewed = (
            await db.execute(
                update(CrdtPeer)
                .where(CrdtPeer.loro_peer == peer, CrdtPeer.held_by == holder)
                .values(held_until=now + PEER_LEASE, last_seen_at=now)
                .returning(CrdtPeer.loro_peer)
            )
        ).scalar_one_or_none()
        return renewed is not None

    @staticmethod
    async def _forget_peers(db: AsyncSession, ref: DocRef) -> None:
        """A new epoch began: drop every peer no live socket holds. No write
        to the new epoch can come from one (a tab entering it is handed a
        fresh peer), and a write names all of its writer's peers, so without
        this they would pile up across epochs until no write could name them."""
        await db.execute(
            delete(CrdtPeer).where(
                CrdtPeer.org_id == ref.org_id,
                CrdtPeer.doc_type == ref.stored_type,
                CrdtPeer.doc_id == ref.doc_id,
                (CrdtPeer.held_by.is_(None))
                | (CrdtPeer.held_until.is_(None))
                | (CrdtPeer.held_until < _now()),
            )
        )

    async def release_peer(self, db: AsyncSession, *, peer: int, holder: str) -> None:
        await db.execute(
            update(CrdtPeer)
            .where(CrdtPeer.loro_peer == peer, CrdtPeer.held_by == holder)
            .values(held_by=None, held_until=None)
        )


__all__ = ["PEERS_PER_PERSON", "PEER_LEASE", "LoroPeers"]
