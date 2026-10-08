"""Name search: folding, scope, chips, and the page a hidden sibling cannot move."""

from __future__ import annotations

import unicodedata
import uuid

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import search
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import ListFilters
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from sqlalchemy import event
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class _Counter:
    """Counts the SQL statements one block of work issues."""

    def __init__(self, repo: FilesRepo) -> None:
        self._engine = repo.session.get_bind()
        self.count = 0

    def __enter__(self) -> _Counter:
        event.listen(self._engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._seen)

    def _seen(self, *_args: object, **_kw: object) -> None:
        self.count += 1


async def _seed(factory: FilesFactory) -> tuple[DriveId, dict[str, FileNode]]:
    drive = await factory.drive()
    nodes = await factory.tree(
        "papers/ papers/README papers/sub/ papers/sub/deep.txt notes/", drive=drive
    )
    return DriveId(drive.id), nodes


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("readme", "papers/README", id="lowercase-finds-uppercase"),
        pytest.param("README", "papers/README", id="exact"),
        pytest.param("EADM", "papers/README", id="substring-mid-word"),
    ],
)
async def test_search_folds_case(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    query: str,
    expected: str,
) -> None:
    drive_id, nodes = await _seed(files_factory)
    async with repo.transaction():
        page = await search.search_names(repo, _ctx(files_org), drive_id, query)
    assert [row.id for row in page.items] == [nodes[expected].id]


async def test_search_finds_the_nfd_twin_of_an_nfc_query(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The stored key is NFC-folded, so a decomposed query still matches."""
    drive = await files_factory.drive()
    composed = "Café"
    nodes = await files_factory.tree(f"{unicodedata.normalize('NFC', composed)}/", drive=drive)
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    async with repo.transaction():
        page = await search.search_names(repo, _ctx(files_org), DriveId(drive.id), decomposed)
    assert [row.id for row in page.items] == [next(iter(nodes.values())).id]


async def test_an_unreadable_sibling_never_appears_and_never_shortens_the_page(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The page is cut *after* readability, so its size cannot leak one."""
    drive = await files_factory.drive()
    # The hidden one sorts FIRST, so a page that did not exclude it before the
    # LIMIT would contain it — the assertions below could not pass by luck.
    nodes = await files_factory.tree("hit-b/ hit-c/ hit-d/ hit-aaa/", drive=drive)
    hidden = nodes["hit-aaa"].id

    def hides_the_secret(table: object) -> object:
        return FileNode.id != hidden

    async with repo.transaction():
        with_secret = await search.search_names(
            repo, _ctx(files_org), DriveId(drive.id), "hit-", limit=2
        )
        without = await search.search_names(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            "hit-",
            limit=2,
            readable_predicate=hides_the_secret,  # type: ignore[arg-type]
        )
    assert hidden in {row.id for row in with_secret.items}
    assert hidden not in {row.id for row in without.items}
    assert [row.name_key for row in without.items] == ["hit-b", "hit-c"]
    # Same page size and the same "there is more" answer with and without it:
    # the hidden row was excluded before the LIMIT, not after.
    assert len(with_secret.items) == len(without.items) == 2
    assert with_secret.has_more is without.has_more is True


async def test_scope_limits_the_search_to_one_subtree(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("a/ a/target.txt b/ b/target.txt", drive=drive)
    async with repo.transaction():
        everywhere = await search.search_names(repo, _ctx(files_org), DriveId(drive.id), "target")
        scoped = await search.search_names(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            "target",
            scope_node_id=NodeId(nodes["a"].id),
        )
    assert len(everywhere.items) == 2
    assert [row.id for row in scoped.items] == [nodes["a/target.txt"].id]
    assert scoped.chain_ids


async def test_a_scope_in_another_org_is_not_found(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_org_factory: object,
) -> None:
    drive = await files_factory.drive()
    async with repo.transaction():
        with pytest.raises(NotFound):
            await search.search_names(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                "anything",
                scope_node_id=NodeId(uuid.uuid4()),
            )


@pytest.mark.parametrize(
    ("chip", "hits"),
    [
        pytest.param(ListFilters(kind="folder"), 1, id="kind-folder"),
        pytest.param(ListFilters(kind="file"), 1, id="kind-file"),
        pytest.param(ListFilters(size_min=1), 0, id="size-min-past-every-row"),
        pytest.param(ListFilters(), 2, id="no-chip"),
    ],
)
async def test_each_chip_narrows_the_search(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    chip: ListFilters,
    hits: int,
) -> None:
    drive = await files_factory.drive()
    await files_factory.tree("thing/ thing2.txt", drive=drive)
    async with repo.transaction():
        page = await search.search_names(
            repo, _ctx(files_org), DriveId(drive.id), "thing", filters=chip
        )
    assert len(page.items) == hits


async def test_search_is_at_most_three_statements(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    drive_id, nodes = await _seed(files_factory)
    async with repo.transaction():
        with _Counter(repo) as counted:
            await search.search_names(
                repo,
                _ctx(files_org),
                drive_id,
                "e",
                scope_node_id=NodeId(nodes["papers"].id),
            )
        assert counted.count <= 3


@pytest.mark.parametrize(
    "bad",
    [pytest.param("", id="empty"), pytest.param("   ", id="whitespace")],
)
async def test_an_empty_query_is_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, bad: str
) -> None:
    drive = await files_factory.drive()
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await search.search_names(repo, _ctx(files_org), DriveId(drive.id), bad)


@pytest.mark.parametrize(
    "limit",
    [pytest.param(0, id="zero"), pytest.param(1001, id="one-past-the-cap")],
)
async def test_a_limit_outside_the_published_range_is_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, limit: int
) -> None:
    drive = await files_factory.drive()
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await search.search_names(repo, _ctx(files_org), DriveId(drive.id), "x", limit=limit)


async def test_a_wildcard_in_the_query_is_a_literal(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """`%` must not match everything: it is a character the user typed."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree("plain/ 100%done/", drive=drive)
    async with repo.transaction():
        page = await search.search_names(repo, _ctx(files_org), DriveId(drive.id), "%")
    assert [row.id for row in page.items] == [nodes["100%done"].id]


async def test_the_marker_pages_through_every_match_exactly_once(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    await files_factory.tree("m1/ m2/ m3/ m4/ m5/", drive=drive)
    seen: list[uuid.UUID] = []
    async with repo.transaction():
        marker = None
        while True:
            page = await search.search_names(
                repo, _ctx(files_org), DriveId(drive.id), "m", limit=2, marker=marker
            )
            seen.extend(row.id for row in page.items)
            marker = page.next_marker
            if marker is None:
                break
    assert len(seen) == len(set(seen)) == 5


async def test_a_marker_from_another_query_is_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("m1/ m2/ m3/", drive=drive)
    async with repo.transaction():
        page = await search.search_names(repo, _ctx(files_org), DriveId(drive.id), "m", limit=1)
        assert page.next_marker is not None
        # Same drive, but scoped to a subtree: a different query, so the cursor
        # cut from the unscoped one would silently skip rows and is refused.
        with pytest.raises(InvalidRequest):
            await search.search_names(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                "m",
                scope_node_id=NodeId(nodes["m1"].id),
                marker=page.next_marker,
            )
