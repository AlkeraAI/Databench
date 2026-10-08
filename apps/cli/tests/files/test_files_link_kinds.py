"""A symlink's kind survives the wire, so a pull rewrites the right ones.

The corpus module proves this too, but only as part of a ~20-minute round trip.
The three link classes are decided by two lines of vocabulary — what the push
sends as ``symlinkKind`` and what the pull reads back off the facet — so they
deserve a case that runs in seconds and names the classification directly.

The claim: a canonical link is re-anchored at the machine that pulls it, a
relative link is byte-identical everywhere, and a host link (absolute, outside
the root) never travels: the drive refuses a door out of the tree. Getting the vocabulary
wrong is silent — every link simply degrades to ``relative`` and is written
verbatim — which is why the assertion is on the text on disk, not on a call.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_cli.files.pull import pull
from alkera_cli.files.push import push
from alkera_core.config import settings
from alkera_core.files.links import strip_extended_prefix
from alkera_core.files.names import display, parse_display
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, home_path, live_backend


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    previous = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    try:
        with live_backend(tmp_path / "server") as running:
            yield running
    finally:
        settings.rate_limit_enabled = previous


@pytest.fixture
def client(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    ) as api:
        yield api


def _host_target(tmp_path: Path) -> str:
    """An absolute path outside the push root, in this platform's own spelling.

    ``/usr/bin/env`` is absolute only on POSIX. Windows reads a path rooted
    without a drive as relative to whichever drive is current, so there it is a
    ``relative`` link and proves nothing about the kind that must survive the
    trip unrewritten. A sibling of the push root is absolute on both platforms
    and under neither root, which is what ``host`` means.
    """
    return os.fsdecode(tmp_path / "outside" / "env")


def _tree(root: Path, host: str) -> Path:
    """One file and the three link classes that point at or past it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "notes.txt").write_bytes(b"plain bytes\n")
    links = root / "links"
    links.mkdir(exist_ok=True)
    # Anchored inside the push root: the one kind a materializer must rewrite.
    os.symlink(os.fsdecode(os.fsencode(root) + b"/data/notes.txt"), links / "canonical")
    # Not anchored at all, and anchored outside the root: both travel verbatim.
    os.symlink("../data/notes.txt", links / "relative")
    os.symlink(host, links / "host")
    return root


def _as_the_machine_spells_it(text: str) -> str:
    """A path in the one spelling both sides of an assertion can be read in.

    Windows reports a symlink's substitute name, which carries an
    extended-length prefix the path it was written from does not, and its
    filesystems do not distinguish case. Elsewhere the text is returned
    untouched, so a POSIX name keeps every byte.
    """
    if os.name != "nt":
        return text
    stripped = strip_extended_prefix(os.fsencode(text), windows=True)
    return os.path.normcase(os.fsdecode(stripped))


def _as_it_was_written(stored: str) -> str:
    """The raw name a stored target spells, from the escaped text on the item.

    A symlink target is a name, so the drive holds it in the display spelling
    ``alkera_core.files.names.display`` produces — and that spelling doubles
    every backslash. A Windows host path therefore comes back as
    ``c:\\\\users\\\\...`` and compares equal to nothing the machine ever wrote.
    A POSIX name has nothing to escape, so this returns it byte for byte and
    the assertion stays exact there.
    """
    return os.fsdecode(parse_display(stored))


def _settle(client: AlkeraClient, dest: str, *, seconds: float = 120.0) -> None:
    """Wait until the pushed subtree stops growing: a commit is an operation."""
    drive_id = str(client.files.drive()["id"])
    root_id = str(client.files.item_by_path(drive_id, dest)["id"])
    deadline = time.monotonic() + seconds
    previous = -1
    stable = 0
    while time.monotonic() < deadline:
        count = _count(client, drive_id, root_id)
        stable = stable + 1 if count == previous else 0
        if stable >= 3:
            return
        previous = count
        time.sleep(1.0)


def _count(client: AlkeraClient, drive_id: str, item_id: str) -> int:
    total = 0
    for child in client.files.children(drive_id, item_id):
        total += 1
        if child.get("kind") == "folder":
            total += _count(client, drive_id, str(child["id"]))
    return total


