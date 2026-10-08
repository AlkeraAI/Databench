"""The box puts a chat's attachments on disk before the turn reads them.

A fake Files API + a fake content origin stand in for the backend and the
object store: the mint answers a redirect, the redirect answers bytes, and the
tests assert what ends up on disk, what the harness is told, and what a reader
is told about a file that never arrived.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.attachments import (
    FAILED_HEADER,
    PROMPT_HEADER,
    Attachment,
    AttachmentFetchError,
    MaterializedAttachments,
    attachments_in,
    materialize_attachments,
    prompt_with_attachments,
)
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.files.live_sync import byte_deadline

CHAT = "chat-abc"
#: What the store declares for the body that stalls. Small, so the deadline it
#: buys — the size over the floor bandwidth — is milliseconds of real time; the
#: shape that gives a 2 GiB file its hours is pinned on the function itself.
_DECLARED = 4096


class FakeFetcher:
    """Bytes by node id; anything else is a refusal, like a 404 mint."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.asked: list[str] = []

    @asynccontextmanager
    async def fetch(self, node_id: str) -> AsyncIterator[AsyncIterator[bytes]]:
        self.asked.append(node_id)
        try:
            body = self.bodies[node_id]
        except KeyError:
            raise AttachmentFetchError(
                "the file could not be read (the server answered 404)"
            ) from None
        yield _served([body[:1], body[1:]] if len(body) > 1 else [body])


async def _served(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _part(node_id: str, filename: str, body: bytes, **extra: Any) -> dict[str, Any]:
    part: dict[str, Any] = {
        "type": "file",
        "part_id": f"p-{node_id}",
        "message_id": "m1",
        "source": "file",
        "node_id": node_id,
        "filename": filename,
        "size": len(body),
        "mime": "text/plain",
        "sha256": "",
    }
    part.update(extra)
    return part


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"id": "m1", "role": "user", "parts": list(parts)}


# -- reading the parts ------------------------------------------------------


def test_attachments_in_reads_node_id_off_the_extra_fields() -> None:
    """`node_id` is not on FilePart 1.0.0, so it arrives as an extra field —
    and the camel spelling the wire may use is read too."""
    found = attachments_in(
        _message(
            _part("n1", "a.txt", b"a"),
            {"type": "file", "nodeId": "n2", "filename": "b.txt", "size": 2},
        )
    )
    assert [one.node_id for one in found] == ["n1", "n2"]
    assert [one.filename for one in found] == ["a.txt", "b.txt"]


@pytest.mark.parametrize(
    "part",
    [
        pytest.param({"type": "text", "text": "hi", "node_id": "n1"}, id="not-a-file-part"),
        pytest.param({"type": "file", "filename": "a.txt"}, id="no-node-id"),
        pytest.param({"type": "file", "node_id": "   ", "filename": "a"}, id="blank-node-id"),
        pytest.param({"type": "file", "node_id": 7, "filename": "a"}, id="node-id-not-a-string"),
    ],
)
def test_attachments_in_skips_a_part_that_names_no_node(part: dict[str, Any]) -> None:
    assert attachments_in(_message(part)) == []


def test_attachments_in_takes_a_repeated_node_once() -> None:
    found = attachments_in(_message(_part("n1", "a.txt", b"a"), _part("n1", "a.txt", b"a")))
    assert [one.node_id for one in found] == ["n1"]


def test_attachments_in_survives_a_message_with_no_parts() -> None:
    assert attachments_in({"id": "m1", "role": "user"}) == []
    assert attachments_in({"id": "m1", "parts": "nope"}) == []


