"""Names at the edge of what a name may be, pushed and pulled against a real backend.

A name is the shortest run of bytes the round trip carries, and the fidelity
bar is made of bytes. Two claims, in both directions:

* every name the drive accepts arrives byte-identical on the other side, up
  to and including the longest one there is — and the server says what a
  *Windows* client would refuse rather than refusing it itself, because one
  drive is shared by machines that disagree about names;
* every name no machine could hold is refused by the namespace, naming the
  rule that refused it, so no box on any OS is handed a name it cannot write.

The length ceiling is not NAME_MAX. A pull streams each file into a
``<name>.alkera-part`` sidecar beside its target and promotes it once the
hash matches, so the longest leaf a round trip can land is NAME_MAX less that
suffix: the sidecar for a name AT NAME_MAX is itself over it. That reserve is
the server's rule, not a client's manners — a 244-to-255-byte name was legal
on the wire and could never be pulled, so the file existed in the drive and
was unreachable from every machine that asked for it. It is the same reserve
the deep path pays against PATH_MAX, one component further down.

The whole name tree makes one push and one pull against one backend, shared by
every case below (``name_trip``), so the module is kept on one xdist worker.
The POSIX corpus beside it (``test_files_push_pull_corpus.py``) boots a backend
per test and spreads.
"""

from __future__ import annotations

import errno
import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_core.config import settings
from alkera_core.files.names import NAME_MAX_BYTES, PULL_PART_SUFFIX
from alkera_sdk import AlkeraClient
from files._live_backend import home_path, live_backend
from files._round_trip import pull_back, push_settled
from files.corpus.build import PULL_SIDECAR

pytestmark = [
    # One backend and one round trip for the whole module (`name_trip`): a case
    # handed to another worker would build the whole trip again there.
    pytest.mark.xdist_group("files_push_pull_names"),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the corpus needs byte-exact names, symlink loops, FIFO refusals and "
            "2,000-byte paths the Windows runner does not offer"
        ),
    ),
]


def _name_max(where: Path) -> int:
    """``NAME_MAX`` in bytes as the filesystem holding ``where`` reports it."""
    try:
        reported = os.pathconf(where, "PC_NAME_MAX")
    except (OSError, ValueError, AttributeError):
        return 255
    return reported if reported > 0 else 255


