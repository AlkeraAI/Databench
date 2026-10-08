"""What a download of a very large folder costs, and what it still refuses.

A zip of a subtree is the one Files verb whose work grows with the *shape* of
the tree rather than with the size of the request, so it is the one verb where
"it works on my folder" says nothing about the folder somebody else has. The
cases here are all about the growth: the rows the walk holds, the statements it
spends, and what it enumerates back to the caller — each pinned against a tree
big enough that an implementation which reads the subtree in one go, or decides
one node per statement, fails rather than merely slows down.

The security half is pinned at the same time and in the same cases, because a
cheaper walk is only worth having if it refuses exactly what the expensive one
refused: the hidden child is asserted absent at both page sizes and on both
sides of the inline ceiling, so an optimisation that skipped a page's decision
could not pass any of them.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Callable

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures
from _oracle import counting
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from backend.services.files.archive import SKIP_NO_EXPORT, SKIPPED_MEMBER
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_files_download_routes import (  # noqa: F401  (content_on/tree are fixtures)
    Tree,
    _put_bytes,
    _redeem,
    _start_download,
    _token_of,
    content_on,
    tree,
)

pytestmark = pytest.mark.asyncio

SYNC_ENGINE = engine.sync_engine

#: A leaf's bytes. Small on purpose: these cases are about the count of nodes,
#: never the size of one.
LEAF = b"x\n"


async def _fan_out(
    client: AsyncClient,
    session: AsyncSession,
    fx: FilesFixtures,
    drive_id: uuid.UUID,
    parent: FileNode,
    count: int,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``count`` sibling files under ``parent``, each with real bytes."""
    for index in range(count):
        leaf = await fx.node(f"leaf-{index:05d}.txt".encode(), parent=parent)
        await _put_bytes(client, session, drive_id, leaf, LEAF, idem)


@pytest_asyncio.fixture
async def wide(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,  # noqa: F811  (the imported fixture)
    idem: Callable[[], dict[str, str]],
) -> Tree:
    """The fixture tree with a folder of many siblings hung off it."""
    wide_folder = await fx.node(b"wide", kind="folder", parent=tree.root)
    await _fan_out(files_client, real_session, fx, tree.drive_id, wide_folder, 40, idem)
    return tree


async def _hidden_chat(
    client: AsyncClient,
    session: AsyncSession,
    fx: FilesFixtures,
    drive_id: uuid.UUID,
    parent: FileNode,
    name: bytes,
    idem: Callable[[], dict[str, str]],
) -> FileNode:
    """A chat folder with a file in it: readable, never exportable."""
    chat = await fx.node(
        name,
        kind="folder",
        parent=parent,
        subtype="chat",
        target_object_id=uuid.uuid4(),
        flags=NO_DOWNLOAD_BIT,
    )
    inside = await fx.node(b"notes.md", parent=chat)
    await _put_bytes(client, session, drive_id, inside, b"private\n", idem)
    return chat


async def _archive(
    client: AsyncClient,
    session: AsyncSession,
    subject: Tree,
    idem: Callable[[], dict[str, str]],
) -> tuple[dict[str, object], bytes]:
    """Start the download and redeem it: the operation body and the zip."""
    started = await _start_download(client, session, subject.drive_id, subject.root.id, idem)
    assert started.status_code == 202, started.text
    body = started.json()
    served = await _redeem(client, _token_of(body["resultUrl"]))
    assert served.status_code == 200, served.text
    return body, served.content


