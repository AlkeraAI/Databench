"""The report page a turn hands over, proved against a real agent subprocess.

The guidance a chat session carries tells the model three things at once: write
into the chat's working directory and nowhere else, keep a page's assets in one
subfolder beside it, and name the page in the final message as a link relative
to that directory. Nothing below asserts the wording — that is pinned where the
wording lives. What is proved here is that the three fit together on a real
agent: the writes the model makes the way the guidance spells them actually
land, the subfolder is admitted by the write fence rather than refused, nobody
is asked to approve any of it, and the two references the reply carries resolve
to the files that were written.

Two subprocesses, two tiers, no API spend:

``opencode_e2e`` drives a REAL bun-driven opencode against a scripted mock
provider. It answers the behavioural half — a turn that writes
``q3-report.html`` and ``charts/q3.png`` into the working directory and replies
with ``[Q3 report](q3-report.html)`` and ``![Chart](charts/q3.png)``.

``claude_e2e`` drives a REAL ``claude`` binary against a scripted mock Anthropic
server. That binary verifies model access against a real key before it will run
a turn's content, so on the mock it never reaches the provider at all and no
scripted tool call can be made to happen — the same limit every other
``claude_e2e`` case works within. What it does carry is the delivery seam: the
composed working-directory guidance is handed to the real subprocess as the
turn's per-turn system text, and the turn still drives the IR pipeline to
closure. Whether a real model acts on that guidance is the live tier's claim
(the report-page live test in the product's suite).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from _helpers.claude_runner import claude_e2e_adapter
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_anthropic_server import text_events
from _mocks.mock_openai_server import (
    FOLLOWUP_KEY,
    parallel_tool_call_chunks,
    text_chunks,
)
from alkera_cli.cloud import fence
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.system_prompt import compose_main_agent_guidance
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    MessageCreated,
    PartCreated,
    PartStarted,
    PermissionRequest,
    SessionStatusChanged,
    TextPart,
)

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: The two names the guidance teaches: the page at the top of the working
#: directory, its one chart in a single subfolder beside it.
PAGE = "q3-report.html"
CHART = "charts/q3.png"

#: A page shaped the way the deliverable guidance asks for: its style inline,
#: its only asset named by a relative path. Written as text by the agent's own
#: ``write`` tool, which is how a model produces one.
PAGE_HTML = (
    "<!doctype html><title>Q3</title><style>body{font:16px/1.4 sans-serif}</style>"
    f'<h1>Q3 revenue</h1><img src="{CHART}" alt="Revenue by month">'
)
#: The chart's bytes. The tool that writes it is a text tool, so this stands in
#: for the encoded image; what the test is about is where it lands and how the
#: reply points at it, neither of which reads a pixel.
CHART_BYTES = "PNG-PLACEHOLDER-8f21"

REPLY = f"Here it is: [Q3 report]({PAGE})\n\n![Chart]({CHART})"

#: ``[label](target)`` and ``![label](target)``, with the leading ``!`` kept so
#: an image reference can be told from a link.
_REFERENCE = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)\)")


def _references(text: str) -> list[tuple[bool, str]]:
    """``(is_image, target)`` for every markdown reference in ``text``."""
    return [(bang == "!", target) for bang, target in _REFERENCE.findall(text)]


def _write_script() -> dict[str, Any]:
    """One assistant turn writing both files the way the guidance spells them,
    then a plain reply naming them. Both writes ride a single turn so the
    script needs no ordering beyond "the tool ran, now answer" — an auxiliary
    request opencode fires on its own cannot desynchronize it."""
    return {
        "*": parallel_tool_call_chunks(
            [
                ("write", {"filePath": PAGE, "content": PAGE_HTML}),
                ("write", {"filePath": CHART, "content": CHART_BYTES}),
            ]
        ),
        FOLLOWUP_KEY: text_chunks(REPLY),
    }


def _recording_broker(prompts: list[PermissionRequest]) -> PermissionBroker:
    """A human broker that records every ask and then allows it, so a wrongly
    prompted write still lets the turn finish and the test fails on the record
    rather than on a timeout."""

    async def _resolve(request: PermissionRequest) -> str:
        prompts.append(request)
        return "allow_once"

    return PermissionBroker(_resolve, default_timeout_seconds=30.0)


async def _drain(sub: Any, *, budget_seconds: float = 120.0) -> list[Event]:
    """One whole turn: wait for ``running``, return on the next terminal."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out
        try:
            async with asyncio.timeout(remaining):
                event: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out
        out.append(event)
        if isinstance(event, SessionStatusChanged):
            if event.status == "running":
                seen_running = True
            elif event.status in ("idle", "error") and seen_running:
                return out


