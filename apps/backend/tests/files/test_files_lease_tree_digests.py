"""The walk's digest request through the real app, driven by the holder's double.

A holder walks its disk to find what its watcher missed and asks the drive for
the digests of the folders it walked; only the folders that differ are sent.
So the drive's answer must agree with the holder's own digest of the same
facts -- the reported facet while bytes are on their way, the landed bytes once
they are not -- and must be answerable only by the holder, at a cost that does
not grow with the number of folders asked about.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from _live_holder import MockHolder
from _oracle import probe
from alkera_core.auth import revocation
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.digest import DigestChild, directory_digest
from alkera_core.schemas.files.lease_tree import DIGEST_MAX_NAMES, DIGEST_MAX_PATHS
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.files._boxes import registered_box as _registered_box

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

MTIME = 1_758_625_000_123_456_789


async def _held_project(
    client: AsyncClient, fx: Any, session: AsyncSession, idem: Any, *, machine: str = "ana-mbp"
) -> tuple[Any, Any, MockHolder]:
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    holder = MockHolder(client, drive.id, project.id, machine=machine)
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    return drive, project, holder


def _file(path: str, *, size: int = 5, mtime: int = MTIME, **extra: Any) -> dict[str, Any]:
    return {"op": "upsert", "path": path, "kind": "file", "size": size, "mtime_ns": mtime, **extra}


def _expect(*children: DigestChild) -> dict[str, Any]:
    return directory_digest(children).wire()


async def _node_id(session: AsyncSession, drive_id: uuid.UUID, name: bytes) -> uuid.UUID:
    value = (
        await session.execute(
            text(
                "SELECT id FROM file_nodes WHERE drive_id = :d AND name = :n AND trashed_at IS NULL"
            ),
            {"d": drive_id, "n": name},
        )
    ).scalar_one()
    await session.commit()
    return uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# The answer agrees with the holder's own digest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "compress", [pytest.param(False, id="plain"), pytest.param(True, id="gzip")]
)
async def test_each_folder_digests_its_direct_children_as_the_holder_would(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, compress: bool
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    reported = await holder.tree(
        [
            _file("README.md", size=11),
            _file("src/a.py", size=23, mtime=MTIME + 1),
            _file("src/deep/b.py", size=7),
            {"op": "upsert", "path": "empty", "kind": "dir"},
        ]
    )
    assert reported.status_code == 200, reported.text

    answer = await holder.digests(
        ["", "src", "src/deep", "empty", "not-there", "README.md"], compress=compress
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["digests"] == {
        "": _expect(
            DigestChild("file", b"README.md", 11, MTIME),
            DigestChild("dir", b"src"),
            DigestChild("dir", b"empty"),
        ),
        "src": _expect(DigestChild("file", b"a.py", 23, MTIME + 1), DigestChild("dir", b"deep")),
        "src/deep": _expect(DigestChild("file", b"b.py", 7, MTIME)),
        "empty": _expect(),
        # A folder the drive does not have, and a path that is a file, hold
        # no children: the holder's walk finds the difference one level up.
        "not-there": _expect(),
        "README.md": _expect(),
    }


@pytest.mark.parametrize(
    ("reported_size", "facet_kept"),
    [
        pytest.param(None, False, id="landed-bytes-equal-to-the-report-read-from-the-head"),
        pytest.param(99, True, id="a-report-ahead-of-the-landed-bytes-reads-from-the-facet"),
    ],
)
async def test_a_file_reads_its_facet_while_it_has_one_and_its_head_after(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    reported_size: int | None,
    facet_kept: bool,
) -> None:
    payload = b"hello, world"
    drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    size = len(payload) if reported_size is None else reported_size
    assert (await holder.tree([_file("note.txt", size=size)])).status_code == 200
    node = await _node_id(real_session, drive.id, b"note.txt")
    landed = await holder.push(real_session, idem, node, payload)
    assert landed.status_code in (200, 201), landed.text
    facet = (
        await real_session.execute(
            text("SELECT holder_size FROM file_nodes WHERE id = :n"), {"n": node}
        )
    ).scalar_one()
    await real_session.commit()
    assert (facet is not None) is facet_kept

    answer = await holder.digests([""])
    assert answer.status_code == 200, answer.text
    # Either way the digest is the disk's: the facet's size while the drive
    # is behind, the head's (equal to it) once it is current.
    assert answer.json()["digests"][""] == _expect(DigestChild("file", b"note.txt", size, MTIME))


# ---------------------------------------------------------------------------
# Only the holder, under the fence
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def box(real_session: AsyncSession, files_org: Any) -> AsyncIterator[tuple[AsyncClient, str]]:
    token, machine_id = await _registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


@pytest.mark.parametrize(
    "fence",
    [
        pytest.param("the-boxs-own-pair", id="a-person-replaying-the-boxs-fence"),
        pytest.param("a-stale-epoch", id="the-holders-instance-at-a-stale-epoch"),
        pytest.param("none", id="no-fence-at-all"),
    ],
)
async def test_a_caller_who_is_not_the_holder_is_refused_with_409(
    box: tuple[AsyncClient, str],
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    fence: str,
) -> None:
    client, machine_id = box
    _drive, _project, holder = await _held_project(
        client, fx, real_session, idem, machine=machine_id
    )
    assert (await holder.tree([_file("mine.txt")])).status_code == 200
    assert (await holder.digests([""])).status_code == 200

    if fence == "a-stale-epoch":
        refused = await holder.digests(
            [""], names=[""], headers=holder.stale_fence(holder.epoch - 1)
        )
    else:
        impostor = MockHolder(files_client, holder.drive_id, holder.node_id)
        refused = await impostor.digests(
            [""], names=[""], headers=holder.fence if fence != "none" else {}
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"
    assert "mine.txt" not in refused.text


async def test_a_released_lease_answers_no_digest(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    _drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    await real_session.execute(
        text("UPDATE file_leases SET released_at = now() WHERE node_id = :n"), {"n": project.id}
    )
    await real_session.commit()
    refused = await holder.digests([""])
    assert (refused.status_code, refused.json()["code"]) == (409, "files.lease_fenced")


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "status"),
    [
        pytest.param(DIGEST_MAX_PATHS, 200, id="the-ceiling-is-answered"),
        pytest.param(DIGEST_MAX_PATHS + 1, 413, id="one-past-it-is-a-413-to-split-on"),
    ],
)
async def test_a_request_past_the_ceiling_is_a_413(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    count: int,
    status: int,
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    answer = await holder.digests([f"d{i}" for i in range(count)], compress=True)
    assert answer.status_code == status, answer.text
    if status == 413:
        assert answer.json()["code"] == "files.batch_too_large"
    else:
        assert len(answer.json()["digests"]) == count


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../outside", id="a-parent-segment"),
        pytest.param("/abs", id="a-leading-slash"),
        pytest.param("a//b", id="an-empty-segment"),
    ],
)
async def test_a_path_outside_the_lease_is_refused_by_name(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, path: str
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    refused = await holder.digests(["", path])
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_mismatch"
    assert refused.json()["detail"]["paths"] == [path]


async def test_the_request_costs_one_statement_whatever_it_names(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """Resolving the folders and reading their children is ONE grouped
    statement: an empty request skips it, one folder pays it, four thousand
    pay exactly the same."""
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file(f"d{i}/f.txt") for i in range(40)])).status_code == 200
    url = f"{holder.item}/lease/tree/digests"
    counts: dict[str, int] = {}
    for label, paths in (
        ("none", []),
        ("one", [""]),
        ("many", ["", *(f"d{i}" for i in range(DIGEST_MAX_PATHS - 1))]),
    ):
        revocation._cache.reset()
        seen = await probe(
            files_client,
            engine.sync_engine,
            "POST",
            url,
            json={"paths": paths},
            headers=holder.fence,
        )
        assert seen.status == 200, seen.body
        counts[label] = seen.statements
    assert counts["one"] - counts["none"] == 1, counts
    assert counts["many"] == counts["one"], counts


async def test_listing_children_costs_one_more_statement_whatever_it_lists(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The names of the listed folders' children are ONE further read: one
    folder listed pays it, a thousand pay exactly the same."""
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file(f"d{i}/f.txt") for i in range(40)])).status_code == 200
    url = f"{holder.item}/lease/tree/digests"
    folders = ["", *(f"d{i}" for i in range(DIGEST_MAX_NAMES - 1))]
    counts: dict[str, int] = {}
    for label, names in (("none", []), ("one", [""]), ("many", folders)):
        revocation._cache.reset()
        seen = await probe(
            files_client,
            engine.sync_engine,
            "POST",
            url,
            json={"paths": folders, "names": names},
            headers=holder.fence,
        )
        assert seen.status == 200, seen.body
        counts[label] = seen.statements
    assert counts["one"] - counts["none"] == 1, counts
    assert counts["many"] == counts["one"], counts


