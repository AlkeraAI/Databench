"""A pull into a chat's folder goes through the chat tree seam: files land with
the sandbox's modes (the stored mode only says whether a file is executable),
a fifo standing at a name never stalls the pull, and no special file is made.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files import chat_fs
from alkera_cli.files.pull import pull

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and fifos")

DRIVE = "11111111-1111-1111-1111-111111111111"
HOME = "cccccccc-0000-0000-0000-000000000001"
ALPHA = "cccccccc-0000-0000-0000-000000000002"
TOOL = "cccccccc-0000-0000-0000-000000000003"
TWIN = "cccccccc-0000-0000-0000-000000000004"
PIPE = "cccccccc-0000-0000-0000-000000000005"
LINK = "cccccccc-0000-0000-0000-000000000006"

BODIES = {ALPHA: b"alpha\n", TOOL: b"#!/bin/sh\n", TWIN: b"alpha\n"}
CAN: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}


def _hash(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


def _file(node_id: str, name: str, mode: int) -> dict[str, Any]:
    payload = BODIES[node_id]
    return {
        "id": node_id,
        "kind": "file",
        "name": name,
        "file": {"size": len(payload), "content_hash": _hash(payload)},
        "attrs": {"mode": mode, "mtimeNs": 1_700_000_000_000_000_000},
        **CAN,
    }


class _Files:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self.rows = rows

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        raise AssertionError("resolved by path")

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": HOME, "kind": "folder", "name": item_path, **CAN}

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[Mapping[str, Any]]:
        if item_id == HOME:
            yield from self.rows


def _serve(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/content"):
        node_id = path.rsplit("/", 2)[-2]
        return httpx.Response(302, headers={"location": f"https://content.test/c/{node_id}"})
    return httpx.Response(200, content=BODIES[path.rsplit("/", 1)[-1]])


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(transport=httpx.MockTransport(_serve), base_url="https://api.test") as c:
        yield c


@pytest.fixture
def root(tmp_path: Path) -> Path:
    where = tmp_path / "chat"
    where.mkdir()
    return where


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_pulled_files_take_the_sandboxs_modes(root: Path, http: httpx.Client) -> None:
    files = _Files(
        [
            _file(ALPHA, "alpha.txt", 0o100444),
            _file(TOOL, "tool.sh", 0o100755),
            _file(TWIN, "twin.txt", 0o100400),
        ]
    )

    summary = pull(files=files, http=http, root=root, source="home", chat_id="chat-a")

    assert summary.files == 2 and summary.deduped == 1
    assert _mode(root / "alpha.txt") == 0o660
    assert _mode(root / "tool.sh") == 0o770
    assert _mode(root / "twin.txt") == 0o660
    assert (root / "twin.txt").read_bytes() == BODIES[ALPHA]
    assert os.stat(root / "alpha.txt").st_mtime_ns == 1_700_000_000_000_000_000
    with (root / "alpha.txt").open("r+b") as handle:
        handle.write(b"A")

    # A local pull of the same rows keeps the stored mode: the seam is what
    # opens the modes, not the pull.
    plain = root.parent / "plain"
    pull(files=files, http=http, root=plain, source="home")
    assert _mode(plain / "alpha.txt") == 0o444


def test_a_fifo_at_a_files_name_never_stalls_the_pull(root: Path, http: httpx.Client) -> None:
    os.mkfifo(root / "alpha.txt")

    pull(
        files=_Files([_file(ALPHA, "alpha.txt", 0o100644)]),
        http=http,
        root=root,
        source="home",
        chat_id="chat-a",
    )

    assert stat.S_ISREG(os.lstat(root / "alpha.txt").st_mode)
    assert (root / "alpha.txt").read_bytes() == BODIES[ALPHA]


def test_no_special_file_is_made_in_a_chat_tree(root: Path, http: httpx.Client) -> None:
    rows = [{"id": PIPE, "kind": "special", "name": "pipe", **CAN}]

    summary = pull(files=_Files(rows), http=http, root=root, source="home", chat_id="chat-a")

    assert not (root / "pipe").exists()
    assert summary.specials == 0
    assert any("pipe" in warning for warning in summary.warnings)


def test_a_stored_link_is_written_as_a_link_and_never_followed(
    root: Path, http: httpx.Client, tmp_path: Path
) -> None:
    rows = [
        {
            "id": LINK,
            "kind": "symlink",
            "name": "latest",
            "symlink": {"target": "alpha.txt", "kind": "relative"},
            **CAN,
        },
        _file(ALPHA, "alpha.txt", 0o100644),
    ]

    pull(files=_Files(rows), http=http, root=root, source="home", chat_id="chat-a")
    # A second pull of the same link leaves it alone rather than failing on a
    # name whose last component is a link.
    pull(files=_Files(rows), http=http, root=root, source="home", chat_id="chat-a")

    assert os.readlink(root / "latest") == "alpha.txt"
    assert (root / "latest").read_bytes() == BODIES[ALPHA]


def test_a_pull_for_a_chat_gives_its_folder_back_first(
    root: Path, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What an older daemon left (read-only, a closed directory) is the
    sandbox's again, before any byte is written, as the chat's identity."""
    gid = next((g for g in os.getgroups() if g != os.getgid()), None)
    if gid is None:
        pytest.skip("this user is in no second group")
    identity = chat_fs.TreeIdentity(uid=os.getuid(), gid=gid)
    monkeypatch.setattr(chat_fs, "_resolver", lambda chat: identity if chat == "chat-a" else None)
    (root / "deep").mkdir(mode=0o700)
    (root / "deep" / "old.md").write_bytes(b"old")
    os.chmod(root / "deep" / "old.md", 0o444)

    pull(files=_Files([]), http=http, root=root, source="home", chat_id="chat-a")

    assert _mode(root / "deep") == chat_fs.DIR_MODE
    assert _mode(root / "deep" / "old.md") == 0o660
    assert os.stat(root / "deep" / "old.md").st_gid == gid


