"""The filesystem driver's own decisions, above what the conformance suite pins.

Everything here is an observable outcome on disk or through the driver's own
listing: which key space a handle owns, where an open multipart session stages
its parts, and what a reconciler walking the store is allowed to see.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import alkera_core.files.store.filesystem as driver
import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ownership import read_owner, write_marker
from alkera_core.files.store._beneath import (
    _directory_open_flags,
    _OpenHow,
    _walk_lstat,
    _walk_openat2,
)
from alkera_core.files.store.errors import InvalidKey, InvalidRequest
from alkera_core.files.store.filesystem import PARTS_ROOT, FilesystemStore
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.files.sync.atomic import default_fsync_dir

DOMAIN = UUID("11111111-2222-3333-4444-555555555555")
OTHER = UUID("22222222-3333-4444-5555-666666666666")
KEY = "objects/ab/cd/abcdef"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 3, 1, tzinfo=UTC))


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def put(store: ObjectStore, key: str, data: bytes) -> None:
    await store.put(key, stream(data), size=len(data), checksum=hash_bytes(data).content_hash)


# -- the two key namespaces ------------------------------------------------


async def test_the_bucket_layout_reads_an_object_a_domain_handle_wrote(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The janitor's handle is the whole point of the bucket layout.

    It is rooted above every domain directory, so the keys it takes are the
    absolute ones ``keys.absolute`` spells — and a domain handle's relative
    namespace refuses those, which is why the layout is a constructor
    argument rather than a guess.
    """
    domain_store = FilesystemStore(
        tmp_path / "root" / "domains" / str(DOMAIN), clock=clock.now, layout="domain"
    )
    admin = FilesystemStore(tmp_path / "root", clock=clock.now, layout="bucket")
    await put(domain_store, KEY, b"the bytes the tenant wrote")
    absolute = f"domains/{DOMAIN}/{KEY}"

    info = await admin.head(absolute)
    page = await admin.list_prefix(f"domains/{DOMAIN}/")
    await admin.move(absolute, f"domains/{DOMAIN}/deleted/{KEY}")

    assert info is not None and info.size == len(b"the bytes the tenant wrote")
    assert list(page.keys) == [absolute]
    assert await admin.head(absolute) is None
    assert await domain_store.head(KEY) is None
    assert await domain_store.head(f"deleted/{KEY}") is not None


