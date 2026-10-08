"""A chat's folder on a live node syncs with the drive in both directions,
through the product as a reader uses it.

Everything here is the real wire against a running stack and whichever node
placement picks: the reader signs in, opens a chat, puts a file in the chat
folder's ``uploads/`` exactly as the composer does, and asks the agent to read
it back and to write a file of its own. The reply proves the upload reached
the container the agent's shell runs in; the file the agent wrote, read back
out of the drive, proves the box pushed its work up. Both halves went missing
on the shared pool node: the box asked the drive for "the caller's drive",
which a machine credential has none of, and never took a single folder.

Costs one agent turn on the stack it is pointed at, so it runs in the ``live``
tier and skips unless the stack is named:

    ALKERA_LIVE_API_URL=http://localhost:27220 ALKERA_LIVE_ORIGIN=http://localhost:27222
    ALKERA_LIVE_EMAIL=… ALKERA_LIVE_PASSWORD=… uv run pytest -m live <this file>
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from blake3 import blake3

#: How long a chat may take to be placed and its box to report ready.
PLACEMENT_BUDGET_SECONDS = 180.0
#: How long one agent turn may take, prompt to reply.
TURN_BUDGET_SECONDS = float(os.environ.get("ALKERA_LIVE_TURN_TIMEOUT", "360"))
#: How long the box's checkpoint push and the live plane may take to land the
#: agent's file in the drive after the turn.
LANDING_BUDGET_SECONDS = 120.0

#: A real turn on a real node outlasts the suite's default per-test budget;
#: the three waits above add up to the whole, with room for the sign-in and
#: the uploads around them.
WHOLE_BUDGET_SECONDS = PLACEMENT_BUDGET_SECONDS + TURN_BUDGET_SECONDS + LANDING_BUDGET_SECONDS + 120
pytestmark = [pytest.mark.live, pytest.mark.timeout(WHOLE_BUDGET_SECONDS)]
POLL_SECONDS = 5.0
FILES = "/api/v1/files"


def _harness_event(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The harness event a transcript row carries, with its kind.

    The machine publishes each row as an entry envelope — ``kind``, ``role``
    and the event itself under ``payload`` — so the text of a reply and the
    output of a tool sit one level below the row's ``payload``. A person's own
    prompt row is the prompt record itself, with no inner event.
    """
    envelope = item.get("payload")
    envelope = envelope if isinstance(envelope, dict) else {}
    kind = str(item.get("kind") or envelope.get("kind") or "")
    inner = envelope.get("payload")
    return kind, inner if isinstance(inner, dict) else envelope


def _text_part(event: dict[str, Any]) -> str:
    """The text of a ``part.created`` event, empty for any other part."""
    part = event.get("part")
    if not isinstance(part, dict) or part.get("type") != "text":
        return ""
    return str(part.get("text") or "")


@dataclass(frozen=True)
class LiveStack:
    api_url: str
    origin: str
    email: str
    password: str


def _stack() -> LiveStack:
    api_url = os.environ.get("ALKERA_LIVE_API_URL", "").rstrip("/")
    email = os.environ.get("ALKERA_LIVE_EMAIL", "")
    password = os.environ.get("ALKERA_LIVE_PASSWORD", "")
    if not (api_url and email and password):
        pytest.skip(
            "names no live stack: set ALKERA_LIVE_API_URL, ALKERA_LIVE_EMAIL, ALKERA_LIVE_PASSWORD"
        )
    return LiveStack(
        api_url=api_url,
        origin=os.environ.get("ALKERA_LIVE_ORIGIN", api_url),
        email=email,
        password=password,
    )