def _assistant_text(events: list[Event]) -> str:
    return "".join(
        event.part.text
        for event in events
        if isinstance(event, PartCreated) and isinstance(event.part, TextPart)
    )


def _statuses(events: list[Event]) -> list[str]:
    return [e.status for e in events if isinstance(e, SessionStatusChanged)]


# ---------------------------------------------------------------------------
# opencode: the turn that produces the page
# ---------------------------------------------------------------------------


@pytest.mark.opencode_e2e
@pytest.mark.asyncio
async def test_the_page_and_its_chart_land_where_the_reply_says_they_are(
    tmp_path: Path,
) -> None:
    """The whole hand-over, end to end on a real opencode subprocess.

    The model writes the two files the way the guidance spells them — a bare
    name for the page, one subfolder for its chart — and names them in the
    reply. Every claim below is checked against something the subprocess
    actually did: the bytes on disk, the directory it had to create, the asks
    the broker was handed, and the targets the reply carries resolved against
    the working directory.
    """
    project = ProjectDirectory(tmp_path / ".alkera")
    async with opencode_e2e_runtime(tmp_path, mock_script=_write_script()) as (
        runtime,
        sid,
        _server,
    ):
        # A cloud chat runs in a child of its own folder and calls that child
        # both its working directory and its sandbox, so the fence, the gate
        # and the brief all name one place. The directory has to exist before
        # the agent is pointed at it.
        working_dir = project.chats_path / sid / "scratch"
        working_dir.mkdir(parents=True, exist_ok=True)
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(
            sid,
            permission_broker=_recording_broker(prompts),
            working_dir=working_dir,
            sandbox_dir=working_dir,
        )
        sub = session.subscribe()
        await session.send_prompt("write up Q3 as a one-page report", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"turn never completed (statuses={statuses})"
    assert "error" not in statuses, f"turn errored (statuses={statuses})"

    page_path = working_dir / PAGE
    chart_path = working_dir / CHART
    landed = sorted(p.name for p in working_dir.iterdir())
    assert page_path.is_file(), f"the page never landed (the working directory holds {landed})"
    assert page_path.read_text().startswith("<!doctype html>"), "the page's bytes are not the page"
    assert chart_path.parent.is_dir(), "the assets subfolder was never created"
    assert chart_path.read_text().strip() == CHART_BYTES, "the chart's bytes never landed"

    # Nothing here was anybody's to approve: a write into the working directory
    # is admitted in every mode, and a page whose asset sits one level down is
    # still a write into the working directory.
    assert prompts == [], f"a write into the chat's own directory was put to a human: {prompts}"

    # The reply points at exactly those two files, in the two forms the
    # transcript renders — an image reference for the chart, a link for the
    # page — and each target resolves, relative to the working directory, to
    # the file that was just written.
    references = _references(_assistant_text(events))
    assert (False, PAGE) in references, f"the page was never named as a link (refs={references})"
    assert (True, CHART) in references, f"the chart was never named as an image (refs={references})"
    for _is_image, target in references:
        assert (working_dir / target).is_file(), (
            f"{target!r} names no file in the working directory"
        )


@pytest.mark.opencode_e2e
@pytest.mark.asyncio
async def test_the_transcript_carries_relative_names_not_the_boxs_paths(
    tmp_path: Path,
) -> None:
    """What the reader's shell is handed has to be resolvable on the reader's
    side: a path relative to the chat's working directory, never the absolute
    path the box happened to run at. An absolute path, a URL or a climb out of
    the folder is text in the transcript rather than a file the panel can open,
    so a reply spelling one of those hands the reader nothing."""
    project = ProjectDirectory(tmp_path / ".alkera")
    async with opencode_e2e_runtime(tmp_path, mock_script=_write_script()) as (
        runtime,
        sid,
        _server,
    ):
        working_dir = project.chats_path / sid / "scratch"
        working_dir.mkdir(parents=True, exist_ok=True)
        session = await runtime.open_chat(
            sid,
            permission_broker=_recording_broker([]),
            working_dir=working_dir,
            sandbox_dir=working_dir,
        )
        sub = session.subscribe()
        await session.send_prompt("write up Q3 as a one-page report", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    text = _assistant_text(events)
    references = _references(text)
    assert references, f"the reply named no file at all: {text!r}"
    assert str(working_dir) not in text, "the reply leaked the box's own absolute path"
    for _is_image, target in references:
        assert not target.startswith("/"), f"{target!r} is absolute"
        assert "://" not in target, f"{target!r} is a URL, which the transcript will not load"
        assert ".." not in target.split("/"), f"{target!r} climbs out of the chat folder"


# ---------------------------------------------------------------------------
# the write fence: one subfolder is inside, the chat's records are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "escapes"),
    [
        pytest.param(PAGE, False, id="the-page-at-the-top"),
        pytest.param(CHART, False, id="one-subfolder-for-its-assets"),
        pytest.param("assets/fonts/inter.woff2", False, id="deeper-still-is-inside"),
        pytest.param("../notes.md", False, id="the-chat-folder-itself-is-inside"),
        pytest.param("../manifest.json", True, id="the-chats-manifest-is-the-boxs"),
        pytest.param("../.runtime/state.json", True, id="the-harness-state-is-the-boxs"),
        pytest.param("../../q3-report.html", True, id="one-level-above-the-folder-is-out"),
        pytest.param("/etc/hosts", True, id="an-absolute-path-is-out"),
        pytest.param("charts/*.png", True, id="a-glob-names-no-destination"),
    ],
)
def test_the_fence_admits_a_pages_assets_and_still_keeps_the_records(
    tmp_path: Path, destination: str, escapes: bool
) -> None:
    """The fence is what makes "one subfolder for a page's assets" safe to
    teach: a name resolved against the working directory is judged against the
    chat folder around it, so a subfolder is inside, the chat folder itself is
    inside, and the records the box owns at its top stay the box's however the
    destination is spelled."""
    chat_folder = tmp_path / "chat"
    working_dir = chat_folder / "scratch"
    working_dir.mkdir(parents=True)

    assert fence.write_escapes(destination, folder=chat_folder, base=working_dir) is escapes


# ---------------------------------------------------------------------------
# claude: the guidance reaches the other subprocess
# ---------------------------------------------------------------------------


@pytest.mark.claude_e2e
@pytest.mark.asyncio
async def test_a_real_claude_turn_carries_the_working_directory_guidance(
    tmp_path: Path,
) -> None:
    """The delivery seam on the other harness, against the real binary.

    The guidance a chat composes for its working directory is several thousand
    characters naming an absolute path; it reaches a Claude session only through
    the per-turn system channel (that harness has no place to put a standing
    project instruction). A turn carrying it has to survive the real subprocess
    intact — an assistant message, finalized text, every opened part closed —
    which is what a broken or over-long steering block would take out. Whether
    the model then writes the page is the live tier's claim: this binary never
    reaches the mock provider, so no scripted turn can answer it here.
    """
    working_dir = tmp_path / "chat" / "scratch"
    working_dir.mkdir(parents=True)
    guidance = compose_main_agent_guidance(sandbox_dir=str(working_dir))
    # The block is only worth delivering if it actually names this chat's
    # directory and the hand-over form; the composition is what the adapter is
    # handed, so a session with no sandbox would be sending nothing useful.
    assert str(working_dir) in guidance
    assert f"[Q3 report]({PAGE})" in guidance

    async with claude_e2e_adapter(tmp_path, mock_script={"*": text_events("ok")}) as (
        adapter,
        _server,
    ):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="write up Q3 as a report", system=guidance))
        events = await _drain(sub)

    assert any(isinstance(e, MessageCreated) and e.role == "assistant" for e in events), (
        f"the steered turn produced no assistant message (statuses={_statuses(events)})"
    )
    finalized = [e for e in events if isinstance(e, PartCreated) and isinstance(e.part, TextPart)]
    assert any(part.part.text for part in finalized), "the steered turn produced no text at all"
    opened = {e.part_id for e in events if isinstance(e, PartStarted)}
    closed = {e.part.part_id for e in events if isinstance(e, PartCreated)}
    assert opened <= closed, f"the steered turn left parts open: {opened - closed}"