# -- materializing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_one_attachment_lands_with_its_exact_bytes_and_is_named_in_the_prompt(
    tmp_path: Path,
) -> None:
    body = b"col_a,col_b\n1,2\n"
    fetcher = FakeFetcher({"n1": body})
    found = attachments_in(_message(_part("n1", "rows.csv", body)))

    landed = await materialize_attachments(
        found, project_path=tmp_path, chat_id=CHAT, fetcher=fetcher
    )

    assert landed.notices == ()
    (one,) = landed.files
    assert one.path == tmp_path / "attachments" / CHAT / "rows.csv"
    assert one.path.read_bytes() == body
    assert one.size == len(body)
    # No sidecar is left behind once the file is promoted.
    assert sorted(p.name for p in one.path.parent.iterdir()) == ["rows.csv"]

    prompt = prompt_with_attachments("what is in this?", landed)
    assert prompt.startswith(PROMPT_HEADER + "\n")
    assert f"- {one.path} ({len(body)} bytes)" in prompt
    assert prompt.endswith("\n\nwhat is in this?")


@pytest.mark.asyncio
async def test_two_attachments_both_land_in_order(tmp_path: Path) -> None:
    bodies = {"n1": b"first", "n2": b"second"}
    found = attachments_in(
        _message(_part("n1", "a.txt", bodies["n1"]), _part("n2", "b.txt", bodies["n2"]))
    )

    landed = await materialize_attachments(
        found, project_path=tmp_path, chat_id=CHAT, fetcher=FakeFetcher(bodies)
    )

    assert landed.notices == ()
    assert [one.path.name for one in landed.files] == ["a.txt", "b.txt"]
    assert [one.path.read_bytes() for one in landed.files] == [b"first", b"second"]
    prompt = prompt_with_attachments("q", landed)
    assert prompt.index("a.txt") < prompt.index("b.txt")


@pytest.mark.asyncio
async def test_a_404_on_one_of_two_notices_it_and_keeps_the_other(tmp_path: Path) -> None:
    """The turn still runs, and the reader is told which file is missing."""
    found = attachments_in(
        _message(_part("gone", "missing.csv", b"xx"), _part("n2", "here.csv", b"ok"))
    )

    landed = await materialize_attachments(
        found, project_path=tmp_path, chat_id=CHAT, fetcher=FakeFetcher({"n2": b"ok"})
    )

    assert [one.path.name for one in landed.files] == ["here.csv"]
    assert (tmp_path / "attachments" / CHAT / "here.csv").read_bytes() == b"ok"
    assert not (tmp_path / "attachments" / CHAT / "missing.csv").exists()
    (notice,) = landed.notices
    assert "missing.csv" in notice and "404" in notice
    # The file that DID arrive is still offered to the agent.
    prompt = prompt_with_attachments("q", landed)
    assert "here.csv" in prompt
    # ...and so is the one that did not. The reader's own words link every file
    # they attached, so an agent told only about the successes hunts the disk
    # for the rest and calls the chat broken.
    assert FAILED_HEADER in prompt
    assert f"- {notice}" in prompt
    assert prompt.index(PROMPT_HEADER) < prompt.index(FAILED_HEADER)
    assert prompt.endswith("\n\nq")


@pytest.mark.asyncio
async def test_every_attachment_failing_still_tells_the_agent_they_are_missing(
    tmp_path: Path,
) -> None:
    """The transcript's failure: the chat renders a link, no bytes land, and the
    question reaches the agent with nothing saying the file never arrived."""
    found = attachments_in(_message(_part("gone", "apollo_leads_import.csv", b"xx")))

    landed = await materialize_attachments(
        found, project_path=tmp_path, chat_id=CHAT, fetcher=FakeFetcher({})
    )

    assert landed.files == ()
    prompt = prompt_with_attachments("what is in this?", landed)
    assert prompt != "what is in this?"
    assert prompt.startswith(FAILED_HEADER + "\n")
    assert "apollo_leads_import.csv" in prompt
    assert PROMPT_HEADER not in prompt
    assert prompt.endswith("\n\nwhat is in this?")


