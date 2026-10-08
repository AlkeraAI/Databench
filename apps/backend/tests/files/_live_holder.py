"""The box's half of the live plane, as a test double that speaks the routes.

The real holder is the mirror inside the CLI: it takes a chat's folder, keeps
the fence headers, reports what it is doing to each file at its own cadence,
drains what people dropped into the chat while it ran, and says it applied
them. None of that is a mock here — every method below issues the same HTTP
request the box issues, against the mounted app and a real Postgres — so a
route that stops answering, renames a field or tightens a refusal fails the
tests that use this class rather than being papered over by a stub.

It exists so the live-plane tests read as "a holder did X, then a person did
Y", which is the only way the ordering invariants (a drop must be offered once
and only once; a settle must survive a resend; a stale fence must see nothing)
are legible at all.
"""

from __future__ import annotations

import gzip
import json
import uuid
from typing import Any

from blake3 import blake3
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PREFIX = "/api/v1/files"

#: Every state a holder may report. ``applied`` and ``superseded`` are the two
#: that CLEAR a row; the rest say what the box is doing with the file.
REPORTABLE = ("writing", "uploading", "on_box", "deferred", "applied", "superseded")


class MockHolder:
    """One box holding one folder, driving the routes a real holder drives."""

    def __init__(
        self,
        client: AsyncClient,
        drive_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        instance: str = "mirror-1",
        machine: str = "box-7",
    ) -> None:
        self._client = client
        self.drive_id = drive_id
        self.node_id = node_id
        self.instance = instance
        self.machine = machine
        self.epoch: int | None = None
        #: Whether this box says its If-Match is the version its bytes were
        #: made on, as every box since that fix does; False is an older box.
        self.names_its_base = True
        #: The last grant the drive handed back, whichever route handed it.
        self.grant: dict[str, Any] = {}

    # -- addressing --------------------------------------------------------

    @property
    def item(self) -> str:
        return f"{PREFIX}/drives/{self.drive_id}/items/{self.node_id}"

    @property
    def fence(self) -> dict[str, str]:
        """The two headers every fenced call carries. Empty before the take, so
        a test that forgets to take the folder fails as an unfenced caller."""
        if self.epoch is None:
            return {}
        fence = {
            "X-Alkera-Lease-Epoch": str(self.epoch),
            "X-Alkera-Lease-Instance": self.instance,
        }
        if self.names_its_base:
            fence["X-Alkera-Lease-Base"] = "agreed"
        return fence

    @property
    def handing_back(self) -> dict[str, str]:
        """The fence of a box that claims this write is the one going with its
        release. The drive reads the epoch and the instance and nothing else:
        which push is the last is a box's own word, so it buys no room."""
        return {**self.fence, "X-Alkera-Lease-Final": "1"}

    def stale_fence(self, epoch: int = 0) -> dict[str, str]:
        """The fence of a holder that has been superseded."""
        return {
            "X-Alkera-Lease-Epoch": str(epoch),
            "X-Alkera-Lease-Instance": self.instance,
        }

    # -- the lease ---------------------------------------------------------

    async def take(
        self,
        session: AsyncSession,
        idem: Any,
        *,
        purpose: str = "chat",
        inbound: bool = False,
        live: bool = False,
    ) -> Any:
        body: dict[str, Any] = {
            "instanceId": self.instance,
            "machineId": self.machine,
            "purpose": purpose,
            "inbound": inbound,
            "live": live,
        }
        answer = await self._client.post(
            f"{self.item}/lease",
            json=body,
            headers={**idem(), "If-Match": await _etag(session, self.node_id)},
        )
        if answer.status_code == 200:
            self.grant = answer.json()
            self.epoch = self.grant["epoch"]
        return answer

    async def release(
        self,
        session: AsyncSession,
        idem: Any,
        *,
        unsynced_count: int | None = None,
        unsynced_paths: list[str] | None = None,
    ) -> Any:
        """The hand-back, with what the drain could not land when it has any."""
        body: dict[str, Any] = {"epoch": self.epoch, "instanceId": self.instance, "final": None}
        if unsynced_count is not None:
            body["unsyncedCount"] = unsynced_count
        if unsynced_paths is not None:
            body["unsyncedPaths"] = unsynced_paths
        return await self._client.post(
            f"{self.item}/lease/release",
            json=body,
            headers={**idem(), "If-Match": await _etag(session, self.node_id)},
        )

    async def beat(self) -> Any:
        answer = await self._client.post(
            f"{self.item}/lease/heartbeat",
            json={"epoch": self.epoch, "instanceId": self.instance},
        )
        if answer.status_code == 200:
            self.grant = answer.json()
        return answer

    # -- the live plane ----------------------------------------------------

    async def report(
        self, entries: list[dict[str, Any]], *, headers: dict[str, str] | None = None
    ) -> Any:
        """One live batch, exactly as the box sends it."""
        return await self._client.post(
            f"{self.item}/lease/live",
            json={"entries": entries},
            headers=self.fence if headers is None else headers,
        )

    async def tree(
        self,
        entries: list[dict[str, Any]],
        *,
        batch_id: uuid.UUID | None = None,
        compress: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """One tree report, exactly as the box sends it: snake_case JSON, a
        fresh batch id unless a replay names one, gzipped when asked."""
        body = json.dumps({"batch_id": str(batch_id or uuid.uuid4()), "entries": entries}).encode()
        sent = {**(self.fence if headers is None else headers), "Content-Type": "application/json"}
        if compress:
            body = gzip.compress(body)
            sent["Content-Encoding"] = "gzip"
        return await self._client.post(f"{self.item}/lease/tree", content=body, headers=sent)

    async def digests(
        self,
        paths: list[str],
        *,
        names: list[str] | None = None,
        compress: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """The drive's digests of the folders a walk names, asked the way the
        box asks: snake_case JSON, gzipped when asked, and ``names`` -- the
        folders whose children the walk wants listed -- only when given."""
        asked: dict[str, Any] = {"paths": paths}
        if names is not None:
            asked["names"] = names
        body = json.dumps(asked).encode()
        sent = {**(self.fence if headers is None else headers), "Content-Type": "application/json"}
        if compress:
            body = gzip.compress(body)
            sent["Content-Encoding"] = "gzip"
        return await self._client.post(
            f"{self.item}/lease/tree/digests", content=body, headers=sent
        )

    async def settle(self, node_id: uuid.UUID, state: str = "applied") -> Any:
        return await self.report([{"nodeId": str(node_id), "state": state}])

    async def drain(self, *, headers: dict[str, str] | None = None) -> Any:
        return await self._client.get(
            f"{self.item}/lease/live",
            params={"inbound": "true"},
            headers=self.fence if headers is None else headers,
        )

    # -- the bytes ---------------------------------------------------------

    async def push(
        self,
        session: AsyncSession,
        idem: Any,
        node_id: uuid.UUID,
        payload: bytes,
        *,
        final: bool = False,
        base: int | None = None,
    ) -> Any:
        """The bytes themselves, onto a node the holder already skeletoned.

        ``base`` is the etag the holder last agreed with the drive; left out it
        is the node's current one, a holder that is up to date.
        """
        return await self._client.put(
            f"{PREFIX}/drives/{self.drive_id}/items/{node_id}/content",
            content=payload,
            headers={
                **idem(),
                **(self.handing_back if final else self.fence),
                "If-Match": str(base) if base is not None else await _etag(session, node_id),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
            },
        )

    async def submit_conflict(
        self,
        idem: Any,
        parent_id: uuid.UUID,
        name: str,
        payload: bytes,
        *,
        conflict_of: uuid.UUID | None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """File the bytes an inbound write displaced on the box: open a session
        with ``conflictOf``, send the one part, complete. Answers the open's
        refusal when it refuses, else the completion."""
        body: dict[str, Any] = {
            "declaredSize": len(payload),
            "name": name,
            "parentId": str(parent_id),
            "conflictCopy": True,
        }
        if conflict_of is not None:
            body["conflictOf"] = str(conflict_of)
        fence = self.fence if headers is None else headers
        opened = await self._client.post(
            f"{PREFIX}/uploads", json=body, headers={**idem(), **fence}
        )
        if opened.status_code != 201:
            return opened
        upload_id = opened.json()["uploadId"]
        checksum = blake3(payload).digest().hex()
        part = await self._client.put(
            f"{PREFIX}/uploads/{upload_id}/parts/1",
            content=payload,
            headers={**idem(), "X-Part-Checksum": checksum},
        )
        assert part.status_code == 200, part.text
        return await self._client.post(
            f"{PREFIX}/uploads/{upload_id}/complete",
            json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
            headers=idem(),
        )

    # -- the file's live document, as its text peer -------------------------

    async def live_text(self, node_id: uuid.UUID, *, headers: dict[str, str] | None = None) -> Any:
        """The file's live document as text, read as the box reads it."""
        return await self._client.get(
            f"{PREFIX}/drives/{self.drive_id}/items/{node_id}/live",
            headers=self.fence if headers is None else headers,
        )

    async def submit_text(
        self,
        node_id: uuid.UUID,
        text: str,
        *,
        base_token: str | None = None,
        base_etag: int | None = None,
        submit_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """The agent's whole text, made on the state ``base_token`` names."""
        body: dict[str, Any] = {"text": text, "submitId": submit_id or uuid.uuid4().hex}
        if base_token is not None:
            body["baseToken"] = base_token
        if base_etag is not None:
            body["baseEtag"] = base_etag
        return await self._client.post(
            f"{PREFIX}/drives/{self.drive_id}/items/{node_id}/live",
            json=body,
            headers=self.fence if headers is None else headers,
        )

    async def owed(self) -> list[tuple[str, str]]:
        """What is waiting for this holder: ``(node id, state)``, oldest first."""
        answer = await self.drain()
        assert answer.status_code == 200, answer.text
        return [(row["nodeId"], row["state"]) for row in answer.json()["entries"]]


async def _etag(session: AsyncSession, node_id: uuid.UUID) -> str:
    row = (
        await session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :node"), {"node": node_id}
        )
    ).scalar_one()
    return str(row)


__all__ = ["REPORTABLE", "MockHolder"]