class Reader:
    """A signed-in person on the stack: cookie session, the origin every
    mutation must carry, an idempotency key per write."""

    def __init__(self, stack: LiveStack) -> None:
        self.stack = stack
        self.http = httpx.Client(
            base_url=stack.api_url,
            headers={"Origin": stack.origin},
            timeout=httpx.Timeout(30.0, read=60.0),
        )
        signed_in = self.http.post(
            "/api/v1/auth/login", json={"email": stack.email, "password": stack.password}
        )
        assert signed_in.status_code == 200, f"login: {signed_in.status_code} {signed_in.text}"

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _idem() -> dict[str, str]:
        return {"Idempotency-Key": secrets.token_hex(8)}

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._idem(), **kwargs.pop("headers", {})}
        return self.http.post(path, headers=headers, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._idem(), **kwargs.pop("headers", {})}
        return self.http.put(path, headers=headers, **kwargs)

    # -- the chat -----------------------------------------------------------

    def open_chat(self, title: str) -> dict[str, Any]:
        made = self.post("/api/v1/chats", json={"title": title, "client_id": str(uuid.uuid4())})
        assert made.status_code == 201, f"open chat: {made.status_code} {made.text}"
        chat: dict[str, Any] = made.json()
        return chat

    def chat(self, chat_id: str) -> dict[str, Any]:
        read = self.http.get(f"/api/v1/chats/{chat_id}")
        assert read.status_code == 200, f"read chat: {read.status_code} {read.text}"
        body: dict[str, Any] = read.json()
        return body

    def wait_placed(self, chat_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + PLACEMENT_BUDGET_SECONDS
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.chat(chat_id)
            status = last.get("machine_status")
            if status == "refused":
                pytest.fail(f"nothing publishes the chat: {last.get('machine_refusal_reason')}")
            if status == "ready" and last.get("files_node_id") and last.get("files_drive_id"):
                return last
            time.sleep(POLL_SECONDS)
        pytest.fail(f"the chat was not placed on a ready box in time: {last}")

    def say(self, chat_id: str, text: str) -> None:
        sent = self.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": text, "client_id": str(uuid.uuid4())},
        )
        assert sent.status_code in (200, 201, 202), f"send: {sent.status_code} {sent.text}"

    def transcript(self, chat_id: str) -> list[dict[str, Any]]:
        page = self.http.get(f"/api/v1/chats/{chat_id}/messages", params={"limit": 200})
        assert page.status_code == 200, f"transcript: {page.status_code} {page.text}"
        items: list[dict[str, Any]] = page.json().get("items", [])
        return items

    def wait_reply(self, chat_id: str, *, containing: str) -> tuple[str, list[str]]:
        """The first assistant text carrying ``containing``, and every tool
        output the turn produced on the way — the box's own words about what
        it saw in the container."""
        deadline = time.monotonic() + TURN_BUDGET_SECONDS
        outputs: list[str] = []
        while time.monotonic() < deadline:
            outputs = []
            for item in self.transcript(chat_id):
                kind, event = _harness_event(item)
                if item.get("role") == "tool" and kind == "tool.call_update":
                    output = event.get("output")
                    if event.get("status") == "completed" and output:
                        outputs.append(str(output))
                if item.get("role") == "assistant" and kind == "part.created":
                    text = _text_part(event)
                    if containing in text:
                        return text, outputs
            if self.chat(chat_id).get("machine_status") == "refused":
                pytest.fail("the box stopped publishing mid-turn")
            time.sleep(POLL_SECONDS)
        pytest.fail(
            f"no reply carrying {containing!r} within {TURN_BUDGET_SECONDS:.0f}s; "
            f"tool outputs seen: {outputs!r}"
        )

    # -- the drive ----------------------------------------------------------

    def child(self, drive: str, parent: str, name: str, *, kind: str = "folder") -> str:
        made = self.post(
            f"{FILES}/drives/{drive}/items/{parent}/children", json={"name": name, "kind": kind}
        )
        if made.status_code == 201:
            return str(made.json()["id"])
        assert made.status_code == 409, f"child {name}: {made.status_code} {made.text}"
        found = self.find(drive, parent, name)
        assert found is not None, f"{name} answered 409 but is not listed"
        return found

    def find(self, drive: str, parent: str, name: str) -> str | None:
        listed = self.http.get(f"{FILES}/drives/{drive}/items/{parent}/children")
        assert listed.status_code == 200, f"list: {listed.status_code} {listed.text}"
        for row in listed.json().get("value", []):
            if row.get("name") == name:
                return str(row["id"])
        return None

    def upload(self, drive: str, parent: str, name: str, payload: bytes) -> None:
        """The composer's path for a new file: open, one part, complete."""
        opened = self.post(
            f"{FILES}/uploads",
            json={"declaredSize": len(payload), "name": name, "parentId": parent},
        )
        assert opened.status_code == 201, f"open upload: {opened.status_code} {opened.text}"
        upload_id = opened.json()["uploadId"]
        checksum = blake3(payload).digest().hex()
        part = self.put(
            f"{FILES}/uploads/{upload_id}/parts/1",
            content=payload,
            headers={"X-Part-Checksum": checksum},
        )
        assert part.status_code == 200, f"part: {part.status_code} {part.text}"
        done = self.post(
            f"{FILES}/uploads/{upload_id}/complete",
            json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
        )
        assert done.status_code == 202, f"complete: {done.status_code} {done.text}"
        assert drive  # the session was opened against a node on this drive

    def content(self, drive: str, item: str) -> bytes:
        served = self.http.get(
            f"{FILES}/drives/{drive}/items/{item}/content", follow_redirects=True
        )
        assert served.status_code == 200, f"content: {served.status_code} {served.text}"
        return served.content

    def wait_landed(self, drive: str, parent: str, name: str) -> bytes:
        deadline = time.monotonic() + LANDING_BUDGET_SECONDS
        while time.monotonic() < deadline:
            found = self.find(drive, parent, name)
            if found is not None:
                return self.content(drive, found)
            time.sleep(POLL_SECONDS)
        pytest.fail(f"{name} never reached the drive within {LANDING_BUDGET_SECONDS:.0f}s")


