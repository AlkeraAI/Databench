"""Two-way writes on a held folder, as properties over random interleavings.

A person works in the chat's folder on the web while the agent works in the
same folder on the box: both write, delete and rename the same few names, in an
order a seeded random draws. The real backend runs on the disposable database,
and the real holder takes the folder over HTTP with its live sync watching the
working directory -- the fixture of ``test_cloud_live_folder``. Two properties
must hold once both sides go quiet, whatever the order was:

* **the trees converge** -- every file on the box is a live file on the drive
  under the same name with the same bytes, and nothing else is;
* **nothing is unrecoverable** -- every content either side ever wrote (and
  that its own side did not replace before it reached the drive) is on the
  drive as a head, a version, or a conflicted copy's head.

The box's own writes wait for their bytes to reach the drive before the next
step, so "written" means one thing on both sides; what interleaves is the
web's writes with the box's, and the box taking the web's changes at random
points rather than at once.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_core.files.hashing import hash_bytes
from backend.api.routes.files import PREFIX
from blake3 import blake3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_cloud_live_folder import (  # noqa: F401  (fixtures registered by import)
    SETUP_WAIT,
    UPLOADS,
    LiveChat,
    _as_member,
    _listing,
    content_server,
    live_chat,
)
from tests.chat_shares import files_on  # noqa: F401

pytestmark = pytest.mark.usefixtures("files_on")

#: The names both sides draw from: few, so they collide.
NAMES = ("a.txt", "b.txt", "c.txt")
#: Steps per seed: enough for every kind of collision to occur, few enough to
#: keep a seed well inside a minute on a shared host.
STEPS = 12
#: How long both sides are given to agree once the steps are done.
QUIET_WAIT = SETUP_WAIT
#: The names the holder keeps to itself on disk, never sent to the drive.
_LOCAL_SUFFIXES = (".alkera-conflict", ".alkera-inbound")
#: What a step may do, writes weighted up so there is something to collide.
OPS = (
    "web_write",
    "web_write",
    "box_write",
    "box_write",
    "web_delete",
    "box_delete",
    "web_rename",
    "box_rename",
    "box_pull",
)


def _content_hash(data: bytes) -> str:
    return hash_bytes(data).content_hash.hex()


async def _web_write(client: httpx.AsyncClient, live: LiveChat, name: str, payload: bytes) -> str:
    """The person saves ``payload`` under ``name`` -- over the file there, at
    the etag they last saw, or as a new file. Answers how the save ended: the
    commit's status, or its operation's final state. A 409, a 412 or a failed
    operation is the box having moved the name first -- a save that did not
    happen, and that the person is told of."""
    row = await _web_row(client, live, name)
    opened = await client.post(
        UPLOADS,
        json={"declaredSize": len(payload), "name": name, "parentId": live.scratch_node_id},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    checksum = blake3(payload).digest().hex()
    sent = await client.put(
        f"{UPLOADS}/{upload_id}/parts/1",
        content=payload,
        headers={"Idempotency-Key": uuid.uuid4().hex, "X-Part-Checksum": checksum},
    )
    assert sent.status_code == 200, sent.text
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    if row is not None:
        headers["If-Match"] = str(row["etag"])
    finished = await client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}],
            "conflictBehavior": "replace" if row is not None else "fail",
        },
        headers=headers,
    )
    assert finished.status_code in (200, 201, 202, 409, 412), finished.text
    if finished.status_code != 202:
        return str(finished.status_code)
    return await _settled_operation(client, live, str(finished.json()["id"]))


#: The states an operation stops in.
_TERMINAL = frozenset({"done", "failed", "cancelled"})


async def _settled_operation(client: httpx.AsyncClient, live: LiveChat, op_id: str) -> str:
    """The state the save's operation ends in, with the failure's code. A 202
    is only the save accepted: the commit runs after it, and a commit whose
    precondition moved meanwhile fails with the caller told -- a save that did
    not happen, not bytes the drive took and lost."""
    deadline = asyncio.get_running_loop().time() + SETUP_WAIT.seconds
    while True:
        polled = await client.get(f"{PREFIX}/drives/{live.drive_id}/operations/{op_id}")
        assert polled.status_code == 200, polled.text
        body = polled.json()
        if body["state"] in _TERMINAL:
            codes = [str(error.get("code")) for error in body.get("errors") or []]
            return body["state"] + (f":{','.join(codes)}" if codes else "")
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"the save's operation {op_id} never finished: {body}")
        await asyncio.sleep(0.05)


async def _web_row(client: httpx.AsyncClient, live: LiveChat, name: str) -> dict[str, Any] | None:
    for row in await _listing(client, live):
        if row.get("name") == name and row.get("file") is not None:
            return row
    return None


async def _web_delete(client: httpx.AsyncClient, live: LiveChat, name: str) -> int | None:
    """The person trashes ``name``; answers the status, ``None`` when absent."""
    row = await _web_row(client, live, name)
    if row is None:
        return None
    done = await client.delete(
        live.url(str(row["id"])),
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(row["etag"])},
    )
    return done.status_code


async def _web_rename(client: httpx.AsyncClient, live: LiveChat, name: str, to: str) -> int | None:
    """The person renames ``name`` to ``to``; answers the status, ``None``
    when absent. A refusal (the name taken) is a step that changed nothing."""
    row = await _web_row(client, live, name)
    if row is None:
        return None
    done = await client.patch(
        live.url(str(row["id"])),
        json={"name": to},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(row["etag"])},
    )
    return done.status_code


async def _drive_hashes(db: AsyncSession, drive_id: str) -> set[str]:
    """Every content the drive can give back: each version of every node in
    the drive, trashed nodes' included -- the trash restores them."""
    db.expire_all()
    rows = await db.execute(
        text(
            "SELECT DISTINCT v.content_hash FROM file_versions v "
            "JOIN file_nodes n ON n.id = v.node_id WHERE n.drive_id = :drive"
        ),
        {"drive": uuid.UUID(drive_id)},
    )
    hashes = {str(row.content_hash) for row in rows}
    await db.commit()
    return hashes