def _round_trip(client: AlkeraClient, tmp_path: Path) -> tuple[Path, Path, list[str]]:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "env").write_bytes(b"outside the tree\n")
    source = _tree(tmp_path / "root-a", _host_target(tmp_path))
    dest = home_path(client, "links")
    summary = push(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=source,
        dest=dest,
        home=tmp_path / "home",
        respect_gitignore=False,
    )
    _settle(client, dest)
    other = tmp_path / "root-b"
    pull(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=other,
        source=dest,
    )
    return source, other, list(summary.warnings)


def test_a_canonical_link_is_re_anchored_at_the_root_that_pulled_it(
    client: AlkeraClient, tmp_path: Path
) -> None:
    """The one kind that is rewritten: absolute under A, absolute under B.

    Rewritten lexically, not resolved — and nothing about A may survive into
    the text, which is what fails if the kind degrades to ``relative`` and the
    stored org path (``/data/notes.txt``) is written out verbatim.
    """
    source, other, _warnings = _round_trip(client, tmp_path)

    target = _as_the_machine_spells_it(os.readlink(other / "links" / "canonical"))
    assert target == _as_the_machine_spells_it(os.fsdecode(other / "data" / "notes.txt"))
    assert _as_the_machine_spells_it(os.fsdecode(source)) not in target
    assert (other / "links" / "canonical").resolve().read_bytes() == b"plain bytes\n"


def test_a_relative_link_is_the_same_text_on_both_machines_and_a_host_link_never_leaves(
    client: AlkeraClient, tmp_path: Path
) -> None:
    """The negative twins of the canonical case.

    A relative target is resolved by the kernel against the directory it sits
    in on whichever machine holds it, so rewriting it would break it. A host
    target is a path on the machine that pushed it, outside the tree: the push
    reports it and leaves it behind, and the machine that pulls never has it.
    """
    _source, other, warnings = _round_trip(client, tmp_path)

    assert os.readlink(other / "links" / "relative") == "../data/notes.txt"
    assert not os.path.lexists(other / "links" / "host")
    refused = "was not pushed: it points outside the folder being pushed"
    assert [w.endswith(f"links/host {refused}") for w in warnings if refused in w] == [True]


def test_the_stored_target_is_the_org_path_and_the_kind_says_so(
    client: AlkeraClient, tmp_path: Path
) -> None:
    """What the drive actually holds, read back off the item.

    The pull can only re-anchor a link the *server* says is canonical, so the
    push storing the pusher's absolute path — or storing the right text under
    the wrong kind — is the failure this names directly, one level below the
    two cases above.
    """
    _source, _other, _warnings = _round_trip(client, tmp_path)

    drive_id = str(client.files.drive()["id"])
    dest = home_path(client, "links")
    stored = {
        name: client.files.item_by_path(drive_id, f"{dest}/links/{name}")
        for name in ("canonical", "relative")
    }
    folder = client.files.item_by_path(drive_id, f"{dest}/links")
    names = sorted(child["name"] for child in client.files.children(drive_id, str(folder["id"])))
    assert names == ["canonical", "relative"]

    assert stored["canonical"]["symlink"]["target"] == "/data/notes.txt"
    assert stored["canonical"]["symlink"]["kind"] == "canonical"
    assert stored["relative"]["symlink"] == {
        **stored["relative"]["symlink"],
        "target": "../data/notes.txt",
        "kind": "relative",
    }


def test_a_stored_windows_host_target_reads_back_as_the_path_that_was_written(
    tmp_path: Path,
) -> None:
    """The escape the round trip above rides over, driven from a POSIX host.

    The drive stores a symlink target the way it stores every other name, and
    that spelling escapes a backslash as two. Comparing the stored text to the
    path the machine wrote therefore fails on Windows and only on Windows — a
    difference of escaping, not of what the link points at. The POSIX case is
    the control: a name with nothing to escape must survive byte for byte, so
    this cannot be satisfied by normalising both sides into mush.
    """
    written = "c:\\users\\runneradmin\\tmp\\outside\\env"
    stored = display(os.fsencode(written))

    assert stored == "c:\\\\users\\\\runneradmin\\\\tmp\\\\outside\\\\env"
    assert stored != written, "the escape this exists for is not happening"
    assert _as_it_was_written(stored) == written

    posix = os.fsdecode(tmp_path / "outside" / "env")
    assert _as_it_was_written(display(os.fsencode(posix))) == posix