def _fill(prefix: str, unit: str, width: int) -> str:
    """``prefix`` padded with whole ``unit`` characters to exactly ``width`` bytes.

    Whole characters: a name cut mid-sequence is a different claim (an invalid
    encoding) than a name at the length cap, and the padding is ASCII ``z``
    rather than a dot so the case does not accidentally become the
    trailing-dot case below.
    """
    room = width - len(prefix.encode())
    unit_bytes = len(unit.encode())
    name = prefix + unit * (room // unit_bytes) + "z" * (room % unit_bytes)
    assert len(name.encode()) == width, (prefix, unit, width)
    return name


#: ``(case id, fixed name or None, the character a grown name is built from,
#: whether Windows could hold it)``. A name with no fixed spelling is grown to
#: the longest leaf the sidecar reserve leaves.
_LEGAL_NAMES: tuple[tuple[str, str | None, str, bool], ...] = (
    ("ascii-at-the-cap", None, "a", True),
    ("three-byte-at-the-cap", None, "日", True),
    ("two-byte-at-the-cap", None, "é", True),
    ("leading-dot", ".hidden", "", True),
    ("the-sidecar-suffix-itself", ".alkera-part", "", True),
    ("a-name-ending-in-the-sidecar-suffix", "payload.alkera-part", "", True),
    ("windows-forbidden-characters", 'a<b>c:d"e|f?g*h.txt', "", False),
    ("a-trailing-dot", "trailing.", "", False),
    ("a-windows-device-stem", "CON", "", False),
    ("a-device-stem-with-an-extension", "NUL.txt", "", False),
    ("an-interior-space", "quarter three plan.txt", "", True),
)


def _legal_names(root: Path) -> dict[str, tuple[str, bool]]:
    """``{case id: (name, windows_safe)}`` for the filesystem holding ``root``."""
    longest = _name_max(root) - len(PULL_SIDECAR)
    return {
        case_id: (
            fixed if fixed is not None else _fill(f"{case_id}-", unit, longest),
            windows_safe,
        )
        for case_id, fixed, unit, windows_safe in _LEGAL_NAMES
    }


@dataclass
class NameTrip:
    """One push and one pull of the name corpus, and what the drive holds."""

    source: Path
    pulled: Path
    items: dict[str, dict[str, Any]]
    api: AlkeraClient
    base_url: str
    parent_id: str


@pytest.fixture(scope="module")
def name_trip(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NameTrip]:
    """Push the name tree once, pull it back, and hold both sides open.

    One backend and one round trip for every case below. Each name is still a
    separately-reported test, but a corpus that paid for a server per name
    would cost more than the whole POSIX corpus beside it.
    """
    root = tmp_path_factory.mktemp("names")
    previous = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    try:
        with live_backend(root / "server") as running:
            with AlkeraClient(
                base_url=running.base_url, token=running.token, timeout=FILES_TRANSFER_TIMEOUT
            ) as api:
                source = root / "source"
                (source / "names").mkdir(parents=True)
                for case_id, (name, _) in _legal_names(source).items():
                    (source / "names" / name).write_bytes(f"{case_id}\n".encode())

                push_settled(api, source, "names", root / "home")
                pulled = root / "pulled"
                pull_back(api, pulled, "names")

                drive_id = str(api.files.drive()["id"])
                folder = api.files.item_by_path(drive_id, f"{home_path(api, 'names')}/names")
                yield NameTrip(
                    source=source,
                    pulled=pulled,
                    items={
                        str(child["name"]): child
                        for child in api.files.children(drive_id, str(folder["id"]))
                    },
                    api=api,
                    base_url=running.base_url,
                    parent_id=str(folder["id"]),
                )
    finally:
        settings.rate_limit_enabled = previous


_LEGAL_CASES = [pytest.param(case[0], id=case[0]) for case in _LEGAL_NAMES]


@pytest.mark.parametrize("case_id", _LEGAL_CASES)
def test_a_legal_name_arrives_byte_identical(name_trip: NameTrip, case_id: str) -> None:
    """The name, and the bytes under it, are the same on both machines.

    Read off the directory rather than by spelling the path again: a name the
    pull wrote differently — a normalized ``é``, a stripped trailing space, a
    cap-length name truncated to fit — is simply absent from the listing,
    which is the failure this case exists to catch.
    """
    name, _ = _legal_names(name_trip.source)[case_id]

    listing = sorted(os.listdir(name_trip.pulled / "names"))
    assert name in listing, f"{name!r} is not among {listing!r}"
    assert (name_trip.pulled / "names" / name).read_bytes() == (
        name_trip.source / "names" / name
    ).read_bytes()


@pytest.mark.parametrize("case_id", _LEGAL_CASES)
def test_a_legal_name_is_stored_and_flagged_rather_than_refused(
    name_trip: NameTrip, case_id: str
) -> None:
    """A name only Windows objects to is stored and flagged, never refused.

    ``CON``, a trailing dot and ``<>:"|?*`` are ordinary names on the machine
    that pushed them, so refusing them would cost a file its owner can see. The
    drive stores them and marks them ``windowsSafe: false`` instead, which is
    what lets a Windows client decide locally. That is a different question
    from the one the refusal table below asks: those names no machine at all
    could hold. Asserted in both directions — a flag that is false for every
    name would say nothing.
    """
    name, windows_safe = _legal_names(name_trip.source)[case_id]

    assert name in name_trip.items, f"{name!r} never reached the drive"
    assert bool(name_trip.items[name]["nameFlags"]["windows_safe"]) is windows_safe


def test_the_longest_leaf_reserves_the_sidecar_the_pull_writes(tmp_path: Path) -> None:
    """The cap the grown names stop at is the one the filesystem can hold.

    Made by writing the bytes rather than by arithmetic: the sidecar for a leaf
    at the reserve is created, and the sidecar for a leaf one byte longer is
    refused by the kernel. Without the reserve those names would grow to
    NAME_MAX, the pull's ``.alkera-part`` for each would be over it, and every
    case above would die with ENAMETOOLONG on a name the server had accepted.
    """
    where = tmp_path / "leaves"
    where.mkdir()
    longest = _name_max(where) - len(PULL_SIDECAR)

    fits = Path(os.fsdecode(os.fsencode(where) + b"/" + b"a" * longest + PULL_SIDECAR))
    fits.write_bytes(b"sidecar\n")
    assert fits.exists()

    over = Path(os.fsdecode(os.fsencode(where) + b"/" + b"a" * (longest + 1) + PULL_SIDECAR))
    with pytest.raises(OSError) as refused:
        over.write_bytes(b"sidecar\n")
    assert refused.value.errno == errno.ENAMETOOLONG


def test_the_reserve_is_the_servers_own_ceiling_and_not_a_client_convention() -> None:
    """The kernel's limit above and the drive's rule are the same number.

    This is the whole decision, in one line: a 244-byte name used to be legal
    on the wire and impossible to pull, so the ceiling is NAME_MAX less the
    sidecar and the push never has to know it. If these ever part, the corpus
    above would keep passing while a real drive handed a real machine a name it
    could not write.
    """
    assert NAME_MAX_BYTES == _name_max(Path(__file__).parent) - len(PULL_SIDECAR)
    assert PULL_SIDECAR == PULL_PART_SUFFIX


#: Every name Linux itself refuses, with the rule that refuses it. The codes
#: are the wire's: a client reads ``files.invalid_name.<rule>`` off the
#: envelope and has to be able to tell the cases apart.
_ILLEGAL_NAMES: tuple[tuple[str, str, str], ...] = (
    ("empty", "", "files.invalid_name.empty"),
    ("a-nul-byte", "before\x00after", "files.invalid_name.nul"),
    ("a-separator", "a/b", "files.invalid_name.separator"),
    ("dot", ".", "files.invalid_name.dot"),
    ("dot-dot", "..", "files.invalid_name.dot"),
    ("ascii-one-byte-over", "a" * (NAME_MAX_BYTES + 1), "files.invalid_name.too_long"),
    (
        "three-byte-one-byte-over",
        "日" * (NAME_MAX_BYTES // 3) + "a",
        "files.invalid_name.too_long",
    ),
    # 122 characters, and 244 bytes: the ceiling counts bytes, so a name a
    # person would call short is over it while a 243-character ASCII name is
    # not. A client measuring characters would send this one and be refused.
    (
        "few-characters-but-one-byte-over",
        "é" * (NAME_MAX_BYTES // 2) + "aa",
        "files.invalid_name.too_long",
    ),
    # Legal on the filesystem, and the sidecar for it is not: this is the whole
    # reason the ceiling is not NAME_MAX.
    ("at-the-filesystems-own-limit", "a" * 255, "files.invalid_name.too_long"),
    ("a-tab", "quarter\tthree", "files.invalid_name.control"),
    ("a-newline", "quarter\nthree", "files.invalid_name.control"),
    ("a-trailing-space", "trailing ", "files.invalid_name.surrounding_space"),
    ("a-leading-space", " leading", "files.invalid_name.surrounding_space"),
)


@pytest.mark.parametrize(
    ("name", "code"),
    [pytest.param(name, code, id=case_id) for case_id, name, code in _ILLEGAL_NAMES],
)
def test_a_name_no_filesystem_could_write_is_refused_by_the_namespace(
    name_trip: NameTrip, name: str, code: str
) -> None:
    """The refusal happens at the drive, so no box is ever handed the name.

    A 422 alone would not prove that: the other half is that the node is not
    there afterwards. A name that landed and was merely *reported* invalid
    would reach the next machine that pulled the folder and fail there — on a
    box, mid-sync, with nothing to point at.
    """
    api = name_trip.api
    drive_id = str(api.files.drive()["id"])
    before = {str(child["name"]) for child in api.files.children(drive_id, name_trip.parent_id)}

    answer = api.raw_client.get_httpx_client().post(
        f"{name_trip.base_url}/api/v1/files/drives/{drive_id}/items/{name_trip.parent_id}/children",
        json={"kind": "folder", "name": name, "conflictBehavior": "fail"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code, answer.text
    after = {str(child["name"]) for child in api.files.children(drive_id, name_trip.parent_id)}
    assert after == before
