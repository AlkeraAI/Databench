"""What a chat started from a chat template is handed before its first turn.

A template is somebody's saved starting point: a brief they wrote and the files
a new chat should open with. The files arrive by being copied into the chat's
working directory, so the only thing left for the box to do is put the prose in
front of the agent — once, framed as the author's words rather than the user's,
with instructions to ASK before running anything it names.

Two independent reads decide what that block says, and the split is the point:

* the **brief** is on the chat's own record, copied there when the chat was
  created, so a box whose operator may not read a colleague's private template
  still has it;
* the **template's name** is on the template's record, read best-effort, so the
  refusal that the copy exists to survive costs a name and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.source_context import (
    MAX_TEMPLATE_NOTES_BYTES,
    TEMPLATE_NOTES_FILE,
    source_brief,
    source_document,
    template_notes,
)

CHAT = "chat-a"
TEMPLATE_ID = "44444444-4444-4444-4444-444444444444"
#: What the template's author wrote, as it was copied onto the chat.
COPIED_BRIEF = "Pull last quarter's mentions for the region the user names, then chart them."
#: What the TEMPLATE's own record says. Deliberately different text: a box that
#: read the brief from the template instead of from the chat would quote this.
TEMPLATE_RECORD_BRIEF = "a brief only the template's owner may read"

TEMPLATE_RECORD: dict[str, Any] = {
    "id": TEMPLATE_ID,
    "type": "chat_template",
    "title": "Weekly mentions",
    "version": 3,
    "spec": {"brief": TEMPLATE_RECORD_BRIEF, "permission_mode": "read_only"},
}


def _chat_record(brief: str | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"source_object_id": TEMPLATE_ID}
    if brief is not None:
        spec["metadata"] = {"template_brief": brief}
    return {"id": CHAT, "type": "chat", "title": "Weekly mentions", "version": 1, "spec": spec}


class Drive:
    """The two object reads a starting chat makes, each answerable on its own.

    ``template_status`` is what the drive says about the TEMPLATE — a private
    one answers 404 for this operator — and the chat's own record is answered
    regardless, because the box always holds the chat it publishes.
    """

    def __init__(self, *, brief: str | None = COPIED_BRIEF, template_status: int = 200) -> None:
        self.brief = brief
        self.template_status = template_status
        self.paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            if request.url.path.endswith(f"/objects/{TEMPLATE_ID}"):
                if self.template_status != 200:
                    return httpx.Response(
                        self.template_status, json={"error": {"code": "not_found"}}
                    )
                return httpx.Response(200, json=TEMPLATE_RECORD)
            if request.url.path.endswith(f"/objects/{CHAT}"):
                return httpx.Response(200, json=_chat_record(self.brief))
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        return httpx.MockTransport(handle)


def _mirror(tmp_path: Path, drive: Drive, *, source: str | None = TEMPLATE_ID) -> Any:
    service, _built = build_service(tmp_path, clock=Clock())
    chat: dict[str, Any] = {"title": "Weekly mentions", "owner_user_id": "u-1"}
    if source is not None:
        chat["source_object_id"] = source
    mirror = service._default_mirror(CHAT, chat)
    mirror._rest = CloudRestClient(
        api_url="http://127.0.0.1:1", token="t", agent_id=CHAT, transport=drive.transport()
    )
    return mirror


def _write_notes(mirror: Any, text: str) -> Path:
    path = mirror.working_dir / TEMPLATE_NOTES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# What may be a source at all
# ---------------------------------------------------------------------------


def test_a_chat_template_is_what_a_chat_starts_from() -> None:
    """The template's record answers the one question the box asks of it: what
    to call it. Its own brief is not read — the chat carries the copy."""
    document = source_document(TEMPLATE_RECORD)

    assert document == {
        "type": "chat_template",
        "object": {"id": TEMPLATE_ID, "title": "Weekly mentions", "version": 3},
    }


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(None, id="nothing came back"),
        pytest.param({"type": "query", "spec": {}}, id="a saved query is not a starting point"),
        pytest.param({"type": "report", "spec": {}}, id="a saved report is not one either"),
        pytest.param({"type": "result", "spec": {}}, id="a frozen result was never one"),
        pytest.param({"type": "chat", "spec": {}}, id="a chat is not a template"),
        pytest.param({"type": "chat_templates"}, id="a near-miss is not a match"),
        pytest.param("chat_template", id="not a record at all"),
    ],
)
def test_only_a_chat_template_produces_a_document(record: Any) -> None:
    """A query or a report could once be started from and no longer can. Naming
    one as a template would put a replication spec in front of the agent under a
    header promising somebody's brief."""
    assert source_document(record) is None