HOST_LINK = "cccccccc-0000-0000-0000-000000000007"
CLIMBING_LINK = "cccccccc-0000-0000-0000-000000000008"
CANONICAL_LINK = "cccccccc-0000-0000-0000-000000000009"


def test_a_link_out_of_the_tree_is_skipped_with_its_reason_and_the_rest_is_pulled(
    root: Path, http: httpx.Client
) -> None:
    """The drive held ``dl-host-auth -> /opt/alkera-home/auth.yml`` beside a
    chat's files, and the box materialized it as root inside the chat's tree.
    A link whose target is absolute or climbs above the root is not made; the
    pull names it and its reason, counts it, and pulls everything else. A
    canonical link, re-anchored at the pulling root as an absolute host path,
    is written in its relative spelling so it resolves inside the sandbox too;
    a second pull finds it unchanged."""
    rows = [
        {
            "id": HOST_LINK,
            "kind": "symlink",
            "name": "dl-host-auth",
            "symlink": {"target": "/opt/alkera-home/auth.yml", "kind": "host"},
            **CAN,
        },
        {
            "id": CLIMBING_LINK,
            "kind": "symlink",
            "name": "up",
            "symlink": {"target": "../../manifest.json", "kind": "relative"},
            **CAN,
        },
        {
            "id": CANONICAL_LINK,
            "kind": "symlink",
            "name": "latest",
            "symlink": {"target": "/alpha.txt", "kind": "canonical"},
            **CAN,
        },
        _file(ALPHA, "alpha.txt", 0o100644),
    ]

    summary = pull(files=_Files(rows), http=http, root=root, source="home", chat_id="chat-a")

    assert not (root / "dl-host-auth").is_symlink() and not (root / "dl-host-auth").exists()
    assert not (root / "up").is_symlink() and not (root / "up").exists()
    assert os.readlink(root / "latest") == "alpha.txt"
    assert (root / "latest").read_bytes() == BODIES[ALPHA]
    assert summary.symlinks == 1 and summary.links_skipped == 2
    assert sorted(summary.links_skipped_paths) == [b"dl-host-auth", b"up"]
    assert [w for w in summary.warnings if "dl-host-auth was not linked" in w]
    assert all("outside the tree" in w for w in summary.warnings if "was not linked" in w)

    again = pull(files=_Files(rows), http=http, root=root, source="home", chat_id="chat-a")
    assert again.symlinks == 0 and again.links_skipped == 2
    assert os.readlink(root / "latest") == "alpha.txt"
    assert not (root / "dl-host-auth").exists() and not (root / "up").exists()
