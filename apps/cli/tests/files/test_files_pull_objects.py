"""What a pull does with the two node shapes that are not ordinary files.

Both claims here are about a tree the server describes but a laptop must not
end up holding a naive copy of:

* a **chat folder** carries ``NO_DOWNLOAD``. Its item reads fine and its
  children list fine, so a walk that only looks at ``kind`` descends straight
  into it and writes the conversation's attachments and outputs to disk — the
  exact bytes the platform's own content route refuses. The pull has to honour
  the capability, and honour it *before* asking for the subtree;
* an **object pointer** (``.alkerareport`` / ``.alkeraquery``) is a stand-in
  document whose whole value is the address it carries. A pointer written with
  a null ``webUrl`` opens nothing.

The server is a fake that answers exactly the four calls a pull makes, over a
real ``httpx`` client whose transport serves the content redirect and the bytes
behind it, so a node the pull decides to download really does land on disk and
the assertions are about files, not about calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.pull import pull
from alkera_core.files.names import NAME_MAX_BYTES

DRIVE = "11111111-1111-1111-1111-111111111111"
HOME = "aaaaaaaa-0000-0000-0000-000000000001"
CHAT_NODE = "aaaaaaaa-0000-0000-0000-000000000002"
CHAT_OUTPUTS = "aaaaaaaa-0000-0000-0000-000000000003"
SECRET_FILE = "aaaaaaaa-0000-0000-0000-000000000004"
PUBLIC_FILE = "aaaaaaaa-0000-0000-0000-000000000005"
REPORT_NODE = "aaaaaaaa-0000-0000-0000-000000000006"
REPORT_OBJECT = "bbbbbbbb-0000-0000-0000-000000000001"

SECRET_BYTES = b"the conversation's own attachment\n"
PUBLIC_BYTES = b"an ordinary file beside it\n"


def _hash(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


def _folder(node_id: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "kind": "folder", "name": name, **extra}


def _file(node_id: str, name: str, payload: bytes, **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "file",
        "name": name,
        "file": {"contentHash": _hash(payload), "size": len(payload)},
        **extra,
    }


#: The bit the chat folder carries. ``can_download`` is already the AND of
#: READ and EXPORT, which is why the folder still lists.
NO_DOWNLOAD: dict[str, Any] = {
    "capabilities": {"can_read": True, "can_download": False, "refusals": {"export": "no_download"}}
}
DOWNLOADABLE: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}


class FakeFiles:
    """The slice of ``client.files`` a pull drives, over a fixed tree."""

    def __init__(self, tree: Mapping[str, list[dict[str, Any]]]) -> None:
        self._tree = tree
        self.listed: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        raise AssertionError("the pull under test resolves its root by path")

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        assert drive_id == DRIVE
        return _folder(HOME, item_path.rsplit("/", 1)[-1], **DOWNLOADABLE)

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        self.listed.append(item_id)
        yield from self._tree.get(item_id, [])


def _content(request: httpx.Request) -> httpx.Response:
    """The content route's redirect, and the origin behind it."""
    path = request.url.path
    if path.endswith("/content"):
        node_id = path.rsplit("/", 2)[-2]
        return httpx.Response(302, headers={"location": f"https://content.test/c/{node_id}"})
    body = {SECRET_FILE: SECRET_BYTES, PUBLIC_FILE: PUBLIC_BYTES}[path.rsplit("/", 1)[-1]]
    return httpx.Response(200, content=body)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(
        transport=httpx.MockTransport(_content), base_url="https://api.test"
    ) as client:
        yield client


def _chat_tree() -> dict[str, list[dict[str, Any]]]:
    """A home folder holding one chat folder and one ordinary file."""
    return {
        HOME: [
            _folder(CHAT_NODE, "Quarterly Revenue.alkerachat", subtype="chat", **NO_DOWNLOAD),
            _file(PUBLIC_FILE, "notes.txt", PUBLIC_BYTES, **DOWNLOADABLE),
        ],
        CHAT_NODE: [_folder(CHAT_OUTPUTS, "outputs", **NO_DOWNLOAD)],
        CHAT_OUTPUTS: [_file(SECRET_FILE, "report.pdf", SECRET_BYTES, **DOWNLOADABLE)],
    }