async def _drive_tree(db: AsyncSession, live: LiveChat) -> dict[str, str | None]:
    """The live files the drive lists in the working directory: name to head
    content hash, ``None`` while a row has no bytes yet."""
    db.expire_all()
    rows = await db.execute(
        text(
            "SELECT n.name, v.content_hash FROM file_nodes n "
            "LEFT JOIN file_versions v ON v.id = n.head_version_id "
            "WHERE n.parent_id = :parent AND n.trashed_at IS NULL AND n.kind = 'file'"
        ),
        {"parent": uuid.UUID(live.scratch_node_id)},
    )
    tree = {
        bytes(row.name).decode("utf-8", "surrogateescape"): (
            str(row.content_hash) if row.content_hash is not None else None
        )
        for row in rows
    }
    await db.commit()
    return tree


def _disk_tree(root: Path) -> dict[str, str | None]:
    return {
        entry.name: _content_hash(entry.read_bytes())
        for entry in root.iterdir()
        if entry.is_file() and not entry.name.endswith(_LOCAL_SUFFIXES)
    }


async def _pull(live: LiveChat) -> None:
    held = live.folders.held(live.chat_id)
    assert held is not None and held.live is not None
    await asyncio.to_thread(held.live.pull_inbound)


async def _landed(db: AsyncSession, live: LiveChat, digest: str) -> None:
    """Wait until the drive holds ``digest`` somewhere: the box's write has
    reached it, as a head or -- when it collided -- as a copy or a version."""
    deadline = asyncio.get_running_loop().time() + SETUP_WAIT.seconds
    while digest not in await _drive_hashes(db, live.drive_id):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"the box's write {digest[:12]} never reached the drive")
        await asyncio.sleep(0.1)


@pytest.mark.parametrize("seed", [pytest.param(seed, id=f"seed-{seed}") for seed in (7, 23, 91)])
async def test_the_trees_converge_and_nothing_written_is_lost(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
    seed: int,
) -> None:
    draw = random.Random(seed)
    root = live_chat.working_dir
    written: set[str] = set()
    saved = {"web": 0, "box": 0}
    trace: list[str] = []
    async with _as_member(live_chat) as member:
        for step in range(STEPS):
            op = draw.choice(OPS)
            name, other = draw.sample(NAMES, 2)
            payload = f"{op} {name} step {step} seed {seed}\n".encode()
            trace.append(f"{op}({name}{', ' + other if 'rename' in op else ''})")
            if op == "web_write":
                status = await _web_write(member, live_chat, name, payload)
                trace[-1] += f"={status}"
                if status in ("200", "201", "done"):
                    written.add(_content_hash(payload))
                    saved["web"] += 1
            elif op == "box_write":
                await asyncio.to_thread((root / name).write_bytes, payload)
                written.add(_content_hash(payload))
                await _landed(real_session, live_chat, _content_hash(payload))
                saved["box"] += 1
            elif op == "web_delete":
                trace[-1] += f"={await _web_delete(member, live_chat, name)}"
            elif op == "box_delete":
                if (root / name).is_file():
                    await asyncio.to_thread((root / name).unlink)
            elif op == "web_rename":
                trace[-1] += f"={await _web_rename(member, live_chat, name, other)}"
            elif op == "box_rename":
                if (root / name).is_file() and not (root / other).exists():
                    await asyncio.to_thread(os.replace, root / name, root / other)
            else:
                await _pull(live_chat)
            await asyncio.sleep(draw.uniform(0.0, 0.3))

        # Quiet: nothing new is written; the box keeps taking what the drive
        # holds for it until the two trees agree, or the wait runs out.
        deadline = asyncio.get_running_loop().time() + QUIET_WAIT.seconds
        while True:
            await _pull(live_chat)
            disk = await asyncio.to_thread(_disk_tree, root)
            drive = await _drive_tree(real_session, live_chat)
            if disk == drive:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"the trees did not converge after {trace}:\n"
                    f"  box   {sorted(disk.items())}\n  drive {sorted(drive.items())}"
                )
            await asyncio.sleep(0.25)

    # Not a vacuous run: both sides saved something the properties cover.
    assert saved["web"] and saved["box"], (saved, trace)
    lost = written - await _drive_hashes(real_session, live_chat.drive_id)
    assert not lost, f"{len(lost)} written content(s) are nowhere on the drive after {trace}"