# ---------------------------------------------------------------------------
# What the block says
# ---------------------------------------------------------------------------


def test_the_header_names_the_template_and_says_who_wrote_the_text() -> None:
    """The quoted text is a stranger's writing reaching a session the user
    opened. It is framed as that, and as something to ask about first —
    otherwise a template becomes an instruction the user never gave."""
    block = source_brief(source_document(TEMPLATE_RECORD), brief=COPIED_BRIEF)

    assert block is not None
    assert "the chat template Weekly mentions" in block
    assert "written by the template's author, not by the user" in block
    assert "ASK the user what should differ this time" in block
    assert COPIED_BRIEF in block


def test_a_template_the_box_could_not_name_still_hands_over_the_brief() -> None:
    """The name is a courtesy; the brief is the substance. A template the drive
    would not show this operator must not take the brief down with it."""
    block = source_brief(None, brief=COPIED_BRIEF)

    assert block is not None
    assert "started FROM a chat template" in block
    assert "written by the template's author, not by the user" in block
    assert COPIED_BRIEF in block


@pytest.mark.parametrize(
    ("brief", "notes"),
    [
        pytest.param("", None, id="nothing at all"),
        pytest.param("   \n ", None, id="a brief of whitespace"),
        pytest.param("", "  ", id="notes of whitespace"),
    ],
)
def test_nothing_to_quote_produces_no_block(brief: str, notes: str | None) -> None:
    """An empty "here is your template" preamble is worse than silence: the
    agent would act as though it had been handed one."""
    assert source_brief(source_document(TEMPLATE_RECORD), brief=brief, notes=notes) is None


# ---------------------------------------------------------------------------
# The author's notes beside the files
# ---------------------------------------------------------------------------


def test_the_authors_notes_ride_with_the_brief(tmp_path: Path) -> None:
    """``TEMPLATE.md`` is where a template's author writes what re-running
    needs — which file does what, and every question to ask first. It is beside
    the copied files, so it reaches the agent from disk rather than from the
    template's record."""
    (tmp_path / TEMPLATE_NOTES_FILE).write_text("## Ask first\n- which region?\n", encoding="utf-8")

    block = source_brief(
        source_document(TEMPLATE_RECORD), brief=COPIED_BRIEF, notes=template_notes(tmp_path)
    )

    assert block is not None
    assert f"--- {TEMPLATE_NOTES_FILE} (from the template's files, written by its author) ---" in (
        block
    )
    assert "- which region?" in block
    assert block.index(COPIED_BRIEF) < block.index("- which region?")


def test_no_notes_beside_the_files_is_no_section(tmp_path: Path) -> None:
    """Most templates have none. The block must not grow an empty heading."""
    assert template_notes(tmp_path) is None

    block = source_brief(source_document(TEMPLATE_RECORD), brief=COPIED_BRIEF)

    assert block is not None
    assert TEMPLATE_NOTES_FILE not in block


def test_long_notes_are_cut_so_they_cannot_eat_the_conversation(tmp_path: Path) -> None:
    """A file somebody pasted a dataset into must not spend the context window
    before the first question is asked. The head is kept, the tail is gone, and
    the cut is stated rather than silent."""
    body = "keep me\n" + ("x" * 32_000) + "\nDROP THIS LAST LINE\n"
    (tmp_path / TEMPLATE_NOTES_FILE).write_text(body, encoding="utf-8")

    notes = template_notes(tmp_path)

    assert notes is not None
    assert notes.startswith("keep me")
    assert "DROP THIS LAST LINE" not in notes
    assert "truncated at 16 KiB" in notes
    assert len(notes.encode("utf-8")) <= MAX_TEMPLATE_NOTES_BYTES + 64