@pytest.mark.asyncio
async def test_a_size_that_does_not_match_is_a_notice_not_a_file(tmp_path: Path) -> None:
    part = _part("n1", "rows.csv", b"12345")
    part["size"] = 99
    landed = await materialize_attachments(
        attachments_in(_message(part)),
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": b"12345"}),
    )
    assert landed.files == ()
    assert "rows.csv" in landed.notices[0]
    assert list((tmp_path / "attachments" / CHAT).iterdir()) == []


@pytest.mark.asyncio
async def test_a_digest_that_does_not_match_is_a_notice_not_a_file(tmp_path: Path) -> None:
    part = _part("n1", "rows.csv", b"12345")
    part["sha256"] = hashlib.sha256(b"something else").hexdigest()
    landed = await materialize_attachments(
        attachments_in(_message(part)),
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": b"12345"}),
    )
    assert landed.files == ()
    assert "digest" in landed.notices[0]
    assert list((tmp_path / "attachments" / CHAT).iterdir()) == []


@pytest.mark.asyncio
async def test_the_declared_digest_of_the_real_bytes_is_accepted(tmp_path: Path) -> None:
    body = b"12345"
    part = _part("n1", "rows.csv", body)
    part["sha256"] = hashlib.sha256(body).hexdigest().upper()
    landed = await materialize_attachments(
        attachments_in(_message(part)),
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": body}),
    )
    assert landed.notices == ()
    assert landed.files[0].path.read_bytes() == body


@pytest.mark.asyncio
async def test_two_files_wanting_one_name_get_the_conflict_rename(tmp_path: Path) -> None:
    bodies = {"n1": b"one", "n2": b"two"}
    found = attachments_in(
        _message(_part("n1", "rows.csv", bodies["n1"]), _part("n2", "rows.csv", bodies["n2"]))
    )
    landed = await materialize_attachments(
        found, project_path=tmp_path, chat_id=CHAT, fetcher=FakeFetcher(bodies)
    )
    assert [one.path.name for one in landed.files] == ["rows.csv", "rows (1).csv"]
    assert (tmp_path / "attachments" / CHAT / "rows.csv").read_bytes() == b"one"
    assert (tmp_path / "attachments" / CHAT / "rows (1).csv").read_bytes() == b"two"


@pytest.mark.asyncio
async def test_a_second_turn_does_not_clobber_the_first_turn_s_file(tmp_path: Path) -> None:
    """A name already on disk from an earlier turn is taken, so the new bytes
    land beside it rather than over it."""
    for node, body in (("n1", b"one"), ("n2", b"two")):
        await materialize_attachments(
            attachments_in(_message(_part(node, "rows.csv", body))),
            project_path=tmp_path,
            chat_id=CHAT,
            fetcher=FakeFetcher({node: body}),
        )
    assert (tmp_path / "attachments" / CHAT / "rows.csv").read_bytes() == b"one"
    assert (tmp_path / "attachments" / CHAT / "rows (1).csv").read_bytes() == b"two"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("../../escaped.txt", id="dotdot-path"),
        pytest.param("..\\..\\escaped.txt", id="windows-dotdot-path"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("..", id="dotdot"),
        pytest.param(".", id="dot"),
        pytest.param("a/b/c.txt", id="nested"),
        pytest.param("", id="empty"),
        pytest.param("no\x00nul.txt", id="nul"),
    ],
)
async def test_a_filename_from_the_transcript_never_escapes_the_chat_directory(
    tmp_path: Path, hostile: str
) -> None:
    """The name is the transcript's, so it is attacker-controlled: whatever it
    says, the byte lands directly inside this chat's own directory."""
    outside = tmp_path / "escaped.txt"
    body = b"pwned"
    landed = await materialize_attachments(
        attachments_in(_message(_part("n1", hostile, body))),
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=FakeFetcher({"n1": body}),
    )

    chat_dir = tmp_path / "attachments" / CHAT
    assert landed.notices == ()
    (one,) = landed.files
    assert one.path.parent == chat_dir
    assert not outside.exists()
    assert not (tmp_path / "etc").exists()
    written = [p for p in chat_dir.iterdir()]
    assert [p.parent for p in written] == [chat_dir]


