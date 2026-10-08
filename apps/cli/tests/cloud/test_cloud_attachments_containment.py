"""The attachment materializer stays inside the chat's directory — sidecar too.

Every byte an attachment writes lands under
``<project>/attachments/<chat_id>/``. The file itself is resolved through
:class:`~alkera_cli.files.target.MaterializationTarget`, but the bytes go to a
``.alkera-part`` sidecar first, and the sidecar is a path of its own: if it is
derived without the same containment, a symlink planted at that name turns the
write into an arbitrary write and the promoted path into an arbitrary read.
These tests plant exactly that and assert nothing lands outside.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alkera_cli.cloud.attachments import (
    Attachment,
    AttachmentFetchError,
    materialize_attachments,
)

CHAT = "chat-contained"


class FakeFetcher:
    """Bytes by node id, with a hook that runs while the fetch is in flight.

    ``during`` is called after the materializer has chosen its paths and before
    a byte is written, which is where a racing process would plant a name.
    """

    def __init__(self, bodies: dict[str, bytes], during: Callable[[], None] | None = None) -> None:
        self.bodies = bodies
        self.during = during

    @asynccontextmanager
    async def fetch(self, node_id: str) -> AsyncIterator[AsyncIterator[bytes]]:
        if self.during is not None:
            self.during()
        try:
            body = self.bodies[node_id]
        except KeyError:
            raise AttachmentFetchError("the file could not be read") from None
        yield _served([body])


async def _served(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _attachment(node_id: str, filename: str, body: bytes) -> Attachment:
    return Attachment(node_id=node_id, filename=filename, size=len(body), mime="text/plain")


def _chat_dir(tmp_path: Path) -> Path:
    root = tmp_path / "attachments" / CHAT
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.asyncio
async def test_a_planted_symlink_sidecar_never_writes_outside_the_chat_directory(
    tmp_path: Path,
) -> None:
    """The sidecar name is derived from an attacker-chosen filename, so a
    symlink standing there must be refused, not written through."""
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    secret.write_bytes(b"PRIVATE KEY")
    chat_dir = _chat_dir(tmp_path)
    os.symlink(secret, chat_dir / "report.csv.alkera-part")

    landed = await materialize_attachments(
        [_attachment("n1", "report.csv", b"pwned")],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": b"pwned"}),
    )

    assert secret.read_bytes() == b"PRIVATE KEY"
    assert landed.files == ()
    assert len(landed.notices) == 1
    assert "report.csv" in landed.notices[0]
    assert not (chat_dir / "report.csv").exists()


@pytest.mark.asyncio
async def test_a_symlink_planted_at_the_leaf_mid_fetch_is_never_promoted(
    tmp_path: Path,
) -> None:
    """A link that appears at the file's own name while the bytes are in flight
    must not become the path the harness is handed."""
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    secret.write_bytes(b"PRIVATE KEY")
    chat_dir = _chat_dir(tmp_path)

    def plant() -> None:
        os.symlink(secret, chat_dir / "report.csv")

    landed = await materialize_attachments(
        [_attachment("n1", "report.csv", b"hello")],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": b"hello"}, during=plant),
    )

    assert secret.read_bytes() == b"PRIVATE KEY"
    assert landed.files == ()
    assert len(landed.notices) == 1
    assert (chat_dir / "report.csv").is_symlink()
    assert not (chat_dir / "report.csv.alkera-part").exists()


@pytest.mark.asyncio
async def test_the_ordinary_attachment_still_lands_and_leaves_no_sidecar(
    tmp_path: Path,
) -> None:
    landed = await materialize_attachments(
        [_attachment("n1", "report.csv", b"a,b\n1,2\n")],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": b"a,b\n1,2\n"}),
    )

    (one,) = landed.files
    assert landed.notices == ()
    assert one.path.read_bytes() == b"a,b\n1,2\n"
    chat_dir = tmp_path / "attachments" / CHAT
    assert sorted(p.name for p in chat_dir.iterdir()) == ["report.csv"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("../../../../etc/cron.d/pwn", id="dotdot"),
        pytest.param("no\x00nul.csv", id="nul"),
        pytest.param("x" * 255, id="max-length-segment"),
    ],
)
async def test_a_hostile_name_still_lands_inside_the_chat_directory(
    tmp_path: Path, hostile: str
) -> None:
    """The Files names contract decides the name; whatever the transcript says,
    the byte lands directly inside this chat's own directory — sidecar too."""
    body = b"payload"
    landed = await materialize_attachments(
        [_attachment("n1", hostile, body)],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": body}),
    )

    chat_dir = tmp_path / "attachments" / CHAT
    assert landed.notices == ()
    (one,) = landed.files
    assert one.path.parent == chat_dir
    assert one.path.read_bytes() == body
    assert not (tmp_path / "etc").exists()
    assert sorted(p.parent for p in chat_dir.iterdir()) == [chat_dir]