def test_a_chat_folder_is_skipped_and_its_subtree_never_reaches_disk(
    tmp_path: Path, http: httpx.Client
) -> None:
    """The bytes the content route refuses do not arrive by way of the walk.

    ``report.pdf`` is marked downloadable *itself* — the flag lives on the
    chat above it — so a pull that decides node by node happily writes it. The
    claim is that the refusal on the folder governs everything beneath it.
    """
    files = FakeFiles(_chat_tree())
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source="home")

    assert not (root / "Quarterly Revenue.alkerachat").exists()
    assert list(root.rglob("report.pdf")) == []
    # The ordinary sibling still lands: the skip is the capability's, not a
    # blanket refusal to pull a tree that happens to contain a chat.
    assert (root / "notes.txt").read_bytes() == PUBLIC_BYTES
    assert summary.files == 1
    assert summary.undownloadable == 1


def test_the_chat_folders_children_are_never_even_listed(
    tmp_path: Path, http: httpx.Client
) -> None:
    """Pruning happens before the request, not after the answer.

    Listing a chat's ``outputs/`` to then throw the rows away would still put
    the conversation's file names in front of a caller the server has told to
    keep out, and would pay a request per folder to do it.
    """
    files = FakeFiles(_chat_tree())

    pull(files=files, http=http, root=tmp_path / "local", source="home")

    assert files.listed == [HOME]


def test_the_skip_names_the_chat_in_one_line(tmp_path: Path, http: httpx.Client) -> None:
    """A silent skip is indistinguishable from an empty chat."""
    files = FakeFiles(_chat_tree())

    summary = pull(files=files, http=http, root=tmp_path / "local", source="home")

    assert len(summary.warnings) == 1
    assert "Quarterly Revenue.alkerachat" in summary.warnings[0]


def test_pulling_an_undownloadable_root_writes_nothing(tmp_path: Path, http: httpx.Client) -> None:
    """Naming the chat itself as the source is refused the same way.

    Otherwise the one case where the capability matters most — a user pulling
    the conversation directly — is the one case the walk never checks, because
    the root is not a child of anything.
    """

    class RootIsTheChat(FakeFiles):
        def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
            return _folder(CHAT_NODE, "Quarterly Revenue.alkerachat", **NO_DOWNLOAD)

    files = RootIsTheChat(_chat_tree())
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source="Quarterly Revenue.alkerachat")

    assert files.listed == []
    assert list(root.rglob("*")) == []
    assert summary.undownloadable == 1
    assert "Quarterly Revenue.alkerachat" in summary.warnings[0]


# ---------------------------------------------------------------------------
# A name that was legal when it was stored and cannot be written today
# ---------------------------------------------------------------------------
#
# The drive's byte ceiling is NAME_MAX less the room the pull's sidecar needs,
# so every name it accepts is one a machine can be handed. Rows stored before
# that ceiling was lowered are never migrated or renamed — they stay readable
# and renameable-to-something-shorter — which means the pull is the one place
# that meets a name it cannot write. Left to the kernel it is an ENAMETOOLONG
# raised from inside a streaming download, with nothing naming the file.

LEGACY_NODE = "aaaaaaaa-0000-0000-0000-000000000007"
LEGACY_CHILD = "aaaaaaaa-0000-0000-0000-000000000008"

#: One byte past what the drive takes today, and legal on the filesystem: the
#: whole 244-to-255 band the old rule let through.
_LEGACY_NAME = "L" * (NAME_MAX_BYTES + 1)


def _legacy_tree(kind: str) -> dict[str, list[dict[str, Any]]]:
    """A home holding one node under a legacy name, and one ordinary file."""
    legacy: dict[str, Any] = (
        _folder(LEGACY_NODE, _LEGACY_NAME, **DOWNLOADABLE)
        if kind == "folder"
        else _file(LEGACY_NODE, _LEGACY_NAME, PUBLIC_BYTES, **DOWNLOADABLE)
    )
    return {
        HOME: [legacy, _file(PUBLIC_FILE, "notes.txt", PUBLIC_BYTES, **DOWNLOADABLE)],
        LEGACY_NODE: [_file(LEGACY_CHILD, "inside.txt", SECRET_BYTES, **DOWNLOADABLE)],
    }


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_a_name_this_machine_cannot_hold_is_refused_rather_than_crashed_on(
    tmp_path: Path, http: httpx.Client, kind: str
) -> None:
    """Named in one line, and the rest of the tree still arrives.

    The sidecar for this name is longer than any filesystem takes, so writing
    it would die with ENAMETOOLONG partway through a download. One legacy row
    must not cost a person the whole pull, so it is reported and stepped over.
    """
    files = FakeFiles(_legacy_tree(kind))
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source="home")

    assert summary.unpullable == 1
    assert (root / "notes.txt").read_bytes() == PUBLIC_BYTES
    assert list(root.iterdir()) == [root / "notes.txt"]
    assert len(summary.warnings) == 1
    said = summary.warnings[0]
    assert str(NAME_MAX_BYTES) in said and str(len(_LEGACY_NAME)) in said
    assert "rename it to something shorter" in said