@pytest.mark.asyncio
async def test_a_chat_id_that_is_a_path_is_refused_before_any_write(tmp_path: Path) -> None:
    with pytest.raises(Exception, match=r"separator|may not"):
        await materialize_attachments(
            attachments_in(_message(_part("n1", "a.txt", b"x"))),
            project_path=tmp_path,
            chat_id="../../elsewhere",
            fetcher=FakeFetcher({"n1": b"x"}),
        )
    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_a_large_body_reaches_the_disk_a_chunk_at_a_time(tmp_path: Path) -> None:
    """A node is a Files node of whatever size the drive holds — a member may
    attach one of tens of gigabytes — so the box must never hold one.

    The STORE counts, at every chunk it serves, how many it has already served
    that are not yet on disk. A fetcher that collects the body before handing
    any of it over runs that count up to the whole file and the box dies of the
    attachment's size, taking every other chat it serves with it.
    """
    chunk = b"z" * (1 << 20)
    served = 64
    staged = tmp_path / "attachments" / CHAT
    ahead: list[int] = []

    def held() -> int:
        """How many served chunks have not reached the sidecar yet."""
        if not staged.is_dir():
            return len(ahead)
        return len(ahead) - sum(one.stat().st_size for one in staged.iterdir()) // len(chunk)

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed"})

        async def body() -> AsyncIterator[bytes]:
            for _ in range(served):
                ahead.append(held())
                yield chunk

        return httpx.Response(200, headers={"content-length": str(served << 20)}, content=body())

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(route),
    )
    digest = hashlib.sha256(chunk * served).hexdigest()
    landed = await materialize_attachments(
        [Attachment(node_id="n1", filename="big.bin", size=served << 20, sha256=digest)],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=await client.attachment_fetcher(),
    )

    (one,) = landed.files
    assert landed.notices == ()
    assert one.size == served << 20
    assert hashlib.sha256(one.path.read_bytes()).hexdigest() == digest
    # Bounded, and by a constant rather than by the file: at most a chunk or
    # two is between the store and the disk at any moment.
    assert max(ahead) <= 2, f"{max(ahead)} of {served} chunks were held before any was written"


@pytest.mark.asyncio
async def test_a_body_that_fails_partway_publishes_no_file_and_leaves_no_sidecar(
    tmp_path: Path,
) -> None:
    """Writing as the bytes arrive must not weaken the promise the buffer used
    to give for free: a body that dies mid-stream leaves the chat's directory
    exactly as it found it, and the reader is told."""

    async def body() -> AsyncIterator[bytes]:
        yield b"the first half"
        raise AttachmentFetchError("the file's bytes stopped arriving")

    class Failing:
        @asynccontextmanager
        async def fetch(self, node_id: str) -> AsyncIterator[AsyncIterator[bytes]]:
            yield body()

    landed = await materialize_attachments(
        [Attachment(node_id="n1", filename="half.bin", size=28)],
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=Failing(),
    )

    assert landed.files == ()
    (notice,) = landed.notices
    assert "half.bin" in notice and "stopped arriving" in notice
    assert list((tmp_path / "attachments" / CHAT).iterdir()) == []


@pytest.mark.asyncio
async def test_no_attachments_asks_for_nothing_and_leaves_the_prompt_alone(
    tmp_path: Path,
) -> None:
    fetcher = FakeFetcher({})
    landed = await materialize_attachments([], project_path=tmp_path, chat_id=CHAT, fetcher=fetcher)
    assert landed.files == () and landed.notices == ()
    assert fetcher.asked == []
    assert not (tmp_path / "attachments").exists()
    assert prompt_with_attachments("just a question", landed) == "just a question"