def test_a_link_named_like_the_notes_is_not_followed(tmp_path: Path) -> None:
    """The copied files came off somebody else's drive. A symlink filed under
    the name would otherwise read whatever it points at straight into a
    prompt."""
    secret = tmp_path / "credentials.yml"
    secret.write_text("token: super-secret\n", encoding="utf-8")
    (tmp_path / TEMPLATE_NOTES_FILE).symlink_to(secret)

    assert template_notes(tmp_path) is None


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda p: None, id="no working directory on disk yet"),
        pytest.param(lambda p: (p / TEMPLATE_NOTES_FILE).mkdir(), id="a directory by that name"),
        pytest.param(lambda p: (p / TEMPLATE_NOTES_FILE).write_text(""), id="an empty file"),
    ],
)
def test_notes_that_are_not_a_file_of_prose_are_no_notes(tmp_path: Path, make: Any) -> None:
    make(tmp_path)

    assert template_notes(tmp_path) is None


# ---------------------------------------------------------------------------
# What the box reads, and from where
# ---------------------------------------------------------------------------


async def test_the_brief_comes_off_the_chats_own_record_not_the_templates(tmp_path: Path) -> None:
    """The copy on the chat is what the box quotes. A template belongs to
    whoever saved it, and the operator whose credential this box holds may hold
    no rung on a colleague's private one — a brief the box had to fetch would
    vanish exactly when the template was somebody else's."""
    drive = Drive()
    mirror = _mirror(tmp_path, drive)

    await mirror._load_source_brief()

    block = mirror._source_brief
    assert block is not None
    assert COPIED_BRIEF in block
    assert TEMPLATE_RECORD_BRIEF not in block
    assert f"/api/v1/objects/{CHAT}" in drive.paths


async def test_a_template_the_drive_refuses_costs_its_name_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The private-template case end to end: the template read is refused, the
    chat still opens with the author's words in front of the agent."""
    drive = Drive(template_status=404)
    mirror = _mirror(tmp_path, drive)

    await mirror._load_source_brief()

    block = mirror._source_brief
    assert block is not None
    assert COPIED_BRIEF in block
    assert "Weekly mentions" not in block


async def test_the_notes_the_template_copied_in_reach_the_first_turn(tmp_path: Path) -> None:
    """The files are already in the chat's working directory when it opens —
    the box copied them before the mirror started — so the notes are read from
    there, not fetched."""
    drive = Drive()
    mirror = _mirror(tmp_path, drive)
    _write_notes(mirror, "## Ask first\n- which region?\n")

    await mirror._load_source_brief()

    block = mirror._source_brief
    assert block is not None
    assert "- which region?" in block


async def test_a_chat_whose_source_left_no_brief_is_handed_nothing(tmp_path: Path) -> None:
    """A chat started from a saved query or report — the shapes that are no
    longer starting points — carries no copied brief and no template notes.
    Rather than a header over nothing, it opens as a chat started from
    nothing."""
    drive = Drive(brief=None)
    mirror = _mirror(tmp_path, drive)

    await mirror._load_source_brief()

    assert mirror._source_brief is None


async def test_a_chat_that_was_started_from_nothing_fetches_nothing(tmp_path: Path) -> None:
    """The overwhelmingly common chat names no source. It must not cost a
    round-trip on every open to discover that."""
    drive = Drive()
    mirror = _mirror(tmp_path, drive, source=None)

    await mirror._load_source_brief()

    assert drive.paths == []
    assert mirror._source_brief is None


async def test_the_block_rides_the_first_turn_and_is_not_repeated(tmp_path: Path) -> None:
    """The agent needs it once. A chat thirty turns into the work must not be
    re-reading the template's brief on every message — and must not keep being
    told to ask the questions it already asked."""
    drive = Drive()
    mirror = _mirror(tmp_path, drive)
    await mirror._load_source_brief()

    first, mirror._source_brief = mirror._source_brief, None

    assert first is not None and COPIED_BRIEF in first
    assert mirror._source_brief is None