# ---------------------------------------------------------------------------
# The children of the folders a walk asks to list
# ---------------------------------------------------------------------------


async def test_names_lists_exactly_the_live_children_of_the_folders_named(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """Files and folders alike, as the drive spells them; a folder asked for
    its digest only lists nothing; a trashed child is not a child; a folder
    the drive does not have lists none."""
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    reported = await holder.tree(
        [
            _file("README.md"),
            _file("src/a.py"),
            _file("src/gone.py"),
            _file("src/deep/b.py"),
            {"op": "upsert", "path": "src/empty", "kind": "dir"},
        ]
    )
    assert reported.status_code == 200, reported.text
    assert (await holder.tree([{"op": "delete", "path": "src/gone.py"}])).status_code == 200

    answer = await holder.digests(
        ["", "src", "src/deep", "not-there"], names=["src", "src/deep", "not-there"]
    )

    assert answer.status_code == 200, answer.text
    listed = {path: sorted(names) for path, names in answer.json()["children"].items()}
    assert listed == {
        "src": ["a.py", "deep", "empty"],
        "src/deep": ["b.py"],
        "not-there": [],
    }
    assert set(answer.json()["digests"]) == {"", "src", "src/deep", "not-there"}


async def test_a_request_without_names_answers_no_children(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("a.txt")])).status_code == 200

    answer = await holder.digests([""])

    assert answer.status_code == 200, answer.text
    assert answer.json()["children"] == {}