def test_a_prompt_with_only_notices_states_the_gap_without_naming_a_path() -> None:
    """A failed fetch never invents a file line the agent would try to open —
    but it does say the file is missing, so the agent stops looking for it."""
    only_notices = MaterializedAttachments(notices=("a.csv: gone",))
    prompt = prompt_with_attachments("q", only_notices)
    assert prompt == f"{FAILED_HEADER}\n- a.csv: gone\n\nq"
    assert PROMPT_HEADER not in prompt


def test_a_nameless_attachment_is_named_after_its_node() -> None:
    assert Attachment(node_id="n1").display_name == "attachment-n1"
    assert Attachment(node_id="n1", filename="  ").display_name == "attachment-n1"
    assert Attachment(node_id="n1", filename="dir/a.csv").display_name == "a.csv"


# -- the real fetcher: mint on the API, follow to the content origin ---------


@pytest.mark.asyncio
async def test_the_rest_fetcher_mints_then_reads_the_signed_url(tmp_path: Path) -> None:
    body = b"real bytes"
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization", "")))
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1", "root_id": "root"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed?x=1"})
        return httpx.Response(200, content=body)

    client = CloudRestClient(
        api_url="http://api.test",
        token="jwt-token",
        agent_id="machine-1",
        transport=httpx.MockTransport(handle),
    )
    fetcher = await client.attachment_fetcher()
    async with fetcher.fetch("node-9") as body_chunks:
        chunks = [chunk async for chunk in body_chunks]

    assert b"".join(chunks) == body
    minted = next(url for url, _ in seen if url.endswith("/content"))
    assert minted == "http://api.test/api/v1/files/drives/drive-1/items/node-9/content"
    # The mint carries the box's credential; the signed URL must not.
    assert dict(seen)["http://api.test/api/v1/files/drives/drive-1/items/node-9/content"] == (
        "Bearer jwt-token"
    )
    assert dict(seen)["http://origin.test/signed?x=1"] == ""

    landed = await materialize_attachments(
        attachments_in(_message(_part("node-9", "real.bin", body))),
        project_path=tmp_path,
        chat_id=CHAT,
        fetcher=fetcher,
    )
    assert landed.files[0].path.read_bytes() == body


@pytest.mark.asyncio
async def test_the_rest_fetcher_refuses_a_mint_that_did_not_redirect() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        return httpx.Response(404, json={"detail": {"code": "not_found"}})

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(handle),
    )
    fetcher = await client.attachment_fetcher()
    with pytest.raises(AttachmentFetchError, match="404"):
        async with fetcher.fetch("node-9"):
            pass


@pytest.mark.asyncio
async def test_the_rest_fetcher_refuses_a_redirect_with_no_location() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        return httpx.Response(302)

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(handle),
    )
    fetcher = await client.attachment_fetcher()
    with pytest.raises(AttachmentFetchError, match="download link"):
        async with fetcher.fetch("node-9"):
            pass


@pytest.mark.asyncio
async def test_a_store_that_refuses_the_signed_url_is_a_fetch_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed"})
        return httpx.Response(403, content=b"expired")

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(handle),
    )
    fetcher = await client.attachment_fetcher()
    with pytest.raises(AttachmentFetchError, match="403"):
        async with fetcher.fetch("node-9"):
            pass


def test_the_attachments_dir_is_under_the_project(tmp_path: Path) -> None:
    """The materialized path is `<workspace>/.alkera/attachments/<chat>/<name>`
    — the contract the harness prompt hands out as absolute paths."""
    project = tmp_path / ".alkera"
    expected = project / "attachments" / CHAT
    # The layout, not the host's separator: ``os.fspath`` spells this with
    # backslashes on Windows and the same three segments are the contract there.
    assert expected.parts[-3:] == (".alkera", "attachments", CHAT)
    assert expected.is_absolute()


# -- the service prepares them for a turn -----------------------------------