@pytest.fixture
def reader() -> Iterator[Reader]:
    person = Reader(_stack())
    try:
        yield person
    finally:
        person.close()


def test_an_upload_reaches_the_agents_container_and_the_agents_file_reaches_the_drive(
    reader: Reader,
) -> None:
    tag = secrets.token_hex(4)
    chat = reader.open_chat(f"qa-folder-live {tag}")
    chat_id = str(chat["id"])
    try:
        placed = reader.wait_placed(chat_id)
        drive, node = str(placed["files_drive_id"]), str(placed["files_node_id"])
        # The sandbox this chat runs in is the boundary; the turn is one read
        # and one write in the chat's own folder, run without a card to click.
        stance = reader.put(f"/api/v1/chats/{chat_id}/permission-mode", json={"mode": "bypass"})
        assert stance.status_code == 200, f"stance: {stance.status_code} {stance.text}"

        # Drive → box: the reader's file, where the composer puts one.
        scratch = reader.child(drive, node, "scratch")
        uploads = reader.child(drive, scratch, "uploads")
        proof = f"folder-proof-{tag}"
        upload_name = f"qa-live-{tag}.txt"
        reader.upload(drive, uploads, upload_name, f"{proof}\n".encode())

        reply_name = f"qa-live-{tag}-reply.txt"
        reader.say(
            chat_id,
            f"Run `cat uploads/{upload_name}` in your working directory and put its exact "
            f"contents on the first line of your answer. Then write a file named `{reply_name}` "
            "in your working directory containing exactly the word pong, and confirm you did.",
        )
        reply, outputs = reader.wait_reply(chat_id, containing=proof)
        assert proof in reply, outputs

        # Box → drive: what the agent wrote is in the chat folder for the reader.
        landed = reader.wait_landed(drive, scratch, reply_name)
        assert landed.strip() == b"pong", landed
    finally:
        reader.http.delete(f"/api/v1/chats/{chat_id}")
