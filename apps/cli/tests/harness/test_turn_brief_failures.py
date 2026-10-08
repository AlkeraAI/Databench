"""A turn brief that cannot be read leaves the turn without it, never without
the person's message.

The notebooks brief asks the box's notebook host for what others did since the
last turn. Right after a box restarts, that host answers "This machine is
reconnecting to the workspace's files" until the folder is taken again, and
the raise used to fail the whole prompt before the agent saw it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness import turn_briefs as turn_briefs_module
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_notebook.tools.port import NotebookToolError


async def _reconnecting(*_args: Any) -> str | None:
    raise NotebookToolError("unavailable", "This machine is reconnecting to the workspace's files.")


async def _digest(*_args: Any) -> str | None:
    return "Someone ran cell 3 in report.py."


@pytest.mark.parametrize(
    ("digest", "brief_in_system"),
    [
        pytest.param(_reconnecting, False, id="a-brief-that-raises-is-left-out"),
        pytest.param(_digest, True, id="a-brief-that-answers-rides-the-turn"),
    ],
)
async def test_the_message_reaches_the_agent_whatever_a_brief_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: Any, brief_in_system: bool
) -> None:
    monkeypatch.setattr(turn_briefs_module, "notebook_digest", digest)
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    session_id = chat.session_id
    chat.close()
    session = await runtime.open_chat(session_id)
    try:
        await session.send_prompt("what changed?")

        (sent,) = factory.adapters[-1].sent_prompts
        assert sent.text == "what changed?"
        assert ("Someone ran cell 3 in report.py." in (sent.system or "")) is brief_in_system
    finally:
        await runtime.close_chat(session_id)
