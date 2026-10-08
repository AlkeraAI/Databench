"""A version is all-or-nothing, whichever moment the machine dies in.

Four points in a put separate two effects that a reader could otherwise catch
half-done: the bytes are in the store but nothing points at them; the store has
confirmed them but the row is not written; the row is about to be written; the
row is committed but the staging object is still there. Each one is reached in a
*real child process* that is SIGKILLed there — not an exception, which would let
`finally:` blocks run the very cleanup a power loss does not get to run — and
then the survivors are read back out of Postgres and off the disk.

What must hold after every one of them:

* a reader sees either no version or one that the node's head points at, never a
  version row nobody can reach and never a head pointing at nothing;
* the object under the content key is absent or complete, never a prefix;
* whatever is left under ``incoming/`` is an orphan the sweeper can find, which
  is what ``fsck_orphans`` stands in for until the real ``fsck`` lands.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store import keys
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

SIGKILL_RETURNCODE = -9

# Every kill point below is reached by the child SIGKILLing itself, and the
# parent asserts returncode -9. Windows has no SIGKILL, and no way to stop a
# process mid-`put_version` without letting its `finally:` blocks run — which is
# the very cleanup a power loss does not get to do.
requires_sigkill = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the kill points SIGKILL the child mid-put; Windows has no SIGKILL",
)

KILL_POINTS = [
    pytest.param("content.after_store_put", id="after-store-put"),
    pytest.param("content.after_head", id="after-head"),
    pytest.param("content.before_commit", id="before-commit"),
    pytest.param("content.after_commit_before_cleanup", id="after-commit-before-cleanup"),
]

PAYLOAD_BYTES = 3 * 1024 * 1024


def payload_of(size: int) -> bytes:
    return (bytes(range(256)) * (size // 256 + 1))[:size]


PAYLOAD = payload_of(PAYLOAD_BYTES)
CONTENT_KEY = keys.object_key(hash_bytes(PAYLOAD).content_hash)

CHILD = '''
import asyncio, json, os, signal, sys, uuid
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.ids import DomainId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

spec = json.loads(sys.argv[1])
kill_at = spec["kill_at"]


class KillingCheckpoints:
    """Die where a power loss would: SIGKILL, so no `finally:` gets to run."""

    async def reach(self, name):
        self.reach_sync(name)

    def reach_sync(self, name):
        if name == kill_at:
            os.kill(os.getpid(), signal.SIGKILL)

    def as_hook(self):
        return self.reach_sync


async def body(payload):
    yield payload


async def main():
    # This runs in a child process that kills itself with SIGKILL, so its engine
    # is the only thing it can connect through — the suite's pooled engine lives
    # in the parent and no connection of it crosses the fork.
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    domain_id = DomainId(uuid.UUID(spec["domain_id"]))
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    root = Path(spec["root"]) / "domains" / spec["domain_id"]
    store = _RootedDomainStore(FilesystemStore(root, clock=clock.now), domain_id)
    repo = FilesRepo(session, OrgScope(org_team_id=uuid.UUID(spec["org_team_id"])))
    ctx = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=spec["actor_id"], org_id=uuid.UUID(spec["org_team_id"])
        )
    )
    service = ContentService(repo, ctx, clock, store, checkpoints=KillingCheckpoints())
    payload = (bytes(range(256)) * (spec["size"] // 256 + 1))[: spec["size"]]
    await service.put_version(
        NodeId(uuid.UUID(spec["node_id"])),
        body(payload),
        size_declared=len(payload),
        if_match=spec["etag"],
    )
    await session.close()
    await engine.dispose()


asyncio.run(main())
'''


def store_key_of(path: PurePath, domain_root: PurePath) -> str:
    """The store key a file on disk holds.

    A store key is always ``/``-separated — it is what a version row carries and
    what the sweeper matches prefixes against — while the path the filesystem
    hands back is separated the way the platform separates paths. Windows would
    otherwise yield ``objects\\ab\\cd\\...``, which matches no referenced key
    and starts with no known prefix, so every published object reads as an
    orphan under an unknown prefix.
    """
    return path.relative_to(domain_root).as_posix()


def fsck_orphans(root: Path, domain_id: uuid.UUID, referenced: set[str]) -> list[str]:
    """Every staged object on disk that no version row points at.

    Stands in for ``alkera files fsck`` until it lands: the point of the crash
    test is that a killed put leaves something *findable*, not something the
    sweeper would have to guess at.
    """
    domain_root = root / "domains" / str(domain_id)
    found: list[str] = []
    for path in sorted(domain_root.rglob("*")):
        if not path.is_file():
            continue
        key = store_key_of(path, domain_root)
        if key not in referenced:
            found.append(key)
    return found


@pytest.mark.parametrize(
    ("flavour", "root", "leaf"),
    [
        pytest.param(
            PureWindowsPath,
            r"C:\store\domains\d",
            r"C:\store\domains\d\objects\ab\cd\beef.bin",
            id="windows",
        ),
        pytest.param(
            PurePosixPath,
            "/store/domains/d",
            "/store/domains/d/objects/ab/cd/beef.bin",
            id="posix",
        ),
    ],
)
def test_a_store_key_is_slash_separated_on_every_platform(
    flavour: type[PurePath], root: str, leaf: str
) -> None:
    """The Windows branch, driven from a POSIX host by a pure Windows path."""
    assert store_key_of(flavour(leaf), flavour(root)) == "objects/ab/cd/beef.bin"


@pytest.fixture
async def crash_target(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, tmp_path: Path
) -> tuple[FileNode, uuid.UUID, Path]:
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin", drive=drive)
    await files_session.commit()
    return tree["papers/a.bin"], drive.dedup_domain_id, tmp_path


def run_child(
    *,
    kill_at: str,
    node: FileNode,
    domain_id: uuid.UUID,
    org: FilesOrg,
    root: Path,
) -> subprocess.CompletedProcess[str]:
    spec = {
        "kill_at": kill_at,
        "node_id": str(node.id),
        "etag": node.etag,
        "domain_id": str(domain_id),
        "org_team_id": str(org.org_team_id),
        "actor_id": str(org.admin_id),
        "root": str(root),
        "size": PAYLOAD_BYTES,
    }
    return subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@requires_sigkill
@pytest.mark.parametrize("kill_at", KILL_POINTS)
async def test_a_put_killed_at_a_checkpoint_leaves_the_old_state_or_the_new(
    kill_at: str,
    crash_target: tuple[FileNode, uuid.UUID, Path],
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    node, domain_id, root = crash_target

    completed = run_child(kill_at=kill_at, node=node, domain_id=domain_id, org=files_org, root=root)

    assert completed.returncode == SIGKILL_RETURNCODE, (
        f"checkpoint {kill_at!r} was never reached: exit {completed.returncode}\n{completed.stderr}"
    )

    # -- what a reader can see -------------------------------------------
    rows = await files_session.execute(
        select(FileVersion).where(FileVersion.node_id == node.id).order_by(FileVersion.seq)
    )
    versions = list(rows.scalars().all())
    await files_session.refresh(node)
    if node.head_version_id is None:
        # The old state: nothing committed, so no version row may exist either —
        # an unreachable version row is exactly the half-done write this forbids.
        assert versions == []
    else:
        assert [version.id for version in versions] == [node.head_version_id]
        head = versions[0]
        assert head.size_bytes == PAYLOAD_BYTES
        assert head.content_hash == hash_bytes(PAYLOAD).content_hash.hex()
        assert head.store_key == CONTENT_KEY

    # -- what is on disk --------------------------------------------------
    content = root / "domains" / str(domain_id) / CONTENT_KEY
    if content.exists():
        assert content.read_bytes() == PAYLOAD, "a published object may never be a prefix"
    else:
        assert node.head_version_id is None, "a committed version must have its bytes"

    # -- what the sweeper is left to find ---------------------------------
    referenced = {version.store_key for version in versions if version.store_key is not None}
    orphans = fsck_orphans(root, domain_id, referenced)
    assert all(key.startswith(("incoming/", "objects/")) for key in orphans), orphans
    for key in orphans:
        blob = root / "domains" / str(domain_id) / key
        if key.startswith("incoming/"):
            # Staged bytes are a prefix of the payload at worst, and always
            # under a session prefix the sweeper can attribute to a session row.
            assert PAYLOAD.startswith(blob.read_bytes())


async def test_a_put_that_is_not_killed_commits_and_leaves_no_orphan(
    crash_target: tuple[FileNode, uuid.UUID, Path],
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    # The negative twin: the same child, no kill point armed. Without it a bug
    # that makes the child die early would make every case above pass vacuously.
    node, domain_id, root = crash_target

    completed = run_child(kill_at="never", node=node, domain_id=domain_id, org=files_org, root=root)

    assert completed.returncode == 0, completed.stderr
    rows = await files_session.execute(select(FileVersion).where(FileVersion.node_id == node.id))
    versions = list(rows.scalars().all())
    assert len(versions) == 1
    await files_session.refresh(node)
    assert node.head_version_id == versions[0].id
    content = root / "domains" / str(domain_id) / CONTENT_KEY
    assert content.read_bytes() == PAYLOAD
    referenced = {version.store_key for version in versions if version.store_key is not None}
    assert fsck_orphans(root, domain_id, referenced) == []