def _service(tmp_path: Path, handle: Any) -> tuple[Any, Any]:
    from _adapter_factory import FakeAdapterFactory
    from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
    from alkera_cli.harness import HarnessRuntime
    from alkera_cli.harness._fake import FakeAdapter
    from alkera_core.project import ProjectDirectory

    project = ProjectDirectory(tmp_path / ".alkera")
    runtime = HarnessRuntime(project, adapter_factory=FakeAdapterFactory(FakeAdapter))
    settings = MirrorSettings(
        api_url="http://api.test",
        token="device-jwt",
        project_dir=tmp_path,
        machine_name="box",
        provider_pod_id="pod-box",
        machine_type_code="cpu3c",
    )
    rest = CloudRestClient(
        api_url=settings.api_url,
        token=settings.token,
        agent_id="machine:box",
        transport=httpx.MockTransport(handle),
    )
    return CloudMirrorService(settings, runtime, rest=rest), project


@pytest.mark.asyncio
async def test_the_service_materializes_a_message_s_attachments_once_per_box(
    tmp_path: Path,
) -> None:
    """`prepare_attachments` fetches through the box's own credential, and the
    drive lookup that binds the fetcher happens once, not once per turn."""
    body = b"attached bytes"
    drive_calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            drive_calls.append(str(request.url))
            return httpx.Response(200, json={"id": "drive-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed"})
        return httpx.Response(200, content=body)

    service, project = _service(tmp_path, handle)

    first = await service.prepare_attachments(CHAT, _message(_part("n1", "a.bin", body)))
    second = await service.prepare_attachments(CHAT, _message(_part("n2", "b.bin", body)))

    assert first.notices == () and second.notices == ()
    assert first.files[0].path == project.path / "attachments" / CHAT / "a.bin"
    assert first.files[0].path.read_bytes() == body
    assert second.files[0].path.read_bytes() == body
    assert len(drive_calls) == 1


@pytest.mark.asyncio
async def test_the_service_notices_a_files_api_it_cannot_reach(tmp_path: Path) -> None:
    """A drive lookup that fails must not stop the turn — it names the files."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"code": "unavailable"}})

    service, project = _service(tmp_path, refuse)

    landed = await service.prepare_attachments(CHAT, _message(_part("n1", "a.bin", b"x")))

    assert landed.files == ()
    assert "a.bin" in landed.notices[0]
    assert not (project.path / "attachments").exists()


@pytest.mark.asyncio
async def test_the_service_asks_for_nothing_when_a_message_has_no_attachments(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "drive-1"})

    service, _project = _service(tmp_path, handle)

    landed = await service.prepare_attachments(CHAT, {"id": "m1", "parts": [{"type": "text"}]})

    assert landed.files == () and landed.notices == ()
    assert calls == []


# -- the fetch is bounded by the file's size, never by a constant -------------


def test_a_transfers_budget_is_its_own_size_over_a_floor_bandwidth() -> None:
    """A 2 GiB file is given hours; a note is given the floor. The shape is
    shared with the live sync's inbound downloads rather than re-spelled."""
    two_gib = 2 << 30
    assert byte_deadline(two_gib, floor=60.0) == two_gib / 131_072
    assert byte_deadline(two_gib, floor=60.0) > 16_000
    assert byte_deadline(1024, floor=60.0) == 60.0
    assert byte_deadline(None, floor=60.0) == 60.0
    assert byte_deadline(0, floor=60.0) == 60.0


