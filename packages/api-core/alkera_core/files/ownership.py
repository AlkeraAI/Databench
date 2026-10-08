"""Which deployment a dedup domain's bytes belong to.

A bucket can outlive, or be shared by, the database that wrote into it: a
staging stack pointed at production objects, a blue/green pair, a database
restored into a new deployment, a self-hosted customer re-pointing a bucket.
The row-less collector in :mod:`alkera_core.files.gc` decides "nobody names
this domain" from ONE database, so without a statement of whose bytes these are
it would collect another deployment's live data.

The statement is a small marker object, ``meta/owner.json``, written under the
domain prefix when the domain is created and never rewritten. It names the
deployment by the id of its ``file_stores`` row: minted by the database the
first time it is pointed at the bucket, so two databases sharing one bucket
hold two different ids with nothing for an operator to configure -- and nothing
for two deployments to leave at the same default.

The collector only ever considers a prefix whose marker names ITS deployment.
An unmarked prefix (bytes older than the marker) and a prefix marked by another
deployment are reported and never collected; an operator adopts an unmarked
prefix explicitly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from blake3 import blake3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import Settings
from alkera_core.files.store.errors import PreconditionFailed, StoreError
from alkera_core.files.store.keys import DOMAIN_PREFIX

#: The domain-relative key of the marker. ``meta/`` is outside every prefix the
#: reachability sweep walks, and the collector steps over it by name.
OWNER_KEY: Final = "meta/owner.json"
OWNER_PREFIX: Final = "meta/"

#: A marker larger than this is not one this code wrote.
_MAX_MARKER_BYTES: Final = 4096


class _Putter(Protocol):
    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
    ) -> Any: ...


class _Getter(Protocol):
    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> Any: ...


async def _one(body: bytes) -> AsyncIterator[bytes]:
    yield body


def marker_body(deployment_id: str, *, written_at: datetime) -> bytes:
    """The marker's bytes. Versioned so a later reader can tell shapes apart."""
    return json.dumps(
        {"v": 1, "deployment_id": deployment_id, "written_at": written_at.isoformat()},
        sort_keys=True,
    ).encode()


async def write_marker(
    store: _Putter, deployment_id: str, *, key: str = OWNER_KEY, now: datetime
) -> bool:
    """Stamp a domain as this deployment's. Returns whether this call wrote it.

    Never overwrites: a marker that is already there -- this deployment's or
    another's -- stands. ``store`` is the domain-bound handle and ``key`` the
    relative marker key; the adopt command passes the bucket-wide handle and
    the absolute key instead.
    """
    body = marker_body(deployment_id, written_at=now)
    try:
        await store.put(
            key, _one(body), size=len(body), checksum=blake3(body).digest(), if_absent=True
        )
    except PreconditionFailed:
        return False
    return True


def marker_key(domain: str) -> str:
    """The bucket-absolute key of ``domain``'s marker."""
    return f"{DOMAIN_PREFIX}{domain}/{OWNER_KEY}"


@dataclass(frozen=True, slots=True)
class OwnerStamp:
    """What a domain's marker says: whose it is, and when that was said."""

    deployment_id: str
    written_at: datetime | None
    """``None`` when the marker carries no readable time; the collector reads
    that as "newer than anything", the refusing direction."""


async def read_owner(admin_store: _Getter, domain: str) -> OwnerStamp | None:
    """The stamp on ``domain``, or ``None``.

    Every way of not knowing reads as ``None``: no marker, a store that would
    not answer, bytes that are not a marker. The collector treats ``None`` as
    "not mine", so not knowing can only ever keep bytes.
    """
    try:
        body = await admin_store.get(marker_key(domain))
        chunks: list[bytes] = []
        total = 0
        async for chunk in body:
            total += len(chunk)
            if total > _MAX_MARKER_BYTES:
                return None
            chunks.append(chunk)
    except (StoreError, OSError, KeyError):
        return None
    try:
        parsed = json.loads(b"".join(chunks))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    owner = parsed.get("deployment_id")
    if not isinstance(owner, str) or not owner:
        return None
    return OwnerStamp(deployment_id=owner, written_at=_parse_time(parsed.get("written_at")))


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def store_identity(config: Settings) -> dict[str, str]:
    """The four columns that identify the configured bucket's ``file_stores`` row.

    One spelling, shared by the API that creates the row and the worker that
    reads its id, so the two can never disagree about which row is "ours".
    """
    driver = config.files_store_driver
    return {
        "driver": driver,
        "bucket": config.files_store_bucket or "",
        "endpoint": config.files_store_endpoint
        or (str(config.files_store_root_path) if driver == "filesystem" else ""),
        "region": config.files_store_region if driver != "filesystem" else "",
    }


async def deployment_id_for(session: AsyncSession, config: Settings) -> str | None:
    """This database's id for the configured bucket, or ``None`` before first use.

    ``file_stores`` is a platform table with no row-level policy, so this read
    needs no tenant context. The row is created by the API the first time a
    drive is provisioned; a deployment that has never provisioned one owns no
    bytes and has nothing to collect.
    """
    row = (
        await session.execute(
            text(
                "SELECT id FROM file_stores WHERE driver = :driver AND bucket = :bucket "
                "AND endpoint = :endpoint AND region = :region"
            ),
            store_identity(config),
        )
    ).first()
    return None if row is None else str(uuid.UUID(str(row[0])))


__all__ = [
    "OWNER_KEY",
    "OWNER_PREFIX",
    "OwnerStamp",
    "deployment_id_for",
    "marker_body",
    "marker_key",
    "read_owner",
    "store_identity",
    "write_marker",
]