def _members(raw: bytes) -> tuple[set[str], str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        manifest = archive.read(SKIPPED_MEMBER).decode("utf-8") if SKIPPED_MEMBER in names else ""
    return names, manifest


async def test_no_download_path_reads_the_whole_subtree_in_one_statement(
    files_client: AsyncClient,
    real_session: AsyncSession,
    wide: Tree,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither planning nor serving may fall back to the uncapped read.

    ``nodes_by_path_prefix`` has no ``LIMIT``: a folder of a hundred thousand
    items is a hundred thousand rows resident, in the request. Other callers
    still have a use for it, so the claim is made where it matters — the two
    download paths — by making the unbounded read fail loudly for the length of
    both.
    """

    async def refuse(self: FilesRepo, prefix: str) -> list[FileNode]:
        raise AssertionError("the download walk read the whole subtree at once")

    monkeypatch.setattr(FilesRepo, "nodes_by_path_prefix", refuse)
    body, raw = await _archive(files_client, real_session, wide, idem)
    names, _ = _members(raw)
    assert body["state"] == "done"
    assert len([name for name in names if name.startswith("wide/leaf-")]) == 40


async def test_a_wider_folder_costs_the_same_statements_while_it_fits_a_page(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    wide: Tree,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Doubling the members must not double the queries.

    Two downloads of the same folder, the second with forty more files in it,
    both inside one page: an implementation that decides a node at a time pays
    per node and fails here by a three-figure margin, while a page-at-a-time
    walk pays per *page* and comes back with the same count. The members are
    asserted too, so "the same count" cannot be bought by reading less.
    """
    assert settings.files_archive_page_nodes >= 200, "this case needs one page"
    first = await _start_download(files_client, real_session, wide.drive_id, wide.root.id, idem)
    assert first.status_code == 202, first.text
    with counting(SYNC_ENGINE) as before:
        small = await _redeem(files_client, _token_of(first.json()["resultUrl"]))
    assert small.status_code == 200

    more = await fx.node(b"wider", kind="folder", parent=wide.root)
    await _fan_out(files_client, real_session, fx, wide.drive_id, more, 40, idem)

    second = await _start_download(files_client, real_session, wide.drive_id, wide.root.id, idem)
    assert second.status_code == 202, second.text
    with counting(SYNC_ENGINE) as after:
        big = await _redeem(files_client, _token_of(second.json()["resultUrl"]))
    assert big.status_code == 200

    grown, plain = _members(big.content)[0], _members(small.content)[0]
    assert len(grown) - len(plain) == 41, "forty more files and the folder holding them"
    # Every member's bytes are still fetched one at a time, so the count grows
    # by the per-member reads and by nothing else: the *decision* is free.
    per_member = (len(after) - len(before)) / 40
    assert per_member <= 3, (
        f"{len(before)} statements for {len(plain)} members, {len(after)} for "
        f"{len(grown)}: the walk is paying per node, not per page"
    )


@pytest.mark.parametrize("page", [1, 5, 1_000], ids=["one-per-page", "many-pages", "one-page"])
async def test_a_chat_folder_is_pruned_at_every_page_size(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,  # noqa: F811  (the imported fixture)
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    page: int,
) -> None:
    """A page boundary is not a hole in the walk's refusals.

    The refusal of a chat folder prunes its children, and the children are only
    known to be pruned because the parent was decided first. Pages preserve that
    order — shallowest first — so the same tree refuses the same subtree whether
    it arrives in one page or one node at a time, including when the boundary
    falls between the chat and the file inside it.
    """
    monkeypatch.setattr(settings, "files_archive_page_nodes", page)
    chat = await _hidden_chat(
        files_client, real_session, fx, tree.drive_id, tree.root, b"Kickoff.alkerachat", idem
    )
    body, raw = await _archive(files_client, real_session, tree, idem)
    names, manifest = _members(raw)
    assert not [name for name in names if name.startswith("Kickoff.alkerachat")]
    assert tree.expected_names <= names
    assert f"{chat.id}\t{SKIP_NO_EXPORT}" in manifest
    assert [error["itemId"] for error in body["errors"]] == [str(chat.id)]


async def test_a_subtree_over_the_inline_ceiling_is_answered_by_its_size(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,  # noqa: F811  (the imported fixture)
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the ceiling the request stops enumerating — and refuses the same.

    The operation comes back with the subtree's size rather than a list of
    every omission, which is the whole point of the ceiling; what must NOT
    change is the archive, so the chat folder is still absent from it and still
    named in its manifest. A ceiling that skipped the decision as well as the
    enumeration would pass the first assertion and fail the last two.
    """
    monkeypatch.setattr(settings, "files_download_inline_max_nodes", 0)
    chat = await _hidden_chat(
        files_client, real_session, fx, tree.drive_id, tree.root, b"Kickoff.alkerachat", idem
    )
    body, raw = await _archive(files_client, real_session, tree, idem)
    counted = await real_session.execute(
        text(
            "SELECT count(*) FROM file_nodes WHERE drive_id = :drive "
            "AND path_ids <@ (SELECT path_ids FROM file_nodes WHERE id = :root)"
        ),
        {"drive": tree.drive_id, "root": tree.root.id},
    )
    assert body["total"] == int(counted.scalar_one())
    assert body["errors"] == [], "an unenumerated plan lists nothing it has not decided"
    names, manifest = _members(raw)
    assert not [name for name in names if name.startswith("Kickoff.alkerachat")]
    assert tree.expected_names <= names
    assert f"{chat.id}\t{SKIP_NO_EXPORT}" in manifest


async def test_the_omissions_stop_at_the_ceiling_and_say_how_many_more(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,  # noqa: F811  (the imported fixture)
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refusals, room recorded for one, and the second is counted.

    A subtree none of which the caller may export would otherwise put one row
    per node into a single jsonb column and one line per node into the archive.
    The cap is what stops that, and a cap that silently dropped the remainder
    would be worse than the unbounded list, so the count is asserted in both
    places it is published.
    """
    monkeypatch.setattr(settings, "files_archive_max_recorded_skips", 1)
    for name in (b"Alpha.alkerachat", b"Beta.alkerachat"):
        await _hidden_chat(files_client, real_session, fx, tree.drive_id, tree.root, name, idem)
    body, raw = await _archive(files_client, real_session, tree, idem)
    assert len(body["errors"]) == 1
    _, manifest = _members(raw)
    assert len([line for line in manifest.splitlines() if not line.startswith("#")]) == 1
    assert "# and 1 more, not listed" in manifest