async def test_a_child_the_drive_still_owes_the_holder_is_not_listed(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A person's write the box has not taken yet is missing from its disk
    because it has not arrived, not because the box deleted it: listing it
    would have the walk trash it. The same for a folder holding such a write."""
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    reported = await holder.tree([_file("kept.txt"), _file("drop.csv"), _file("new/inner.csv")])
    assert reported.status_code == 200, reported.text
    for name in (b"drop.csv", b"inner.csv"):
        node = await _node_id(real_session, drive.id, name)
        await real_session.execute(
            text(
                "INSERT INTO file_lease_live_entries "
                "(lease_node_id, node_id, org_team_id, state, lease_epoch, seq, updated_at) "
                "SELECT :lease, n.id, n.org_team_id, 'inbound', :epoch, 1, now() "
                "FROM file_nodes n WHERE n.id = :node"
            ),
            {"lease": project.id, "node": node, "epoch": holder.epoch},
        )
    await real_session.commit()

    answer = await holder.digests(["", "new"], names=["", "new"])

    assert answer.status_code == 200, answer.text
    assert answer.json()["children"] == {"": ["kept.txt"], "new": []}


async def test_an_undecodable_name_travels_as_its_surrogate_escape_and_back(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A child whose name is not UTF-8 -- one another writer filed, since a
    tree report cannot carry it -- is listed as ``surrogateescape`` decodes
    its bytes, escaped in an all-ASCII body, so it reads back as those bytes
    rather than failing the whole answer."""
    raw = b"r\xe9sum\xe9.txt"
    _drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("plain.txt")])).status_code == 200
    await fx.node(raw, parent=project)

    answer = await holder.digests([""], names=[""])

    assert answer.status_code == 200, answer.text
    assert answer.content.isascii()
    listed = answer.json()["children"][""]
    assert sorted(name.encode("utf-8", "surrogateescape") for name in listed) == [
        b"plain.txt",
        raw,
    ]


async def test_a_name_that_is_not_one_of_the_paths_is_a_422(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)

    refused = await holder.digests(["", "a"], names=["a", "b"])

    assert refused.status_code == 422, refused.text


@pytest.mark.parametrize(
    ("count", "status"),
    [
        pytest.param(DIGEST_MAX_NAMES, 200, id="the-ceiling-is-answered"),
        pytest.param(DIGEST_MAX_NAMES + 1, 413, id="one-past-it-is-a-413-to-split-on"),
    ],
)
async def test_names_past_their_ceiling_are_a_413(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    count: int,
    status: int,
) -> None:
    _drive, _project, holder = await _held_project(files_client, fx, real_session, idem)
    folders = [f"d{i}" for i in range(count)]

    answer = await holder.digests(folders, names=folders, compress=True)

    assert answer.status_code == status, answer.text
    if status == 413:
        assert answer.json()["code"] == "files.batch_too_large"
    else:
        assert len(answer.json()["children"]) == count