async def test_a_domain_handle_refuses_the_absolute_key_the_admin_handle_takes(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The negative twin: the namespaces do not overlap in either direction."""
    domain_store = FilesystemStore(tmp_path / "d", clock=clock.now, layout="domain")
    admin = FilesystemStore(tmp_path / "b", clock=clock.now, layout="bucket")

    with pytest.raises(InvalidKey):
        await domain_store.head(f"domains/{DOMAIN}/{KEY}")
    with pytest.raises(InvalidKey):
        await admin.head(KEY)


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("domains/not-a-uuid/objects/x", id="domain-is-not-a-uuid"),
        pytest.param(f"domains/{DOMAIN}", id="domain-with-no-object"),
        pytest.param(f"domains/{DOMAIN}/../{OTHER}/x", id="escaping-the-domain"),
        pytest.param("objects/x", id="relative"),
    ],
)
async def test_the_bucket_layout_refuses_a_key_that_is_not_one_domains_object(
    tmp_path: Path, clock: FakeClock, malformed: str
) -> None:
    admin = FilesystemStore(tmp_path / "b", clock=clock.now, layout="bucket")
    with pytest.raises(InvalidKey):
        await admin.head(malformed)


# -- multipart staging is per session, not per key -------------------------


async def test_two_sessions_on_one_key_stage_their_parts_apart(
    tmp_path: Path, clock: FakeClock
) -> None:
    """Two uploads racing for the same content key must not read each other's parts.

    Staging under the object key made the second session's parts land on the
    first's, so whichever completed first assembled a mixture of both.
    """
    store = FilesystemStore(tmp_path / "s", clock=clock.now)
    first = await store.multipart_create(KEY, size=8)
    second = await store.multipart_create(KEY, size=8)
    mine, theirs = b"aaaaaaaa", b"bbbbbbbb"

    await store.multipart_put_part(
        first, 1, stream(mine), size=8, checksum=hash_bytes(mine).content_hash
    )
    part = await store.multipart_put_part(
        second, 1, stream(theirs), size=8, checksum=hash_bytes(theirs).content_hash
    )
    result = await store.multipart_complete(
        second, [part], checksum=hash_bytes(theirs).content_hash
    )

    assert result.checksum == hash_bytes(theirs).content_hash
    assert (tmp_path / "s" / KEY).read_bytes() == theirs
    # The first session is untouched: its parts are still staged, and its own
    # complete is the only thing that can publish them.
    assert (tmp_path / "s" / PARTS_ROOT / first.upload_id / "1").read_bytes() == mine


async def test_aborting_one_session_leaves_the_other_running(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = FilesystemStore(tmp_path / "s", clock=clock.now)
    first = await store.multipart_create(KEY, size=8)
    second = await store.multipart_create(KEY, size=8)
    mine = b"aaaaaaaa"
    part = await store.multipart_put_part(
        first, 1, stream(mine), size=8, checksum=hash_bytes(mine).content_hash
    )

    await store.multipart_abort(second)

    result = await store.multipart_complete(first, [part], checksum=hash_bytes(mine).content_hash)
    assert result.checksum == hash_bytes(mine).content_hash


async def test_a_caller_cannot_name_the_staging_directory_as_a_key(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The reserved namespace is what makes staging unreachable, so pin it."""
    store = FilesystemStore(tmp_path / "s", clock=clock.now)
    handle = await store.multipart_create(KEY, size=8)

    with pytest.raises(InvalidKey):
        await store.head(f"{PARTS_ROOT}/{handle.upload_id}/1")
    with pytest.raises(InvalidKey):
        await put(store, f"{PARTS_ROOT}/{handle.upload_id}/1", b"forged")


# -- what a reconciler is allowed to see -----------------------------------


async def test_list_prefix_reports_neither_staged_parts_nor_temp_files(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A reconciler compares this listing against its rows and deletes the rest.

    A staged part or a half-published temp file that read as an object would
    be an orphan it has never heard of — and the sweep's job is to delete
    exactly those.
    """
    root = tmp_path / "s"
    store = FilesystemStore(root, clock=clock.now)
    await put(store, KEY, b"a real object")
    handle = await store.multipart_create("objects/ab/cd/staged", size=8)
    part = b"aaaaaaaa"
    await store.multipart_put_part(
        handle, 1, stream(part), size=8, checksum=hash_bytes(part).content_hash
    )
    # What a SIGKILL between the temp write and the publish leaves behind.
    (root / "objects" / "ab" / "cd").mkdir(parents=True, exist_ok=True)
    (root / "objects" / "ab" / "cd" / ".abcdef.9f3c.tmp").write_bytes(b"half a write")

    page = await store.list_prefix("")

    assert list(page.keys) == [KEY]


async def test_the_staged_part_is_really_on_disk_where_the_listing_hides_it(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The negative twin of the filter: it hides a file that is genuinely there."""
    root = tmp_path / "s"
    store = FilesystemStore(root, clock=clock.now)
    handle = await store.multipart_create(KEY, size=8)
    part = b"aaaaaaaa"
    await store.multipart_put_part(
        handle, 1, stream(part), size=8, checksum=hash_bytes(part).content_hash
    )

    assert (root / PARTS_ROOT / handle.upload_id / "1").read_bytes() == part
    assert list((await store.list_prefix("")).keys) == []


async def test_a_part_that_disagrees_with_its_declared_size_stages_nothing(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = FilesystemStore(tmp_path / "s", clock=clock.now)
    handle = await store.multipart_create(KEY, size=8)
    part = b"aaaaaaaa"

    with pytest.raises(InvalidRequest):
        await store.multipart_put_part(
            handle, 1, stream(part), size=7, checksum=hash_bytes(part).content_hash
        )

    assert not (tmp_path / "s" / PARTS_ROOT / handle.upload_id / "1").exists()


# -- publishing where the platform has no directory fsync --------------------


def _as_windows(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Both halves of what Windows does to a directory handle.

    Faking ``sys.platform`` alone is inert on a POSIX host — the guard would be
    taken but the call it guards would have succeeded anyway — so the real
    ``default_fsync_dir`` is driven with ``os.open`` refusing a directory the
    way Windows does, with ``EACCES``. The refusal is armed around THAT call
    only: a process-wide one also breaks the containment walk, which opens the
    store root for a reason that has nothing to do with durability, and the
    move would then fail for a reason the platform never had.
    """
    real_open = os.open

    def refuse_a_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str | bytes | os.PathLike) and os.path.isdir(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    flushed: list[Path] = []

    def fsync_dir_without_a_directory_handle(directory: Path) -> None:
        monkeypatch.setattr(os, "open", refuse_a_directory)
        try:
            default_fsync_dir(directory)
        finally:
            monkeypatch.setattr(os, "open", real_open)
        flushed.append(directory)

    monkeypatch.setattr(driver, "default_fsync_dir", fsync_dir_without_a_directory_handle)
    monkeypatch.setattr(sys, "platform", "win32")
    # The caller asserts this is not empty: a publish that stopped renaming
    # would take the durability step with it, and the test would pass proving
    # nothing.
    return flushed


async def test_a_move_publishes_where_the_platform_cannot_fsync_a_directory(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged object still lands where no directory handle can be opened.

    ``move`` is how every staged upload becomes an object: the bytes are
    written under an ``incoming/`` key and renamed onto their content key. A
    platform whose directories cannot be opened for reading must not lose the
    rename over the durability flush that follows it.
    """
    store = FilesystemStore(tmp_path / "root", clock=clock.now, layout="domain")
    payload = b"the staged bytes a session assembled"
    await put(store, "incoming/abc/single", payload)

    flushed = _as_windows(monkeypatch)
    await store.move("incoming/abc/single", KEY)
    assert flushed, "the move never reached the directory flush the platform cannot take"

    published = await store.head(KEY)
    assert published is not None
    assert published.size == len(payload)
    assert await store.head("incoming/abc/single") is None


# -- the first object a domain ever gets --------------------------------------


async def test_the_ownership_marker_round_trips_for_a_domain_with_no_prefix_yet(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A brand-new domain has no directory, and the marker is what makes one.

    The bucket-rooted handle every deployment writes ownership through resolves
    a key beneath the DOMAIN's own directory — which does not exist until this
    very write creates it. A driver that treats that as an unresolvable key
    cannot mark a new domain at all, and cannot read back the one it did not
    write; a self-hosted install on this driver would then have every domain it
    ever made reported as nobody's.
    """
    admin = FilesystemStore(tmp_path / "bucket", clock=clock.now, layout="bucket")
    key = f"domains/{DOMAIN}/meta/owner.json"

    assert await read_owner(admin, str(DOMAIN)) is None, "nothing is there to begin with"
    await write_marker(admin, "deployment-a", key=key, now=clock.now())

    stamp = await read_owner(admin, str(DOMAIN))
    assert stamp is not None
    assert stamp.deployment_id == "deployment-a"
    # And the marker stands: a second writer takes the key as it finds it.
    assert await write_marker(admin, "deployment-b", key=key, now=clock.now()) is False
    again = await read_owner(admin, str(DOMAIN))
    assert again is not None and again.deployment_id == "deployment-a"


# -- the kernel's own containment walk ---------------------------------------
#
# ``openat2`` is a Linux syscall, so on every other platform the walk's error
# handling is only reachable through its seam. These drive it with a fake
# kernel: what the walk does with an answer is the contract, not which errno
# this host's kernel happens to give.


def _fake_kernel(answer: BaseException | int) -> Any:
    def open_beneath(root_fd: int, path: str, how: _OpenHow) -> int:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return open_beneath


def _a_descriptor_that_is_not_a_directory(_root: Path) -> int:
    """A real, closable descriptor standing in for the root's own.

    The kernel is faked in every case below, so all the walk ever does with the
    root's descriptor is close it. Taking the real one would make these cases
    depend on the host having directory descriptors at all — Windows has none,
    and every refusal below would then be the platform's rather than the
    kernel's answer, which is the thing being pinned.
    """
    return os.open(os.devnull, os.O_RDONLY)


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(PermissionError(13, "Permission denied"), id="eacces"),
        pytest.param(PermissionError(1, "Operation not permitted"), id="eperm"),
        pytest.param(OSError(40, "Too many levels of symbolic links"), id="eloop"),
        pytest.param(OSError(18, "Invalid cross-device link"), id="exdev"),
    ],
)
def test_a_component_the_kernel_refuses_is_an_invalid_key(tmp_path: Path, refusal: OSError) -> None:
    """A refusal is the key's answer, not a raw OSError escaping the store.

    ``RESOLVE_BENEATH`` reports an escape attempt as ``EXDEV`` and a symlink as
    ``ELOOP``, and a directory the process may not open is ``EACCES`` — every
    one of them means the same thing to a caller, and every one of them has to
    arrive as the refusal both enforcement paths spell.
    """
    with pytest.raises(InvalidKey) as refused:
        _walk_openat2(
            tmp_path,
            "objects/ab/cd/ef",
            open_beneath=_fake_kernel(refusal),
            open_root=_a_descriptor_that_is_not_a_directory,
        )

    assert "objects/ab/cd/ef" in str(refused.value)
    assert refused.value.__cause__ is refusal


def test_a_component_that_does_not_exist_yet_ends_the_walk(tmp_path: Path) -> None:
    """Nothing below the deepest existing component can escape, so the walk stops.

    A put writes a key whose directories do not exist yet; a walk that refused
    the first missing one would make every first write to a shard impossible.
    """
    _walk_openat2(
        tmp_path,
        "objects/ab/cd/ef",
        open_beneath=_fake_kernel(FileNotFoundError(2, "No such file")),
        open_root=_a_descriptor_that_is_not_a_directory,
    )


@pytest.mark.parametrize(
    "walk",
    [
        pytest.param(
            lambda root, relative: _walk_openat2(root, relative, open_beneath=_fake_kernel(0)),
            id="the kernel walk",
        ),
        pytest.param(_walk_lstat, id="the portable walk"),
    ],
)
def test_a_root_that_does_not_exist_yet_ends_the_walk(tmp_path: Path, walk: Any) -> None:
    """A prefix nothing has been written under yet is not a refusal.

    The bucket-rooted handle resolves a key beneath the DOMAIN's own directory,
    and that directory does not exist until something is written into it — so
    the first write to a new domain, and every read that looks for one, opens a
    root that is not there. Nothing below a directory that does not exist can
    escape through it, so both walks have to say the same thing.

    They did not. The portable walk stops at the first missing component and
    never opens the root at all; the kernel walk opened it and turned the
    ENOENT into `InvalidKey`, which made the ownership marker — the first
    object any domain ever gets — impossible to write or read on Linux, and on
    Linux alone. Parametrized over both because it is their DISAGREEMENT that
    was the bug: either one alone looks correct.
    """
    missing = tmp_path / "domains" / "b0f3c2ee-0000-4000-8000-000000000001"

    walk(missing, "meta/owner.json")


def test_a_root_that_cannot_be_opened_is_the_same_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root's own open answers in the key's vocabulary too.

    The portable walk turns an unreadable parent into ``InvalidKey``; a caller
    that catches the store's refusal must not have a bare ``PermissionError``
    come past it because this host resolves through the kernel instead.
    """
    real_open = os.open

    def refuse_the_root(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str | bytes | os.PathLike) and os.path.isdir(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_the_root)

    with pytest.raises(InvalidKey):
        _walk_openat2(tmp_path, "objects/ab/cd/ef", open_beneath=_fake_kernel(0))


def test_the_walk_takes_the_root_where_the_platform_has_no_o_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' ``os`` module defines no ``O_DIRECTORY`` at all.

    Naming the flag directly is an ``AttributeError`` there — raised while the
    open's arguments are being built, before any refusal the walk knows how to
    translate — so every case above died on the import of a constant rather
    than on anything about containment. The flag is removed here to drive that
    exact platform from a host that has it.
    """
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)

    assert _directory_open_flags() == os.O_RDONLY

    # The root is faked too: on Windows os.open refuses a directory outright,
    # which is a different refusal from the flag this case is about.
    _walk_openat2(
        tmp_path,
        "objects/ab/cd/ef",
        open_beneath=_fake_kernel(FileNotFoundError(2, "No such file")),
        open_root=_a_descriptor_that_is_not_a_directory,
    )