@pytest.mark.asyncio
async def test_a_body_that_stops_arriving_fails_by_the_size_it_declared() -> None:
    """The store said how many bytes are coming, so the wait is sized by them:
    a transfer that stalls is named with the size and with what did arrive,
    and the sixty-second ceiling that used to cut a large file is gone."""

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed"})

        async def body() -> AsyncIterator[bytes]:
            yield b"x" * 1024
            # Then a wait far longer than the size buys: the stall the
            # deadline exists to catch.
            await asyncio.sleep(5.0)
            yield b"never seen"

        return httpx.Response(200, headers={"content-length": str(_DECLARED)}, content=body())

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(route),
    )
    fetcher = await client.attachment_fetcher()
    # A floor small enough that the derived deadline — the declared size over
    # the floor bandwidth — is milliseconds, so the test pays no real time for
    # proving that the SIZE is what set it.
    fetcher._timeout = 0.001
    expected = byte_deadline(_DECLARED, floor=0.001)
    assert expected > 0.001  # the size, not the floor, is the budget

    started = time.monotonic()
    with pytest.raises(AttachmentFetchError) as refused:
        async with fetcher.fetch("node-9") as stalling:
            async for _ in stalling:
                pass

    assert f"{_DECLARED} bytes" in str(refused.value)
    # What did arrive is named too: a stall reads as a stall, not as a file
    # the store never sent.
    assert "1024 bytes did" in str(refused.value)
    assert time.monotonic() - started >= expected


@pytest.mark.asyncio
async def test_a_body_slower_than_the_old_minute_still_lands() -> None:
    """The regression a fixed ceiling causes: bytes that arrive slowly are
    bytes that arrive. Nothing is cut while the body is still coming."""
    declared = 4 << 20

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/drives":
            return httpx.Response(200, json={"id": "drive-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://origin.test/signed"})

        async def body() -> AsyncIterator[bytes]:
            for _ in range(4):
                await asyncio.sleep(0.01)
                yield b"y" * (1 << 20)

        return httpx.Response(200, headers={"content-length": str(declared)}, content=body())

    client = CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine-1",
        transport=httpx.MockTransport(route),
    )
    fetcher = await client.attachment_fetcher()
    # The per-read cap is gone, so a body that dawdles past a short ceiling is
    # not cut: only the size-derived total would end it.
    fetcher._timeout = 0.02
    async with fetcher.fetch("node-9") as body_chunks:
        arrived = sum([len(chunk) async for chunk in body_chunks])

    assert arrived == declared


# ---- the composer's own links: under uploads/, and the older top-level form ----


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        pytest.param(
            "see ![Image 1](uploads/paste-1-ab12.png)",
            ["uploads/paste-1-ab12.png"],
            id="image-under-uploads",
        ),
        pytest.param(
            "[File 2: q.csv](uploads/file-2-cd34.csv) and [File 1: Makefile](uploads/file-1-ef56)",
            ["uploads/file-2-cd34.csv", "uploads/file-1-ef56"],
            id="files-under-uploads",
        ),
        pytest.param(
            "an older chat: ![Image 1](paste-1-ab12.png)",
            ["paste-1-ab12.png"],
            id="top-level-from-before-uploads",
        ),
        pytest.param(
            "![Image 1](uploads/paste-1-ab12.png) ![Image 1](uploads/paste-1-ab12.png)",
            ["uploads/paste-1-ab12.png"],
            id="one-file-linked-twice",
        ),
    ],
)
def test_a_composer_link_is_taken_under_uploads_and_at_the_top_level(
    text: str, paths: list[str]
) -> None:
    from alkera_cli.cloud.attachments import linked_files_in

    assert [one.path for one in linked_files_in({"text": text})] == paths


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("![chart](charts/paste-1-ab12.png)", id="another-folder"),
        pytest.param("![x](uploads/report.png)", id="uploads-but-not-the-staged-shape"),
        pytest.param("![x](uploads/uploads/paste-1-ab12.png)", id="nested-uploads"),
        pytest.param("![x](../uploads/paste-1-ab12.png)", id="escape"),
        pytest.param("![x](/uploads/paste-1-ab12.png)", id="absolute"),
    ],
)
def test_a_link_the_composer_did_not_write_is_not_taken_for_one(text: str) -> None:
    from alkera_cli.cloud.attachments import linked_files_in

    assert linked_files_in({"text": text}) == []