async def _live_heads(db: AsyncSession, live: LiveChat) -> dict[str, str]:
    """Each live file in the working directory, by name, and its head's hash:
    what a person opening the folder can see without digging in history."""
    return {name: digest for name, digest in (await _drive_tree(db, live)).items() if digest}


async def test_a_web_replace_the_box_appends_over_before_taking_it_loses_neither(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The walkthrough, exactly: the agent appends to shared.txt in a loop and
    each append reaches the drive; a person replaces the file from the browser
    with a line added at the top; before the box takes that version down, the
    agent appends again and the holder uploads. The last arrival (the box's
    append) keeps the name, and the browser's version is the head of a
    conflicted copy beside it -- not a version buried under the append, which
    is what a person saw as their line being gone."""
    root = live_chat.working_dir
    shared = root / "shared.txt"
    body = b""
    for tick in range(3):
        body += f"tick {tick}\n".encode()
        await asyncio.to_thread(shared.write_bytes, body)
        await _landed(real_session, live_chat, _content_hash(body))

    browser = b"a line from the browser\n" + body
    async with _as_member(live_chat) as member:
        # The holder's own bookkeeping on the row (the digest it proved) may
        # still move the etag the person read; a save refused for that is not
        # a save, and the person saves again on what they now see.
        for _attempt in range(20):
            saved = await _web_write(member, live_chat, "shared.txt", browser)
            if saved in ("200", "done"):
                break
            await asyncio.sleep(0.1)
        assert saved in ("200", "done"), saved
        # Inside the inbound window: the box has not taken the web's version.
        appended = body + b"tick 3\n"
        await asyncio.to_thread(shared.write_bytes, appended)
        await _landed(real_session, live_chat, _content_hash(appended))

        heads = await _live_heads(real_session, live_chat)
        assert heads.get("shared.txt") == _content_hash(appended), heads
        copies = {name: digest for name, digest in heads.items() if name != "shared.txt"}
        assert list(copies.values()).count(_content_hash(browser)) == 1, heads
        [copy] = [name for name, digest in copies.items() if digest == _content_hash(browser)]
        assert copy.startswith("shared (conflicted copy from "), copy
        assert copy.endswith(").txt"), copy
        real_session.expire_all()
        states = (
            (
                await real_session.execute(
                    text(
                        "SELECT c.state FROM file_conflicts c "
                        "JOIN file_nodes n ON n.id = c.node_id "
                        "WHERE n.parent_id = :parent AND n.name = :name"
                    ),
                    {"parent": uuid.UUID(live_chat.scratch_node_id), "name": b"shared.txt"},
                )
            )
            .scalars()
            .all()
        )
        await real_session.commit()
        assert list(states) == ["auto"]

        # The box takes what the drive holds for it: the copy lands beside the
        # agent's file, and the agent is told on its next call naming the file.
        deadline = asyncio.get_running_loop().time() + QUIET_WAIT.seconds
        while not (root / copy).is_file():
            await _pull(live_chat)
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"the copy {copy} never reached the box")
            await asyncio.sleep(0.25)
        assert (root / copy).read_bytes() == browser
        assert shared.read_bytes() == appended
        held = live_chat.folders.held(live_chat.chat_id)
        assert held is not None and held.live is not None
        assert held.live.take_conflict_notices([str(shared)]) == [
            f"Your version of shared.txt kept the name; the version saved on the web is at {copy}."
        ]