def test_a_legacy_folder_is_not_descended_into(tmp_path: Path, http: httpx.Client) -> None:
    """There is no path for its children to hang off.

    Listing it would spend a request per level to then throw every row away,
    and each child would be reported separately for a reason that belongs to
    the folder above it.
    """
    files = FakeFiles(_legacy_tree("folder"))

    summary = pull(files=files, http=http, root=tmp_path / "local", source="home")

    assert files.listed == [HOME]
    assert summary.unpullable == 1


def test_pulling_a_legacy_name_as_the_root_writes_nothing(
    tmp_path: Path, http: httpx.Client
) -> None:
    """The root is a child of nothing, so the walk never sees it.

    Asking for the folder directly is exactly what a person does after being
    told it stayed behind, and it is the one path where a missed check reaches
    the kernel.
    """

    class RootIsLegacy(FakeFiles):
        def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
            return _folder(LEGACY_NODE, _LEGACY_NAME, **DOWNLOADABLE)

    files = RootIsLegacy(_legacy_tree("folder"))
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source=_LEGACY_NAME)

    assert summary.unpullable == 1
    assert files.listed == []
    assert list(root.rglob("*")) == []


def test_a_name_exactly_at_the_ceiling_is_pulled_like_any_other(
    tmp_path: Path, http: httpx.Client
) -> None:
    """The negative twin, one byte shorter.

    Without it the refusal above would pass just as well if the pull had begun
    refusing every long name, which would strand files the drive still takes.
    """
    at_cap = "L" * NAME_MAX_BYTES
    files = FakeFiles({HOME: [_file(PUBLIC_FILE, at_cap, PUBLIC_BYTES, **DOWNLOADABLE)]})
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source="home")

    assert summary.unpullable == 0
    assert (root / at_cap).read_bytes() == PUBLIC_BYTES
    assert summary.warnings == []


def _pointer(node: dict[str, Any], root: Path) -> dict[str, Any]:
    text = (root / str(node["name"])).read_text(encoding="utf-8")
    decoded: dict[str, Any] = json.loads(text)
    return decoded


@pytest.mark.parametrize(
    ("facet", "why"),
    [
        pytest.param(
            {"type": "report", "id": REPORT_OBJECT, "web_url": f"/objects/{REPORT_OBJECT}"},
            "the facet the server sends today",
            id="facet-carries-the-url",
        ),
        pytest.param(
            {"type": "report", "id": REPORT_OBJECT},
            "a server that sends no address at all",
            id="facet-carries-no-url",
        ),
        pytest.param(
            {"type": "report", "id": REPORT_OBJECT, "webUrl": f"/objects/{REPORT_OBJECT}"},
            "a facet spelled the other way",
            id="facet-spelled-camel",
        ),
    ],
)
def test_a_pointer_carries_the_address_the_registry_gives(
    tmp_path: Path, http: httpx.Client, facet: dict[str, Any], why: str
) -> None:
    """The on-disk pointer opens the page the drive's own item opens.

    Both sides derive the path from ``providers.registry.object_web_path``, so
    the pointer cannot be null while the facet has an address, and cannot name
    a different page from the one the portal routes to — whatever spelling the
    wire used, or whether it carried the address at all.
    """
    node = {
        "id": REPORT_NODE,
        "kind": "object",
        "name": "Q3 Revenue.alkerareport",
        "subtype": "report",
        "object": facet,
        **DOWNLOADABLE,
    }
    files = FakeFiles({HOME: [node]})
    root = tmp_path / "local"

    summary = pull(files=files, http=http, root=root, source="home")

    written = _pointer(node, root)
    assert summary.pointers == 1
    assert written["webUrl"] == f"/objects/{REPORT_OBJECT}", why
    assert written["kind"] == "report"
    assert written["nodeId"] == REPORT_NODE


def test_a_chat_pointer_opens_the_conversation_not_the_object_page(
    tmp_path: Path, http: httpx.Client
) -> None:
    """The registry is asked, not a rule spelled a second time here.

    A chat routes to ``/chat/<id>`` and everything else to ``/objects/<id>``;
    deriving the path means a pointer for a kind added later is right without
    this module being touched.
    """
    node = {
        "id": REPORT_NODE,
        "kind": "object",
        "name": "Old Chat.alkerachat",
        "object": {"type": "chat", "id": REPORT_OBJECT},
        **DOWNLOADABLE,
    }
    files = FakeFiles({HOME: [node]})
    root = tmp_path / "local"

    pull(files=files, http=http, root=root, source="home")

    assert _pointer(node, root)["webUrl"] == f"/chat/{REPORT_OBJECT}"
